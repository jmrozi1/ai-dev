"""The lifecycle coordinator: composes the accepted primitives without loosening any of them."""

from __future__ import annotations

# Four accepted modules already answer four separate questions. `authorization`
# decides whether an action is authorized at all; `session_binding` records which
# session an assignment belongs to and when a real process backed it;
# `claude_runtime` constructs a request that cannot reach an ambient source; and
# `claude_worker` owns the process. This module is the order in which they are
# asked, and nothing more.
#
# Order is the substance here, not glue. A launch request must be built while its
# binding is still `reserved`, because attaching a process turns that record
# `bound` and a launch is authorized by a reservation -- get the sequence wrong and
# the request cannot be constructed at all. A stop must prove the process group is
# gone before the binding is terminalized, or a terminal record would claim
# something about a process still running. And after a controller restart, a
# binding whose owned handle is gone is Disconnected: not a thing to look up by
# pid, adopt, replace, or quietly rebind, because none of those can prove the
# process on the other end is the session the binding names.
#
# Ownership here means a handle this controller itself created and still holds. It
# is deliberately in-memory: a registry that survived restart would be a claim
# about a process this controller can no longer prove anything about, and that
# claim is exactly what Disconnected exists to refuse.
#
# Elapsed time is computed for display and never consulted for a decision. Nothing
# in this module treats duration as evidence of liveness, progress, or authority.

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple
import uuid

from .authorization import ACTION_CONTINUE, ACTION_LAUNCH, AuthorizationDecision
from .claude_runtime import RuntimeRequest, launch_request, resume_request
from .claude_worker import (
    WorkerHandle,
    process_group_alive,
    run_request,
    shutdown_worker,
    start_worker,
)
from .session_binding import (
    BINDING_STATE_BOUND,
    BindingRecord,
    BindingStore,
    RailIteration,
    reserve_binding,
    unbind_session,
)


STATE_WAITING = "waiting"
STATE_RUNNING = "running"
STATE_DISCONNECTED = "disconnected"
PROJECTED_STATES = (STATE_WAITING, STATE_RUNNING, STATE_DISCONNECTED)

RAIL_STATUS_RUNNING = "running"
RAIL_STATUS_BLOCKED = "blocked"

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class LifecycleError(Exception):
    """A fail-closed lifecycle refusal carrying one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


REASON_NOT_AUTHORIZED = "not-authorized"
REASON_WRONG_ACTION = "wrong-action"
REASON_SCOPE_MISMATCH = "scope-mismatch"
REASON_ITERATION_DRIFT = "iteration-drift"
REASON_BINDING_MISSING = "binding-missing"
REASON_BINDING_NOT_BOUND = "binding-not-bound"
REASON_BINDING_TERMINAL = "binding-terminal"
REASON_HANDLE_MISSING = "handle-missing"
REASON_HANDLE_MISMATCH = "handle-mismatch"
REASON_INVOCATION_IN_FLIGHT = "invocation-in-flight"
REASON_SHUTDOWN_INCOMPLETE = "shutdown-incomplete"
REASON_OBSERVATION_INCOMPLETE = "observation-incomplete"
REASON_BLOCKED_WITHOUT_DECISION = "blocked-without-decision"
REASON_RAIL_NOT_RUNNING = "rail-not-running"
REASON_LAUNCH_FAILED = "launch-failed"
REASON_INVALID_TIMESTAMP = "invalid-timestamp"

REASON_DISCONNECTED_NO_HANDLE = "disconnected-no-owned-handle"
REASON_DISCONNECTED_NOT_LIVE = "disconnected-process-group-gone"
REASON_DISCONNECTED_MISMATCH = "disconnected-identity-mismatch"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise LifecycleError(
            REASON_INVALID_TIMESTAMP, "{0} must be a UTC timestamp; got {1!r}.".format(field, value)
        )
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise LifecycleError(
            REASON_INVALID_TIMESTAMP, "{0} {1!r} is not {2}: {3}".format(field, value, _TIMESTAMP_FORMAT, exc)
        ) from exc


def elapsed_seconds(started_at: Any, now: Any) -> int:
    """Display-only age of a state. Never liveness, never authority.

    A clock that moved backwards clamps to zero rather than reporting a negative
    age; a wrong number here should be visibly uninformative, not quietly signed.
    """
    start = _parse_timestamp(started_at, field="startedAt")
    current = _parse_timestamp(now, field="now")
    return max(0, int((current - start).total_seconds()))


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnedSession:
    """A worker this controller started and still holds a handle to.

    Every field is recorded at attachment, from the process that actually started,
    so a later match is a comparison against observed facts rather than against
    what a lookup happens to return now.
    """

    session_id: str
    handle: WorkerHandle
    pid: int
    pid_domain: str
    pgid: int
    started_at: str
    iteration: RailIteration
    workspace_path: str
    role: str

    def mismatches(self, record: BindingRecord) -> Tuple[str, ...]:
        """Every way this handle fails to be the process the binding describes."""
        differences = []
        for label, mine, theirs in (
            ("sessionId", self.session_id, record.session_id),
            ("pid", self.pid, record.pid),
            ("pidDomain", self.pid_domain, record.pid_domain),
            ("startedAt", self.started_at, record.started_at),
            ("iteration", self.iteration, record.iteration),
            ("workspacePath", self.workspace_path, record.workspace_path),
            ("role", self.role, record.role),
        ):
            if mine != theirs:
                differences.append("{0} {1!r} != {2!r}".format(label, mine, theirs))
        return tuple(differences)


class SessionRegistry:
    """Controller-local ownership, deliberately not durable.

    Restart empties it, and that is the point: a binding whose handle is gone must
    project Disconnected rather than pretend the controller still owns something.
    """

    def __init__(self) -> None:
        self._owned = {}
        self._in_flight = set()

    def add(self, owned: OwnedSession) -> OwnedSession:
        self._owned[owned.session_id] = owned
        return owned

    def get(self, session_id: str) -> Optional[OwnedSession]:
        return self._owned.get(session_id)

    def remove(self, session_id: str) -> None:
        self._owned.pop(session_id, None)
        self._in_flight.discard(session_id)

    def sessions(self) -> List[OwnedSession]:
        return [self._owned[key] for key in sorted(self._owned)]

    def in_flight(self) -> Tuple[str, ...]:
        return tuple(sorted(self._in_flight))

    def begin_invocation(self, session_id: str) -> None:
        if session_id in self._in_flight:
            raise LifecycleError(
                REASON_INVOCATION_IN_FLIGHT,
                "session {0} already has an invocation in flight.".format(session_id),
            )
        self._in_flight.add(session_id)

    def end_invocation(self, session_id: str) -> None:
        self._in_flight.discard(session_id)


def require_owned(
    registry: SessionRegistry, record: BindingRecord, *, alive=None
) -> OwnedSession:
    """The exact owned handle for one binding, or a stable refusal naming why not."""
    owned = registry.get(record.session_id)
    if owned is None:
        raise LifecycleError(
            REASON_HANDLE_MISSING,
            "this controller holds no worker handle for session {0}.".format(record.session_id),
        )
    differences = owned.mismatches(record)
    if differences:
        raise LifecycleError(
            REASON_HANDLE_MISMATCH,
            "the owned handle for session {0} does not match its binding: {1}.".format(
                record.session_id, "; ".join(differences)
            ),
        )
    prober = alive if alive is not None else process_group_alive
    if not prober(owned.pgid):
        raise LifecycleError(
            REASON_HANDLE_MISMATCH,
            "process group {0} for session {1} is gone; the handle is stale.".format(
                owned.pgid, record.session_id
            ),
        )
    return owned


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    """The exact scope an action must be authorized for."""

    project: str
    ticket: str
    rail: str
    role: str
    head: str
    iteration: RailIteration
    workspace_key: str
    worktree_id: str
    workspace_path: str


def _require_decision(decision: AuthorizationDecision, assignment: Assignment, *, action: str) -> None:
    if not isinstance(decision, AuthorizationDecision) or not decision.authorized:
        raise LifecycleError(
            REASON_NOT_AUTHORIZED,
            "the authorization decision refuses this action: {0}.".format(
                getattr(decision, "reason", "no decision supplied")
            ),
        )
    if decision.action != action:
        raise LifecycleError(
            REASON_WRONG_ACTION,
            "the decision authorizes {0!r}, not {1!r}.".format(decision.action, action),
        )
    for label, mine, theirs in (
        ("project", assignment.project, decision.project),
        ("ticket", assignment.ticket, decision.ticket),
        ("rail", assignment.rail, decision.rail),
        ("role", assignment.role, decision.role),
        ("head", assignment.head, decision.head),
    ):
        if mine != theirs:
            raise LifecycleError(
                REASON_SCOPE_MISMATCH,
                "the decision is for {0} {1!r}, not {2!r}.".format(label, theirs, mine),
            )
    if decision.iteration != assignment.iteration:
        raise LifecycleError(
            REASON_ITERATION_DRIFT,
            "the decision names iteration {0}, not {1}.".format(
                getattr(decision.iteration, "blob", None), assignment.iteration.blob
            ),
        )


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchOutcome:
    binding: BindingRecord
    owned: OwnedSession
    request: RuntimeRequest
    result: Mapping


def launch_session(
    decision: AuthorizationDecision,
    assignment: Assignment,
    *,
    store: BindingStore,
    registry: SessionRegistry,
    reference: Any,
    request_kwargs: Mapping,
    prompt: str,
    package_root: Any,
    markers: Sequence = (),
    now: Optional[Callable] = None,
    new_session_id: Optional[Callable] = None,
    start: Optional[Callable] = None,
    send: Optional[Callable] = None,
    stop: Optional[Callable] = None,
    environment_source: Optional[Mapping] = None,
    ready_timeout: Optional[float] = None,
    command_timeout: Optional[float] = None,
) -> LaunchOutcome:
    """authorize -> reserve -> build request while reserved -> start/attach -> send.

    The request is built at step three on purpose. Attaching a process turns the
    record `bound`, and `launch_request` is authorized by a reservation, so any
    other ordering cannot construct the request at all -- the failure is
    structural, not incidental.
    """
    _require_decision(decision, assignment, action=ACTION_LAUNCH)
    clock = now if now is not None else _utc_now
    mint = new_session_id if new_session_id is not None else (lambda: str(uuid.uuid4()))
    starter = start if start is not None else start_worker
    sender = send if send is not None else run_request
    stopper = stop if stop is not None else shutdown_worker

    session_id = mint()
    reserved = reserve_binding(
        store,
        project=assignment.project,
        ticket=assignment.ticket,
        reference=reference,
        workspace_path=assignment.workspace_path,
        worktree_id=assignment.worktree_id,
        rail=assignment.rail,
        role=assignment.role,
        iteration=assignment.iteration,
        session_id=session_id,
        launched_at_head=assignment.head,
        reserved_at=clock(),
    )

    # Built here, while the record is still reserved. Nothing has been spawned yet,
    # so a construction failure leaves a reservation and no process.
    request = launch_request(reserved, **dict(request_kwargs))

    start_arguments = {
        "expected_iteration": assignment.iteration,
        "package_root": package_root,
        "now": clock,
    }
    if environment_source is not None:
        start_arguments["environment_source"] = environment_source
    if ready_timeout is not None:
        start_arguments["ready_timeout"] = ready_timeout
    handle, bound = starter(store, reserved, **start_arguments)

    owned = registry.add(
        OwnedSession(
            session_id=bound.session_id,
            handle=handle,
            pid=bound.pid,
            pid_domain=bound.pid_domain,
            pgid=handle.pgid,
            started_at=bound.started_at,
            iteration=bound.iteration,
            workspace_path=bound.workspace_path,
            role=bound.role,
        )
    )

    send_arguments = {"prompt": prompt, "markers": list(markers)}
    if command_timeout is not None:
        send_arguments["timeout"] = command_timeout
    try:
        result = sender(handle, request, **send_arguments)
    except Exception as exc:
        # The process really did start and really did bind, so the record stays
        # bound: that is the truth, and it is what makes the next observation
        # report Disconnected instead of inventing a cleaner story. Only the exact
        # owned process is stopped, and the session id stays consumed.
        try:
            stopper(handle)
        except Exception:
            pass
        registry.remove(owned.session_id)
        raise LifecycleError(
            REASON_LAUNCH_FAILED,
            "session {0} bound to pid {1} but its launch invocation failed: {2}: {3}".format(
                owned.session_id, owned.pid, type(exc).__name__, exc
            ),
        ) from exc

    return LaunchOutcome(binding=bound, owned=owned, request=request, result=result)


# ---------------------------------------------------------------------------
# Continue
# ---------------------------------------------------------------------------


def continue_session(
    decision: AuthorizationDecision,
    assignment: Assignment,
    *,
    store: BindingStore,
    registry: SessionRegistry,
    session_id: str,
    request_kwargs: Mapping,
    prompt: str,
    markers: Sequence = (),
    send: Optional[Callable] = None,
    alive: Optional[Callable] = None,
    command_timeout: Optional[float] = None,
) -> Mapping:
    """Resume exactly the bound session this controller still owns, once at a time."""
    _require_decision(decision, assignment, action=ACTION_CONTINUE)
    record = store.read(session_id)
    if record is None:
        raise LifecycleError(
            REASON_BINDING_MISSING, "no binding for session {0}.".format(session_id)
        )
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; a terminal binding cannot continue.".format(
                session_id, record.state
            ),
        )
    if record.state != BINDING_STATE_BOUND:
        raise LifecycleError(
            REASON_BINDING_NOT_BOUND,
            "session {0} is {1}; only a bound binding can continue.".format(
                session_id, record.state
            ),
        )
    if record.iteration != assignment.iteration:
        raise LifecycleError(
            REASON_ITERATION_DRIFT,
            "session {0} is bound at iteration {1}, not {2}.".format(
                session_id, record.iteration.blob, assignment.iteration.blob
            ),
        )
    if record.rail != assignment.rail or record.role != assignment.role:
        raise LifecycleError(
            REASON_SCOPE_MISMATCH,
            "session {0} is bound to rail {1} as {2}.".format(
                session_id, record.rail, record.role
            ),
        )

    owned = require_owned(registry, record, alive=alive)
    request = resume_request(record, **dict(request_kwargs))
    sender = send if send is not None else run_request

    send_arguments = {"prompt": prompt, "markers": list(markers)}
    if command_timeout is not None:
        send_arguments["timeout"] = command_timeout

    registry.begin_invocation(session_id)
    try:
        return sender(owned.handle, request, **send_arguments)
    finally:
        # Cleared regardless of outcome, and without swallowing it: a failed
        # invocation must not leave the session permanently un-continuable.
        registry.end_invocation(session_id)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RailFacts:
    """What a fresh control-plane read said about one rail.

    `pending_human_decision` is the durable decision fact, not an inference from
    the rail being blocked. A blocked rail with nothing recorded is unexplained,
    and unexplained is not Waiting.
    """

    identifier: str
    status: str
    rail_blob: str
    pending_human_decision: Optional[str] = None


@dataclass(frozen=True)
class SessionProjection:
    state: str
    reason: str
    detail: str
    session_id: str
    rail: str
    elapsed_seconds: int


def observe_session(
    rail: Optional[RailFacts],
    record: Optional[BindingRecord],
    registry: SessionRegistry,
    *,
    now: str,
    alive: Optional[Callable] = None,
) -> SessionProjection:
    """Project one session's state from exact current facts, or refuse to project."""
    if rail is None or record is None:
        raise LifecycleError(
            REASON_OBSERVATION_INCOMPLETE,
            "projection needs both a rail observation and a binding; got rail={0!r} "
            "binding={1!r}.".format(rail, record),
        )
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; a terminal binding projects no live state.".format(
                record.session_id, record.state
            ),
        )
    if record.rail != rail.identifier:
        raise LifecycleError(
            REASON_SCOPE_MISMATCH,
            "binding names rail {0}, the observation names {1}.".format(
                record.rail, rail.identifier
            ),
        )
    if record.iteration.blob != rail.rail_blob:
        raise LifecycleError(
            REASON_ITERATION_DRIFT,
            "session {0} is bound at iteration {1} but rail {2} is now {3}.".format(
                record.session_id, record.iteration.blob, rail.identifier, rail.rail_blob
            ),
        )

    age = elapsed_seconds(record.started_at or record.reserved_at, now)

    def projected(state: str, reason: str, detail: str) -> SessionProjection:
        return SessionProjection(
            state=state, reason=reason, detail=detail,
            session_id=record.session_id, rail=record.rail, elapsed_seconds=age,
        )

    if rail.status == RAIL_STATUS_BLOCKED:
        if rail.pending_human_decision:
            return projected(
                STATE_WAITING, "human-decision-pending", rail.pending_human_decision
            )
        raise LifecycleError(
            REASON_BLOCKED_WITHOUT_DECISION,
            "rail {0} is blocked but durable state records no pending human decision, so "
            "the work is unexplained rather than waiting.".format(rail.identifier),
        )

    try:
        require_owned(registry, record, alive=alive)
    except LifecycleError as exc:
        if exc.reason == REASON_HANDLE_MISSING:
            return projected(
                STATE_DISCONNECTED, REASON_DISCONNECTED_NO_HANDLE, exc.detail
            )
        if "is gone" in exc.detail:
            return projected(
                STATE_DISCONNECTED, REASON_DISCONNECTED_NOT_LIVE, exc.detail
            )
        return projected(STATE_DISCONNECTED, REASON_DISCONNECTED_MISMATCH, exc.detail)

    if rail.status != RAIL_STATUS_RUNNING:
        raise LifecycleError(
            REASON_RAIL_NOT_RUNNING,
            "session {0} is live but rail {1} is {2}; a live session on a rail that is "
            "not running is exactly the ambiguity this refuses.".format(
                record.session_id, rail.identifier, rail.status
            ),
        )
    return projected(STATE_RUNNING, "owned-process-live", "pid {0} is live.".format(record.pid))


# ---------------------------------------------------------------------------
# Stop and recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopOutcome:
    binding: BindingRecord
    session_id: str
    pid: int
    pgid: int
    graceful: bool
    exit_code: Any
    process_group_gone: bool


def stop_session(
    store: BindingStore,
    registry: SessionRegistry,
    record: BindingRecord,
    *,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
) -> StopOutcome:
    """Stop the exact owned process, prove it is gone, and only then terminalize.

    Unbinding first would leave a terminal record asserting something about a
    process that might still be running, which is the one claim this system must
    never make.
    """
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is already {1}.".format(record.session_id, record.state),
        )
    owned = require_owned(registry, record, alive=alive)
    stopper = stop if stop is not None else shutdown_worker
    prober = alive if alive is not None else process_group_alive

    report = stopper(owned.handle)
    if not isinstance(report, Mapping) or not report.get("process_group_gone"):
        raise LifecycleError(
            REASON_SHUTDOWN_INCOMPLETE,
            "shutdown did not prove process group {0} is gone; binding {1} stays "
            "nonterminal.".format(owned.pgid, record.session_id),
        )
    if prober(owned.pgid):
        raise LifecycleError(
            REASON_SHUTDOWN_INCOMPLETE,
            "process group {0} is still alive after shutdown; binding {1} stays "
            "nonterminal.".format(owned.pgid, record.session_id),
        )

    terminal = unbind_session(store, record.session_id)
    registry.remove(record.session_id)
    return StopOutcome(
        binding=terminal,
        session_id=record.session_id,
        pid=owned.pid,
        pgid=owned.pgid,
        graceful=bool(report.get("graceful")),
        exit_code=report.get("exit_code"),
        process_group_gone=True,
    )


@dataclass(frozen=True)
class RecoveryReport:
    """A description of a disconnected session, and nothing done about it."""

    session_id: str
    rail: str
    state: str
    reason: str
    detail: str
    elapsed_seconds: int
    human_decision: str


def recover_session(
    record: BindingRecord, registry: SessionRegistry, *, now: str, alive: Optional[Callable] = None
) -> RecoveryReport:
    """Describe a disconnected binding for a human decision. Perform no recovery.

    Every automatic option here is unsound. A pid lookup, an agent-registry search,
    or process adoption would all bind this controller to a process it cannot prove
    is the session named; a replacement launch would duplicate a session that may
    still be running; clearing identity or rebinding would destroy the only record
    of what happened. So this reports, and a human decides.
    """
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; there is nothing disconnected to recover.".format(
                record.session_id, record.state
            ),
        )
    try:
        require_owned(registry, record, alive=alive)
    except LifecycleError as exc:
        reason = (
            REASON_DISCONNECTED_NO_HANDLE
            if exc.reason == REASON_HANDLE_MISSING
            else REASON_DISCONNECTED_NOT_LIVE
            if "is gone" in exc.detail
            else REASON_DISCONNECTED_MISMATCH
        )
        return RecoveryReport(
            session_id=record.session_id,
            rail=record.rail,
            state=STATE_DISCONNECTED,
            reason=reason,
            detail=exc.detail,
            elapsed_seconds=elapsed_seconds(record.started_at or record.reserved_at, now),
            human_decision=(
                "Session {0} on rail {1} is bound but this controller cannot prove it owns "
                "the process: {2}. Continuation, stop, and rebinding are all refused. A "
                "human decides whether to unbind and reserve a new session id.".format(
                    record.session_id, record.rail, exc.detail
                )
            ),
        )
    raise LifecycleError(
        REASON_HANDLE_MISMATCH,
        "session {0} has a live owned handle; it is not disconnected.".format(record.session_id),
    )
