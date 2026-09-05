"""`orchestrator_outcome` verifies durable effects of one invocation, and never publishes."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow import orchestrator_outcome as outcome_module
from ai_dev_flow.authorization import (
    ACTION_LAUNCH,
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
    authorize,
)
from ai_dev_flow.control_plane import ReadSource
from ai_dev_flow.orchestrator_invocation import InvocationOutcome
from ai_dev_flow.orchestrator_outcome import (
    OutcomeError,
    ReconciliationReport,
    reconcile_outcome,
)
from ai_dev_flow.orchestrator_trigger import (
    LIFECYCLE_BECAME_DISCONNECTED,
    LIFECYCLE_INVOCATION_ENDED,
    WAKE_UNRECONCILED_HANDOFF,
    LifecycleFact,
    RailSnapshot,
    ScopeSnapshot,
    TriggerCursor,
    WakeProposal,
    WakeReason,
    build_packet,
    build_snapshot,
    propose_wake,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BINDING_STATES,
    NONTERMINAL_BINDING_STATES,
)

PROJECT = "ai-dev"
TICKET = "issue-55"

ORCH_RAIL = "issue-55-standing-orchestrator"
SOURCE_RAIL = "issue-55-executor-work"
REVIEW_RAIL = "issue-55-bounded-review"

HEAD_BEFORE = "a" * 40
HEAD_AFTER = "b" * 40
ORCH_BLOB = "c" * 40
SOURCE_BLOB = "d" * 40
STATE_BLOB_BEFORE = "e" * 40
STATE_BLOB_AFTER = "f" * 40
SESSION_ID = "11111111-1111-4111-8111-111111111111"

ORCH_RESOURCE = "issue-55-orchestrator-session"
WORK_RESOURCE = "issue-55-product-worktree"
REVIEW_RESOURCE = "issue-55-review-workspace"

STATE_SECRET = "ACCEPTED-STATE-PROSE-that-must-never-be-copied"
RAIL_SECRET = "RAIL-AUTHORIZATION-PROSE-that-must-never-be-copied"
HANDOFF_SECRET = "HANDOFF-EVIDENCE-PROSE-that-must-never-be-copied"
EVIDENCE_SECRET = "EVIDENCE-PAYLOAD-that-must-never-be-copied"


# --------------------------------------------------------------------------
# Builders for the pure gates
# --------------------------------------------------------------------------


def rail(
    identifier: str,
    *,
    blob: str,
    status: str = "running",
    proposed: Optional[str] = None,
    role: Optional[str] = "executor",
    handoff: Optional[str] = None,
    evidence: Optional[str] = None,
) -> RailSnapshot:
    return RailSnapshot(
        identifier=identifier,
        authorization_blob=blob,
        status=status,
        proposed_status=proposed,
        handoff_blob=handoff,
        evidence_blob=evidence,
        role=role,
    )


def standing(*, status: str = "running", role: str = "orchestrator", proposed=None) -> RailSnapshot:
    return rail(ORCH_RAIL, blob=ORCH_BLOB, status=status, role=role, proposed=proposed)


def snapshot(
    head: str = HEAD_BEFORE,
    rails=(),
    *,
    project: str = PROJECT,
    ticket: str = TICKET,
    state_blob: str = STATE_BLOB_BEFORE,
) -> ScopeSnapshot:
    return ScopeSnapshot(
        project=project,
        ticket=ticket,
        head=head,
        state_blob=state_blob,
        rails=tuple(sorted(rails, key=lambda entry: entry.identifier)),
    )


def reason(kind: str = WAKE_UNRECONCILED_HANDOFF, rail_id: str = SOURCE_RAIL) -> WakeReason:
    return WakeReason(kind=kind, rail=rail_id, fingerprint=(kind, rail_id, SOURCE_BLOB))


def proposal(
    reasons=None, *, head: str = HEAD_BEFORE, project: str = PROJECT, ticket: str = TICKET
) -> WakeProposal:
    return WakeProposal(
        project=project,
        ticket=ticket,
        head=head,
        reasons=tuple(reasons if reasons is not None else (reason(),)),
    )


def invocation_outcome(**overrides) -> InvocationOutcome:
    base = dict(
        project=PROJECT,
        ticket=TICKET,
        head=HEAD_BEFORE,
        rail=ORCH_RAIL,
        role="orchestrator",
        session_id=SESSION_ID,
        iteration_blob=ORCH_BLOB,
        wake_rails=(SOURCE_RAIL,),
        binding_state=BINDING_STATE_UNBOUND,
        process_group_gone=True,
        graceful=True,
    )
    base.update(overrides)
    return InvocationOutcome(**base)


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
    for attr in getattr(value, "__dataclass_fields__", {}):
        found.extend(_strings(getattr(value, attr), seen))
    return found


class OutcomeTestBase(unittest.TestCase):
    """The accepted arrangement: one source rail woke one standing orchestrator."""

    def before(self, *extra) -> ScopeSnapshot:
        return snapshot(
            HEAD_BEFORE,
            (
                standing(),
                rail(SOURCE_RAIL, blob=SOURCE_BLOB, proposed="completed", handoff="h" * 40),
            )
            + tuple(extra),
        )

    def after(self, source: Optional[RailSnapshot] = None, *, standing_rail=None, **kwargs):
        rails = [standing_rail if standing_rail is not None else standing()]
        if source is not None:
            rails.append(source)
        return snapshot(HEAD_AFTER, tuple(rails), state_blob=STATE_BLOB_AFTER, **kwargs)

    def reconciled_source(self) -> RailSnapshot:
        """The source rail after the orchestrator accepted its handoff."""
        return rail(
            SOURCE_RAIL, blob=SOURCE_BLOB, status="completed", proposed="completed",
            handoff="h" * 40,
        )

    def run_outcome(self, **overrides) -> ReconciliationReport:
        kwargs = dict(
            before=self.before(),
            proposal=proposal(),
            after=self.after(self.reconciled_source()),
            orchestrator_rail=ORCH_RAIL,
        )
        result = overrides.pop("outcome", None) or invocation_outcome()
        kwargs.update(overrides)
        return reconcile_outcome(result, **kwargs)

    def refused(self, **overrides) -> OutcomeError:
        with self.assertRaises(OutcomeError) as caught:
            self.run_outcome(**overrides)
        return caught.exception


# --------------------------------------------------------------------------
# Gate one: exact invocation identity and terminal state
# --------------------------------------------------------------------------


class ExactInvocationIdentityTests(OutcomeTestBase):
    """A near-miss outcome must never reconcile a proposal it did not run."""

    def test_the_accepted_arrangement_reconciles(self) -> None:
        report = self.run_outcome()
        self.assertTrue(report.reconciled)
        self.assertEqual(report.unresolved_rails, ())
        self.assertEqual(report.before_head, HEAD_BEFORE)
        self.assertEqual(report.after_head, HEAD_AFTER)
        self.assertEqual(report.rail, ORCH_RAIL)
        self.assertEqual(report.session_id, SESSION_ID)

    def test_a_duck_typed_outcome_is_refused(self) -> None:
        """Every gate below reads attributes; a look-alike must not reach them."""

        class LooksLikeAnOutcome:
            project = PROJECT
            ticket = TICKET
            head = HEAD_BEFORE
            rail = ORCH_RAIL
            role = "orchestrator"
            session_id = SESSION_ID
            iteration_blob = ORCH_BLOB
            wake_rails = (SOURCE_RAIL,)
            binding_state = BINDING_STATE_UNBOUND
            process_group_gone = True
            graceful = True

        error = self.refused(outcome=LooksLikeAnOutcome())
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_SHAPE)

    def test_a_foreign_scope_outcome_is_refused(self) -> None:
        for field, value in (("project", "other-project"), ("ticket", "issue-99")):
            with self.subTest(field=field):
                error = self.refused(outcome=invocation_outcome(**{field: value}))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_SCOPE)

    def test_an_outcome_from_another_head_is_refused(self) -> None:
        error = self.refused(outcome=invocation_outcome(head="9" * 40))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_HEAD)

    def test_a_proposal_from_another_head_is_refused(self) -> None:
        error = self.refused(proposal=proposal(head="9" * 40))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_HEAD)

    def test_a_proposal_from_another_scope_is_refused(self) -> None:
        error = self.refused(proposal=proposal(project="other-project"))
        self.assertEqual(error.reason, outcome_module.REASON_PROPOSAL_SCOPE)

    def test_a_proposal_with_no_reasons_is_refused(self) -> None:
        error = self.refused(proposal=proposal(reasons=()))
        self.assertEqual(error.reason, outcome_module.REASON_PROPOSAL_EMPTY)

    def test_an_outcome_naming_another_rail_is_refused(self) -> None:
        error = self.refused(outcome=invocation_outcome(rail=SOURCE_RAIL))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_RAIL)

    def test_a_non_orchestrator_outcome_role_is_refused(self) -> None:
        for role in ("executor", "reviewer", "evidence-worker", ""):
            with self.subTest(role=role):
                error = self.refused(outcome=invocation_outcome(role=role))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_ROLE)

    def test_a_standing_rail_absent_before_is_refused(self) -> None:
        thin = snapshot(
            HEAD_BEFORE,
            (rail(SOURCE_RAIL, blob=SOURCE_BLOB, proposed="completed", handoff="h" * 40),),
        )
        error = self.refused(before=thin)
        self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_BEFORE)

    def test_another_iteration_of_the_same_rail_is_refused(self) -> None:
        """The rail was rewritten; an authorization read at one blob is not this one."""
        error = self.refused(outcome=invocation_outcome(iteration_blob="0" * 40))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_ITERATION)

    def test_wake_rails_must_match_the_proposal_exactly(self) -> None:
        for wake_rails in ((), (REVIEW_RAIL,), (SOURCE_RAIL, REVIEW_RAIL)):
            with self.subTest(wake_rails=wake_rails):
                error = self.refused(outcome=invocation_outcome(wake_rails=wake_rails))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_WAKE_RAILS)

    def test_wake_rails_ordering_is_part_of_the_identity(self) -> None:
        reasons = (reason(rail_id=SOURCE_RAIL), reason(rail_id=REVIEW_RAIL))
        both = self.before(
            rail(REVIEW_RAIL, blob="7" * 40, proposed="completed", handoff="i" * 40)
        )
        error = self.refused(
            before=both,
            proposal=proposal(reasons),
            outcome=invocation_outcome(wake_rails=(SOURCE_RAIL, REVIEW_RAIL)),
        )
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_WAKE_RAILS)

    def test_a_nonterminal_binding_reconciles_nothing(self) -> None:
        for state in (BINDING_STATE_RESERVED, BINDING_STATE_BOUND):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_an_unproven_process_group_is_refused(self) -> None:
        for value in (False, None, 1, "yes"):
            with self.subTest(value=value):
                error = self.refused(outcome=invocation_outcome(process_group_gone=value))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_PROCESS_ALIVE)

    def test_an_ungraceful_shutdown_is_refused(self) -> None:
        for value in (False, None, 1, "yes"):
            with self.subTest(value=value):
                error = self.refused(outcome=invocation_outcome(graceful=value))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_UNGRACEFUL)

    def test_an_empty_session_id_is_refused(self) -> None:
        error = self.refused(outcome=invocation_outcome(session_id="   "))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_SHAPE)


# --------------------------------------------------------------------------
# Terminality is an allowlist, never "absent from today's nonterminal set"
# --------------------------------------------------------------------------


class ExactTerminalStateTests(OutcomeTestBase):
    """Only the canonical `unbound` proves a session finished.

    A denylist would let an unknown state through as terminal, and an unknown
    state is unproven -- the one thing a reconciliation gate may not assume.
    """

    def test_the_canonical_terminal_state_succeeds(self) -> None:
        report = self.run_outcome(outcome=invocation_outcome(binding_state=BINDING_STATE_UNBOUND))
        self.assertTrue(report.reconciled)

    def test_the_canonical_vocabulary_still_has_exactly_one_terminal_state(self) -> None:
        """If a second terminal state ever lands, this gate must be revisited, not guessed."""
        terminal = [s for s in BINDING_STATES if s not in NONTERMINAL_BINDING_STATES]
        self.assertEqual(terminal, [BINDING_STATE_UNBOUND])

    def test_a_nonterminal_state_cannot_establish_terminality(self) -> None:
        for state in (BINDING_STATE_RESERVED, BINDING_STATE_BOUND):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_an_unrecognized_state_is_unproven_rather_than_terminal(self) -> None:
        for state in ("completed", "terminated", "gone", "garbage", "done", "finished", "closed"):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)
                self.assertNotIn(state, NONTERMINAL_BINDING_STATES)

    def test_a_whitespace_decorated_or_recased_state_is_not_the_canonical_one(self) -> None:
        for state in (" unbound", "unbound ", "\tunbound", "unbound\n", "UNBOUND", "Unbound"):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_an_empty_or_whitespace_state_is_refused(self) -> None:
        for state in ("", "   ", "\t", "\n"):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_a_non_string_state_is_refused(self) -> None:
        for state in (None, 0, 1, True, object(), ["unbound"], ("unbound",)):
            with self.subTest(state=state):
                error = self.refused(outcome=invocation_outcome(binding_state=state))
                self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_a_string_subclass_cannot_pass_as_the_canonical_state(self) -> None:
        """It compares equal and formats identically; it was still never carried."""

        class BindingState(str):
            pass

        substitute = BindingState(BINDING_STATE_UNBOUND)
        self.assertEqual(substitute, BINDING_STATE_UNBOUND)
        error = self.refused(outcome=invocation_outcome(binding_state=substitute))
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)

    def test_the_refusal_precedes_any_durable_reason_evaluation(self) -> None:
        """A later gate would also refuse; the terminal-state one must answer first."""
        error = self.refused(
            outcome=invocation_outcome(binding_state="garbage"),
            after=self.after(None),
        )
        self.assertEqual(error.reason, outcome_module.REASON_OUTCOME_NONTERMINAL)
        self.assertNotEqual(error.reason, outcome_module.REASON_SOURCE_RAIL_MISSING)


# --------------------------------------------------------------------------
# Gate two: a fresh durable effect
# --------------------------------------------------------------------------


class FreshDurableEffectTests(OutcomeTestBase):
    """Movement is not reconciliation. Every reason is read on its own source rail."""

    def test_the_same_head_is_never_reconciliation(self) -> None:
        stale = snapshot(
            HEAD_BEFORE, (standing(), self.reconciled_source()), state_blob=STATE_BLOB_AFTER
        )
        error = self.refused(after=stale)
        self.assertEqual(error.reason, outcome_module.REASON_SNAPSHOT_SAME_HEAD)

    def test_a_cross_scope_post_snapshot_is_refused(self) -> None:
        for field, value in (("project", "other-project"), ("ticket", "issue-99")):
            with self.subTest(field=field):
                foreign = self.after(self.reconciled_source(), **{field: value})
                error = self.refused(after=foreign)
                self.assertEqual(error.reason, outcome_module.REASON_SNAPSHOT_SCOPE)

    def test_a_post_snapshot_of_the_wrong_type_is_refused(self) -> None:
        error = self.refused(after=object())
        self.assertEqual(error.reason, outcome_module.REASON_SNAPSHOT_SCOPE)

    def test_head_and_state_moving_while_the_reason_stays_material_is_unresolved(self) -> None:
        """The orchestrator published *something*; that is not this reason's resolution."""
        still_material = rail(
            SOURCE_RAIL, blob=SOURCE_BLOB, status="running", proposed="completed",
            handoff="h" * 40,
        )
        report = self.run_outcome(after=self.after(still_material))
        self.assertFalse(report.reconciled)
        self.assertEqual(report.unresolved_rails, (SOURCE_RAIL,))
        self.assertNotEqual(report.before_head, report.after_head)

    def test_a_new_unreconciled_handoff_is_follow_up_not_success(self) -> None:
        reopened = rail(
            SOURCE_RAIL, blob=SOURCE_BLOB, status="completed", proposed="blocked",
            handoff="j" * 40,
        )
        report = self.run_outcome(after=self.after(reopened))
        self.assertFalse(report.reconciled)
        self.assertEqual(report.unresolved_rails, (SOURCE_RAIL,))


# --------------------------------------------------------------------------
# The three resolution rules
# --------------------------------------------------------------------------


class ResolutionRuleTests(OutcomeTestBase):
    def _report(self, kind: str, after_source: RailSnapshot) -> ReconciliationReport:
        return self.run_outcome(
            proposal=proposal((reason(kind=kind),)),
            after=self.after(after_source),
        )

    def test_unreconciled_handoff_resolves_only_when_reconciled(self) -> None:
        resolved = self._report(WAKE_UNRECONCILED_HANDOFF, self.reconciled_source())
        self.assertTrue(resolved.reconciled)
        unresolved = self._report(
            WAKE_UNRECONCILED_HANDOFF,
            rail(SOURCE_RAIL, blob=SOURCE_BLOB, proposed="completed", handoff="h" * 40),
        )
        self.assertFalse(unresolved.reconciled)

    def test_invocation_ended_resolves_only_with_a_reconciled_handoff(self) -> None:
        resolved = self._report(LIFECYCLE_INVOCATION_ENDED, self.reconciled_source())
        self.assertTrue(resolved.reconciled)

        no_handoff = self._report(
            LIFECYCLE_INVOCATION_ENDED, rail(SOURCE_RAIL, blob=SOURCE_BLOB, status="completed")
        )
        self.assertFalse(no_handoff.reconciled)

        unreconciled = self._report(
            LIFECYCLE_INVOCATION_ENDED,
            rail(SOURCE_RAIL, blob=SOURCE_BLOB, proposed="blocked", handoff="h" * 40),
        )
        self.assertFalse(unreconciled.reconciled)

    def test_disconnected_resolves_only_when_the_rail_stops_running(self) -> None:
        for status in ("completed", "blocked", "ready"):
            with self.subTest(status=status):
                report = self._report(
                    LIFECYCLE_BECAME_DISCONNECTED,
                    rail(SOURCE_RAIL, blob=SOURCE_BLOB, status=status),
                )
                self.assertTrue(report.reconciled)

        still_running = self._report(
            LIFECYCLE_BECAME_DISCONNECTED, rail(SOURCE_RAIL, blob=SOURCE_BLOB, status="running")
        )
        self.assertFalse(still_running.reconciled)

    def test_every_trigger_wake_kind_has_a_rule(self) -> None:
        """A kind the trigger can emit but this module cannot classify would fail closed."""
        from ai_dev_flow.orchestrator_trigger import WAKE_KINDS

        self.assertEqual(set(WAKE_KINDS), set(outcome_module.RESOLUTION_RULES))

    def test_an_unsupported_reason_kind_fails_closed(self) -> None:
        invented = WakeReason(
            kind="looks-reasonable", rail=SOURCE_RAIL, fingerprint=("looks-reasonable",)
        )
        error = self.refused(proposal=proposal((invented,)))
        self.assertEqual(error.reason, outcome_module.REASON_UNKNOWN_REASON_KIND)

    def test_mixed_reasons_report_each_rail_independently(self) -> None:
        before = self.before(
            rail(REVIEW_RAIL, blob="7" * 40, status="running")
        )
        reasons = (
            reason(kind=WAKE_UNRECONCILED_HANDOFF, rail_id=SOURCE_RAIL),
            reason(kind=LIFECYCLE_BECAME_DISCONNECTED, rail_id=REVIEW_RAIL),
        )
        after = snapshot(
            HEAD_AFTER,
            (
                standing(),
                self.reconciled_source(),
                rail(REVIEW_RAIL, blob="7" * 40, status="running"),
            ),
            state_blob=STATE_BLOB_AFTER,
        )
        report = self.run_outcome(
            before=before,
            proposal=proposal(reasons),
            after=after,
            outcome=invocation_outcome(wake_rails=tuple(sorted((SOURCE_RAIL, REVIEW_RAIL)))),
        )
        self.assertFalse(report.reconciled)
        self.assertEqual(report.unresolved_rails, (REVIEW_RAIL,))
        self.assertEqual(
            {(e.rail, e.resolved) for e in report.resolutions},
            {(SOURCE_RAIL, True), (REVIEW_RAIL, False)},
        )


# --------------------------------------------------------------------------
# Source and standing-rail fail-closed behavior
# --------------------------------------------------------------------------


class FailClosedRailTests(OutcomeTestBase):
    def test_a_source_rail_that_vanished_fails_closed(self) -> None:
        error = self.refused(after=self.after(None))
        self.assertEqual(error.reason, outcome_module.REASON_SOURCE_RAIL_MISSING)

    def test_a_standing_rail_that_vanished_fails_closed(self) -> None:
        gone = snapshot(HEAD_AFTER, (self.reconciled_source(),), state_blob=STATE_BLOB_AFTER)
        error = self.refused(after=gone)
        self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_AFTER)

    def test_a_standing_rail_left_not_running_fails_closed(self) -> None:
        for status in ("completed", "blocked", "ready"):
            with self.subTest(status=status):
                error = self.refused(
                    after=self.after(self.reconciled_source(), standing_rail=standing(status=status))
                )
                self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_AFTER)

    def test_a_standing_rail_left_unreconciled_fails_closed(self) -> None:
        error = self.refused(
            after=self.after(
                self.reconciled_source(), standing_rail=standing(proposed="completed")
            )
        )
        self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_AFTER)

    def test_a_standing_rail_reassigned_elsewhere_fails_closed(self) -> None:
        for role in ("executor", "reviewer", "evidence-worker", None):
            with self.subTest(role=role):
                error = self.refused(
                    after=self.after(
                        self.reconciled_source(), standing_rail=standing(role=role)
                    )
                )
                self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_AFTER)

    def test_the_standing_rail_is_checked_even_when_every_reason_resolved(self) -> None:
        error = self.refused(
            after=self.after(self.reconciled_source(), standing_rail=standing(status="blocked"))
        )
        self.assertEqual(error.reason, outcome_module.REASON_STANDING_RAIL_AFTER)


# --------------------------------------------------------------------------
# A real Git control plane: no content, no writes
# --------------------------------------------------------------------------


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
        self.write_state()

    def _write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_state(self, body: str = STATE_SECRET) -> None:
        self._write(
            "{0}/{1}/state.md".format(PROJECT, TICKET),
            "# Control Plane State\n\n## Accepted State\n\n- {0}\n".format(body),
        )

    def write_rail(
        self,
        rail_id: str,
        *,
        status: str = "running",
        role: Optional[str] = "executor",
        resource: str = WORK_RESOURCE,
        body: str = RAIL_SECRET,
    ) -> None:
        header = "# Rail: {0}\n\nStatus: {1}\nOwner: orchestrator\n".format(rail_id, status)
        if role is not None:
            header += "Role: {0}\n".format(role)
        header += "Shared resource: {0}\n\n## Goal\n\n{1}\n".format(resource, body)
        self._write("{0}/{1}/rails/{2}/rail.md".format(PROJECT, TICKET, rail_id), header)

    def write_handoff(self, rail_id: str, *, status: str, body: str = HANDOFF_SECRET) -> None:
        self._write(
            "{0}/{1}/rails/{2}/handoff.md".format(PROJECT, TICKET, rail_id),
            "# Rail Handoff: {0}\n\nStatus: {1}\n\n## Delivered\n\n{2}\n".format(
                rail_id, status, body
            ),
        )

    def write_evidence(self, rail_id: str, *, body: str = EVIDENCE_SECRET) -> None:
        self._write(
            "{0}/{1}/rails/{2}/evidence.json".format(PROJECT, TICKET, rail_id),
            '{{"schemaVersion": 1, "observations": [{{"kind": "{0}"}}]}}\n'.format(body),
        )

    def commit(self, message: str = "publish") -> str:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "--quiet", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def source(self) -> ReadSource:
        resolved = _git(self.root, "rev-parse", "HEAD")
        return ReadSource(self.root, resolved, resolved)

    def snapshot(self) -> ScopeSnapshot:
        return build_snapshot(self.source(), project=PROJECT, ticket=TICKET)

    def blob(self, relative: str) -> str:
        return _git(self.root, "rev-parse", "HEAD:{0}".format(relative))

    def rail_blob(self, rail_id: str) -> str:
        return self.blob("{0}/{1}/rails/{2}/rail.md".format(PROJECT, TICKET, rail_id))


class GitBackedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="outcome-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plane = ControlPlaneFixture(self.tmp / "control-plane")

    def arrange_wake(self) -> None:
        """One unreconciled executor handoff plus a healthy standing orchestrator rail."""
        self.plane.write_rail(ORCH_RAIL, role="orchestrator", resource=ORCH_RESOURCE)
        self.plane.write_rail(SOURCE_RAIL, role="executor", resource=WORK_RESOURCE)
        self.plane.write_handoff(SOURCE_RAIL, status="completed")
        self.plane.write_evidence(SOURCE_RAIL)
        self.plane.commit("wake")


class NoContentAndNoWriteTests(GitBackedTestCase):
    """The report is identity only, and reconciliation is a read."""

    def setUp(self) -> None:
        super().setUp()
        self.arrange_wake()
        self.before_snapshot = self.plane.snapshot()
        self.wake = propose_wake(self.before_snapshot)
        self.plane.write_rail(SOURCE_RAIL, status="completed", role="executor")
        self.plane.commit("accept")
        self.after_snapshot = self.plane.snapshot()
        self.outcome = invocation_outcome(
            head=self.before_snapshot.head,
            iteration_blob=self.before_snapshot.rail(ORCH_RAIL).authorization_blob,
            wake_rails=self.wake.rails,
        )

    def _reconcile(self) -> ReconciliationReport:
        return reconcile_outcome(
            self.outcome,
            before=self.before_snapshot,
            proposal=self.wake,
            after=self.after_snapshot,
            orchestrator_rail=ORCH_RAIL,
        )

    def test_the_report_carries_no_artifact_prose(self) -> None:
        report = self._reconcile()
        self.assertTrue(report.reconciled)
        found = _strings(report)
        for secret in (STATE_SECRET, RAIL_SECRET, HANDOFF_SECRET, EVIDENCE_SECRET):
            for text in found:
                self.assertNotIn(secret, text)

    def test_reconciling_writes_nothing_to_the_control_plane(self) -> None:
        root = self.plane.root
        head = _git(root, "rev-parse", "HEAD")
        status = _git(root, "status", "--porcelain")
        refs = _git(root, "for-each-ref", "--format=%(refname) %(objectname)")

        self._reconcile()

        self.assertEqual(_git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(root, "status", "--porcelain"), status)
        self.assertEqual(_git(root, "for-each-ref", "--format=%(refname) %(objectname)"), refs)

    def test_reconciling_mutates_no_trigger_cursor(self) -> None:
        cursor = TriggerCursor()
        for entry in self.wake.reasons:
            cursor.remember(entry)
        remembered = set(cursor._seen)
        self._reconcile()
        self.assertEqual(set(cursor._seen), remembered)

    def test_the_report_and_its_resolutions_are_frozen(self) -> None:
        report = self._reconcile()
        with self.assertRaises(Exception):
            report.session_id = "rewritten"  # type: ignore[misc]
        with self.assertRaises(Exception):
            report.resolutions[0].resolved = False  # type: ignore[misc]

    def test_the_module_exposes_no_publication_or_retry_surface(self) -> None:
        """Publication and judgment stay with the fresh orchestrator, not here."""
        public = [name for name in dir(outcome_module) if not name.startswith("_")]
        for name in public:
            lowered = name.lower()
            for forbidden in ("publish", "write", "commit", "retry", "decide", "judge"):
                self.assertNotIn(forbidden, lowered, name)

    def test_the_module_never_names_a_provider_content_surface(self) -> None:
        """No field of an outcome carries provider text; nothing here may reach for one."""
        source = Path(outcome_module.__file__).read_text(encoding="utf-8")
        for surface in ("assistant", "transcript", "tool_call", "result_text", "worker"):
            self.assertNotIn(surface, source)

    def test_a_before_snapshot_or_proposal_of_the_wrong_type_is_refused(self) -> None:
        for field in ("before", "proposal"):
            with self.subTest(field=field):
                with self.assertRaises(OutcomeError) as caught:
                    reconcile_outcome(
                        self.outcome,
                        **{
                            "before": self.before_snapshot,
                            "proposal": self.wake,
                            "after": self.after_snapshot,
                            "orchestrator_rail": ORCH_RAIL,
                            field: object(),
                        }
                    )
                self.assertEqual(caught.exception.reason, outcome_module.REASON_OUTCOME_SHAPE)

    def test_the_report_holds_only_identity_and_classification_fields(self) -> None:
        report = self._reconcile()
        self.assertEqual(
            set(report.__dataclass_fields__),
            {"project", "ticket", "before_head", "after_head", "rail", "session_id", "resolutions"},
        )
        self.assertEqual(
            set(report.resolutions[0].__dataclass_fields__), {"kind", "rail", "resolved"}
        )


# --------------------------------------------------------------------------
# The composed, provider-free fresh-review loop
# --------------------------------------------------------------------------


class FreshReviewLoopTests(GitBackedTestCase):
    """Handoff -> fresh orchestrator -> reconciled source plus a reviewer rail -> new wake."""

    def observation(self, scope: ScopeSnapshot) -> ControlPlaneObservation:
        """Reduce a snapshot to the accepted observation the predicate already takes."""
        rails = []
        for entry in scope.rails:
            resource = {
                ORCH_RAIL: ORCH_RESOURCE,
                SOURCE_RAIL: WORK_RESOURCE,
                REVIEW_RAIL: REVIEW_RESOURCE,
            }[entry.identifier]
            rails.append(
                RailObservation(
                    identifier=entry.identifier,
                    status=entry.status,
                    rail_blob=entry.authorization_blob,
                    role=entry.role,
                    unreconciled=entry.unreconciled,
                    shared_resource=resource,
                )
            )
        return ControlPlaneObservation(
            project=scope.project,
            ticket=scope.ticket,
            head=scope.head,
            rails=tuple(rails),
            workspace=WorkspaceObservation(
                workspace_key="ai-dev/issue-55",
                worktree_id="ai-dev-issue-55",
                workspace_path="/workspace/ai-dev-issue-55",
            ),
        )

    def test_the_checkpoint_three_loop_closes_without_a_provider(self) -> None:
        # 1. An unreconciled executor handoff yields one material wake and a packet.
        self.arrange_wake()
        before = self.plane.snapshot()
        cursor = TriggerCursor()
        wake = propose_wake(before, cursor=cursor)

        self.assertIsNotNone(wake)
        self.assertEqual(wake.rails, (SOURCE_RAIL,))
        self.assertEqual({r.kind for r in wake.reasons}, {WAKE_UNRECONCILED_HANDOFF})
        packet = build_packet(before)
        self.assertEqual(packet.head, before.head)
        self.assertEqual(packet.role, "orchestrator")
        self.assertEqual(packet.session_mode, "new")

        # 2. The orchestrator publishes: the source handoff is accepted, and it
        #    authorizes a distinct bounded reviewer rail in the same publication.
        self.plane.write_rail(SOURCE_RAIL, status="completed", role="executor")
        self.plane.write_rail(REVIEW_RAIL, status="running", role="reviewer", resource=REVIEW_RESOURCE)
        self.plane.commit("accept and commission review")
        after = self.plane.snapshot()

        outcome = invocation_outcome(
            head=before.head,
            iteration_blob=before.rail(ORCH_RAIL).authorization_blob,
            wake_rails=wake.rails,
        )
        report = reconcile_outcome(
            outcome, before=before, proposal=wake, after=after, orchestrator_rail=ORCH_RAIL
        )
        self.assertTrue(report.reconciled)
        self.assertEqual(report.unresolved_rails, ())

        # 3. The reviewer rail is distinct, reconciled, running, and exactly `reviewer`.
        reviewer = after.rail(REVIEW_RAIL)
        self.assertIsNotNone(reviewer)
        self.assertNotEqual(reviewer.identifier, ORCH_RAIL)
        self.assertNotEqual(reviewer.identifier, SOURCE_RAIL)
        self.assertEqual(reviewer.role, "reviewer")
        self.assertEqual(reviewer.status, "running")
        self.assertFalse(reviewer.unreconciled)

        # 4. The existing generic predicate authorizes a fresh reviewer launch.
        decision = authorize(
            self.observation(after),
            project=PROJECT,
            ticket=TICKET,
            rail=REVIEW_RAIL,
            role="reviewer",
            expected_head=after.head,
            rail_blob=reviewer.authorization_blob,
        )
        self.assertTrue(decision.authorized, decision.detail)
        self.assertEqual(decision.action, ACTION_LAUNCH)

        # 5. The reviewer's own handoff becomes unreconciled and wakes another
        #    fresh orchestrator -- a new wake, not a continuation of the first.
        self.plane.write_handoff(REVIEW_RAIL, status="completed")
        self.plane.commit("reviewer reports")
        third = self.plane.snapshot()

        second_wake = propose_wake(third, cursor=cursor)
        self.assertIsNotNone(second_wake)
        self.assertEqual(second_wake.rails, (REVIEW_RAIL,))
        self.assertEqual({r.kind for r in second_wake.reasons}, {WAKE_UNRECONCILED_HANDOFF})
        self.assertNotEqual(second_wake.head, wake.head)

        second_packet = build_packet(third)
        self.assertEqual(second_packet.session_mode, "new")
        self.assertNotEqual(second_packet.head, packet.head)

    def test_a_lifecycle_wake_completes_the_same_round_trip(self) -> None:
        """The disconnected rule closes through the same seams, with no new primitive."""
        self.plane.write_rail(ORCH_RAIL, role="orchestrator", resource=ORCH_RESOURCE)
        self.plane.write_rail(SOURCE_RAIL, status="running", role="executor")
        self.plane.commit("dispatch")
        before = self.plane.snapshot()

        # No handoff exists, so nothing wakes from the handoff rule; the wake comes
        # only from the injected transition, tied to this exact head and iteration.
        self.assertIsNone(propose_wake(before))
        fact = LifecycleFact(
            kind=LIFECYCLE_BECAME_DISCONNECTED,
            rail=SOURCE_RAIL,
            session_id=SESSION_ID,
            iteration=before.rail(SOURCE_RAIL).authorization_blob,
            head=before.head,
        )
        wake = propose_wake(before, lifecycle_facts=(fact,))
        self.assertIsNotNone(wake)
        self.assertEqual(wake.rails, (SOURCE_RAIL,))
        self.assertEqual({r.kind for r in wake.reasons}, {LIFECYCLE_BECAME_DISCONNECTED})

        self.plane.write_rail(SOURCE_RAIL, status="blocked", role="executor")
        self.plane.commit("stand the rail down")
        after = self.plane.snapshot()

        report = reconcile_outcome(
            invocation_outcome(
                head=before.head,
                iteration_blob=before.rail(ORCH_RAIL).authorization_blob,
                wake_rails=wake.rails,
            ),
            before=before,
            proposal=wake,
            after=after,
            orchestrator_rail=ORCH_RAIL,
        )
        self.assertTrue(report.reconciled)
        self.assertEqual(
            [(e.kind, e.rail, e.resolved) for e in report.resolutions],
            [(LIFECYCLE_BECAME_DISCONNECTED, SOURCE_RAIL, True)],
        )


if __name__ == "__main__":
    unittest.main()
