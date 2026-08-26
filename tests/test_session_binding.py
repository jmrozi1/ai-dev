from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import session_binding, workspaces
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BindingStore,
    RailIteration,
    SessionBindingError,
    attach_process,
    find_binding_for_iteration,
    prove_workspace_identity,
    reserve_binding,
    unbind_session,
    validate_session_id,
)
from ai_dev_flow.tickets import TicketReference


RAIL = "issue-55-binding-authorization-foundation"

BLOB_A = "a" * 40
BLOB_B = "b" * 40
HEAD = "c" * 40
SESSION_ONE = "1a2b3c4d-0001-4000-8000-00000000000a"
SESSION_TWO = "1a2b3c4d-0002-4000-8000-00000000000b"


class SessionBindingTestBase(unittest.TestCase):
    """Real Git worktrees and real claims: workspace identity is the whole point."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()
        self.repo_root = self._init_repo("product")
        self.store = BindingStore(self.tmp_path / "controller")

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "prune"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._tmpdir.cleanup()

    def _git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Binding Tests")
        self._git(repo_root, "config", "user.email", "binding-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo_root, "add", "tracked.txt")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _reference(self, ticket_id: str = "55") -> TicketReference:
        return TicketReference(provider="github", ticket_id=ticket_id, repository="owner/repo")

    def _add_workspace(self, name: str, reference: TicketReference) -> tuple[Path, str]:
        """A linked worktree holding an active claim for `reference`."""
        path = self.tmp_path / name
        branch = "flow/{0}".format(name)
        self._git(self.repo_root, "worktree", "add", "-q", "-b", branch, str(path), "main")
        worktree_id = workspaces.effective_worktree_id(path)
        workspaces.create_active_claim(
            path, reference=reference, worktree_id=worktree_id,
            workspace_path=path, branch=branch,
        )
        return path, worktree_id

    def _reserve(self, **overrides: object) -> session_binding.BindingRecord:
        arguments: dict = {
            "project": "ai-dev",
            "ticket": "issue-55",
            "reference": self.reference,
            "workspace_path": self.workspace,
            "worktree_id": self.worktree_id,
            "rail": RAIL,
            "role": "executor",
            "iteration": RailIteration(rail=RAIL, blob=BLOB_A),
            "session_id": SESSION_ONE,
            "launched_at_head": HEAD,
            "reserved_at": "2026-08-26T12:00:00Z",
        }
        arguments.update(overrides)
        return reserve_binding(self.store, **arguments)  # type: ignore[arg-type]

    def _attach(self, session_id: str = SESSION_ONE, **overrides: object):
        arguments: dict = {
            "pid": 4242,
            "pid_domain": "test-host",
            "started_at": "2026-08-26T12:00:02Z",
            "bound_at": "2026-08-26T12:00:03Z",
        }
        arguments.update(overrides)
        return attach_process(self.store, session_id, **arguments)  # type: ignore[arg-type]

    def _bind(self, **overrides: object) -> session_binding.BindingRecord:
        """The whole launch sequence: reserve, then attach after a simulated spawn."""
        reserved = self._reserve(**overrides)
        return self._attach(reserved.session_id)


class SessionBindingTests(SessionBindingTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.reference = self._reference()
        self.workspace, self.worktree_id = self._add_workspace("workspace-55", self.reference)

    # Phase one: reservation

    def test_reservation_records_the_exact_assignment_with_no_process_identity(self) -> None:
        record = self._reserve()

        self.assertEqual(record.project, "ai-dev")
        self.assertEqual(record.ticket, "issue-55")
        self.assertEqual(record.workspace_key, "github:owner/repo#55")
        self.assertEqual(record.worktree_id, self.worktree_id)
        self.assertEqual(record.workspace_path, str(self.workspace))
        self.assertEqual(record.rail, RAIL)
        self.assertEqual(record.role, "executor")
        self.assertEqual(record.iteration, RailIteration(rail=RAIL, blob=BLOB_A))
        self.assertEqual(record.session_id, SESSION_ONE)
        self.assertEqual(record.launched_at_head, HEAD)
        self.assertEqual(record.reserved_at, "2026-08-26T12:00:00Z")
        self.assertEqual(record.state, BINDING_STATE_RESERVED)
        self.assertTrue(record.is_reserved)
        self.assertFalse(record.is_terminal)

        self.assertIsNone(record.pid)
        self.assertIsNone(record.pid_domain)
        self.assertIsNone(record.started_at)
        self.assertIsNone(record.bound_at)
        self.assertFalse(record.has_process_identity)

        self.assertEqual(self.store.read(SESSION_ONE), record)
        self.assertEqual(self.store.records(), [record])

    def test_a_reservation_is_durable_before_any_spawn(self) -> None:
        self._reserve()
        # A fresh store over the same root -- as a restarted controller would open --
        # sees the reservation even though no process was ever started.
        reopened = BindingStore(self.store.root)
        record = reopened.read(SESSION_ONE)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.state, BINDING_STATE_RESERVED)
        self.assertEqual(record.session_id, SESSION_ONE)

    def test_a_reserved_record_carries_no_process_keys_on_disk(self) -> None:
        self._reserve()
        payload = json.loads(self.store.path_for(SESSION_ONE).read_text(encoding="utf-8"))
        for key in session_binding.PROCESS_FIELD_NAMES:
            self.assertNotIn(key, payload)
        self.assertEqual(payload["reservedAt"], "2026-08-26T12:00:00Z")

    def test_reservation_refuses_a_record_carrying_prelaunch_process_identity(self) -> None:
        with self.assertRaises(SessionBindingError) as caught:
            session_binding.build_record(
                project="ai-dev", ticket="issue-55", workspace_key="github:owner/repo#55",
                worktree_id=self.worktree_id, workspace_path=self.workspace, rail=RAIL,
                role="executor", iteration=RailIteration(rail=RAIL, blob=BLOB_A),
                session_id=SESSION_ONE, launched_at_head=HEAD,
                reserved_at="2026-08-26T12:00:00Z",
                state=BINDING_STATE_RESERVED,
                pid=4242, pid_domain="test-host",
                started_at="2026-08-26T12:00:02Z", bound_at="2026-08-26T12:00:03Z",
            )
        self.assertEqual(caught.exception.reason, session_binding.REASON_INVALID_IDENTITY)

    def test_process_identity_is_all_four_fields_or_none(self) -> None:
        for omitted in ("pid", "pid_domain", "started_at", "bound_at"):
            with self.subTest(omitted=omitted):
                self._reserve()
                with self.assertRaises(SessionBindingError) as caught:
                    self._attach(**{omitted: None})
                self.assertEqual(
                    caught.exception.reason, session_binding.REASON_INVALID_IDENTITY
                )
                self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_RESERVED)
                self.store.path_for(SESSION_ONE).unlink()

    # Phase two: attaching the process a successful spawn produced

    def test_attach_transitions_reserved_to_bound_and_keeps_the_assignment(self) -> None:
        reserved = self._reserve()
        bound = self._attach()

        self.assertEqual(bound.state, BINDING_STATE_BOUND)
        self.assertTrue(bound.has_process_identity)
        self.assertEqual(bound.pid, 4242)
        self.assertEqual(bound.pid_domain, "test-host")
        self.assertEqual(bound.started_at, "2026-08-26T12:00:02Z")
        self.assertEqual(bound.bound_at, "2026-08-26T12:00:03Z")

        for name in ("project", "ticket", "workspace_key", "worktree_id", "workspace_path",
                     "rail", "role", "iteration", "session_id", "launched_at_head",
                     "reserved_at"):
            self.assertEqual(getattr(bound, name), getattr(reserved, name), msg=name)

        self.assertEqual(self.store.read(SESSION_ONE), bound)
        self.assertEqual(self.store.records(), [bound])

    def test_attach_validates_every_process_field(self) -> None:
        for field, value in (
            ("pid", 0),
            ("pid", "4242"),
            ("pid_domain", "  "),
            ("started_at", "2026-08-26 12:00:02"),
            ("bound_at", "yesterday"),
        ):
            with self.subTest(field=field, value=value):
                self._reserve()
                with self.assertRaises(SessionBindingError):
                    self._attach(**{field: value})
                self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_RESERVED)
                self.store.path_for(SESSION_ONE).unlink()

    def test_attach_refuses_an_already_bound_record(self) -> None:
        self._bind()
        with self.assertRaises(SessionBindingError) as caught:
            self._attach(pid=9999)
        self.assertEqual(caught.exception.reason, session_binding.REASON_NOT_RESERVED)
        self.assertEqual(self.store.read(SESSION_ONE).pid, 4242)

    def test_attach_refuses_a_terminal_record(self) -> None:
        self._reserve()
        unbind_session(self.store, SESSION_ONE)
        with self.assertRaises(SessionBindingError) as caught:
            self._attach()
        self.assertEqual(caught.exception.reason, session_binding.REASON_NOT_RESERVED)
        self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_UNBOUND)

    def test_attach_refuses_an_unknown_session(self) -> None:
        with self.assertRaises(SessionBindingError) as caught:
            self._attach(SESSION_TWO)
        self.assertEqual(caught.exception.reason, session_binding.REASON_UNKNOWN_SESSION)

    def test_attach_refuses_a_reservation_made_at_another_iteration(self) -> None:
        self._reserve()
        with self.assertRaises(SessionBindingError) as caught:
            attach_process(
                self.store, SESSION_ONE, pid=4242, pid_domain="test-host",
                started_at="2026-08-26T12:00:02Z", bound_at="2026-08-26T12:00:03Z",
                expected_iteration=RailIteration(rail=RAIL, blob=BLOB_B),
            )
        self.assertEqual(caught.exception.reason, session_binding.REASON_ITERATION_MISMATCH)
        self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_RESERVED)

    def test_current_pid_domain_names_the_host_a_pid_is_meaningful_on(self) -> None:
        domain = session_binding.current_pid_domain()
        self.assertEqual(domain, socket.gethostname())
        self._reserve()
        bound = self._attach(pid_domain=domain)
        self.assertEqual(bound.pid_domain, domain)

    def test_attach_accepts_the_iteration_the_reservation_was_made_at(self) -> None:
        self._reserve()
        bound = attach_process(
            self.store, SESSION_ONE, pid=4242, pid_domain="test-host",
            started_at="2026-08-26T12:00:02Z", bound_at="2026-08-26T12:00:03Z",
            expected_iteration=RailIteration(rail=RAIL, blob=BLOB_A),
        )
        self.assertEqual(bound.state, BINDING_STATE_BOUND)

    def test_a_failed_attach_leaves_the_reservation_exactly_as_it_was(self) -> None:
        reserved = self._reserve()
        with patch("ai_dev_flow.session_binding.write_json_object_atomic",
                   side_effect=session_binding.JsonFileError("device is full")):
            with self.assertRaises(SessionBindingError) as caught:
                self._attach()
        self.assertEqual(caught.exception.reason, session_binding.REASON_STORE_WRITE_FAILED)
        self.assertEqual(self.store.read(SESSION_ONE), reserved)

    def test_iteration_is_the_rail_slug_and_rail_blob_not_a_new_counter(self) -> None:
        record = self._reserve()
        payload = json.loads(self.store.path_for(SESSION_ONE).read_text(encoding="utf-8"))
        self.assertEqual(payload["iteration"], {"rail": RAIL, "blob": BLOB_A})
        self.assertEqual(record.iteration.rail, record.rail)

    def test_iteration_naming_another_rail_is_refused(self) -> None:
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(iteration=RailIteration(rail="some-other-rail", blob=BLOB_A))
        self.assertEqual(caught.exception.reason, session_binding.REASON_INVALID_ITERATION)
        self.assertEqual(self.store.record_files(), [])

    # Duplicates

    def test_duplicate_session_id_is_refused_without_touching_the_stored_record(self) -> None:
        first = self._reserve()
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(rail="issue-55-second-rail",
                          iteration=RailIteration(rail="issue-55-second-rail", blob=BLOB_B))
        self.assertEqual(caught.exception.reason, session_binding.REASON_DUPLICATE_SESSION_ID)
        self.assertEqual(self.store.records(), [first])

    def test_second_reservation_for_one_rail_iteration_is_refused(self) -> None:
        first = self._reserve()
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(session_id=SESSION_TWO)
        self.assertEqual(caught.exception.reason, session_binding.REASON_DUPLICATE_RAIL_ITERATION)
        self.assertEqual(self.store.records(), [first])

    def test_a_bound_binding_also_holds_its_rail_iteration(self) -> None:
        self._bind()
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(session_id=SESSION_TWO)
        self.assertEqual(caught.exception.reason, session_binding.REASON_DUPLICATE_RAIL_ITERATION)

    def test_a_new_rail_iteration_may_be_reserved_by_a_new_session(self) -> None:
        self._reserve()
        second = self._reserve(
            session_id=SESSION_TWO,
            iteration=RailIteration(rail=RAIL, blob=BLOB_B),
        )
        self.assertEqual(second.session_id, SESSION_TWO)
        self.assertEqual(len(self.store.records()), 2)

    def test_find_binding_for_iteration_covers_reserved_and_bound_but_not_terminal(self) -> None:
        reserved = self._reserve()
        iteration = reserved.iteration

        def held():
            return find_binding_for_iteration(
                self.store, project="ai-dev", ticket="issue-55", iteration=iteration
            )

        self.assertEqual(held(), [reserved])
        bound = self._attach()
        self.assertEqual(held(), [bound])
        unbind_session(self.store, SESSION_ONE)
        self.assertEqual(held(), [])

    # Identity

    def test_path_is_recorded_but_never_substitutes_for_worktree_identity(self) -> None:
        other_reference = self._reference("56")
        other_path, other_worktree_id = self._add_workspace("workspace-56", other_reference)
        self.assertNotEqual(other_worktree_id, self.worktree_id)

        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(workspace_path=other_path)
        self.assertEqual(
            caught.exception.reason, session_binding.REASON_WORKTREE_IDENTITY_MISMATCH
        )
        self.assertEqual(self.store.record_files(), [])

    def test_claim_naming_another_worktree_refuses_the_binding(self) -> None:
        stranger_reference = self._reference("77")
        stranger_path, stranger_worktree_id = self._add_workspace(
            "workspace-77", stranger_reference
        )
        # The stranger workspace holds the claim for #77; binding #77 from *this*
        # workspace must fail even though the caller states this worktree's id.
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(reference=stranger_reference)
        self.assertEqual(
            caught.exception.reason, session_binding.REASON_WORKSPACE_IDENTITY_UNPROVEN
        )
        self.assertIn(stranger_worktree_id, caught.exception.detail + str(stranger_path))
        self.assertEqual(self.store.record_files(), [])

    def test_workspace_without_any_claim_is_refused(self) -> None:
        unclaimed = self.tmp_path / "workspace-unclaimed"
        self._git(self.repo_root, "worktree", "add", "-q", "-b", "flow/unclaimed",
                  str(unclaimed), "main")
        unclaimed_id = workspaces.effective_worktree_id(unclaimed)
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(
                reference=self._reference("99"),
                workspace_path=unclaimed,
                worktree_id=unclaimed_id,
            )
        self.assertEqual(
            caught.exception.reason, session_binding.REASON_WORKSPACE_IDENTITY_UNPROVEN
        )

    def test_missing_workspace_path_is_refused(self) -> None:
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(workspace_path=self.tmp_path / "not-there")
        self.assertEqual(
            caught.exception.reason, session_binding.REASON_WORKSPACE_IDENTITY_UNPROVEN
        )

    def test_prove_workspace_identity_returns_the_canonical_key(self) -> None:
        self.assertEqual(
            prove_workspace_identity(
                self.workspace, reference=self.reference, worktree_id=self.worktree_id
            ),
            "github:owner/repo#55",
        )

    # Field validation

    def test_invalid_role_is_refused(self) -> None:
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve(role="controller")
        self.assertEqual(caught.exception.reason, session_binding.REASON_INVALID_ROLE)
        self.assertEqual(self.store.record_files(), [])

    def test_missing_required_identity_is_refused(self) -> None:
        for field, value in (
            ("project", ""),
            ("ticket", "  "),
            ("worktree_id", ""),
            ("launched_at_head", "not-a-sha"),
            ("reserved_at", "2026-08-26 12:00:00"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(SessionBindingError):
                    self._reserve(**{field: value})
        self.assertEqual(self.store.record_files(), [])

    def test_session_id_must_be_a_canonical_uuid(self) -> None:
        for candidate in ("", "session-one", SESSION_ONE.upper(), SESSION_ONE.replace("-", "")):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SessionBindingError) as caught:
                    validate_session_id(candidate)
                self.assertEqual(
                    caught.exception.reason, session_binding.REASON_INVALID_SESSION_ID
                )
        self.assertEqual(validate_session_id(" {0} ".format(SESSION_ONE)), SESSION_ONE)

    # Malformed and unreadable records

    def test_malformed_record_fails_closed_and_is_never_discarded(self) -> None:
        self._reserve()
        path = self.store.path_for(SESSION_ONE)
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(SessionBindingError) as caught:
            self.store.records()
        self.assertEqual(caught.exception.reason, session_binding.REASON_MALFORMED_RECORD)
        self.assertTrue(path.is_file())

    def test_record_stored_under_a_foreign_session_filename_is_malformed(self) -> None:
        record = self._reserve()
        foreign = self.store.path_for(SESSION_TWO)
        foreign.write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(SessionBindingError) as caught:
            self.store.read(SESSION_TWO)
        self.assertEqual(caught.exception.reason, session_binding.REASON_MALFORMED_RECORD)

    def test_unknown_key_in_a_record_is_malformed(self) -> None:
        record = self._reserve()
        payload = record.to_dict()
        payload["transcriptPath"] = "/somewhere/transcript.jsonl"
        self.store.path_for(SESSION_ONE).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaises(SessionBindingError) as caught:
            self.store.read(SESSION_ONE)
        self.assertEqual(caught.exception.reason, session_binding.REASON_MALFORMED_RECORD)

    def test_unreadable_record_fails_closed(self) -> None:
        self._reserve()
        path = self.store.path_for(SESSION_ONE)
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            with self.assertRaises(SessionBindingError) as caught:
                self.store.read(SESSION_ONE)
        self.assertEqual(caught.exception.reason, session_binding.REASON_UNREADABLE_RECORD)
        self.assertTrue(path.is_file())

    # Atomicity

    def test_a_failed_reservation_leaves_no_partial_record(self) -> None:
        with patch("os.fdopen", side_effect=OSError("disk full")):
            with self.assertRaises(SessionBindingError) as caught:
                self._reserve()
        self.assertEqual(caught.exception.reason, session_binding.REASON_STORE_WRITE_FAILED)
        self.assertEqual(self.store.record_files(), [])

    def test_a_failed_unbind_leaves_the_previous_record_intact(self) -> None:
        record = self._reserve()
        with patch("ai_dev_flow.session_binding.write_json_object_atomic",
                   side_effect=session_binding.JsonFileError("device is full")):
            with self.assertRaises(SessionBindingError) as caught:
                unbind_session(self.store, SESSION_ONE)
        self.assertEqual(caught.exception.reason, session_binding.REASON_STORE_WRITE_FAILED)
        self.assertEqual(self.store.read(SESSION_ONE), record)

    def test_reservation_uses_exclusive_creation_rather_than_replacement(self) -> None:
        # Not read-then-write: the write itself must refuse an existing file, so a
        # binding created between the check and the write is never overwritten.
        record = self._reserve()
        with self.assertRaises(SessionBindingError) as caught:
            self.store.write_new(record)
        self.assertEqual(caught.exception.reason, session_binding.REASON_DUPLICATE_SESSION_ID)
        self.assertEqual(self.store.read(SESSION_ONE), record)

    def test_a_malformed_record_blocks_reservation_instead_of_being_overwritten(self) -> None:
        self.store.bindings_directory.mkdir(parents=True, exist_ok=True)
        self.store.path_for(SESSION_ONE).write_text("squatter\n", encoding="utf-8")
        with self.assertRaises(SessionBindingError) as caught:
            self._reserve()
        self.assertEqual(caught.exception.reason, session_binding.REASON_MALFORMED_RECORD)
        self.assertEqual(
            self.store.path_for(SESSION_ONE).read_text(encoding="utf-8"), "squatter\n"
        )

    # Rebinding and abandoned launches

    def test_rebinding_requires_an_explicit_unbind_and_a_new_session_id(self) -> None:
        self._bind()

        # In place, under the same session id: refused.
        with self.assertRaises(SessionBindingError) as reused:
            self._reserve(iteration=RailIteration(rail=RAIL, blob=BLOB_B))
        self.assertEqual(reused.exception.reason, session_binding.REASON_DUPLICATE_SESSION_ID)

        terminal = unbind_session(self.store, SESSION_ONE)
        self.assertEqual(terminal.state, BINDING_STATE_UNBOUND)
        self.assertTrue(terminal.is_terminal)

        # The consumed session id stays consumed even after unbinding.
        with self.assertRaises(SessionBindingError) as consumed:
            self._reserve()
        self.assertEqual(consumed.exception.reason, session_binding.REASON_DUPLICATE_SESSION_ID)

        rebound = self._reserve(session_id=SESSION_TWO)
        self.assertEqual(rebound.session_id, SESSION_TWO)
        self.assertEqual(rebound.state, BINDING_STATE_RESERVED)

    def test_an_abandoned_reservation_terminalizes_and_keeps_its_session_id(self) -> None:
        # The spawn failed: no process was ever attached.
        self._reserve()
        abandoned = unbind_session(self.store, SESSION_ONE)
        self.assertEqual(abandoned.state, BINDING_STATE_UNBOUND)
        self.assertFalse(abandoned.has_process_identity)
        self.assertTrue(self.store.path_for(SESSION_ONE).is_file())

        with self.assertRaises(SessionBindingError) as caught:
            self._reserve()
        self.assertEqual(caught.exception.reason, session_binding.REASON_DUPLICATE_SESSION_ID)

        # The rail itself is free again for a new preassigned id.
        retried = self._reserve(session_id=SESSION_TWO)
        self.assertEqual(retried.state, BINDING_STATE_RESERVED)

    def test_unbinding_a_bound_record_keeps_its_process_identity_as_history(self) -> None:
        self._bind()
        terminal = unbind_session(self.store, SESSION_ONE)
        self.assertEqual(terminal.state, BINDING_STATE_UNBOUND)
        self.assertTrue(terminal.has_process_identity)
        self.assertEqual(terminal.pid, 4242)

    def test_no_operation_deletes_a_record(self) -> None:
        self._bind()
        unbind_session(self.store, SESSION_ONE)
        self.assertEqual(len(self.store.record_files()), 1)
        self.assertFalse(hasattr(self.store, "delete"))
        self.assertFalse(hasattr(session_binding, "delete_binding"))

    def test_unbinding_never_moves_a_binding_to_another_ticket_or_workspace(self) -> None:
        record = self._bind()
        unbind_session(self.store, SESSION_ONE)
        stored = self.store.read(SESSION_ONE)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.ticket, record.ticket)
        self.assertEqual(stored.workspace_key, record.workspace_key)
        self.assertEqual(stored.worktree_id, record.worktree_id)
        self.assertEqual(stored.rail, record.rail)
        self.assertEqual(stored.iteration, record.iteration)

    def test_unbinding_an_unknown_or_terminal_session_is_refused(self) -> None:
        with self.assertRaises(SessionBindingError) as unknown:
            unbind_session(self.store, SESSION_TWO)
        self.assertEqual(unknown.exception.reason, session_binding.REASON_UNKNOWN_SESSION)

        self._reserve()
        unbind_session(self.store, SESSION_ONE)
        with self.assertRaises(SessionBindingError) as again:
            unbind_session(self.store, SESSION_ONE)
        self.assertEqual(again.exception.reason, session_binding.REASON_ALREADY_UNBOUND)

    # Store root

    def test_the_storage_root_is_supplied_rather_than_a_hard_coded_host_path(self) -> None:
        elsewhere = BindingStore(self.tmp_path / "another-controller")
        self.assertEqual(elsewhere.root, self.tmp_path / "another-controller")
        self.assertEqual(elsewhere.records(), [])
        self._reserve()
        self.assertEqual(elsewhere.records(), [])
        self.assertFalse(str(session_binding.BINDINGS_DIRECTORY_NAME).startswith(os.sep))

    def test_the_record_carries_no_transcript_or_terminal_evidence(self) -> None:
        record = self._reserve()
        payload = record.to_dict()
        for forbidden in ("transcript", "transcriptPath", "tab", "terminal", "cwd", "focus"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
