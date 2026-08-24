from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.copilot_report import (
    merge_intervals,
    parse_agent_debug,
    parse_agent_debug_files,
    parse_otel,
    parse_terminal_diagnostics,
    render_copilot_report,
    render_latest_copilot_report,
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

        other_session = parse_otel(self._otel(session="other"), session="session-1", start=1.0, end=1.5)
        self.assertEqual(other_session["status"], "partial")
        self.assertIn("session unmatched", other_session["detail"])
        self.assertIn("3 in-window records", other_session["detail"])
        self.assertNotIn("0", str(other_session.get("inputTokens", {}).get("value", "")))

        no_window = parse_otel(self._otel(), session="session-1", start=10.0, end=20.0)
        self.assertEqual(no_window["status"], "partial")
        self.assertIn("no in-window records", no_window["detail"])
        self.assertIn("3 recognized records", no_window["detail"])
        self.assertNotIn("0", str(no_window.get("inputTokens", {}).get("value", "")))

    def test_valid_otel_sessions_remain_validated_and_exact_match_keeps_totals(self) -> None:
        result = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["inputTokens"]["value"], 10)
        self.assertEqual(result["outputTokens"]["value"], 2)
        self.assertEqual(result["models"]["value"], {"gpt-test": 1})

    def test_source_health_states_are_explicit_for_missing_and_malformed_otel(self) -> None:
        self.assertEqual(parse_otel(self.root / "missing-otel.jsonl", session="session-1", start=1.0, end=1.5)["status"], "unavailable")
        malformed = self._write("bad-shape-otel.jsonl", [{"hrTime": [1, 1], "resource": {"_rawAttributes": [["session.id", "session-1"]]}, "attributes": {"event.name": "gen_ai.client.inference.operation.details", "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": "zero", "gen_ai.usage.output_tokens": 1}}])
        self.assertEqual(parse_otel(malformed, session="session-1", start=0, end=2)["status"], "error: unexpected log format")

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

    def test_structured_assistant_parts_become_readable_text(self) -> None:
        debug = self._write("structured-response.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "plain prompt", "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": [{"role": "assistant", "parts": [{"type": "text", "content": "readable outcome"}]}]}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ])
        result = parse_agent_debug(debug, self.repo)
        self.assertEqual(result["finalResponse"]["value"], "readable outcome")

    def test_json_string_structured_content_becomes_readable_text(self) -> None:
        debug = self._write("json-string-content.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": '{"role":"user","parts":[{"type":"text","content":"structured prompt"}]}', "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": '[{"role":"assistant","parts":[{"type":"text","content":"structured outcome"}]}]'}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ])
        result = parse_agent_debug(debug, self.repo)
        self.assertEqual(result["prompt"]["value"], "structured prompt")
        self.assertEqual(result["finalResponse"]["value"], "structured outcome")

    def test_attachment_only_prompt_without_accepted_content_is_partial(self) -> None:
        debug = self._write("attachment-only.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "#attachment:Pasted text #1", "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": "done"}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ])
        result = parse_agent_debug(debug, self.repo)
        self.assertEqual(result["prompt"]["status"], "partial")
        self.assertIn("#attachment:Pasted text #1", result["prompt"]["value"])
        self.assertIn("content unavailable", result["prompt"]["value"])

    def test_plaintext_prompt_and_outcome_remain_unchanged(self) -> None:
        result = parse_agent_debug(self._debug(), self.repo)
        self.assertEqual(result["prompt"]["value"], "do work")
        self.assertEqual(result["finalResponse"]["value"], "done")

    def test_renderer_labels_partial_prompt(self) -> None:
        debug = parse_agent_debug(self._write("partial-prompt.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "#attachment:Pasted text #1", "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": "done"}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ]), self.repo)
        report = render_copilot_report(agent_debug=debug, otel={"source": "otel", "status": "unavailable"})
        self.assertIn("Prompt [partial]: #attachment:Pasted text #1 (content unavailable)", report)

    def test_renderer_exposes_bounded_otel_detail_for_partial_and_error_states(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        session_unmatched = parse_otel(self._otel(session="other"), session="session-1", start=1.0, end=1.5)
        report = render_copilot_report(agent_debug=debug, otel=session_unmatched)
        self.assertIn("otel: partial - validated source; session unmatched", report)
        self.assertIn("3 in-window records", report)

        no_window = parse_otel(self._otel(), session="session-1", start=10.0, end=20.0)
        report = render_copilot_report(agent_debug=debug, otel=no_window)
        self.assertIn("otel: partial - validated source; no in-window records", report)
        self.assertIn("3 recognized records", report)

        validated = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        compact = render_copilot_report(agent_debug=debug, otel=validated)
        self.assertIn("otel: validated", compact)
        self.assertNotIn("otel: validated -", compact)

        missing_detail = render_copilot_report(agent_debug=debug, otel={"source": "otel", "status": "partial"})
        self.assertIn("otel: partial", missing_detail)
        self.assertNotIn("otel: partial -", missing_detail)

        error_detail = parse_otel(self._write("bad-shape-otel.jsonl", [{"hrTime": [1, 1], "resource": {"_rawAttributes": [["session.id", "session-1"]]}, "attributes": {"event.name": "gen_ai.client.inference.operation.details", "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": "zero", "gen_ai.usage.output_tokens": 1}}]), session="session-1", start=0, end=2)
        bad_report = render_copilot_report(agent_debug=debug, otel=error_detail)
        self.assertIn("otel: error: unexpected log format -", bad_report)
        self.assertIn("inference record missing model/tokens", bad_report)

        unavailable = render_copilot_report(agent_debug=debug, otel={"source": "otel", "status": "unavailable", "detail": "validated source is empty"})
        self.assertIn("otel: unavailable - validated source is empty", unavailable)

    def test_renderer_marks_truncated_final_outcome_with_metadata(self) -> None:
        outcome = "outcome " * 100
        debug = parse_agent_debug(self._write("truncated-outcome.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "plain prompt", "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": outcome}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ]), self.repo)
        report = render_copilot_report(agent_debug=debug, otel={"source": "otel", "status": "unavailable"})
        self.assertIn("Final outcome:", report)
        self.assertIn("truncated; length 800; sha256 ", report)
        self.assertIn(debug["finalResponse"]["sha256"], report)

    def test_renderer_keeps_complete_content_compact(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        report = render_copilot_report(agent_debug=debug, otel={"source": "otel", "status": "unavailable"})
        self.assertIn("Prompt: do work", report)
        self.assertIn("Final outcome: done", report)
        self.assertNotIn("truncated;", report)

    def test_malformed_structured_content_fails_closed_without_crashing(self) -> None:
        debug = self._write("malformed-structured.jsonl", [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "[{\"role\":", "repository": self.repo}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": "[{\"role\":"}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {}},
        ])
        result = parse_agent_debug(debug, self.repo)
        self.assertEqual(result["prompt"]["status"], "unavailable")
        self.assertEqual(result["finalResponse"]["status"], "unavailable")

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

    def _global_debug_file(self, root: Path, name: str, records: list[dict[str, object]]) -> Path:
        path = root / name / "GitHub.copilot-chat" / "debug-logs" / "session" / "main.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        return path

    def _turn_records(self, session: str, prompt: str, outcome: str, end: int, *, complete: bool = True) -> list[dict[str, object]]:
        records: list[dict[str, object]] = [
            {"ts": end - 20, "sid": session, "type": "user_message", "attrs": {"content": prompt, "repository": self.repo}},
            {"ts": end - 10, "sid": session, "type": "agent_response", "attrs": {"response": outcome}},
        ]
        if complete:
            records.append({"ts": end, "sid": session, "type": "turn_end", "attrs": {}})
        return records

    def test_global_selection_uses_completion_timestamp_over_file_mtime(self) -> None:
        old = self._global_debug_file(self.root / "mtime", "newer-file", self._turn_records("old", "old prompt", "old outcome", 100))
        new = self._global_debug_file(self.root / "mtime", "older-file", self._turn_records("new", "new prompt", "new outcome", 200))
        os.utime(old, (300, 300))
        os.utime(new, (100, 100))
        result = parse_agent_debug_files([old, new], self.repo)
        self.assertEqual(result["session"]["value"], "new")
        self.assertEqual(result["prompt"]["value"], "new prompt")
        self.assertEqual(result["finalResponse"]["value"], "new outcome")
        self.assertEqual(result["firstTimestamp"]["value"], 180)
        self.assertEqual(result["lastTimestamp"]["value"], 200)

    def test_global_selection_is_independent_of_discovery_order(self) -> None:
        first = self._global_debug_file(self.root / "order", "z-file", self._turn_records("z", "z prompt", "z outcome", 300))
        second = self._global_debug_file(self.root / "order", "a-file", self._turn_records("a", "a prompt", "a outcome", 400))
        forward = parse_agent_debug_files([first, second], self.repo)
        reverse = parse_agent_debug_files([second, first], self.repo)
        self.assertEqual(forward["session"], reverse["session"])
        self.assertEqual(forward["finalResponse"], reverse["finalResponse"])

    def test_global_selection_ignores_newer_incomplete_report_segment(self) -> None:
        records = self._turn_records("session", "completed prompt", "completed outcome", 500)
        records.extend(self._turn_records("session", "/report", "in progress report", 700, complete=False))
        path = self._global_debug_file(self.root / "incomplete", "current", records)
        result = parse_agent_debug_files([path], self.repo)
        self.assertEqual(result["prompt"]["value"], "completed prompt")
        self.assertEqual(result["finalResponse"]["value"], "completed outcome")
        self.assertEqual(result["turnEndTimestamp"]["value"], 500)

    def test_global_selection_equal_completion_timestamps_has_deterministic_tie(self) -> None:
        first = self._global_debug_file(self.root / "tie", "b-file", self._turn_records("b", "b prompt", "b outcome", 600))
        second = self._global_debug_file(self.root / "tie", "a-file", self._turn_records("a", "a prompt", "a outcome", 600))
        result = parse_agent_debug_files([first, second], self.repo)
        self.assertEqual(result["session"]["value"], "a")

    def test_global_selection_preserves_single_file_behavior(self) -> None:
        path = self._write("single-file.jsonl", self._turn_records("single", "single prompt", "single outcome", 800))
        result = parse_agent_debug_files([path], self.repo)
        self.assertEqual(result["session"]["value"], "single")
        self.assertEqual(result["prompt"]["value"], "single prompt")
        self.assertEqual(result["finalResponse"]["value"], "single outcome")

    def test_global_selection_skips_completed_report_after_work(self) -> None:
        records = self._turn_records("session", "work prompt", "work outcome", 900)
        records.extend(self._turn_records("session", " /report ", "report outcome", 1000))
        result = parse_agent_debug_files([self._global_debug_file(self.root / "report-skip", "one", records)], self.repo)
        self.assertEqual(result["prompt"]["value"], "work prompt")
        self.assertEqual(result["finalResponse"]["value"], "work outcome")

    def test_global_selection_skips_multiple_completed_reports(self) -> None:
        records = self._turn_records("session", "work prompt", "work outcome", 1100)
        records.extend(self._turn_records("session", "/report", "first report", 1200))
        records.extend(self._turn_records("session", "\n/report\n", "second report", 1300))
        result = parse_agent_debug_files([self._global_debug_file(self.root / "report-skip", "many", records)], self.repo)
        self.assertEqual(result["prompt"]["value"], "work prompt")
        self.assertEqual(result["finalResponse"]["value"], "work outcome")

    def test_newer_report_in_another_file_cannot_beat_latest_work(self) -> None:
        work = self._global_debug_file(self.root / "cross-file", "work", self._turn_records("work", "latest work", "latest outcome", 1400))
        report = self._global_debug_file(self.root / "cross-file", "report", self._turn_records("report", "/report", "report outcome", 1500))
        result = parse_agent_debug_files([work, report], self.repo)
        self.assertEqual(result["session"]["value"], "work")
        self.assertEqual(result["finalResponse"]["value"], "latest outcome")

    def test_prompt_mentioning_report_remains_eligible(self) -> None:
        records = self._turn_records("session", "fix the /report command", "fixed report command", 1600)
        result = parse_agent_debug_files([self._global_debug_file(self.root / "report-skip", "mention", records)], self.repo)
        self.assertEqual(result["prompt"]["value"], "fix the /report command")

    def test_report_only_sources_are_explicitly_unavailable(self) -> None:
        records = self._turn_records("session", "/report", "report outcome", 1700)
        result = parse_agent_debug_files([self._global_debug_file(self.root / "report-skip", "only", records)], self.repo)
        self.assertEqual(result["status"], "unavailable")

    def test_incomplete_report_remains_excluded(self) -> None:
        records = self._turn_records("session", "work prompt", "work outcome", 1800)
        records.extend(self._turn_records("session", "/report", "in progress report", 1900, complete=False))
        result = parse_agent_debug_files([self._global_debug_file(self.root / "report-skip", "incomplete", records)], self.repo)
        self.assertEqual(result["finalResponse"]["value"], "work outcome")

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
        self.assertEqual(result["approvalWaitSeconds"], {"status": "validated", "value": 0})

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

    def test_renderer_zero_approval_timing_is_scalar_and_exact(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python --version\"]]",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: All sub-commands auto-approved",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command ` python --version` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        self.assertEqual(terminal["approvalWaitSeconds"], {"status": "validated", "value": 0})
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Approval timing: 0 seconds validated", report)
        self.assertIn("Timing: approval wait 0 seconds validated", report)
        self.assertNotIn("{'status': 'validated', 'value': 0}", report)

    def test_unresolved_approval_timing_remains_partial(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"custom-script\"]]",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        self.assertEqual(terminal["approvalWaitSeconds"]["status"], "partial")
        self.assertNotEqual(terminal["approvalWaitSeconds"]["value"], 0)

    def test_terminal_action_unique_exit_zero_is_completed(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `ok-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(terminal["actions"]["status"], "validated")
        self.assertEqual(terminal["actions"]["value"][0]["executionStatus"], "completed")
        self.assertEqual(terminal["actions"]["value"][0]["exitOutcome"], "zero")
        self.assertEqual(terminal["actions"]["value"][0]["exitCode"], "0")

    def test_known_zero_exit_with_no_failure_renders_zero_validated(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `ok-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(terminal["errors"], {"status": "validated", "value": [], "unknownExitCount": 0})
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Terminal errors: 0 validated", report)
        self.assertIn("Action: +0.000s ok-cmd (executed/completed; exit 0)", report)

    def test_terminal_action_nonzero_exit_is_visible(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `bad-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `7`, result.length `2`, error `failed` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(terminal["actions"]["value"][0]["executionStatus"], "completed")
        self.assertEqual(terminal["actions"]["value"][0]["exitOutcome"], "nonzero")
        self.assertEqual(terminal["errors"]["value"][0]["category"], "nonzero_exit")
        self.assertEqual(terminal["errors"]["value"][0]["outcome"], "unresolved_failure")

    def test_undefined_exit_is_completed_with_unknown_exit_and_no_error(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `unknown-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `undefined`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        action = terminal["actions"]["value"][0]
        self.assertEqual(action["executionStatus"], "completed")
        self.assertEqual(action["exitOutcome"], "unknown")
        self.assertEqual(terminal["errors"]["value"], [])
        self.assertEqual(terminal["errors"]["status"], "partial")
        self.assertEqual(terminal["errors"]["unknownExitCount"], 1)
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Action: +0.000s unknown-cmd (executed/completed; exit unknown)", report)
        self.assertIn("Terminal errors: partial - 1 completed action has unknown exit outcome", report)
        self.assertNotIn("Terminal errors: 1 shown", report)

    def test_live_shaped_seventeen_undefined_exits_use_one_aggregate_limitation(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        lines = []
        for index in range(17):
            lines.append(f"2026-08-21 14:00:{index:02d}.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `live-cmd-{index}` []")
            lines.append(f"2026-08-21 14:00:{index:02d}.500 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `undefined`, result.length `2`, error `undefined` []")
        terminal = parse_terminal_diagnostics(self._terminal_log(lines), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:01:00"))
        self.assertEqual(len(terminal["actions"]["value"]), 17)
        self.assertTrue(all(action["executionStatus"] == "completed" for action in terminal["actions"]["value"]))
        self.assertEqual(terminal["errors"]["value"], [])
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Terminal actions: 17 shown", report)
        self.assertNotIn("Terminal errors: 17 shown", report)
        self.assertEqual(report.count("Terminal errors:"), 1)
        self.assertIn("Terminal errors: partial - 17 completed actions have unknown exit outcomes", report)

    def test_mixed_proven_failure_and_unknown_exits_render_entries_plus_limitation(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `bad-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `7`, result.length `2`, error `boom` []",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `unknown-cmd` []",
            "2026-08-21 14:00:01.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `undefined`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:02"))
        self.assertEqual([error["category"] for error in terminal["errors"]["value"]], ["nonzero_exit"])
        self.assertEqual(terminal["errors"]["status"], "partial")
        self.assertEqual(terminal["errors"]["unknownExitCount"], 1)
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Terminal errors: 1 shown", report)
        self.assertIn("Error: +0.100s bad-cmd (nonzero_exit; unresolved_failure; boom)", report)
        self.assertIn("Terminal errors: partial - 1 completed action has unknown exit outcome", report)

    def test_finish_correlation_ignores_executions_after_the_finish(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.500 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `later-cmd` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:02"))
        statuses = [action["executionStatus"] for action in terminal["actions"]["value"]]
        self.assertEqual(statuses, ["unassociated", "started"])
        self.assertEqual(terminal["errors"]["value"], [])

    def test_relative_timestamps_are_deterministic(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:12.341 [info] RunInTerminalTool: Using `rich` execute strategy for command `timed-cmd` []",
            "2026-08-21 14:00:12.900 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `3`, result.length `2`, error `boom` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:20"))
        self.assertAlmostEqual(terminal["actions"]["value"][0]["relativeSeconds"], 12.341, places=3)
        self.assertAlmostEqual(terminal["actions"]["value"][0]["timestamp"], datetime_timestamp("2026-08-21 14:00:12") + 0.341, places=3)
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Action: +12.341s timed-cmd (executed/completed; exit 3)", report)
        self.assertIn("Error: +12.900s timed-cmd (nonzero_exit; unresolved_failure; boom)", report)

    def test_unresolved_permission_interruption_remains_visible(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"blocked-cmd\"]]",
            "2026-08-21 14:00:01.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:01.002 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'blocked-cmd' has no matching auto approve entries",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:05"))
        error = terminal["errors"]["value"][0]
        self.assertEqual(error["category"], "permission_interruption")
        self.assertEqual(error["outcome"], "unresolved_permission")
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("permission_interruption; unresolved_permission", report)

    def test_request_only_unresolved_and_ambiguous_actions_remain_visible(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"never-run\"]]",
            "2026-08-21 14:00:01.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:02.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"twice-cmd\"]]",
            "2026-08-21 14:00:02.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:03.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `twice-cmd` []",
            "2026-08-21 14:00:04.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `twice-cmd` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:10"))
        request_actions = [action for action in terminal["actions"]["value"] if action["actionType"] == "request"]
        self.assertEqual([action["executionStatus"] for action in request_actions], ["unresolved", "ambiguous"])
        self.assertEqual([error["category"] for error in terminal["errors"]["value"]], ["permission_interruption"])

    def test_unassociated_finish_is_preserved_as_unknown(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(terminal["actions"]["status"], "partial")
        self.assertEqual(terminal["actions"]["value"][0]["executionStatus"], "unassociated")
        self.assertIsNone(terminal["actions"]["value"][0]["command"])

    def test_multiple_unfinished_executions_make_finish_ambiguous(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `first-cmd` []",
            "2026-08-21 14:00:00.010 [info] RunInTerminalTool: Using `rich` execute strategy for command `second-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(terminal["actions"]["status"], "partial")
        self.assertEqual([action["executionStatus"] for action in terminal["actions"]["value"]], ["started", "started", "ambiguous"])
        self.assertEqual(terminal["errors"]["value"], [])

    def test_unique_approval_attaches_to_one_action(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"approved-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Using `rich` execute strategy for command `approved-cmd` []",
            "2026-08-21 14:00:00.200 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        actions = terminal["actions"]["value"]
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0]["approvalRequest"])
        self.assertEqual(terminal["terminalApprovalRequestCount"]["value"], 1)

    def test_auto_approved_action_has_no_approval_wait(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"auto-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: All sub-commands auto-approved",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Using `rich` execute strategy for command `auto-cmd` []",
            "2026-08-21 14:00:00.200 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertNotIn("approvalRequest", terminal["actions"]["value"][0])
        self.assertEqual(terminal["approvalWaitSeconds"]["value"], 0)

    def test_nonzero_followed_by_same_command_success_is_recovered(self) -> None:
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `retry-cmd` []",
            "2026-08-21 14:00:00.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `7`, result.length `2`, error `failed` []",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `retry-cmd` []",
            "2026-08-21 14:00:01.100 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `2`, error `undefined` []",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:02"))
        self.assertEqual(terminal["errors"]["value"][0]["category"], "nonzero_exit")
        self.assertEqual(terminal["errors"]["value"][0]["outcome"], "recovered_failure")

    def test_action_renderer_is_chronological_and_bounded(self) -> None:
        lines = []
        for index in range(52):
            lines.append(f"2026-08-21 14:00:{index % 60:02d}.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `cmd-{index}` []")
        terminal = parse_terminal_diagnostics(self._terminal_log(lines), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:01:00"))
        self.assertEqual(len(terminal["actions"]["value"]), 50)
        self.assertTrue(terminal["actions"]["truncated"])
        self.assertEqual(terminal["actions"]["length"], 52)

    def test_multiple_repositories_and_bounded_sensitive_fields(self) -> None:
        other_repo = str(self.root / "other")
        Path(other_repo).mkdir()
        debug_path = self._write("multi-debug.jsonl", [
            {"ts": 1000, "sid": "other-session", "type": "user_message", "attrs": {"content": "other secret", "repository": other_repo}},
            {"ts": 1100, "sid": "other-session", "type": "turn_end", "attrs": {"turnId": "other"}},
            {"ts": 2000, "sid": "repo-session", "type": "user_message", "attrs": {"content": "repo secret", "repository": self.repo}},
            {"ts": 2100, "sid": "repo-session", "type": "agent_response", "attrs": {"response": "response secret"}},
            {"ts": 2200, "sid": "repo-session", "type": "turn_end", "attrs": {"turnId": "repo"}},
        ])
        result = parse_agent_debug(debug_path, self.repo)
        self.assertEqual(result["session"]["value"], "repo-session")
        self.assertEqual(result["prompt"]["value"], "repo secret")
        self.assertEqual(result["finalResponse"]["value"], "response secret")
        bounded = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python -c 'print(\\\"secret\\\")'\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:01"))
        self.assertEqual(bounded["status"], "validated")
        self.assertLessEqual(len(bounded["approvalRequests"]["value"][0]["command"]), 400)

    def test_case_a_simple_denied_and_executed(self) -> None:
        """Case A: Control - Simple denied command + executed"""
        log = self._terminal_log([
            "2026-08-21 12:28:38.409 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"skills/copilot/flow/scripts/ticket-status verbose\"]]",
            "2026-08-21 12:28:38.410 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 12:28:38.411 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'skills/copilot/flow/scripts/ticket-status verbose' has no matching auto approve entries",
            "2026-08-21 12:28:46.535 [info] RunInTerminalTool: Using `rich` execute strategy for command `skills/copilot/flow/scripts/ticket-status verbose` []",
            "2026-08-21 12:28:46.636 [info] RunInTerminalTool: Finished `rich` execute strategy with exitCode `0`, result.length `8126`, error `undefined` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 12:28:00"), end=datetime_timestamp("2026-08-21 12:29:00"))
        self.assertEqual(result["status"], "validated")
        requests = result["approvalRequests"]["value"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["command"], "skills/copilot/flow/scripts/ticket-status verbose")
        self.assertEqual(request["disposition"], "executed")
        self.assertAlmostEqual(request["approvalWaitSeconds"], 8.126, places=2)
        self.assertEqual(result["approvalWaitSeconds"]["status"], "validated")
        self.assertAlmostEqual(result["approvalWaitSeconds"]["value"], 8.126, places=2)

    def test_case_b_compound_command_with_later_element_executed(self) -> None:
        """Case B: A later parsed command element can correlate to execution."""
        log = self._terminal_log([
            "2026-08-21 19:42:51.365 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python3 -m unittest tests.test_copilot_report\", \"git diff --check\"]]",
            "2026-08-21 19:42:51.366 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 19:42:51.367 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'git diff --check' has no matching auto approve entries",
            "2026-08-21 19:42:56.991 [info] RunInTerminalTool: Using `rich` execute strategy for command `git diff --check` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 19:40:00"), end=datetime_timestamp("2026-08-21 19:50:00"))
        self.assertEqual(result["status"], "validated")
        requests = result["approvalRequests"]["value"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["commands"], ["python3 -m unittest tests.test_copilot_report", "git diff --check"])
        self.assertEqual(request["command"], '["python3 -m unittest tests.test_copilot_report","git diff --check"]')
        self.assertEqual(request["disposition"], "executed")
        self.assertAlmostEqual(request["approvalWaitSeconds"], 5.626, places=2)

    def test_case_b_first_element_and_no_arbitrary_request_identity(self) -> None:
        log = self._terminal_log([
            "2026-08-21 19:42:51.365 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"python3 --version\", \"git diff --check\"]]",
            "2026-08-21 19:42:51.366 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 19:42:51.367 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'python3 --version' has no matching auto approve entries",
            "2026-08-21 19:42:52.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `python3 --version` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 19:40:00"), end=datetime_timestamp("2026-08-21 19:50:00"))
        request = result["approvalRequests"]["value"][0]
        self.assertEqual(request["commands"], ["python3 --version", "git diff --check"])
        self.assertEqual(request["disposition"], "executed")

    def test_case_b_ambiguous_mapping_consumes_no_execution(self) -> None:
        log = self._terminal_log([
            "2026-08-21 19:42:51.365 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 19:42:51.366 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 19:42:51.367 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'review-cmd' has no matching auto approve entries",
            "2026-08-21 19:42:52.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `review-cmd` []",
            "2026-08-21 19:42:53.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `review-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 19:40:00"), end=datetime_timestamp("2026-08-21 19:50:00"))
        request = result["approvalRequests"]["value"][0]
        self.assertEqual(request["disposition"], "ambiguous")
        self.assertIsNone(request["executionTimestamp"])
        self.assertIsNone(request["approvalWaitSeconds"])

    def test_case_b_one_execution_cannot_satisfy_two_occurrences(self) -> None:
        log = self._terminal_log([
            "2026-08-21 19:42:51.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 19:42:51.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 19:42:52.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 19:42:52.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 19:42:53.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `review-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 19:40:00"), end=datetime_timestamp("2026-08-21 19:50:00"))
        requests = result["approvalRequests"]["value"]
        self.assertEqual([request["disposition"] for request in requests], ["executed", "unresolved"])

    def test_case_c_denied_no_execution(self) -> None:
        """Case C: Denied request with no matching execution"""
        log = self._terminal_log([
            "2026-08-21 12:53:59.968 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"custom-script --flag\"]]",
            "2026-08-21 12:53:59.969 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 12:53:59.970 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'custom-script --flag' has no matching auto approve entries",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 12:53:00"), end=datetime_timestamp("2026-08-21 12:55:00"))
        self.assertEqual(result["status"], "validated")
        requests = result["approvalRequests"]["value"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["command"], "custom-script --flag")
        self.assertEqual(request["disposition"], "unresolved")
        self.assertIsNone(request["approvalWaitSeconds"])
        self.assertEqual(result["approvalWaitSeconds"]["status"], "partial")
        self.assertIn("0/1 approval requests correlated", str(result["approvalWaitSeconds"]["value"]))

    def test_case_d_duplicate_analyzer_same_timestamp_deduplicated(self) -> None:
        """Case D: Duplicate analyzer records with same command array and timestamp (deduplicated)"""
        log = self._terminal_log([
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"./skills/copilot/auto-review/scripts/review-evidence --mode checkpoint\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:52.706 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command './skills/copilot/auto-review/scripts/review-evidence --mode checkpoint' has no matching auto approve entries",
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"./skills/copilot/auto-review/scripts/review-evidence --mode checkpoint\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:52.706 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command './skills/copilot/auto-review/scripts/review-evidence --mode checkpoint' has no matching auto approve entries",
            "2026-08-21 20:07:57.200 [info] RunInTerminalTool: Using `rich` execute strategy for command `./skills/copilot/auto-review/scripts/review-evidence --mode checkpoint` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 20:07:00"), end=datetime_timestamp("2026-08-21 20:08:00"))
        self.assertEqual(result["status"], "validated")
        requests = result["approvalRequests"]["value"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["disposition"], "executed")

    def test_case_d_same_timestamp_different_denial_state_or_reason_is_distinct(self) -> None:
        log = self._terminal_log([
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:07:52.706 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:52.707 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'review-cmd' has no matching auto approve entries",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 20:07:00"), end=datetime_timestamp("2026-08-21 20:08:00"))
        self.assertEqual(result["terminalRequestCount"]["value"], 2)
        self.assertEqual(result["terminalApprovalRequestCount"]["value"], 1)

    def test_case_d_same_timestamp_different_denial_reasons_is_distinct(self) -> None:
        log = self._terminal_log([
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:52.706 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'review-cmd' has no matching auto approve entries one",
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:52.706 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: - Command 'review-cmd' has no matching auto approve entries two",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 20:07:00"), end=datetime_timestamp("2026-08-21 20:08:00"))
        self.assertEqual(result["terminalRequestCount"]["value"], 2)
        self.assertEqual(result["terminalApprovalRequestCount"]["value"], 2)

    def test_case_d_duplicate_different_timestamps_preserved(self) -> None:
        """Case D: Same command analyzed at different times (different occurrences, not duplicates)"""
        log = self._terminal_log([
            "2026-08-21 20:07:52.704 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:07:52.705 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:29:38.293 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"review-cmd\"]]",
            "2026-08-21 20:29:38.294 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 20:07:57.200 [info] RunInTerminalTool: Using `rich` execute strategy for command `review-cmd` []",
            "2026-08-21 20:29:43.100 [info] RunInTerminalTool: Using `rich` execute strategy for command `review-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 20:07:00"), end=datetime_timestamp("2026-08-21 20:30:00"))
        self.assertEqual(result["status"], "validated")
        requests = result["approvalRequests"]["value"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["disposition"], "ambiguous")
        self.assertEqual(requests[1]["disposition"], "executed")
        self.assertEqual(result["approvalWaitSeconds"]["status"], "partial")

    def test_multiple_uniquely_executed_approvals_sum_waits(self) -> None:
        log = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"first-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"second-cmd\"]]",
            "2026-08-21 14:00:01.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command `first-cmd` []",
            "2026-08-21 14:00:04.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `second-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:05"))
        self.assertEqual(result["approvalWaitSeconds"]["status"], "validated")
        self.assertAlmostEqual(result["approvalWaitSeconds"]["value"], 5.5, places=3)

    def test_mixed_executed_and_unresolved_approvals_are_partial(self) -> None:
        log = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"first-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"missing-cmd\"]]",
            "2026-08-21 14:00:01.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:02.500 [info] RunInTerminalTool: Using `rich` execute strategy for command `first-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:05"))
        self.assertEqual(result["approvalWaitSeconds"], {"status": "partial", "value": "1/2 approval requests correlated"})

    def test_mixed_executed_and_ambiguous_approvals_are_partial(self) -> None:
        log = self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"ambiguous-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:01.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"unique-cmd\"]]",
            "2026-08-21 14:00:01.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
            "2026-08-21 14:00:02.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `ambiguous-cmd` []",
            "2026-08-21 14:00:03.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `ambiguous-cmd` []",
            "2026-08-21 14:00:04.000 [info] RunInTerminalTool: Using `rich` execute strategy for command `unique-cmd` []",
        ])
        result = parse_terminal_diagnostics(log, start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:05"))
        self.assertEqual(result["approvalWaitSeconds"], {"status": "partial", "value": "1/2 approval requests correlated"})

    def test_renderer_keeps_partial_timing_consistent(self) -> None:
        debug = parse_agent_debug(self._debug(), self.repo)
        otel = parse_otel(self._otel(), session="session-1", start=1.0, end=1.5)
        terminal = parse_terminal_diagnostics(self._terminal_log([
            "2026-08-21 14:00:00.000 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Parsed sub-commands via bash grammar [[\"missing-cmd\"]]",
            "2026-08-21 14:00:00.001 [info] RunInTerminalTool#CommandLineAutoApproveAnalyzer: Sub-command DENIED auto approval",
        ]), start=datetime_timestamp("2026-08-21 14:00:00"), end=datetime_timestamp("2026-08-21 14:00:03"))
        report = render_copilot_report(agent_debug=debug, otel=otel, terminal=terminal, active_issue="49")
        self.assertIn("Approval timing: 0/1 approval requests correlated (partial)", report)
        self.assertIn("Timing: approval wait 0/1 approval requests correlated (partial)", report)



def datetime_timestamp(value: str) -> float:
    from datetime import datetime
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()


if __name__ == "__main__":
    unittest.main()
