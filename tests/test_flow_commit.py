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


class FlowCommitTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Flow Commit Tests")
        self._run_git(repo_root, "config", "user.email", "flow-commit-tests@example.com")

        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        state_path = repo_root / ".ai-dev" / "workflow.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": 8,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

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

    def test_commit_includes_untracked_and_staged_changes(self) -> None:
        repo_root = self._init_repo("repo-commit-staging")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")
        (repo_root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Created checkpoint 1\n", out)

        tracked_files = self._run_git(repo_root, "show", "--name-only", "--pretty=format:", "HEAD")
        self.assertIn("staged.txt", tracked_files)
        self.assertIn("untracked.txt", tracked_files)

    def test_successful_commit_removes_rolling_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-commit-cleans-review")
        review_root = repo_root / ".ai-dev" / "review"
        review_root.mkdir(parents=True, exist_ok=True)
        (review_root / "package.json").write_text('{"review_id":"review-0123456789abcdef"}\n', encoding="utf-8")
        (review_root / "task.md").write_text("task\n", encoding="utf-8")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")

        code, _, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(review_root.exists())

    def test_failed_commit_preserves_rolling_review_workspace(self) -> None:
        repo_root = self._init_repo("repo-commit-preserves-review-on-failure")
        review_root = repo_root / ".ai-dev" / "review"
        review_root.mkdir(parents=True, exist_ok=True)
        marker = review_root / "package.json"
        marker.write_text('{"review_id":"review-0123456789abcdef"}\n', encoding="utf-8")

        code, out, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no staged changes", err)
        self.assertTrue(review_root.exists())
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
