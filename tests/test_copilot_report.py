from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.copilot_report import (
    merge_intervals,
    parse_agent_debug,
    parse_otel,
    parse_terminal_diagnostics,
    render_copilot_report,
)


class CopilotReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = str(self.root / "repo")
        Path(self.repo).mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, name: str, records: list[dict[str, object]]) -> Path:
        path = self.root / name
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        return path

    def _debug(self, *, complete: bool = True, repo: str | None = None) -> Path:
        repo = repo or self.repo
        records = [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "do work", "repository": repo}},
            {"ts": 1100, "dur": 10, "sid": "session-1", "type": "turn_start", "attrs": {"turnId": "turn-1"}},
            {"ts": 1200, "dur": 10, "sid": "session-1", "type": "agent_response", "attrs": {"response": "done"}},
        ]
        if complete:
            records.append({"ts": 1300, "sid": "session-1", "type": "turn_end", "attrs": {"turnId": "turn-1"}})
        return self._write("debug.jsonl", records)

    def _otel(self, *, session: str = "session-1", tokens: tuple[int, int] = (10, 2)) -> Path:
        resource = {"_rawAttributes": [["session.id", session]]}
        return self._write(
            "otel.jsonl",
            [
                {"hrTime": [1, 100000000], "resource": resource, "attributes": {"event.name": "copilot_chat.agent.turn", "turn.index": 0, "gen_ai.usage.input_tokens": tokens[0], "gen_ai.usage.output_tokens": tokens[1]}},
                {"hrTime": [1, 200000000], "resource": resource, "attributes": {"event.name": "copilot_chat.tool.call", "gen_ai.tool.name": "terminal", "success": True, "duration_ms": 25}},
                {"hrTime": [1, 300000000], "resource": resource, "attributes": {"event.name": "gen_ai.client.inference.operation.details", "gen_ai.request.model": "gpt-test", "gen_ai.usage.input_tokens": tokens[0], "gen_ai.usage.output_tokens": tokens[1]}},
            ],
        )

    def _terminal_log(self, lines: list[str]) -> Path:
        path = self.root / "terminal.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_successful_correlation_and_bounded_render(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python -c 'print(1)'\"]]",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))

        self.assertEqual(debug["status"], "validated")
        self.assertEqual(otel["inputTokens"]["value"], 10)
        self.assertEqual(terminal["terminalRequestCount"]["value"], 1)
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Issue: 49", report)
        self.assertIn("Prompt: do work", report)
        self.assertIn("Tokens: 10 input, 2 output", report)

    def test_genuine_zero_and_overlap_are_validated(self) -> None:
        otel = parse_otel(self._otel(tokens=(0, 0)), session="session-1", start=1.0, end=1.5)
        self.assertEqual(otel["inputTokens"], {"status": "validated", "value": 0})
        self.assertEqual(merge_intervals([(0, 1), (0.5, 2), (3, 3)]), {"status": "validated", "value": 2.0})

    def test_missing_and_malformed_sources_fail_closed(self) -> None:
        missing = parse_agent_debug(self.root / "missing.jsonl", self.repo)
        malformed = parse_agent_debug(self._write("bad.jsonl", [{"type": "user_message"}]), self.repo)
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(malformed["status"], "unavailable")
        self.assertEqual(parse_otel(self._write("bad-otel.jsonl", [{"attributes": {"event.name": "bad"}}]), session="x", start=0, end=2)["status"], "unavailable")
        terminal = parse_terminal_diagnostics(self._terminal_log(["not a terminal record"]))
        self.assertEqual(terminal["status"], "unavailable")

    def test_incomplete_and_partial_correlation_are_not_totals(self) -> None:
        self.assertEqual(parse_agent_debug(self._debug(complete=False), self.repo)["status"], "unavailable")
        self.assertEqual(parse_agent_debug(self._debug(repo="/other"), self.repo)["status"], "unavailable")
        self.assertEqual(parse_otel(self._otel(session="other"), session="session-1", start=1.0, end=1.5)["status"], "unavailable")

    def test_unexpected_otel_format_and_long_content(self) -> None:
        bad = self._write("bad-shape.jsonl", [{"hrTime": [1, 1], "resource": {"_rawAttributes": [["session.id", "s"]]}, "attributes": {"event.name": "gen_ai.client.inference.operation.details", "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": "zero", "gen_ai.usage.output_tokens": 1}}])
        result = parse_otel(bad, session="s", start=0, end=2)
        self.assertEqual(result["status"], "error: unexpected log format")
        long_debug = self._debug()
        lines = long_debug.read_text(encoding="utf-8").splitlines()
        records = json.loads(lines[0])
        records["attrs"]["content"] = "x" * 1000
        long_debug.write_text("\n".join([json.dumps(records), *lines[1:]]), encoding="utf-8")
        result = parse_agent_debug(long_debug, self.repo)
        self.assertTrue(result["prompt"]["truncated"])
        self.assertEqual(result["prompt"]["length"], 1000)

    def test_plaintext_terminal_approval_correlation_and_wait(self) -> None:
        log = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"chmod --version\"]]",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:00.011 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'chmod --version' has no matching auto approve entries",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command ` chmod --version` []",
            "2026-08-21 14:00:02.600 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `42`, error `undefined` []",
        ])
        result = parse_terminal_diagnostics(log, start= datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        self.assertEqual(result["status"], "validated")
        request = result["approvalRequests"]["value"][0]
        self.assertEqual(request["command"], "chmod --version")
        self.assertEqual(request["disposition"], "executed")
        self.assertAlmostEqual(request["approvalWaitSeconds"], 2.5)

    def test_plaintext_terminal_zero_duplicate_unresolved_and_malformed(self) -> None:
        empty = self._terminal_log(["2026-08-21 14:00:00.000 [info] unrelated diagnostic []"])
        self.assertEqual(parse_terminal_diagnostics(empty, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))["status"], "unavailable")
        duplicate = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"chmod --version\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"chmod --version\"]]",
            "2026-08-21 14:00:00.002 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
        ])
        result = parse_terminal_diagnostics(duplicate, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(result["terminalApprovalRequestCount"]["value"], 1)
        self.assertEqual(result["approvalRequests"]["value"][0]["disposition"], "unresolved")
        malformed = self._terminal_log(["2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [not-json]"])
        self.assertEqual(parse_terminal_diagnostics(malformed, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))["status"], "error: unexpected log format")

    def test_auto_approved_commands_do_not_create_approval_wait(self) -> None:
        log = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python --version\"]]",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: All sub-commands auto-approved",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command ` python --version` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        self.assertEqual(result["terminalRequestCount"]["value"], 1)
        self.assertEqual(result["terminalApprovalRequestCount"]["value"], 0)
        self.assertEqual(result["approvalWaitSeconds"]["value"], 0)

    def test_renderer_includes_bounded_approval_and_timing_sections(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"chmod --version\"]]",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:00.011 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'chmod --version' has no matching auto approve entries",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command ` chmod --version` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Approvals: 1 terminal approval requests", report)
        self.assertIn("Approval timing: 2.5 seconds validated", report)
        self.assertIn("approval wait 2.500s", report)


def datetime_timestamp(value: str) -> float:
    from datetime import datetime
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()


if __name__ == "__main__":
    unittest.main()
