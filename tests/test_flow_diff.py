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
from ai_dev_flow.bootstrap import run_bootstrap


class FlowDiffTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Flow Diff Tests")
        self._run_git(repo_root, "config", "user.email", "flow-diff-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": 23,
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
        if not arguments:
            raise ValueError("command is required")
        command = arguments[0]
        command_arguments = list(arguments[1:])

        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = f"flow-{command}"
        sys.argv = [
            f"flow-{command}",
            cli._DIRECT_FLOW_ROUTE_TOKEN,
            command,
            *command_arguments,
        ]
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

    def _index_tree_hash(self, repo_root: Path) -> str:
        return self._run_git(repo_root, "write-tree")

    def _workflow_json_bytes(self, repo_root: Path) -> bytes:
        return (repo_root / ".ai-dev" / "workflow.json").read_bytes()

    def _checkpoint(self, repo_root: Path) -> int:
        data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        return int(data["checkpoint"])

    def test_diff_help_lists_options(self) -> None:
        repo_root = self._init_repo("repo-diff-help")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Usage: flow-diff [--all] [--stdout]", stdout)
        self.assertIn("--all", stdout)
        self.assertIn("--stdout", stdout)

    def test_diff_default_includes_staged_unstaged_and_untracked(self) -> None:
        repo_root = self._init_repo("repo-diff-default")

        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nchanged\nagain\n", encoding="utf-8")
        (repo_root / "new.txt").write_text("new file\n", encoding="utf-8")

        before_status = self._run_git(repo_root, "status", "--short")
        before_branch = self._run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

        code, stdout, stderr = self._invoke(repo_root, "diff")

        after_status = self._run_git(repo_root, "status", "--short")
        after_branch = self._run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/new.txt b/new.txt", stdout)
        self.assertEqual(before_status, after_status)
        self.assertEqual(before_branch, after_branch)

    def test_diff_is_read_only_for_index_and_workflow_and_artifacts(self) -> None:
        repo_root = self._init_repo("repo-diff-read-only")
        (repo_root / "tracked.txt").write_text("base\nchanged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nchanged\nunstaged\n", encoding="utf-8")
        (repo_root / "untracked.txt").write_text("u\n", encoding="utf-8")

        index_before = self._index_tree_hash(repo_root)
        workflow_before = self._workflow_json_bytes(repo_root)
        checkpoint_before = self._checkpoint(repo_root)

        code, stdout, stderr = self._invoke(repo_root, "diff")

        index_after = self._index_tree_hash(repo_root)
        workflow_after = self._workflow_json_bytes(repo_root)
        checkpoint_after = self._checkpoint(repo_root)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout)
        self.assertEqual(index_before, index_after)
        self.assertEqual(workflow_before, workflow_after)
        self.assertEqual(checkpoint_before, checkpoint_after)

        self.assertFalse((repo_root / ".ai-dev" / "review").exists())
        self.assertFalse((repo_root / ".ai-dev" / "tasks").exists())
        self.assertFalse((repo_root / ".ai-dev" / "summarize").exists())
        self.assertFalse((repo_root / ".ai-dev" / "current-task.md").exists())
        self.assertFalse((repo_root / ".ai-dev" / "review-manifest.json").exists())

    def test_diff_shows_staged_and_unstaged_for_same_file(self) -> None:
        repo_root = self._init_repo("repo-diff-staged-unstaged")
        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertGreaterEqual(stdout.count("diff --git a/tracked.txt b/tracked.txt"), 2)

    def test_diff_all_includes_committed_workflow_changes(self) -> None:
        repo_root = self._init_repo("repo-diff-all")

        (repo_root / "committed.txt").write_text("committed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "committed.txt")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/committed.txt b/committed.txt", stdout)

    def test_diff_all_includes_committed_staged_unstaged_and_untracked(self) -> None:
        repo_root = self._init_repo("repo-diff-all-combined")

        (repo_root / "committed.txt").write_text("committed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "committed.txt")
        commit_code, _, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        (repo_root / "tracked.txt").write_text("base\nstaged\nunstaged\n", encoding="utf-8")
        (repo_root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/committed.txt b/committed.txt", stdout)
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", stdout)
        self.assertIn("diff --git a/untracked.txt b/untracked.txt", stdout)

    def test_diff_untracked_names_with_spaces_and_leading_hyphens(self) -> None:
        repo_root = self._init_repo("repo-diff-special-names")
        (repo_root / "name with spaces.txt").write_text("space\n", encoding="utf-8")
        (repo_root / "--leading-hyphen.txt").write_text("dash\n", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("name with spaces.txt", stdout)
        self.assertIn("--leading-hyphen.txt", stdout)

    def test_diff_untracked_empty_and_binary_files(self) -> None:
        repo_root = self._init_repo("repo-diff-empty-binary")
        (repo_root / "empty.txt").write_text("", encoding="utf-8")
        (repo_root / "binary.bin").write_bytes(b"\x00\xff\x00\xff")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("diff --git a/empty.txt b/empty.txt", stdout)
        self.assertIn("new file mode 100644", stdout)
        self.assertIn("diff --git a/binary.bin b/binary.bin", stdout)
        self.assertTrue(
            "GIT binary patch" in stdout
            or "Binary files /dev/null and b/binary.bin differ" in stdout
        )

    def test_diff_empty_scope_notice_on_stderr_only(self) -> None:
        repo_root = self._init_repo("repo-diff-empty-scope")

        code, stdout, stderr = self._invoke(repo_root, "diff")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "No diff content for current scope.\n")

    def test_installed_flow_diff_help_uses_executable_name(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-help")
        home = self.tmp_path / "home-launcher-help"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: flow-diff [--all] [--stdout]", completed.stdout)
        self.assertNotIn("Usage: flow-diff diff", completed.stdout)

    def test_installed_flow_diff_outputs_raw_diff_on_stdout(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-stdout")
        home = self.tmp_path / "home-launcher-stdout"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        (repo_root / "tracked.txt").write_text("base\nstaged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher), "--all", "--stdout"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("diff --git a/tracked.txt b/tracked.txt", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_installed_flow_diff_empty_scope_notice_on_stderr(self) -> None:
        repo_root = self._init_repo("repo-diff-launcher-empty")
        home = self.tmp_path / "home-launcher-empty"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=Path(__file__).resolve().parents[1],
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
        )

        launcher = install_dir / "flow-diff"
        completed = subprocess.run(
            [str(launcher)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "No diff content for current scope.\n")


if __name__ == "__main__":
    unittest.main()
