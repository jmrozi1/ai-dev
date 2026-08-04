from __future__ import annotations

import io
import json
import os
import shutil
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

    def _write_user_config(self, name: str, text: str) -> Path:
        config_path = self.tmp_path / name
        config_path.write_text(text, encoding="utf-8")
        return config_path

    def _latest_review_dir(self, repo_root: Path) -> Path:
        review_root = repo_root / ".ai-dev" / "review"
        self.assertTrue(review_root.exists())
        self.assertTrue(review_root.is_dir())
        return review_root

    def _snapshot_tree_bytes(self, root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        snapshot: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
        return snapshot

    def _assert_no_review_temp_or_backup_dirs(self, repo_root: Path) -> None:
        ai_dev_root = repo_root / ".ai-dev"
        if not ai_dev_root.exists():
            return
        leftovers = [
            path.name
            for path in ai_dev_root.iterdir()
            if path.is_dir()
            and (path.name.startswith("review.tmp-") or path.name.startswith("review.bak-"))
        ]
        self.assertEqual(leftovers, [])

    def _review_backup_dirs(self, repo_root: Path) -> list[Path]:
        ai_dev_root = repo_root / ".ai-dev"
        if not ai_dev_root.exists():
            return []
        return sorted(
            path
            for path in ai_dev_root.iterdir()
            if path.is_dir() and path.name.startswith("review.bak-")
        )

    def test_review_stdout_default_scope_prepares_task_and_pointer(self) -> None:
        repo_root = self._init_repo("repo-review-cli-default")
        self._activate_issue_workflow(repo_root, issue_number=71)
        (repo_root / "wip.txt").write_text("wip\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(self.tmp_path / "missing-default.yaml")}, clear=False):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Read and execute .ai-dev/review/task.md", out)
        self.assertIn("Prepared review task for review-", out)
        self.assertIn("Review task: .ai-dev/review/task.md", out)
        self.assertIn("Review package: .ai-dev/review/package.md", out)
        self.assertIn("Changes: .ai-dev/review/changes.diff", out)
        self.assertIn("Expected report: .ai-dev/review/report.md", out)
        self.assertIn("diff --git a/wip.txt b/wip.txt", out)
        self.assertNotIn("review is complete", out.lower())

        latest = self._latest_review_dir(repo_root)
        payload = json.loads((latest / "package.json").read_text(encoding="utf-8"))
        task_id = build_review_task_id(payload["review_id"])
        task_path = repo_root / ".ai-dev" / "review" / "task.md"
        self.assertTrue(task_path.exists())

        pointer = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")
        self.assertIn(f"Task-ID: {task_id}", pointer)
        self.assertIn("Task-Type: review", pointer)
        self.assertIn("Task-File: .ai-dev/review/task.md", pointer)

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
        task_text = (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8")
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
        task_text = (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8")
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
        self.assertFalse((repo_root / ".ai-dev" / "review").exists())

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
        self.assertFalse((repo_root_invalid / ".ai-dev" / "review").exists())

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
        self.assertFalse((repo_root_adapter / ".ai-dev" / "review").exists())

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
        review_root = repo_root / ".ai-dev" / "review"
        self.assertFalse(review_root.exists())

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

    def test_second_review_replaces_workspace_and_clears_stale_reviewer_outputs(self) -> None:
        repo_root = self._init_repo("repo-review-cli-replace-clears-stale")
        self._activate_issue_workflow(repo_root, issue_number=81)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-replace-clears-stale.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        review_root = self._latest_review_dir(repo_root)
        first_payload = json.loads((review_root / "package.json").read_text(encoding="utf-8"))
        (review_root / "report.md").write_text("stale report\n", encoding="utf-8")
        (review_root / "verification.md").write_text("stale verification\n", encoding="utf-8")
        (review_root / "verification.json").write_text("{}\n", encoding="utf-8")

        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")
        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            second_code, _, second_err = self._invoke(repo_root, "review")
        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")

        second_payload = json.loads((review_root / "package.json").read_text(encoding="utf-8"))
        self.assertNotEqual(first_payload["review_id"], second_payload["review_id"])
        self.assertFalse((review_root / "report.md").exists())
        self.assertFalse((review_root / "verification.md").exists())
        self.assertFalse((review_root / "verification.json").exists())

    def test_successful_review_cleans_legacy_reviews_and_only_generated_legacy_tasks(self) -> None:
        repo_root = self._init_repo("repo-review-cli-legacy-cleanup")
        self._activate_issue_workflow(repo_root, issue_number=82)
        (repo_root / "wip.txt").write_text("legacy\n", encoding="utf-8")

        legacy_reviews_root = repo_root / ".ai-dev" / "reviews" / "review-legacy"
        legacy_reviews_root.mkdir(parents=True, exist_ok=True)
        (legacy_reviews_root / "package.json").write_text("{}\n", encoding="utf-8")

        tasks_root = repo_root / ".ai-dev" / "tasks"
        tasks_root.mkdir(parents=True, exist_ok=True)
        (tasks_root / "review-0123456789abcdef-task.md").write_text("legacy generated task\n", encoding="utf-8")
        unrelated_task = tasks_root / "my-custom-task.md"
        unrelated_task.write_text("user-authored\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-legacy-cleanup.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, _, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse((repo_root / ".ai-dev" / "reviews").exists())
        self.assertFalse((tasks_root / "review-0123456789abcdef-task.md").exists())
        self.assertTrue(unrelated_task.exists())

    def test_delivery_reads_published_new_task_not_old_task(self) -> None:
        repo_root = self._init_repo("repo-review-cli-delivery-sees-new-task")
        self._activate_issue_workflow(repo_root, issue_number=83)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-delivery-sees-new-task.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        old_task_text = (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8")

        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")
        observed: dict[str, str] = {}

        class _ReaderAdapter:
            def deliver(self, invocation: str) -> None:
                observed["task"] = (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_ReaderAdapter()),
        ):
            second_code, _, second_err = self._invoke(repo_root, "review")

        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        self.assertIn("task", observed)
        self.assertNotEqual(observed["task"], old_task_text)
        self.assertEqual(observed["task"], (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8"))
        self._assert_no_review_temp_or_backup_dirs(repo_root)

    def test_delivery_failure_after_publication_restores_previous_workspace_and_pointer(self) -> None:
        repo_root = self._init_repo("repo-review-cli-delivery-failure-restores")
        self._activate_issue_workflow(repo_root, issue_number=84)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-delivery-failure-restores.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        review_root = repo_root / ".ai-dev" / "review"
        previous_workspace = self._snapshot_tree_bytes(review_root)
        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        previous_pointer = pointer_path.read_text(encoding="utf-8")

        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")

        class _FailingAdapter:
            def deliver(self, invocation: str) -> None:
                raise ClipboardDeliveryError("delivery failed after publication")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_FailingAdapter()),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Review task preparation failed", err)
        self.assertEqual(self._snapshot_tree_bytes(review_root), previous_workspace)
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), previous_pointer)
        self._assert_no_review_temp_or_backup_dirs(repo_root)

    def test_rollback_delete_canonical_failure_retains_backup_and_reports_path(self) -> None:
        repo_root = self._init_repo("repo-review-cli-rollback-delete-failure")
        self._activate_issue_workflow(repo_root, issue_number=86)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-rollback-delete-failure.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        previous_workspace = self._snapshot_tree_bytes(repo_root / ".ai-dev" / "review")
        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")
        original_rmtree = shutil.rmtree

        def _failing_remove(path: Path) -> None:
            if Path(path).name == "review":
                raise OSError("simulated canonical delete failure")
            original_rmtree(path)

        class _FailingAdapter:
            def deliver(self, invocation: str) -> None:
                raise ClipboardDeliveryError("delivery failed")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_FailingAdapter()),
            patch("ai_dev_flow.cli.shutil.rmtree", side_effect=_failing_remove),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        backup_dirs = self._review_backup_dirs(repo_root)
        self.assertEqual(len(backup_dirs), 1)
        self.assertEqual(self._snapshot_tree_bytes(backup_dirs[0]), previous_workspace)
        self.assertIn(str(backup_dirs[0]), err)

    def test_rollback_restore_failure_keeps_backup_recoverable(self) -> None:
        repo_root = self._init_repo("repo-review-cli-rollback-restore-failure")
        self._activate_issue_workflow(repo_root, issue_number=87)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-rollback-restore-failure.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        previous_workspace = self._snapshot_tree_bytes(repo_root / ".ai-dev" / "review")
        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")

        original_rename = Path.rename

        def _failing_restore_rename(path: Path, target: Path) -> Path:
            target_path = Path(target)
            if path.name.startswith("review.bak-") and target_path.name == "review":
                raise OSError("simulated restore rename failure")
            return original_rename(path, target)

        class _FailingAdapter:
            def deliver(self, invocation: str) -> None:
                raise ClipboardDeliveryError("delivery failed")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_FailingAdapter()),
            patch("pathlib.Path.rename", new=_failing_restore_rename),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        backup_dirs = self._review_backup_dirs(repo_root)
        self.assertEqual(len(backup_dirs), 1)
        self.assertEqual(self._snapshot_tree_bytes(backup_dirs[0]), previous_workspace)
        self.assertIn(str(backup_dirs[0]), err)

    def test_backup_delete_failure_after_delivery_keeps_new_review_and_pointer(self) -> None:
        repo_root = self._init_repo("repo-review-cli-backup-delete-post-commit")
        self._activate_issue_workflow(repo_root, issue_number=89)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-backup-delete-post-commit.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        first_payload = json.loads(
            (repo_root / ".ai-dev" / "review" / "package.json").read_text(encoding="utf-8")
        )
        previous_workspace = self._snapshot_tree_bytes(repo_root / ".ai-dev" / "review")

        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")
        original_rmtree = shutil.rmtree
        observed: dict[str, str] = {}

        def _fail_backup_delete(path: Path) -> None:
            path_obj = Path(path)
            if path_obj.name.startswith("review.bak-"):
                raise OSError("simulated backup delete failure")
            original_rmtree(path_obj)

        class _ReaderAdapter:
            def deliver(self, invocation: str) -> None:
                observed["invocation"] = invocation
                observed["task"] = (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_ReaderAdapter()),
            patch("ai_dev_flow.cli.shutil.rmtree", side_effect=_fail_backup_delete),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("post-commit cleanup failed", err)

        second_payload = json.loads(
            (repo_root / ".ai-dev" / "review" / "package.json").read_text(encoding="utf-8")
        )
        self.assertNotEqual(first_payload["review_id"], second_payload["review_id"])

        pointer = (repo_root / ".ai-dev" / "current-task.md").read_text(encoding="utf-8")
        self.assertIn(f"Task-ID: {build_review_task_id(second_payload['review_id'])}", pointer)
        self.assertIn("Task-File: .ai-dev/review/task.md", pointer)

        self.assertIn("task", observed)
        self.assertEqual(
            observed["task"],
            (repo_root / ".ai-dev" / "review" / "task.md").read_text(encoding="utf-8"),
        )

        backup_dirs = self._review_backup_dirs(repo_root)
        self.assertEqual(len(backup_dirs), 1)
        self.assertEqual(self._snapshot_tree_bytes(backup_dirs[0]), previous_workspace)
        self.assertIn(str(backup_dirs[0]), err)
        self.assertFalse((repo_root / ".ai-dev" / "review.tmp-stale").exists())

    def test_legacy_cleanup_failure_after_delivery_preserves_published_review(self) -> None:
        repo_root = self._init_repo("repo-review-cli-legacy-cleanup-failure")
        self._activate_issue_workflow(repo_root, issue_number=88)
        (repo_root / "wip.txt").write_text("first\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-legacy-cleanup-failure.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            first_code, _, first_err = self._invoke(repo_root, "review")
        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")

        first_payload = json.loads(
            (repo_root / ".ai-dev" / "review" / "package.json").read_text(encoding="utf-8")
        )
        (repo_root / "wip.txt").write_text("second\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli._cleanup_legacy_review_storage", side_effect=OSError("legacy cleanup failed")),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertIn("Read and execute .ai-dev/review/task.md", out)
        self.assertIn("Published review remains available", err)

        second_payload = json.loads(
            (repo_root / ".ai-dev" / "review" / "package.json").read_text(encoding="utf-8")
        )
        self.assertNotEqual(first_payload["review_id"], second_payload["review_id"])
        self.assertTrue((repo_root / ".ai-dev" / "review" / "task.md").exists())
        self.assertFalse(self._review_backup_dirs(repo_root))

    def test_delivery_failure_with_no_prior_review_leaves_no_rolling_review(self) -> None:
        repo_root = self._init_repo("repo-review-cli-delivery-failure-no-prior")
        self._activate_issue_workflow(repo_root, issue_number=85)
        (repo_root / "wip.txt").write_text("single\n", encoding="utf-8")

        pointer_path = repo_root / ".ai-dev" / "current-task.md"
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        pointer_path.write_text("old pointer\n", encoding="utf-8")

        user_config = self._write_user_config(
            "review-delivery-failure-no-prior.yaml",
            "ai:\n  delivery: stdout\n  invocation: \"Read and execute {task_file}\"\n",
        )

        class _FailingAdapter:
            def deliver(self, invocation: str) -> None:
                raise ClipboardDeliveryError("delivery failed")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False),
            patch("ai_dev_flow.cli.build_delivery_adapter", return_value=_FailingAdapter()),
        ):
            code, out, err = self._invoke(repo_root, "review")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Review task preparation failed", err)
        self.assertFalse((repo_root / ".ai-dev" / "review").exists())
        self.assertEqual(pointer_path.read_text(encoding="utf-8"), "old pointer\n")
        self._assert_no_review_temp_or_backup_dirs(repo_root)

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
        self.assertIn("Read and execute .ai-dev/review/task.md", cs_out)
        self.assertEqual(cs_out.count("Read and execute .ai-dev/review/task.md"), 1)
        self.assertIn("Warning: clipboard delivery failed; invocation was still written to stdout", cs_err)


if __name__ == "__main__":
    unittest.main()
