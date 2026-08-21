from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import json

from ai_dev_flow.copilot_report import render_latest_copilot_report


class CopilotReportEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_report_help_is_available_from_both_entrypoints(self) -> None:
        scripts = self.repo_root / "skills/copilot/flow/scripts"
        environment = {**os.environ, "PATH": "/usr/bin:/bin"}
        posix = subprocess.run([str(scripts / "flow-report"), "--help"], cwd=self.repo, env=environment, text=True, capture_output=True)
        self.assertEqual(posix.returncode, 0, posix.stderr)
        self.assertIn("Usage: flow-report", posix.stdout)
        self.assertIn('invoke-flow.ps1', (scripts / "flow-report.ps1").read_text(encoding="utf-8"))

    def test_report_command_works_without_active_workflow(self) -> None:
        scripts = self.repo_root / "skills/copilot/flow/scripts"
        result = subprocess.run([str(scripts / "flow-report")], cwd=self.repo, env={**os.environ, "PATH": "/usr/bin:/bin"}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Issue: unavailable", result.stdout)
        self.assertIn("terminal-diagnostic:", result.stdout)
        self.assertIn("unavailable", result.stdout)

    def test_flow_skill_maps_report_to_canonical_helper(self) -> None:
        skill = (self.repo_root / "skills/copilot/flow/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`/report` or `report this turn`", skill)
        self.assertIn("scripts/flow-report", skill)

    def test_report_script_is_executable_and_power_shell_is_thin(self) -> None:
        script = self.repo_root / "skills/copilot/flow/scripts/flow-report"
        self.assertTrue(os.access(script, os.X_OK))
        self.assertIn('invoke-flow.ps1', (script.parent / "flow-report.ps1").read_text(encoding="utf-8"))

    def test_latest_report_excludes_in_progress_report_turn(self) -> None:
        debug_root = self.repo / "debug"
        debug_dir = debug_root / "workspace" / "GitHub.copilot-chat" / "debug-logs" / "session"
        debug_dir.mkdir(parents=True)
        records = [
            {"ts": 1000, "sid": "session-1", "type": "user_message", "attrs": {"content": "eligible work", "repository": str(self.repo)}},
            {"ts": 1100, "sid": "session-1", "type": "agent_response", "attrs": {"response": "completed work"}},
            {"ts": 1200, "sid": "session-1", "type": "turn_end", "attrs": {"turnId": "work"}},
            {"ts": 1300, "sid": "session-1", "type": "user_message", "attrs": {"content": "/report", "repository": str(self.repo)}},
            {"ts": 1400, "sid": "session-1", "type": "agent_response", "attrs": {"response": "in progress report"}},
        ]
        (debug_dir / "main.jsonl").write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
        settings = self.repo / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        report = render_latest_copilot_report(self.repo, settings_path=settings, debug_root=debug_root, terminal_root=self.repo / "missing-terminal")
        self.assertIn("Prompt: eligible work", report)
        self.assertNotIn("Prompt: /report", report)

    def test_latest_report_includes_active_issue_identity_and_source_health(self) -> None:
        (self.repo / ".ai-dev").mkdir()
        (self.repo / ".ai-dev/workflow.json").write_text(json.dumps({"activeIssueNumber": 49, "activeIssueTitle": "Report work"}), encoding="utf-8")
        settings = self.repo / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        report = render_latest_copilot_report(self.repo, settings_path=settings, debug_root=self.repo / "missing-debug", terminal_root=self.repo / "missing-terminal")
        self.assertIn("Issue: 49 Report work", report)
        self.assertIn("agent-debug: unavailable", report)
        self.assertIn("otel: unavailable", report)
        self.assertNotIn("Tokens: 0", report)

    def test_latest_report_surfaces_unexpected_source_format(self) -> None:
        debug_root = self.repo / "debug"
        debug_dir = debug_root / "workspace" / "GitHub.copilot-chat" / "debug-logs" / "session"
        debug_dir.mkdir(parents=True)
        (debug_dir / "main.jsonl").write_text("not-json\n", encoding="utf-8")
        settings = self.repo / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        report = render_latest_copilot_report(self.repo, settings_path=settings, debug_root=debug_root, terminal_root=self.repo / "missing-terminal")
        self.assertIn("agent-debug: error: unexpected log format", report)
        self.assertNotIn("Tokens: 0", report)


if __name__ == "__main__":
    unittest.main()
