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
from ai_dev_flow import task_artifacts as task_artifacts_module
from ai_dev_flow.editor_opening import EditorOpenResult
from ai_dev_flow.editor_selection import EditorLaunchResult
from ai_dev_flow.json_files import JsonFileError
from ai_dev_flow.task_artifacts import TaskArtifactError, create_generated_task
from ai_dev_flow.task_config import TaskConfigError, load_task_config
from ai_dev_flow.task_delivery import ClipboardDeliveryError
from ai_dev_flow.task_delivery import FileOnlyDeliveryAdapter, StdoutDeliveryAdapter
from ai_dev_flow.task_invocation import render_invocation


class TaskSliceOneTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Task Slice Tests")
        self._run_git(repo_root, "config", "user.email", "task-slice-tests@example.com")
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

        os.environ["FLOW_COMMAND_NAME"] = "flow"
        sys.argv = ["flow", *arguments]
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

    def test_default_configuration(self) -> None:
        repo_root = self._init_repo("repo-default-config")

        with patch.dict(os.environ, {}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "stdout")
        self.assertEqual(config.invocation, "Read and execute {task_file}")
        self.assertIsNone(config.editor_command)
        self.assertEqual(config.report_presentation, "stdout")

    def test_user_configuration_loading(self) -> None:
        repo_root = self._init_repo("repo-user-config")
        user_config = self.tmp_path / "user-config.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: file-only\n"
            "  invocation: \"Execute {task_id} at {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "file-only")
        self.assertEqual(config.invocation, "Execute {task_id} at {task_file}")

    def test_user_configuration_accepts_clipboard_delivery(self) -> None:
        repo_root = self._init_repo("repo-user-clipboard")
        user_config = self.tmp_path / "user-clipboard.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Execute {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "clipboard")

    def test_user_configuration_accepts_clipboard_stdout_delivery(self) -> None:
        repo_root = self._init_repo("repo-user-clipboard-stdout")
        user_config = self.tmp_path / "user-clipboard-stdout.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard+stdout\n"
            "  invocation: \"Execute {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "clipboard+stdout")

    def test_repository_config_compatible_and_machine_preferences_ignored(self) -> None:
        repo_root = self._init_repo("repo-repo-config")
        (repo_root / ".ai-dev.yaml").write_text(
            "provider:\n"
            "  default: copilot\n"
            "summarization:\n"
            "  mode: assisted\n"
            "taskExecution:\n"
            "  delivery: file-only\n"
            "  invocation: \"Repo says {task_type} {task_id}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-user.yaml")}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "stdout")
        self.assertEqual(config.invocation, "Read and execute {task_file}")

    def test_user_preference_protection_against_repo_override(self) -> None:
        repo_root = self._init_repo("repo-precedence")
        user_config = self.tmp_path / "user-preference.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"User {task_file}\"\n",
            encoding="utf-8",
        )
        (repo_root / ".ai-dev.yaml").write_text(
            "taskExecution:\n"
            "  delivery: file-only\n"
            "  invocation: \"Repo {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "stdout")
        self.assertEqual(config.invocation, "User {task_file}")

    def test_environment_override_for_user_config_path(self) -> None:
        repo_root = self._init_repo("repo-env-override")
        overridden = self.tmp_path / "custom-user-config.yaml"
        overridden.write_text(
            "ai:\n"
            "  delivery: file-only\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(overridden)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "file-only")

    def test_user_config_unknown_root_key_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-unknown-key")
        bad_path = self.tmp_path / "bad-user.yaml"
        bad_path.write_text(
            "unexpected: value\n"
            "ai:\n"
            "  delivery: stdout\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(bad_path)}, clear=False):
            with self.assertRaises(TaskConfigError) as context:
                load_task_config(repo_root)

        text = str(context.exception)
        self.assertIn(str(bad_path), text)
        self.assertIn("at <root>", text)
        self.assertIn("Expected keys: ai, aliases, editor, reports", text)

    def test_user_editor_command_is_loaded(self) -> None:
        repo_root = self._init_repo("repo-editor-config")
        user_config = self.tmp_path / "editor-user.yaml"
        user_config.write_text(
            "editor:\n"
            "  command: \"code --wait\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.editor_command, "code --wait")

    def test_empty_editor_command_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-editor-empty")
        user_config = self.tmp_path / "editor-empty.yaml"
        user_config.write_text(
            "editor:\n"
            "  command: \"   \"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            with self.assertRaises(TaskConfigError) as context:
                load_task_config(repo_root)

        text = str(context.exception)
        self.assertIn("editor.command", text)
        self.assertIn("expected non-empty string", text)

    def test_unknown_editor_key_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-editor-unknown")
        user_config = self.tmp_path / "editor-unknown.yaml"
        user_config.write_text(
            "editor:\n"
            "  unknown: value\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            with self.assertRaises(TaskConfigError) as context:
                load_task_config(repo_root)

        text = str(context.exception)
        self.assertIn("at editor", text)
        self.assertIn("Expected keys: command", text)

    def test_allowed_report_presentation_values(self) -> None:
        repo_root = self._init_repo("repo-report-presentations")
        values = ["stdout", "editor", "path-only"]

        for value in values:
            user_config = self.tmp_path / f"report-{value}.yaml"
            user_config.write_text(
                "reports:\n"
                f"  presentation: {value}\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
                config = load_task_config(repo_root)

            self.assertEqual(config.report_presentation, value)

    def test_invalid_report_presentation_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-report-invalid")
        user_config = self.tmp_path / "report-invalid.yaml"
        user_config.write_text(
            "reports:\n"
            "  presentation: invalid\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            with self.assertRaises(TaskConfigError) as context:
                load_task_config(repo_root)

        text = str(context.exception)
        self.assertIn("reports.presentation", text)
        self.assertIn("stdout, editor, path-only", text)

    def test_repository_values_resembling_machine_preferences_are_ignored(self) -> None:
        repo_root = self._init_repo("repo-machine-pref-ignored")
        repo_path = repo_root / ".ai-dev.yaml"
        repo_path.write_text(
            "taskExecution:\n"
            "  delivery: file-only\n"
            "  invocation: \"Repo-run {task_file}\"\n"
            "knowledge:\n"
            "  roots:\n"
            "    - docs/\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing.yaml")}, clear=False):
            config = load_task_config(repo_root)

        self.assertEqual(config.delivery, "stdout")
        self.assertEqual(config.invocation, "Read and execute {task_file}")
        self.assertIsNone(config.editor_command)
        self.assertEqual(config.report_presentation, "stdout")
        self.assertEqual(config.repository_config_path, repo_path)

    def test_repository_cannot_override_editor_or_reports_preferences(self) -> None:
        repo_root = self._init_repo("repo-editor-reports-ignored")
        (repo_root / ".ai-dev.yaml").write_text(
            "editor:\n"
            "  command: \"repo-editor\"\n"
            "reports:\n"
            "  presentation: editor\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-user.yaml")}, clear=False):
            config = load_task_config(repo_root)

        self.assertIsNone(config.editor_command)
        self.assertEqual(config.report_presentation, "stdout")

    def test_invalid_delivery_error_contains_expected_values(self) -> None:
        repo_root = self._init_repo("repo-invalid-delivery")
        user_path = self.tmp_path / "invalid-user.yaml"
        user_path.write_text(
            "ai:\n"
            "  delivery: invalid-mode\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_path)}, clear=False):
            with self.assertRaises(TaskConfigError) as context:
                load_task_config(repo_root)

        text = str(context.exception)
        self.assertIn(str(user_path), text)
        self.assertIn("ai.delivery", text)
        self.assertIn("stdout, file-only, clipboard, clipboard+stdout", text)

    def test_multiline_yaml_invocation_is_preserved(self) -> None:
        repo_root = self._init_repo("repo-multiline")
        user_path = self.tmp_path / "multiline-user.yaml"
        user_path.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: |\n"
            "    Read and execute {task_file}\n"
            "    Task: {task_id}\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_path)}, clear=False):
            config = load_task_config(repo_root)

        rendered = render_invocation(
            config.invocation,
            task_file=".ai-dev/tasks/demo.md",
            task_id="demo",
            task_type="investigation",
            config_path=user_path,
        )
        self.assertIn("Read and execute .ai-dev/tasks/demo.md", rendered)
        self.assertIn("Task: demo", rendered)

    def test_unknown_invocation_variable_rejected(self) -> None:
        with self.assertRaises(TaskConfigError) as context:
            render_invocation(
                "Do {unknown} with {task_file}",
                task_file=".ai-dev/tasks/t.md",
                task_id="t",
                task_type="investigation",
                config_path=Path("/tmp/config.yaml"),
            )

        text = str(context.exception)
        self.assertIn("ai.invocation", text)
        self.assertIn("unknown template variable", text)

    def test_immutable_task_creation_and_pointer_update(self) -> None:
        repo_root = self._init_repo("repo-artifacts")

        first = create_generated_task(
            repo_root=repo_root,
            task_id="task-one",
            task_type="investigation",
            requested_command="flow task-prepare",
            task_body="Body one",
            constraints="Constraint one",
            expected_output="Output one",
        )
        second = create_generated_task(
            repo_root=repo_root,
            task_id="task-two",
            task_type="investigation",
            requested_command="flow task-prepare",
            task_body="Body two",
            constraints="Constraint two",
            expected_output="Output two",
        )

        self.assertTrue((repo_root / first.repository_relative_path).exists())
        self.assertTrue((repo_root / second.repository_relative_path).exists())

        pointer_text = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")
        self.assertIn("Task-ID: task-two", pointer_text)
        self.assertIn("Task-File: .ai-dev/tasks/task-two.md", pointer_text)

    def test_overwrite_rejection(self) -> None:
        repo_root = self._init_repo("repo-overwrite")

        create_generated_task(
            repo_root=repo_root,
            task_id="same-id",
            task_type="investigation",
            requested_command="flow task-prepare",
            task_body="Body",
            constraints="Constraint",
            expected_output="Output",
        )

        with self.assertRaises(TaskArtifactError):
            create_generated_task(
                repo_root=repo_root,
                task_id="same-id",
                task_type="investigation",
                requested_command="flow task-prepare",
                task_body="Body",
                constraints="Constraint",
                expected_output="Output",
            )

    def test_stdout_and_file_only_delivery(self) -> None:
        stdout_capture = io.StringIO()
        with redirect_stdout(stdout_capture):
            StdoutDeliveryAdapter().deliver("hello")
        self.assertEqual(stdout_capture.getvalue(), "hello\n")

        quiet_capture = io.StringIO()
        with redirect_stdout(quiet_capture):
            FileOnlyDeliveryAdapter().deliver("hello")
        self.assertEqual(quiet_capture.getvalue(), "")

    def test_fake_delivery_adapter_injection(self) -> None:
        repo_root = self._init_repo("repo-fake-adapter")
        user_config = self.tmp_path / "fake-adapter-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        class FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def deliver(self, invocation_text: str) -> None:
                self.calls.append(invocation_text)

        fake_adapter = FakeAdapter()

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=fake_adapter),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "inject-demo",
                "investigation",
                "flow status -v",
                "--body",
                "hello body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Task file: .ai-dev/tasks/inject-demo.md", out)
        self.assertEqual(len(fake_adapter.calls), 1)
        self.assertIn(".ai-dev/tasks/inject-demo.md", fake_adapter.calls[0])

    def test_task_id_validation_rejects_unsafe_ids(self) -> None:
        repo_root = self._init_repo("repo-task-id-invalid")
        invalid_ids = [
            "../escaped",
            "subdir/task",
            "subdir\\task",
            "/tmp/absolute-like",
            "C:/absolute-like",
            ".",
            "..",
        ]

        for invalid_id in invalid_ids:
            with self.assertRaises(TaskArtifactError) as context:
                create_generated_task(
                    repo_root=repo_root,
                    task_id=invalid_id,
                    task_type="investigation",
                    requested_command="flow task-prepare",
                    task_body="Body",
                    constraints="Constraint",
                    expected_output="Output",
                )
            self.assertIn("Invalid task id", str(context.exception))

    def test_task_id_validation_accepts_valid_representative(self) -> None:
        repo_root = self._init_repo("repo-task-id-valid")

        generated = create_generated_task(
            repo_root=repo_root,
            task_id="task-1.alpha_beta",
            task_type="investigation",
            requested_command="flow task-prepare",
            task_body="Body",
            constraints="Constraint",
            expected_output="Output",
        )

        self.assertEqual(generated.repository_relative_path, ".ai-dev/tasks/task-1.alpha_beta.md")
        self.assertTrue((repo_root / generated.repository_relative_path).exists())

    def test_pointer_write_failure_rolls_back_new_task_and_preserves_prior_pointer(self) -> None:
        repo_root = self._init_repo("repo-pointer-rollback")
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer content\n", encoding="utf-8")

        original_write_text_atomic = task_artifacts_module.write_text_atomic

        def fail_pointer_write(path: Path, text: str) -> None:
            if path.name == "current-task.md":
                raise JsonFileError("Cannot write current pointer")
            original_write_text_atomic(path, text)

        with patch("ai_dev_flow.task_artifacts.write_text_atomic", side_effect=fail_pointer_write):
            with self.assertRaises(TaskArtifactError) as context:
                create_generated_task(
                    repo_root=repo_root,
                    task_id="rollback-target",
                    task_type="investigation",
                    requested_command="flow task-prepare",
                    task_body="Body",
                    constraints="Constraint",
                    expected_output="Output",
                )

        self.assertIn("Rolled back newly created task file", str(context.exception))
        self.assertFalse((repo_root / ".ai-dev" / "tasks" / "rollback-target.md").exists())
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer content\n")

    def test_pointer_write_failure_reports_cleanup_failure(self) -> None:
        repo_root = self._init_repo("repo-pointer-cleanup-failure")

        original_write_text_atomic = task_artifacts_module.write_text_atomic

        def fail_pointer_write(path: Path, text: str) -> None:
            if path.name == "current-task.md":
                raise JsonFileError("Cannot write current pointer")
            original_write_text_atomic(path, text)

        with (
            patch("ai_dev_flow.task_artifacts.write_text_atomic", side_effect=fail_pointer_write),
            patch("ai_dev_flow.task_artifacts._remove_generated_task_file", side_effect=OSError("cleanup failed")),
        ):
            with self.assertRaises(TaskArtifactError) as context:
                create_generated_task(
                    repo_root=repo_root,
                    task_id="cleanup-target",
                    task_type="investigation",
                    requested_command="flow task-prepare",
                    task_body="Body",
                    constraints="Constraint",
                    expected_output="Output",
                )

        text = str(context.exception)
        self.assertIn("Cannot write current pointer", text)
        self.assertIn("Cleanup failure", text)

    def test_task_prepare_rejects_conflicting_body_options(self) -> None:
        with self.assertRaises(cli.FlowError) as context:
            cli._parse_task_prepare_options(
                "flow",
                ["--body", "inline", "--body-file", "body.md"],
            )
        self.assertIn("Specify exactly one of --body or --body-file", str(context.exception))

    def test_task_prepare_rejects_duplicate_body_options(self) -> None:
        with self.assertRaises(cli.FlowError) as inline_context:
            cli._parse_task_prepare_options(
                "flow",
                ["--body", "one", "--body", "two"],
            )
        self.assertIn("Specify exactly one of --body or --body-file", str(inline_context.exception))

        file_body = self.tmp_path / "duplicate-body-file.md"
        file_body.write_text("payload\n", encoding="utf-8")
        with self.assertRaises(cli.FlowError) as file_context:
            cli._parse_task_prepare_options(
                "flow",
                ["--body-file", str(file_body), "--body-file", str(file_body)],
            )
        self.assertIn("Specify exactly one of --body or --body-file", str(file_context.exception))

    def test_task_prepare_rejects_duplicate_constraints_and_expected_output(self) -> None:
        with self.assertRaises(cli.FlowError) as constraints_context:
            cli._parse_task_prepare_options(
                "flow",
                ["--body", "inline", "--constraints", "c1", "--constraints", "c2"],
            )
        self.assertIn("--constraints may be provided at most once", str(constraints_context.exception))

        with self.assertRaises(cli.FlowError) as expected_context:
            cli._parse_task_prepare_options(
                "flow",
                [
                    "--body",
                    "inline",
                    "--expected-output",
                    "o1",
                    "--expected-output",
                    "o2",
                ],
            )
        self.assertIn("--expected-output may be provided at most once", str(expected_context.exception))

    def test_task_prepare_accepts_exactly_one_body_source(self) -> None:
        file_body = self.tmp_path / "task-body.md"
        file_body.write_text("body from file\n", encoding="utf-8")

        body_text, constraints, expected_output = cli._parse_task_prepare_options(
            "flow",
            ["--body-file", str(file_body)],
        )

        self.assertEqual(body_text, "body from file\n")
        self.assertEqual(constraints, "(none)")
        self.assertEqual(expected_output, "(none)")

    def test_invalid_invocation_template_does_not_create_or_update_task_artifacts(self) -> None:
        repo_root = self._init_repo("repo-invalid-invocation-no-artifact")
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer content\n", encoding="utf-8")

        user_config = self.tmp_path / "invalid-invocation-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Run {unknown} and {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "invalid-template-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo body",
            )

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown template variable", err)
        self.assertFalse((repo_root / ".ai-dev" / "tasks" / "invalid-template-demo.md").exists())
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer content\n")

    def _assert_malformed_template_fails_cleanly(self, invocation_template: str) -> None:
        repo_root = self._init_repo("repo-malformed-template")
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer content\n", encoding="utf-8")

        user_config = self.tmp_path / "malformed-invocation-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            f"  invocation: {invocation_template!r}\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "malformed-template-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo body",
            )

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("malformed template", err)
        self.assertIn(str(user_config), err)
        self.assertIn("ai.invocation", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((repo_root / ".ai-dev" / "tasks" / "malformed-template-demo.md").exists())
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer content\n")

    def test_malformed_invocation_template_unclosed_brace(self) -> None:
        self._assert_malformed_template_fails_cleanly("Read and execute {task_file")

    def test_malformed_invocation_template_unexpected_closing_brace(self) -> None:
        self._assert_malformed_template_fails_cleanly("Read and execute }task_file{")

    def test_malformed_invocation_template_invalid_conversion(self) -> None:
        self._assert_malformed_template_fails_cleanly("{task_file!invalid}")

    def test_malformed_invocation_template_invalid_format_spec(self) -> None:
        self._assert_malformed_template_fails_cleanly("{task_file:invalid-format}")

    def test_cli_task_prepare_end_to_end(self) -> None:
        repo_root = self._init_repo("repo-cli-proof")
        user_config = self.tmp_path / "cli-proof-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Read and execute {task_file} ({task_id}/{task_type})\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "slice1-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
                "--constraints",
                "Do not modify source files.",
                "--expected-output",
                "Write report to out.txt",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Read and execute .ai-dev/tasks/slice1-demo.md (slice1-demo/investigation)", out)
        self.assertIn("Task file: .ai-dev/tasks/slice1-demo.md", out)

        task_path = repo_root / ".ai-dev" / "tasks" / "slice1-demo.md"
        self.assertTrue(task_path.exists())
        task_text = task_path.read_text(encoding="utf-8")
        self.assertIn("Task-ID: slice1-demo", task_text)
        self.assertIn("Task-Type: investigation", task_text)
        self.assertIn("Requested-Command: flow status -v", task_text)

        current_task_path = repo_root / ".ai-dev" / "current-task.md"
        self.assertTrue(current_task_path.exists())

    def test_cli_task_prepare_file_only_delivery_is_silent_for_invocation(self) -> None:
        repo_root = self._init_repo("repo-cli-file-only")
        user_config = self.tmp_path / "cli-file-only-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: file-only\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "file-only-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Task file: .ai-dev/tasks/file-only-demo.md", out)
        self.assertNotIn("Read and execute .ai-dev/tasks/file-only-demo.md", out)

    def test_cli_task_prepare_clipboard_success(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard-success")
        user_config = self.tmp_path / "cli-clipboard-success-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "clipboard-success-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Invocation copied to clipboard.", out)
        self.assertIn("Task file: .ai-dev/tasks/clipboard-success-demo.md", out)
        self.assertNotIn("Read and execute .ai-dev/tasks/clipboard-success-demo.md", out)

    def test_cli_task_prepare_clipboard_failure_falls_back_to_stdout_and_succeeds(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard-failure")
        user_config = self.tmp_path / "cli-clipboard-failure-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer content\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard failed"),
            ),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "clipboard-failure-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
            )

        self.assertEqual(code, 0)
        self.assertIn("Clipboard delivery failed; falling back to stdout.", out)
        self.assertIn("Read and execute .ai-dev/tasks/clipboard-failure-demo.md", out)
        self.assertIn("Task file: .ai-dev/tasks/clipboard-failure-demo.md", out)
        self.assertIn("Warning: clipboard delivery failed", err)
        self.assertTrue((repo_root / ".ai-dev" / "tasks" / "clipboard-failure-demo.md").exists())
        self.assertIn("Task-ID: clipboard-failure-demo", pointer_path.read_text(encoding="utf-8"))

    def test_cli_task_prepare_clipboard_stdout_success_prints_invocation_once(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard-stdout-success")
        user_config = self.tmp_path / "cli-clipboard-stdout-success-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard+stdout\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "clipboard-stdout-success-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.count("Read and execute .ai-dev/tasks/clipboard-stdout-success-demo.md"), 1)
        self.assertIn("Invocation copied to clipboard.", out)
        self.assertIn("Task file: .ai-dev/tasks/clipboard-stdout-success-demo.md", out)

    def test_cli_task_prepare_clipboard_stdout_failure_still_succeeds(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard-stdout-failure")
        user_config = self.tmp_path / "cli-clipboard-stdout-failure-user.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard+stdout\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard failed"),
            ),
        ):
            code, out, err = self._invoke(
                repo_root,
                "task-prepare",
                "clipboard-stdout-failure-demo",
                "investigation",
                "flow status -v",
                "--body",
                "Demo task body",
            )

        self.assertEqual(code, 0)
        self.assertEqual(out.count("Read and execute .ai-dev/tasks/clipboard-stdout-failure-demo.md"), 1)
        self.assertNotIn("Clipboard delivery failed; falling back to stdout.", out)
        self.assertIn("Task file: .ai-dev/tasks/clipboard-stdout-failure-demo.md", out)
        self.assertIn("Warning: clipboard delivery failed; invocation was still written to stdout", err)

    def test_flow_config_creates_missing_file_and_invokes_editor(self) -> None:
        repo_root = self._init_repo("repo-flow-config-create")
        user_config_path = self.tmp_path / "cfg" / "ai-dev" / "config.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
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
        self.assertIn(f"Created AI Dev config: {user_config_path}", out)
        self.assertIn("Opened config with: code --wait", out)
        self.assertTrue(user_config_path.exists())
        text = user_config_path.read_text(encoding="utf-8")
        self.assertIn("ai:", text)
        self.assertIn("delivery: stdout", text)
        self.assertIn("reports:", text)

    def test_flow_config_preserves_existing_file(self) -> None:
        repo_root = self._init_repo("repo-flow-config-preserve")
        user_config_path = self.tmp_path / "preserve" / "config.yaml"
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "ai:\n  delivery: file-only\n"
        user_config_path.write_text(original_text, encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
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
        self.assertNotIn("Created AI Dev config", out)
        self.assertIn(f"AI Dev config: {user_config_path}", out)
        self.assertEqual(user_config_path.read_text(encoding="utf-8"), original_text)

    def test_flow_config_editor_unavailable_prints_path(self) -> None:
        repo_root = self._init_repo("repo-flow-config-fallback")
        user_config_path = self.tmp_path / "fallback" / "config.yaml"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
            patch(
                "ai_dev_flow.cli.launch_selected_editor",
                return_value=EditorLaunchResult(
                    opened=False,
                    status="no-editor-candidate",
                    command_display=None,
                    warning="editor unavailable",
                ),
            ),
        ):
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertIn(f"Created AI Dev config: {user_config_path}", out)
        self.assertIn("No editor could be launched. Edit this file manually.", out)
        self.assertIn("Warning: editor unavailable", err)

    def test_flow_config_malformed_yaml_still_opens_file_with_fallback_editor(self) -> None:
        repo_root = self._init_repo("repo-flow-config-malformed")
        user_config_path = self.tmp_path / "malformed" / "config.yaml"
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "ai:\n  invocation: [\n"
        user_config_path.write_text(original_text, encoding="utf-8")

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
        self.assertEqual(user_config_path.read_text(encoding="utf-8"), original_text)

    def test_flow_config_invalid_field_still_opens_file_with_fallback_editor(self) -> None:
        repo_root = self._init_repo("repo-flow-config-invalid-field")
        user_config_path = self.tmp_path / "invalid-field" / "config.yaml"
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "editor:\n  command: \"   \"\n"
        user_config_path.write_text(original_text, encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
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
        self.assertIn("Opened config with: vim", out)
        self.assertIn("Invalid AI Dev config", err)
        self.assertEqual(user_config_path.read_text(encoding="utf-8"), original_text)

    def test_flow_config_broken_file_and_no_editor_prints_path(self) -> None:
        repo_root = self._init_repo("repo-flow-config-broken-no-editor")
        user_config_path = self.tmp_path / "broken-no-editor" / "config.yaml"
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        original_text = "ai:\n  delivery: invalid-mode\n"
        user_config_path.write_text(original_text, encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config_path)}, clear=False),
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
            code, out, err = self._invoke(repo_root, "config")

        self.assertEqual(code, 0)
        self.assertIn(f"AI Dev config: {user_config_path}", out)
        self.assertIn("No editor could be launched. Edit this file manually.", out)
        self.assertIn("No editor command is available on this machine.", err)
        self.assertEqual(user_config_path.read_text(encoding="utf-8"), original_text)

    def test_showreport_uses_default_output_path_and_stdout_mode_once(self) -> None:
        repo_root = self._init_repo("repo-showreport-default")
        report_path = repo_root / "out.txt"
        report_text = "Issue report\nline 2\n"
        report_path.write_text(report_text, encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-user.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "showreport")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, report_text)
        self.assertEqual(out.count("Issue report"), 1)

    def test_showreport_uses_configured_output_path(self) -> None:
        repo_root = self._init_repo("repo-showreport-configured")
        reports_dir = repo_root / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / "custom.md"
        report_path.write_text("Configured report\n", encoding="utf-8")
        config_dir = repo_root / ".ai-dev"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            '{\n  "out": "reports/custom.md"\n}\n',
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-user.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "showreport")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "Configured report\n")

    def test_showreport_path_only_mode(self) -> None:
        repo_root = self._init_repo("repo-showreport-path-only")
        report_path = repo_root / "out.txt"
        report_path.write_text("Path only report\n", encoding="utf-8")
        user_config = self.tmp_path / "showreport-path-only.yaml"
        user_config.write_text("reports:\n  presentation: path-only\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "showreport")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, f"Report path: {report_path}\n")

    def test_showreport_editor_mode_and_failure_path_fallback(self) -> None:
        repo_root = self._init_repo("repo-showreport-editor")
        report_path = repo_root / "out.txt"
        report_path.write_text("Editor report\n", encoding="utf-8")
        user_config = self.tmp_path / "showreport-editor.yaml"
        user_config.write_text("reports:\n  presentation: editor\n", encoding="utf-8")

        class FakeOpener:
            def __init__(self, opened: bool) -> None:
                self.opened = opened
                self.calls: list[Path] = []

            def open_path(self, target_path: Path) -> EditorOpenResult:
                self.calls.append(target_path)
                if self.opened:
                    return EditorOpenResult(opened=True)
                return EditorOpenResult(opened=False, warning="launch failed")

        success_opener = FakeOpener(opened=True)
        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_editor_opener", return_value=success_opener),
        ):
            success_code, success_out, success_err = self._invoke(repo_root, "showreport")

        self.assertEqual(success_code, 0)
        self.assertEqual(success_err, "")
        self.assertIn(f"Opened report in editor: {report_path}", success_out)
        self.assertEqual(success_opener.calls, [report_path])

        failure_opener = FakeOpener(opened=False)
        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_editor_opener", return_value=failure_opener),
        ):
            failure_code, failure_out, failure_err = self._invoke(repo_root, "showreport")

        self.assertEqual(failure_code, 0)
        self.assertIn(f"Report path: {report_path}", failure_out)
        self.assertIn("Warning: launch failed", failure_err)

    def test_showreport_missing_report_prints_path_on_failure(self) -> None:
        repo_root = self._init_repo("repo-showreport-missing")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-user.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "showreport")

        expected_path = repo_root / "out.txt"
        self.assertEqual(code, 1)
        self.assertIn(f"Report path: {expected_path}", out)
        self.assertIn("Report file does not exist", err)

    def test_showreport_fake_presenter_injection(self) -> None:
        repo_root = self._init_repo("repo-showreport-fake-presenter")
        report_path = repo_root / "out.txt"
        report_path.write_text("presenter source\n", encoding="utf-8")
        received_paths: list[Path] = []

        class FakePresenter:
            def present(self, resolved_path: Path) -> None:
                received_paths.append(resolved_path)
                print("fake presenter output")

        with patch("ai_dev_flow.cli.build_report_presenter", return_value=FakePresenter()):
            code, out, err = self._invoke(repo_root, "showreport")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(received_paths, [report_path])
        self.assertEqual(out, "fake presenter output\n")


if __name__ == "__main__":
    unittest.main()
