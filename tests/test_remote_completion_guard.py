from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.promotion_sync import PromotionSyncRecord, load_promotion_sync_record, save_promotion_sync_record
from ai_dev_flow.tickets import Ticket, TicketReference
from ai_dev_flow.workflow_state import WorkflowState, WorkflowStateError, load_state


class RemoteCompletionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _build_repo(self, name: str, *, tracked_upstream: bool = True) -> tuple[Path, Path | None]:
        remote: Path | None = None
        if tracked_upstream:
            remote = self.tmp_path / f"{name}-remote.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

        repo = self.tmp_path / name
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Remote Completion Tests")
        self._git(repo, "config", "user.email", "remote-completion@example.com")
        (repo / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore", "tracked.txt")
        self._git(repo, "commit", "--quiet", "-m", "initial")
        self._git(repo, "branch", "-M", "main")
        if remote is not None:
            self._git(repo, "remote", "add", "shared", str(remote))
            self._git(repo, "push", "--quiet", "-u", "shared", "main")
            self._git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(repo, "checkout", "--quiet", "-b", "scratch")
        (repo / ".ai-dev").mkdir()
        (repo / ".ai-dev" / "workflow.json").write_text(
            json.dumps(
                {
                    "patchDescription": "Remote completion",
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                }
            ),
            encoding="utf-8",
        )
        return repo, remote

    def _invoke(self, repo: Path, command: str, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")
        os.environ["FLOW_COMMAND_NAME"] = f"flow-{command}"
        sys.argv = [f"flow-{command}", cli._DIRECT_FLOW_ROUTE_TOKEN, command, *arguments]
        os.chdir(repo)
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
            if previous_command_name is None:
                os.environ.pop("FLOW_COMMAND_NAME", None)
            else:
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
        return code, stdout.getvalue(), stderr.getvalue()

    def _save_record(
        self,
        repo: Path,
        *,
        status: str = "synchronized",
        promoted_main_commit: str | None = None,
        patch_description: str = "Remote completion",
        remote_name: str = "shared",
        upstream_ref: str = "refs/heads/main",
    ) -> None:
        save_promotion_sync_record(
            repo,
            PromotionSyncRecord(
                status=status,
                main_branch="main",
                scratch_branch="scratch",
                promoted_main_commit=promoted_main_commit or self._git(repo, "rev-parse", "main"),
                remote_name=remote_name,
                upstream_ref=upstream_ref,
                patch_description=patch_description,
            ),
        )

    def test_matching_synchronized_record_allows_completion_and_clears_state(self) -> None:
        repo, _ = self._build_repo("synchronized")
        self._save_record(repo)

        code, out, err = self._invoke(repo, "complete", [])

        self.assertEqual(code, 0, err)
        self.assertIn("Completed patch", out)
        self.assertIsNone(load_promotion_sync_record(repo))
        self.assertIsNone(load_state(repo / ".ai-dev" / "workflow.json").patch_description)

    def test_pending_missing_malformed_and_mismatched_records_block_completion(self) -> None:
        cases = ("pending", "missing", "malformed", "sha", "identity", "upstream")
        for case in cases:
            with self.subTest(case=case):
                repo, _ = self._build_repo(case)
                if case == "pending":
                    self._save_record(repo, status="pending")
                elif case == "malformed":
                    path = repo / ".ai-dev" / "promotion-sync.json"
                    path.write_text("{not json", encoding="utf-8")
                elif case == "sha":
                    self._save_record(repo, promoted_main_commit="a" * 40)
                elif case == "identity":
                    self._save_record(repo, patch_description="Different workflow")
                elif case == "upstream":
                    self._save_record(repo, remote_name="other")

                code, _, err = self._invoke(repo, "complete", [])

                self.assertEqual(code, 1)
                self.assertIn("has not been synchronized", err)
                self.assertEqual(load_state(repo / ".ai-dev" / "workflow.json").patch_description, "Remote completion")

    def test_no_upstream_retains_local_completion_behavior(self) -> None:
        repo, _ = self._build_repo("local-only", tracked_upstream=False)

        code, out, err = self._invoke(repo, "complete", [])

        self.assertEqual(code, 0, err)
        self.assertIn("Completed patch", out)

    def test_remote_advance_after_synchronization_does_not_block_completion(self) -> None:
        repo, remote = self._build_repo("remote-advance")
        assert remote is not None
        self._save_record(repo)
        writer = self.tmp_path / "writer"
        subprocess.run(["git", "clone", "--quiet", str(remote), str(writer)], check=True)
        self._git(writer, "config", "user.name", "Remote Writer")
        self._git(writer, "config", "user.email", "remote-writer@example.com")
        self._git(writer, "checkout", "--quiet", "main")
        (writer / "remote.txt").write_text("advance\n", encoding="utf-8")
        self._git(writer, "add", "remote.txt")
        self._git(writer, "commit", "--quiet", "-m", "advance")
        self._git(writer, "push", "--quiet", "origin", "main")

        code, _, err = self._invoke(repo, "complete", [])

        self.assertEqual(code, 0, err)

    def test_failed_completion_persistence_keeps_sync_record(self) -> None:
        repo, _ = self._build_repo("completion-persist-failure")
        self._save_record(repo)

        with patch("ai_dev_flow.cli.save_state", side_effect=WorkflowStateError("simulated state failure")):
            code, _, err = self._invoke(repo, "complete", [])

        self.assertEqual(code, 1)
        self.assertIn("workflow state could not be cleared", err)
        record = load_promotion_sync_record(repo)
        assert record is not None
        self.assertEqual(record.status, "synchronized")

    def test_start_patch_and_reset_clear_stale_sync_state_after_success(self) -> None:
        repo, _ = self._build_repo("cleanup", tracked_upstream=False)
        self._save_record(repo, remote_name="shared")

        code, _, err = self._invoke(repo, "reset", [])
        self.assertEqual(code, 0, err)
        self.assertIsNone(load_promotion_sync_record(repo))

        code, _, err = self._invoke(repo, "complete", [])
        self.assertEqual(code, 0, err)

        self._save_record(repo, remote_name="shared")
        code, _, err = self._invoke(repo, "patch", ["New patch"])
        self.assertEqual(code, 0, err)
        self.assertIsNone(load_promotion_sync_record(repo))

    def test_start_clears_stale_sync_state_after_successful_activation(self) -> None:
        repo, _ = self._build_repo("start-cleanup", tracked_upstream=False)
        code, _, err = self._invoke(repo, "complete", [])
        self.assertEqual(code, 0, err)
        self._save_record(repo, remote_name="shared")
        reference = TicketReference(provider="local", ticket_id="42", url="https://example.test/issues/42")
        ticket = Ticket(
            reference=reference,
            title="Fresh issue",
            body=None,
            acceptance_criteria=(),
            labels=(),
            lifecycle_state="open",
            workflow_state="inactive",
            block_reason=None,
            created_at=None,
            updated_at=None,
            closed_at=None,
        )

        class Provider:
            def get(self, ticket_id: str) -> Ticket:
                self.ticket_id = ticket_id
                return ticket

            def mark_active(self, active_reference: TicketReference) -> Ticket:
                self.active_reference = active_reference
                return Ticket(
                    reference=ticket.reference,
                    title=ticket.title,
                    body=ticket.body,
                    acceptance_criteria=ticket.acceptance_criteria,
                    labels=ticket.labels,
                    lifecycle_state="open",
                    workflow_state="active",
                    block_reason=None,
                    created_at=None,
                    updated_at=None,
                    closed_at=None,
                )

        with patch("ai_dev_flow.cli._resolve_ticket_provider_for_repo_root", return_value=Provider()):
            code, _, err = self._invoke(repo, "start", ["42"])

        self.assertEqual(code, 0, err)
        self.assertIsNone(load_promotion_sync_record(repo))


if __name__ == "__main__":
    unittest.main()