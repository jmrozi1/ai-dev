"""When durable facts require one fresh orchestrator, and the smallest packet that starts it."""

from __future__ import annotations

# Checkpoint 2 ends with a controller that can launch, observe, and stop exactly
# one bound session. Checkpoint 3 needs the other half: deciding, without any
# provider or judgment, that something durable has changed enough to be worth one
# fresh orchestrator invocation. That decision has to be deterministic, because a
# wake spends a real Claude session.
#
# Three boundaries hold this module honest.
#
# First, facts come from one already-resolved control-plane revision. Every blob
# id in a snapshot is read from the same revision, so two facts can never be
# tied to different heads and silently compared. Reading Git freshness, parsing
# rails and handoffs, and validating identifiers belong to `control_plane`; this
# module calls those and reimplements none of them.
#
# Second, a snapshot carries identities, never content. Blob ids, statuses, and
# rail slugs are enough to decide that something changed; the accepted state, the
# handoff prose, and the evidence payload are the orchestrator's to read after it
# wakes. Copying them here would make the controller a reader of judgment.
#
# Third, the cursor that suppresses duplicates is in-memory on purpose. A durable
# event log or lease would claim that this controller is the only one allowed to
# reconcile a fact, and would go stale the moment it was wrong. After a restart an
# unreconciled handoff may wake one fresh orchestrator again -- that redundant
# reconciliation is safe, because the orchestrator reads durable state anyway, and
# it is much cheaper than being wrong about a lease.
#
# The packet is deliberately almost empty: project, ticket, head, role, and a
# fixed directive to go read. It cannot resume anything. An orchestrator that
# resumed a prior conversation would be reasoning from accumulated transcript
# instead of durable state, which is the thing checkpoint 3 exists to avoid.

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .control_plane import (
    ReadSource,
    artifact_relative,
    collect_rail_states,
    validate_identifier,
)

__all__ = [
    "DIRECTIVE",
    "LIFECYCLE_KINDS",
    "LifecycleFact",
    "OrchestratorPacket",
    "RailSnapshot",
    "ScopeSnapshot",
    "TriggerCursor",
    "TriggerError",
    "WakeProposal",
    "WakeReason",
    "build_packet",
    "build_snapshot",
    "propose_wake",
    "require_current",
]

ROLE_ORCHESTRATOR = "orchestrator"
SESSION_MODE_NEW = "new"

# The whole instruction. A fresh orchestrator is told where to look and nothing
# about what it will find; anything more would be this module pre-judging.
DIRECTIVE = "Read the current control-plane state for this scope fresh and reconcile it."

LIFECYCLE_INVOCATION_ENDED = "invocation-ended-without-handoff"
LIFECYCLE_BECAME_DISCONNECTED = "running-binding-disconnected"
LIFECYCLE_KINDS = (LIFECYCLE_INVOCATION_ENDED, LIFECYCLE_BECAME_DISCONNECTED)

WAKE_UNRECONCILED_HANDOFF = "unreconciled-handoff"
WAKE_KINDS = (WAKE_UNRECONCILED_HANDOFF,) + LIFECYCLE_KINDS

REASON_SOURCE_UNRESOLVED = "source-unresolved"
REASON_HEAD_UNRESOLVED = "head-unresolved"
REASON_REVISION_DRIFT = "revision-drift"
REASON_STATE_BLOB_MISSING = "accepted-state-blob-missing"
REASON_RAIL_BLOB_MISSING = "rail-blob-missing"
REASON_DUPLICATE_RAIL = "duplicate-rail-identity"
REASON_ARTIFACT_BLOB_MISSING = "artifact-blob-missing"
REASON_INCOMPLETE_FACT = "incomplete-lifecycle-fact"
REASON_UNKNOWN_KIND = "unknown-lifecycle-kind"
REASON_CROSS_HEAD_FACT = "cross-head-lifecycle-fact"
REASON_UNKNOWN_RAIL = "unknown-rail"
REASON_ITERATION_DRIFT = "iteration-drift"
REASON_PACKET_STALE = "packet-stale"
REASON_INVALID_ROLE = "invalid-role"
REASON_INVALID_SESSION_MODE = "invalid-session-mode"


class TriggerError(Exception):
    """A refusal to propose a wake or a packet, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def _require_text(value: object, *, label: str, reason: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TriggerError(reason, "{0} must be non-empty text".format(label))
    return value.strip()


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RailSnapshot:
    """One rail reduced to identity and status. No authorization or handoff text."""

    identifier: str
    authorization_blob: str
    status: str
    proposed_status: Optional[str] = None
    handoff_blob: Optional[str] = None
    evidence_blob: Optional[str] = None

    @property
    def unreconciled(self) -> bool:
        return self.proposed_status is not None and self.proposed_status != self.status

    @property
    def material_fingerprint(self) -> Tuple[str, ...]:
        """Exactly the identity the wake rule names: rail, rail blob, handoff, evidence."""
        return (
            self.identifier,
            self.authorization_blob,
            self.handoff_blob or "",
            self.evidence_blob or "",
        )


@dataclass(frozen=True)
class ScopeSnapshot:
    """Every fact this module is allowed to hold, all from one resolved revision."""

    project: str
    ticket: str
    head: str
    state_blob: str
    rails: Tuple[RailSnapshot, ...] = ()

    def rail(self, identifier: str) -> Optional[RailSnapshot]:
        for entry in self.rails:
            if entry.identifier == identifier:
                return entry
        return None


def build_snapshot(source: ReadSource, *, project: str, ticket: str) -> ScopeSnapshot:
    """Reduce one already-resolved control-plane revision to identities and statuses.

    Fails closed rather than snapshotting a scope it cannot pin: an unresolved
    revision, a head that does not match the revision actually served, a missing
    accepted state, a rail without an authorization blob, or a repeated rail slug.
    """
    project = validate_identifier(project, label="project")
    ticket = validate_identifier(ticket, label="ticket")

    revision = getattr(source, "revision", None)
    if revision is None:
        raise TriggerError(
            REASON_SOURCE_UNRESOLVED,
            "a snapshot requires a published resolved revision, not a local worktree read",
        )
    revision = _require_text(revision, label="revision", reason=REASON_SOURCE_UNRESOLVED)
    head = _require_text(getattr(source, "head", None), label="head", reason=REASON_HEAD_UNRESOLVED)
    if revision != head:
        raise TriggerError(
            REASON_REVISION_DRIFT,
            "revision {0} and head {1} disagree; facts cannot be tied to one read".format(
                revision, head
            ),
        )

    state_relative = artifact_relative(project=project, ticket=ticket, artifact="state", rail=None)
    state_blob = source.blob_sha(state_relative)
    if not state_blob:
        raise TriggerError(
            REASON_STATE_BLOB_MISSING,
            "no accepted state at {0}; the scope is not readable at this revision".format(
                state_relative
            ),
        )

    rails: List[RailSnapshot] = []
    seen: Set[str] = set()
    for state in collect_rail_states(source, project=project, ticket=ticket):
        if state.identifier in seen:
            raise TriggerError(
                REASON_DUPLICATE_RAIL,
                "rail '{0}' appears more than once at this revision".format(state.identifier),
            )
        seen.add(state.identifier)

        authorization_blob = source.blob_sha(
            artifact_relative(project=project, ticket=ticket, artifact="rail", rail=state.identifier)
        )
        if not authorization_blob:
            raise TriggerError(
                REASON_RAIL_BLOB_MISSING,
                "rail '{0}' has no authorization blob at this revision".format(state.identifier),
            )

        rails.append(
            RailSnapshot(
                identifier=state.identifier,
                authorization_blob=authorization_blob,
                status=state.status,
                proposed_status=state.proposed_status,
                handoff_blob=_optional_blob(
                    source, project, ticket, state, artifact="handoff"
                ),
                evidence_blob=_optional_blob(
                    source, project, ticket, state, artifact="evidence"
                ),
            )
        )

    return ScopeSnapshot(
        project=project,
        ticket=ticket,
        head=head,
        state_blob=state_blob,
        rails=tuple(sorted(rails, key=lambda entry: entry.identifier)),
    )


def _optional_blob(
    source: ReadSource,
    project: str,
    ticket: str,
    state: object,
    *,
    artifact: str,
) -> Optional[str]:
    """A blob id for an artifact the rail reader already proved present, or None."""
    if artifact not in getattr(state, "artifacts", ()):
        return None
    identifier = getattr(state, "identifier")
    blob = source.blob_sha(
        artifact_relative(project=project, ticket=ticket, artifact=artifact, rail=identifier)
    )
    if not blob:
        # The rail reader saw this artifact at the same revision. Disagreement
        # means the read is not internally consistent, which is not snapshottable.
        raise TriggerError(
            REASON_ARTIFACT_BLOB_MISSING,
            "rail '{0}' reports a {1} artifact with no blob at this revision".format(
                identifier, artifact
            ),
        )
    return blob


# --------------------------------------------------------------------------
# Lifecycle facts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleFact:
    """An exact transition the controller observed. Injected; never discovered here.

    Hooks and process notifications are signals. This module accepts them only as
    fully identified transitions and still checks them against the fresh snapshot.
    """

    kind: str
    rail: str
    session_id: str
    iteration: str
    head: str

    def __post_init__(self) -> None:
        if self.kind not in LIFECYCLE_KINDS:
            raise TriggerError(
                REASON_UNKNOWN_KIND,
                "kind '{0}' is not one of {1}".format(self.kind, ", ".join(LIFECYCLE_KINDS)),
            )
        for label in ("rail", "session_id", "iteration", "head"):
            _require_text(getattr(self, label), label=label, reason=REASON_INCOMPLETE_FACT)


def _lifecycle_reason(fact: LifecycleFact, snapshot: ScopeSnapshot) -> Optional["WakeReason"]:
    """Tie one injected transition to the fresh snapshot, or refuse to use it."""
    if fact.head != snapshot.head:
        raise TriggerError(
            REASON_CROSS_HEAD_FACT,
            "lifecycle fact for rail '{0}' names head {1}, snapshot head is {2}".format(
                fact.rail, fact.head, snapshot.head
            ),
        )

    rail = snapshot.rail(fact.rail)
    if rail is None:
        raise TriggerError(
            REASON_UNKNOWN_RAIL,
            "lifecycle fact names rail '{0}', which does not exist at this head".format(fact.rail),
        )
    if fact.iteration != rail.authorization_blob:
        raise TriggerError(
            REASON_ITERATION_DRIFT,
            "lifecycle fact for rail '{0}' names iteration {1}; this head authorizes {2}".format(
                fact.rail, fact.iteration, rail.authorization_blob
            ),
        )

    if fact.kind == LIFECYCLE_INVOCATION_ENDED:
        # An invocation that ended having already produced a reconciled handoff
        # left nothing for an orchestrator to do. An unreconciled one is covered
        # by the handoff reason and coalesces with it.
        if rail.proposed_status is not None and not rail.unreconciled:
            return None
    else:
        # "Previously Running" is only meaningful while the rail is still the
        # authorization that was being run. Once it moved, the orchestrator has
        # already reconciled this.
        if rail.status != "running":
            return None

    return WakeReason(
        kind=fact.kind,
        rail=rail.identifier,
        fingerprint=(fact.kind,) + rail.material_fingerprint + (fact.session_id,),
    )


# --------------------------------------------------------------------------
# Wake
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeReason:
    """Why one wake is being proposed. Identity only; no artifact text."""

    kind: str
    rail: str
    fingerprint: Tuple[str, ...]

    @property
    def key(self) -> str:
        return "|".join(self.fingerprint)


@dataclass(frozen=True)
class WakeProposal:
    """One wake, carrying every material fact observed at this head, sorted."""

    project: str
    ticket: str
    head: str
    reasons: Tuple[WakeReason, ...]

    @property
    def rails(self) -> Tuple[str, ...]:
        return tuple(sorted({reason.rail for reason in self.reasons}))


@dataclass
class TriggerCursor:
    """Deliberately non-durable memory of material facts already proposed.

    Nothing here is written anywhere. A durable event history or lease would be a
    claim this controller cannot keep across a restart, and the redundant wake it
    would prevent is cheap: the fresh orchestrator re-reads durable state anyway.
    """

    _seen: Set[str] = field(default_factory=set)

    def seen(self, reason: WakeReason) -> bool:
        return reason.key in self._seen

    def remember(self, reason: WakeReason) -> None:
        self._seen.add(reason.key)


def _handoff_reasons(snapshot: ScopeSnapshot) -> List[WakeReason]:
    reasons: List[WakeReason] = []
    for rail in snapshot.rails:
        if not rail.unreconciled:
            continue
        reasons.append(
            WakeReason(
                kind=WAKE_UNRECONCILED_HANDOFF,
                rail=rail.identifier,
                fingerprint=(WAKE_UNRECONCILED_HANDOFF,) + rail.material_fingerprint,
            )
        )
    return reasons


def propose_wake(
    snapshot: ScopeSnapshot,
    *,
    lifecycle_facts: Sequence[LifecycleFact] = (),
    cursor: Optional[TriggerCursor] = None,
) -> Optional[WakeProposal]:
    """Propose at most one wake for everything material at this head.

    Returns None when nothing is material, or when every material fact was already
    proposed within this controller's lifetime. Several simultaneous facts coalesce
    into a single proposal with stable, sorted reasons.
    """
    reasons: Dict[str, WakeReason] = {}
    for reason in _handoff_reasons(snapshot):
        reasons[reason.key] = reason
    for fact in lifecycle_facts:
        reason = _lifecycle_reason(fact, snapshot)
        if reason is not None:
            reasons[reason.key] = reason

    if not reasons:
        return None

    ordered = tuple(sorted(reasons.values(), key=lambda entry: (entry.rail, entry.kind, entry.key)))
    if cursor is not None:
        if all(cursor.seen(reason) for reason in ordered):
            return None
        for reason in ordered:
            cursor.remember(reason)

    return WakeProposal(
        project=snapshot.project,
        ticket=snapshot.ticket,
        head=snapshot.head,
        reasons=ordered,
    )


# --------------------------------------------------------------------------
# Packet
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorPacket:
    """Everything a fresh orchestrator is given. There is no field for resuming one."""

    project: str
    ticket: str
    head: str
    role: str = ROLE_ORCHESTRATOR
    session_mode: str = SESSION_MODE_NEW
    directive: str = DIRECTIVE

    def __post_init__(self) -> None:
        if self.role != ROLE_ORCHESTRATOR:
            raise TriggerError(
                REASON_INVALID_ROLE,
                "a wake packet addresses '{0}', not '{1}'".format(ROLE_ORCHESTRATOR, self.role),
            )
        if self.session_mode != SESSION_MODE_NEW:
            raise TriggerError(
                REASON_INVALID_SESSION_MODE,
                "a wake packet cannot resume or continue a prior orchestrator; "
                "session mode must be '{0}'".format(SESSION_MODE_NEW),
            )
        for label in ("project", "ticket", "head"):
            _require_text(getattr(self, label), label=label, reason=REASON_INCOMPLETE_FACT)


def build_packet(snapshot: ScopeSnapshot) -> OrchestratorPacket:
    """The smallest packet that can start one fresh orchestrator, bound to this head."""
    return OrchestratorPacket(
        project=snapshot.project,
        ticket=snapshot.ticket,
        head=snapshot.head,
    )


def require_current(packet: OrchestratorPacket, snapshot: ScopeSnapshot) -> OrchestratorPacket:
    """Refuse a packet built against an older head; rebuild it from the newer snapshot."""
    if packet.head != snapshot.head:
        raise TriggerError(
            REASON_PACKET_STALE,
            "packet is bound to head {0}; current head is {1}. Discard and rebuild.".format(
                packet.head, snapshot.head
            ),
        )
    if packet.project != snapshot.project or packet.ticket != snapshot.ticket:
        raise TriggerError(
            REASON_PACKET_STALE,
            "packet addresses {0}/{1}; snapshot is {2}/{3}".format(
                packet.project, packet.ticket, snapshot.project, snapshot.ticket
            ),
        )
    return packet
