from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli


class FlowAbandonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Abandon Tests")
        self._git("config", "user.email", "abandon-tests@example.com")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        self._git("add", "tracked.txt", ".gitignore")
        self._git("commit", "-q", "-m", "base")
        self._git("branch", "-M", "main")
        self._git("checkout", "-q", "-b", "scratch")
        self.state_path = self.repo / ".ai-dev" / "workflow.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _branch_exists(self, branch: str) -> bool:
        return subprocess.run(
            ["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode == 0

    def _write_state(self, *, provider: str = "github", active: bool = True, checkpoint: int = 0) -> None:
        state = {
            "mainBranch": "main",
            "scratchBranch": "scratch",
            "checkpoint": checkpoint,
        }
        if active:
            state.update(
                {
                    "activeIssueNumber": 45,
                    "activeIssueTitle": "Abandon test",
                    "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/45",
                    "ticket": {
                        "provider": provider,
                        "ticketId": "45",
                        **({"repository": "jmrozi1/ai-dev"} if provider == "github" else {"path": ".ai-dev/tickets"}),
                    },
                }
            )
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def _invoke(self) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        stdout = io.StringIO()
        stderr = io.StringIO()
        import os
        old_name = os.environ.get("FLOW_COMMAND_NAME")
        os.environ["FLOW_COMMAND_NAME"] = "flow-abandon"
        try:
            import sys
            old_argv = sys.argv
            sys.argv = ["flow-abandon", cli._DIRECT_FLOW_ROUTE_TOKEN, "abandon"]
            os.chdir(self.repo)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code)
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = old_argv
            if old_name is None:
                os.environ.pop("FLOW_COMMAND_NAME", None)
            else:
                os.environ["FLOW_COMMAND_NAME"] = old_name
        return code, stdout.getvalue(), stderr.getvalue()

    def _assert_failure(self, expected: str) -> None:
        before_state = self.state_path.read_bytes() if self.state_path.exists() else None
        before_main = self._git("rev-parse", "main") if self._branch_exists("main") else None
        before_scratch = self._git("rev-parse", "scratch") if self._branch_exists("scratch") else None
        code, stdout, stderr = self._invoke()
        self.assertEqual(code, 1, stdout)
        self.assertIn(expected, stderr)
        self.assertEqual(self.state_path.read_bytes() if self.state_path.exists() else None, before_state)
        after_main = self._git("rev-parse", "main") if self._branch_exists("main") else None
        after_scratch = self._git("rev-parse", "scratch") if self._branch_exists("scratch") else None
        self.assertEqual(after_main, before_main)
        self.assertEqual(after_scratch, before_scratch)

    def test_synchronized_clean_github_workflow_clears_local_state_without_provider(self) -> None:
        self._write_state()
        (self.repo / ".ai-dev" / "promotion-review.json").write_text("review", encoding="utf-8")
        (self.repo / ".ai-dev" / "promotion-sync.json").write_text("sync", encoding="utf-8")
        baseline = self.repo / ".ai-dev" / "diff-baseline"
        baseline.mkdir()
        (baseline / "baseline.json").write_text("baseline", encoding="utf-8")
        preserved = {
            ".ai-dev/config.json": "config",
            ".ai-dev/blocked-workflows.json": "blocked",
            ".ai-dev/current-task.md": "task",
            ".ai-dev/review/evidence.md": "review artifact",
            ".ai-dev/usage/issue-45.json": "usage",
        }
        for relative_path, content in preserved.items():
            path = self.repo / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        ticket_before = self.state_path.read_bytes()
        main_before = self._git("rev-parse", "main")
        scratch_before = self._git("rev-parse", "scratch")

        with patch.object(cli, "resolve_ticket_provider_for_reference", side_effect=AssertionError("provider must not be resolved")):
            code, stdout, stderr = self._invoke()

        self.assertEqual(code, 0, stderr)
        self.assertIn("Abandoned local workflow", stdout)
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.repo / ".ai-dev/promotion-review.json").exists())
        self.assertFalse((self.repo / ".ai-dev/promotion-sync.json").exists())
        self.assertFalse(baseline.exists())
        for relative_path, content in preserved.items():
            self.assertEqual((self.repo / relative_path).read_text(encoding="utf-8"), content)
        self.assertEqual(self._git("rev-parse", "main"), main_before)
        self.assertEqual(self._git("rev-parse", "scratch"), scratch_before)
        self.assertEqual(ticket_before, json.dumps(json.loads(ticket_before), indent=2).encode() + b"\n")

    def test_local_ticket_file_is_untouched_byte_for_byte(self) -> None:
        self._write_state(provider="local")
        ticket_path = self.repo / ".ai-dev" / "tickets" / "45.json"
        ticket_path.parent.mkdir(parents=True)
        ticket_path.write_bytes(b'{"title":"local ticket","workflowState":"active"}\n')
        before = ticket_path.read_bytes()

        with patch.object(cli, "resolve_ticket_provider_for_reference", side_effect=AssertionError("provider must not be resolved")):
            code, _, stderr = self._invoke()

        self.assertEqual(code, 0, stderr)
        self.assertEqual(ticket_path.read_bytes(), before)

    def test_dirty_worktree_fails_without_mutation(self) -> None:
        self._write_state()
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self._assert_failure("repository must be clean")

    def test_ahead_scratch_fails_without_mutation(self) -> None:
        self._git("config", "user.name", "Abandon Tests")
        (self.repo / "ahead.txt").write_text("ahead\n", encoding="utf-8")
        self._git("add", "ahead.txt")
        self._git("commit", "-q", "-m", "ahead")
        self._write_state()
        self._assert_failure("scratch is ahead of main")

    def test_behind_scratch_fails_without_mutation(self) -> None:
        self._git("checkout", "-q", "main")
        (self.repo / "main.txt").write_text("main\n", encoding="utf-8")
        self._git("add", "main.txt")
        self._git("commit", "-q", "-m", "main ahead")
        self._git("checkout", "-q", "scratch")
        self._write_state()
        self._assert_failure("scratch is behind main")

    def test_diverged_scratch_fails_without_mutation(self) -> None:
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        self._git("add", "scratch.txt")
        self._git("commit", "-q", "-m", "scratch")
        self._git("checkout", "-q", "main")
        (self.repo / "main.txt").write_text("main\n", encoding="utf-8")
        self._git("add", "main.txt")
        self._git("commit", "-q", "-m", "main")
        self._git("checkout", "-q", "scratch")
        self._write_state()
        self._assert_failure("have diverged")

    def test_wrong_branch_fails_without_mutation(self) -> None:
        self._write_state()
        self._git("checkout", "-q", "main")
        self._assert_failure("current branch main does not match scratchBranch scratch")

    def test_missing_scratch_branch_fails_without_mutation(self) -> None:
        self._write_state()
        self._git("checkout", "-q", "main")
        self._git("branch", "-D", "scratch")
        self._assert_failure("Scratch branch does not exist locally: scratch")

    def test_missing_main_branch_fails_without_mutation(self) -> None:
        self._write_state()
        self._git("branch", "-D", "main")
        self._assert_failure("Main branch does not exist locally: main")

    def test_inactive_workflow_fails_without_mutation(self) -> None:
        self._write_state(active=False)
        self._assert_failure("no active workflow is set")

    def test_active_git_operation_fails_without_mutation(self) -> None:
        self._write_state()
        (self.repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        try:
            self._assert_failure("active operation(s)")
        finally:
            (self.repo / ".git" / "MERGE_HEAD").unlink()


if __name__ == "__main__":
    unittest.main()
