"""One gate between a stated role assignment and a managed executor or reviewer session."""

from __future__ import annotations

# Until this module existed, the package could start exactly one kind of managed
# session. `invoke_orchestrator` hard-binds `orchestrator` in three independent
# places, `ManagerController.launch` had no production caller, and the only driven
# lifecycle call anywhere was `controller.dispatch(`. An executor-role or
# reviewer-role session was reachable from tests and from nothing else.
#
# That was judged at checkpoint 50 and left deliberately closed: no rail was
# permitted to add executor launch capability merely to satisfy old wording, and if
# it were ever wanted it needed its own authorization. It now has one. This module
# is that capability and nothing beyond it.
#
# What it is NOT is as load-bearing as what it is:
#
#   * It is not a second orchestrator door. `orchestrator` is refused here by name,
#     before anything else is read. The orchestrator's first gate -- a material wake
#     that still matches the fresh snapshot -- lives in `orchestrator_invocation` and
#     is not reproduced here, so a door that admitted `orchestrator` without it would
#     be a bypass of that gate rather than a second route to the same place. Refusing
#     the role is what makes "I left the wake gate exactly alone" a structural fact
#     instead of a promise.
#
#   * It is not a driver, a pool, a scheduler, or a loop, and it still is not one.
#     `open_role_session` admits one session and hands it back live; `invoke_role`
#     admits one, offers the caller the instant at which it is live, and stops it.
#     Neither decides that a session should exist: a human's durable authorization
#     on a named rail does.
#
#     Until checkpoint 74 this module additionally refused outright whenever the
#     registry it was launching into already held a session (`_require_sequential`,
#     `session-already-live`), so that "one managed session at a time" was enforced
#     by the door rather than by the caller's good behaviour. Checkpoint 73 placed
#     that refusal to be removed by the slice that earned it and said so in as many
#     words. **This checkpoint removes it deliberately**, because holding more than
#     one managed session live at once is exactly the capability the accepted middle
#     cut authorizes next, and a door that refuses it cannot be that capability.
#
#     Nothing else moved with it, and the deletion is narrower than it sounds. What
#     bounds concurrency now is what always bounded it on the accepted path: the D6
#     ceiling inside `authorize`, evaluated per admission against occupancy the
#     caller reconciled from durable records and proved ownership, and evaluated a
#     second time inside the store lock by `reserve_binding` from the ceiling this
#     decision carried. That ceiling is a limit, not a target -- nothing here starts
#     a session because a slot happens to be free -- and every launch still needs
#     its own standing durable authorization on its own rail.
#
#   * It is not a second authorization system. There is no parameter for a decision
#     and no way to hand this module a fabricated `authorized=True`; it always calls
#     the accepted `authorization.authorize` predicate itself, with the role the
#     caller asked for, and every refusal below it keeps that predicate's own reason.
#     The D6 ceiling is therefore evaluated here at exactly the point it is evaluated
#     on the accepted path -- before a binding is reserved -- and again inside the
#     store lock by `reserve_binding`, from the ceiling this decision carried.
#
# Why there is one gate here and two on the orchestrator path. The orchestrator's
# wake gate exists because an orchestrator is started by control-plane events an
# orchestrator also produces: without it the controller would authorize itself from
# an event it generated. Nothing produces role launches. There is no wake kind for
# "an executor rail is ready", no producer of one, and inventing one would be the
# autonomous continuation loop this ticket has explicitly deferred. So the standing
# durable authorization -- a human's decision, written in the rail -- is the whole
# authority, exactly as it is for the orchestrator, and this door adds a refusal of
# its own -- the launchable-role refusal -- that the orchestrator door has neither
# need of nor equivalent to.
#
# Role fidelity is structural in five places and conventional in none. The role a
# session is launched in is:
#
#   1. stated by the caller and carried on the packet, which refuses any role but
#      `executor` or `reviewer` at construction;
#   2. checked against the rail's durable `Role:` from the snapshot, here;
#   3. re-checked from the observation by `authorize`, which refuses
#      `rail-role-mismatch` before it authorizes anything;
#   4. carried into the `Assignment`, which `session_lifecycle._require_decision`
#      refuses unless it equals the role the decision was granted for, and which
#      `reserve_binding` writes into the durable record and `launch_request` reads
#      back onto the runtime request; and
#   5. compared against the runtime package the session will actually run:
#      `claude_runtime._build_request` hands `record.role` -- read off the durable
#      binding, not off any argument it was given -- to `validate_plugin_surface`,
#      which refuses `plugin-role-mismatch` unless the one skill that package
#      exposes is that role's.
#
# The fifth was added at checkpoint 75 and the first four were published without
# it. Checkpoints 73 and 74 stated that role fidelity was "structural in four
# places", which was true of the four and false about what they covered: every one
# of them compares a role to another statement of the *same* role, and none of them
# looked at the prompt file, the plugin root or the expected skill. A run stating
# `--role executor` on an executor-assigned rail, with the reviewer plugin and
# `--expected-skill reviewer`, passed all four, wrote `executor` into the durable
# binding, and ran the reviewer's package. That is corrected in the product rather
# than only in prose, and item 5 is what corrects it.
#
# So a session launched in a role its authorization was not granted for cannot be
# constructed: the two would have to disagree at a point where the product compares
# them and fails closed. A path that could launch an executor against an
# orchestrator authorization would be worse than no path at all, and this ticket's
# accepted contracts already refuse it -- what this module had to do was use them
# rather than route around them.
#
# What item 5 does NOT cover, stated because it is the honest edge: the *system
# prompt file* is still an operator input with no structural declaration of the role
# it was written for, so an executor launch can still be handed a prompt file
# written for a reviewer. What is bound is the plugin package -- the skill the
# provider actually loads -- and the directive, which is `DIRECTIVES[role]` and
# cannot be stated on the command line at all.
#
# Reuse, not respelling. The refusal type, the workspace rule, the head-currency
# rule and the outcome shape are the accepted ones, imported. A second spelling of
# a rule is two rules free to drift, and this module is a door onto machinery that
# already exists rather than a second copy of it.

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
import uuid

from .authorization import (
    ACTION_LAUNCH,
    ControlPlaneObservation,
    authorize,
)
from .claude_allowance_ledger import KIND_LAUNCH, AllowanceLedger
from .orchestrator_invocation import (
    REASON_CONTINUATION_REFUSED,
    REASON_NOT_AUTHORIZED,
    InvocationOutcome,
    InvocationRefused,
    _require_workspace,
)
from .orchestrator_trigger import ScopeSnapshot, require_current
from .session_binding import ROLE_EXECUTOR, ROLE_ORCHESTRATOR, ROLE_REVIEWER, RailIteration
from .session_lifecycle import Assignment, launch_session, stop_session

__all__ = [
    "DIRECTIVES",
    "LAUNCHABLE_ROLES",
    "REASON_RAIL_MISSING",
    "REASON_RAIL_NOT_RUNNING",
    "REASON_RAIL_ROLE",
    "REASON_RAIL_UNRECONCILED",
    "REASON_ROLE_NOT_LAUNCHABLE",
    "OpenSession",
    "RolePacket",
    "build_role_packet",
    "invoke_role",
    "open_role_session",
]

# Exactly the two roles the human middle-cut decision authorized. `orchestrator` is
# absent on purpose and is refused by name below; it is not an oversight and
# widening this tuple is a capability change, not a configuration one.
LAUNCHABLE_ROLES = (ROLE_EXECUTOR, ROLE_REVIEWER)

SESSION_MODE_NEW = "new"

# The whole instruction, one per role, fixed. A session is told where its authority
# lives and nothing about what it will find there. It is a constant rather than an
# operator input for the reason the orchestrator's is: work content stated at the
# command line would be a manager inventing an assignment nobody wrote down, and
# the rail is where an assignment is written down.
DIRECTIVES = {
    ROLE_EXECUTOR: (
        "Read your authorized rail in the control plane fresh and continue it."
    ),
    ROLE_REVIEWER: (
        "Read your authorized rail in the control plane fresh and return its verdict."
    ),
}

REASON_ROLE_NOT_LAUNCHABLE = "role-not-launchable"
# `REASON_SESSION_ALREADY_LIVE` ("session-already-live") stood here until checkpoint
# 74 and is gone with the refusal that raised it. It is named here, once, so a reader
# of a checkpoint-73 transcript can find out what happened to it rather than assume
# the reason merely stopped being reachable.
REASON_RAIL_MISSING = "role-rail-missing"
REASON_RAIL_NOT_RUNNING = "role-rail-not-running"
REASON_RAIL_UNRECONCILED = "role-rail-unreconciled"
REASON_RAIL_ROLE = "role-rail-role-mismatch"


@dataclass(frozen=True)
class RolePacket:
    """Everything one managed executor or reviewer session is given.

    It names a rail, because a role assignment without a rail is not an assignment:
    the rail is where the human's durable authority for this work lives, and the
    role on it is what this session may be. There is no field for resuming one --
    this door starts fresh sessions only, exactly as the orchestrator's packet does,
    and for the same reason: a session that resumed a prior conversation would be
    reasoning from a transcript instead of from durable state.
    """

    project: str
    ticket: str
    head: str
    rail: str
    role: str
    session_mode: str = SESSION_MODE_NEW
    directive: Optional[str] = None

    def __post_init__(self) -> None:
        if self.role not in LAUNCHABLE_ROLES:
            raise InvocationRefused(
                REASON_ROLE_NOT_LAUNCHABLE,
                "a role packet addresses one of {0}, not '{1}'".format(
                    ", ".join(LAUNCHABLE_ROLES), self.role
                ),
            )
        if self.session_mode != SESSION_MODE_NEW:
            raise InvocationRefused(
                REASON_ROLE_NOT_LAUNCHABLE,
                "a role packet cannot resume or continue a prior session; session "
                "mode must be '{0}'".format(SESSION_MODE_NEW),
            )
        for label in ("project", "ticket", "head", "rail"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise InvocationRefused(
                    REASON_ROLE_NOT_LAUNCHABLE,
                    "a role packet needs a non-empty {0}".format(label),
                )
        # Defaulted here rather than in the field, because the default depends on
        # the role and a field default cannot. A caller may not substitute its own:
        # the directive is this module's, and a packet carrying anything else is a
        # stated assignment that the rail did not state.
        object.__setattr__(self, "directive", DIRECTIVES[self.role])


def build_role_packet(snapshot: ScopeSnapshot, *, rail: str, role: str) -> RolePacket:
    """The smallest packet that can start one session in one role, bound to this head."""
    return RolePacket(
        project=snapshot.project,
        ticket=snapshot.ticket,
        head=snapshot.head,
        rail=rail,
        role=role,
    )


# --------------------------------------------------------------------------
# The door's own refusal
# --------------------------------------------------------------------------


def _require_launchable_role(role: str) -> str:
    """`orchestrator` is refused here, by name, before anything else is read.

    The orchestrator's first gate is a material wake, and it lives in
    `orchestrator_invocation`. This door has no wake and cannot have one, so an
    orchestrator admitted here would be an orchestrator started without the gate
    that exists to stop it authorizing itself. Refusing the role is how that gate
    stays whole while this door exists beside it.
    """
    if role == ROLE_ORCHESTRATOR:
        raise InvocationRefused(
            REASON_ROLE_NOT_LAUNCHABLE,
            "this door starts '{0}' sessions and refuses '{1}': an orchestrator is "
            "started by `orchestrator_invocation`, behind a material-wake gate this "
            "door does not have".format("' and '".join(LAUNCHABLE_ROLES), role),
        )
    if role not in LAUNCHABLE_ROLES:
        raise InvocationRefused(
            REASON_ROLE_NOT_LAUNCHABLE,
            "role '{0}' is not one of {1}".format(role, ", ".join(LAUNCHABLE_ROLES)),
        )
    return role


# --------------------------------------------------------------------------
# The standing authorization, for the role that was asked for
# --------------------------------------------------------------------------


def _require_standing_authorization(
    snapshot: ScopeSnapshot,
    packet: RolePacket,
    observation: ControlPlaneObservation,
    *,
    bindings: Iterable,
    in_flight_session_ids: Sequence,
    slots,
):
    """The named rail must be running, reconciled, assigned this role, and authorized.

    The rail's role is read twice from two independent sources that must agree --
    once from the snapshot here, once from the observation inside the predicate --
    and neither may stand in for the other. That is the same shape the orchestrator
    path uses, and it is the reason a session cannot be launched in a role its
    authorization was not granted for: the disagreement is refused before anything
    is reserved, spawned, or sent.
    """
    rail = snapshot.rail(packet.rail)
    if rail is None:
        raise InvocationRefused(
            REASON_RAIL_MISSING,
            "no rail '{0}' at head {1}".format(packet.rail, snapshot.head),
        )
    if rail.status != "running":
        raise InvocationRefused(
            REASON_RAIL_NOT_RUNNING,
            "rail '{0}' is '{1}'; only a running rail authorizes a launch".format(
                packet.rail, rail.status
            ),
        )
    if rail.unreconciled:
        raise InvocationRefused(
            REASON_RAIL_UNRECONCILED,
            "rail '{0}' carries an unreconciled handoff; its authorization is not "
            "settled".format(packet.rail),
        )
    if rail.role != packet.role:
        raise InvocationRefused(
            REASON_RAIL_ROLE,
            "rail '{0}' is assigned to '{1}'; a '{2}' session may only be started on "
            "a rail durably assigned to '{2}'".format(
                packet.rail, rail.role or "no role", packet.role
            ),
        )

    decision = authorize(
        observation,
        project=packet.project,
        ticket=packet.ticket,
        rail=packet.rail,
        role=packet.role,
        expected_head=packet.head,
        rail_blob=rail.authorization_blob,
        slots=slots,
        bindings=tuple(bindings),
        in_flight_session_ids=tuple(in_flight_session_ids),
    )
    if not decision.authorized:
        raise InvocationRefused(
            REASON_NOT_AUTHORIZED,
            "the accepted authorization predicate refuses this launch: {0}".format(
                decision.reason
            ),
        )
    if decision.action != ACTION_LAUNCH:
        # A nonterminal binding already exists on this rail. A fresh session is the
        # only thing this door may start, so this is a refusal and not a fallback.
        raise InvocationRefused(
            REASON_CONTINUATION_REFUSED,
            "authorization answers '{0}' for rail '{1}'; this door may only start a "
            "fresh session, never continue one".format(decision.action, packet.rail),
        )
    return decision, rail


# --------------------------------------------------------------------------
# Enactment
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenSession:
    """One managed session that is live right now, and what it was granted.

    Returned rather than an `InvocationOutcome` because an outcome describes a
    session that is over, and this one is not: the process is running, the binding
    is nonterminal, and the handle is in the caller's registry. The caller that
    opened it owns stopping it, and there is deliberately no finaliser, timer, or
    context manager here that would stop it on the caller's behalf -- a session
    torn down by something other than the code that asked for it is exactly how a
    process group outlives its record.
    """

    assignment: Assignment
    launched: Any


def open_role_session(
    snapshot: ScopeSnapshot,
    packet: RolePacket,
    observation: ControlPlaneObservation,
    *,
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
) -> OpenSession:
    """Every gate, then one launch, and the session is handed back still running.

    This is the whole of `invoke_role` except the stop, factored out rather than
    copied, so that a caller which needs to hold a session open reaches exactly the
    gates a caller which stops it immediately reaches. A second admission path with
    its own copy of these checks is two policies free to drift, and the one that
    drifts is always the one that admits more.

    It stops nothing and returns the session live. The caller must stop what it
    opened; `invoke_role` does so immediately, and `role_driver` does so after
    holding several at once.
    """
    # Checked at the enactment boundary, from the value that will actually be
    # carried into the binding, rather than trusted from construction alone.
    _require_launchable_role(packet.role)

    decision, rail = _require_standing_authorization(
        snapshot,
        packet,
        observation,
        slots=slots,
        bindings=bindings,
        in_flight_session_ids=in_flight_session_ids,
    )
    workspace = _require_workspace(observation)

    # Last thing before enactment: the packet must still be the current head. The
    # accepted rule, called rather than restated.
    require_current(packet, snapshot)

    assignment = Assignment(
        project=packet.project,
        ticket=packet.ticket,
        rail=packet.rail,
        role=packet.role,
        head=packet.head,
        iteration=RailIteration(rail=packet.rail, blob=rail.authorization_blob),
        workspace_key=workspace.workspace_key,
        worktree_id=workspace.worktree_id,
        workspace_path=workspace.workspace_path,
    )

    launch_arguments = dict(launch_kwargs or {})
    identity = None
    if ledger is not None:
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
        if identity is not None:
            ledger.record_failure(identity, error)
        raise

    if identity is not None:
        try:
            ledger.record_completed(identity, launched.result)
        except Exception:
            # The session exists and the accounting for it failed, so it is stopped
            # here rather than handed back: an unaccounted live session is the one
            # thing worse than a failed launch.
            stop_session(store, registry, launched.binding, **dict(stop_kwargs or {}))
            raise

    return OpenSession(assignment=assignment, launched=launched)


def invoke_role(
    snapshot: ScopeSnapshot,
    packet: RolePacket,
    observation: ControlPlaneObservation,
    *,
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
    """One gated launch of exactly one executor- or reviewer-role session, then stopped.

    Raises `InvocationRefused` before anything is enacted when any gate fails, and
    lets a lifecycle failure propagate unchanged: a failed invocation is not
    retried, resumed, or terminalized without proof. Every accepted rule below this
    call -- reservation order, the commit-point ceiling, the readiness handshake,
    the ownership record, the stop that proves the process group gone -- is reached
    exactly as the accepted path reaches it, because this calls the same functions
    with the same arguments and adds none of its own.

    `while_running` is the one instant at which this session is live and provable,
    offered to the caller and nothing else, on exactly the accepted terms: it is
    given the `LaunchOutcome`, asked for nothing back, and if it raises, the session
    is stopped and the failure propagates rather than being swallowed.

    Accounting mirrors the accepted invocation exactly. A door that spent real
    provider budget without the accounting the other door has would be a worse door,
    not a smaller one.

    This function stops what it started before it returns, and that has not changed.
    What changed at checkpoint 74 is that it no longer *refuses* when the registry
    already holds someone else's session, because refusing that was the thing
    standing between this package and a concurrent driver. The single-session shape
    of this call is now a property of this function alone rather than a rule it
    imposed on every caller of the module.
    """
    opened = open_role_session(
        snapshot,
        packet,
        observation,
        store=store,
        registry=registry,
        reference=reference,
        request_kwargs=request_kwargs,
        package_root=package_root,
        slots=slots,
        bindings=bindings,
        in_flight_session_ids=in_flight_session_ids,
        markers=markers,
        launch_kwargs=launch_kwargs,
        stop_kwargs=stop_kwargs,
        ledger=ledger,
    )
    assignment = opened.assignment
    launched = opened.launched

    if while_running is not None:
        try:
            while_running(launched)
        except Exception:
            stop_session(store, registry, launched.binding, **dict(stop_kwargs or {}))
            raise

    stopped = stop_session(store, registry, launched.binding, **dict(stop_kwargs or {}))

    return InvocationOutcome(
        project=assignment.project,
        ticket=assignment.ticket,
        head=assignment.head,
        rail=assignment.rail,
        role=assignment.role,
        session_id=stopped.session_id,
        iteration_blob=assignment.iteration.blob,
        # Empty, and literally true: nothing woke this session. A role launch is
        # stated by a human at the command line, not proposed by a trigger, and
        # there is no wake kind that could name one.
        wake_rails=(),
        binding_state=stopped.binding.state,
        process_group_gone=stopped.process_group_gone,
        graceful=stopped.graceful,
    )
