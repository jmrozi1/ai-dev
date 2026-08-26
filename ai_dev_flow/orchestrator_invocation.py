"""Two independent gates between a material wake and one fresh orchestrator session."""

from __future__ import annotations

# A wake says reconciliation is needed. It does not say anyone is allowed to spend
# a Claude session on it. Those are different claims, and this module refuses to
# let the first stand in for the second.
#
# So a launch needs both gates, at one exact head:
#
#   1. a material wake proposal that still matches the fresh snapshot, and
#   2. a separate standing orchestrator rail that is `running`, reconciled, and
#      authorized by the accepted `authorization.authorize` predicate for role
#      `orchestrator`, action `launch`, at that head and that rail's exact blob.
#
# The standing rail is where a human's durable authority lives. If a wake alone
# could launch, the controller would be authorizing itself from an event it also
# generated -- and the most direct version of that failure is an orchestrator's own
# lifecycle waking an orchestrator, so a reason naming the dedicated rail is
# refused outright rather than merely deduplicated.
#
# There is deliberately no parameter for an authorization decision. This module
# always calls the accepted predicate itself, so no caller can hand it a
# fabricated `authorized=True`. The only things injectable are the process-level
# fakes that `session_lifecycle` already accepts.
#
# One shot, never a continuation. If a nonterminal binding already exists the
# accepted predicate answers `continue`, and this module refuses before anything
# is enacted rather than quietly resuming: a fresh orchestrator that resumed a
# prior conversation would be reasoning from transcript instead of durable state.
# After a successful invocation the owned group is proven gone and the binding is
# terminalized, so the next wake starts a genuinely new session.
#
# Failure is never retried here. In particular, an invocation that fails after the
# process bound leaves the binding nonterminal on purpose -- that is the truth, and
# it is what later projects Disconnected instead of a cleaner story.

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .authorization import (
    ACTION_LAUNCH,
    ControlPlaneObservation,
    authorize,
)
from .orchestrator_trigger import (
    OrchestratorPacket,
    ScopeSnapshot,
    WakeProposal,
    require_current,
)
from .session_binding import RailIteration
from .session_lifecycle import Assignment, launch_session, stop_session

__all__ = [
    "InvocationOutcome",
    "InvocationRefused",
    "ORCHESTRATOR_ROLE",
    "invoke_orchestrator",
]

ORCHESTRATOR_ROLE = "orchestrator"

REASON_NO_MATERIAL_WAKE = "no-material-wake"
REASON_WAKE_SCOPE_MISMATCH = "wake-scope-mismatch"
REASON_WAKE_STALE = "wake-stale"
REASON_SELF_WAKE = "self-wake-refused"
REASON_RAIL_MISSING = "orchestrator-rail-missing"
REASON_RAIL_NOT_RUNNING = "orchestrator-rail-not-running"
REASON_RAIL_UNRECONCILED = "orchestrator-rail-unreconciled"
REASON_NOT_AUTHORIZED = "not-authorized"
REASON_CONTINUATION_REFUSED = "continuation-refused"
REASON_WORKSPACE_UNPROVEN = "workspace-identity-unproven"
REASON_WORKSPACE_MISSING = "workspace-observation-missing"
REASON_PACKET_ROLE = "packet-role-mismatch"


class InvocationRefused(Exception):
    """A refusal to spend one orchestrator session, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class InvocationOutcome:
    """What happened, in identity terms only. No provider content, no live handle."""

    project: str
    ticket: str
    head: str
    rail: str
    role: str
    session_id: str
    iteration_blob: str
    wake_rails: Tuple[str, ...]
    binding_state: str
    process_group_gone: bool
    graceful: bool


# --------------------------------------------------------------------------
# Gate one: the wake
# --------------------------------------------------------------------------


def _require_material_wake(
    proposal: Optional[WakeProposal], snapshot: ScopeSnapshot, orchestrator_rail: str
) -> Tuple[str, ...]:
    """A wake is necessary, current, in scope, and never about the orchestrator itself."""
    if proposal is None or not proposal.reasons:
        raise InvocationRefused(
            REASON_NO_MATERIAL_WAKE,
            "no material wake was proposed; a launch needs one",
        )
    if (
        proposal.project != snapshot.project
        or proposal.ticket != snapshot.ticket
        or proposal.head != snapshot.head
    ):
        raise InvocationRefused(
            REASON_WAKE_SCOPE_MISMATCH,
            "wake is for {0}/{1}@{2}; snapshot is {3}/{4}@{5}".format(
                proposal.project,
                proposal.ticket,
                proposal.head,
                snapshot.project,
                snapshot.ticket,
                snapshot.head,
            ),
        )

    for reason in proposal.reasons:
        if reason.rail == orchestrator_rail:
            raise InvocationRefused(
                REASON_SELF_WAKE,
                "wake reason '{0}' names the dedicated orchestrator rail '{1}'; an "
                "orchestrator's own lifecycle may not authorize another orchestrator".format(
                    reason.kind, orchestrator_rail
                ),
            )
        rail = snapshot.rail(reason.rail)
        if rail is None:
            raise InvocationRefused(
                REASON_WAKE_STALE,
                "wake reason names rail '{0}', absent from this snapshot".format(reason.rail),
            )
        if tuple(reason.fingerprint[1:5]) != rail.material_fingerprint:
            raise InvocationRefused(
                REASON_WAKE_STALE,
                "wake reason for rail '{0}' no longer matches that rail at this head".format(
                    reason.rail
                ),
            )

    return proposal.rails


# --------------------------------------------------------------------------
# Gate two: the standing orchestrator authorization
# --------------------------------------------------------------------------


def _require_standing_authorization(
    snapshot: ScopeSnapshot,
    packet: OrchestratorPacket,
    observation: ControlPlaneObservation,
    *,
    orchestrator_rail: str,
    bindings: Iterable,
    in_flight_session_ids: Sequence,
):
    """The dedicated rail must be running, reconciled, and pass the accepted predicate."""
    rail = snapshot.rail(orchestrator_rail)
    if rail is None:
        raise InvocationRefused(
            REASON_RAIL_MISSING,
            "no dedicated orchestrator rail '{0}' at head {1}".format(
                orchestrator_rail, snapshot.head
            ),
        )
    if rail.status != "running":
        raise InvocationRefused(
            REASON_RAIL_NOT_RUNNING,
            "orchestrator rail '{0}' is '{1}'; only a running rail authorizes a launch".format(
                orchestrator_rail, rail.status
            ),
        )
    if rail.unreconciled:
        raise InvocationRefused(
            REASON_RAIL_UNRECONCILED,
            "orchestrator rail '{0}' carries an unreconciled handoff; its authorization is "
            "not settled".format(orchestrator_rail),
        )

    # The accepted predicate decides. Nothing here re-implements or overrides it,
    # and there is no way for a caller to supply a decision instead.
    decision = authorize(
        observation,
        project=packet.project,
        ticket=packet.ticket,
        rail=orchestrator_rail,
        role=ORCHESTRATOR_ROLE,
        expected_head=packet.head,
        rail_blob=rail.authorization_blob,
        bindings=tuple(bindings),
        in_flight_session_ids=tuple(in_flight_session_ids),
    )
    if not decision.authorized:
        raise InvocationRefused(
            REASON_NOT_AUTHORIZED,
            "the accepted authorization predicate refuses this launch: {0}".format(decision.reason),
        )
    if decision.action != ACTION_LAUNCH:
        # A nonterminal binding already exists. A fresh orchestrator is the only
        # thing this module may start, so this is a refusal, not a fallback.
        raise InvocationRefused(
            REASON_CONTINUATION_REFUSED,
            "authorization answers '{0}' for rail '{1}'; a wake may only start a fresh "
            "orchestrator, never continue one".format(decision.action, orchestrator_rail),
        )
    return decision, rail


def _require_workspace(observation: ControlPlaneObservation) -> Any:
    workspace = observation.workspace
    if workspace is None:
        raise InvocationRefused(
            REASON_WORKSPACE_MISSING,
            "the observation proves no workspace; a session cannot be assigned one",
        )
    if workspace.identity_problem is not None:
        raise InvocationRefused(
            REASON_WORKSPACE_UNPROVEN,
            "workspace ownership is unproven: {0}".format(workspace.identity_problem),
        )
    return workspace


# --------------------------------------------------------------------------
# Enactment
# --------------------------------------------------------------------------


def invoke_orchestrator(
    snapshot: ScopeSnapshot,
    proposal: Optional[WakeProposal],
    packet: OrchestratorPacket,
    observation: ControlPlaneObservation,
    *,
    orchestrator_rail: str,
    store: Any,
    registry: Any,
    reference: Any,
    request_kwargs: Mapping,
    package_root: Any,
    bindings: Iterable = (),
    in_flight_session_ids: Sequence = (),
    markers: Sequence = (),
    launch_kwargs: Optional[Mapping] = None,
    stop_kwargs: Optional[Mapping] = None,
) -> InvocationOutcome:
    """Both gates, then exactly one fresh orchestrator session, stopped and unbound.

    Raises `InvocationRefused` before anything is enacted when either gate fails,
    and lets a lifecycle failure propagate unchanged: a failed invocation is not
    retried, resumed, or terminalized without proof.
    """
    if packet.role != ORCHESTRATOR_ROLE:
        raise InvocationRefused(
            REASON_PACKET_ROLE,
            "packet addresses '{0}', not '{1}'".format(packet.role, ORCHESTRATOR_ROLE),
        )

    wake_rails = _require_material_wake(proposal, snapshot, orchestrator_rail)
    decision, rail = _require_standing_authorization(
        snapshot,
        packet,
        observation,
        orchestrator_rail=orchestrator_rail,
        bindings=bindings,
        in_flight_session_ids=in_flight_session_ids,
    )
    workspace = _require_workspace(observation)

    # Last thing before enactment: the packet must still be the current head.
    require_current(packet, snapshot)

    assignment = Assignment(
        project=packet.project,
        ticket=packet.ticket,
        rail=orchestrator_rail,
        role=ORCHESTRATOR_ROLE,
        head=packet.head,
        iteration=RailIteration(rail=orchestrator_rail, blob=rail.authorization_blob),
        workspace_key=workspace.workspace_key,
        worktree_id=workspace.worktree_id,
        workspace_path=workspace.workspace_path,
    )

    launched = launch_session(
        decision,
        assignment,
        store=store,
        registry=registry,
        reference=reference,
        request_kwargs=request_kwargs,
        prompt=packet.directive,
        package_root=package_root,
        markers=markers,
        **dict(launch_kwargs or {})
    )

    stopped = stop_session(
        store, registry, launched.binding, **dict(stop_kwargs or {})
    )

    return InvocationOutcome(
        project=assignment.project,
        ticket=assignment.ticket,
        head=assignment.head,
        rail=assignment.rail,
        role=assignment.role,
        session_id=stopped.session_id,
        iteration_blob=assignment.iteration.blob,
        wake_rails=tuple(wake_rails),
        binding_state=stopped.binding.state,
        process_group_gone=stopped.process_group_gone,
        graceful=stopped.graceful,
    )
