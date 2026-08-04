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
from ai_dev_flow.editor_opening import EditorOpenResult
from ai_dev_flow.editor_selection import EditorLaunchResult
from ai_dev_flow.task_delivery import ClipboardDeliveryError


class _FakeEditorOpener:
    def __init__(self, *, opened: bool = True, warning: str | None = None) -> None:
        self.opened = opened
        self.warning = warning
        self.opened_paths: list[Path] = []

    def open_path(self, target_path: Path) -> EditorOpenResult:
        self.opened_paths.append(target_path)
        return EditorOpenResult(opened=self.opened, warning=self.warning)


class GeneratedTaskFoundationIntegrationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Integration Tests")
        self._run_git(repo_root, "config", "user.email", "integration-tests@example.com")
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

        lifecycle_commands = {
            "start",
            "patch",
            "task-prepare",
            "status",
            "review",
            "commit",
            "reset",
            "promote",
            "complete",
            "block",
            "resume",
        }
        invocation_arguments = list(arguments)
        if invocation_arguments and invocation_arguments[0] in lifecycle_commands:
            invocation_arguments = ["flow", *invocation_arguments]

        os.environ["FLOW_COMMAND_NAME"] = "flow"
        sys.argv = ["flow", *invocation_arguments]
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

    def test_default_flow_generates_artifacts_and_stdout_invocation(self) -> None:
        repo_root = self._init_repo("repo-default-flow")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "default-flow-demo",
                "issue",
                "flow status -v",
                "--body",
                "Default flow body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            out,
            "Read and execute .ai-dev/tasks/default-flow-demo.md\n"
            "Task file: .ai-dev/tasks/default-flow-demo.md\n",
        )

        task_path = repo_root / ".ai-dev" / "tasks" / "default-flow-demo.md"
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        self.assertTrue(task_path.exists())
        self.assertTrue(pointer_path.exists())

        task_text = task_path.read_text(encoding="utf-8")
        pointer_text = pointer_path.read_text(encoding="utf-8")
        self.assertIn("# AI Dev Generated Task: default-flow-demo", task_text)
        self.assertIn("## Metadata", task_text)
        self.assertIn("## Task", task_text)
        self.assertIn("## Constraints", task_text)
        self.assertIn("## Expected Output", task_text)
        self.assertIn("- Task-ID: default-flow-demo", pointer_text)
        self.assertIn("- Task-File: .ai-dev/tasks/default-flow-demo.md", pointer_text)

    def test_customized_machine_flow_uses_user_preferences_and_ignores_repo_overrides(self) -> None:
        repo_root = self._init_repo("repo-customized-flow")
        user_config = self.tmp_path / "customized-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard+stdout\n"
            "  invocation: \"Run {task_id}:{task_type} from {task_file}\"\n"
            "editor:\n"
            "  command: \"code --wait\"\n"
            "reports:\n"
            "  presentation: editor\n",
            encoding="utf-8",
        )
        (repo_root / ".ai-dev.yaml").write_text(
            "ai:\n"
            "  delivery: file-only\n"
            "  invocation: \"Repo override {task_file}\"\n"
            "editor:\n"
            "  command: \"repo-editor\"\n"
            "reports:\n"
            "  presentation: path-only\n",
            encoding="utf-8",
        )

        report_path = repo_root / "out.txt"
        report_path.write_text("customized report\n", encoding="utf-8")

        fake_editor = _FakeEditorOpener(opened=True, warning="configured fallback used")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None) as clipboard_copy,
            patch("ai_dev_flow.cli.build_editor_opener", return_value=fake_editor) as build_editor,
        ):
            prepare_code, prepare_out, prepare_err = self._invoke(
                repo_root,
                "task-prepare",
                "customized-flow-demo",
                "issue",
                "flow status -v",
                "--body",
                "Customized flow body",
            )
            report_code, report_out, report_err = self._invoke(repo_root, "showreport")

        self.assertEqual(prepare_code, 0)
        self.assertEqual(prepare_err, "")
        self.assertIn("Run customized-flow-demo:issue from .ai-dev/tasks/customized-flow-demo.md", prepare_out)
        self.assertEqual(prepare_out.count("Run customized-flow-demo:issue from .ai-dev/tasks/customized-flow-demo.md"), 1)
        self.assertIn("Invocation copied to clipboard.", prepare_out)
        self.assertIn("Task file: .ai-dev/tasks/customized-flow-demo.md", prepare_out)
        self.assertNotIn("Repo override", prepare_out)

        clipboard_copy.assert_called_once_with(
            "Run customized-flow-demo:issue from .ai-dev/tasks/customized-flow-demo.md"
        )

        self.assertEqual(report_code, 0)
        self.assertIn(f"Opened report in editor: {report_path}", report_out)
        self.assertIn("Warning: configured fallback used", report_err)
        self.assertEqual(fake_editor.opened_paths, [report_path])
        self.assertEqual(build_editor.call_args.args[0], "code --wait")

    def test_clipboard_failure_flow_preserves_artifacts_and_succeeds(self) -> None:
        repo_root = self._init_repo("repo-clipboard-failure-flow")
        user_config = self.tmp_path / "clipboard-failure-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard unavailable"),
            ),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "clipboard-failure-flow",
                "issue",
                "flow status -v",
                "--body",
                "Clipboard fallback body",
            )

        self.assertEqual(code, 0)
        self.assertIn("Clipboard delivery failed; falling back to stdout.", out)
        self.assertIn("Read and execute .ai-dev/tasks/clipboard-failure-flow.md", out)
        self.assertIn("Task file: .ai-dev/tasks/clipboard-failure-flow.md", out)
        self.assertIn("Warning: clipboard delivery failed", err)
        self.assertNotIn("Traceback", err)

        task_path = repo_root / ".ai-dev" / "tasks" / "clipboard-failure-flow.md"
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        self.assertTrue(task_path.exists())
        self.assertTrue(pointer_path.exists())
        self.assertIn("Task-ID: clipboard-failure-flow", pointer_path.read_text(encoding="utf-8"))

    def test_broken_config_repair_flow_opens_same_file_without_modifying_it(self) -> None:
        repo_root = self._init_repo("repo-broken-config-flow")
        user_config_path = self.tmp_path / "broken-config" / "config.yaml"
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        original = "ai:\n  invocation: [\n"
        user_config_path.write_text(original, encoding="utf-8")

        fake_editor = _FakeEditorOpener(opened=True)

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=True,
                    status="opened",
                    command_display="nano",
                    warning=None,
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertIn("Opened config with: nano", out)
        self.assertIn("Invalid YAML in AI Dev config", err)
        self.assertEqual(user_config_path.read_text(encoding="utf-8"), original)

    def test_report_flow_uses_configured_output_path_without_duplication(self) -> None:
        repo_root = self._init_repo("repo-report-flow")
        user_config = self.tmp_path / "report-user.yaml"
        user_config.write_text(
            "reports:\n"
            "  presentation: stdout\n",
            encoding="utf-8",
        )

        report_dir = repo_root / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "generated.md"
        report_path.write_text("report-line\n", encoding="utf-8")

        config_dir = repo_root / ".ai-dev"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            '{\n  "out": "reports/generated.md"\n}\n',
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "showreport")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "report-line\n")
        self.assertEqual(out.count("report-line"), 1)

    def test_unsupported_platform_fallback_flow_surfaces_paths_without_tracebacks(self) -> None:
        repo_root = self._init_repo("repo-unsupported-fallback-flow")
        user_config = self.tmp_path / "unsupported-fallback-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.platform.system", return_value="Linux"),
            patch("ai_dev_flow.task_delivery.shutil.which", return_value=None),
        ):
            prepare_code, prepare_out, prepare_err = self._invoke(
                repo_root,
                "task-prepare",
                "unsupported-flow-demo",
                "issue",
                "flow status -v",
                "--body",
                "Unsupported flow body",
            )

        self.assertEqual(prepare_code, 0)
        self.assertIn("Clipboard delivery failed; falling back to stdout.", prepare_out)
        self.assertIn("Read and execute .ai-dev/tasks/unsupported-flow-demo.md", prepare_out)
        self.assertIn("Task file: .ai-dev/tasks/unsupported-flow-demo.md", prepare_out)
        self.assertIn("No supported clipboard command is available", prepare_err)
        self.assertNotIn("Traceback", prepare_err)

        no_editor_config_path = self.tmp_path / "no-editor" / "config.yaml"
        no_editor_config_path.parent.mkdir(parents=True, exist_ok=True)
        no_editor_config_path.write_text("ai:\n  delivery: stdout\n", encoding="utf-8")

        with (
            patch.dict(
                os.environ,
                {
                    "AI_DEV_CONFIG": str(no_editor_config_path),
                    "VISUAL": "",
                    "EDITOR": "",
                },
                clear=False,
            ),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="no-editor-candidate",
                    command_display=None,
                    warning="No editor command is available on this machine.",
                ),
            ),
        ):
            config_code, config_out, config_err = self._invoke(repo_root, "config")

        self.assertEqual(config_code, 0)
        self.assertIn(f"AI Dev config: {no_editor_config_path}", config_out)
        self.assertIn("No editor could be launched. Edit this file manually.", config_out)
        self.assertIn("No editor command is available on this machine.", config_err)
        self.assertNotIn("Traceback", config_err)


if __name__ == "__main__":
    unittest.main()
