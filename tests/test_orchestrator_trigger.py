"""`orchestrator_trigger` decides one wake from durable facts and builds a head-bound packet."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import List, Optional
from unittest import mock
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow import orchestrator_trigger as trigger
from ai_dev_flow.control_plane import ReadSource

PROJECT = "ai-dev"
TICKET = "issue-55"

STATE_SECRET = "ACCEPTED-STATE-PROSE-that-must-never-be-copied"
RAIL_SECRET = "RAIL-AUTHORIZATION-PROSE-that-must-never-be-copied"
HANDOFF_SECRET = "HANDOFF-EVIDENCE-PROSE-that-must-never-be-copied"
EVIDENCE_SECRET = "EVIDENCE-PAYLOAD-that-must-never-be-copied"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class ControlPlaneFixture:
    """A real Git control plane, so blob ids and reads are genuine Git objects."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "--quiet", "-b", "main")
        _git(root, "config", "user.email", "fixture@example.invalid")
        _git(root, "config", "user.name", "Fixture")
        self.write_state(STATE_SECRET)

    # writing

    def _write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_state(self, body: str) -> None:
        self._write(
            "{0}/{1}/state.md".format(PROJECT, TICKET),
            "# Control Plane State\n\n## Accepted State\n\n- {0}\n".format(body),
        )

    def write_rail(
        self,
        rail: str,
        *,
        status: str = "running",
        body: str = RAIL_SECRET,
        depends_on: Optional[str] = None,
        role: Optional[str] = "executor",
    ) -> None:
        header = "# Rail: {0}\n\nStatus: {1}\nOwner: orchestrator\n".format(rail, status)
        if role is not None:
            header += "Role: {0}\n".format(role)
        if depends_on:
            header += "Depends on: {0}\n".format(depends_on)
        header += "Shared resource: product-worktree\n\n## Goal\n\n{0}\n".format(body)
        self._write("{0}/{1}/rails/{2}/rail.md".format(PROJECT, TICKET, rail), header)

    def write_handoff(self, rail: str, *, status: str, body: str = HANDOFF_SECRET) -> None:
        self._write(
            "{0}/{1}/rails/{2}/handoff.md".format(PROJECT, TICKET, rail),
            "# Rail Handoff: {0}\n\nStatus: {1}\nRole: executor\n\n## Delivered\n\n{2}\n".format(
                rail, status, body
            ),
        )

    def write_evidence(self, rail: str, *, body: str = EVIDENCE_SECRET) -> None:
        self._write(
            "{0}/{1}/rails/{2}/evidence.json".format(PROJECT, TICKET, rail),
            '{{"schemaVersion": 1, "observations": [{{"kind": "{0}"}}]}}\n'.format(body),
        )

    def write_receipt(self, value: int) -> None:
        self._write("{0}/{1}/proceed-sequence.txt".format(PROJECT, TICKET), "{0}\n".format(value))

    # reading

    def commit(self, message: str = "publish") -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "--quiet", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def source(self, revision: Optional[str] = None) -> ReadSource:
        resolved = revision or _git(self.root, "rev-parse", "HEAD")
        return ReadSource(self.root, resolved, resolved)

    def blob(self, relative: str, revision: Optional[str] = None) -> str:
        resolved = revision or _git(self.root, "rev-parse", "HEAD")
        return _git(self.root, "rev-parse", "{0}:{1}".format(resolved, relative))


def _strings(value: object, seen: Optional[List[int]] = None) -> List[str]:
    """Every string reachable from a value, so leakage cannot hide in a nested field."""
    seen = [] if seen is None else seen
    if id(value) in seen:
        return []
    seen.append(id(value))
    if isinstance(value, str):
        return [value]
    found: List[str] = []
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found.extend(_strings(item, seen))
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_strings(key, seen))
            found.extend(_strings(item, seen))
        return found
    for attribute in getattr(type(value), "__dataclass_fields__", {}):
        found.extend(_strings(getattr(value, attribute), seen))
    return found


class TriggerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="trigger-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plane = ControlPlaneFixture(self.tmp / "plane")

    def snapshot(self, revision: Optional[str] = None) -> trigger.ScopeSnapshot:
        return trigger.build_snapshot(
            self.plane.source(revision), project=PROJECT, ticket=TICKET
        )


# --------------------------------------------------------------------------
# Snapshot provenance
# --------------------------------------------------------------------------


class SnapshotProvenanceTests(TriggerTestCase):
    def test_every_blob_id_comes_from_the_one_resolved_revision(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.write_evidence("rail-alpha")
        head = self.plane.commit()

        snapshot = self.snapshot()
        self.assertEqual(snapshot.head, head)
        self.assertEqual(snapshot.project, PROJECT)
        self.assertEqual(snapshot.ticket, TICKET)
        self.assertEqual(
            snapshot.state_blob, self.plane.blob("{0}/{1}/state.md".format(PROJECT, TICKET))
        )

        rail = snapshot.rail("rail-alpha")
        base = "{0}/{1}/rails/rail-alpha".format(PROJECT, TICKET)
        self.assertEqual(rail.authorization_blob, self.plane.blob(base + "/rail.md"))
        self.assertEqual(rail.handoff_blob, self.plane.blob(base + "/handoff.md"))
        self.assertEqual(rail.evidence_blob, self.plane.blob(base + "/evidence.json"))
        self.assertEqual(rail.status, "running")
        self.assertEqual(rail.proposed_status, "completed")
        self.assertTrue(rail.unreconciled)

    def test_absent_handoff_and_evidence_are_none_not_empty_strings(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.commit()
        rail = self.snapshot().rail("rail-alpha")
        self.assertIsNone(rail.handoff_blob)
        self.assertIsNone(rail.evidence_blob)
        self.assertIsNone(rail.proposed_status)
        self.assertFalse(rail.unreconciled)

    def test_rails_are_sorted_so_directory_order_cannot_change_the_snapshot(self) -> None:
        for rail in ("rail-zulu", "rail-alpha", "rail-mike"):
            self.plane.write_rail(rail)
        self.plane.commit()
        identifiers = [rail.identifier for rail in self.snapshot().rails]
        self.assertEqual(identifiers, sorted(identifiers))

    def test_a_local_worktree_read_is_refused_as_unpublished(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.commit()
        source = ReadSource(self.plane.root, None, "deadbeef")
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.build_snapshot(source, project=PROJECT, ticket=TICKET)
        self.assertEqual(caught.exception.reason, trigger.REASON_SOURCE_UNRESOLVED)

    def test_a_blank_head_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha")
        head = self.plane.commit()
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.build_snapshot(
                ReadSource(self.plane.root, head, ""), project=PROJECT, ticket=TICKET
            )
        self.assertEqual(caught.exception.reason, trigger.REASON_HEAD_UNRESOLVED)

    def test_revision_and_head_disagreement_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha")
        first = self.plane.commit()
        self.plane.write_rail("rail-beta")
        second = self.plane.commit()
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.build_snapshot(
                ReadSource(self.plane.root, first, second), project=PROJECT, ticket=TICKET
            )
        self.assertEqual(caught.exception.reason, trigger.REASON_REVISION_DRIFT)

    def test_a_scope_without_accepted_state_is_refused(self) -> None:
        (self.plane.root / PROJECT / TICKET / "state.md").unlink()
        self.plane.write_rail("rail-alpha")
        self.plane.commit()
        with self.assertRaises(trigger.TriggerError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.reason, trigger.REASON_STATE_BLOB_MISSING)

    def test_a_duplicated_rail_identity_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.commit()
        states = [
            mock.Mock(
                identifier="rail-alpha", status="running", proposed_status=None, artifacts=["rail"]
            )
            for _ in range(2)
        ]
        with mock.patch.object(trigger, "collect_rail_states", return_value=states):
            with self.assertRaises(trigger.TriggerError) as caught:
                self.snapshot()
        self.assertEqual(caught.exception.reason, trigger.REASON_DUPLICATE_RAIL)

    def test_a_rail_without_an_authorization_blob_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.commit()
        source = self.plane.source()
        real = source.blob_sha

        def missing_rail_blob(relative: str) -> Optional[str]:
            return None if relative.endswith("/rail.md") else real(relative)

        with mock.patch.object(source, "blob_sha", side_effect=missing_rail_blob):
            with self.assertRaises(trigger.TriggerError) as caught:
                trigger.build_snapshot(source, project=PROJECT, ticket=TICKET)
        self.assertEqual(caught.exception.reason, trigger.REASON_RAIL_BLOB_MISSING)

    def test_an_artifact_present_to_the_reader_but_blobless_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        source = self.plane.source()
        real = source.blob_sha

        def missing_handoff_blob(relative: str) -> Optional[str]:
            return None if relative.endswith("/handoff.md") else real(relative)

        with mock.patch.object(source, "blob_sha", side_effect=missing_handoff_blob):
            with self.assertRaises(trigger.TriggerError) as caught:
                trigger.build_snapshot(source, project=PROJECT, ticket=TICKET)
        self.assertEqual(caught.exception.reason, trigger.REASON_ARTIFACT_BLOB_MISSING)


# --------------------------------------------------------------------------
# No durable content leakage
# --------------------------------------------------------------------------


class NoContentLeakageTests(TriggerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plane.write_rail("rail-alpha")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.write_evidence("rail-alpha")
        self.plane.write_receipt(41)
        self.plane.commit()

    def test_no_artifact_prose_reaches_the_snapshot(self) -> None:
        snapshot = self.snapshot()
        blob = " ".join(_strings(snapshot)) + repr(snapshot)
        for secret in (STATE_SECRET, RAIL_SECRET, HANDOFF_SECRET, EVIDENCE_SECRET):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_no_artifact_prose_reaches_the_packet_or_the_wake(self) -> None:
        snapshot = self.snapshot()
        proposal = trigger.propose_wake(snapshot)
        packet = trigger.build_packet(snapshot)
        blob = " ".join(_strings(proposal) + _strings(packet)) + repr(proposal) + repr(packet)
        for secret in (STATE_SECRET, RAIL_SECRET, HANDOFF_SECRET, EVIDENCE_SECRET):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_the_packet_carries_no_receipt_or_event_payload_fields(self) -> None:
        names = {entry.name for entry in fields(trigger.OrchestratorPacket)}
        self.assertEqual(names, {"project", "ticket", "head", "role", "session_mode", "directive"})
        for forbidden in ("resume", "continue", "session_id", "transcript", "receipt", "elapsed"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, names)

    def test_the_snapshot_carries_only_identity_and_status_fields(self) -> None:
        self.assertEqual(
            {entry.name for entry in fields(trigger.ScopeSnapshot)},
            {"project", "ticket", "head", "state_blob", "rails"},
        )
        self.assertEqual(
            {entry.name for entry in fields(trigger.RailSnapshot)},
            {
                "identifier",
                "authorization_blob",
                "status",
                "proposed_status",
                "handoff_blob",
                "evidence_blob",
                "role",
            },
        )


class DurableRoleSnapshotTests(TriggerTestCase):
    """The snapshot carries the durable role as a normalized token, and nothing more."""

    def test_the_role_is_surfaced_from_the_resolved_revision(self) -> None:
        self.plane.write_rail("rail-alpha", role="orchestrator")
        self.plane.commit()
        self.assertEqual(self.snapshot().rail("rail-alpha").role, "orchestrator")

    def test_a_rail_without_a_role_snapshots_as_none_and_still_reads(self) -> None:
        self.plane.write_rail("rail-alpha", role=None)
        self.plane.commit()
        snapshot = self.snapshot()
        self.assertIsNone(snapshot.rail("rail-alpha").role)
        self.assertEqual(len(snapshot.rails), 1)

    def test_a_non_managed_role_snapshots_normalized(self) -> None:
        self.plane.write_rail("rail-alpha", role="Evidence-Worker")
        self.plane.commit()
        self.assertEqual(self.snapshot().rail("rail-alpha").role, "evidence-worker")

    def test_the_role_is_not_a_second_wake_fingerprint_input(self) -> None:
        """Changing `Role:` already changes the rail blob; counting it twice would
        make one edit look like two material facts."""
        self.plane.write_rail("rail-alpha", role="executor")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        rail = self.snapshot().rail("rail-alpha")
        self.assertNotIn("executor", rail.material_fingerprint)
        self.assertEqual(
            rail.material_fingerprint,
            (rail.identifier, rail.authorization_blob, rail.handoff_blob, ""),
        )

    def test_a_role_change_is_material_only_through_the_rail_blob(self) -> None:
        self.plane.write_rail("rail-alpha", role="executor")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        cursor = trigger.TriggerCursor()
        first = trigger.propose_wake(self.snapshot(), cursor=cursor)
        self.assertIsNotNone(first)

        self.plane.write_rail("rail-alpha", role="reviewer")
        self.plane.commit()
        second = self.snapshot().rail("rail-alpha")
        self.assertEqual(second.role, "reviewer")
        self.assertNotEqual(first.reasons[0].fingerprint[2], second.authorization_blob)


# --------------------------------------------------------------------------
# Material wake: unreconciled handoff
# --------------------------------------------------------------------------


class UnreconciledHandoffWakeTests(TriggerTestCase):
    def test_a_new_unreconciled_handoff_proposes_exactly_one_wake(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        head = self.plane.commit()

        proposal = trigger.propose_wake(self.snapshot())
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.head, head)
        self.assertEqual(proposal.project, PROJECT)
        self.assertEqual(proposal.ticket, TICKET)
        self.assertEqual(len(proposal.reasons), 1)
        self.assertEqual(proposal.reasons[0].kind, trigger.WAKE_UNRECONCILED_HANDOFF)
        self.assertEqual(proposal.reasons[0].rail, "rail-alpha")
        self.assertEqual(proposal.rails, ("rail-alpha",))

    def test_a_reconciled_handoff_is_not_material(self) -> None:
        self.plane.write_rail("rail-alpha", status="completed")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot()))

    def test_a_rail_with_no_handoff_is_not_material(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot()))

    def test_the_handoff_reason_is_identified_by_rail_rail_blob_handoff_and_evidence(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.write_evidence("rail-alpha")
        self.plane.commit()

        rail = self.snapshot().rail("rail-alpha")
        reason = trigger.propose_wake(self.snapshot()).reasons[0]
        self.assertEqual(
            reason.fingerprint,
            (
                trigger.WAKE_UNRECONCILED_HANDOFF,
                "rail-alpha",
                rail.authorization_blob,
                rail.handoff_blob,
                rail.evidence_blob,
            ),
        )


# --------------------------------------------------------------------------
# Material wake: injected lifecycle transitions
# --------------------------------------------------------------------------


class LifecycleWakeTests(TriggerTestCase):
    def _fact(self, kind: str, *, rail: str = "rail-alpha", **overrides: str):
        snapshot = self.snapshot()
        entry = snapshot.rail(rail)
        payload = dict(
            kind=kind,
            rail=rail,
            session_id="1a2b3c4d-0001-4000-8000-00000000000a",
            iteration=entry.authorization_blob if entry else "0" * 40,
            head=snapshot.head,
        )
        payload.update(overrides)
        return trigger.LifecycleFact(**payload)

    def test_an_ended_invocation_without_a_handoff_wakes_once(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_INVOCATION_ENDED)

        proposal = trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact])
        self.assertEqual(len(proposal.reasons), 1)
        self.assertEqual(proposal.reasons[0].kind, trigger.LIFECYCLE_INVOCATION_ENDED)

    def test_a_running_binding_becoming_disconnected_wakes_once(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_BECAME_DISCONNECTED)

        proposal = trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact])
        self.assertEqual(len(proposal.reasons), 1)
        self.assertEqual(proposal.reasons[0].kind, trigger.LIFECYCLE_BECAME_DISCONNECTED)

    def test_an_ended_invocation_whose_handoff_is_already_reconciled_is_not_material(self) -> None:
        self.plane.write_rail("rail-alpha", status="completed")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_INVOCATION_ENDED)
        self.assertIsNone(trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact]))

    def test_disconnection_on_a_rail_no_longer_running_is_not_material(self) -> None:
        self.plane.write_rail("rail-alpha", status="blocked")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_BECAME_DISCONNECTED)
        self.assertIsNone(trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact]))

    def test_an_incomplete_fact_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        with self.assertRaises(trigger.TriggerError) as caught:
            self._fact(trigger.LIFECYCLE_INVOCATION_ENDED, session_id="   ")
        self.assertEqual(caught.exception.reason, trigger.REASON_INCOMPLETE_FACT)

    def test_an_unknown_transition_kind_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        with self.assertRaises(trigger.TriggerError) as caught:
            self._fact("looked-idle-for-a-while")
        self.assertEqual(caught.exception.reason, trigger.REASON_UNKNOWN_KIND)

    def test_a_fact_from_another_head_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_INVOCATION_ENDED, head="0" * 40)
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact])
        self.assertEqual(caught.exception.reason, trigger.REASON_CROSS_HEAD_FACT)

    def test_a_fact_naming_an_unknown_rail_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_INVOCATION_ENDED, rail="rail-ghost")
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact])
        self.assertEqual(caught.exception.reason, trigger.REASON_UNKNOWN_RAIL)

    def test_a_fact_from_another_rail_iteration_is_refused(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        fact = self._fact(trigger.LIFECYCLE_INVOCATION_ENDED, iteration="0" * 40)
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.propose_wake(self.snapshot(), lifecycle_facts=[fact])
        self.assertEqual(caught.exception.reason, trigger.REASON_ITERATION_DRIFT)


# --------------------------------------------------------------------------
# Coalescing and duplicate suppression
# --------------------------------------------------------------------------


class CoalescingTests(TriggerTestCase):
    def test_simultaneous_material_facts_coalesce_into_one_proposal(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.write_rail("rail-beta", status="running")
        self.plane.write_handoff("rail-beta", status="blocked")
        self.plane.commit()

        proposal = trigger.propose_wake(self.snapshot())
        self.assertEqual(len(proposal.reasons), 2)
        self.assertEqual(proposal.rails, ("rail-alpha", "rail-beta"))

    def test_a_handoff_and_a_lifecycle_fact_on_one_rail_coalesce_into_one_proposal(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        snapshot = self.snapshot()
        fact = trigger.LifecycleFact(
            kind=trigger.LIFECYCLE_BECAME_DISCONNECTED,
            rail="rail-alpha",
            session_id="1a2b3c4d-0001-4000-8000-00000000000a",
            iteration=snapshot.rail("rail-alpha").authorization_blob,
            head=snapshot.head,
        )
        proposal = trigger.propose_wake(snapshot, lifecycle_facts=[fact])
        self.assertEqual(len(proposal.reasons), 2)
        self.assertEqual(proposal.rails, ("rail-alpha",))
        self.assertEqual(
            sorted(reason.kind for reason in proposal.reasons),
            sorted([trigger.WAKE_UNRECONCILED_HANDOFF, trigger.LIFECYCLE_BECAME_DISCONNECTED]),
        )

    def test_reason_order_is_stable_regardless_of_input_order(self) -> None:
        for rail in ("rail-alpha", "rail-beta", "rail-mike"):
            self.plane.write_rail(rail, status="running")
            self.plane.write_handoff(rail, status="completed")
        self.plane.commit()
        snapshot = self.snapshot()

        facts = [
            trigger.LifecycleFact(
                kind=trigger.LIFECYCLE_BECAME_DISCONNECTED,
                rail=rail,
                session_id="1a2b3c4d-0001-4000-8000-00000000000a",
                iteration=snapshot.rail(rail).authorization_blob,
                head=snapshot.head,
            )
            for rail in ("rail-mike", "rail-alpha", "rail-beta")
        ]
        forward = trigger.propose_wake(snapshot, lifecycle_facts=facts)
        backward = trigger.propose_wake(snapshot, lifecycle_facts=list(reversed(facts)))
        self.assertEqual(
            [(reason.rail, reason.kind) for reason in forward.reasons],
            [(reason.rail, reason.kind) for reason in backward.reasons],
        )
        self.assertEqual(
            [(reason.rail, reason.kind) for reason in forward.reasons],
            sorted((reason.rail, reason.kind) for reason in forward.reasons),
        )

    def test_a_repeated_identical_signal_is_suppressed_within_one_lifetime(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        cursor = trigger.TriggerCursor()

        self.assertIsNotNone(trigger.propose_wake(self.snapshot(), cursor=cursor))
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=cursor))
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=cursor))

    def test_a_newly_material_rail_wakes_again_and_carries_both_reasons(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        cursor = trigger.TriggerCursor()
        trigger.propose_wake(self.snapshot(), cursor=cursor)

        self.plane.write_rail("rail-beta", status="running")
        self.plane.write_handoff("rail-beta", status="blocked")
        self.plane.commit()

        second = trigger.propose_wake(self.snapshot(), cursor=cursor)
        self.assertIsNotNone(second)
        self.assertEqual(second.rails, ("rail-alpha", "rail-beta"))

    def test_a_republished_handoff_is_a_new_fact(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        cursor = trigger.TriggerCursor()
        trigger.propose_wake(self.snapshot(), cursor=cursor)

        self.plane.write_handoff("rail-alpha", status="blocked", body="revised " + HANDOFF_SECRET)
        self.plane.commit()
        self.assertIsNotNone(trigger.propose_wake(self.snapshot(), cursor=cursor))


class RestartTests(TriggerTestCase):
    def test_a_fresh_cursor_may_wake_once_again_for_the_same_handoff(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()

        first = trigger.TriggerCursor()
        self.assertIsNotNone(trigger.propose_wake(self.snapshot(), cursor=first))
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=first))

        restarted = trigger.TriggerCursor()
        self.assertIsNotNone(trigger.propose_wake(self.snapshot(), cursor=restarted))

    def test_omitting_the_cursor_never_suppresses(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        self.assertIsNotNone(trigger.propose_wake(self.snapshot()))
        self.assertIsNotNone(trigger.propose_wake(self.snapshot()))

    def test_the_cursor_holds_no_path_file_or_lease_state(self) -> None:
        cursor = trigger.TriggerCursor()
        names = {entry.name for entry in fields(trigger.TriggerCursor)}
        self.assertEqual(names, {"_seen"})
        for forbidden in ("path", "file", "lease", "store", "log", "history", "expires"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(any(forbidden in name for name in dir(cursor) if not name.startswith("__")))


# --------------------------------------------------------------------------
# Non-material change must not wake
# --------------------------------------------------------------------------


class NonMaterialChangeTests(TriggerTestCase):
    def _prime(self) -> trigger.TriggerCursor:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.plane.commit()
        cursor = trigger.TriggerCursor()
        self.assertIsNotNone(trigger.propose_wake(self.snapshot(), cursor=cursor))
        return cursor

    def test_a_receipt_only_head_movement_does_not_wake(self) -> None:
        cursor = self._prime()
        before = self.snapshot().head
        self.plane.write_receipt(42)
        after = self.plane.commit()
        self.assertNotEqual(before, after)
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=cursor))

    def test_an_accepted_state_change_alone_does_not_wake(self) -> None:
        cursor = self._prime()
        before = self.snapshot()
        self.plane.write_state("orchestrator rewrote the accepted state")
        self.plane.commit()
        after = self.snapshot()
        self.assertNotEqual(before.state_blob, after.state_blob)
        self.assertIsNone(trigger.propose_wake(after, cursor=cursor))

    def test_a_rail_authorization_change_on_a_reconciled_rail_does_not_wake(self) -> None:
        cursor = self._prime()
        self.plane.write_rail("rail-beta", status="completed")
        self.plane.write_handoff("rail-beta", status="completed")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=cursor))

        self.plane.write_rail("rail-beta", status="completed", body="reworded authorization")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot(), cursor=cursor))

    def test_rail_reordering_does_not_wake(self) -> None:
        cursor = self._prime()
        snapshot = self.snapshot()
        shuffled = trigger.ScopeSnapshot(
            project=snapshot.project,
            ticket=snapshot.ticket,
            head=snapshot.head,
            state_blob=snapshot.state_blob,
            rails=tuple(reversed(snapshot.rails)),
        )
        self.assertIsNone(trigger.propose_wake(shuffled, cursor=cursor))

    def test_long_running_work_alone_does_not_wake(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot()))

    def test_blocked_work_alone_does_not_wake(self) -> None:
        self.plane.write_rail("rail-alpha", status="blocked")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot()))

    def test_ready_work_alone_does_not_wake(self) -> None:
        self.plane.write_rail("rail-alpha", status="ready")
        self.plane.commit()
        self.assertIsNone(trigger.propose_wake(self.snapshot()))

    def test_elapsed_time_is_not_an_input_anywhere(self) -> None:
        import inspect

        for function in (trigger.propose_wake, trigger.build_snapshot, trigger.build_packet):
            parameters = set(inspect.signature(function).parameters)
            with self.subTest(function=function.__name__):
                for forbidden in ("now", "elapsed", "since", "deadline", "timeout", "clock"):
                    self.assertNotIn(forbidden, parameters)


# --------------------------------------------------------------------------
# Packet
# --------------------------------------------------------------------------


class PacketTests(TriggerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        self.head = self.plane.commit()

    def test_the_packet_is_built_only_from_the_snapshot(self) -> None:
        packet = trigger.build_packet(self.snapshot())
        self.assertEqual(packet.project, PROJECT)
        self.assertEqual(packet.ticket, TICKET)
        self.assertEqual(packet.head, self.head)
        self.assertEqual(packet.role, trigger.ROLE_ORCHESTRATOR)
        self.assertEqual(packet.session_mode, trigger.SESSION_MODE_NEW)
        self.assertEqual(packet.directive, trigger.DIRECTIVE)

    def test_the_directive_is_fixed_and_says_only_to_read_fresh(self) -> None:
        first = trigger.build_packet(self.snapshot()).directive
        self.plane.write_state("something else entirely")
        self.plane.commit()
        self.assertEqual(trigger.build_packet(self.snapshot()).directive, first)
        self.assertEqual(first, trigger.DIRECTIVE)

    def test_a_packet_cannot_request_resume_or_continuation(self) -> None:
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.OrchestratorPacket(
                project=PROJECT, ticket=TICKET, head=self.head, session_mode="resume"
            )
        self.assertEqual(caught.exception.reason, trigger.REASON_INVALID_SESSION_MODE)

    def test_a_packet_cannot_address_another_role(self) -> None:
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.OrchestratorPacket(
                project=PROJECT, ticket=TICKET, head=self.head, role="executor"
            )
        self.assertEqual(caught.exception.reason, trigger.REASON_INVALID_ROLE)

    def test_a_packet_without_a_head_is_refused(self) -> None:
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.OrchestratorPacket(project=PROJECT, ticket=TICKET, head="")
        self.assertEqual(caught.exception.reason, trigger.REASON_INCOMPLETE_FACT)

    def test_a_current_packet_is_returned_unchanged(self) -> None:
        snapshot = self.snapshot()
        packet = trigger.build_packet(snapshot)
        self.assertIs(trigger.require_current(packet, snapshot), packet)

    def test_head_drift_invalidates_the_packet(self) -> None:
        packet = trigger.build_packet(self.snapshot())
        self.plane.write_receipt(99)
        self.plane.commit()
        newer = self.snapshot()
        self.assertNotEqual(packet.head, newer.head)
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.require_current(packet, newer)
        self.assertEqual(caught.exception.reason, trigger.REASON_PACKET_STALE)
        self.assertEqual(trigger.build_packet(newer).head, newer.head)

    def test_a_packet_for_another_scope_is_refused(self) -> None:
        snapshot = self.snapshot()
        other = trigger.OrchestratorPacket(project="other-proj", ticket=TICKET, head=snapshot.head)
        with self.assertRaises(trigger.TriggerError) as caught:
            trigger.require_current(other, snapshot)
        self.assertEqual(caught.exception.reason, trigger.REASON_PACKET_STALE)


# --------------------------------------------------------------------------
# The module does nothing but decide
# --------------------------------------------------------------------------


class ModulePurityTests(TriggerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source_text = (
            Path(trigger.__file__).with_suffix(".py").read_text(encoding="utf-8")
        )

    def _absent(self, tokens: "tuple", *, lowered: bool = False) -> None:
        haystack = self.source_text.lower() if lowered else self.source_text
        self.assertEqual(
            [token for token in tokens if token in haystack],
            [],
            msg="orchestrator_trigger.py must not mention these",
        )

    def test_the_module_never_writes_the_control_plane(self) -> None:
        self._absent(
            (
                "publish(",
                "write_text_atomic",
                "allocate_proceed_number",
                "ensure_publishable",
                "open(",
                "write_text",
                "mkdir",
            )
        )

    def test_the_module_takes_no_process_provider_or_timing_action(self) -> None:
        self._absent(
            (
                "subprocess",
                "Popen",
                "os.kill",
                "killpg",
                "claude_agent_sdk",
                "claude_worker",
                "session_lifecycle",
                "threading",
                "Timer",
                "sleep",
                "datetime",
                "import time",
                "time.time",
                "time.monotonic",
                "perf_counter",
            )
        )

    def test_the_module_has_no_counter_retry_or_scoring_machinery(self) -> None:
        self._absent(
            ("retry", "attempts", "score", "backoff", "threshold", "counter"), lowered=True
        )

    def test_a_full_pass_leaves_the_control_plane_untouched(self) -> None:
        self.plane.write_rail("rail-alpha", status="running")
        self.plane.write_handoff("rail-alpha", status="completed")
        head = self.plane.commit()
        status_before = _git(self.plane.root, "status", "--porcelain")

        snapshot = self.snapshot()
        cursor = trigger.TriggerCursor()
        proposal = trigger.propose_wake(snapshot, cursor=cursor)
        packet = trigger.require_current(trigger.build_packet(snapshot), snapshot)

        self.assertIsNotNone(proposal)
        self.assertEqual(packet.head, head)
        self.assertEqual(_git(self.plane.root, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(self.plane.root, "status", "--porcelain"), status_before)

    def test_the_module_reuses_the_control_plane_readers(self) -> None:
        self.assertEqual(
            [
                name
                for name in ("collect_rail_states", "artifact_relative", "validate_identifier")
                if name not in self.source_text
            ],
            [],
            msg="orchestrator_trigger.py must reuse the control-plane readers",
        )
        self._absent(("_parse_rail_header", "_parse_handoff_status", "rev-parse", "ls-tree"))


if __name__ == "__main__":
    unittest.main()
