from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.review_task_generation import build_review_task_id
from ai_dev_flow.task_config import TaskConfigError
from ai_dev_flow.task_delivery import ClipboardDeliveryError


class ReviewCliPreparationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Review CLI Preparation Tests")
        self._run_git(repo_root, "config", "user.email", "review-cli-prep-tests@example.com")
        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")
        self._run_git(repo_root, "branch", "-M", "main")
        return repo_root

    def _activate_issue_workflow(self, repo_root: Path, issue_number: int) -> None:
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": issue_number,
                    "activeIssueTitle": f"Issue {issue_number}",
                    "activeIssueUrl": f"https://example.test/issues/{issue_number}",
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _activate_patch_workflow(self, repo_root: Path, description: str) -> None:
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "patchDescription": description,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

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

    def _write_user_config(self, name: str, text: str) -> Path:
        config_path = self.tmp_path / name
        config_path.write_text(text, encoding="utf-8")
        return config_path

    def _latest_review_dir(self, repo_root: Path) -> Path:
        reviews_root = repo_root / ".ai-dev" / "reviews"
        self.assertTrue(reviews_root.exists())
        review_dirs = sorted(path for path in reviews_root.iterdir() if path.is_dir())
        self.assertTrue(review_dirs)
        return review_dirs[-1]

    def test_review_stdout_default_scope_prepares_task_and_pointer(self) -> None:
        repo_root = self._init_repo("repo-review-cli-default")
        self._activate_issue_workflow(repo_root, issue_number=71)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Read and execute .ai-dev/tasks/", out)
        self.assertIn("Prepared review task for review-", out)
        self.assertIn("Review task: .ai-dev/tasks/", out)
        self.assertIn("Review package: .ai-dev/reviews/", out)
        self.assertIn("Changes: .ai-dev/reviews/", out)
        self.assertIn("Expected report: .ai-dev/reviews/", out)
        self.assertIn("diff --git a/wip.txt b/wip.txt", out)
        self.assertNotIn("review is complete", out.lower())

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        task_id = build_review_task_id(payload["review_id"])
        task_path = repo_root / ".ai-dev" / "tasks" / f"{task_id}.md"
        self.assertTrue(task_path.exists())

        pointer = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")
        self.assertIn(f"Task-ID: {task_id}", pointer)
        self.assertIn("Task-Type: review", pointer)
        self.assertIn(f"Task-File: .ai-dev/tasks/{task_id}.md", pointer)

    def test_review_all_and_short_flag_keep_scope_semantics_and_same_output(self) -> None:
        repo_root = self._init_repo("repo-review-cli-all")
        self._activate_issue_workflow(repo_root, issue_number=72)

        (repo_root / "checkpoint.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        (repo_root / "overlay.txt").write_text("overlay\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            long_code, long_out, long_err = self._invoke(repo_root, "review", "--all")
            short_code, short_out, short_err = self._invoke(repo_root, "review", "-a")

        self.assertEqual(long_code, 0)
        self.assertEqual(short_code, 0)
        self.assertEqual(long_err, "")
        self.assertEqual(short_err, "")
        self.assertEqual(long_out, short_out)
        self.assertIn("diff --git a/checkpoint.txt b/checkpoint.txt", long_out)
        self.assertIn("diff --git a/overlay.txt b/overlay.txt", long_out)

    def test_review_task_references_package_without_embedding_diff(self) -> None:
        repo_root = self._init_repo("repo-review-cli-no-diff-embed")
        self._activate_issue_workflow(repo_root, issue_number=73)
        (repo_root / "snippet.txt").write_text("distinctive-line-4455\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, _, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        task_id = build_review_task_id(payload["review_id"])
        task_text = (repo_root / ".ai-dev" / "tasks" / f"{task_id}.md").read_text(encoding="utf-8")
        changes_text = (latest / "changes.diff").read_text(encoding="utf-8")

        self.assertIn("distinctive-line-4455", changes_text)
        self.assertNotIn("distinctive-line-4455", task_text)
        self.assertIn(f"Changes-Diff-Path: {payload['artifacts']['changes_diff_path']}", task_text)

    def test_patch_workflow_task_metadata(self) -> None:
        repo_root = self._init_repo("repo-review-cli-patch")
        self._activate_patch_workflow(repo_root, description="Fix local docs")
        (repo_root / "patch.txt").write_text("patch\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Patch: Fix local docs", out)

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        task_id = build_review_task_id(payload["review_id"])
        task_text = (repo_root / ".ai-dev" / "tasks" / f"{task_id}.md").read_text(encoding="utf-8")
        self.assertIn("- Workflow-Type: patch", task_text)
        self.assertIn("- Patch-Description: Fix local docs", task_text)

    def test_malformed_invocation_leaves_no_task_or_pointer_changes(self) -> None:
        repo_root = self._init_repo("repo-review-cli-malformed-invocation")
        self._activate_issue_workflow(repo_root, issue_number=74)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-malformed.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("malformed template", err)
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self.assertFalse((repo_root / ".ai-dev" / "tasks").exists())
        self.assertFalse((repo_root / ".ai-dev" / "reviews").exists())

    def test_invalid_delivery_and_adapter_failure_leave_no_writes(self) -> None:
        repo_root_invalid = self._init_repo("repo-review-cli-invalid-delivery")
        self._activate_issue_workflow(repo_root_invalid, issue_number=75)
        (repo_root_invalid / "wip.txt").write_text("wip\n", encoding="utf-8")

        invalid_config = self._write_user_config(
            "review-invalid-delivery.yaml",
            "ai:\n  delivery: not-a-mode\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(invalid_config)}, clear=False):
            invalid_code, invalid_out, invalid_err = self._invoke(repo_root_invalid, "review")

        self.assertEqual(invalid_code, 1)
        self.assertEqual(invalid_out, "")
        self.assertIn("unsupported value", invalid_err)
        self.assertFalse((repo_root_invalid / ".ai-dev" / "tasks").exists())
        self.assertFalse((repo_root_invalid / ".ai-dev" / "reviews").exists())

        repo_root_adapter = self._init_repo("repo-review-cli-adapter-failure")
        self._activate_issue_workflow(repo_root_adapter, issue_number=76)
        (repo_root_adapter / "wip.txt").write_text("wip\n", encoding="utf-8")

        valid_config = self._write_user_config(
            "review-adapter-failure.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(valid_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", side_effect=TaskConfigError("adapter construction failed")),
        ):
            adapter_code, adapter_out, adapter_err = self._invoke(repo_root_adapter, "review")

        self.assertEqual(adapter_code, 1)
        self.assertEqual(adapter_out, "")
        self.assertIn("adapter construction failed", adapter_err)
        self.assertFalse((repo_root_adapter / ".ai-dev" / "tasks").exists())
        self.assertFalse((repo_root_adapter / ".ai-dev" / "reviews").exists())

    def test_pointer_write_failure_rolls_back_new_task_and_package(self) -> None:
        repo_root = self._init_repo("repo-review-cli-pointer-rollback")
        self._activate_issue_workflow(repo_root, issue_number=77)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-pointer-fail.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.write_current_task_pointer", side_effect=cli.ReviewTaskGenerationError("pointer write failed")),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Review task preparation failed", err)
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        tasks_root = repo_root / ".ai-dev" / "tasks"
        if tasks_root.exists():
            task_files = [path for path in tasks_root.iterdir() if path.is_file()]
            self.assertEqual(task_files, [])

        reviews_root = repo_root / ".ai-dev" / "reviews"
        if reviews_root.exists():
            review_dirs = [path for path in reviews_root.iterdir() if path.is_dir()]
            self.assertEqual(review_dirs, [])

    def test_existing_immutable_package_is_preserved_on_later_failure(self) -> None:
        repo_root = self._init_repo("repo-review-cli-preserve-existing-package")
        self._activate_issue_workflow(repo_root, issue_number=78)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-preserve.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        first_review_dir = self._latest_review_dir(repo_root)
        first_payload = json.loads((first_review_dir / "package.json").read_text(encoding="utf-8"))
        first_pointer = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.write_current_task_pointer", side_effect=cli.ReviewTaskGenerationError("pointer write failed")),
        ):
            second_code, second_out, second_err = self._invoke(repo_root, "review")

        self.assertEqual(second_code, 1)
        self.assertEqual(second_out, "")
        self.assertIn("Review task preparation failed", second_err)

        second_review_dir = self._latest_review_dir(repo_root)
        second_payload = json.loads((second_review_dir / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(first_review_dir, second_review_dir)
        self.assertEqual(first_payload["review_id"], second_payload["review_id"])
        self.assertTrue((first_review_dir / "package.md").exists())
        self.assertTrue((first_review_dir / "package.json").exists())
        self.assertTrue((first_review_dir / "changes.diff").exists())
        self.assertEqual((repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8"), first_pointer)

    def test_clipboard_modes_deliver_compact_task_reference_once(self) -> None:
        repo_root_clipboard = self._init_repo("repo-review-cli-clipboard")
        self._activate_issue_workflow(repo_root_clipboard, issue_number=79)
        (repo_root_clipboard / "wip.txt").write_text("wip\n", encoding="utf-8")

        clipboard_config = self._write_user_config(
            "review-clipboard.yaml",
            "ai:\n  delivery: clipboard\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(clipboard_config)}, clear=False),
            patch("ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text", return_value=None),
        ):
            clipboard_code, clipboard_out, clipboard_err = self._invoke(repo_root_clipboard, "review")

        self.assertEqual(clipboard_code, 0)
        self.assertEqual(clipboard_err, "")
        self.assertIn("Invocation copied to clipboard.", clipboard_out)
        self.assertNotIn("Read and execute .ai-dev/reviews/", clipboard_out)

        repo_root_clipboard_stdout = self._init_repo("repo-review-cli-clipboard-stdout")
        self._activate_issue_workflow(repo_root_clipboard_stdout, issue_number=80)
        (repo_root_clipboard_stdout / "wip.txt").write_text("wip\n", encoding="utf-8")

        clipboard_stdout_config = self._write_user_config(
            "review-clipboard-stdout.yaml",
            "ai:\n  delivery: clipboard+stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(clipboard_stdout_config)}, clear=False),
            patch(
                "ai_dev_flow.task_delivery.PlatformClipboardWriter.copy_text",
                side_effect=ClipboardDeliveryError("clipboard failed"),
            ),
        ):
            cs_code, cs_out, cs_err = self._invoke(repo_root_clipboard_stdout, "review")

        self.assertEqual(cs_code, 0)
        self.assertIn("Read and execute .ai-dev/tasks/", cs_out)
        self.assertEqual(cs_out.count("Read and execute .ai-dev/tasks/"), 1)
        self.assertIn("Warning: clipboard delivery failed; invocation was still written to stdout", cs_err)


if __name__ == "__main__":
    unittest.main()
