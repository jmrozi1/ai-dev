"""Whether one fresh orchestrator invocation actually reconciled what woke it."""

from __future__ import annotations

# An invocation that returns cleanly has proved that a session ran, and nothing
# else. Whether the durable facts that justified spending it were actually
# resolved is a separate claim, and checkpoint 3 is only closed when the
# controller can make that claim from evidence rather than from the invocation
# having ended.
#
# Four boundaries hold this module honest.
#
# First, the outcome must be the exact invocation the proposal asked for. Same
# scope, same pre-invocation head, same dedicated rail at the same iteration
# blob, the same sorted wake rails, role `orchestrator`, and a session that
# terminalized with its process group proven gone. Anything missing, mismatched,
# nonterminal, or merely truthy is refused, because a near-miss outcome
# reconciling a proposal it did not run is exactly the confusion this seam exists
# to prevent.
#
# Second, the durable effect is read from a *newly resolved* snapshot at a
# different published head. The accepted state blob moving, or the head moving,
# is never reconciliation on its own: an orchestrator may publish anything at a
# new head, including work unrelated to what woke it. Every original reason is
# evaluated on its own source rail, and only the parsed identity/status facts the
# trigger seam already surfaces are consulted.
#
# Third, an unresolved reason is a report, not an error and not a retry. A rail
# that carries a new unreconciled handoff is follow-up work; saying so is the
# whole product of this module. What fails closed instead is anything that would
# make the report itself untrustworthy -- a missing source rail, a cross-scope or
# same-head snapshot, an unsupported reason kind, or a fact that cannot be tied
# back to the original proposal.
#
# Fourth, this module reads. It publishes no artifact, mutates no cursor, retries
# nothing, and never turns an unresolved outcome into a judgment about what
# should happen next. Publication and judgment belong to the fresh orchestrator
# through the existing control-plane contract; the controller only verifies that
# the effects it can identify are there.

from dataclasses import dataclass
from typing import Tuple

from .orchestrator_invocation import ORCHESTRATOR_ROLE, InvocationOutcome
from .orchestrator_trigger import (
    LIFECYCLE_BECAME_DISCONNECTED,
    LIFECYCLE_INVOCATION_ENDED,
    WAKE_UNRECONCILED_HANDOFF,
    RailSnapshot,
    ScopeSnapshot,
    WakeProposal,
)
from .session_binding import NONTERMINAL_BINDING_STATES

__all__ = [
    "OutcomeError",
    "ReasonResolution",
    "ReconciliationReport",
    "RESOLUTION_RULES",
    "reconcile_outcome",
]

# The status that means a rail is still the authorization being run. Matched to
# `authorization.DISPATCHED_RAIL_STATUS` and `control_plane.RAIL_STATUSES`; the
# disconnected rule is the only one that reads it.
RUNNING_RAIL_STATUS = "running"

# Stable refusal reasons, in the order they are evaluated.
REASON_OUTCOME_SHAPE = "outcome-shape-invalid"
REASON_OUTCOME_SCOPE = "outcome-scope-mismatch"
REASON_OUTCOME_HEAD = "outcome-head-mismatch"
REASON_OUTCOME_RAIL = "outcome-rail-mismatch"
REASON_OUTCOME_ROLE = "outcome-role-mismatch"
REASON_OUTCOME_ITERATION = "outcome-iteration-mismatch"
REASON_OUTCOME_WAKE_RAILS = "outcome-wake-rails-mismatch"
REASON_OUTCOME_NONTERMINAL = "outcome-binding-nonterminal"
REASON_OUTCOME_PROCESS_ALIVE = "outcome-process-group-unproven"
REASON_OUTCOME_UNGRACEFUL = "outcome-shutdown-ungraceful"
REASON_PROPOSAL_SCOPE = "proposal-scope-mismatch"
REASON_PROPOSAL_EMPTY = "proposal-has-no-reasons"
REASON_STANDING_RAIL_BEFORE = "standing-rail-missing-before"
REASON_SNAPSHOT_SCOPE = "post-snapshot-scope-mismatch"
REASON_SNAPSHOT_SAME_HEAD = "post-snapshot-same-head"
REASON_SOURCE_RAIL_MISSING = "source-rail-missing-after"
REASON_UNKNOWN_REASON_KIND = "unsupported-wake-reason"
REASON_STANDING_RAIL_AFTER = "standing-rail-unhealthy-after"


class OutcomeError(Exception):
    """A refusal to report on an outcome, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def _exact_text(value: object, *, label: str, reason: str) -> str:
    """Exactly a non-empty `str`.

    A subclass, a lazily-rendered object, or anything else that merely formats
    like an identity is refused: the whole point of these gates is that the
    identity was carried, not reconstructed.
    """
    if type(value) is not str or not value.strip():
        raise OutcomeError(
            reason, "{0} must be exact non-empty text, got {1!r}".format(label, value)
        )
    return value


def _exact_true(value: object, *, label: str, reason: str) -> None:
    """Exactly `True`. A truthy substitute never proves a process gone."""
    if value is not True:
        raise OutcomeError(
            reason, "{0} must be exactly True, got {1!r}".format(label, value)
        )


# --------------------------------------------------------------------------
# Gate one: this is the exact invocation the proposal asked for
# --------------------------------------------------------------------------


def _require_exact_invocation(
    outcome: InvocationOutcome,
    before: ScopeSnapshot,
    proposal: WakeProposal,
    orchestrator_rail: str,
) -> RailSnapshot:
    """Refuse anything that is not the run this proposal authorized, at that head."""
    if not isinstance(outcome, InvocationOutcome):
        raise OutcomeError(
            REASON_OUTCOME_SHAPE,
            "an outcome must be an InvocationOutcome the accepted seam produced, got "
            "{0!r}".format(type(outcome).__name__),
        )
    if not isinstance(before, ScopeSnapshot) or not isinstance(proposal, WakeProposal):
        raise OutcomeError(
            REASON_OUTCOME_SHAPE,
            "the before-snapshot and proposal must be the accepted trigger types",
        )

    rail_id = _exact_text(orchestrator_rail, label="orchestrator rail", reason=REASON_OUTCOME_RAIL)

    if (proposal.project, proposal.ticket) != (before.project, before.ticket):
        raise OutcomeError(
            REASON_PROPOSAL_SCOPE,
            "proposal addresses {0}/{1}; before-snapshot is {2}/{3}".format(
                proposal.project, proposal.ticket, before.project, before.ticket
            ),
        )
    if proposal.head != before.head:
        raise OutcomeError(
            REASON_OUTCOME_HEAD,
            "proposal is bound to head {0}; before-snapshot is {1}".format(
                proposal.head, before.head
            ),
        )
    if not proposal.reasons:
        raise OutcomeError(
            REASON_PROPOSAL_EMPTY,
            "a proposal with no reasons authorizes nothing to verify",
        )

    if (outcome.project, outcome.ticket) != (before.project, before.ticket):
        raise OutcomeError(
            REASON_OUTCOME_SCOPE,
            "outcome addresses {0}/{1}; before-snapshot is {2}/{3}".format(
                outcome.project, outcome.ticket, before.project, before.ticket
            ),
        )
    if _exact_text(outcome.head, label="outcome head", reason=REASON_OUTCOME_HEAD) != before.head:
        raise OutcomeError(
            REASON_OUTCOME_HEAD,
            "outcome ran at head {0}; the proposal was made at {1}".format(
                outcome.head, before.head
            ),
        )
    if _exact_text(outcome.rail, label="outcome rail", reason=REASON_OUTCOME_RAIL) != rail_id:
        raise OutcomeError(
            REASON_OUTCOME_RAIL,
            "outcome names rail '{0}'; the dedicated rail is '{1}'".format(outcome.rail, rail_id),
        )
    if outcome.role != ORCHESTRATOR_ROLE:
        raise OutcomeError(
            REASON_OUTCOME_ROLE,
            "outcome carries role '{0}'; only '{1}' reconciles a wake".format(
                outcome.role, ORCHESTRATOR_ROLE
            ),
        )

    standing = before.rail(rail_id)
    if standing is None:
        raise OutcomeError(
            REASON_STANDING_RAIL_BEFORE,
            "no dedicated orchestrator rail '{0}' at head {1}".format(rail_id, before.head),
        )
    if outcome.iteration_blob != standing.authorization_blob:
        raise OutcomeError(
            REASON_OUTCOME_ITERATION,
            "outcome ran iteration {0}; head {1} authorizes {2}".format(
                outcome.iteration_blob, before.head, standing.authorization_blob
            ),
        )

    if tuple(outcome.wake_rails) != proposal.rails:
        raise OutcomeError(
            REASON_OUTCOME_WAKE_RAILS,
            "outcome reconciles rails {0}; the proposal named {1}".format(
                tuple(outcome.wake_rails), proposal.rails
            ),
        )

    state = _exact_text(
        outcome.binding_state, label="binding state", reason=REASON_OUTCOME_NONTERMINAL
    )
    if state in NONTERMINAL_BINDING_STATES:
        raise OutcomeError(
            REASON_OUTCOME_NONTERMINAL,
            "binding is '{0}'; a nonterminal session has not finished and reconciles "
            "nothing".format(state),
        )
    _exact_true(
        outcome.process_group_gone,
        label="process_group_gone",
        reason=REASON_OUTCOME_PROCESS_ALIVE,
    )
    _exact_true(outcome.graceful, label="graceful", reason=REASON_OUTCOME_UNGRACEFUL)

    _exact_text(outcome.session_id, label="session id", reason=REASON_OUTCOME_SHAPE)
    return standing


# --------------------------------------------------------------------------
# Gate two: a fresh durable effect, read per original reason
# --------------------------------------------------------------------------


def _resolved_unreconciled_handoff(rail: RailSnapshot) -> bool:
    """The source handoff the orchestrator woke for is settled.

    A rail carrying a *new* unreconciled handoff reads as unresolved on purpose:
    that is the next wake's work, not evidence that this one succeeded.
    """
    return not rail.unreconciled


def _resolved_invocation_ended(rail: RailSnapshot) -> bool:
    """The session that ended without a handoff now has a reconciled one."""
    return rail.proposed_status is not None and not rail.unreconciled


def _resolved_became_disconnected(rail: RailSnapshot) -> bool:
    """The rail whose binding went Disconnected is no longer the running authorization."""
    return rail.status != RUNNING_RAIL_STATUS


# One rule per wake kind the trigger seam can produce. A kind with no rule fails
# closed rather than defaulting to resolved or to unresolved; both defaults would
# be a judgment this module is not allowed to make.
RESOLUTION_RULES = {
    WAKE_UNRECONCILED_HANDOFF: _resolved_unreconciled_handoff,
    LIFECYCLE_INVOCATION_ENDED: _resolved_invocation_ended,
    LIFECYCLE_BECAME_DISCONNECTED: _resolved_became_disconnected,
}


def _require_fresh_effect(before: ScopeSnapshot, after: ScopeSnapshot) -> None:
    """The post-snapshot must describe the same scope at a genuinely newer read."""
    if not isinstance(after, ScopeSnapshot):
        raise OutcomeError(
            REASON_SNAPSHOT_SCOPE,
            "the post-invocation snapshot must be a resolved ScopeSnapshot",
        )
    if (after.project, after.ticket) != (before.project, before.ticket):
        raise OutcomeError(
            REASON_SNAPSHOT_SCOPE,
            "post-snapshot is {0}/{1}; the invocation was for {2}/{3}".format(
                after.project, after.ticket, before.project, before.ticket
            ),
        )
    if after.head == before.head:
        raise OutcomeError(
            REASON_SNAPSHOT_SAME_HEAD,
            "post-snapshot is still head {0}; nothing was published to verify".format(after.head),
        )


def _require_healthy_standing_rail(after: ScopeSnapshot, rail_id: str) -> None:
    """The dedicated rail must survive publication still able to authorize the next wake."""
    rail = after.rail(rail_id)
    if rail is None:
        raise OutcomeError(
            REASON_STANDING_RAIL_AFTER,
            "dedicated orchestrator rail '{0}' is gone at head {1}".format(rail_id, after.head),
        )
    if rail.role != ORCHESTRATOR_ROLE:
        raise OutcomeError(
            REASON_STANDING_RAIL_AFTER,
            "dedicated rail '{0}' is now assigned to '{1}', not '{2}'".format(
                rail_id, rail.role or "no role", ORCHESTRATOR_ROLE
            ),
        )
    if rail.status != RUNNING_RAIL_STATUS:
        raise OutcomeError(
            REASON_STANDING_RAIL_AFTER,
            "dedicated rail '{0}' is '{1}'; a later wake would not be authorizable".format(
                rail_id, rail.status
            ),
        )
    if rail.unreconciled:
        raise OutcomeError(
            REASON_STANDING_RAIL_AFTER,
            "dedicated rail '{0}' now carries an unreconciled handoff; its authorization is "
            "not settled".format(rail_id),
        )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReasonResolution:
    """One original wake reason and whether its durable effect is present."""

    kind: str
    rail: str
    resolved: bool


@dataclass(frozen=True)
class ReconciliationReport:
    """Compact identity and classification facts. No artifact text, no judgment."""

    project: str
    ticket: str
    before_head: str
    after_head: str
    rail: str
    session_id: str
    resolutions: Tuple[ReasonResolution, ...] = ()

    @property
    def reconciled(self) -> bool:
        return all(entry.resolved for entry in self.resolutions)

    @property
    def unresolved_rails(self) -> Tuple[str, ...]:
        return tuple(sorted({e.rail for e in self.resolutions if not e.resolved}))


def reconcile_outcome(
    outcome: InvocationOutcome,
    *,
    before: ScopeSnapshot,
    proposal: WakeProposal,
    after: ScopeSnapshot,
    orchestrator_rail: str,
) -> ReconciliationReport:
    """Report whether every reason that spent this session was durably resolved.

    Raises `OutcomeError` when the question itself cannot be answered honestly --
    a substituted outcome, an unfinished session, a snapshot that moved nowhere or
    describes another scope, a source rail that vanished, a wake kind with no
    rule, or a dedicated rail that publication left unable to authorize the next
    wake. An outcome that is merely *unresolved* is reported, never raised.
    """
    standing = _require_exact_invocation(outcome, before, proposal, orchestrator_rail)
    _require_fresh_effect(before, after)

    resolutions = []
    for reason in proposal.reasons:
        rule = RESOLUTION_RULES.get(reason.kind)
        if rule is None:
            raise OutcomeError(
                REASON_UNKNOWN_REASON_KIND,
                "wake reason '{0}' on rail '{1}' has no resolution rule".format(
                    reason.kind, reason.rail
                ),
            )
        source = after.rail(reason.rail)
        if source is None:
            raise OutcomeError(
                REASON_SOURCE_RAIL_MISSING,
                "rail '{0}' woke this invocation but is absent at head {1}; its resolution "
                "cannot be read".format(reason.rail, after.head),
            )
        resolutions.append(
            ReasonResolution(kind=reason.kind, rail=reason.rail, resolved=bool(rule(source)))
        )

    _require_healthy_standing_rail(after, standing.identifier)

    return ReconciliationReport(
        project=before.project,
        ticket=before.ticket,
        before_head=before.head,
        after_head=after.head,
        rail=standing.identifier,
        session_id=outcome.session_id,
        resolutions=tuple(resolutions),
    )
