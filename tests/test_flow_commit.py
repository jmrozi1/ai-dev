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
from ai_dev_flow import repository


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

    def _diff_baseline_path(self, repo_root: Path) -> Path:
        return repo_root / ".ai-dev" / "diff-baseline" / "baseline.json"

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

    def test_successful_commit_preserves_legacy_review_workspace(self) -> None:
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
        self.assertTrue(review_root.exists())
        self.assertTrue((review_root / "package.json").exists())

    def test_failed_commit_preserves_legacy_review_workspace(self) -> None:
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

    def test_successful_commit_clears_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-commit-clears-baseline")
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text('{"version":1,"repository":{"root":"x"},"workflow":{},"status":{},"snapshots":{"working":{}}}\n', encoding="utf-8")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")

        code, out, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Created checkpoint 1\n", out)
        self.assertFalse(baseline_path.exists())

    def test_failed_commit_preserves_review_baseline(self) -> None:
        repo_root = self._init_repo("repo-commit-preserves-baseline-on-failure")
        baseline_path = self._diff_baseline_path(repo_root)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text('{"version":1,"repository":{"root":"x"},"workflow":{},"status":{},"snapshots":{"working":{}}}\n', encoding="utf-8")

        code, out, err = self._invoke(repo_root, "commit")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no staged changes", err)
        self.assertTrue(baseline_path.exists())

    def test_successful_commit_reports_warning_when_baseline_cleanup_fails(self) -> None:
        repo_root = self._init_repo("repo-commit-cleanup-warning")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")

        with patch(
            "ai_dev_flow.cli.clear_diff_baseline_for_repo_root",
            side_effect=cli.RepositoryError("cleanup denied"),
        ):
            code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertIn("Created checkpoint 1\n", out)
        self.assertIn("Warning: review-baseline cleanup failed", err)
        self.assertIn("flow-diff --refresh", err)

        state_data = json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(state_data.get("checkpoint"), 1)

    def _boundary_paths(self, out: str) -> list[str]:
        """The changed-path block exactly as the command reported it."""
        lines = out.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("changed-paths: "):
                collected = []
                for candidate in lines[index + 1 :]:
                    if not candidate.startswith("  "):
                        break
                    collected.append(candidate[2:])
                return collected
        raise AssertionError(f"no changed-paths block in output: {out!r}")

    def _committed_paths(self, repo_root: Path, revision: str) -> list[str]:
        """Changed paths read independently from the commit object itself."""
        raw = self._run_git(
            repo_root,
            "diff-tree",
            "--no-commit-id",
            "-r",
            "--no-renames",
            "--name-only",
            "-z",
            revision,
        )
        return sorted(record for record in raw.split("\0") if record)

    def test_commit_reports_boundary_read_from_the_created_commit(self) -> None:
        repo_root = self._init_repo("repo-boundary-single-parent")

        (repo_root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        parent_before = self._run_git(repo_root, "rev-parse", "HEAD")

        code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        head = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertIn("Created checkpoint 1\n", out)
        self.assertIn(f"commit: {head}\n", out)
        self.assertIn(f"parent: {parent_before}\n", out)
        self.assertIn("changed-paths: 2\n", out)

        self.assertEqual(self._boundary_paths(out), ["alpha.txt", "tracked.txt"])
        self.assertEqual(
            self._boundary_paths(out), self._committed_paths(repo_root, head)
        )
        self.assertNotIn(".ai-dev", "\n".join(self._boundary_paths(out)))

    def test_commit_boundary_lists_every_changed_path(self) -> None:
        repo_root = self._init_repo("repo-boundary-multi-path")

        (repo_root / "doomed.txt").write_text("doomed\n", encoding="utf-8")
        self._run_git(repo_root, "add", "doomed.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "seed deletable file")

        (repo_root / "added.txt").write_text("added\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (repo_root / "doomed.txt").unlink()
        nested = repo_root / "nested" / "deep"
        nested.mkdir(parents=True)
        (nested / "child.txt").write_text("child\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("changed-paths: 4\n", out)
        self.assertEqual(
            self._boundary_paths(out),
            ["added.txt", "doomed.txt", "nested/deep/child.txt", "tracked.txt"],
        )

    def test_commit_boundary_keeps_both_sides_of_a_rename(self) -> None:
        repo_root = self._init_repo("repo-boundary-rename")

        payload = "".join(f"distinct payload line {index}\n" for index in range(60))
        (repo_root / "old name.txt").write_text(payload, encoding="utf-8")
        self._run_git(repo_root, "add", "old name.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "seed rename source")

        os.replace(repo_root / "old name.txt", repo_root / "new name.txt")

        code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")

        head = self._run_git(repo_root, "rev-parse", "HEAD")

        # The rename really is detectable, so a collapsing derivation would hide
        # the old path. Without this guard the assertion below could pass by
        # accident on a repository where git saw two unrelated files.
        collapsed = self._run_git(
            repo_root, "show", "--name-only", "--pretty=format:", head
        ).split("\n")
        self.assertEqual([line for line in collapsed if line], ["new name.txt"])

        self.assertIn("changed-paths: 2\n", out)
        self.assertEqual(self._boundary_paths(out), ["new name.txt", "old name.txt"])

    def test_commit_boundary_reports_a_non_ascii_path_unquoted(self) -> None:
        repo_root = self._init_repo("repo-boundary-non-ascii")

        (repo_root / "caf\u00e9-\u65e5\u672c.txt").write_text("x\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("\n  caf\u00e9-\u65e5\u672c.txt\n", out)
        self.assertEqual(self._boundary_paths(out), ["caf\u00e9-\u65e5\u672c.txt"])

    def test_commit_boundary_includes_a_file_created_after_pre_check_evidence(self) -> None:
        repo_root = self._init_repo("repo-boundary-late-file")

        (repo_root / "early.txt").write_text("early\n", encoding="utf-8")

        pre_check = self._run_git(
            repo_root, "status", "--short", "--untracked-files=all"
        )
        self.assertNotIn("late-generated.txt", pre_check)

        def late_then_stage(root: Path) -> None:
            (Path(root) / "late-generated.txt").write_text("late\n", encoding="utf-8")
            repository.stage_all_changes(root)

        with patch("ai_dev_flow.cli.stage_all_changes", side_effect=late_then_stage):
            code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("changed-paths: 2\n", out)
        self.assertEqual(
            self._boundary_paths(out), ["early.txt", "late-generated.txt"]
        )

        head = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertIn("late-generated.txt", self._committed_paths(repo_root, head))

    def test_commit_boundary_includes_a_file_generated_during_the_commit(self) -> None:
        """The strongest form: nothing observable before the commit can see it.

        A pre-commit hook creates and stages the file after the runtime has
        already staged and already captured its own working-tree evidence, so
        only the created commit object records it.
        """
        repo_root = self._init_repo("repo-boundary-commit-time-file")

        hooks = repo_root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'generated at commit time\\n' > late-generated.txt\n"
            "git add late-generated.txt\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(hook, 0o755)

        (repo_root / "added.txt").write_text("added\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")

        pre_check = self._run_git(
            repo_root, "status", "--short", "--untracked-files=all"
        )
        self.assertNotIn("late-generated.txt", pre_check)

        code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("changed-paths: 3\n", out)
        self.assertEqual(
            self._boundary_paths(out),
            ["added.txt", "late-generated.txt", "tracked.txt"],
        )

        head = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertEqual(
            self._boundary_paths(out), self._committed_paths(repo_root, head)
        )

    def test_unreadable_commit_boundary_fails_closed_after_the_checkpoint_exists(
        self,
    ) -> None:
        repo_root = self._init_repo("repo-boundary-unreadable")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")
        parent_before = self._run_git(repo_root, "rev-parse", "HEAD")

        with patch(
            "ai_dev_flow.cli.read_commit_boundary",
            side_effect=cli.RepositoryError("bad object HEAD"),
        ):
            code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")

        head = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertNotEqual(head, parent_before)
        self.assertIn("Checkpoint 1 commit was created", err)
        self.assertIn(head, err)
        self.assertIn("No changed-path boundary is claimed", err)
        self.assertIn("History was not changed", err)
        self.assertIn("flow-status", err)
        self.assertNotIn("changed-paths:", err)
        self.assertNotIn("Traceback", err)

        self.assertEqual(self._run_git(repo_root, "rev-parse", "HEAD^"), parent_before)
        self.assertEqual(
            self._run_git(repo_root, "log", "-1", "--format=%B").strip(), "1"
        )

        state_data = json.loads(
            (repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state_data.get("checkpoint"), 1)

    def test_boundary_output_is_stdout_only_alongside_a_stderr_warning(self) -> None:
        repo_root = self._init_repo("repo-boundary-streams")

        (repo_root / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(repo_root, "add", "staged.txt")

        with patch(
            "ai_dev_flow.cli.clear_diff_baseline_for_repo_root",
            side_effect=cli.RepositoryError("cleanup denied"),
        ):
            code, out, err = self._invoke(repo_root, "commit")

        self.assertEqual(code, 0)
        self.assertIn("parent: ", out)
        self.assertIn("changed-paths: 1\n", out)
        self.assertEqual(self._boundary_paths(out), ["staged.txt"])
        self.assertIn("Warning: review-baseline cleanup failed", err)
        self.assertNotIn("changed-paths", err)
        self.assertNotIn("parent: ", err)

    def test_boundary_read_rejects_a_zero_parent_commit(self) -> None:
        repo_root = self._init_repo("repo-boundary-root-commit")

        root_commit = self._run_git(repo_root, "rev-list", "--max-parents=0", "HEAD")

        with self.assertRaises(cli.RepositoryError) as caught:
            repository.read_commit_boundary(repo_root, commit=root_commit)

        self.assertIn("0 parents", str(caught.exception))

    def test_boundary_read_rejects_a_multi_parent_commit(self) -> None:
        repo_root = self._init_repo("repo-boundary-merge-commit")

        self._run_git(repo_root, "checkout", "-q", "-b", "side", "main")
        (repo_root / "side.txt").write_text("side\n", encoding="utf-8")
        self._run_git(repo_root, "add", "side.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "side commit")
        self._run_git(repo_root, "checkout", "-q", "scratch")
        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self._run_git(repo_root, "commit", "-q", "-am", "scratch commit")
        self._run_git(repo_root, "merge", "-q", "--no-ff", "--no-edit", "side")

        merge_commit = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertEqual(
            len(self._run_git(repo_root, "rev-list", "--parents", "-n", "1", merge_commit).split()),
            3,
        )

        with self.assertRaises(cli.RepositoryError) as caught:
            repository.read_commit_boundary(repo_root, commit=merge_commit)

        self.assertIn("2 parents", str(caught.exception))

    def test_boundary_read_rejects_an_object_that_is_not_a_commit(self) -> None:
        repo_root = self._init_repo("repo-boundary-non-commit")

        tree_hash = self._run_git(repo_root, "rev-parse", "HEAD^{tree}")

        with self.assertRaises(cli.RepositoryError) as caught:
            repository.read_commit_boundary(repo_root, commit=tree_hash)

        self.assertIn("does not resolve to a commit object", str(caught.exception))

    def test_boundary_read_rejects_a_commit_that_changed_nothing(self) -> None:
        repo_root = self._init_repo("repo-boundary-empty-commit")

        self._run_git(repo_root, "commit", "-q", "--allow-empty", "-m", "empty")
        empty_commit = self._run_git(repo_root, "rev-parse", "HEAD")

        with self.assertRaises(cli.RepositoryError) as caught:
            repository.read_commit_boundary(repo_root, commit=empty_commit)

        self.assertIn("no changed paths", str(caught.exception))

    def test_boundary_read_rejects_a_path_containing_a_line_break(self) -> None:
        repo_root = self._init_repo("repo-boundary-newline-path")

        (repo_root / "weird\nname.txt").write_text("weird\n", encoding="utf-8")
        self._run_git(repo_root, "add", "--all", ".")
        self._run_git(repo_root, "commit", "-q", "-m", "newline path")
        commit = self._run_git(repo_root, "rev-parse", "HEAD")

        with self.assertRaises(cli.RepositoryError) as caught:
            repository.read_commit_boundary(repo_root, commit=commit)

        self.assertIn("line break", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
