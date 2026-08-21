from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
