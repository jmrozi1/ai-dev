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
#
# One dispatched invocation is worth exactly one allowance entry, and only a caller
# that owns a ledger gets one. The ledger is supplied, never built: this module has
# no business knowing where a store lives, and a per-invocation ledger would restart
# the ordinals it exists to keep unique. Omit it and nothing below changes at all --
# no id is minted here, no store is imported, no file is written.
#
# The entry is named before the dispatch it names. The session id is obtained up
# front and handed to the lifecycle, so the key and the binding cannot disagree and
# no recovery path is ever tempted to mint a second key for one invocation.
#
# The `try` around `launch_session` is exactly as wide as the dispatch. Stretching it
# over `stop_session` or the outcome would let `shutdown-incomplete` reach
# `record_failure` and be written down as an invocation hole, which is a different
# and false claim about what the provider consumed.
#
# One dispatch is observable exactly once, while it is running. Between the launch
# landing and the stop there is an instant at which the caller's registry really
# holds this session's handle and its binding is really nonterminal, and that is the
# only instant at which anything can honestly report live occupancy. `while_running`
# is that instant offered to the caller and nothing else: it is given the outcome,
# asked for nothing back, and cannot authorize, extend, re-dispatch, or
# terminalize anything. Without it the live window is unreachable from outside this call, and a
# surface that draws its count after the dispatch returns can only ever draw a
# stopped one.

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple
import uuid

from .authorization import (
    ACTION_LAUNCH,
    ControlPlaneObservation,
    authorize,
)
from .claude_allowance_ledger import KIND_LAUNCH, AllowanceLedger
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
REASON_RAIL_ROLE = "orchestrator-rail-role-mismatch"
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
    slots,
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
    if rail.role != ORCHESTRATOR_ROLE:
        # Checked here, from the snapshot, before the predicate re-checks it from
        # the observation. Two sources that must agree; neither may stand in for
        # the other, and an executor or evidence rail can never become one.
        raise InvocationRefused(
            REASON_RAIL_ROLE,
            "rail '{0}' is assigned to '{1}'; only a rail durably assigned to '{2}' may "
            "start one".format(orchestrator_rail, rail.role or "no role", ORCHESTRATOR_ROLE),
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
        slots=slots,
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
    slots,
    bindings: Iterable = (),
    in_flight_session_ids: Sequence = (),
    markers: Sequence = (),
    launch_kwargs: Optional[Mapping] = None,
    stop_kwargs: Optional[Mapping] = None,
    ledger: Optional[AllowanceLedger] = None,
    while_running: Optional[Callable] = None,
) -> InvocationOutcome:
    """Both gates, then exactly one fresh orchestrator session, stopped and unbound.

    Raises `InvocationRefused` before anything is enacted when either gate fails,
    and lets a lifecycle failure propagate unchanged: a failed invocation is not
    retried, resumed, or terminalized without proof.

    When `ledger` is supplied, this dispatch is accounted for exactly once: the
    reported cost when the launch returned one, a hole when it did not, and nothing
    at all when the refusal is proven to precede the provider. Accounting never
    weakens cleanup -- a launched session is stopped even when its recording fails --
    and never invents success: the outcome is returned only once both have.

    When `while_running` is supplied it is called exactly once, after the session
    has really started and before it is stopped, with the `LaunchOutcome`. It is an
    observation point, not a second decision: its return value is discarded, and if
    it raises, the session is stopped and the failure propagates rather than being
    swallowed -- observing badly is no reason to leave a session running, and no
    reason to report success. Omit it and nothing below changes at all.
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
        slots=slots,
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

    launch_arguments = dict(launch_kwargs or {})
    identity = None
    if ledger is not None:
        # Before the dispatch, so the key exists before the outcome does. An injected
        # mint is the caller's, and it is called once; otherwise this reproduces the
        # lifecycle's own default. The canonical spelling `next_identity` returns is
        # what the lifecycle is then given, so one invocation cannot end up with a
        # key and a binding that name the session two different ways.
        injected_mint = launch_arguments.get("new_session_id")
        session_id = injected_mint() if injected_mint is not None else str(uuid.uuid4())
        identity = ledger.next_identity(session_id, KIND_LAUNCH)
        preassigned = identity.session_id
        launch_arguments["new_session_id"] = lambda: preassigned

    try:
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
            **launch_arguments
        )
    except Exception as error:
        # Only what escaped the dispatch, and only the ledger decides what it was
        # worth: `launch-failed` is a hole, a proven pre-dispatch refusal is nothing.
        # If that recording itself fails the accounting failure is raised from here,
        # so the launch failure it displaces remains its context -- allowance is never
        # lost quietly.
        if identity is not None:
            ledger.record_failure(identity, error)
        raise

    if identity is not None:
        try:
            ledger.record_completed(identity, launched.result)
        except Exception:
            # The session really is running. Recording it badly is no reason to leave
            # it running, and no reason to report success either.
            stop_session(store, registry, launched.binding, **dict(stop_kwargs or {}))
            raise

    if while_running is not None:
        # The one instant this dispatch is live and provable: the process started,
        # the handle is in the caller's own registry, and the binding is nonterminal.
        # A caller holding both halves can reduce a real occupancy here and nowhere
        # else. Exactly the ledger's failure rule above, for exactly the same reason.
        try:
            while_running(launched)
        except Exception:
            stop_session(store, registry, launched.binding, **dict(stop_kwargs or {}))
            raise

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
