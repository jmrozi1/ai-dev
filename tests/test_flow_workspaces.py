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
from ai_dev_flow import workspaces
from ai_dev_flow.tickets import TicketReference


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()

    def tearDown(self) -> None:
        for root in sorted(self.tmp_path.glob("*")):
            if (root / ".git").exists():
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "prune"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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

    def _write_ticket(
        self,
        repo_root: Path,
        ticket_id: str,
        *,
        title: str = "Ticket",
        lifecycle_state: str = "open",
        workflow_state: str = "inactive",
    ) -> None:
        path = repo_root / ".ai-dev" / "tickets" / f"{ticket_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "reference": {
                        "provider": "local",
                        "ticketId": ticket_id,
                        "path": ".ai-dev/tickets",
                    },
                    "title": title,
                    "lifecycleState": lifecycle_state,
                    "workflowState": workflow_state,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_config(self, repo_root: Path, *, out: str | None = None) -> None:
        payload: dict = {
            "tickets": {"provider": "local", "path": ".ai-dev/tickets"},
        }
        if out is not None:
            payload["out"] = out
        config_path = repo_root / ".ai-dev" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_state(
        self,
        repo_root: Path,
        *,
        main_branch: str = "main",
        scratch_branch: str = "scratch",
        checkpoint: int = 0,
    ) -> None:
        state_path = repo_root / ".ai-dev" / "workflow.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "mainBranch": main_branch,
                    "scratchBranch": scratch_branch,
                    "checkpoint": checkpoint,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _read_state(self, repo_root: Path) -> dict:
        return json.loads((repo_root / ".ai-dev" / "workflow.json").read_text(encoding="utf-8"))

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Workspace Tests")
        self._run_git(repo_root, "config", "user.email", "workspace-tests@example.com")
        (repo_root / ".gitignore").write_text(".ai-dev/\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_config(repo_root)
        self._write_state(repo_root)
        return repo_root

    def _invoke(self, cwd: Path, command: str, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_name = "FLOW_COMMAND_NAME" in os.environ
        previous_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()
        argv0 = f"flow-{command}"
        os.environ["FLOW_COMMAND_NAME"] = argv0
        sys.argv = [argv0, cli._DIRECT_FLOW_ROUTE_TOKEN, command, *arguments]
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
            if had_name:
                assert previous_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)
        return code, stdout.getvalue(), stderr.getvalue()

    def _local_reference(self, ticket_id: str) -> TicketReference:
        return TicketReference(provider="local", ticket_id=ticket_id)

    def _claim_path(self, repo_root: Path, ticket_id: str) -> Path:
        key = workspaces.canonical_ticket_key(self._local_reference(ticket_id))
        return workspaces.claim_path(repo_root, key)


class IdentityAndNamingTests(WorkspaceTestBase):
    def test_claim_filename_is_the_complete_sha256_digest(self) -> None:
        key = workspaces.canonical_ticket_key(
            TicketReference(provider="github", ticket_id="50", repository="jmrozi1/ai-dev")
        )
        self.assertEqual(key, "github:jmrozi1/ai-dev#50")
        filename = workspaces.claim_filename(key)
        self.assertEqual(
            filename,
            "7aec27cc5e127097f2028a2368fab39e48e56d5710d8fc07bfaebc35222d31d9.json",
        )
        self.assertEqual(len(filename), 64 + len(".json"))

    def test_branch_names_are_deterministic_and_provider_aware(self) -> None:
        github = TicketReference(provider="github", ticket_id="50", repository="jmrozi1/ai-dev")
        self.assertEqual(
            workspaces.workspace_branch_name(github),
            "flow/github/jmrozi1/ai-dev/50",
        )
        local = TicketReference(provider="local", ticket_id="50")
        self.assertTrue(workspaces.workspace_branch_name(local).startswith("flow/local/50-"))

    def test_worktree_identity_and_common_dir_resolve_per_worktree(self) -> None:
        repo_root = self._init_repo("repo-identity")
        linked = self.tmp_path / "linked"
        self._run_git(repo_root, "worktree", "add", "-q", "-b", "linked-branch", str(linked), "main")

        self.assertEqual(
            workspaces.git_common_dir(repo_root),
            workspaces.git_common_dir(linked),
        )
        self.assertIsNone(workspaces.worktree_id_for_repo_root(repo_root))
        self.assertEqual(workspaces.worktree_id_for_repo_root(linked), "linked")

        entries = {entry.worktree_id: entry for entry in workspaces.list_worktrees(repo_root)}
        self.assertIn("linked", entries)
        self.assertEqual(entries["linked"].branch, "linked-branch")


class DuplicateClaimTests(WorkspaceTestBase):
    def test_second_workspace_for_same_ticket_is_refused(self) -> None:
        repo_root = self._init_repo("repo-duplicate")
        self._write_ticket(repo_root, "7")

        code, stdout, stderr = self._invoke(repo_root, "workspace", "add", "7")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Added workspace for issue 7", stdout)

        code, stdout, stderr = self._invoke(
            repo_root, "workspace", "add", "7", str(self.tmp_path / "second")
        )
        self.assertEqual(code, 1)
        self.assertIn("already active in workspace", stderr)
        self.assertFalse((self.tmp_path / "second").exists())

    def test_flow_start_refuses_a_ticket_claimed_by_another_workspace(self) -> None:
        repo_root = self._init_repo("repo-start-conflict")
        self._write_ticket(repo_root, "11")

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "11")
        self.assertEqual(code, 0, msg=stderr)

        # A linked worktree shares the registry, which is what makes the second
        # activation attempt visible to the first.
        linked = self.tmp_path / "conflict-linked"
        self._run_git(repo_root, "worktree", "add", "-q", "-b", "other", str(linked), "main")
        self._write_config(linked)
        self._write_state(linked, scratch_branch="other")
        self._write_ticket(linked, "11")

        code, stdout, stderr = self._invoke(linked, "start", "11")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("already active in workspace", stderr)

    def test_flow_start_claims_before_touching_git_or_provider(self) -> None:
        repo_root = self._init_repo("repo-start-claim")
        self._write_ticket(repo_root, "3")

        code, stdout, stderr = self._invoke(repo_root, "start", "3")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Started issue 3", stdout)
        self.assertTrue(self._claim_path(repo_root, "3").exists())

        record = workspaces.read_claim(
            repo_root, workspaces.canonical_ticket_key(self._local_reference("3"))
        )
        self.assertEqual(record.status, workspaces.CLAIM_STATUS_ACTIVE)
        self.assertEqual(record.worktree_id, workspaces.PRIMARY_WORKTREE_ID)
        self.assertEqual(len(record.token), 32)


class ReservationRollbackTests(WorkspaceTestBase):
    def test_failed_worktree_creation_releases_the_reservation(self) -> None:
        repo_root = self._init_repo("repo-rollback")
        self._write_ticket(repo_root, "9")

        claim_file = self._claim_path(repo_root, "9")
        target = self.tmp_path / "rollback-target"

        original = cli.add_worktree

        def failing_add_worktree(*args, **kwargs):
            raise cli.RepositoryError("simulated worktree failure")

        cli.add_worktree = failing_add_worktree
        try:
            code, stdout, stderr = self._invoke(
                repo_root, "workspace", "add", "9", str(target)
            )
        finally:
            cli.add_worktree = original

        self.assertEqual(code, 1)
        self.assertIn("simulated worktree failure", stderr)
        self.assertFalse(claim_file.exists(), "reservation must be released on rollback")
        self.assertFalse(target.exists())
        branches = self._run_git(repo_root, "branch", "--list", "flow/local/9-*")
        self.assertEqual(branches, "")

    def test_preconditions_refuse_before_any_reservation_exists(self) -> None:
        repo_root = self._init_repo("repo-precondition")
        self._write_ticket(repo_root, "12")
        occupied = self.tmp_path / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("existing\n", encoding="utf-8")

        code, _, stderr = self._invoke(
            repo_root, "workspace", "add", "12", str(occupied)
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists and is not empty", stderr)
        self.assertFalse(self._claim_path(repo_root, "12").exists())
        self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "existing\n")

    def test_nested_workspace_path_is_refused(self) -> None:
        repo_root = self._init_repo("repo-nested")
        self._write_ticket(repo_root, "13")

        code, _, stderr = self._invoke(
            repo_root, "workspace", "add", "13", str(repo_root / ".ai-dev" / "workspaces" / "13")
        )
        self.assertEqual(code, 1)
        self.assertIn("is inside the existing worktree", stderr)
        self.assertFalse(self._claim_path(repo_root, "13").exists())


class AdoptionTests(WorkspaceTestBase):
    def _manual_workspace(self, repo_root: Path, name: str, branch: str) -> Path:
        workspace = self.tmp_path / name
        self._run_git(repo_root, "worktree", "add", "-q", "-b", branch, str(workspace), "main")
        self._write_config(workspace)
        self._write_state(workspace, scratch_branch=branch)
        self._write_ticket(workspace, "21")
        return workspace

    def test_adopt_preserves_commits_index_and_worktree(self) -> None:
        repo_root = self._init_repo("repo-adopt")
        workspace = self._manual_workspace(repo_root, "adopt-ws", "flow/manual/21")

        (workspace / "one.txt").write_text("one\n", encoding="utf-8")
        self._run_git(workspace, "add", "one.txt")
        self._run_git(workspace, "commit", "-q", "-m", "1")
        (workspace / "two.txt").write_text("two\n", encoding="utf-8")
        self._run_git(workspace, "add", "two.txt")
        self._run_git(workspace, "commit", "-q", "-m", "2")

        (workspace / "staged.txt").write_text("staged\n", encoding="utf-8")
        self._run_git(workspace, "add", "staged.txt")
        (workspace / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        head_before = self._run_git(workspace, "rev-parse", "HEAD")
        status_before = self._run_git(workspace, "status", "--porcelain")

        code, stdout, stderr = self._invoke(workspace, "workspace", "adopt", "21")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Adopted issue 21", stdout)
        self.assertIn("checkpoint: 2", stdout)

        self.assertEqual(self._run_git(workspace, "rev-parse", "HEAD"), head_before)
        self.assertEqual(self._run_git(workspace, "status", "--porcelain"), status_before)

        state = self._read_state(workspace)
        self.assertEqual(state["activeIssueNumber"], 21)
        self.assertEqual(state["scratchBranch"], "flow/manual/21")
        self.assertEqual(state["checkpoint"], 2)
        self.assertTrue(self._claim_path(repo_root, "21").exists())

    def test_adopt_refuses_when_out_lives_in_another_worktree(self) -> None:
        repo_root = self._init_repo("repo-adopt-foreign-out")
        workspace = self._manual_workspace(repo_root, "foreign-out-ws", "flow/manual/21b")
        self._write_config(workspace, out=str(repo_root / "out.txt"))

        code, stdout, stderr = self._invoke(workspace, "workspace", "adopt", "21")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("is inside another worktree", stderr)
        self.assertFalse(self._claim_path(repo_root, "21").exists())

    def test_adopt_refuses_when_the_workspace_already_has_a_workflow(self) -> None:
        repo_root = self._init_repo("repo-adopt-active")
        workspace = self._manual_workspace(repo_root, "active-ws", "flow/manual/21c")

        code, _, stderr = self._invoke(workspace, "workspace", "adopt", "21")
        self.assertEqual(code, 0, msg=stderr)

        self._write_ticket(workspace, "22")
        code, _, stderr = self._invoke(workspace, "workspace", "adopt", "22")
        self.assertEqual(code, 1)
        self.assertIn("already has an active workflow", stderr)


class StaleClaimTests(WorkspaceTestBase):
    def test_prune_removes_claims_whose_worktree_is_gone(self) -> None:
        repo_root = self._init_repo("repo-stale")
        self._write_ticket(repo_root, "31")

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "31")
        self.assertEqual(code, 0, msg=stderr)
        claim_file = self._claim_path(repo_root, "31")
        self.assertTrue(claim_file.exists())

        workspace = self.tmp_path / "repo-stale-issue-31"
        self.assertTrue(workspace.exists())
        subprocess.run(
            ["rm", "-rf", str(workspace)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._run_git(repo_root, "worktree", "prune")

        code, stdout, stderr = self._invoke(repo_root, "workspace", "prune")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Removed stale claim", stdout)
        self.assertFalse(claim_file.exists())

    def test_missing_recorded_path_alone_does_not_make_a_claim_stale(self) -> None:
        repo_root = self._init_repo("repo-moved")
        self._write_ticket(repo_root, "32")

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "32")
        self.assertEqual(code, 0, msg=stderr)

        claim_file = self._claim_path(repo_root, "32")
        payload = json.loads(claim_file.read_text(encoding="utf-8"))
        payload["intendedPath"] = str(self.tmp_path / "path-that-never-existed")
        claim_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        record = workspaces.read_claim_file(claim_file)
        status = workspaces.evaluate_claim(repo_root, record, claim_file)
        self.assertEqual(status.state, workspaces.CLAIM_STATUS_ACTIVE)
        self.assertEqual(
            Path(os.path.abspath(str(status.live_path))),
            Path(os.path.abspath(str(self.tmp_path / "repo-moved-issue-32"))),
        )

        code, stdout, stderr = self._invoke(repo_root, "workspace", "prune")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("No stale claims.", stdout)
        self.assertTrue(claim_file.exists())

    def test_unreadable_claim_is_occupied_not_stale(self) -> None:
        repo_root = self._init_repo("repo-malformed")
        self._write_ticket(repo_root, "33")

        claim_file = self._claim_path(repo_root, "33")
        claim_file.parent.mkdir(parents=True, exist_ok=True)
        claim_file.write_text("{ this is not json", encoding="utf-8")

        code, stdout, stderr = self._invoke(repo_root, "workspace", "prune")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Kept unreadable claim", stdout)
        self.assertTrue(claim_file.exists())

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "33")
        self.assertEqual(code, 1)
        self.assertIn("treated as occupied", stderr)

        code, stdout, stderr = self._invoke(
            repo_root, "workspace", "prune", "--claim", claim_file.stem
        )
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Removed claim record", stdout)
        self.assertFalse(claim_file.exists())

    def test_liveness_refuses_automated_recovery_for_foreign_hosts(self) -> None:
        self.assertIsNone(workspaces.process_is_absent(1234, "some-other-host"))
        self.assertIsNone(workspaces.process_is_absent(None, None))
        self.assertIs(workspaces.process_is_absent(os.getpid(), None), None)
        if os.name == "posix":
            import socket

            self.assertIs(
                workspaces.process_is_absent(os.getpid(), socket.gethostname()), False
            )


class ConfigurationRelocationTests(WorkspaceTestBase):
    def test_out_inside_the_source_workspace_is_relocated(self) -> None:
        repo_root = self._init_repo("repo-relocate")
        self._write_config(repo_root, out=str(repo_root / "out.txt"))
        self._write_ticket(repo_root, "41")

        code, stdout, stderr = self._invoke(repo_root, "workspace", "add", "41")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("out relocated to", stdout)

        workspace = self.tmp_path / "repo-relocate-issue-41"
        seeded = json.loads((workspace / ".ai-dev" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(seeded["out"], str(workspace / "out.txt"))

    def test_external_out_is_preserved(self) -> None:
        repo_root = self._init_repo("repo-external-out")
        external = self.tmp_path / "external" / "out.txt"
        self._write_config(repo_root, out=str(external))
        self._write_ticket(repo_root, "42")

        code, stdout, stderr = self._invoke(repo_root, "workspace", "add", "42")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("preserved", stdout)

        workspace = self.tmp_path / "repo-external-out-issue-42"
        seeded = json.loads((workspace / ".ai-dev" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(seeded["out"], str(external))

    def test_relocation_helper_preserves_nested_relative_layout(self) -> None:
        source = Path("/srv/project")
        target = Path("/srv/project-issue-9")
        relocation = workspaces.relocate_config_for_workspace(
            {"out": "/srv/project/artifacts/out.txt"},
            source_root=source,
            target_root=target,
        )
        self.assertTrue(relocation.relocated)
        self.assertEqual(
            relocation.config["out"],
            str(Path("/srv/project-issue-9/artifacts/out.txt")),
        )


class WorkspaceLifecycleTests(WorkspaceTestBase):
    def test_two_tickets_stay_independently_active(self) -> None:
        repo_root = self._init_repo("repo-concurrent")
        self._write_ticket(repo_root, "51", title="Ticket A")
        self._write_ticket(repo_root, "52", title="Ticket B")

        code, _, stderr = self._invoke(repo_root, "start", "51")
        self.assertEqual(code, 0, msg=stderr)

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "52")
        self.assertEqual(code, 0, msg=stderr)
        workspace = self.tmp_path / "repo-concurrent-issue-52"

        primary_state = self._read_state(repo_root)
        workspace_state = self._read_state(workspace)
        self.assertEqual(primary_state["activeIssueNumber"], 51)
        self.assertEqual(primary_state["scratchBranch"], "scratch")
        self.assertEqual(workspace_state["activeIssueNumber"], 52)
        self.assertTrue(workspace_state["scratchBranch"].startswith("flow/local/52-"))

        code, stdout, stderr = self._invoke(repo_root, "status")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Issue 51", stdout)
        self.assertNotIn("52", stdout)

        code, stdout, stderr = self._invoke(workspace, "status")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Issue 52", stdout)
        self.assertNotIn("51", stdout)

    def test_checkpoints_do_not_leak_between_workspaces(self) -> None:
        repo_root = self._init_repo("repo-checkpoints")
        self._write_ticket(repo_root, "61")
        self._write_ticket(repo_root, "62")

        code, _, stderr = self._invoke(repo_root, "start", "61")
        self.assertEqual(code, 0, msg=stderr)
        code, _, stderr = self._invoke(repo_root, "workspace", "add", "62")
        self.assertEqual(code, 0, msg=stderr)
        workspace = self.tmp_path / "repo-checkpoints-issue-62"

        (repo_root / "a.txt").write_text("a\n", encoding="utf-8")
        code, _, stderr = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0, msg=stderr)

        (workspace / "b.txt").write_text("b\n", encoding="utf-8")
        code, _, stderr = self._invoke(workspace, "commit")
        self.assertEqual(code, 0, msg=stderr)
        (workspace / "c.txt").write_text("c\n", encoding="utf-8")
        code, _, stderr = self._invoke(workspace, "commit")
        self.assertEqual(code, 0, msg=stderr)

        self.assertEqual(self._read_state(repo_root)["checkpoint"], 1)
        self.assertEqual(self._read_state(workspace)["checkpoint"], 2)

    def test_remove_refuses_active_or_dirty_workspaces_then_succeeds(self) -> None:
        repo_root = self._init_repo("repo-remove")
        self._write_ticket(repo_root, "71")

        code, _, stderr = self._invoke(repo_root, "workspace", "add", "71")
        self.assertEqual(code, 0, msg=stderr)
        workspace = self.tmp_path / "repo-remove-issue-71"
        claim_file = self._claim_path(repo_root, "71")

        code, _, stderr = self._invoke(repo_root, "workspace", "remove", str(workspace))
        self.assertEqual(code, 1)
        self.assertIn("still has an active workflow", stderr)
        self.assertTrue(workspace.exists())

        code, _, stderr = self._invoke(workspace, "abandon")
        self.assertEqual(code, 0, msg=stderr)
        self.assertFalse(claim_file.exists(), "abandon must release the claim")

        (workspace / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        code, _, stderr = self._invoke(repo_root, "workspace", "remove", str(workspace))
        self.assertEqual(code, 1)
        self.assertIn("working tree is not clean", stderr)

        (workspace / "dirty.txt").unlink()
        code, stdout, stderr = self._invoke(repo_root, "workspace", "remove", str(workspace))
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Removed workspace", stdout)
        self.assertFalse(workspace.exists())

    def test_remove_refuses_the_primary_worktree(self) -> None:
        repo_root = self._init_repo("repo-remove-primary")
        code, _, stderr = self._invoke(repo_root, "workspace", "remove", str(repo_root))
        self.assertEqual(code, 1)
        self.assertIn("primary worktree is not removable", stderr)

    def test_list_reports_each_claim_and_marks_the_current_workspace(self) -> None:
        repo_root = self._init_repo("repo-list")
        self._write_ticket(repo_root, "81")
        self._write_ticket(repo_root, "82")

        code, _, stderr = self._invoke(repo_root, "start", "81")
        self.assertEqual(code, 0, msg=stderr)
        code, _, stderr = self._invoke(repo_root, "workspace", "add", "82")
        self.assertEqual(code, 0, msg=stderr)

        code, stdout, stderr = self._invoke(repo_root, "workspace", "list")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("local:81", stdout)
        self.assertIn("local:82", stdout)
        self.assertIn("(current)", stdout)


class SingleWorkspaceRegressionTests(WorkspaceTestBase):
    def test_ordinary_single_workspace_lifecycle_is_unchanged(self) -> None:
        repo_root = self._init_repo("repo-single")
        self._write_ticket(repo_root, "91")

        code, stdout, stderr = self._invoke(repo_root, "start", "91")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Started issue 91", stdout)
        self.assertIn("scratchBranch: scratch", stdout)

        state = self._read_state(repo_root)
        self.assertEqual(state["mainBranch"], "main")
        self.assertEqual(state["scratchBranch"], "scratch")
        self.assertEqual(state["checkpoint"], 0)
        self.assertEqual(state["activeIssueNumber"], 91)

        (repo_root / "work.txt").write_text("work\n", encoding="utf-8")
        code, stdout, stderr = self._invoke(repo_root, "commit")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Created checkpoint 1", stdout)
        self.assertEqual(
            self._run_git(repo_root, "log", "-1", "--format=%s"),
            "1",
        )

        code, stdout, stderr = self._invoke(repo_root, "reset")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Reset scratch to main", stdout)

        code, stdout, stderr = self._invoke(repo_root, "abandon")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Abandoned local workflow", stdout)

    def test_no_registry_directory_is_created_by_read_only_commands(self) -> None:
        repo_root = self._init_repo("repo-no-registry")
        registry = workspaces.registry_directory(repo_root)

        code, _, stderr = self._invoke(repo_root, "status")
        self.assertEqual(code, 0, msg=stderr)
        self.assertFalse(registry.exists())

        code, stdout, stderr = self._invoke(repo_root, "workspace", "list")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("No workspace claims.", stdout)
        self.assertFalse(registry.exists())

    def test_complete_releases_the_workspace_claim(self) -> None:
        repo_root = self._init_repo("repo-complete")
        self._write_ticket(repo_root, "92")

        code, _, stderr = self._invoke(repo_root, "start", "92")
        self.assertEqual(code, 0, msg=stderr)
        claim_file = self._claim_path(repo_root, "92")
        self.assertTrue(claim_file.exists())

        code, stdout, stderr = self._invoke(repo_root, "complete")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Completed issue 92", stdout)
        self.assertFalse(claim_file.exists())

        # The ticket can be claimed again once the workflow ends.
        self._write_ticket(repo_root, "92", workflow_state="inactive")
        code, _, stderr = self._invoke(repo_root, "start", "92")
        self.assertEqual(code, 0, msg=stderr)


class BlockedClaimTests(WorkspaceTestBase):
    def test_prune_never_removes_a_live_blocked_claim(self) -> None:
        repo_root = self._init_repo("repo-blocked-claim")
        self._write_ticket(repo_root, "101")

        code, _, stderr = self._invoke(repo_root, "start", "101")
        self.assertEqual(code, 0, msg=stderr)
        claim_file = self._claim_path(repo_root, "101")
        self.assertTrue(claim_file.exists())

        code, _, stderr = self._invoke(repo_root, "block", "waiting on review")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIsNone(self._read_state(repo_root).get("activeIssueNumber"))
        self.assertTrue(claim_file.exists(), "block must keep the ticket claim")

        # Bulk prune leaves it alone because it is live, not stale.
        code, stdout, stderr = self._invoke(repo_root, "workspace", "prune")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("No stale claims.", stdout)
        self.assertTrue(claim_file.exists())

        # Targeted prune must refuse it and point at workspace-local recovery.
        code, stdout, stderr = self._invoke(
            repo_root, "workspace", "prune", "--claim", "101"
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("is live", stderr)
        self.assertIn("resume 101", stderr)
        self.assertIn("abandon", stderr)
        self.assertTrue(claim_file.exists())

    def test_blocked_claim_recovers_through_resume_in_its_own_workspace(self) -> None:
        repo_root = self._init_repo("repo-blocked-resume")
        self._write_ticket(repo_root, "102")

        code, _, stderr = self._invoke(repo_root, "start", "102")
        self.assertEqual(code, 0, msg=stderr)
        code, _, stderr = self._invoke(repo_root, "block", "paused")
        self.assertEqual(code, 0, msg=stderr)
        claim_file = self._claim_path(repo_root, "102")
        self.assertTrue(claim_file.exists())

        code, stdout, stderr = self._invoke(repo_root, "resume", "102")
        self.assertEqual(code, 0, msg=stderr)
        self.assertEqual(self._read_state(repo_root)["activeIssueNumber"], 102)
        self.assertTrue(claim_file.exists())

        code, _, stderr = self._invoke(repo_root, "abandon")
        self.assertEqual(code, 0, msg=stderr)
        self.assertFalse(claim_file.exists())

    def test_another_workspace_cannot_start_a_blocked_ticket(self) -> None:
        repo_root = self._init_repo("repo-blocked-cross")
        self._write_ticket(repo_root, "103")

        code, _, stderr = self._invoke(repo_root, "start", "103")
        self.assertEqual(code, 0, msg=stderr)
        code, _, stderr = self._invoke(repo_root, "block", "paused")
        self.assertEqual(code, 0, msg=stderr)

        linked = self.tmp_path / "blocked-cross-linked"
        self._run_git(repo_root, "worktree", "add", "-q", "-b", "cross", str(linked), "main")
        self._write_config(linked)
        self._write_state(linked, scratch_branch="cross")
        self._write_ticket(linked, "103")

        code, _, stderr = self._invoke(linked, "start", "103")
        self.assertEqual(code, 1)
        self.assertIn("already active in workspace", stderr)


class ClaimAuthorizationTests(WorkspaceTestBase):
    def _linked_workspace(self, repo_root: Path, name: str, branch: str) -> Path:
        workspace = self.tmp_path / name
        self._run_git(repo_root, "worktree", "add", "-q", "-b", branch, str(workspace), "main")
        self._write_config(workspace)
        self._write_state(workspace, scratch_branch=branch)
        return workspace

    def test_another_worktree_cannot_release_a_claim_even_knowing_its_token(self) -> None:
        repo_root = self._init_repo("repo-auth-token")
        self._write_ticket(repo_root, "111")

        code, _, stderr = self._invoke(repo_root, "start", "111")
        self.assertEqual(code, 0, msg=stderr)

        key = workspaces.canonical_ticket_key(self._local_reference("111"))
        record = workspaces.read_claim(repo_root, key)
        self.assertEqual(record.worktree_id, workspaces.PRIMARY_WORKTREE_ID)

        other = self._linked_workspace(repo_root, "auth-other", "auth-other")
        other_id = workspaces.effective_worktree_id(other)
        self.assertNotEqual(other_id, record.worktree_id)

        # The token is known and correct; ownership still refuses the release.
        with self.assertRaises(workspaces.WorkspaceError) as caught:
            workspaces.release_claim(
                other,
                key=key,
                token=record.token,
                worktree_id=other_id,
            )
        self.assertIn("owned by workspace", str(caught.exception))
        self.assertTrue(self._claim_path(repo_root, "111").exists())

        # An unidentified caller is refused for the same reason.
        with self.assertRaises(workspaces.WorkspaceError):
            workspaces.release_claim(repo_root, key=key, token=record.token)
        self.assertTrue(self._claim_path(repo_root, "111").exists())

    def test_a_replaced_record_generation_blocks_release_by_a_stale_token(self) -> None:
        repo_root = self._init_repo("repo-auth-generation")
        self._write_ticket(repo_root, "112")

        code, _, stderr = self._invoke(repo_root, "start", "112")
        self.assertEqual(code, 0, msg=stderr)

        key = workspaces.canonical_ticket_key(self._local_reference("112"))
        claim_file = self._claim_path(repo_root, "112")
        stale_record = workspaces.read_claim(repo_root, key)

        # A new generation of the same claim, same owner, different token.
        payload = json.loads(claim_file.read_text(encoding="utf-8"))
        payload["token"] = "f" * 32
        claim_file.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        with self.assertRaises(workspaces.WorkspaceError) as caught:
            workspaces.release_claim(
                repo_root,
                key=key,
                token=stale_record.token,
                worktree_id=stale_record.worktree_id,
            )
        self.assertIn("replaced since it was read", str(caught.exception))
        self.assertTrue(claim_file.exists())

        # The current generation releases normally.
        self.assertTrue(
            workspaces.release_claim(
                repo_root,
                key=key,
                token="f" * 32,
                worktree_id=stale_record.worktree_id,
            )
        )
        self.assertFalse(claim_file.exists())

    def test_owning_workspace_reacquires_its_own_claim_idempotently(self) -> None:
        repo_root = self._init_repo("repo-auth-reentrant")
        self._write_ticket(repo_root, "113")

        code, _, stderr = self._invoke(repo_root, "start", "113")
        self.assertEqual(code, 0, msg=stderr)
        key = workspaces.canonical_ticket_key(self._local_reference("113"))
        first = workspaces.read_claim(repo_root, key)

        again = workspaces.acquire_active_claim(
            repo_root,
            reference=self._local_reference("113"),
            worktree_id=workspaces.worktree_id_for_repo_root(repo_root),
            workspace_path=repo_root,
            branch="scratch",
        )
        self.assertEqual(again.token, first.token)


class AddFailurePhaseTests(WorkspaceTestBase):
    def test_claim_promotion_failure_retains_worktree_and_reservation(self) -> None:
        repo_root = self._init_repo("repo-phase-c")
        self._write_ticket(repo_root, "121")
        target = self.tmp_path / "phase-c-target"

        original = cli.promote_claim

        def failing_promote(*args, **kwargs):
            raise workspaces.WorkspaceError("simulated promotion failure")

        cli.promote_claim = failing_promote
        try:
            code, stdout, stderr = self._invoke(
                repo_root, "workspace", "add", "121", str(target)
            )
        finally:
            cli.promote_claim = original

        self.assertEqual(code, 1)
        self.assertIn("claim promotion failed", stderr)
        self.assertIn("simulated promotion failure", stderr)

        # Both artifacts are preserved and both are named.
        self.assertTrue(target.exists(), "the worktree must be retained")
        self.assertIn(str(target), stderr)
        claim_file = self._claim_path(repo_root, "121")
        self.assertTrue(claim_file.exists(), "the reservation must be retained")
        self.assertIn("reservation", stderr)
        self.assertIn(str(claim_file), stderr)

        retained = workspaces.read_claim_file(claim_file)
        self.assertEqual(retained.status, workspaces.CLAIM_STATUS_CREATING)
        self.assertIn(retained.token, stderr)

    def test_seed_and_activation_failure_retains_a_coherent_active_claim(self) -> None:
        repo_root = self._init_repo("repo-phase-d")
        self._write_ticket(repo_root, "122")
        target = self.tmp_path / "phase-d-target"

        original = cli._seed_workspace_ai_dev

        def failing_seed(*args, **kwargs):
            raise cli.FlowError("simulated seed failure")

        cli._seed_workspace_ai_dev = failing_seed
        try:
            code, stdout, stderr = self._invoke(
                repo_root, "workspace", "add", "122", str(target)
            )
        finally:
            cli._seed_workspace_ai_dev = original

        self.assertEqual(code, 1)
        self.assertIn("simulated seed failure", stderr)
        self.assertTrue(target.exists())
        self.assertIn(str(target), stderr)

        claim_file = self._claim_path(repo_root, "122")
        self.assertTrue(claim_file.exists())
        retained = workspaces.read_claim_file(claim_file)
        self.assertEqual(retained.status, workspaces.CLAIM_STATUS_ACTIVE)
        self.assertEqual(
            retained.worktree_id,
            workspaces.effective_worktree_id(target),
            "the retained claim must name the worktree that actually exists",
        )
        self.assertIn("remove", stderr)

        # The documented recovery path works and leaves nothing behind.
        code, stdout, stderr = self._invoke(
            repo_root, "workspace", "remove", str(target)
        )
        self.assertEqual(code, 0, msg=stderr)
        self.assertFalse(target.exists())
        self.assertFalse(claim_file.exists())


class PostAcquisitionFailureTests(WorkspaceTestBase):
    def test_start_keeps_a_coherent_claim_when_state_persistence_fails(self) -> None:
        repo_root = self._init_repo("repo-start-persist")
        self._write_ticket(repo_root, "131")

        original = cli.save_state
        calls = {"count": 0}

        def failing_save_state(path, state):
            calls["count"] += 1
            raise cli.WorkflowStateError("simulated state persistence failure")

        cli.save_state = failing_save_state
        try:
            code, stdout, stderr = self._invoke(repo_root, "start", "131")
        finally:
            cli.save_state = original

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("workflow state could not be saved", stderr)
        self.assertIn("keeps the ticket claim", stderr)

        claim_file = self._claim_path(repo_root, "131")
        self.assertTrue(claim_file.exists())
        retained = workspaces.read_claim_file(claim_file)
        self.assertEqual(retained.status, workspaces.CLAIM_STATUS_ACTIVE)
        self.assertEqual(retained.worktree_id, workspaces.effective_worktree_id(repo_root))

        # Retrying from the owning workspace succeeds instead of self-colliding.
        code, stdout, stderr = self._invoke(repo_root, "start", "131")
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("Started issue 131", stdout)
        self.assertEqual(self._read_state(repo_root)["activeIssueNumber"], 131)

    def test_start_releases_its_claim_when_the_provider_refuses(self) -> None:
        repo_root = self._init_repo("repo-start-provider")
        self._write_ticket(repo_root, "132")
        claim_file = self._claim_path(repo_root, "132")

        original = cli._resolve_ticket_provider_for_repo_root

        class _RefusingProvider:
            def __init__(self, inner):
                self._inner = inner

            def get(self, ticket_id):
                return self._inner.get(ticket_id)

            def mark_active(self, reference):
                raise cli.TicketProviderError("simulated provider refusal")

        def wrapped(root):
            return _RefusingProvider(original(root))

        cli._resolve_ticket_provider_for_repo_root = wrapped
        try:
            code, stdout, stderr = self._invoke(repo_root, "start", "132")
        finally:
            cli._resolve_ticket_provider_for_repo_root = original

        self.assertEqual(code, 1)
        self.assertIn("simulated provider refusal", stderr)
        self.assertFalse(
            claim_file.exists(),
            "a claim must not survive a start that changed nothing outside the registry",
        )
        self.assertIsNone(self._read_state(repo_root).get("activeIssueNumber"))

    def test_adopt_releases_its_claim_when_the_provider_refuses(self) -> None:
        repo_root = self._init_repo("repo-adopt-provider")
        workspace = self.tmp_path / "adopt-provider-ws"
        self._run_git(
            repo_root, "worktree", "add", "-q", "-b", "flow/manual/141", str(workspace), "main"
        )
        self._write_config(workspace)
        self._write_state(workspace, scratch_branch="flow/manual/141")
        self._write_ticket(workspace, "141")
        claim_file = self._claim_path(repo_root, "141")

        original = cli._resolve_ticket_provider_for_repo_root

        class _RefusingProvider:
            def __init__(self, inner):
                self._inner = inner

            def get(self, ticket_id):
                return self._inner.get(ticket_id)

            def mark_active(self, reference):
                raise cli.TicketProviderError("simulated provider refusal")

        def wrapped(root):
            return _RefusingProvider(original(root))

        cli._resolve_ticket_provider_for_repo_root = wrapped
        try:
            code, stdout, stderr = self._invoke(workspace, "workspace", "adopt", "141")
        finally:
            cli._resolve_ticket_provider_for_repo_root = original

        self.assertEqual(code, 1)
        self.assertIn("simulated provider refusal", stderr)
        self.assertFalse(claim_file.exists())
        self.assertIsNone(self._read_state(workspace).get("activeIssueNumber"))

    def test_adopt_keeps_a_coherent_claim_when_state_persistence_fails(self) -> None:
        repo_root = self._init_repo("repo-adopt-persist")
        workspace = self.tmp_path / "adopt-persist-ws"
        self._run_git(
            repo_root, "worktree", "add", "-q", "-b", "flow/manual/142", str(workspace), "main"
        )
        self._write_config(workspace)
        self._write_state(workspace, scratch_branch="flow/manual/142")
        self._write_ticket(workspace, "142")

        original = cli.save_state

        def failing_save_state(path, state):
            raise cli.WorkflowStateError("simulated state persistence failure")

        cli.save_state = failing_save_state
        try:
            code, stdout, stderr = self._invoke(workspace, "workspace", "adopt", "142")
        finally:
            cli.save_state = original

        self.assertEqual(code, 1)
        self.assertIn("workflow state could not be saved", stderr)
        self.assertIn("keeps the ticket claim", stderr)

        claim_file = self._claim_path(repo_root, "142")
        self.assertTrue(claim_file.exists())
        retained = workspaces.read_claim_file(claim_file)
        self.assertEqual(retained.status, workspaces.CLAIM_STATUS_ACTIVE)
        self.assertEqual(retained.worktree_id, workspaces.effective_worktree_id(workspace))

        # Retrying adopt from the owning workspace reuses the claim.
        code, stdout, stderr = self._invoke(workspace, "workspace", "adopt", "142")
        self.assertEqual(code, 0, msg=stderr)
        self.assertEqual(self._read_state(workspace)["activeIssueNumber"], 142)


if __name__ == "__main__":
    unittest.main()
