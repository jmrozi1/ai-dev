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

    def test_successful_correlation_and_bounded_render(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._write("terminal.jsonl", [{"timestamp": 1.2, "command": "python -c 'print(1)'", "status": 0, "durationMs": 25}]), start=1.0, end=1.5)

        self.assertEqual(debug["status"], "validated")
        self.assertEqual(otel["inputTokens"]["value"], 10)
        self.assertEqual(terminal["count"]["value"], 1)
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
        terminal = parse_terminal_diagnostics(self._write("bad-terminal.jsonl", [{"bad": True}]))
        self.assertEqual(terminal["status"], "error: unexpected log format")

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


if __name__ == "__main__":
    unittest.main()
