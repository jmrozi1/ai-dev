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
from ai_dev_flow.ticket_providers import LocalTicketProvider
from ai_dev_flow.workflow_state import WorkflowStateError, normalize_and_validate


class FlowPrerequisiteTests(unittest.TestCase):
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

    def _repo(self, name: str) -> Path:
        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Prerequisite Tests")
        self._git(repo, "config", "user.email", "prerequisite@example.com")
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "tracked.txt")
        self._git(repo, "commit", "-q", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "checkout", "-q", "-b", "scratch")
        ai_dev = repo / ".ai-dev"
        tickets = ai_dev / "tickets"
        tickets.mkdir(parents=True)
        (ai_dev / "config.json").write_text(json.dumps({
            "tickets": {"provider": "local", "path": ".ai-dev/tickets"},
            "review": {"promotionGate": False},
        }), encoding="utf-8")
        self._ticket(tickets, 10, "Active A", "active")
        self._ticket(tickets, 20, "Prerequisite B", "inactive")
        (ai_dev / "workflow.json").write_text(json.dumps({
            "mainBranch": "main",
            "scratchBranch": "scratch",
            "checkpoint": 3,
            "activeIssueNumber": 10,
            "activeIssueTitle": "Active A",
            "ticket": {"provider": "local", "ticketId": "10", "path": ".ai-dev/tickets"},
        }), encoding="utf-8")
        (repo / "partial.txt").write_text("A partial tree\n", encoding="utf-8")
        self._git(repo, "add", "partial.txt")
        self._git(repo, "commit", "-q", "-m", "A checkpoint")
        for index in (2, 3):
            (repo / f"a-checkpoint-{index}.txt").write_text(f"A checkpoint {index}\n", encoding="utf-8")
            self._git(repo, "add", f"a-checkpoint-{index}.txt")
            self._git(repo, "commit", "-q", "-m", f"A checkpoint {index}")
        return repo

    def _ticket(
        self,
        directory: Path,
        number: int,
        title: str,
        workflow: str,
        labels: tuple[str, ...] = (),
    ) -> None:
        payload = {
            "reference": {"provider": "local", "ticketId": str(number), "path": ".ai-dev/tickets"},
            "title": title,
            "lifecycleState": "open",
            "workflowState": workflow,
        }
        if labels:
            payload["labels"] = list(labels)
        (directory / f"{number}.json").write_text(json.dumps(payload), encoding="utf-8")

    def _invoke(self, repo: Path, *args: str) -> tuple[int, str, str]:
        return self._invoke_command(repo, "start", *args)

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

    def test_clean_handoff_preserves_tree_and_persists_ownership(self) -> None:
        repo = self._repo("success")
        head_before = self._git(repo, "rev-parse", "scratch")
        tree_before = self._git(repo, "rev-parse", "scratch^{tree}")

        code, out, err = self._invoke(repo, "20", "--prerequisite-for", "10")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("Started prerequisite issue 20 for issue 10", out)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), head_before)
        self.assertEqual(self._git(repo, "rev-parse", "scratch^{tree}"), tree_before)
        state = json.loads((repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(state["activeIssueNumber"], 20)
        self.assertEqual(state["checkpoint"], 0)
        self.assertEqual(state["stackedHandoff"]["inheritedBase"]["commit"], head_before)
        self.assertEqual(state["stackedHandoff"]["inheritedBase"]["tree"], tree_before)
        suspended = state["stackedHandoff"]["suspendedIssue"]
        self.assertEqual(suspended["issueNumber"], 10)
        self.assertEqual(suspended["checkpoint"], 3)
        self.assertEqual(suspended["commit"], head_before)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "blocked")
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/20.json").read_text())["workflowState"], "active")

    def test_stacked_checkpoint_diff_and_status_use_active_scope(self) -> None:
        repo = self._repo("scope")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual((code, err), (0, ""))
        state = json.loads((repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"))
        inherited_base = state["stackedHandoff"]["inheritedBase"]["commit"]

        (repo / "b-checkpoint.txt").write_text("B checkpoint\n", encoding="utf-8")
        self._git(repo, "add", "b-checkpoint.txt")
        code, out, err = self._invoke_command(repo, "commit")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Created checkpoint 1", out)
        (repo / "b-working.txt").write_text("B working\n", encoding="utf-8")

        code, diff, err = self._invoke_command(repo, "diff", "--all")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("b-checkpoint.txt", diff)
        self.assertIn("b-working.txt", diff)
        self.assertNotIn("partial.txt", diff)
        self.assertEqual(self._git(repo, "rev-list", "--count", f"{inherited_base}..scratch"), "1")

        code, status, err = self._invoke_command(repo, "status", "-v")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("active prerequisite: issue 20", status)
        self.assertIn("suspended original: issue 10", status)
        self.assertIn("suspended checkpoint: 3", status)
        self.assertIn(f"inherited base commit: {inherited_base}", status)
        self.assertIn("managed suspended ref: refs/ai-dev/suspended/10", status)
        self.assertIn("active scope: checkpoint 1, 1 commit(s) from inherited base", status)
        self.assertIn("includes inherited A tree", status)

    def test_stacked_baseline_cannot_cross_scope_identity(self) -> None:
        repo = self._repo("scope-baseline")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        (repo / "b-working.txt").write_text("B working\n", encoding="utf-8")
        code, _, err = self._invoke_command(repo, "diff", "--refresh")
        self.assertEqual((code, err), (0, ""))
        baseline_path = repo / ".ai-dev" / "diff-baseline" / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["workflow"]["stackedScope"]["inheritedBaseCommit"] = "cross-scope"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        code, _, err = self._invoke_command(repo, "diff")
        self.assertEqual(code, 1)
        self.assertIn("Review baseline is stale or invalid", err)

    def test_stacked_reset_discards_only_b_and_preserves_a_state(self) -> None:
        repo = self._repo("reset")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        state_before = json.loads((repo / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        inherited_base = state_before["stackedHandoff"]["inheritedBase"]["commit"]
        ref_before = self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10")
        blocked_before = (repo / ".ai-dev" / "blocked-workflows.json").read_bytes()
        main_before = self._git(repo, "rev-parse", "main")

        for filename in ("b-one.txt", "b-two.txt"):
            (repo / filename).write_text(filename, encoding="utf-8")
            self._git(repo, "add", filename)
            code, _, err = self._invoke_command(repo, "commit")
            self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads((repo / ".ai-dev" / "workflow.json").read_text())["checkpoint"], 2)
        self.assertEqual(self._invoke_command(repo, "diff", "--refresh")[0], 0)

        code, out, err = self._invoke_command(repo, "reset")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Reset scratch to inherited base", out)
        self.assertEqual(self._git(repo, "rev-parse", "main"), main_before)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), inherited_base)
        self.assertTrue((repo / "partial.txt").exists())
        self.assertFalse((repo / "b-one.txt").exists())
        self.assertFalse((repo / "b-two.txt").exists())
        state_after = json.loads((repo / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(state_after["checkpoint"], 0)
        self.assertEqual(state_after["stackedHandoff"], state_before["stackedHandoff"])
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"), ref_before)
        self.assertEqual((repo / ".ai-dev" / "blocked-workflows.json").read_bytes(), blocked_before)
        self.assertFalse((repo / ".ai-dev" / "diff-baseline" / "baseline.json").exists())
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "blocked")
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/20.json").read_text())["workflowState"], "active")

    def test_non_ancestor_inherited_base_rejects_without_mutation(self) -> None:
        repo = self._repo("non-ancestor")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self._git(repo, "checkout", "-q", "-b", "unrelated")
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        self._git(repo, "add", "unrelated.txt")
        self._git(repo, "commit", "-q", "-m", "unrelated")
        unrelated_commit = self._git(repo, "rev-parse", "HEAD")
        unrelated_tree = self._git(repo, "rev-parse", "HEAD^{tree}")
        self._git(repo, "checkout", "-q", "scratch")
        self._git(repo, "update-ref", "refs/ai-dev/suspended/10", unrelated_commit)
        state_path = repo / ".ai-dev" / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        handoff = state["stackedHandoff"]
        handoff["inheritedBase"]["commit"] = unrelated_commit
        handoff["inheritedBase"]["tree"] = unrelated_tree
        handoff["suspendedIssue"]["commit"] = unrelated_commit
        handoff["suspendedIssue"]["tree"] = unrelated_tree
        state_path.write_text(json.dumps(state), encoding="utf-8")
        head_before = self._git(repo, "rev-parse", "scratch")
        state_before = state_path.read_bytes()
        code, _, err = self._invoke_command(repo, "reset")
        self.assertEqual(code, 1)
        self.assertIn("not an ancestor", err)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), head_before)
        self.assertEqual(state_path.read_bytes(), state_before)

    def test_missing_or_mismatched_inherited_base_rejects_without_mutation(self) -> None:
        repo = self._repo("missing-base")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        state_path = repo / ".ai-dev" / "workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["stackedHandoff"]["inheritedBase"]["commit"] = "missing-commit"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        head_before = self._git(repo, "rev-parse", "scratch")
        code, _, err = self._invoke_command(repo, "reset")
        self.assertEqual(code, 1)
        self.assertIn("inherited base does not match suspended commit", err)
        self.assertEqual(self._git(repo, "rev-parse", "scratch"), head_before)

    def test_stacked_promotion_completion_resume_and_ref_cleanup(self) -> None:
        repo = self._repo("lifecycle")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        (repo / "b.txt").write_text("B\n", encoding="utf-8")
        self._git(repo, "add", "b.txt")
        self.assertEqual(self._invoke_command(repo, "commit")[0], 0)
        scratch_tree_before = self._git(repo, "rev-parse", "scratch^{tree}")

        code, output, err = self._invoke_command(repo, "promote", "Publish stacked tree")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("complete physical A+B tree", output)
        self.assertEqual(self._git(repo, "rev-parse", "main^{tree}"), scratch_tree_before)
        self.assertEqual(self._git(repo, "rev-parse", "main"), self._git(repo, "rev-parse", "scratch"))
        blocked = json.loads((repo / ".ai-dev/blocked-workflows.json").read_text(encoding="utf-8"))
        resume_metadata = blocked["blockedWorkflows"][0]["stackedResume"]
        promoted_commit = self._git(repo, "rev-parse", "main")
        self.assertEqual(resume_metadata["promotedMainCommit"], promoted_commit)
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"), resume_metadata["suspendedCommit"])

        code, _, err = self._invoke_command(repo, "complete")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/20.json").read_text())["lifecycleState"], "closed")
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "blocked")
        blocked_after_b = json.loads((repo / ".ai-dev/blocked-workflows.json").read_text())
        self.assertIn("stackedResume", blocked_after_b["blockedWorkflows"][0])

        code, resume_output, err = self._invoke_command(repo, "resume", "10")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("checkpoint: 3", resume_output)
        resumed_state = json.loads((repo / ".ai-dev/workflow.json").read_text())
        self.assertEqual(resumed_state["activeIssueNumber"], 10)
        self.assertEqual(resumed_state["checkpoint"], 3)
        self.assertEqual(self._git(repo, "rev-parse", "main"), self._git(repo, "rev-parse", "scratch"))
        code, diff, err = self._invoke_command(repo, "diff", "--all")
        self.assertEqual(code, 0)
        self.assertIn("No diff content for current scope", err)
        self.assertEqual(diff, "")

        (repo / "a-next.txt").write_text("A next\n", encoding="utf-8")
        self._git(repo, "add", "a-next.txt")
        code, output, err = self._invoke_command(repo, "commit")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Created checkpoint 4", output)

        code, _, err = self._invoke_command(repo, "promote", "Publish resumed A")
        self.assertEqual((code, err), (0, ""))
        code, _, err = self._invoke_command(repo, "complete")
        self.assertEqual((code, err), (0, ""))
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(repo), "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).returncode,
            0,
        )

    def test_empty_resumed_original_completes_without_synthetic_work(self) -> None:
        repo = self._repo("direct-complete")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)
        (repo / ".ai-dev" / "promotion-sync.json").unlink(missing_ok=True)
        (repo / ".ai-dev" / "promotion-review.json").unlink(missing_ok=True)

        code, output, err = self._invoke_command(repo, "complete")

        self.assertEqual((code, err), (0, ""))
        self.assertIn("Completed issue 10", output)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["lifecycleState"], "closed")
        self.assertFalse((repo / ".ai-dev/workflow.json").read_text().find("stackedResume") >= 0)
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(repo), "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).returncode,
            0,
        )

    def test_new_resumed_work_still_requires_promotion(self) -> None:
        repo = self._repo("direct-complete-new-work")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)
        (repo / "new-a.txt").write_text("new A\n", encoding="utf-8")
        self._git(repo, "add", "new-a.txt")
        self.assertEqual(self._invoke_command(repo, "commit")[0], 0)

        code, _, err = self._invoke_command(repo, "complete")

        self.assertEqual(code, 1)
        self.assertIn("scratch is not the recorded promoted commit", err)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["lifecycleState"], "open")

    def test_direct_completion_rejects_canonical_or_ref_mismatch_before_provider(self) -> None:
        repo = self._repo("direct-complete-mismatch")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)
        self._git(repo, "update-ref", "refs/ai-dev/suspended/10", self._git(repo, "rev-parse", "main"))

        with patch.object(LocalTicketProvider, "complete", side_effect=AssertionError("provider must not run")):
            code, _, err = self._invoke_command(repo, "complete")

        self.assertEqual(code, 1)
        self.assertIn("suspended ref does not match", err)

    def test_resumed_original_rejects_canonical_drift_before_provider(self) -> None:
        repo = self._repo("canonical-drift")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)

        drift_branch = "canonical-drift"
        self._git(repo, "branch", drift_branch)
        (repo / "canonical-drift.txt").write_text("canonical drift\n", encoding="utf-8")
        self._git(repo, "add", "canonical-drift.txt")
        self._git(repo, "commit", "-q", "-m", "canonical drift")
        drift_commit = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "checkout", "-q", "main")
        self._git(repo, "merge", "--ff-only", drift_commit)
        self._git(repo, "checkout", "-q", "scratch")
        ref_before = self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10")

        with patch.object(LocalTicketProvider, "complete", side_effect=AssertionError("provider must not run")):
            code, _, err = self._invoke_command(repo, "complete")

        self.assertEqual(code, 1)
        self.assertIn("main is not the recorded promoted commit", err)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["lifecycleState"], "open")
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"), ref_before)

    def test_direct_completion_rejects_pending_synchronization(self) -> None:
        repo = self._repo("direct-complete-pending")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)
        (repo / ".ai-dev" / "promotion-sync.json").write_text(
            json.dumps({
                "version": 1,
                "status": "pending",
                "mainBranch": "main",
                "scratchBranch": "scratch",
                "promotedMainCommit": self._git(repo, "rev-parse", "main"),
                "remote": "origin",
                "upstreamRef": "refs/heads/main",
                "activeIssueNumber": 10,
            }),
            encoding="utf-8",
        )

        code, _, err = self._invoke_command(repo, "complete")

        self.assertEqual(code, 1)
        self.assertIn("promotion synchronization is pending", err)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["lifecycleState"], "open")

    def test_b_completion_provider_failure_preserves_relationship_and_ref(self) -> None:
        repo = self._repo("completion-failure")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        ref_before = self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10")
        with patch.object(LocalTicketProvider, "complete", side_effect=cli.TicketProviderError("close failed")):
            code, _, err = self._invoke_command(repo, "complete")
        self.assertEqual(code, 1)
        self.assertIn("close failed", err)
        self.assertEqual(json.loads((repo / ".ai-dev/workflow.json").read_text())["activeIssueNumber"], 20)
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"), ref_before)

    def test_resume_rejects_inconsistent_canonical_commit_without_mutation(self) -> None:
        repo = self._repo("resume-premature")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        state_path = repo / ".ai-dev" / "workflow.json"
        before_state = state_path.read_bytes()
        self._git(repo, "checkout", "-q", "-b", "unrelated")
        (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        self._git(repo, "add", "unrelated.txt")
        self._git(repo, "commit", "-q", "-m", "unrelated")
        self._git(repo, "checkout", "-q", "main")
        self._git(repo, "merge", "--ff-only", "unrelated")
        self._git(repo, "checkout", "-q", "scratch")
        code, _, err = self._invoke_command(repo, "resume", "10")
        self.assertEqual(code, 1)
        self.assertIn("must equal", err)
        self.assertEqual(state_path.read_bytes(), before_state)

    def test_resumed_a_ref_mismatch_blocks_completion_before_provider_mutation(self) -> None:
        repo = self._repo("cleanup-mismatch")
        self.assertEqual(self._invoke(repo, "20", "--prerequisite-for", "10")[0], 0)
        self.assertEqual(self._invoke_command(repo, "promote", "Publish")[0], 0)
        self.assertEqual(self._invoke_command(repo, "complete")[0], 0)
        self.assertEqual(self._invoke_command(repo, "resume", "10")[0], 0)
        self._git(repo, "update-ref", "refs/ai-dev/suspended/10", self._git(repo, "rev-parse", "main"))
        with patch.object(LocalTicketProvider, "complete", side_effect=AssertionError("provider must not run")):
            code, _, err = self._invoke_command(repo, "complete")
        self.assertEqual(code, 1)
        self.assertIn("suspended ref does not match", err)

    def test_successful_handoff_creates_and_persists_matching_managed_ref(self) -> None:
        repo = self._repo("ref-success")
        head_before = self._git(repo, "rev-parse", "scratch")
        ref_name = "refs/ai-dev/suspended/10"

        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", ref_name), head_before)
        state = json.loads((repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"))
        suspended = state["stackedHandoff"]["suspendedIssue"]
        self.assertEqual(suspended["refName"], ref_name)
        self.assertEqual(suspended["commit"], head_before)

    def test_conflicting_ref_and_commit_mismatch_fail_closed(self) -> None:
        repo = self._repo("ref-conflict")
        ref_name = "refs/ai-dev/suspended/10"
        main_commit = self._git(repo, "rev-parse", "main")
        scratch_commit = self._git(repo, "rev-parse", "scratch")
        self._git(repo, "update-ref", ref_name, main_commit)
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("already points", err)
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", ref_name), main_commit)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "active")

        repo = self._repo("ref-mismatch")
        second_scratch_commit = self._git(repo, "rev-parse", "scratch")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 0)
        state_path = repo / ".ai-dev/workflow.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["stackedHandoff"]["suspendedIssue"]["commit"] = main_commit
        state_path.write_text(json.dumps(state), encoding="utf-8")
        code, _, err = self._invoke(repo, "30", "--prerequisite-for", "20")
        self.assertEqual(code, 1)
        self.assertIn("does not match its persisted commit", err)
        self.assertEqual(self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"), second_scratch_commit)

    def test_ref_is_removed_on_provider_registry_and_state_failures(self) -> None:
        for failure_target, failure in (
            ("provider", cli.TicketProviderError("provider unavailable")),
            ("registry", cli.BlockedWorkflowsError("registry unavailable")),
            ("state", WorkflowStateError("disk full")),
        ):
            with self.subTest(failure_target=failure_target):
                repo = self._repo(f"ref-rollback-{failure_target}")
                patch_target = {
                    "provider": "ai_dev_flow.ticket_providers.LocalTicketProvider.mark_active",
                    "registry": "ai_dev_flow.cli.upsert_blocked_workflow",
                    "state": "ai_dev_flow.cli.save_state",
                }[failure_target]
                with patch(patch_target, side_effect=failure):
                    code, _, _ = self._invoke(repo, "20", "--prerequisite-for", "10")
                self.assertEqual(code, 1)
                self.assertIsNone(
                    self._git(repo, "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10")
                    if subprocess.run(
                        ["git", "-C", str(repo), "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/10"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    ).returncode == 0
                    else None
                )

    def test_malformed_or_missing_ref_metadata_fails_closed(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "malformed fields"):
            normalize_and_validate(
                {
                    "activeIssueNumber": 20,
                    "stackedHandoff": {
                        "relationship": "prerequisite",
                        "prerequisiteForIssueNumber": 10,
                        "inheritedBase": {"commit": "a", "tree": "b"},
                        "suspendedIssue": {
                            "issueNumber": 10,
                            "issueTitle": "A",
                            "ticket": {"provider": "local", "ticketId": "10", "path": ".ai-dev/tickets"},
                            "checkpoint": 1,
                            "commit": "c",
                            "tree": "d",
                            "baseCommit": "e",
                        },
                    },
                },
                context="missing ref",
            )

    def test_ordinary_start_creates_no_suspended_ref(self) -> None:
        repo = self._repo("ordinary-start")
        workflow_path = repo / ".ai-dev/workflow.json"
        workflow_path.write_text(json.dumps({"mainBranch": "main", "scratchBranch": "scratch", "checkpoint": 0}), encoding="utf-8")
        ticket_path = repo / ".ai-dev/tickets/20.json"
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
        ticket["workflowState"] = "inactive"
        ticket_path.write_text(json.dumps(ticket), encoding="utf-8")
        code, _, err = self._invoke(repo, "20")
        self.assertEqual((code, err), (0, ""))
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(repo), "show-ref", "--verify", "--hash", "refs/ai-dev/suspended/20"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).returncode,
            0,
        )

    def test_deactivate_restores_unlabeled_and_backlog_membership(self) -> None:
        repo = self._repo("label-rollback")
        provider = LocalTicketProvider(repo_root=repo, tickets_path=".ai-dev/tickets")
        reference = provider.get("20").reference
        provider.mark_active(reference)
        provider.deactivate(reference, ())
        restored = provider.get("20")
        self.assertEqual(restored.labels, ())
        self.assertEqual(restored.workflow_state, "inactive")

        self._ticket(repo / ".ai-dev/tickets", 20, "Prerequisite B", "inactive", ("backlog", "triage"))
        reference = provider.get("20").reference
        provider.mark_active(reference)
        provider.deactivate(reference, ("backlog", "triage"))
        restored = provider.get("20")
        self.assertEqual(restored.labels, ("backlog", "triage"))
        self.assertEqual(restored.workflow_state, "inactive")

    def test_dirty_and_mismatched_handoffs_do_not_mutate(self) -> None:
        repo = self._repo("rejections")
        before = (self._git(repo, "rev-parse", "HEAD"), (repo / ".ai-dev/workflow.json").read_text())
        (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("repository must be clean", err)
        self.assertEqual(self._git(repo, "rev-parse", "HEAD"), before[0])
        self.assertEqual((repo / ".ai-dev/workflow.json").read_text(), before[1])
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "99")
        self.assertEqual(code, 1)
        self.assertIn("active issue is 10, not 99", err)

    def test_provider_failure_rolls_back_prior_provider_state(self) -> None:
        repo = self._repo("provider-failure")
        with patch.object(LocalTicketProvider, "block", side_effect=cli.TicketProviderError("provider unavailable")):
            code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("provider unavailable", err)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "active")
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/20.json").read_text())["workflowState"], "inactive")

    def test_state_persistence_failure_rolls_back_provider_and_registry(self) -> None:
        repo = self._repo("state-failure")
        with patch("ai_dev_flow.cli.save_state", side_effect=WorkflowStateError("disk full")):
            code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"], "active")
        self.assertEqual(json.loads((repo / ".ai-dev/tickets/20.json").read_text())["workflowState"], "inactive")
        self.assertFalse((repo / ".ai-dev/blocked-workflows.json").exists())

    def test_blocked_registry_failure_rolls_back_provider(self) -> None:
        repo = self._repo("blocked-registry-failure")
        with patch(
            "ai_dev_flow.cli.upsert_blocked_workflow",
            side_effect=cli.BlockedWorkflowsError("registry unavailable"),
        ):
            code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("registry unavailable", err)
        self.assertEqual(
            json.loads((repo / ".ai-dev/tickets/10.json").read_text())["workflowState"],
            "active",
        )
        self.assertEqual(
            json.loads((repo / ".ai-dev/tickets/20.json").read_text())["workflowState"],
            "inactive",
        )
        self.assertFalse((repo / ".ai-dev/blocked-workflows.json").exists())

    def test_malformed_handoff_state_is_rejected(self) -> None:
        with self.assertRaisesRegex(WorkflowStateError, "stackedHandoff relationship"):
            normalize_and_validate({
                "activeIssueNumber": 20,
                "stackedHandoff": {"relationship": "dependency"},
            }, context="test")

    def test_patch_nested_and_blocked_ambiguity_is_rejected(self) -> None:
        repo = self._repo("ambiguity")
        workflow_path = repo / ".ai-dev/workflow.json"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        workflow.pop("activeIssueNumber")
        workflow.pop("activeIssueTitle")
        workflow.pop("ticket")
        workflow["patchDescription"] = "local patch"
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("patch workflows are unsupported", err)

        repo = self._repo("nested")
        workflow = json.loads((repo / ".ai-dev/workflow.json").read_text(encoding="utf-8"))
        nested_commit = self._git(repo, "rev-parse", "HEAD")
        nested_tree = self._git(repo, "rev-parse", "HEAD^{tree}")
        self._git(repo, "update-ref", "refs/ai-dev/suspended/30", nested_commit)
        workflow["stackedHandoff"] = {
            "relationship": "prerequisite",
            "prerequisiteForIssueNumber": 30,
            "inheritedBase": {"commit": nested_commit, "tree": nested_tree},
            "suspendedIssue": {
                "issueNumber": 30,
                "issueTitle": "Older issue",
                "ticket": {"provider": "local", "ticketId": "30", "path": ".ai-dev/tickets"},
                "checkpoint": 1,
                "commit": nested_commit,
                "tree": nested_tree,
                "baseCommit": nested_commit,
                "refName": "refs/ai-dev/suspended/30",
            },
        }
        workflow_path = repo / ".ai-dev/workflow.json"
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("nested handoffs are unsupported", err)

        repo = self._repo("blocked")
        blocked_path = repo / ".ai-dev/blocked-workflows.json"
        blocked_path.write_text(json.dumps({"blockedWorkflows": [{
            "issueNumber": 20,
            "issueTitle": "Prerequisite B",
            "reason": "already waiting",
            "blockedAt": "2026-08-20T00:00:00Z",
        }]}), encoding="utf-8")
        code, _, err = self._invoke(repo, "20", "--prerequisite-for", "10")
        self.assertEqual(code, 1)
        self.assertIn("issue 20 is already blocked", err)


if __name__ == "__main__":
    unittest.main()
