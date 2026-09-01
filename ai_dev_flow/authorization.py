"""The controller's authorization predicate: a pure decision, never an action."""

from __future__ import annotations

# Everything a controller may do comes from one freshly resolved control-plane
# observation. This module decides whether that observation authorizes launching a
# session on a rail, continuing the session already bound to it, or nothing at all
# -- and it does so without touching Git, the provider, a process, the binding
# store, or any control-plane artifact.
#
# It deliberately parses nothing. Rail status, dependencies, shared resources,
# freshness, workspace claims, and cross-scope discovery are already owned by
# `control_plane` and `workspaces`; their results arrive here normalized. Keeping
# the predicate pure is what makes "refusal performs no action" a property of the
# code rather than a promise about the caller.

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .session_binding import (
    BINDING_ROLES,
    BindingRecord,
    RailIteration,
)


# What the controller may be authorized to do. Nothing else is decided here.
ACTION_LAUNCH = "launch"
ACTION_CONTINUE = "continue"

# Only `running` is dispatched authorization. `ready` is eligible work the
# orchestrator has not dispatched, and marking it `running` is the orchestrator's
# act -- so treating `ready` as executable would let the controller dispatch
# itself. Launch and continuation are told apart by binding state, not by status:
# no live binding means launch, a bound one means continuation, and a reserved one
# means a launch is already under way and neither is authorized.
DISPATCHED_RAIL_STATUS = "running"

# Whether the snapshot describes the whole scope or only part of it.
OBSERVATION_COMPLETE = "complete"
OBSERVATION_PARTIAL = "partial"

# Whether the coordination repository's own state is safe to act on. Only
# `published` is: unpushed or diverged local state is not the shared surface, and
# `unknown` means freshness was never established.
SOURCE_HEALTH_PUBLISHED = "published"
SOURCE_HEALTH_UNPUSHED = "unpushed"
SOURCE_HEALTH_DIVERGED = "diverged"
SOURCE_HEALTH_UNKNOWN = "unknown"

COMPLETED_RAIL_STATUS = "completed"

# Stable refusal reasons, in the order they are evaluated.
REASON_INVALID_ROLE = "invalid-role"
REASON_SCOPE_MISMATCH = "scope-mismatch"
REASON_OBSERVATION_INCOMPLETE = "observation-incomplete"
REASON_SOURCE_UNHEALTHY = "source-unhealthy"
REASON_HEAD_MISMATCH = "head-mismatch"
REASON_RAIL_MISSING = "rail-missing"
REASON_RAIL_ROLE_MISSING = "rail-role-missing"
REASON_RAIL_ROLE_MISMATCH = "rail-role-mismatch"
REASON_RAIL_DUPLICATED = "rail-duplicated"
REASON_RAIL_UNRECONCILED = "rail-unreconciled"
REASON_RAIL_NOT_DISPATCHED = "rail-not-dispatched"
REASON_ITERATION_MISMATCH = "iteration-mismatch"
REASON_DEPENDENCY_UNSATISFIED = "dependency-unsatisfied"
REASON_RESOURCE_CONTENDED = "resource-contended"
REASON_WORKSPACE_IDENTITY_AMBIGUOUS = "workspace-identity-ambiguous"
REASON_BINDING_DUPLICATED = "binding-duplicated"
REASON_BINDING_MISMATCHED = "binding-mismatched"
REASON_INVOCATION_IN_FLIGHT = "invocation-in-flight"
# A launch this controller already started and has not yet attached a process to.
REASON_BINDING_NOT_READY = "binding-not-ready"

# Accepted decision D6. The ceiling is a hard admission limit across every managed
# role, never a target, and never a per-role quota.
REASON_INVALID_CEILING = "invalid-concurrency-ceiling"
REASON_CONCURRENCY_UNPROVABLE = "concurrency-count-unprovable"
REASON_CONCURRENCY_CEILING = "concurrency-ceiling-reached"

REASON_LAUNCH_AUTHORIZED = "launch-authorized"
REASON_CONTINUATION_AUTHORIZED = "continuation-authorized"


@dataclass(frozen=True)
class RailObservation:
    """One rail's deterministic facts, as `control_plane.RailState` reports them,
    plus the blob id of the `rail.md` those facts were read from."""

    identifier: str
    status: str
    rail_blob: str
    # The rail's durable assignment, exactly as the control plane read it. `None`
    # means the rail names no role, which authorizes nothing rather than defaulting
    # to something convenient.
    role: Optional[str] = None
    unreconciled: bool = False
    depends_on: Tuple[str, ...] = ()
    shared_resource: Optional[str] = None


@dataclass(frozen=True)
class WorkspaceObservation:
    """What `workspaces` proved about the worktree the rail would run in.

    `identity_problem` carries `IdentityProblem.detail` verbatim when ownership
    could not be proved; `None` means it was.
    """

    workspace_key: str
    worktree_id: str
    workspace_path: str
    identity_problem: Optional[str] = None


@dataclass(frozen=True)
class ControlPlaneObservation:
    """A complete snapshot of one control-plane scope at one resolved head.

    `foreign_resource_holders` maps a shared-resource id to descriptions of
    holders *outside* this scope. Discovering them belongs to the reader that
    walks other scopes; this module only refuses when the map says a resource is
    held.
    """

    project: str
    ticket: str
    head: str
    rails: Tuple[RailObservation, ...] = ()
    workspace: Optional[WorkspaceObservation] = None
    completeness: str = OBSERVATION_COMPLETE
    source_health: str = SOURCE_HEALTH_PUBLISHED
    foreign_resource_holders: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


# The human-owned default. Changing it is a human concurrency decision, so it is a
# value a caller states rather than something derived from load, history, or the
# context-rotation threshold that happens to share the number.
CONCURRENCY_CEILING_DEFAULT = 6


@dataclass(frozen=True)
class AgentSlots:
    """Manager-wide occupancy at one instant, already reconciled by the caller.

    `occupants` are the sessions proved to hold a slot; `unprovable` are the ones
    whose ownership could not be established at all. They are separate on purpose:
    an unprovable session is never quietly dropped from the total, because
    subtracting it optimistically is precisely how a seventh agent gets admitted.
    """

    ceiling: int
    occupants: Tuple[str, ...] = ()
    unprovable: Tuple[str, ...] = ()

    @property
    def occupied(self) -> int:
        return len(self.occupants)

    @property
    def provable(self) -> bool:
        return not self.unprovable


def reconcile_agent_slots(
    bindings: Iterable[BindingRecord],
    *,
    ownership: Mapping[str, bool],
    ceiling: int = CONCURRENCY_CEILING_DEFAULT,
) -> AgentSlots:
    """Reconcile durable bindings against controller-proved ownership into occupancy.

    Every nonterminal binding occupies a slot, across every project, ticket, rail
    and role: the ceiling counts managed agents, not rails, and separate per-role
    quotas are exactly what the accepted decision forbids.

    A reservation occupies a slot on durable evidence alone. That is not an
    inference about liveness -- it is a launch this controller has already
    committed to, and refusing to count it is what would let a reserved launch and
    a fresh one both pass the same ceiling.

    A bound record needs ownership proved by the controller that holds the handle.
    Absent or negative evidence makes the total unprovable rather than smaller;
    duplicates do the same. Nothing here reads a clock, a transcript, a status
    file, or a pid: whether a handle is live was decided by the caller that owns
    it, and this function only reconciles what it was told.
    """
    seen = set()
    occupants = []
    unprovable = []
    for record in bindings:
        if record.is_terminal:
            continue
        session = record.session_id
        if session in seen:
            unprovable.append(session)
            continue
        seen.add(session)
        if record.is_reserved:
            occupants.append(session)
            continue
        if ownership.get(session) is True:
            occupants.append(session)
        else:
            unprovable.append(session)
    return AgentSlots(
        ceiling=ceiling,
        occupants=tuple(sorted(occupants)),
        unprovable=tuple(sorted(unprovable)),
    )


@dataclass(frozen=True)
class AuthorizationDecision:
    """One authorize-or-refuse answer with exactly one stable reason."""

    authorized: bool
    reason: str
    detail: str
    project: str
    ticket: str
    rail: str
    role: str
    action: Optional[str] = None
    iteration: Optional[RailIteration] = None
    session_id: Optional[str] = None
    head: Optional[str] = None
    # The exact ceiling this decision was made against, so the reservation that
    # follows enforces the same policy value rather than reading its own.
    ceiling: Optional[int] = None

    def __bool__(self) -> bool:
        return self.authorized


def _refuse(reason, detail, *, project, ticket, rail, role, head=None):
    return AuthorizationDecision(
        authorized=False,
        reason=reason,
        detail=detail,
        project=project,
        ticket=ticket,
        rail=rail,
        role=role,
        head=head,
    )


def _matches_assignment(record: BindingRecord, *, project, ticket, rail, role, iteration) -> bool:
    return (
        record.project == project
        and record.ticket == ticket
        and record.rail == rail
        and record.role == role
        and record.iteration == iteration
    )


def authorize(
    observation: ControlPlaneObservation,
    *,
    project: str,
    ticket: str,
    rail: str,
    role: str,
    expected_head: str,
    rail_blob: str,
    slots: AgentSlots,
    bindings: Iterable[BindingRecord] = (),
    in_flight_session_ids: Sequence[str] = (),
) -> AuthorizationDecision:
    """Decide whether this snapshot authorizes launch, continuation, or nothing.

    `rail_blob` is the exact iteration the caller intends to act on. Requiring
    the caller to state it -- rather than reading whatever the rail says now --
    is what makes an authorization that was read at one iteration unusable once
    the orchestrator rewrites the rail.

    `slots` is required and has no default. A caller that cannot state manager-wide
    occupancy gets a `TypeError` rather than a quiet empty set, because a default
    would silently authorize a seventh agent whenever a caller forgot to reconcile
    one -- and the ceiling is worth nothing if it can be bypassed by omission.
    """
    iteration = RailIteration(rail=rail, blob=rail_blob)

    def refuse(reason: str, detail: str) -> AuthorizationDecision:
        return _refuse(reason, detail, project=project, ticket=ticket, rail=rail, role=role,
                       head=observation.head)

    if role not in BINDING_ROLES:
        return refuse(
            REASON_INVALID_ROLE,
            "role '{0}' is not one of {1}.".format(role, ", ".join(BINDING_ROLES)),
        )

    if type(slots.ceiling) is not int or slots.ceiling < 1:
        return refuse(
            REASON_INVALID_CEILING,
            "the configured concurrency ceiling must be a positive whole number of "
            "agents, got {0!r}.".format(slots.ceiling),
        )

    if observation.project != project or observation.ticket != ticket:
        return refuse(
            REASON_SCOPE_MISMATCH,
            "observation covers {0}/{1}, not {2}/{3}.".format(
                observation.project, observation.ticket, project, ticket
            ),
        )

    if observation.completeness != OBSERVATION_COMPLETE:
        return refuse(
            REASON_OBSERVATION_INCOMPLETE,
            "the observation is {0}; a partial snapshot cannot prove a rail is "
            "uncontended.".format(observation.completeness),
        )

    if observation.source_health != SOURCE_HEALTH_PUBLISHED:
        return refuse(
            REASON_SOURCE_UNHEALTHY,
            "coordination source health is '{0}'; only published state is "
            "authoritative.".format(observation.source_health),
        )

    if not observation.head or observation.head != expected_head:
        return refuse(
            REASON_HEAD_MISMATCH,
            "authorization was read at head {0} but the observation is at {1}.".format(
                expected_head, observation.head or "an unresolved head"
            ),
        )

    matching = [state for state in observation.rails if state.identifier == rail]
    if not matching:
        return refuse(REASON_RAIL_MISSING, "no rail '{0}' in {1}/{2}.".format(rail, project, ticket))
    if len(matching) > 1:
        return refuse(
            REASON_RAIL_DUPLICATED,
            "rail '{0}' appears {1} times in the observation.".format(rail, len(matching)),
        )
    state = matching[0]

    # Who a rail assigns is checked before anything a rail permits. A caller may
    # request a role; it may not invent one the rail never granted, so this refuses
    # ahead of dispatch, binding, reservation, and any lifecycle action.
    if not state.role:
        return refuse(
            REASON_RAIL_ROLE_MISSING,
            "rail '{0}' names no durable role, so it cannot authorize a managed "
            "'{1}' session.".format(rail, role),
        )
    if state.role != role:
        return refuse(
            REASON_RAIL_ROLE_MISMATCH,
            "rail '{0}' is assigned to '{1}', not to the requested '{2}'.".format(
                rail, state.role, role
            ),
        )

    if state.unreconciled:
        return refuse(
            REASON_RAIL_UNRECONCILED,
            "rail '{0}' authorizes '{1}' while its handoff proposes a different status; "
            "the orchestrator must reconcile it first.".format(rail, state.status),
        )

    if state.status != DISPATCHED_RAIL_STATUS:
        return refuse(
            REASON_RAIL_NOT_DISPATCHED,
            "rail '{0}' is '{1}'; only '{2}' is dispatched authorization.".format(
                rail, state.status, DISPATCHED_RAIL_STATUS
            ),
        )

    if state.rail_blob != rail_blob:
        return refuse(
            REASON_ITERATION_MISMATCH,
            "rail '{0}' is now iteration {1}, not the {2} this authorization names.".format(
                rail, state.rail_blob, rail_blob
            ),
        )

    by_identifier = {other.identifier: other for other in observation.rails}
    for dependency in state.depends_on:
        other = by_identifier.get(dependency)
        if other is None:
            return refuse(
                REASON_DEPENDENCY_UNSATISFIED,
                "rail '{0}' depends on '{1}', which the observation does not "
                "contain.".format(rail, dependency),
            )
        if other.status != COMPLETED_RAIL_STATUS:
            return refuse(
                REASON_DEPENDENCY_UNSATISFIED,
                "rail '{0}' depends on '{1}', which is '{2}'.".format(rail, dependency, other.status),
            )

    if state.shared_resource is not None:
        contenders = [
            other.identifier
            for other in observation.rails
            if other.identifier != rail
            and other.shared_resource == state.shared_resource
            and other.status == DISPATCHED_RAIL_STATUS
        ]
        foreign = list(observation.foreign_resource_holders.get(state.shared_resource, ()))
        if contenders or foreign:
            return refuse(
                REASON_RESOURCE_CONTENDED,
                "shared resource '{0}' is held by {1}.".format(
                    state.shared_resource, ", ".join(contenders + foreign)
                ),
            )

    workspace = observation.workspace
    if workspace is None:
        return refuse(
            REASON_WORKSPACE_IDENTITY_AMBIGUOUS,
            "the observation carries no proven workspace identity for {0}/{1}.".format(
                project, ticket
            ),
        )
    if workspace.identity_problem is not None:
        return refuse(REASON_WORKSPACE_IDENTITY_AMBIGUOUS, workspace.identity_problem)

    records = list(bindings)
    seen = set()
    for record in records:
        if record.session_id in seen:
            return refuse(
                REASON_BINDING_DUPLICATED,
                "session {0} appears in more than one binding.".format(record.session_id),
            )
        seen.add(record.session_id)

    # Reserved and bound records both occupy the rail. A reservation is a launch
    # this controller has already committed to, so it must not look like an empty
    # rail waiting for another one.
    live = [
        record
        for record in records
        if not record.is_terminal and record.project == project
        and record.ticket == ticket and record.rail == rail
    ]

    if len(live) > 1:
        return refuse(
            REASON_BINDING_DUPLICATED,
            "rail '{0}' has {1} live bindings ({2}); ambiguous identity routes "
            "nothing.".format(rail, len(live), ", ".join(sorted(r.session_id for r in live))),
        )

    if not live:
        if slots.unprovable:
            return refuse(
                REASON_CONCURRENCY_UNPROVABLE,
                "manager-wide agent count is not established: ownership is unprovable "
                "for {0}. A launch is refused rather than admitted against a total that "
                "may already be at the ceiling.".format(", ".join(slots.unprovable)),
            )
        if slots.occupied >= slots.ceiling:
            return refuse(
                REASON_CONCURRENCY_CEILING,
                "{0} of {1} permitted managed agents are already running ({2}); the "
                "ceiling is a hard admission limit.".format(
                    slots.occupied, slots.ceiling, ", ".join(slots.occupants)
                ),
            )
        return AuthorizationDecision(
            authorized=True,
            reason=REASON_LAUNCH_AUTHORIZED,
            detail="rail '{0}' is running at iteration {1} with no live binding.".format(
                rail, rail_blob
            ),
            project=project,
            ticket=ticket,
            rail=rail,
            role=role,
            action=ACTION_LAUNCH,
            iteration=iteration,
            head=observation.head,
            ceiling=slots.ceiling,
        )

    held = live[0]
    if not _matches_assignment(
        held, project=project, ticket=ticket, rail=rail, role=role, iteration=iteration
    ):
        return refuse(
            REASON_BINDING_MISMATCHED,
            "session {0} is {6} to {1}/{2} rail {3} as {4} at iteration {5}; continuing it "
            "here would be an in-place rebinding.".format(
                held.session_id, held.project, held.ticket, held.rail, held.role,
                held.iteration.blob, held.state,
            ),
        )
    if workspace.workspace_key != held.workspace_key or workspace.worktree_id != held.worktree_id:
        return refuse(
            REASON_BINDING_MISMATCHED,
            "session {0} names workspace {1} in worktree {2}, but the observed "
            "workspace is {3} in {4}.".format(
                held.session_id, held.workspace_key, held.worktree_id,
                workspace.workspace_key, workspace.worktree_id,
            ),
        )
    if held.is_reserved:
        # The spawn has not reported back yet. Continuation has nothing to resume
        # and a second launch would duplicate the session this reservation names.
        return refuse(
            REASON_BINDING_NOT_READY,
            "session {0} is reserved for rail '{1}' but has no attached process yet; "
            "neither a second launch nor a continuation is authorized until it is bound "
            "or unbound.".format(held.session_id, rail),
        )
    if held.session_id in set(in_flight_session_ids):
        return refuse(
            REASON_INVOCATION_IN_FLIGHT,
            "session {0} already has an invocation in flight.".format(held.session_id),
        )

    if slots.unprovable:
        # A continuation is evaluated against the same reconciled evidence a launch
        # is. An unknown total is not something a resume may step around.
        return refuse(
            REASON_CONCURRENCY_UNPROVABLE,
            "manager-wide agent count is not established: ownership is unprovable "
            "for {0}.".format(", ".join(slots.unprovable)),
        )
    if held.session_id not in slots.occupants and slots.occupied >= slots.ceiling:
        # Only a continuation that would actually add an agent is refused. A session
        # already counted among the occupants increases nothing, and refusing it at
        # the ceiling would strand precisely the work the ceiling already admitted.
        return refuse(
            REASON_CONCURRENCY_CEILING,
            "{0} of {1} permitted managed agents are already running and session {2} "
            "is not among them.".format(
                slots.occupied, slots.ceiling, held.session_id
            ),
        )

    return AuthorizationDecision(
        authorized=True,
        reason=REASON_CONTINUATION_AUTHORIZED,
        detail="session {0} is the single live binding for rail '{1}' at iteration {2}.".format(
            held.session_id, rail, rail_blob
        ),
        project=project,
        ticket=ticket,
        rail=rail,
        role=role,
        action=ACTION_CONTINUE,
        iteration=iteration,
        session_id=held.session_id,
        head=observation.head,
        ceiling=slots.ceiling,
    )
