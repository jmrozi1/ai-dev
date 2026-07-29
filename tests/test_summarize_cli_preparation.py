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
from ai_dev_flow.summarize_batching import build_summarize_batches
from ai_dev_flow.summarize_config import load_repository_summarize_config
from ai_dev_flow.summarize_planning import build_summarize_plan
from ai_dev_flow.summarize_task_generation import plan_summarize_task_artifacts
from ai_dev_flow.task_config import TaskConfigError
from ai_dev_flow.task_delivery import ClipboardDeliveryError


class SummarizeCliPreparationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize CLI Preparation Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-cli-preparation-tests@example.com")
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

    def _write_basic_summarize_repo(self, repo_root: Path) -> None:
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            "    max_files: 2\n"
            "  rules:\n"
            "    - match: \"**/*.py\"\n"
            "      instructions: \"python rule\"\n",
            encoding="utf-8",
        )
        (repo_root / "src").mkdir(parents=True)
        (repo_root / "src" / "a.py").write_text("UNIQUE_SOURCE_TEXT_A\n", encoding="utf-8")
        (repo_root / "src" / "b.py").write_text("UNIQUE_SOURCE_TEXT_B\n", encoding="utf-8")
        (repo_root / "src" / "c.py").write_text("UNIQUE_SOURCE_TEXT_C\n", encoding="utf-8")

    def _planned_summarize_artifacts(self, repo_root: Path, requested_glob: str):
        summarize_config = load_repository_summarize_config(repo_root)
        plan = build_summarize_plan(repo_root, requested_glob)
        batches = build_summarize_batches(plan, max_files=summarize_config.batch_max_files)
        return plan_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)

    def test_stdout_mode_prepares_tasks_and_prints_invocation(self) -> None:
        repo_root = self._init_repo("repo-cli-stdout")
        self._write_basic_summarize_repo(repo_root)

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Read and execute .ai-dev/tasks/summarize-", out)
        self.assertIn("Prepared summarize tasks for plan", out)
        self.assertIn("Coordinator task: .ai-dev/tasks/summarize-", out)
        self.assertIn("Manifest: .ai-dev/summarize/", out)
        self.assertIn("Task file: .ai-dev/tasks/summarize-", out)
        self.assertNotIn("Execution task generation is not implemented", out)
        self.assertNotIn("UNIQUE_SOURCE_TEXT_A", out)
        self.assertNotIn("summaries completed", out.lower())

    def test_file_only_mode_suppresses_invocation_text(self) -> None:
        repo_root = self._init_repo("repo-cli-file-only")
        self._write_basic_summarize_repo(repo_root)

        user_config = self.tmp_path / "file-only.yaml"
        user_config.write_text("ai:\n  delivery: file-only\n  invocation: \"Read and execute {task_file}\"\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Task file: .ai-dev/tasks/summarize-", out)
        self.assertNotIn("Read and execute .ai-dev/tasks/summarize-", out)

    def test_clipboard_mode_success(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard")
        self._write_basic_summarize_repo(repo_root)

        user_config = self.tmp_path / "clipboard.yaml"
        user_config.write_text("ai:\n  delivery: clipboard\n  invocation: \"Read and execute {task_file}\"\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None),
        ):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Invocation copied to clipboard.", out)
        self.assertIn("Task file: .ai-dev/tasks/summarize-", out)
        self.assertNotIn("Read and execute .ai-dev/tasks/summarize-", out)

    def test_clipboard_mode_failure_falls_back_to_stdout(self) -> None:
        repo_root = self._init_repo("repo-cli-clipboard-fallback")
        self._write_basic_summarize_repo(repo_root)

        user_config = self.tmp_path / "clipboard-fallback.yaml"
        user_config.write_text("ai:\n  delivery: clipboard\n  invocation: \"Read and execute {task_file}\"\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard failed"),
            ),
        ):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 0)
        self.assertIn("Clipboard delivery failed; falling back to stdout.", out)
        self.assertIn("Read and execute .ai-dev/tasks/summarize-", out)
        self.assertIn("Task file: .ai-dev/tasks/summarize-", out)
        self.assertIn("Warning: clipboard delivery failed", err)

    def test_clipboard_stdout_mode_success_and_failure(self) -> None:
        repo_root_success = self._init_repo("repo-cli-clipboard-stdout-success")
        self._write_basic_summarize_repo(repo_root_success)
        repo_root_failure = self._init_repo("repo-cli-clipboard-stdout-failure")
        self._write_basic_summarize_repo(repo_root_failure)

        user_config = self.tmp_path / "clipboard-stdout.yaml"
        user_config.write_text("ai:\n  delivery: clipboard+stdout\n  invocation: \"Read and execute {task_file}\"\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None),
        ):
            success_code, success_out, success_err = self._invoke(repo_root_success, "summarize", "src/*.py")

        self.assertEqual(success_code, 0)
        self.assertEqual(success_err, "")
        self.assertEqual(success_out.count("Read and execute .ai-dev/tasks/summarize-"), 1)

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard failed"),
            ),
        ):
            fail_code, fail_out, fail_err = self._invoke(repo_root_failure, "summarize", "src/*.py")

        self.assertEqual(fail_code, 0)
        self.assertEqual(fail_out.count("Read and execute .ai-dev/tasks/summarize-"), 1)
        self.assertIn("Warning: clipboard delivery failed; invocation was still written to stdout", fail_err)

    def test_custom_invocation_template_supported(self) -> None:
        repo_root = self._init_repo("repo-cli-custom-invocation")
        self._write_basic_summarize_repo(repo_root)

        user_config = self.tmp_path / "custom-invocation.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Execute {task_type}:{task_id} from {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Execute summarize:summarize-", out)
        self.assertIn("from .ai-dev/tasks/summarize-", out)

    def test_malformed_invocation_leaves_no_state_and_retry_succeeds(self) -> None:
        repo_root = self._init_repo("repo-cli-malformed-invocation")
        self._write_basic_summarize_repo(repo_root)
        planned = self._planned_summarize_artifacts(repo_root, "src/*.py")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self.tmp_path / "malformed-invocation.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: clipboard\n"
            "  invocation: \"Read and execute {task_file\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text") as copy_text,
        ):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("malformed template", err)
        copy_text.assert_not_called()
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self.assertFalse((repo_root / planned.coordinator_planned.repository_relative_path).exists())
        self.assertFalse((repo_root / planned.manifest_relative_path).exists())
        for planned_batch in planned.batch_plans:
            self.assertFalse((repo_root / planned_batch.repository_relative_path).exists())

        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            retry_code, retry_out, retry_err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(retry_code, 0)
        self.assertEqual(retry_err, "")
        self.assertIn("Prepared summarize tasks for plan", retry_out)
        self.assertTrue((repo_root / planned.coordinator_planned.repository_relative_path).exists())
        self.assertTrue((repo_root / planned.manifest_relative_path).exists())

    def test_invalid_delivery_config_leaves_no_state(self) -> None:
        repo_root = self._init_repo("repo-cli-invalid-delivery-config")
        self._write_basic_summarize_repo(repo_root)
        planned = self._planned_summarize_artifacts(repo_root, "src/*.py")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self.tmp_path / "invalid-delivery.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: not-a-mode\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text") as copy_text,
        ):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unsupported value", err)
        copy_text.assert_not_called()
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self.assertFalse((repo_root / planned.coordinator_planned.repository_relative_path).exists())
        self.assertFalse((repo_root / planned.manifest_relative_path).exists())
        for planned_batch in planned.batch_plans:
            self.assertFalse((repo_root / planned_batch.repository_relative_path).exists())

    def test_adapter_construction_failure_leaves_no_state(self) -> None:
        repo_root = self._init_repo("repo-cli-adapter-construction-failure")
        self._write_basic_summarize_repo(repo_root)
        planned = self._planned_summarize_artifacts(repo_root, "src/*.py")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self.tmp_path / "adapter-failure.yaml"
        user_config.write_text(
            "ai:\n"
            "  delivery: stdout\n"
            "  invocation: \"Read and execute {task_file}\"\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", side_effect=TaskConfigError("adapter construction failed")),
        ):
            code, out, err = self._invoke(repo_root, "summarize", "src/*.py")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("adapter construction failed", err)
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self.assertFalse((repo_root / planned.coordinator_planned.repository_relative_path).exists())
        self.assertFalse((repo_root / planned.manifest_relative_path).exists())
        for planned_batch in planned.batch_plans:
            self.assertFalse((repo_root / planned_batch.repository_relative_path).exists())


if __name__ == "__main__":
    unittest.main()
