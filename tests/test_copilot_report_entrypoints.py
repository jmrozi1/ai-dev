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

    def test_report_package_posix_adapter_invokes_actual_repository_file(self) -> None:
        adapter = self.repo_root / "skills/copilot/report/scripts/flow-report"
        result = subprocess.run([str(adapter), "--help"], cwd=self.repo, env={**os.environ, "PATH": "/usr/bin:/bin"}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage: flow-report", result.stdout)

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

    def test_report_skill_uses_packaged_adapters_and_not_bare_relative_helpers(self) -> None:
        skill = (self.repo_root / "skills/copilot/report/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("[POSIX adapter](./scripts/flow-report)", skill)
        self.assertIn("[PowerShell adapter](./scripts/flow-report.ps1)", skill)
        self.assertNotIn("Run the installed Copilot Flow helper `scripts/flow-report` from the current", skill)
        self.assertNotIn("`scripts/flow-report`", skill)

    def test_posix_adapter_delegates_to_sibling_flow_helper(self) -> None:
        temp_root = Path(self.temp.name) / "adapter-layout"
        report_dir = temp_root / "skills" / "copilot" / "report" / "scripts"
        flow_dir = temp_root / "skills" / "copilot" / "flow" / "scripts"
        report_dir.mkdir(parents=True)
        flow_dir.mkdir(parents=True)
        (flow_dir / "flow-report").write_text("#!/usr/bin/env bash\nexec printf '%s\\n' \"flow helper:${*}\"\n", encoding="utf-8")
        (flow_dir / "flow-report").chmod(0o755)
        adapter = report_dir / "flow-report"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "script_path=\"${BASH_SOURCE[0]:-$0}\"\n"
            "while [[ -L \"$script_path\" ]]; do\n"
            "  script_dir=\"$(cd \"$(dirname \"$script_path\")\" && pwd)\"\n"
            "  script_path=\"$(readlink \"$script_path\")\"\n"
            "  [[ \"$script_path\" = /* ]] || script_path=\"$script_dir/$script_path\"\n"
            "done\n"
            "script_dir=\"$(cd \"$(dirname \"$script_path\")\" && pwd -P)\"\n"
            "flow_helper=\"$(cd \"$script_dir/../../flow/scripts\" && pwd -P)/flow-report\"\n"
            "if [[ ! -x \"$flow_helper\" ]]; then\n"
            "  echo \"error: missing sibling Flow helper at $flow_helper\" >&2\n"
            "  exit 1\n"
            "fi\n"
            "exec \"$flow_helper\" \"$@\"\n",
            encoding="utf-8",
        )
        adapter.chmod(0o755)

        result = subprocess.run([str(adapter), "alpha", "beta"], cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flow helper:alpha beta", result.stdout)

    def test_report_package_symlink_delegation_keeps_sibling_flow_resolution(self) -> None:
        temp_root = Path(self.temp.name) / "symlink-layout"
        real_report_dir = temp_root / "real" / "skills" / "copilot" / "report" / "scripts"
        real_flow_dir = temp_root / "real" / "skills" / "copilot" / "flow" / "scripts"
        real_report_dir.mkdir(parents=True)
        real_flow_dir.mkdir(parents=True)
        (real_flow_dir / "flow-report").write_text("#!/usr/bin/env bash\nexec printf '%s\\n' \"from symlink:$@\"\n", encoding="utf-8")
        (real_flow_dir / "flow-report").chmod(0o755)
        adapter = real_report_dir / "flow-report"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            "script_path=\"${BASH_SOURCE[0]:-$0}\"\n"
            "script_dir=\"$(cd \"$(dirname \"$script_path\")\" && pwd -P)\"\n"
            "flow_helper=\"$(cd \"$script_dir/../../flow/scripts\" && pwd -P)/flow-report\"\n"
            "exec \"$flow_helper\" \"$@\"\n",
            encoding="utf-8",
        )
        adapter.chmod(0o755)

        installed_root = temp_root / "installed"
        installed_root.mkdir()
        installed_report_dir = installed_root / "report"
        installed_report_dir.symlink_to(real_report_dir.parent, target_is_directory=True)
        result = subprocess.run([str(installed_report_dir / "scripts" / "flow-report"), "symlinked"], cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from symlink:symlinked", result.stdout)

    def test_missing_sibling_flow_helper_fails_with_actionable_error(self) -> None:
        temp_root = Path(self.temp.name) / "missing-flow"
        report_dir = temp_root / "skills" / "copilot" / "report" / "scripts"
        report_dir.mkdir(parents=True)
        adapter = report_dir / "flow-report"
        adapter.write_text(
            "#!/usr/bin/env bash\n"
            "script_dir=\"$(cd \"$(dirname \"${BASH_SOURCE[0]:-$0}\")\" && pwd -P)\"\n"
            "flow_helper=\"$(cd \"$script_dir/../../flow/scripts\" && pwd -P)/flow-report\"\n"
            "if [[ ! -x \"$flow_helper\" ]]; then\n"
            "  echo \"error: missing sibling Flow helper at $flow_helper; repair the installed skill layout\" >&2\n"
            "  exit 1\n"
            "fi\n",
            encoding="utf-8",
        )
        adapter.chmod(0o755)

        result = subprocess.run([str(adapter)], cwd=self.repo, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing sibling Flow helper", result.stderr)
        self.assertIn("repair the installed skill layout", result.stderr)

    def test_powershell_adapter_is_thin_and_targets_sibling_flow_helper(self) -> None:
        script = self.repo_root / "skills/copilot/report/scripts/flow-report.ps1"
        self.assertTrue(script.exists())
        text = script.read_text(encoding="utf-8")
        self.assertIn('flow-report.ps1', text)
        self.assertIn("Join-Path", text)
        self.assertIn("Test-Path", text)
        self.assertIn("throw \"error: missing sibling Flow helper", text)
        self.assertNotIn("copilot_report", text)
        self.assertNotIn("render_latest_copilot_report", text)

    def test_report_skill_has_no_duplicated_report_logic(self) -> None:
        skill = (self.repo_root / "skills/copilot/report/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("parser-health", skill)
        self.assertNotIn("render_latest_copilot_report", skill)
        self.assertNotIn("jsonl", skill)

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
