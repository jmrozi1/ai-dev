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
# itself. Launch and continuation are distinguished by binding state, not status.
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

REASON_LAUNCH_AUTHORIZED = "launch-authorized"
REASON_CONTINUATION_AUTHORIZED = "continuation-authorized"


@dataclass(frozen=True)
class RailObservation:
    """One rail's deterministic facts, as `control_plane.RailState` reports them,
    plus the blob id of the `rail.md` those facts were read from."""

    identifier: str
    status: str
    rail_blob: str
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
    bindings: Iterable[BindingRecord] = (),
    in_flight_session_ids: Sequence[str] = (),
) -> AuthorizationDecision:
    """Decide whether this snapshot authorizes launch, continuation, or nothing.

    `rail_blob` is the exact iteration the caller intends to act on. Requiring
    the caller to state it -- rather than reading whatever the rail says now --
    is what makes an authorization that was read at one iteration unusable once
    the orchestrator rewrites the rail.
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
        )

    bound = live[0]
    if not _matches_assignment(
        bound, project=project, ticket=ticket, rail=rail, role=role, iteration=iteration
    ):
        return refuse(
            REASON_BINDING_MISMATCHED,
            "session {0} is bound to {1}/{2} rail {3} as {4} at iteration {5}; continuing it "
            "here would be an in-place rebinding.".format(
                bound.session_id, bound.project, bound.ticket, bound.rail, bound.role,
                bound.iteration.blob,
            ),
        )
    if workspace.workspace_key != bound.workspace_key or workspace.worktree_id != bound.worktree_id:
        return refuse(
            REASON_BINDING_MISMATCHED,
            "session {0} is bound to workspace {1} in worktree {2}, but the observed "
            "workspace is {3} in {4}.".format(
                bound.session_id, bound.workspace_key, bound.worktree_id,
                workspace.workspace_key, workspace.worktree_id,
            ),
        )
    if bound.session_id in set(in_flight_session_ids):
        return refuse(
            REASON_INVOCATION_IN_FLIGHT,
            "session {0} already has an invocation in flight.".format(bound.session_id),
        )

    return AuthorizationDecision(
        authorized=True,
        reason=REASON_CONTINUATION_AUTHORIZED,
        detail="session {0} is the single live binding for rail '{1}' at iteration {2}.".format(
            bound.session_id, rail, rail_blob
        ),
        project=project,
        ticket=ticket,
        rail=rail,
        role=role,
        action=ACTION_CONTINUE,
        iteration=iteration,
        session_id=bound.session_id,
        head=observation.head,
    )
