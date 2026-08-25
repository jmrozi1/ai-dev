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
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.repository import RepositoryError
from ai_dev_flow.ticket_providers import LocalTicketProvider, TicketProviderError
from ai_dev_flow.workflow_state import WorkflowStateError


class _FlowAdoptFixture(unittest.TestCase):
    """Checkpoints adoption-interface-and-validation and adoption-state-transition.

    Covers the bounded ``--adopt`` interface, commit-ish resolution, the
    idle/repository/ancestry preconditions, and the adoption state transition
    that places scratch at the exact adopted SHA and binds the issue workflow.
    Failure atomicity lives in :class:`FlowAdoptFailureSafetyTests`.
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



class FlowAdoptInterfaceTests(_FlowAdoptFixture):
    # Successful validation path -------------------------------------------------

    def test_adoption_places_scratch_at_the_exact_adopted_sha(self) -> None:
        repo = self._repo("valid")
        main_before = self._git(repo, "rev-parse", "main")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("Adopted issue 57", out)
        self.assertIn("adoptedCommit: " + recovered, out)
        # Exact SHA preservation, not an equivalent replay.
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)
        self.assertEqual(self._git(repo, "rev-parse", "HEAD"), recovered)
        self.assertEqual(self._git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "scratch")
        # main is untouched.
        self.assertEqual(self._git(repo, "rev-parse", "main"), main_before)

    def test_adoption_binds_a_normal_issue_workflow(self) -> None:
        repo = self._repo("bound")

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        state = self._state(repo)
        self.assertEqual(state["activeIssueNumber"], 57)
        self.assertEqual(state["activeIssueTitle"], "Adoptable issue")
        self.assertEqual(state["ticket"]["ticketId"], "57")
        self.assertEqual(state["mainBranch"], "main")
        self.assertEqual(state["scratchBranch"], "scratch")
        self.assertNotIn("patchDescription", state)
        self.assertEqual(
            json.loads((repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8"))["workflowState"],
            "active",
        )

    def test_adopted_checkpoint_uses_max_numbered_checkpoint_semantics(self) -> None:
        repo = self._repo("checkpoint")
        # Restarted numbering: 1,2,3 then 1,2. Max wins, not the visible tip.
        self._git(repo, "checkout", "-q", "recovered")
        for subject in ("1", "2"):
            (repo / ("restarted-" + subject + ".txt")).write_text(subject, encoding="utf-8")
            self._git(repo, "add", "restarted-" + subject + ".txt")
            self._git(repo, "commit", "-q", "-m", subject)
        self._git(repo, "checkout", "-q", "main")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("checkpoint: 3", out)
        self.assertEqual(self._state(repo)["checkpoint"], 3)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)

        # The next checkpoint continues past the maximum instead of colliding.
        (repo / "next.txt").write_text("next\n", encoding="utf-8")
        self._git(repo, "add", "next.txt")
        code, out, err = self._invoke_command(repo, "commit")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Created checkpoint 4", out)

    def test_adoption_derives_zero_when_no_numbered_subjects_exist(self) -> None:
        repo = self._repo("unnumbered")
        self._git(repo, "checkout", "-q", "-b", "prose", "main")
        (repo / "prose.txt").write_text("prose\n", encoding="utf-8")
        self._git(repo, "add", "prose.txt")
        self._git(repo, "commit", "-q", "-m", "descriptive subject")
        self._git(repo, "checkout", "-q", "main")

        code, out, err = self._invoke(repo, "57", "--adopt", "prose")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("checkpoint: 0", out)
        self.assertEqual(self._state(repo)["checkpoint"], 0)

    def test_adoption_leaves_no_passing_promotion_review_record(self) -> None:
        repo = self._repo("review-gate")
        (repo / ".ai-dev/promotion-review.json").write_text(
            json.dumps({"status": "pass", "issueNumber": 57, "scratchCommit": "stale"}),
            encoding="utf-8",
        )

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        self.assertFalse((repo / ".ai-dev/promotion-review.json").exists())

    def test_adoption_accepts_a_bare_commit_sha(self) -> None:
        repo = self._repo("sha")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, out, err = self._invoke(repo, "57", "--adopt", recovered)

        self.assertEqual((code, err), (0, ""))
        self.assertIn("adoptedCommit: " + recovered, out)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)

    def test_adoption_accepts_a_tag(self) -> None:
        repo = self._repo("tag")
        self._git(repo, "tag", "recovery-point", "recovered")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, out, err = self._invoke(repo, "57", "--adopt", "recovery-point")

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)

    def test_adoption_from_scratch_checkout_moves_the_branch(self) -> None:
        """Scratch cannot be force-updated while checked out; Flow must handle it."""
        repo = self._repo("on-scratch")
        self._git(repo, "checkout", "-q", "scratch")
        recovered = self._git(repo, "rev-parse", "recovered")

        code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)

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


class FlowAdoptFailureSafetyTests(_FlowAdoptFixture):
    """Checkpoint adoption-failure-safety.

    Adoption is all-or-nothing. For every covered failure the externally
    visible state afterwards must equal the state before the attempt, across
    Git, Flow, review/promotion artifacts, and the ticket provider.
    """

    # Full externally visible state -------------------------------------------

    def _visible_state(self, repo: Path) -> dict:
        def read(relative: str):
            path = repo / relative
            return path.read_bytes() if path.is_file() else None

        return {
            "branch": self._git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "head": self._git(repo, "rev-parse", "HEAD"),
            "main": self._git(repo, "rev-parse", "main"),
            "scratch": (
                self._git(repo, "rev-parse", "scratch")
                if self._branch_exists(repo, "scratch")
                else None
            ),
            "workflow": read(".ai-dev/workflow.json"),
            "promotion_review": read(".ai-dev/promotion-review.json"),
            "promotion_sync": read(".ai-dev/promotion-sync.json"),
            "diff_baseline": read(".ai-dev/diff-baseline/baseline.json"),
            # Provider records cannot be byte-restored: deactivate() stamps a
            # fresh updatedAt. The invariant is that lifecycle/label state is
            # not left contradicting local Flow state, so compare that.
            "ticket_57_state": self._ticket_state(repo, 57),
            "git_operations": sorted(
                marker
                for marker in (
                    "MERGE_HEAD",
                    "CHERRY_PICK_HEAD",
                    "REVERT_HEAD",
                    "BISECT_LOG",
                    "rebase-apply",
                    "rebase-merge",
                )
                if (repo / ".git" / marker).exists()
            ),
        }

    def _ticket_state(self, repo: Path, number: int) -> tuple:
        payload = json.loads(
            (repo / f".ai-dev/tickets/{number}.json").read_text(encoding="utf-8")
        )
        return (
            payload["lifecycleState"],
            payload["workflowState"],
            tuple(payload.get("labels", ())),
        )

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/" + branch],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _seed_review_and_sync_artifacts(self, repo: Path) -> None:
        """Pre-existing artifacts prove restoration, not merely absence."""
        (repo / ".ai-dev/promotion-review.json").write_text(
            json.dumps({"status": "pass", "issueNumber": 99, "scratchCommit": "prior"}),
            encoding="utf-8",
        )
        (repo / ".ai-dev/promotion-sync.json").write_text(
            json.dumps({"status": "pending", "issueNumber": 99}), encoding="utf-8"
        )
        baseline = repo / ".ai-dev/diff-baseline"
        baseline.mkdir(parents=True, exist_ok=True)
        (baseline / "baseline.json").write_text(
            json.dumps({"baseline": "prior"}), encoding="utf-8"
        )

    def _assert_failed_adoption_preserved_everything(
        self, repo: Path, before: dict, code: int, err: str
    ) -> None:
        self.assertEqual(code, 1)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(self._visible_state(repo), before)

    # Provider activation failure ----------------------------------------------

    def test_provider_activation_failure_restores_exact_pre_adoption_state(self) -> None:
        repo = self._repo("activation-failure")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)
        recovered = self._git(repo, "rev-parse", "recovered")
        self.assertNotEqual(before["scratch"], recovered)

        with patch.object(
            LocalTicketProvider,
            "mark_active",
            side_effect=TicketProviderError("simulated activation outage"),
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertIn("failed to mark ticket 57 active", err)
        self.assertIn("simulated activation outage", err)
        self.assertNotIn("Rollback failures", err)
        self._assert_failed_adoption_preserved_everything(repo, before, code, err)
        # The adopted commit was never left on scratch.
        self.assertNotEqual(self._git(repo, "rev-parse", "scratch"), recovered)

    def test_provider_activation_failure_leaves_ticket_inactive(self) -> None:
        repo = self._repo("activation-provider-state")

        with patch.object(
            LocalTicketProvider,
            "mark_active",
            side_effect=TicketProviderError("simulated activation outage"),
        ):
            code, _out, _err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual(code, 1)
        ticket = json.loads((repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8"))
        # Provider must not claim an active workflow Flow does not own.
        self.assertEqual(ticket["workflowState"], "inactive")
        self.assertNotIn("activeIssueNumber", self._state(repo))

    def test_provider_activation_failure_restores_state_when_scratch_checked_out(self) -> None:
        repo = self._repo("activation-on-scratch")
        self._git(repo, "checkout", "-q", "scratch")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)
        self.assertEqual(before["branch"], "scratch")

        with patch.object(
            LocalTicketProvider,
            "mark_active",
            side_effect=TicketProviderError("simulated activation outage"),
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self._assert_failed_adoption_preserved_everything(repo, before, code, err)
        # Still on scratch, still at its original commit.
        self.assertEqual(self._git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "scratch")

    def test_rollback_deletes_a_scratch_branch_it_created(self) -> None:
        repo = self._repo("activation-no-scratch")
        self._git(repo, "branch", "-D", "scratch")
        before = self._visible_state(repo)
        self.assertIsNone(before["scratch"])

        with patch.object(
            LocalTicketProvider,
            "mark_active",
            side_effect=TicketProviderError("simulated activation outage"),
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self._assert_failed_adoption_preserved_everything(repo, before, code, err)
        self.assertFalse(self._branch_exists(repo, "scratch"))

    # Other transactional seams ------------------------------------------------

    def test_workflow_state_write_failure_rolls_back_git_and_provider(self) -> None:
        repo = self._repo("state-write-failure")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)

        with patch.object(
            cli, "save_state", side_effect=WorkflowStateError("simulated state write failure")
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertIn("simulated state write failure", err)
        self.assertNotIn("Rollback failures", err)
        self._assert_failed_adoption_preserved_everything(repo, before, code, err)
        # Activation happened before the failure and must have been undone.
        ticket = json.loads((repo / ".ai-dev/tickets/57.json").read_text(encoding="utf-8"))
        self.assertEqual(ticket["workflowState"], "inactive")

    def test_diff_baseline_failure_rolls_back(self) -> None:
        repo = self._repo("baseline-failure")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)

        with patch.object(
            cli,
            "clear_diff_baseline_for_repo_root",
            side_effect=RepositoryError("simulated baseline failure"),
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertIn("simulated baseline failure", err)
        self._assert_failed_adoption_preserved_everything(repo, before, code, err)

    def test_scratch_move_failure_rolls_back(self) -> None:
        repo = self._repo("branch-move-failure")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)

        real = cli.create_or_reset_branch_from_source
        calls = {"n": 0}

        def fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RepositoryError("simulated branch move failure")
            return real(*args, **kwargs)

        with patch.object(cli, "create_or_reset_branch_from_source", side_effect=fail_first):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertIn("simulated branch move failure", err)
        self._assert_failed_adoption_preserved_everything(repo, before, code, err)

    def test_promotion_review_clear_failure_rolls_back(self) -> None:
        repo = self._repo("review-clear-failure")
        self._seed_review_and_sync_artifacts(repo)
        before = self._visible_state(repo)

        with patch.object(
            cli,
            "_clear_promotion_review_record",
            side_effect=OSError("simulated review record failure"),
        ):
            code, _out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertIn("simulated review record failure", err)
        self._assert_failed_adoption_preserved_everything(repo, before, code, err)
        # The pre-existing passing record was restored, not silently consumed.
        self.assertEqual(
            json.loads((repo / ".ai-dev/promotion-review.json").read_text(encoding="utf-8"))[
                "issueNumber"
            ],
            99,
        )

    # Success path is unaffected by the transaction ----------------------------

    def test_successful_adoption_still_clears_prior_artifacts(self) -> None:
        repo = self._repo("success-through-transaction")
        self._seed_review_and_sync_artifacts(repo)
        recovered = self._git(repo, "rev-parse", "recovered")

        code, out, err = self._invoke(repo, "57", "--adopt", "recovered")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("adoptedCommit: " + recovered, out)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), recovered)
        self.assertFalse((repo / ".ai-dev/promotion-review.json").exists())
        self.assertFalse((repo / ".ai-dev/diff-baseline/baseline.json").exists())
        self.assertEqual(self._state(repo)["activeIssueNumber"], 57)


if __name__ == "__main__":
    unittest.main()
