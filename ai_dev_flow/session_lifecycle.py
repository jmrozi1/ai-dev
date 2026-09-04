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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from .authorization import ACTION_CONTINUE, ACTION_LAUNCH, AuthorizationDecision
from .claude_runtime import RuntimeRequest, launch_request, resume_request
from .claude_worker import (
    ClaudeWorkerError,
    WorkerHandle,
    process_group_alive,
    run_request,
    shutdown_worker,
    start_worker,
)
from .context_lifecycle import ContextLifecycleLedger, SessionContextLifecycle
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

# D9's safe boundary, projected the same way every other lifecycle state is: from
# exact current facts, never stored. These are not scheduler states -- nothing
# queues, schedules, or waits on them, and reaching `rotation-ready` performs no
# action at all.
ROTATION_READY = "rotation-ready"
ROTATION_NOT_READY = "not-rotation-ready"
ROTATION_STATES = (ROTATION_READY, ROTATION_NOT_READY)

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

REASON_NOT_MARKED_FOR_ROTATION = "not-marked-for-rotation"
REASON_HANDOFF_NOT_PUBLISHED = "durable-handoff-not-published"
REASON_HANDOFF_NOT_CURRENT = "durable-handoff-not-current"
REASON_WORKTREE_INCOHERENT = "worktree-incoherent"
REASON_ROTATION_HANDOFF_ESTABLISHED = "durable-handoff-established"

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


@dataclass(frozen=True)
class HandoffPublicationObservation:
    """Which handoff publication this controller saw appear, and across which work boundary.

    `publication` is the Git object name of the published bytes -- the identity of
    one publication, taken from the control-plane read that already names a rail
    iteration the same way. It says *which* handoff is being offered and nothing
    whatever about what it says; the handoff itself remains the only carrier of
    outcome, evidence, unresolved work and next action.

    `work_boundary` is the count of invocations this controller had begun when that
    publication appeared. It is not a time, not a sequence anything schedules on,
    and not an event log -- it exists only to answer whether anything happened
    after this publication.
    """

    session_id: str
    publication: str
    work_boundary: int


class SessionRegistry:
    """Controller-local ownership, deliberately not durable.

    Restart empties it, and that is the point: a binding whose handle is gone must
    project Disconnected rather than pretend the controller still owns something.
    """

    def __init__(self, *, rotation_threshold: Any = None) -> None:
        self._owned = {}
        self._in_flight = set()
        # How many invocations of each held session this controller has begun, and
        # which handoff publication it saw appear across one of those boundaries.
        # Both live and die with the handle for exactly the reason ownership does:
        # a work boundary that survived restart would be a claim about work this
        # process never watched.
        self._work_boundary = {}
        self._handoff_publication = {}
        # Context lifecycle lives beside ownership because it is the same kind of
        # claim: what this controller itself observed about a process it holds. It
        # is created and dropped with the handle, which is also the only bound the
        # deduplication memory needs.
        self._context = ContextLifecycleLedger(threshold=rotation_threshold)

    @property
    def rotation_threshold(self) -> int:
        return self._context.rotation_threshold

    def add(self, owned: OwnedSession, *, observed_from_start: bool = False) -> OwnedSession:
        """Take ownership of one session, and start observing its context with it.

        `observed_from_start` defaults to false because taking ownership is not the
        same as having watched a session from its beginning. Only a caller that
        minted the session id and started the process can say otherwise, and a
        caller that adopts an already-running session must report its history as
        unavailable rather than as zero.
        """
        self._owned[owned.session_id] = owned
        self._context.begin(
            owned.session_id, role=owned.role, observed_from_start=observed_from_start
        )
        return owned

    def get(self, session_id: str) -> Optional[OwnedSession]:
        return self._owned.get(session_id)

    def remove(self, session_id: str) -> None:
        self._owned.pop(session_id, None)
        self._in_flight.discard(session_id)
        self._work_boundary.pop(session_id, None)
        self._handoff_publication.pop(session_id, None)
        self._context.forget(session_id)

    def context(self, session_id: str) -> Optional[SessionContextLifecycle]:
        return self._context.get(session_id)

    def observe_context_events(self, session_id: str, events) -> SessionContextLifecycle:
        """Fold one invocation's decoded lifecycle events into that session's state."""
        return self._context.observe(session_id, events)

    def observe_failed_invocation(
        self, session_id: str, detail: str, events=()
    ) -> Optional[SessionContextLifecycle]:
        """Keep what a failed invocation observed, and stop calling this session complete."""
        return self._context.observe_failure(session_id, detail, events)

    def context_readings(self) -> Dict[str, Any]:
        """What this controller may honestly say about each held session's compaction."""
        return self._context.readings()

    def rotation_marked_session_ids(self) -> Tuple[str, ...]:
        return self._context.rotation_marked_session_ids()

    def sessions(self) -> List[OwnedSession]:
        return [self._owned[key] for key in sorted(self._owned)]

    def in_flight(self) -> Tuple[str, ...]:
        return tuple(sorted(self._in_flight))

    def work_boundary(self, session_id: str) -> int:
        """How many invocations of this session this controller has begun.

        A monotonic controller-local count and nothing more. It carries no time, no
        content and no ordering anyone else can observe; its only question is
        whether work happened *after* some other fact this controller recorded.
        """
        return self._work_boundary.get(session_id, 0)

    def handoff_publication(self, session_id: str) -> Optional[HandoffPublicationObservation]:
        """The handoff publication this controller saw appear, and where in its work."""
        return self._handoff_publication.get(session_id)

    def observe_handoff_publication(
        self, session_id: str, publication: Optional[str], *, previous: Optional[str]
    ) -> Optional[HandoffPublicationObservation]:
        """Record a handoff publication that appeared across this session's current boundary.

        `previous` is what the caller's read said *before* the unit of work,
        `publication` what the same read says after it. A publication becomes
        current only by appearing or changing across one unit of work, because that
        crossing is the only thing that shows the bytes were written for the work
        just performed rather than for some earlier boundary. An unchanged read is
        therefore not an observation of currency: it deliberately leaves whatever
        was already recorded standing at the older boundary it was established at,
        which is what makes work performed after a publication visible at all.

        No publication clears the record. A read that could not say which
        publication stands must not leave one behind claiming to be current.

        `previous` is required rather than defaulted so that no caller can record a
        publication without having said what it replaced. `continue_session` is
        where that pair is taken around real work, which is what makes the crossing
        structural instead of a convention a caller has to remember.
        """
        if not publication:
            self._handoff_publication.pop(session_id, None)
            return None
        if previous is not None and publication == previous:
            return self._handoff_publication.get(session_id)
        observation = HandoffPublicationObservation(
            session_id=session_id,
            publication=publication,
            work_boundary=self.work_boundary(session_id),
        )
        self._handoff_publication[session_id] = observation
        return observation

    def begin_invocation(self, session_id: str) -> None:
        if session_id in self._in_flight:
            raise LifecycleError(
                REASON_INVOCATION_IN_FLIGHT,
                "session {0} already has an invocation in flight.".format(session_id),
            )
        self._in_flight.add(session_id)
        # One unit of work is starting, so this session is leaving the boundary any
        # earlier publication was written for. Only a publication observed across
        # this invocation can describe the boundary it ends at. A refused
        # double-begin does not reach here, and so does not move the boundary.
        self._work_boundary[session_id] = self._work_boundary.get(session_id, 0) + 1

    def end_invocation(self, session_id: str) -> None:
        self._in_flight.discard(session_id)


def single_liveness_snapshot(alive=None):
    """One prober whose answer per process group is fixed for the caller's read.

    Liveness is a fact about an instant, and a caller that needs it more than once
    within a single read needs it *from one instant* -- otherwise it can combine a
    session that was live when ownership was proved with the same session already
    gone when its state was projected, and describe a moment that never existed.
    That is not a hypothetical: proving ownership and projecting a state are two
    separate consumers of this question, and an ordinary worker exit lands between
    them often enough to matter.

    So this resolves the accepted default prober once and answers each process
    group once, reusing that exact observation for the rest of the read.

    It is deliberately **not** a cache. Nothing here is durable, nothing is shared
    between reads, and nothing is invalidated or refreshed, because there is
    nothing to invalidate: the snapshot is created by one read and dies with it.
    The next read asks again, from scratch, and gets that instant's truth. Holding
    one of these open across reads would be the durable liveness cache the
    lifecycle refuses, and it would be a claim about a process nobody re-observed.
    """
    prober = alive if alive is not None else process_group_alive
    observed = {}

    def observe(pgid) -> bool:
        if pgid not in observed:
            observed[pgid] = bool(prober(pgid))
        return observed[pgid]

    return observe


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


def ownership_evidence(
    registry: SessionRegistry,
    bindings: Sequence[BindingRecord],
    *,
    alive=None,
) -> Mapping[str, bool]:
    """Which bound sessions this controller can actually prove it still owns.

    One entry per nonterminal bound binding: `True` only when this controller holds
    a matching handle whose process group is still there, which is the same proof
    `require_owned` demands before any action. Anything less is `False` -- a missing
    handle after restart, a handle that disagrees with its record, or a process
    group that is gone.

    `False` deliberately does not mean "not running". It means this controller
    cannot prove what that session is doing, which is the Disconnected reading, and
    the admission reconciler treats it as an unprovable total rather than as a free
    slot. Reserved records are absent from the result because no handle exists for
    them yet; they occupy a slot on their durable record alone.
    """
    evidence = {}
    for record in bindings:
        if record.is_terminal or record.is_reserved:
            continue
        try:
            require_owned(registry, record, alive=alive)
        except LifecycleError:
            evidence[record.session_id] = False
        else:
            evidence[record.session_id] = True
    return evidence


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


def _observe_context(registry: SessionRegistry, session_id: str, result: Any) -> None:
    """Fold one invocation's decoded lifecycle events into manager lifecycle state.

    The events were decoded by the worker and carried on the invocation's own
    channel, so they are folded into exactly the session that invocation was for --
    never into whichever session an event happens to name, which is the one way a
    compaction could be counted against work it did not happen to.

    A result carrying no events is not a session with no history. It is one turn
    that reported none, and what the session's history is worth saying is already
    decided by its observation health.
    """
    events = result.get("events") if isinstance(result, Mapping) else None
    registry.observe_context_events(session_id, events or ())


def _read_publication(reader: Optional[Callable]) -> Optional[str]:
    """One caller-supplied read of the current handoff publication, or nothing.

    Supplied by the caller for the same reason `RailFacts` is: this module reads no
    repository and shells out to nothing, so a lifecycle fact can never be
    contaminated by a read taken at some other instant than the caller's.

    A failed or absent read is silence, never a raise: the invocation around it
    either happened or did not, and a control-plane read cannot change that after
    the fact. Silence is also never mistaken for evidence -- it clears any recorded
    publication, so an unreadable control plane leaves the boundary unproven and
    rotation readiness fails closed.
    """
    if reader is None:
        return None
    try:
        publication = reader()
    except Exception:
        return None
    return publication or None


def _observe_failed_invocation(registry: SessionRegistry, session_id: str, exc: Exception) -> None:
    """Fold a failed invocation into lifecycle state on a session that survives it.

    The asymmetry this exists for: a failed *launch* stops the process it started and
    drops the session, so nothing is left to misrepresent. A failed *continue*
    deliberately leaves the session continuable -- and that is the only path on which a
    session lives long enough to compact repeatedly. Left alone, such a session went on
    reporting a complete history whose last window nobody finished watching, and threw
    away any compaction that window had already reported.

    Both halves are needed and neither substitutes for the other. The events are kept
    because they were genuinely observed; the health is degraded because the invocation
    that observed them did not finish, so a boundary it never got to report cannot be
    ruled out. This says nothing about *which* failure happened: a timeout and a worker
    fatal leave exactly the same claim, because the thing that changed is the same.

    Only the worker channel's own refusal may carry lifecycle evidence. Any other
    failure still degrades the claim, but an arbitrary exception cannot introduce
    events -- and the events that are carried are folded through the same association,
    shape and identity checks as a successful invocation's, never around them.
    """
    events = exc.events if isinstance(exc, ClaudeWorkerError) else ()
    registry.observe_failed_invocation(
        session_id,
        "an invocation this session's observation depended on failed ({0}: {1})".format(
            type(exc).__name__, exc
        ),
        events,
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
        # The ceiling the authorization was decided against, carried rather than
        # re-read, so the commit-point guard cannot enforce a different policy.
        ceiling=decision.ceiling,
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

    # Observed from the session's authoritative start: this call minted the session
    # id, reserved it before any process existed, and started the process itself, so
    # there is no earlier history for a boundary to have happened in. It is the only
    # place in the product that may say so.
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
        ),
        observed_from_start=True,
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

    _observe_context(registry, owned.session_id, result)
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
    read_handoff_publication: Optional[Callable] = None,
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

    # Which handoff publication stood *before* this unit of work. Reading it here,
    # inside the same bracket that already marks the invocation in flight, is what
    # makes the pair of reads describe one unit of work rather than two moments a
    # caller chose. A publication that is unchanged across the bracket was written
    # for an earlier boundary; only one that appears or changes across it was
    # written for this one.
    publication_before = _read_publication(read_handoff_publication)

    registry.begin_invocation(session_id)
    try:
        result = sender(owned.handle, request, **send_arguments)
    except Exception as exc:
        # The session survives this failure by design, so its lifecycle state must
        # survive it truthfully: whatever compactions the invocation already reported
        # are kept, and the count stops being advertised as a complete history. The
        # failure itself is untouched and still leaves here.
        _observe_failed_invocation(registry, session_id, exc)
        raise
    finally:
        # Cleared regardless of outcome, and without swallowing it: a failed
        # invocation must not leave the session permanently un-continuable.
        registry.end_invocation(session_id)

    # Exact resume is the same session, so its observation is the same observation:
    # the count carries across the resume rather than restarting, and the identity
    # pairs already seen keep a replayed boundary from counting twice.
    _observe_context(registry, session_id, result)
    # And which one stands after it. The failure path above raises before reaching
    # here on purpose: an invocation that failed leaves the boundary it opened
    # unmatched by any publication, which is exactly the work-boundary uncertainty
    # checkpoint 58 already established -- and it fails closed rather than
    # inferring that nothing changed.
    registry.observe_handoff_publication(
        session_id,
        _read_publication(read_handoff_publication),
        previous=publication_before,
    )
    return result


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


# ---------------------------------------------------------------------------
# Rotation readiness -- D9's safe boundary, and deliberately nothing past it
# ---------------------------------------------------------------------------
#
# D9 says a marked session must, "at the next safe boundary", leave durable
# resumable state, and that "only after that safe handoff" may the manager
# terminate the old context. This section answers exactly the first half: has that
# boundary been reached, and is that durable state established? It terminates
# nothing, stops nothing, launches nothing, and writes nothing.
#
# Two choices are worth stating because both were the smaller option.
#
# First, rotation readiness is *projected*, not stored. There is no new scheduler
# state, no new flag on a session, and nothing to invalidate -- for the same
# reason `observe_session` projects rather than caches. A stored `rotation-ready`
# bit would be a claim about a boundary nobody re-observed, and it would be false
# the instant the next invocation starts or the worktree is touched. Projecting it
# means the answer is always about the moment it was asked.
#
# Second, the boundary is composed from facts that already exist: the invocation
# the registry already tracks, the threshold mark checkpoint 57 already computes,
# the binding the store already holds, the rail authorization the control plane
# already publishes, and the executor handoff already required on that rail. This
# invents no artifact and no second collaboration model. It is the design test for
# this slice, applied to itself: a replacement agent becomes ready by relying on
# exactly the durable facts a fresh agent on this rail would already resolve.
#
# Third, an existing handoff is not the same thing as a *current* one. D9 requires
# the handoff to carry "its current outcome and evidence", so the boundary is not
# reached merely because some handoff sits at the rail's canonical path: an
# earlier handoff, followed by further authorized work, is an ordinary state on a
# persistent rail, and handing that to a replacement loses precisely the work no
# transcript is allowed to recover. Currency is therefore proved from two
# deterministic facts and no prose whatsoever -- the Git object name of the
# published bytes, which says *which* publication is being offered, and this
# controller's own count of the invocations it began, which says whether anything
# happened after it. The handoff remains the single carrier of *what* the work
# says; nothing here reads a word of it, and no second representation of it
# exists. When either fact is missing, readiness fails closed, like every other
# not-ready path here.
#
# Fourth, those two facts bracket a publication but do not order it. They are both
# taken from outside a provider turn, and a turn is opaque: a handoff published
# midway through one, with further work after it, presents this controller with
# exactly the observations a handoff published at the end of one does. The
# distinguishing fact does not exist out here to be taken, so it is taken where it
# does exist -- at the instant of publication, by the publishing act, which records
# the product state those bytes were written against. Readiness then asks one
# question of it: does the repository still stand there? A commit after publication
# moves it; work carrying no commit leaves the tree dirty, which the coherent-
# workspace requirement already refuses. That pair is the ordering proof, and it is
# structural in the only place a structure could hold it.
#
# What it deliberately does not do is treat every act after publication as work.
# The boundary is the product repository, because that is what a replacement
# resumes from and what D9 names first. Allocating a receipt in the coordination
# repository -- which the supported executor path performs *after* publishing --
# moves no product state and invalidates nothing.


@dataclass(frozen=True)
class WorktreeFacts:
    """What a fresh read of one workspace said. Facts, not a judgement.

    Supplied by the caller for the same reason `RailFacts` is: this module reads no
    repository and shells out to nothing, so a projection can never be contaminated
    by a read taken at some other instant than the caller's.
    """

    worktree_id: str
    path: str
    clean: bool
    # A rebase, merge, cherry-pick or bisect in progress. Present means the
    # repository is mid-operation, which is exactly the ambiguous state a rotation
    # must not hand to a replacement.
    active_operation: Optional[str] = None
    # Where this repository stands now: the commit name a fresh read returned. An
    # identity, like every other fact here -- it is only ever compared with another
    # commit name for equality, never ordered, dated, or counted. Optional because a
    # read that could not name the head must be able to say so, and a readiness that
    # cannot name it fails closed.
    head: Optional[str] = None

    @property
    def coherent(self) -> bool:
        return self.clean and not self.active_operation


@dataclass(frozen=True)
class RotationHandoffFacts:
    """What a fresh control-plane read said about one rail's published handoff.

    Presence and location only. The handoff's *content* contract -- outcome and
    evidence, unresolved work, the exact next action -- is the existing executor
    handoff convention, enforced where it already is: by the reviewer and
    orchestrator loop that reads it. Re-parsing that prose here would invent a
    schema the published handoffs in this ticket do not carry, and would make the
    lifecycle a second judge of work it does not own.

    `publication` is the Git object name the same control-plane read returned for
    those bytes. It is an identity, not content: it distinguishes one publication
    from another without saying anything about either, which is exactly the
    distinction currency needs and the most this layer is entitled to know. It is
    optional because an observation that cannot name the publication must be able
    to say so, and a readiness that cannot name it fails closed.

    `work_state` is the product-repository state those bytes were published
    *against*, recorded by the publishing act itself at the instant it made them
    durable. It is the same kind of fact as `publication` -- a commit name compared
    only for equality -- and it is the one fact this controller cannot take for
    itself, because the moment it describes is inside a provider turn this
    controller cannot see into. Optional for the same reason as `publication`: a
    publication that made no such claim must be able to say so, and readiness then
    fails closed.
    """

    rail: str
    published: bool
    location: str
    publication: Optional[str] = None
    work_state: Optional[str] = None


@dataclass(frozen=True)
class RotationHandoff:
    """Exactly the durable facts a fresh agent on this rail would already resolve.

    Every field is copied from durable state that existed before this slice: the
    binding record carries project, ticket, workspace, rail, iteration, role and
    the head the session was launched at; the control plane carries the rail
    authorization and the published handoff. Nothing here is derived from a
    transcript, and nothing here is readable only by a rotating agent -- this is
    the `rail` invocation and the workspace a fresh executor starts from.
    """

    project: str
    ticket: str
    rail: str
    iteration_blob: str
    role: str
    workspace_key: str
    worktree_id: str
    workspace_path: str
    launched_at_head: str
    handoff_location: str
    handoff_publication: str
    session_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "ticket": self.ticket,
            "rail": self.rail,
            "iteration": self.iteration_blob,
            "role": self.role,
            "workspaceKey": self.workspace_key,
            "worktreeId": self.worktree_id,
            "workspacePath": self.workspace_path,
            "launchedAtHead": self.launched_at_head,
            "handoff": self.handoff_location,
            # Which publication was proven current, so what a replacement is handed
            # is one exact set of bytes rather than a path that may have moved on
            # between the projection and the reading of it.
            "handoffPublication": self.handoff_publication,
            "sessionId": self.session_id,
        }


@dataclass(frozen=True)
class RotationReadiness:
    """One session's rotation boundary, projected from exact current facts.

    `handoff` is present only when the state is `rotation-ready`, because a handoff
    locator on a session that is not ready would be an invitation to act on it.
    """

    session_id: str
    rail: str
    state: str
    reason: str
    detail: str
    observed: int
    threshold: int
    handoff: Optional[RotationHandoff] = None

    @property
    def ready(self) -> bool:
        return self.state == ROTATION_READY


def evaluate_rotation_readiness(
    rail: Optional[RailFacts],
    record: Optional[BindingRecord],
    registry: SessionRegistry,
    *,
    handoff: Optional[RotationHandoffFacts],
    worktree: Optional[WorktreeFacts],
) -> RotationReadiness:
    """Project whether one marked session has reached D9's safe rotation boundary.

    Refuses rather than answers when the durable identity is missing or
    contradictory, and answers `not-rotation-ready` when the boundary simply has
    not been reached. The difference is deliberate: an absent condition is a fact
    about now, while a contradiction between the binding, the rail and the
    workspace means this controller cannot say which session it is describing --
    and guessing which durable record is right is exactly what D9 forbids before a
    context could be replaced.

    Nothing here mutates anything, on any path.
    """
    if rail is None or record is None or handoff is None or worktree is None:
        raise LifecycleError(
            REASON_OBSERVATION_INCOMPLETE,
            "a rotation boundary needs a rail observation, a binding, a handoff "
            "observation and a worktree observation; got rail={0!r} binding={1!r} "
            "handoff={2!r} worktree={3!r}.".format(
                rail, record, handoff, worktree
            ),
        )
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; a terminal binding has no context to rotate.".format(
                record.session_id, record.state
            ),
        )
    if record.state != BINDING_STATE_BOUND:
        raise LifecycleError(
            REASON_BINDING_NOT_BOUND,
            "session {0} is {1}; only a bound session has a context that could be "
            "rotated.".format(record.session_id, record.state),
        )
    # The same three identity refusals `observe_session` already makes, for the same
    # reason: a binding and a rail that disagree describe no single piece of work.
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
            "session {0} is bound at iteration {1} but rail {2} is now {3}; the "
            "durable next action a replacement would resume is not the one this "
            "session was launched for.".format(
                record.session_id, record.iteration.blob, rail.identifier, rail.rail_blob
            ),
        )
    if handoff.rail != record.rail:
        raise LifecycleError(
            REASON_SCOPE_MISMATCH,
            "the handoff observation is for rail {0}, the binding for {1}.".format(
                handoff.rail, record.rail
            ),
        )
    if (worktree.worktree_id, worktree.path) != (record.worktree_id, record.workspace_path):
        raise LifecycleError(
            REASON_SCOPE_MISMATCH,
            "the worktree observation is of {0} at {1}, the binding names {2} at "
            "{3}.".format(
                worktree.worktree_id, worktree.path,
                record.worktree_id, record.workspace_path,
            ),
        )

    context = registry.context(record.session_id)
    reading = context.reading() if context is not None else None

    def projected(state: str, reason: str, detail: str, carried=None) -> RotationReadiness:
        return RotationReadiness(
            session_id=record.session_id,
            rail=record.rail,
            state=state,
            reason=reason,
            detail=detail,
            observed=reading.observed if reading is not None else 0,
            threshold=(
                reading.threshold if reading is not None else registry.rotation_threshold
            ),
            handoff=carried,
        )

    # 1. Marked, and only marked. Rotation is D9's threshold consequence, not
    #    something a boundary alone earns. `is not True` is the point: an
    #    undetermined mark on a partial history has not proven the threshold, and
    #    undetermined is not permission.
    if reading is None or reading.rotation_marked is not True:
        return projected(
            ROTATION_NOT_READY,
            REASON_NOT_MARKED_FOR_ROTATION,
            "session {0} is not marked for rotation, so there is no boundary to "
            "reach.".format(record.session_id),
        )

    # 2. The whole of the safe boundary: no invocation of this session is in
    #    flight. The registry already knows this exactly -- `begin_invocation`
    #    records it and `end_invocation` clears it on every path including failure
    #    -- so the boundary is *between* two bounded commands, which is the
    #    smallest deterministic instant at which nothing is interrupted. It needs
    #    no timer, no quiescence heuristic, and no cooperation from the agent.
    if record.session_id in registry.in_flight():
        return projected(
            ROTATION_NOT_READY,
            REASON_INVOCATION_IN_FLIGHT,
            "session {0} has an invocation in flight; a rotation boundary never "
            "interrupts one.".format(record.session_id),
        )

    # 3. No ambiguous product state. A replacement resumes from the repository, so
    #    a dirty tree or a half-finished git operation is work only the predecessor
    #    could explain -- which is the one thing a rotation must never require.
    if not worktree.coherent:
        return projected(
            ROTATION_NOT_READY,
            REASON_WORKTREE_INCOHERENT,
            "workspace {0} is {1}; a replacement cannot be handed a repository "
            "state only the predecessor could explain.".format(
                record.workspace_path,
                "mid-{0}".format(worktree.active_operation)
                if worktree.active_operation
                else "not clean",
            ),
        )

    # 4. The durable handoff this rail already requires is published. This is where
    #    outcome and evidence, unresolved work and the exact next action live, and
    #    where a fresh agent already reads them.
    if not handoff.published:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_PUBLISHED,
            "rail {0} has no published handoff at {1}, so the outcome, unresolved "
            "work and next action a replacement would resume are not durable.".format(
                record.rail, handoff.location
            ),
        )

    # 5. And that publication is the current one. `published` says a handoff
    #    exists; this says the one that exists was written for the boundary this
    #    session is standing at. Both facts are mechanical: the object name of the
    #    published bytes, and this controller's own count of invocations begun. No
    #    part of the handoff's text is read, here or anywhere in this module.
    boundary = registry.work_boundary(record.session_id)
    observation = registry.handoff_publication(record.session_id)
    if handoff.publication is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the observation of {0} does not name which publication is there, so "
            "whether it is the one describing session {1}'s current work cannot be "
            "established.".format(handoff.location, record.session_id),
        )
    if observation is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "a handoff is published at {0}, but this controller never observed one "
            "appear across any of session {1}'s {2} invocations, so it cannot say "
            "the published handoff describes work this session performed.".format(
                handoff.location, record.session_id, boundary
            ),
        )
    if observation.publication != handoff.publication:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} is {1}, but the publication this "
            "controller saw established for session {2} is {3}; the one on offer is "
            "not the one whose currency was proven.".format(
                handoff.location, handoff.publication, record.session_id,
                observation.publication,
            ),
        )
    if observation.work_boundary != boundary:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} was established at work boundary {1} and "
            "session {2} is at work boundary {3}; {4} further invocation(s) have "
            "begun since, so it does not carry the outcome, evidence, unresolved "
            "work and next action a replacement would resume from.".format(
                handoff.location, observation.work_boundary, record.session_id,
                boundary, boundary - observation.work_boundary,
            ),
        )

    # 6. And nothing was done after it. Every check above brackets the publication
    #    between two moments this controller chose, which is the most it can see:
    #    one provider turn is opaque, so a handoff published in the middle of a turn
    #    and a handoff published at the end of one are the same two observations. The
    #    fact that separates them cannot be taken from out here at all -- it has to
    #    be taken at the instant of publication, by the act that publishes.
    #
    #    So the publication carries the product state it was written against, and
    #    this compares it with where the repository stands now. Equal means no
    #    commit landed after those bytes were written; and work that landed no
    #    commit leaves the tree dirty, which check 3 already refused. Between them
    #    the two cover every way product work survives a turn, which is why this is
    #    an ordering proof and not an ordering convention.
    #
    #    It is deliberately the *product* repository and not "anything that ran".
    #    The supported path allocates a receipt after publishing, in the
    #    coordination repository; that changes no product state, alters nothing a
    #    replacement resumes, and must not invalidate a handoff that is otherwise
    #    current.
    if worktree.head is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the observation of workspace {0} does not name where the repository "
            "stands, so whether work followed the handoff published at {1} cannot "
            "be established.".format(record.workspace_path, handoff.location),
        )
    if handoff.work_state is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} does not record which product state it "
            "was written against, so it cannot be shown to follow the last work of "
            "session {1}'s boundary.".format(handoff.location, record.session_id),
        )
    if handoff.work_state != worktree.head:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} was written against product state {1} "
            "and workspace {2} now stands at {3}; work landed after that "
            "publication, so it does not carry the outcome, evidence, unresolved "
            "work and next action a replacement would resume from.".format(
                handoff.location, handoff.work_state,
                record.workspace_path, worktree.head,
            ),
        )

    return projected(
        ROTATION_READY,
        REASON_ROTATION_HANDOFF_ESTABLISHED,
        "session {0} is marked at {1} of {2} observed compactions, is between "
        "invocations, has a coherent workspace, and its rail carries handoff "
        "publication {3}, established at the work boundary {4} this session is "
        "still standing at and written against product state {5}, which is where "
        "the workspace still stands. Nothing is terminated or replaced by "
        "this.".format(
            record.session_id, reading.observed, reading.threshold,
            handoff.publication, boundary, handoff.work_state,
        ),
        carried=RotationHandoff(
            project=record.project,
            ticket=record.ticket,
            rail=record.rail,
            iteration_blob=record.iteration.blob,
            role=record.role,
            workspace_key=record.workspace_key,
            worktree_id=record.worktree_id,
            workspace_path=record.workspace_path,
            launched_at_head=record.launched_at_head,
            handoff_location=handoff.location,
            handoff_publication=handoff.publication,
            session_id=record.session_id,
        ),
    )
