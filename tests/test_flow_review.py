from __future__ import annotations

from collections import Counter
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


class FlowReviewTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Flow Review Tests")
        self._run_git(repo_root, "config", "user.email", "flow-review-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
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

    def _diff_headers(self, text: str) -> list[str]:
        headers: list[str] = []
        for line in text.splitlines():
            if not line.startswith("diff --git a/"):
                continue
            if " b/" not in line:
                continue
            left = line[len("diff --git a/") : line.index(" b/")]
            headers.append(left)
        return headers

    def test_review_help_includes_all_option(self) -> None:
        repo_root = self._init_repo("repo-review-help")

        short_code, short_out, short_err = self._invoke(repo_root, "review", "-h")
        self.assertEqual(short_code, 0)
        self.assertEqual(short_err, "")
        self.assertIn("Usage: flow review [-a|--all]", short_out)
        self.assertIn("all changes in the active workflow since main", short_out)

        long_code, long_out, long_err = self._invoke(repo_root, "review", "--help")
        self.assertEqual(long_code, 0)
        self.assertEqual(long_err, "")
        self.assertEqual(long_out, short_out)

    def test_review_argument_validation(self) -> None:
        repo_root = self._init_repo("repo-review-arg-validation")
        self._activate_issue_workflow(repo_root, issue_number=21)
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")

        accepted_default = self._invoke(repo_root, "review")
        self.assertEqual(accepted_default[0], 0)

        accepted_short = self._invoke(repo_root, "review", "-a")
        self.assertEqual(accepted_short[0], 0)

        accepted_long = self._invoke(repo_root, "review", "--all")
        self.assertEqual(accepted_long[0], 0)

        reject_unknown = self._invoke(repo_root, "review", "--bogus")
        self.assertEqual(reject_unknown[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_unknown[2])

        reject_multiple = self._invoke(repo_root, "review", "-a", "--all")
        self.assertEqual(reject_multiple[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_multiple[2])

        reject_positional = self._invoke(repo_root, "review", "extra")
        self.assertEqual(reject_positional[0], 1)
        self.assertIn("Usage: flow review [-a|--all]", reject_positional[2])

    def test_default_review_behavior_after_checkpoint(self) -> None:
        repo_root = self._init_repo("repo-review-default-scope")
        self._activate_issue_workflow(repo_root, issue_number=22)

        (repo_root / "checkpoint-change.txt").write_text("checkpoint\n", encoding="utf-8")
        commit_code, commit_out, commit_err = self._invoke(repo_root, "commit")
        self.assertEqual(commit_code, 0)
        self.assertEqual(commit_err, "")
        self.assertIn("Created checkpoint 1", commit_out)

        no_change_code, no_change_out, no_change_err = self._invoke(repo_root, "review")
        self.assertEqual(no_change_code, 1)
        self.assertEqual(no_change_out, "")
        self.assertIn("No proposed changes to review", no_change_err)

        (repo_root / "new-edit.txt").write_text("new edit\n", encoding="utf-8")
        changed_code, changed_out, changed_err = self._invoke(repo_root, "review")
        self.assertEqual(changed_code, 0)
        self.assertEqual(changed_err, "")
        self.assertIn("diff --git a/new-edit.txt b/new-edit.txt", changed_out)
        self.assertNotIn("diff --git a/checkpoint-change.txt b/checkpoint-change.txt", changed_out)

    def test_review_all_includes_workflow_commits_and_uncommitted(self) -> None:
        repo_root = self._init_repo("repo-review-all-scope")
        self._activate_issue_workflow(repo_root, issue_number=23)

        (repo_root / "checkpoint-1.txt").write_text("checkpoint 1\n", encoding="utf-8")
        first_commit = self._invoke(repo_root, "commit")
        self.assertEqual(first_commit[0], 0)

        (repo_root / "checkpoint-2.txt").write_text("checkpoint 2\n", encoding="utf-8")
        second_commit = self._invoke(repo_root, "commit")
        self.assertEqual(second_commit[0], 0)

        (repo_root / "uncommitted.txt").write_text("working tree\n", encoding="utf-8")

        workflow_state_before = (repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8")
        head_before = self._run_git(repo_root, "rev-parse", "HEAD")

        long_code, long_out, long_err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(long_code, 0)
        self.assertEqual(long_err, "")
        self.assertIn("Issue: 23", long_out)
        self.assertIn("Review summary:", long_out)
        self.assertIn("diff --git a/checkpoint-1.txt b/checkpoint-1.txt", long_out)
        self.assertIn("diff --git a/checkpoint-2.txt b/checkpoint-2.txt", long_out)
        self.assertIn("diff --git a/uncommitted.txt b/uncommitted.txt", long_out)
        self.assertNotIn(".ai-dev/workflow.json", long_out)

        short_code, short_out, short_err = self._invoke(repo_root, "review", "-a")
        self.assertEqual(short_code, 0)
        self.assertEqual(short_err, "")
        self.assertEqual(short_out, long_out)

        workflow_state_after = (repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8")
        head_after = self._run_git(repo_root, "rev-parse", "HEAD")
        self.assertEqual(workflow_state_after, workflow_state_before)
        self.assertEqual(head_after, head_before)

    def test_review_all_clean_checkpoint_emits_each_path_once(self) -> None:
        repo_root = self._init_repo("repo-review-all-clean-once")
        self._activate_issue_workflow(repo_root, issue_number=24)

        (repo_root / "checkpoint-a.txt").write_text("a\n", encoding="utf-8")
        first_commit = self._invoke(repo_root, "commit")
        self.assertEqual(first_commit[0], 0)

        (repo_root / "checkpoint-b.txt").write_text("b\n", encoding="utf-8")
        second_commit = self._invoke(repo_root, "commit")
        self.assertEqual(second_commit[0], 0)

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.count("Issue: "), 1)
        self.assertEqual(out.count("Review summary:"), 1)
        self.assertEqual(out.count("Diff legend:"), 1)

        expected_paths = [
            line
            for line in self._run_git(repo_root, "diff", "--name-only", "main...scratch").splitlines()
            if line
        ]
        headers = self._diff_headers(out)
        self.assertEqual(len(headers), len(expected_paths))

        expected_counter = Counter(expected_paths)
        header_counter = Counter(headers)
        self.assertEqual(set(header_counter.keys()), set(expected_counter.keys()))
        for path in expected_counter:
            self.assertEqual(header_counter[path], 1, msg=f"expected one diff header for {path}")

    def test_review_all_staged_overlay_emits_committed_and_overlay_once(self) -> None:
        repo_root = self._init_repo("repo-review-all-overlay-once")
        self._activate_issue_workflow(repo_root, issue_number=25)

        (repo_root / "checkpoint-1.txt").write_text("checkpoint one\n", encoding="utf-8")
        commit_one = self._invoke(repo_root, "commit")
        self.assertEqual(commit_one[0], 0)

        (repo_root / "checkpoint-2.txt").write_text("checkpoint two\n", encoding="utf-8")
        commit_two = self._invoke(repo_root, "commit")
        self.assertEqual(commit_two[0], 0)

        uncommitted_path = "overlay-wip.txt"
        (repo_root / uncommitted_path).write_text("overlay\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.count("Issue: "), 1)
        self.assertEqual(out.count("Review summary:"), 1)
        self.assertEqual(out.count("Diff legend:"), 1)

        committed_paths = {
            line
            for line in self._run_git(repo_root, "diff", "--name-only", "main...scratch").splitlines()
            if line
        }
        headers = self._diff_headers(out)
        counts = Counter(headers)

        self.assertIn(uncommitted_path, counts)
        self.assertEqual(counts[uncommitted_path], 1)
        for path in committed_paths:
            self.assertEqual(counts[path], 1, msg=f"expected one committed diff header for {path}")

        expected_total = len(committed_paths)
        if uncommitted_path not in committed_paths:
            expected_total += 1
        self.assertEqual(len(headers), expected_total)

    def test_review_all_fails_without_active_workflow_and_preserves_output(self) -> None:
        repo_root = self._init_repo("repo-review-all-inactive")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")

        output_path = self.tmp_path / "inactive-review-output.txt"
        output_path.write_text("existing report\n", encoding="utf-8")

        config_path = repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"out": str(output_path)}, indent=2) + "\n",
            encoding="utf-8",
        )
        self._run_git(repo_root, "add", "-f", ".ai-dev/config.json")
        self._run_git(repo_root, "commit", "-q", "-m", "track config")

        (repo_root / "tracked.txt").write_text("changed\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review", "--all")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot review workflow: no active issue is set.", err)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "existing report\n")


if __name__ == "__main__":
    unittest.main()
