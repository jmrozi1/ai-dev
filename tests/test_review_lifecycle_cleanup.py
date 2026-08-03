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

from ai_dev_flow import cli


class ReviewLifecycleCleanupTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Review Lifecycle Cleanup Tests")
        self._run_git(repo_root, "config", "user.email", "review-lifecycle-cleanup-tests@example.com")
        (repo_root / ".gitignore").write_text(
            ".ai-dev/workflow.json\n"
            ".ai-dev/review/\n"
            ".ai-dev/reviews/\n"
            ".ai-dev/tasks/review-*-task.md\n",
            encoding="utf-8",
        )
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        return repo_root

    def _write_state(self, repo_root: Path, payload: dict[str, object]) -> None:
        state_path = repo_root / ".ai-dev" / "workflow.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

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

    def _create_review_workspace(self, repo_root: Path) -> Path:
        review_root = repo_root / ".ai-dev" / "review"
        review_root.mkdir(parents=True, exist_ok=True)
        (review_root / "package.json").write_text('{"review_id":"review-0123456789abcdef"}\n', encoding="utf-8")
        return review_root

    def test_successful_reset_removes_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-reset-cleanup")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_state(
            repo_root,
            {
                "activeIssueNumber": 401,
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 2,
            },
        )
        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        review_root = self._create_review_workspace(repo_root)

        code, _, err = self._invoke(repo_root, "reset")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(review_root.exists())

    def test_failed_reset_preserves_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-reset-preserve-on-failure")
        self._run_git(repo_root, "branch", "scratch")
        self._write_state(
            repo_root,
            {
                "activeIssueNumber": 402,
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 2,
            },
        )
        review_root = self._create_review_workspace(repo_root)

        code, out, err = self._invoke(repo_root, "reset")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("current branch main does not match scratchBranch scratch", err)
        self.assertTrue(review_root.exists())

    def test_successful_promote_removes_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-promote-cleanup")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_state(
            repo_root,
            {
                "patchDescription": "cleanup",
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 1,
            },
        )
        (repo_root / "tracked.txt").write_text("promote\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "scratch commit")
        review_root = self._create_review_workspace(repo_root)

        code, _, err = self._invoke(repo_root, "promote", "promote cleanup")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(review_root.exists())

    def test_failed_promote_preserves_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-promote-preserve-on-failure")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_state(
            repo_root,
            {
                "patchDescription": "cleanup",
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 1,
            },
        )
        (repo_root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        review_root = self._create_review_workspace(repo_root)

        code, out, err = self._invoke(repo_root, "promote", "promote cleanup")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("repository must be clean", err)
        self.assertTrue(review_root.exists())

    def test_successful_complete_removes_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-complete-cleanup")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_state(
            repo_root,
            {
                "patchDescription": "cleanup",
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 0,
            },
        )
        review_root = self._create_review_workspace(repo_root)

        code, _, err = self._invoke(repo_root, "complete")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(review_root.exists())

    def test_failed_complete_preserves_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-complete-preserve-on-failure")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_state(
            repo_root,
            {
                "patchDescription": "cleanup",
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "checkpoint": 1,
            },
        )
        review_root = self._create_review_workspace(repo_root)

        code, out, err = self._invoke(repo_root, "complete")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("checkpoint must be 0", err)
        self.assertTrue(review_root.exists())


if __name__ == "__main__":
    unittest.main()
