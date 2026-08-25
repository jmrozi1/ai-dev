from __future__ import annotations

import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_dev_flow import cli


class FlowAdoptInterfaceTests(unittest.TestCase):
    """Checkpoint adoption-interface-and-validation.

    Covers the bounded ``--adopt`` interface, commit-ish resolution, and the
    idle/repository/ancestry preconditions. The state transition itself lands in
    the adoption-state-transition checkpoint, so the successful path here is
    asserted up to its explicit boundary and proven non-mutating.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def _ticket(
        self,
        directory: Path,
        number: int,
        title: str,
        workflow: str,
        lifecycle: str = "open",
    ) -> None:
        payload = {
            "reference": {"provider": "local", "ticketId": str(number), "path": ".ai-dev/tickets"},
            "title": title,
            "lifecycleState": lifecycle,
            "workflowState": workflow,
        }
        (directory / f"{number}.json").write_text(json.dumps(payload), encoding="utf-8")

    def _repo(self, name: str) -> Path:
        """Idle Flow repository with a recovered branch descended from main."""
        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Adopt Tests")
        self._git(repo, "config", "user.email", "adopt@example.com")
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "tracked.txt")
        self._git(repo, "commit", "-q", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "branch", "scratch", "main")

        # Recovered work: descends from main, carries numbered checkpoints.
        self._git(repo, "checkout", "-q", "-b", "recovered", "main")
        for index in (1, 2, 3):
            (repo / f"recovered-{index}.txt").write_text(f"recovered {index}\n", encoding="utf-8")
            self._git(repo, "add", f"recovered-{index}.txt")
            self._git(repo, "commit", "-q", "-m", str(index))
        self._git(repo, "checkout", "-q", "main")

        ai_dev = repo / ".ai-dev"
        tickets = ai_dev / "tickets"
        tickets.mkdir(parents=True)
        (ai_dev / "config.json").write_text(
            json.dumps({"tickets": {"provider": "local", "path": ".ai-dev/tickets"}}),
            encoding="utf-8",
        )
        self._ticket(tickets, 57, "Adoptable issue", "inactive")
        self._ticket(tickets, 58, "Closed issue", "inactive", lifecycle="closed")
        (ai_dev / "workflow.json").write_text(
            json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}),
            encoding="utf-8",
        )
        return repo

    def _invoke_command(self, repo: Path, command: str, *args: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = sys.argv
        os.environ["FLOW_COMMAND_NAME"] = f"flow-{command}"
        sys.argv = [f"flow-{command}", cli._DIRECT_FLOW_ROUTE_TOKEN, command, *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        os.chdir(repo)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    return int(exc.code), stdout.getvalue(), stderr.getvalue()
                return 0, stdout.getvalue(), stderr.getvalue()
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv

    def _invoke(self, repo: Path, *args: str) -> tuple[int, str, str]:
        return self._invoke_command(repo, "start", *args)

    def _state(self, repo: Path) -> dict:
        return json.loads((repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"))

    def _assert_unmutated(self, repo: Path, snapshot: dict) -> None:
        self.assertEqual(self._git(repo, "rev-parse", "main"), snapshot["main"])
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), snapshot["scratch"])
        self.assertEqual(self._state(repo), snapshot["state"])
        self.assertEqual(
            json.loads((repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8"))["workflowState"],
            snapshot["ticket_workflow_state"],
        )

    def _snapshot(self, repo: Path) -> dict:
        return {
            "main": self._git(repo, "rev-parse", "main"),
            "scratch": self._git(repo, "rev-parse", "scratch"),
            "state": self._state(repo),
            "ticket_workflow_state": json.loads(
                (repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8")
            )["workflowState"],
        }

    # Successful validation path -------------------------------------------------

    def test_valid_adoption_passes_every_precondition_without_mutating(self) -> None:
        repo = self._repo("valid")
        snapshot = self._snapshot(repo)
        recovered = self._git(repo, "rev-parse", "recovered")

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        # Validation succeeded end to end and stopped at the declared boundary.
        self.assertEqual(code, 1)
        self.assertIn("the adoption state transition is not implemented yet", err)
        self.assertIn(recovered, err)
        self._assert_unmutated(repo, snapshot)

    def test_adoption_accepts_a_bare_commit_sha(self) -> None:
        repo = self._repo("sha")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, _out, err = self._invoke(repo, "57", "--adopt", recovered)

        self.assertEqual(code, 1)
        self.assertIn("the adoption state transition is not implemented yet", err)
        self.assertIn(recovered, err)

    def test_adoption_accepts_a_tag(self) -> None:
        repo = self._repo("tag")
        self._git(repo, "tag", "recovery-point", "recovered")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovery-point")

        self.assertEqual(code, 1)
        self.assertIn(recovered, err)

    # Interface rejections -------------------------------------------------------

    def test_missing_adopt_value_is_a_usage_error(self) -> None:
        repo = self._repo("usage-missing")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt")

        self.assertEqual(code, 1)
        self.assertIn("Usage:", err)
        self.assertIn("--adopt <commit-ish>", err)
        self._assert_unmutated(repo, snapshot)

    def test_adopt_cannot_be_combined_with_prerequisite_for(self) -> None:
        repo = self._repo("usage-combined")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(
            repo, "57", "--adopt", "recovered", "--prerequisite-for", "58"
        )

        self.assertEqual(code, 1)
        self.assertIn("cannot be combined", err)
        self._assert_unmutated(repo, snapshot)

    def test_empty_adopt_value_is_rejected(self) -> None:
        repo = self._repo("usage-empty")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "   ")

        self.assertEqual(code, 1)
        self.assertIn("adopted revision cannot be empty", err)
        self._assert_unmutated(repo, snapshot)

    def test_option_like_revision_is_rejected(self) -> None:
        repo = self._repo("usage-option")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "--all")

        self.assertEqual(code, 1)
        self.assertIn("Revision cannot start with '-'", err)
        self._assert_unmutated(repo, snapshot)

    def test_non_numeric_issue_is_rejected(self) -> None:
        repo = self._repo("usage-issue")

        code, _out, err = self._invoke(repo, "abc", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("issue-number must be a positive integer", err)

    # Revision resolution rejections ---------------------------------------------

    def test_unresolvable_revision_is_rejected(self) -> None:
        repo = self._repo("unresolvable")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "no-such-ref")

        self.assertEqual(code, 1)
        self.assertIn("Cannot resolve revision to a commit: no-such-ref", err)
        self._assert_unmutated(repo, snapshot)

    def test_non_commit_revision_is_rejected(self) -> None:
        repo = self._repo("non-commit")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "main:tracked.txt")

        self.assertEqual(code, 1)
        self.assertIn("Cannot resolve revision to a commit", err)
        self._assert_unmutated(repo, snapshot)

    # Relationship rejections -----------------------------------------------------

    def test_revision_equal_to_main_is_rejected(self) -> None:
        repo = self._repo("equals-main")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "main")

        self.assertEqual(code, 1)
        self.assertIn("already equals main", err)
        self.assertIn("no work to adopt", err)
        self._assert_unmutated(repo, snapshot)

    def test_revision_not_descended_from_main_is_rejected(self) -> None:
        repo = self._repo("diverged")
        # Advance main so the recovered branch is behind and divergent.
        self._git(repo, "checkout", "-q", "main")
        (repo / "main-only.txt").write_text("main only\n", encoding="utf-8")
        self._git(repo, "add", "main-only.txt")
        self._git(repo, "commit", "-q", "-m", "main only")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("main is not an ancestor of adopted revision recovered", err)
        self.assertIn("does not fetch, merge, rebase", err)
        self._assert_unmutated(repo, snapshot)

    def test_unrelated_history_is_rejected(self) -> None:
        repo = self._repo("unrelated")
        self._git(repo, "checkout", "-q", "--orphan", "unrelated")
        self._git(repo, "rm", "-rq", "--cached", ".")
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        self._git(repo, "add", "unrelated.txt")
        self._git(repo, "commit", "-q", "-m", "unrelated root")
        self._git(repo, "checkout", "-q", "-f", "main")
        self._git(repo, "clean", "-qfd")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "unrelated")

        self.assertEqual(code, 1)
        self.assertIn("is not an ancestor of adopted revision unrelated", err)
        self._assert_unmutated(repo, snapshot)

    # Idle-state and repository rejections ----------------------------------------

    def test_active_issue_workflow_blocks_adoption(self) -> None:
        repo = self._repo("active-issue")
        (repo / ".ai-dev/workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 2,
                    "activeIssueNumber": 99,
                    "activeIssueTitle": "Something else",
                    "ticket": {"provider": "local", "ticketId": "99", "path": ".ai-dev/tickets"},
                }
            ),
            encoding="utf-8",
        )
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("active issue 99 is already set", err)
        self._assert_unmutated(repo, snapshot)

    def test_active_patch_workflow_blocks_adoption(self) -> None:
        repo = self._repo("active-patch")
        (repo / ".ai-dev/workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 1,
                    "patchDescription": "in-flight patch",
                }
            ),
            encoding="utf-8",
        )
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("active patch in-flight patch is already set", err)
        self._assert_unmutated(repo, snapshot)

    def test_blocked_issue_is_routed_to_resume(self) -> None:
        repo = self._repo("blocked")
        (repo / ".ai-dev/blocked-workflows.json").write_text(
            json.dumps(
                {
                    "blockedWorkflows": [
                        {
                            "issueNumber": 57,
                            "issueTitle": "Adoptable issue",
                            "reason": "waiting",
                            "checkpoint": 0,
                            "blockedAt": "2026-08-25T00:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("issue 57 is blocked", err)
        self.assertIn("flow-resume 57", err)
        self._assert_unmutated(repo, snapshot)

    def test_dirty_worktree_blocks_adoption(self) -> None:
        repo = self._repo("dirty")
        (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("working tree is not clean", err)
        self._assert_unmutated(repo, snapshot)

    def test_active_git_operation_blocks_adoption(self) -> None:
        repo = self._repo("git-op")
        (repo / ".git" / "MERGE_HEAD").write_text(
            self._git(repo, "rev-parse", "recovered") + "\n", encoding="utf-8"
        )
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("active operation(s): merge", err)
        self._assert_unmutated(repo, snapshot)

    # Ticket rejections -------------------------------------------------------------

    def test_closed_ticket_is_rejected(self) -> None:
        repo = self._repo("closed")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "58", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self.assertIn("ticket 58 is closed", err)
        self._assert_unmutated(repo, snapshot)

    def test_unknown_ticket_is_rejected(self) -> None:
        repo = self._repo("unknown-ticket")
        snapshot = self._snapshot(repo)

        code, _out, err = self._invoke(repo, "404", "--adopt", "recovered")

        self.assertEqual(code, 1)
        self._assert_unmutated(repo, snapshot)

    # Regression --------------------------------------------------------------------

    def test_ordinary_start_is_unchanged(self) -> None:
        repo = self._repo("ordinary")
        main_before = self._git(repo, "rev-parse", "main")

        code, out, err = self._invoke(repo, "57")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("Started issue 57", out)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), main_before)
        state = self._state(repo)
        self.assertEqual(state["activeIssueNumber"], 57)
        self.assertEqual(state["checkpoint"], 0)

    def test_start_help_documents_the_adopt_form(self) -> None:
        repo = self._repo("help")

        code, out, err = self._invoke(repo, "--help")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("--adopt <commit-ish>", out)


if __name__ == "__main__":
    unittest.main()
