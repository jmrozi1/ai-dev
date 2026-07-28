from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_dev_flow import cli


class FlowOutputExcludeTests(unittest.TestCase):
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
        nested = repo_root / "nested"
        nested.mkdir(parents=True)

        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Flow Output Exclude Tests")
        self._run_git(repo_root, "config", "user.email", "flow-output-exclude-tests@example.com")

        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        (nested / ".keep").write_text("keep\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt", "nested/.keep")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")

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

    def _exclude_path(self, repo_root: Path) -> Path:
        path_text = self._run_git(repo_root, "rev-parse", "--git-path", "info/exclude")
        candidate = Path(path_text)
        if candidate.is_absolute():
            return candidate
        return repo_root / candidate

    def _exclude_text(self, repo_root: Path) -> str:
        path = self._exclude_path(repo_root)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _assert_ignored(self, repo_root: Path, path: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", "--", path],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

    def _assert_not_ignored(self, repo_root: Path, path: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", "--", path],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 1, msg=completed.stderr)

    def _status(self, repo_root: Path) -> str:
        return self._run_git(repo_root, "status", "--short", "--untracked-files=all")

    def _managed_block(self, repo_root: Path) -> list[str]:
        text = self._exclude_text(repo_root)
        lines = text.splitlines()
        begin = "# BEGIN ai-dev managed excludes"
        end = "# END ai-dev managed excludes"
        if begin not in lines:
            return []
        start_index = lines.index(begin) + 1
        end_index = lines.index(end)
        return lines[start_index:end_index]

    def _activate_issue_workflow(self, repo_root: Path, issue_number: int = 1) -> None:
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        workflow_dir = repo_root / ".ai-dev"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        (workflow_dir / "workflow.json").write_text(
            "{\n"
            f'  "activeIssueNumber": {issue_number},\n'
            '  "mainBranch": "main",\n'
            '  "scratchBranch": "scratch",\n'
            '  "checkpoint": 0\n'
            "}\n",
            encoding="utf-8",
        )

    def test_relative_output_inside_repository_is_locally_ignored(self) -> None:
        repo_root = self._init_repo("repo-relative")

        code, out, err = self._invoke(repo_root, "set", "out=out.txt")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "out: out.txt\n")
        self._assert_ignored(repo_root, "out.txt")
        self.assertIn(".ai-dev/", self._managed_block(repo_root))

    def test_nested_relative_output_is_ignored(self) -> None:
        repo_root = self._init_repo("repo-nested-relative")

        code, out, err = self._invoke(repo_root / "nested", "set", "out=reports/review.txt")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "out: reports/review.txt\n")
        self._assert_ignored(repo_root, "reports/review.txt")

    def test_absolute_output_inside_repository_is_ignored_using_relative_form(self) -> None:
        repo_root = self._init_repo("repo-absolute-inside")
        destination = repo_root / "reports" / "absolute.txt"

        code, out, err = self._invoke(repo_root, "set", f"out={destination}")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, f"out: {destination}\n")
        self._assert_ignored(repo_root, "reports/absolute.txt")
        self.assertNotIn(str(destination), self._exclude_text(repo_root))

    def test_output_outside_repository_does_not_add_output_exclusion(self) -> None:
        repo_root = self._init_repo("repo-outside")
        destination = self.tmp_path / "outside-review.txt"

        code, out, err = self._invoke(repo_root, "set", f"out={destination}")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, f"out: {destination}\n")
        self.assertNotIn("outside-review.txt", self._exclude_text(repo_root))
        self.assertEqual(self._managed_block(repo_root), [".ai-dev/"])

    def test_changing_output_removes_prior_managed_exclusion(self) -> None:
        repo_root = self._init_repo("repo-change-output")

        self._invoke(repo_root, "set", "out=old.txt")
        code, out, err = self._invoke(repo_root, "set", "out=new.txt")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "out: new.txt\n")
        managed_block = self._managed_block(repo_root)
        self.assertNotIn("old.txt", managed_block)
        self.assertIn("new.txt", managed_block)
        self._assert_not_ignored(repo_root, "old.txt")
        self._assert_ignored(repo_root, "new.txt")

    def test_unsetting_output_removes_managed_output_exclusion(self) -> None:
        repo_root = self._init_repo("repo-unset-output")

        self._invoke(repo_root, "set", "out=out.txt")
        code, out, err = self._invoke(repo_root, "unset", "out")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "out: not configured\n")
        self.assertEqual(self._managed_block(repo_root), [".ai-dev/"])
        self._assert_not_ignored(repo_root, "out.txt")

    def test_existing_user_exclusions_are_preserved(self) -> None:
        repo_root = self._init_repo("repo-preserve-user")
        exclude_path = self._exclude_path(repo_root)
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("# user\n*.tmp\nout.txt\n", encoding="utf-8")

        self._invoke(repo_root, "set", "out=managed.txt")
        self._invoke(repo_root, "unset", "out")

        exclude_text = self._exclude_text(repo_root)
        self.assertIn("# user\n", exclude_text)
        self.assertIn("*.tmp\n", exclude_text)
        self.assertIn("out.txt\n", exclude_text)
        self.assertIn(".ai-dev/", exclude_text)

    def test_repeated_set_out_is_idempotent(self) -> None:
        repo_root = self._init_repo("repo-idempotent")

        self._invoke(repo_root, "set", "out=repeat.txt")
        self._invoke(repo_root, "set", "out=repeat.txt")

        managed_block = self._managed_block(repo_root)
        self.assertEqual(managed_block.count("repeat.txt"), 1)
        self.assertEqual(managed_block.count(".ai-dev/"), 1)

    def test_spaces_and_special_characters_in_output_paths_are_ignored(self) -> None:
        repo_root = self._init_repo("repo-special-output")
        relative_path = "reports/space name [draft] #1!.txt"

        code, out, err = self._invoke(repo_root, "set", f"out={relative_path}")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, f"out: {relative_path}\n")
        self._assert_ignored(repo_root, relative_path)
        managed_block = self._managed_block(repo_root)
        self.assertTrue(any("\\ " in line and "\\[" in line and "\\#" in line and "\\!" in line for line in managed_block))

    def test_git_worktree_support_uses_worktree_local_exclude(self) -> None:
        repo_root = self._init_repo("repo-worktree")
        worktree_root = self.tmp_path / "linked-worktree"
        self._run_git(repo_root, "worktree", "add", "-b", "scratch-worktree", str(worktree_root), "HEAD")

        code, out, err = self._invoke(worktree_root, "set", "out=worktree-review.txt")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out, "out: worktree-review.txt\n")
        self._assert_ignored(worktree_root, "worktree-review.txt")
        self.assertIn("worktree-review.txt", self._exclude_text(worktree_root))

    def test_review_leaves_only_genuine_source_changes_visible_in_git_status(self) -> None:
        repo_root = self._init_repo("repo-review-clean-status")
        self._activate_issue_workflow(repo_root, issue_number=77)
        self._invoke(repo_root, "set", "out=out.txt")
        (repo_root / "tracked.txt").write_text("base\nsource change\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(str(repo_root / "out.txt"), out)
        self.assertEqual(self._status(repo_root).strip(), "M  tracked.txt")


if __name__ == "__main__":
    unittest.main()