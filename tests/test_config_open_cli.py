from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.editable_config import EditableConfigError
from ai_dev_flow.editor_selection import EditorLaunchResult


class ConfigOpenCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Config Open CLI Tests")
        self._run_git(repo_root, "config", "user.email", "config-open-cli-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")
        return repo_root

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = "ai-dev"
        sys.argv = ["ai-dev", *arguments]
        os.chdir(cwd)

        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 1
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv
            if had_command_name:
                assert previous_command_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)

        return code, stdout.getvalue(), stderr.getvalue()

    def test_bare_config_creates_file_and_reports_opened_command(self) -> None:
        repo_root = self._init_repo("repo-config-create")
        config_path = self.tmp_path / "cfg" / "ai-dev" / "config.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=True,
                    status="opened",
                    command_display="code --wait",
                    warning=None,
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(f"Created AI Dev config: {config_path}", out)
        self.assertIn("Opened config with: code --wait", out)
        self.assertTrue(config_path.is_absolute())
        self.assertTrue(config_path.exists())
        created_text = config_path.read_text(encoding="utf-8")
        self.assertIn("aliases: {}", created_text)
        self.assertIn("presentation: path-only", created_text)

    def test_existing_config_is_unchanged(self) -> None:
        repo_root = self._init_repo("repo-config-existing")
        config_path = self.tmp_path / "cfg" / "existing.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        original = "ai:\n  delivery: file-only\n"
        config_path.write_text(original, encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=True,
                    status="opened",
                    command_display="vim",
                    warning=None,
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(f"AI Dev config: {config_path}", out)
        self.assertNotIn("Created AI Dev config", out)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_editor_unavailable_falls_back_to_manual_path(self) -> None:
        repo_root = self._init_repo("repo-config-no-editor")
        config_path = self.tmp_path / "cfg" / "no-editor.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="no-editor-candidate",
                    command_display=None,
                    warning=None,
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(f"Created AI Dev config: {config_path}", out)
        self.assertIn("No editor could be launched. Edit this file manually.", out)

    def test_editor_launch_failure_warns_and_keeps_exit_zero(self) -> None:
        repo_root = self._init_repo("repo-config-launch-failure")
        config_path = self.tmp_path / "cfg" / "launch-failure.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="launch-failed",
                    command_display="vim",
                    warning="Editor command vim failed: exit code 2",
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertIn("No editor could be launched. Edit this file manually.", out)
        self.assertIn("Warning: Editor command vim failed: exit code 2", err)

    def test_config_path_failure_returns_exit_one(self) -> None:
        repo_root = self._init_repo("repo-config-failure")

        with patch(
            "ai_dev_flow.cli.ensure_editable_user_config",
            side_effect=EditableConfigError("Cannot resolve config path /bad/path"),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot resolve config path", err)

    def test_help_and_get_set_unset_behavior_preserved(self) -> None:
        repo_root = self._init_repo("repo-config-help")

        help_code, help_out, help_err = self._invoke(repo_root, "config", "--help")
        self.assertEqual(help_code, 0)
        self.assertEqual(help_err, "")
        self.assertIn("Usage: ai-dev config", help_out)

        set_code, set_out, set_err = self._invoke(repo_root, "set", "out=reports/out.md")
        self.assertEqual(set_code, 0)
        self.assertEqual(set_err, "")
        self.assertIn("out: reports/out.md", set_out)

        get_code, get_out, get_err = self._invoke(repo_root, "get", "out")
        self.assertEqual(get_code, 0)
        self.assertEqual(get_err, "")
        self.assertEqual(get_out.strip(), "reports/out.md")

        unset_code, unset_out, unset_err = self._invoke(repo_root, "unset", "out")
        self.assertEqual(unset_code, 0)
        self.assertEqual(unset_err, "")
        self.assertIn("out: not configured", unset_out)

    def test_no_repo_required_and_malformed_workflow_ignored(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg" / "outside.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="no-editor-candidate",
                    command_display=None,
                    warning="No editor candidate is available",
                ),
            ),
        ):
            code, out, err = self._invoke(outside, "config")

        self.assertEqual(code, 0)
        self.assertIn(f"Created AI Dev config: {config_path}", out)
        self.assertIn("No editor could be launched. Edit this file manually.", out)
        self.assertIn("No editor candidate is available", err)

        repo_root = self._init_repo("repo-malformed-workflow")
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text("{ invalid json\n", encoding="utf-8")
        config_path_2 = self.tmp_path / "cfg" / "malformed-workflow.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path_2)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="no-editor-candidate",
                    command_display=None,
                    warning=None,
                ),
            ),
        ):
            code2, out2, err2 = self._invoke(repo_root, "config")

        self.assertEqual(code2, 0)
        self.assertEqual(err2, "")
        self.assertIn("No editor could be launched", out2)


if __name__ == "__main__":
    unittest.main()
