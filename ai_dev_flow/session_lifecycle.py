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
from typing import (
    Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple,
)
import uuid

from .authorization import (
    ACTION_CONTINUE,
    ACTION_LAUNCH,
    AuthorizationDecision,
    authorize,
)
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
REASON_OLD_CONTEXT_RETIRED = "old-context-retired"
REASON_ROTATION_REQUIRES_RETIREMENT = "rotation-requires-retirement-gate"
REASON_STOP_CATEGORY_UNPROVEN = "stop-category-unprovable"
REASON_SUPERVISED_TEARDOWN = "supervised-teardown-category-unprovable"
REASON_CATEGORY_IS_PROVABLE = "stop-category-provable"

# What one post-turn finalization attempt says about itself. These are reported
# facts on the invocation's own result, never durable state and never authority:
# only `FINALIZATION_ESTABLISHED` records anything at all, and every other value
# leaves the session exactly as un-credited as it was before the turn.
FINALIZATION_ESTABLISHED = "terminal-handoff-established"
FINALIZATION_NOT_ATTEMPTED = "no-finalizer-supplied"
FINALIZATION_ERRORED_TURN = "invocation-reported-an-error"
FINALIZATION_NO_PAYLOAD = "no-terminal-handoff-payload"
FINALIZATION_AMBIGUOUS_PAYLOAD = "ambiguous-terminal-handoff-payload"
FINALIZATION_PUBLICATION_FAILED = "durable-publication-failed"
FINALIZATION_BOOKKEEPING_FAILED = "required-bookkeeping-failed"
FINALIZATION_UNIDENTIFIED = "publication-not-identified"

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
class TerminalFinalization:
    """One handoff *this controller published itself*, after a turn had ended.

    The distinction from an observation is the whole point, and it is the fact
    checkpoints 59-61 could not obtain. An observation says a publication appeared
    somewhere between two reads a controller took; it cannot say whether the agent
    went on working afterwards, because a provider turn is opaque and both
    orderings present the same two reads. This record is not an observation at all:
    it exists only because this controller performed the publishing act, at a
    moment it chose, and that moment is strictly after the agent's provider turn
    ended. Nothing the agent did can follow it inside that invocation, because the
    invocation is over -- and that is an ordering fact about the act, not an
    inference from repository state.

    `publication` is the Git object name of the published bytes, from the same
    control-plane read that already names a rail iteration the same way. It says
    *which* handoff is being offered and nothing about what it says; the handoff
    remains the only carrier of outcome, evidence, unresolved work and next action,
    and nothing here reads a word of it.

    `work_boundary` is checkpoint 60's counter, unchanged: the count of invocations
    this controller had begun when the finalization happened. Not a time, not a
    schedulable sequence, not an event log. It answers one question -- has a further
    invocation begun since -- which is what keeps a finalization from being credited
    across the next unit of work.
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
        self._terminal_finalization = {}
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
        self._terminal_finalization.pop(session_id, None)
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

    def terminal_finalization(self, session_id: str) -> Optional[TerminalFinalization]:
        """The handoff this controller published after this session's turn ended."""
        return self._terminal_finalization.get(session_id)

    def clear_terminal_finalization(self, session_id: str) -> None:
        """Drop any standing credit for this session. Used only to fail closed.

        Called at the start of every post-turn finalization, before any attempt is
        made, so that a turn which produces no creditable finalization can never
        leave an earlier one standing. That earlier record would already fail the
        work-boundary check, so this is belt to that brace -- but the fail-closed
        direction is worth being redundant about.
        """
        self._terminal_finalization.pop(session_id, None)

    def record_terminal_finalization(
        self, session_id: str, publication: Optional[str]
    ) -> Optional[TerminalFinalization]:
        """Credit one publication this controller made after the turn that produced it.

        This is the only way a session becomes rotation-creditable, and it is
        deliberately not reachable from anything an agent does. It records no
        publication it merely *saw*: `finalize_terminal_handoff` is its single
        caller, it runs only after the invocation bracket has closed, and it passes
        the identity returned by the publishing act it just performed.

        A falsy identity records nothing. A publishing act that cannot name what it
        published has not established a boundary, and silence is never evidence.
        """
        if not publication:
            self._terminal_finalization.pop(session_id, None)
            return None
        finalization = TerminalFinalization(
            session_id=session_id,
            publication=publication,
            work_boundary=self.work_boundary(session_id),
        )
        self._terminal_finalization[session_id] = finalization
        return finalization

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


# ---------------------------------------------------------------------------
# Terminal handoff finalization
# ---------------------------------------------------------------------------
#
# The permission this implements is exactly one sentence wide: the controller may
# use the terminal result of a *completed* invocation as transient input to
# deterministic durable handoff finalization. Everything below is an attempt to
# spend no more than that.
#
# It authorizes no transcript replay, no provider-message history, no tool-use
# auditing, no command log, no timestamp, no global sequence, and no second durable
# representation of a handoff. Exactly one string crosses from the provider side,
# it belongs to the one message that ends a turn, it is published verbatim without
# being read, and it is not retained anywhere afterwards. The published handoff
# stays the single canonical artifact and the replacement still reads only that.
#
# One integration requirement is recorded here and deliberately not performed,
# because the surface it lands on belongs to an isolated parallel lane:
#
#   Executor and orchestrator publication must use this supported
#   terminal-finalization path rather than self-certifying rotation currency from
#   an arbitrary mid-turn handoff publication.
#
# Until that is done, the documented executor path publishes mid-turn and this
# controller finalizes nothing, so no session is credited at all. The wiring is
# absent in the fail-closed direction, which is the right place for it to be
# absent while it waits.

# The delimiters the agent wraps its final handoff in. A delimiter, not a schema:
# the controller finds the boundaries and publishes what lies between them without
# parsing, validating or interpreting a word of it. Anything outside them -- an
# agent's closing remarks to its manager, say -- is simply not the handoff, and is
# discarded with the rest of the turn.
HANDOFF_ENVELOPE_BEGIN = "<<<AI-DEV-HANDOFF-BEGIN>>>"
HANDOFF_ENVELOPE_END = "<<<AI-DEV-HANDOFF-END>>>"


def extract_terminal_handoff(payload: Optional[str]) -> Optional[str]:
    """The exact handoff bytes one terminal result carried, or nothing.

    Deliberately the dullest function that can do this. It locates two delimiters
    and returns what is between them; it does not parse the handoff, does not
    validate it, does not normalise it, and could not tell a handoff from a
    shopping list. That is the property that matters: the controller publishes what
    the agent wrote, byte for byte, so no second representation of the handoff
    exists and nothing here becomes a judge of the work.

    More than one envelope is refused rather than resolved. Choosing the first or
    the last would be a guess about which set of bytes the agent meant to be
    durable, and D9 forbids guessing about exactly this. An ambiguous final message
    yields no payload, which yields no finalization, which fails closed.
    """
    if not isinstance(payload, str):
        return None
    if payload.count(HANDOFF_ENVELOPE_BEGIN) != 1:
        return None
    if payload.count(HANDOFF_ENVELOPE_END) != 1:
        return None
    start = payload.index(HANDOFF_ENVELOPE_BEGIN) + len(HANDOFF_ENVELOPE_BEGIN)
    end = payload.index(HANDOFF_ENVELOPE_END)
    if end < start:
        return None
    body = payload[start:end].strip("\n")
    return body if body.strip() else None


def terminal_finalizer(*, publish: Callable, bookkeeping: Optional[Callable] = None):
    """Compose the deterministic durable finalization: publish, then finish the job.

    Two steps, in this order, and the order is the point. `publish` makes the bytes
    durable and returns the identity of what it published. `bookkeeping` is whatever
    the supported path *requires* after that -- pushing the coordination repository,
    allocating a receipt -- and it runs second because it can only run second.

    Publication alone is not enough. The checkpoint-61 review's third scenario is
    precisely this: publish succeeds, the push or the receipt then fails closed, and
    the exact next action and unresolved work have changed while the standing handoff
    still claims otherwise. So a bookkeeping failure is a finalization failure. The
    credit is not awarded, the old context stays alive and continuable, and the
    correct recovery is another bounded invocation whose terminal result carries a
    handoff that says what actually happened. Nothing here terminates anything, so
    "the old context survives" is not a promise this makes but a property it cannot
    violate.

    Neither callable is defaulted. This module reads no repository and shells out to
    nothing, for the same reason `RailFacts` is supplied rather than fetched: a
    durable act taken from in here could not be the caller's own instant.
    """

    def finalize(handoff_bytes: str) -> str:
        try:
            publication = publish(handoff_bytes)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise LifecycleError(
                FINALIZATION_PUBLICATION_FAILED,
                "the handoff could not be made durable ({0}: {1}).".format(
                    type(exc).__name__, exc
                ),
            ) from exc
        if bookkeeping is not None:
            try:
                bookkeeping()
            except Exception as exc:  # noqa: BLE001 - the same, one step later
                raise LifecycleError(
                    FINALIZATION_BOOKKEEPING_FAILED,
                    "the handoff was published but required bookkeeping after it "
                    "failed ({0}: {1}), so the unresolved work and next action a "
                    "replacement would resume have changed.".format(
                        type(exc).__name__, exc
                    ),
                ) from exc
        return publication

    return finalize


def finalize_terminal_handoff(
    registry: SessionRegistry,
    session_id: str,
    result: Any,
    finalize: Optional[Callable],
) -> Dict[str, Any]:
    """Publish the handoff a completed turn ended with, and credit it -- or credit nothing.

    Called once, from exactly one place: after `continue_session`'s invocation
    bracket has closed. That placement is the whole mechanism. The agent's provider
    turn has ended before the first line of this function runs, so no product work,
    no evidence work and no publication of the agent's own can occur after the
    publication this makes, inside this invocation. The next invocation is the next
    invocation, and the work-boundary counter already refuses a finalization
    credited across one.

    Every path other than a complete success credits nothing, and each says which
    path it took. A missing finalizer, an errored turn, a final message with no
    envelope or with two, a publish that raises, bookkeeping that raises, a publish
    that cannot name what it published: all of them leave the session exactly as
    un-credited as it was, and the standing handoff -- if any -- is dropped first so
    that a failed finalization can never coast on an earlier one.

    This never raises. The invocation it follows genuinely happened and genuinely
    succeeded; turning a finalization failure into an invocation failure would
    misreport the work. It reports instead, and readiness fails closed on the
    absence of a credit rather than on an exception.
    """
    registry.clear_terminal_finalization(session_id)

    def reported(state: str, detail: str, publication: Optional[str] = None) -> Dict[str, Any]:
        fact = {"state": state, "detail": detail}
        if publication:
            fact["publication"] = publication
        if isinstance(result, MutableMapping):
            result["finalization"] = fact
        return fact

    if finalize is None:
        return reported(
            FINALIZATION_NOT_ATTEMPTED,
            "no finalizer was supplied, so nothing about session {0}'s handoff was "
            "made durable by this controller.".format(session_id),
        )

    # An invocation that reported a provider error did not complete, and the
    # permission being spent is over the terminal result of a *completed* one.
    # `interpret_result` already drops the payload on that path; this refuses it
    # again here, because a fail-closed condition that only one layer enforces is
    # one refactor away from not being enforced at all.
    if isinstance(result, Mapping) and result.get("is_error"):
        return reported(
            FINALIZATION_ERRORED_TURN,
            "session {0}'s invocation reported a provider error, so it has no "
            "completed turn whose terminal handoff could be made durable.".format(
                session_id
            ),
        )

    payload = result.get("terminal_payload") if isinstance(result, Mapping) else None
    handoff_bytes = extract_terminal_handoff(payload)
    if handoff_bytes is None:
        ambiguous = isinstance(payload, str) and (
            payload.count(HANDOFF_ENVELOPE_BEGIN) > 1
            or payload.count(HANDOFF_ENVELOPE_END) > 1
        )
        return reported(
            FINALIZATION_AMBIGUOUS_PAYLOAD if ambiguous else FINALIZATION_NO_PAYLOAD,
            "session {0}'s terminal result carried {1}, so there is no handoff to "
            "make durable.".format(
                session_id,
                "more than one handoff envelope" if ambiguous
                else "no single delimited handoff",
            ),
        )

    try:
        publication = finalize(handoff_bytes)
    except LifecycleError as exc:
        # The composed finalizer already named which of its two steps failed, and
        # that difference is worth keeping: a failed publication left nothing
        # durable, while failed bookkeeping left a publication whose surrounding
        # facts have since moved. Neither is credited; both are reported as
        # themselves rather than flattened.
        state = (
            FINALIZATION_BOOKKEEPING_FAILED
            if exc.reason == FINALIZATION_BOOKKEEPING_FAILED
            else FINALIZATION_PUBLICATION_FAILED
        )
        return reported(
            state,
            "the durable finalization of session {0}'s terminal handoff failed: "
            "{1} Nothing is credited and the context stays alive.".format(
                session_id, exc.detail
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a failed finalization is a fact, not a crash
        return reported(
            FINALIZATION_PUBLICATION_FAILED,
            "the durable finalization of session {0}'s terminal handoff failed "
            "({1}: {2}); nothing is credited and the context stays alive.".format(
                session_id, type(exc).__name__, exc
            ),
        )

    finalization = registry.record_terminal_finalization(session_id, publication)
    if finalization is None:
        return reported(
            FINALIZATION_UNIDENTIFIED,
            "session {0}'s handoff was published but the act could not name what it "
            "published, so no publication can be proven current.".format(session_id),
        )
    return reported(
        FINALIZATION_ESTABLISHED,
        "session {0}'s terminal handoff was published as {1} at work boundary "
        "{2}, after its provider turn ended.".format(
            session_id, finalization.publication, finalization.work_boundary
        ),
        publication=finalization.publication,
    )


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


@dataclass(frozen=True)
class _NewBinding:
    """One session freshly minted, reserved, started and owned -- and sent nothing.

    There is no `result` field, and the absence is the point: coming into existence
    is not the same act as being given work, so the type that records the first
    cannot accidentally carry evidence of the second.
    """

    binding: BindingRecord
    owned: OwnedSession
    request: RuntimeRequest
    handle: Any


def _reserve_and_bind(
    assignment: Assignment,
    *,
    store: BindingStore,
    registry: SessionRegistry,
    reference: Any,
    request_kwargs: Mapping,
    package_root: Any,
    ceiling: Optional[int],
    clock: Callable,
    mint: Callable,
    starter: Callable,
    environment_source: Optional[Mapping] = None,
    ready_timeout: Optional[float] = None,
) -> _NewBinding:
    """mint -> reserve -> build the request while reserved -> start -> take ownership.

    Module-private, and the only place in this module that brings a managed session
    into existence. Both routes that produce one -- an ordinary `launch_session` and
    the replacement `replace_old_context` binds after a retirement -- reach a live
    binding through here, so the ordering that makes a launch honest is stated once
    rather than restated per route: the session id is minted before anything durable
    exists, the reservation is written before any process is spawned, the request is
    built while that record is still `reserved`, and the record becomes `bound` only
    on the readiness handshake the starter performs.

    It sends nothing and returns no result, because sending is not part of coming
    into existence. A caller that wants work done invokes it afterwards; a caller
    that must not -- checkpoint 66's replacement -- simply does not, and has no
    sender to forget to withhold.
    """
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
        # Stated by the caller, which took it from the decision that authorized this
        # launch. It is carried rather than re-read here so the commit-point guard
        # inside `reserve_binding` cannot enforce a different policy than the one
        # admission was decided against.
        ceiling=ceiling,
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
    return _NewBinding(binding=bound, owned=owned, request=request, handle=handle)


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
    sender = send if send is not None else run_request
    stopper = stop if stop is not None else shutdown_worker

    new = _reserve_and_bind(
        assignment,
        store=store,
        registry=registry,
        reference=reference,
        request_kwargs=request_kwargs,
        package_root=package_root,
        # The ceiling the authorization was decided against, carried rather than
        # re-read, so the commit-point guard cannot enforce a different policy.
        ceiling=decision.ceiling,
        clock=now if now is not None else _utc_now,
        mint=new_session_id if new_session_id is not None else (lambda: str(uuid.uuid4())),
        starter=start if start is not None else start_worker,
        environment_source=environment_source,
        ready_timeout=ready_timeout,
    )
    bound = new.binding
    handle = new.handle
    owned = new.owned
    request = new.request

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
    finalize_handoff: Optional[Callable] = None,
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
    # The turn is over. `sender` returned, which means the provider emitted its
    # terminal result and this worker is idle awaiting the next command; the
    # `finally` above has already cleared in-flight. Only now does the controller
    # publish, and that placement -- not a rule the agent is asked to follow -- is
    # what makes the credited publication follow the agent's last act. There is no
    # ordering left to prove afterwards, because there is no "afterwards" inside
    # this invocation.
    #
    # The failure path above raises before reaching here on purpose: an invocation
    # that failed leaves the boundary it opened with no terminal result to finalize
    # from, which is exactly the work-boundary uncertainty checkpoint 58
    # established, and it fails closed rather than inferring that nothing changed.
    finalize_terminal_handoff(registry, session_id, result, finalize_handoff)
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


# ---------------------------------------------------------------------------
# What kind of stop this is -- established from the session, never from the caller
# ---------------------------------------------------------------------------
#
# D9 marks a session for graceful rotation and then forbids terminating its
# context until a safe handoff has been left behind. That mark is the only
# structural fact separating a rotation-shaped stop from ordinary teardown. It is
# the session's own state, projected by the controller that holds it, so a stop
# does not get to *say* what kind of stop it is -- it is told, from the registry,
# in-call, immediately before anything is destroyed. No parameter carries it, no
# name declares it, and no caller is trusted about it.
#
# The reading is three-valued, and already was, for reasons this module settled
# long before rotation existed: `True` is a mark, `False` is a proof the threshold
# has not been reached, and `None` is the case where the observed floor is under
# the threshold but the history is not complete. Only `False` is ordinary
# teardown. `True` must come through `retire_old_context`. `None` -- a category
# that cannot be established -- is refused rather than read as unmarked, because
# an unprovable category quietly defaulting to an unconditional stop is precisely
# the fail-open this exists to prevent.
#
# This is a precondition, not an act: every refusal here happens before ownership
# has been used for anything and before the stopper is called, so a refused stop
# leaves the session exactly as it was -- owned, bound, nonterminal, continuable.

STOP_CATEGORY_NON_ROTATION = "non-rotation-teardown"
STOP_CATEGORY_ROTATION = "rotation-shaped"
STOP_CATEGORY_UNPROVEN = "category-unprovable"
STOP_CATEGORIES = (
    STOP_CATEGORY_NON_ROTATION,
    STOP_CATEGORY_ROTATION,
    STOP_CATEGORY_UNPROVEN,
)


def stop_category(registry: SessionRegistry, session_id: str) -> str:
    """Which kind of stop this session may receive, read fresh from the registry.

    A session this controller holds no context observation for is `unprovable`
    rather than unmarked: not having watched a session is not evidence about it.
    """
    context = registry.context(session_id)
    if context is None:
        return STOP_CATEGORY_UNPROVEN
    marked = context.reading().rotation_marked
    if marked is True:
        return STOP_CATEGORY_ROTATION
    if marked is False:
        return STOP_CATEGORY_NON_ROTATION
    return STOP_CATEGORY_UNPROVEN


@dataclass(frozen=True)
class _RetirementAuthorization:
    """Proof that this exact stop is the destructive act of `retire_old_context`.

    Module-private and minted in exactly one place: the line in
    `retire_old_context` after every refusal that gate makes has already declined
    to fire. It carries the readiness verdict that gate projected for this exact
    session in this exact call, and `stop_session` accepts it only for the session
    it names and only when that verdict is ready. So it cannot be kept, replayed
    against a second session, or stand in for a projection that never said yes.
    """

    session_id: str
    readiness: Any


def _authorizes(authorization: Any, session_id: str) -> bool:
    """Whether this is a live retirement's own authorization for this session."""
    return (
        isinstance(authorization, _RetirementAuthorization)
        and authorization.session_id == session_id
        and getattr(authorization.readiness, "ready", False) is True
        and getattr(authorization.readiness, "session_id", None) == session_id
    )


def _stop_owned_process(
    store: BindingStore,
    registry: SessionRegistry,
    record: BindingRecord,
    owned: OwnedSession,
    *,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
    _retirement: Any = None,
) -> StopOutcome:
    """End one proven-owned process group, prove it gone, and only then terminalize.

    Module-private, and the only place in this module that destroys anything. Every
    route that stops a session -- ordinary teardown, the retirement gate, and the
    supervised teardown of a session whose category cannot be established -- reaches
    termination through here, which is exactly why the rotation refusal lives here
    rather than in any of them. A route that forgot to ask, or a route added later
    that never thought to, still cannot stop a marked session: the primitive asks
    again, for itself, immediately before the stopper.

    It re-reads the category from the registry rather than being handed one, for the
    same reason `stop_session` reads it in-call: a category carried across a seam is
    a caller's claim about a session, and this module trusts none. The caller has
    already proven ownership -- that is what `owned` is -- so nothing here re-proves
    it, and nothing here is reached by a session this controller cannot hold.
    """
    if stop_category(registry, record.session_id) == STOP_CATEGORY_ROTATION and (
        not _authorizes(_retirement, record.session_id)
    ):
        raise LifecycleError(
            REASON_ROTATION_REQUIRES_RETIREMENT,
            "session {0} is marked for rotation, so stopping it is a rotation and "
            "not teardown; it may only be stopped through `retire_old_context`, "
            "which proves a safe handoff was left behind first. Nothing was "
            "stopped and the binding stays nonterminal.".format(record.session_id),
        )
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


def stop_session(
    store: BindingStore,
    registry: SessionRegistry,
    record: BindingRecord,
    *,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
    _retirement: Any = None,
) -> StopOutcome:
    """Stop the exact owned process, prove it is gone, and only then terminalize.

    Unbinding first would leave a terminal record asserting something about a
    process that might still be running, which is the one claim this system must
    never make.

    Non-rotation teardown only. Whether this stop is a rotation is decided by the
    session's own mark, read here rather than declared by the caller: a marked
    session is refused and must come through `retire_old_context`, and a session
    whose category cannot be established is refused too, and must come through
    `supervised_teardown`, which stops it while reporting the ambiguity rather than
    calling it teardown. `_retirement` is private and is the retirement gate's own
    authorization for this one call; there is no public way to obtain one, and it is
    not a way for a caller to assert anything.
    """
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is already {1}.".format(record.session_id, record.state),
        )
    owned = require_owned(registry, record, alive=alive)
    # Category before consequence. Ownership is proven first because a session
    # nobody can prove they hold is not actionable by any route; the category is
    # then read fresh from the registry, and the refusal below precedes the stopper,
    # as does the rotation refusal the primitive makes for itself, so neither can
    # leave a half-stopped session behind.
    if stop_category(registry, record.session_id) == STOP_CATEGORY_UNPROVEN:
        raise LifecycleError(
            REASON_STOP_CATEGORY_UNPROVEN,
            "session {0} cannot be shown to be unmarked, so this stop cannot "
            "establish that it is teardown rather than a rotation. An unprovable "
            "category is refused rather than treated as unmarked; "
            "`supervised_teardown` is the one route that acts on it, and it reports "
            "the ambiguity rather than resolving it. Nothing was stopped and the "
            "binding stays nonterminal.".format(record.session_id),
        )
    return _stop_owned_process(
        store, registry, record, owned, stop=stop, alive=alive, _retirement=_retirement
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
# Supervised teardown -- the third route, for a category that cannot be established
# ---------------------------------------------------------------------------
#
# Two routes stop a session, and each requires the category to be *established*:
# ordinary teardown requires a session provably below the rotation threshold, and
# the retirement gate requires one provably marked. That is right, and it leaves a
# third state with no route at all. A session whose observation history is not
# complete reads `category-unprovable`, and until now every door refused it: the
# stop refuses an unprovable category, the retirement gate refuses it as
# `not-marked-for-rotation`, and `recover_session` refuses it as not disconnected,
# because its handle is live and owned. Refusing it everywhere is not neutral.
# `reconcile_agent_slots` counts *every* nonterminal binding against D6's ceiling
# of six, so a stop that can never succeed holds a manager slot for as long as the
# controller lives, with no accepted way to release it.
#
# So this route stops it. It is not a bypass, and three separate things make that
# true rather than merely intended.
#
# First, it is a *distinct category*, not a distinct caller. It acts on exactly one
# reading -- `category-unprovable` -- and refuses both established ones: a marked
# session is sent to `retire_old_context`, and a provably unmarked one to the
# ordinary teardown that already accepts it. Nothing about who is asking, or which
# name they called, moves that.
#
# Second, the refusal that matters is below it, not in it. `_stop_owned_process`
# re-reads the category for itself and refuses a marked session without the
# retirement gate's own authorization, so even a defect in the check above cannot
# make this a way to stop a marked session. This route mints no authorization and
# carries none; `retire_old_context` remains the single producer.
#
# Third, it launches and binds nothing -- like every route in this module -- so it
# can never *perform* a rotation. A rotation is a stop followed by a replacement
# continuing from durable state; this is a stop that reports why it could not tell
# what kind of stop it was, and stops there.
#
# What it cannot do is resolve the ambiguity, and it does not pretend to. The
# session it stops *might* have been due for rotation, and if it was, D9's safe
# handoff was never proven. That is precisely what the report says, in the words a
# human needs to act on: the ambiguity, and what to check. Reporting it is the
# honest half of a route that exists because the alternative -- refusing forever --
# strands the session and its slot without reporting anything at all.
#
# Ownership is proven the same way every destructive path here proves it, and a
# session this controller cannot prove it owns still goes to recovery rather than
# to a stop. Termination is proven, never asserted. And when the stop cannot be
# proven, this fails closed exactly as the accepted stop does -- the session
# survives, truthfully nonterminal, still holding its slot -- and says what a human
# must do about it.

SUPERVISED_STOPPED = "supervised-teardown-stopped"
SUPERVISED_REFUSED = "supervised-teardown-refused"
SUPERVISED_STATES = (SUPERVISED_STOPPED, SUPERVISED_REFUSED)


@dataclass(frozen=True)
class SupervisedTeardown:
    """What one supervised teardown did, and the ambiguity it could not resolve.

    `ambiguity` is present on every path, including success: it is the whole reason
    this route exists, and a route that swallowed it would be the bypass this one is
    not. `human_action` is present on every path too, because a stop performed under
    an unresolved category always leaves a human something to check -- and a stop
    that failed leaves them a process to deal with.

    `stopped` is present only when a process group was proven gone, and `recovery`
    only when the session was handed to a human as disconnected instead. Nothing
    carries a replacement, a reservation, or a launch, because this performs none.
    """

    session_id: str
    rail: str
    state: str
    reason: str
    detail: str
    ambiguity: str
    human_action: str
    stopped: Optional[StopOutcome] = None
    recovery: Optional[RecoveryReport] = None

    @property
    def torn_down(self) -> bool:
        return self.state == SUPERVISED_STOPPED


def _ambiguity(registry: SessionRegistry, session_id: str) -> str:
    """Why this controller cannot say what kind of stop this session's stop would be."""
    context = registry.context(session_id)
    if context is None:
        return (
            "this controller holds no context observation for session {0} at all, so "
            "nothing can be said about whether it reached the rotation "
            "threshold.".format(session_id)
        )
    return context.reading().detail


def supervised_teardown(
    store: BindingStore,
    registry: SessionRegistry,
    record: BindingRecord,
    *,
    now: str,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
) -> SupervisedTeardown:
    """Stop a session whose rotation category cannot be established, and report it.

    For `category-unprovable` and nothing else: a marked session is refused to the
    retirement gate, and a provably unmarked one to the ordinary teardown that
    already handles it. Routes a session it cannot prove it owns to
    `recover_session` rather than stopping it, proves the process group gone rather
    than asserting it, and leaves the binding terminal -- which is what releases the
    D6 slot the refusal was holding. Launches nothing and binds nothing.

    Fails closed on a stop it cannot prove: the session survives exactly as it was,
    nonterminal and still occupying its slot, and the report says what a human must
    do. Raises `LifecycleError` when the category is established, because then this
    is the wrong route and the right one is named in the refusal.
    """
    if record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; there is no live context left to tear "
            "down.".format(record.session_id, record.state),
        )
    # One liveness instant for the pre-flight, for the reason the retirement gate
    # takes one: proving ownership and then describing a disconnection are two
    # consumers of the same question. Deliberately not reused across the stop below,
    # whose termination proof must be a new observation.
    preflight = single_liveness_snapshot(alive)
    try:
        owned = require_owned(registry, record, alive=preflight)
    except LifecycleError as exc:
        if exc.reason in (REASON_HANDLE_MISSING, REASON_HANDLE_MISMATCH):
            report = recover_session(record, registry, now=now, alive=preflight)
            return SupervisedTeardown(
                session_id=record.session_id,
                rail=record.rail,
                state=SUPERVISED_REFUSED,
                reason=report.reason,
                detail=report.detail,
                ambiguity=_ambiguity(registry, record.session_id),
                human_action=report.human_decision,
                recovery=report,
            )
        raise
    category = stop_category(registry, record.session_id)
    if category == STOP_CATEGORY_ROTATION:
        raise LifecycleError(
            REASON_ROTATION_REQUIRES_RETIREMENT,
            "session {0} is marked for rotation, so its category is established and "
            "this is not the route for it: supervised teardown acts only on a "
            "category that cannot be established. Stopping a marked session is a "
            "rotation and may only go through `retire_old_context`, which proves a "
            "safe handoff was left behind first. Nothing was stopped and the binding "
            "stays nonterminal.".format(record.session_id),
        )
    if category == STOP_CATEGORY_NON_ROTATION:
        raise LifecycleError(
            REASON_CATEGORY_IS_PROVABLE,
            "session {0} is provably below the rotation threshold, so its stop is "
            "ordinary teardown and needs no supervision; `stop_session` performs it "
            "unchanged. Nothing was stopped and the binding stays "
            "nonterminal.".format(record.session_id),
        )
    ambiguity = _ambiguity(registry, record.session_id)
    try:
        stopped = _stop_owned_process(
            store, registry, record, owned, stop=stop, alive=alive
        )
    except LifecycleError as exc:
        if exc.reason == REASON_ROTATION_REQUIRES_RETIREMENT:
            # The primitive's own refusal, and never softened into a report: a
            # rotation-shaped stop is refused, not described.
            raise
        return SupervisedTeardown(
            session_id=record.session_id,
            rail=record.rail,
            state=SUPERVISED_REFUSED,
            reason=exc.reason,
            detail=exc.detail,
            ambiguity=ambiguity,
            human_action=(
                "Supervised teardown of session {0} on rail {1} could not be "
                "completed: {2} The binding is deliberately left nonterminal and "
                "still occupies one of this manager's agent slots, because a "
                "terminal record would claim this process group is gone when it "
                "cannot be shown to be. Nothing was launched in its place. A human "
                "must establish on the host whether process group {3} (pid {4}) is "
                "still running, end it if it is, and only then decide whether to "
                "unbind session {0}. The rotation category was never established "
                "either ({5}), so the work this session held may not have been "
                "handed off.".format(
                    record.session_id, record.rail, exc.detail, owned.pgid, owned.pid,
                    ambiguity,
                )
            ),
        )
    return SupervisedTeardown(
        session_id=stopped.session_id,
        rail=record.rail,
        state=SUPERVISED_STOPPED,
        reason=REASON_SUPERVISED_TEARDOWN,
        detail=(
            "session {0} was stopped under supervision because its rotation category "
            "could not be established; this controller's own handle for pid {1} was "
            "proven to be the process the binding names, process group {2} is gone "
            "({3} shutdown, exit code {4}), the binding is {5} and no longer occupies "
            "an agent slot, and nothing was launched or bound in its place.".format(
                stopped.session_id, stopped.pid, stopped.pgid,
                "acknowledged" if stopped.graceful else "escalated",
                stopped.exit_code, stopped.binding.state,
            )
        ),
        ambiguity=ambiguity,
        human_action=(
            "No process action is required: process group {0} is proven gone and the "
            "agent slot session {1} held is released. The ambiguity was not resolved "
            "by stopping it ({2}). If this session had in fact reached the rotation "
            "threshold, D9's safe handoff was never proven for it, so a human should "
            "check rail {3} for a current published handoff before treating its work "
            "as carried forward, and should treat a recurrence as a fault in "
            "observation rather than as routine.".format(
                stopped.pgid, stopped.session_id, ambiguity, record.rail,
            )
        ),
        stopped=stopped,
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
# Fourth -- and this is where checkpoints 59 to 61 stopped short -- neither of
# those facts orders the publication against the work. Both are taken from outside
# a provider turn, and a turn is opaque: a handoff published midway through one,
# with further work after it, presents this controller with exactly the
# observations a handoff published at the end of one does. Checkpoint 61 tried to
# recover the ordering from repository state, by having the publishing act record
# the product head it was written against and comparing that with where the
# workspace now stands. For work that commits, that does distinguish the two
# orderings. For work that does not, it distinguishes nothing at all: on a
# reviewer, orchestrator or evidence-only rail the recorded state and the observed
# state are the same commit name from the session's first instant to its last, and
# a constant cannot order anything. D10 puts orchestrators in scope for the same
# invariant, so that was not a residual sliver but a whole role.
#
# The distinguishing fact was never going to be an observation. It is an act. The
# controller does not watch for a publication and try to date it; it *performs* the
# publication, from the terminal result of an invocation that has already ended,
# and credits only what it published itself. The turn is over before the credited
# bytes become durable, so nothing the agent did -- committing or not, producing
# evidence or not, publishing something of its own mid-turn or not -- can follow
# them inside that invocation. That is structural in the strongest available sense:
# it is not a rule the agent is asked to obey, and it holds identically for a rail
# that never touches the product repository.
#
# The permission this rests on is narrow and worth restating where it is spent: the
# terminal result of a completed invocation may be transient input to deterministic
# durable finalization. One string, from the one message that ends a turn,
# published verbatim without being interpreted, retained nowhere. It is not
# transcript replay, not provider-message history, not a tool-use audit, not a
# command log, not a timestamp, not a sequence number, and not a second durable
# representation of the handoff. The published handoff is still the only canonical
# artifact, and a replacement still reads only that.
#
# What this deliberately does not do is treat every act after publication as work.
# The boundary is still the product repository, because that is what a replacement
# resumes from and what D9 names first. Allocating a receipt in the coordination
# repository -- which the supported executor path performs *after* publishing --
# moves no product state and invalidates nothing when it succeeds. When it fails,
# it is not the repository comparison that catches it: the finalization required
# that bookkeeping, the bookkeeping failed, and so nothing was credited at all.


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

    # 5. And that publication is the current one -- which now means: this controller
    #    published it itself, from the terminal result of a completed invocation,
    #    after that invocation's provider turn had ended, and no further invocation
    #    has begun since.
    #
    #    This is the check checkpoints 59-61 were reaching for and could not hold.
    #    Their currency facts were all *observations*: a publication seen to appear
    #    between two reads, and later the product state it was written against. Both
    #    are taken from outside a provider turn, and the checkpoint-61 review proved
    #    what that costs -- for any rail whose remaining work produces evidence
    #    without a product commit (every read-only reviewer and orchestrator rail
    #    included, which D10 places in scope), the discriminator is constant from
    #    first instant to last, and a constant orders nothing.
    #
    #    The fact that does order it is not an observation at all. It is the
    #    controller's own act: `finalize_terminal_handoff` publishes the handoff
    #    *after* `sender` has returned, so the agent's turn is provably over before
    #    the credited bytes become durable. No product work, no evidence work, no
    #    coordination act and no publication of the agent's own can follow it inside
    #    that invocation, whether or not anything was committed. Nothing about the
    #    repository has to move for this to hold, which is exactly the class that was
    #    unreachable before.
    boundary = registry.work_boundary(record.session_id)
    finalization = registry.terminal_finalization(record.session_id)
    if handoff.publication is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the observation of {0} does not name which publication is there, so "
            "whether it is the one describing session {1}'s current work cannot be "
            "established.".format(handoff.location, record.session_id),
        )
    if finalization is None:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "a handoff is published at {0}, but this controller never finalized one "
            "from the terminal result of any of session {1}'s {2} invocations, so "
            "it cannot say the published handoff followed that session's last "
            "act.".format(handoff.location, record.session_id, boundary),
        )
    if finalization.publication != handoff.publication:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} is {1}, but the publication this "
            "controller finalized for session {2} is {3}; the one on offer is not "
            "the one whose currency was proven.".format(
                handoff.location, handoff.publication, record.session_id,
                finalization.publication,
            ),
        )
    if finalization.work_boundary != boundary:
        return projected(
            ROTATION_NOT_READY,
            REASON_HANDOFF_NOT_CURRENT,
            "the handoff published at {0} was finalized at work boundary {1} and "
            "session {2} is at work boundary {3}; {4} further invocation(s) have "
            "begun since, so it does not carry the outcome, evidence, unresolved "
            "work and next action a replacement would resume from.".format(
                handoff.location, finalization.work_boundary, record.session_id,
                boundary, boundary - finalization.work_boundary,
            ),
        )

    # 6. And the product repository still stands where the publication said it did.
    #
    #    Checkpoint 61 introduced this as *the* ordering proof. It is no longer that,
    #    and saying so plainly is the honest thing to do: check 5 now proves the
    #    ordering structurally, for every rail rather than only for rails that
    #    commit. What this check still does is real but smaller -- it refuses a
    #    boundary where the product repository moved after the finalization, in the
    #    gap between the turn ending and this projection being asked. Nothing inside
    #    the invocation can produce that any more, but a human, another session, or
    #    a controller-side act outside any invocation can, and a replacement handed
    #    a handoff written against a state the workspace has since left would be
    #    resuming from a description of somewhere else.
    #
    #    It is kept for that residual guard, and for nothing else. It is retained
    #    rather than deleted because it costs one comparison and closes a real gap;
    #    it is demoted rather than relied upon because the review proved it cannot
    #    order work that lands no commit.
    #
    #    It remains deliberately the *product* repository and not "anything that
    #    ran". The supported path allocates a receipt after publishing, in the
    #    coordination repository; that changes no product state, alters nothing a
    #    replacement resumes, and must not invalidate a handoff that is otherwise
    #    current. When the receipt or the push *fails*, the finalization that
    #    required it was never credited in the first place, so the failure is caught
    #    at check 5 rather than here.
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


# ---------------------------------------------------------------------------
# Old-context retirement -- D9's "only after that safe handoff", and nothing past it
# ---------------------------------------------------------------------------
#
# D9 permits exactly one destructive act, and only in one place: "Only after that
# safe handoff may the manager terminate the old context and launch a fresh agent
# against durable state." This is the first half of that sentence and deliberately
# not the second. It retires; it does not replace. There is no launcher, no
# runtime request, no starter and no binding reservation anywhere in this section,
# and that absence is the mechanism rather than a convention: a caller cannot ask
# this to launch a replacement, because there is nothing here to ask.
#
# Three things make it the smallest seam that is still safe.
#
# First, it takes *observations*, never a verdict. There is no `readiness`
# parameter, so a caller physically cannot hand it a rotation-ready answer computed
# a moment ago and have that authorize a termination. It projects readiness itself,
# from the caller's fresh rail, handoff and worktree reads plus the registry's own
# current facts, immediately before it acts. Readiness was built as a fresh
# mechanical projection with nothing stored precisely so it could be re-asked at
# the instant it matters, and this is that instant. If anything moved since the
# caller last looked -- a further invocation began, the workspace moved, the
# finalization no longer stands -- the projection says so now and retirement
# refuses. A stale ready is not merely rejected here; it is unrepresentable.
#
# Second, ownership is proven separately, before readiness is even asked.
# Readiness deliberately says nothing about process liveness, which is exactly why
# it cannot be allowed to authorize a kill on its own: a session can be perfectly
# ready by every durable fact while the process this controller believes it holds
# is already gone, or is no longer the process the binding names. So the ownership
# proof is `require_owned`, unweakened and unbypassed -- the same proof `stop_session`
# demands -- and a session that fails it is not retired at all. It is routed to
# `recover_session`, which describes the disconnection for a human and performs
# nothing. Retiring on the durable record alone would be the one claim this system
# must never make: terminalizing a binding for a process nobody could prove
# anything about.
#
# Ownership is asked first for that reason. A restarted controller holds no handle
# and therefore no context observation either, so a readiness-first order would
# answer such a session with "not marked for rotation" -- true, and a description of
# the wrong problem. The session is disconnected, that is a human decision, and
# saying so is the useful refusal. Readiness is then asked last, so the gap between
# the projection and the destructive act is as small as this composition can make
# it.
#
# Third, termination is proven rather than asserted. `stop_session` already refuses
# to terminalize a binding until the shutdown reports the process group gone *and*
# an independent probe agrees, and it removes the registry entry -- ownership, work
# boundary, terminal finalization and the context-lifecycle observation together --
# only after that proof. Nothing is added to that here. What retirement adds is the
# order in which it is allowed to happen at all.
#
# `graceful` on the outcome is a reported fact about how the worker went, not a
# precondition: it says the worker acknowledged the shutdown command before its
# group was proven gone. D9's "graceful rotation" is about rotating at a safe
# boundary with durable state left behind -- which is what readiness proves -- and
# not about which signal ended the process. A stop that had to escalate is still a
# retirement, and the outcome says plainly that it escalated.
#
# Failure is fail-closed in the only direction that matters: never half-retired.
# Every refusal here happens before anything is stopped, and the one failure that
# can happen after the stop is attempted -- a shutdown that cannot be proven --
# already leaves `stop_session`'s binding nonterminal and the registry entry
# intact, so the session survives owned, bound and continuable. A session that
# could not be retired is emphatically not a session that may be replaced: this
# raises, terminalizes nothing, and still launches nothing.

RETIREMENT_RETIRED = "old-context-retired"
RETIREMENT_REFUSED = "retirement-refused"
RETIREMENT_STATES = (RETIREMENT_RETIRED, RETIREMENT_REFUSED)


@dataclass(frozen=True)
class ContextRetirement:
    """What one retirement attempt did, and the exact facts it did it on.

    `readiness` is the projection this attempt took itself, not one it was given.
    It is absent only on the disconnected route, where ownership failed before
    readiness was asked -- which is the honest record of that path: no readiness
    value existed, so none could have authorized anything.

    `stopped` is present only when a process group was proven gone, and `recovery`
    only when the session was handed to a human instead. Nothing carries a
    replacement, a reservation, or a launch, because retirement performs none.
    """

    session_id: str
    rail: str
    state: str
    reason: str
    detail: str
    readiness: Optional[RotationReadiness] = None
    stopped: Optional[StopOutcome] = None
    recovery: Optional[RecoveryReport] = None

    @property
    def retired(self) -> bool:
        return self.state == RETIREMENT_RETIRED


def retire_old_context(
    store: BindingStore,
    registry: SessionRegistry,
    rail: Optional[RailFacts],
    record: Optional[BindingRecord],
    *,
    handoff: Optional[RotationHandoffFacts],
    worktree: Optional[WorktreeFacts],
    now: str,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
) -> ContextRetirement:
    """Terminate the exact owned context of a session that is rotation-ready *now*.

    Refuses -- touching nothing -- when the session is not ready at this instant,
    and routes a session it cannot prove it owns to `recover_session` rather than
    retiring it. Launches nothing and binds nothing on any path, including success.

    Raises `LifecycleError` on a contradiction between the durable records, and on
    a shutdown that cannot be proven; in both cases the session is left exactly as
    it was.
    """
    if record is not None and record.is_terminal:
        raise LifecycleError(
            REASON_BINDING_TERMINAL,
            "session {0} is {1}; there is no live context left to retire.".format(
                record.session_id, record.state
            ),
        )

    # One liveness instant for the pre-flight, because proving ownership and then
    # describing the disconnection are two consumers of the same question and must
    # not answer it twice. Deliberately *not* reused across the stop below: the
    # post-shutdown proof has to be a new observation, and a memoized one would
    # report the liveness this read began with.
    preflight = single_liveness_snapshot(alive)
    if record is not None:
        try:
            require_owned(registry, record, alive=preflight)
        except LifecycleError as exc:
            if exc.reason in (REASON_HANDLE_MISSING, REASON_HANDLE_MISMATCH):
                report = recover_session(record, registry, now=now, alive=preflight)
                return ContextRetirement(
                    session_id=record.session_id,
                    rail=record.rail,
                    state=RETIREMENT_REFUSED,
                    reason=report.reason,
                    detail=report.human_decision,
                    recovery=report,
                )
            raise

    readiness = evaluate_rotation_readiness(
        rail, record, registry, handoff=handoff, worktree=worktree
    )
    if not readiness.ready:
        return ContextRetirement(
            session_id=readiness.session_id,
            rail=readiness.rail,
            state=RETIREMENT_REFUSED,
            reason=readiness.reason,
            detail=readiness.detail,
            readiness=readiness,
        )

    # The one place an authorization is minted, and it is minted only here, only
    # after every refusal above declined to fire, and only from the verdict this
    # call projected itself a moment ago.
    stopped = stop_session(
        store,
        registry,
        record,
        stop=stop,
        alive=alive,
        _retirement=_RetirementAuthorization(
            session_id=record.session_id, readiness=readiness
        ),
    )
    return ContextRetirement(
        session_id=stopped.session_id,
        rail=readiness.rail,
        state=RETIREMENT_RETIRED,
        reason=REASON_OLD_CONTEXT_RETIRED,
        detail=(
            "session {0} was rotation-ready at the instant of retirement on handoff "
            "publication {1}, and this controller's own handle for pid {2} was proven "
            "to be the process the binding names; process group {3} is gone "
            "({4} shutdown, exit code {5}), the binding is {6}, and nothing was "
            "launched or bound in its place.".format(
                stopped.session_id,
                readiness.handoff.handoff_publication if readiness.handoff else "",
                stopped.pid,
                stopped.pgid,
                "acknowledged" if stopped.graceful else "escalated",
                stopped.exit_code,
                stopped.binding.state,
            )
        ),
        readiness=readiness,
        stopped=stopped,
    )


# ---------------------------------------------------------------------------
# Replacement: retired old context -> a successor launched and bound
# ---------------------------------------------------------------------------
#
# D9's sentence has two halves. Checkpoint 63 implemented the first -- "only after
# that safe handoff may the manager terminate the old context" -- and deliberately
# stopped there, launching nothing. This is the second half, "and launch a fresh
# agent against durable state", and it too stops deliberately short: it launches
# and binds a successor, and it does not continue anything through it. The third
# half of the rotation, resuming work from the durable handoff alone, is a
# separate act that this route has no way to perform: it takes no prompt and no
# sender, so there is nothing here to invoke work with.
#
# The ordering is the whole substance. A replacement bound beside a live
# predecessor is two agents claiming one rail's work, both able to move the same
# worktree, and it is exactly what the ceiling and the binding store exist to make
# impossible. So retirement is not merely requested here and its answer is not
# merely read: this route re-proves, for itself and at the instant it is about to
# reserve, that the predecessor's process group is gone, its binding is terminal in
# the store, and this controller no longer holds a handle for it. Only then does it
# ask whether a launch is authorized at all.
#
# That ordering also settles D6 without arithmetic. The predecessor's slot is
# released by the terminalization the retirement performed, before the successor's
# reservation is written, so the swap passes through N-1 rather than N+1 -- and
# never leaves N+1 behind, because a rotation that stopped here would leave the
# rail with one fewer agent, not two.

REPLACEMENT_LAUNCHED = "replacement-launched"
REPLACEMENT_REFUSED = "replacement-refused"
REPLACEMENT_STATES = (REPLACEMENT_LAUNCHED, REPLACEMENT_REFUSED)

REASON_REPLACEMENT_LAUNCHED = "replacement-launched"
REASON_PREDECESSOR_MISSING = "predecessor-binding-missing"
REASON_PREDECESSOR_NOT_RETIRED = "predecessor-not-retired"
REASON_RETIREMENT_UNPROVEN = "retirement-not-proven-at-launch"
REASON_SUCCESSOR_IDENTITY_REUSED = "successor-identity-reused"
REASON_REPLACEMENT_NOT_AUTHORIZED = "replacement-not-authorized"


@dataclass(frozen=True)
class BoundReplacement:
    """The successor: a distinct session, reserved before it existed, now bound.

    It carries no result and no prompt, because nothing was sent to it. What it
    proves is identity and state -- that a new session id was minted, that its
    binding passed through `reserved` before any process existed, and that it is
    `bound` now -- and nothing about work, because none was done.
    """

    session_id: str
    binding: BindingRecord
    owned: OwnedSession
    request: RuntimeRequest


@dataclass(frozen=True)
class ContextReplacement:
    """One rotation swap: what was retired, and what -- if anything -- replaced it.

    `retirement` is the retirement this call performed itself, never one it was
    handed. `replacement` is present only when a successor was actually reserved
    and bound, and is absent on every refusal, so a caller cannot read a launch out
    of a run that did not perform one.
    """

    predecessor_session_id: str
    rail: str
    state: str
    reason: str
    detail: str
    retirement: Optional[ContextRetirement] = None
    replacement: Optional[BoundReplacement] = None

    @property
    def launched(self) -> bool:
        return self.state == REPLACEMENT_LAUNCHED


def replace_old_context(
    store: BindingStore,
    registry: SessionRegistry,
    *,
    session_id: str,
    assignment: Assignment,
    read_rail: Callable,
    read_handoff: Callable,
    read_worktree: Callable,
    read_slots: Callable,
    read_observation: Callable,
    reference: Any,
    request_kwargs: Mapping,
    package_root: Any,
    now: str,
    clock: Optional[Callable] = None,
    new_session_id: Optional[Callable] = None,
    start: Optional[Callable] = None,
    stop: Optional[Callable] = None,
    alive: Optional[Callable] = None,
    environment_source: Optional[Mapping] = None,
    ready_timeout: Optional[float] = None,
) -> ContextReplacement:
    """Retire one rotation-ready context, prove it gone, and bind a successor to it.

    This is the first production call site of the retirement gate, and it owns its
    own facts. Every input the gate decides on -- the durable binding, the rail, the
    published handoff, the worktree, manager-wide occupancy, and the control-plane
    observation a launch is authorized against -- is obtained *here*, inside this
    call,
    immediately before the decision that consumes it. That is why the readers are
    parameters and the facts are not: there is no `rail=`, `handoff=`,
    `worktree=`, `slots=` or `decision=` argument to hand a value that was true
    earlier in some flow, so a stale input is not something a caller has to
    remember not to pass -- it is something this signature cannot accept.

    Nothing is launched unless the predecessor is *retired*: not projected ready,
    not asked to stop, but stopped, with its process group proven gone by a probe
    this function takes for itself after the retirement returned, its binding
    terminal in a re-read of the store, and its handle no longer held. A retirement
    that refuses, or that fails closed, launches nothing and leaves the old session
    exactly as it was.

    Nothing is continued through the successor either. There is no `send`, no
    `prompt` and no `markers` parameter, so no work invocation is reachable from
    here; resuming from the durable handoff is a separate act by a separate caller.
    """
    prober = alive if alive is not None else process_group_alive

    # Fact one, read here: which durable binding this session actually has, now.
    record = store.read(session_id)
    if record is None:
        return ContextReplacement(
            predecessor_session_id=session_id,
            rail=assignment.rail,
            state=REPLACEMENT_REFUSED,
            reason=REASON_PREDECESSOR_MISSING,
            detail=(
                "no binding for session {0}, so there is no old context to retire and "
                "nothing whose work a replacement could claim to carry forward. "
                "Nothing was launched.".format(session_id)
            ),
        )

    # Facts two, three and four, read here, in the order the gate consumes them and
    # at the instant it consumes them. A caller that read these a step earlier and
    # passed the values would be handing the gate a description of a moment that has
    # already gone; this call takes them itself so it cannot be handed one.
    rail = read_rail()
    handoff = read_handoff()
    worktree = read_worktree()

    # The retirement this call performs, and the only one it will act on. A
    # `LifecycleError` from here -- a contradiction in the records, or a shutdown
    # that could not be proven -- propagates untouched: it is a retirement that
    # failed, so nothing is launched and the old session survives exactly as the
    # accepted gate left it.
    retirement = retire_old_context(
        store,
        registry,
        rail,
        record,
        handoff=handoff,
        worktree=worktree,
        now=now,
        stop=stop,
        alive=alive,
    )
    if not retirement.retired:
        return ContextReplacement(
            predecessor_session_id=session_id,
            rail=retirement.rail,
            state=REPLACEMENT_REFUSED,
            reason=REASON_PREDECESSOR_NOT_RETIRED,
            detail=(
                "the old context was not retired ({0}: {1}), so nothing was launched "
                "and session {2} survives exactly as it was. A replacement is bound "
                "only beside a predecessor that is gone.".format(
                    retirement.reason, retirement.detail, session_id
                )
            ),
            retirement=retirement,
        )

    # "Retired" is a claim, and this is where it stops being one. The retirement's
    # own report is checked against the world it describes, immediately before the
    # reservation that would make a successor real: the group is probed again, by
    # this function, after the stop; the binding is re-read from the store rather
    # than taken from the outcome object; and the registry is asked whether this
    # controller still holds a handle. Each of these is the retirement's claim
    # restated as an independent observation, and any one of them failing means the
    # predecessor may still be alive -- which is the one state a launch must never
    # be reached from.
    stopped = retirement.stopped
    unproven = None
    if stopped is None or not stopped.process_group_gone:
        unproven = "the retirement reported no proven process-group shutdown"
    elif not stopped.binding.is_terminal:
        unproven = "the retirement left binding {0} in state {1}".format(
            session_id, stopped.binding.state
        )
    else:
        after = store.read(session_id)
        if after is None or not after.is_terminal:
            unproven = (
                "a fresh read of the store does not show binding {0} terminal".format(
                    session_id
                )
            )
        elif registry.get(session_id) is not None:
            unproven = (
                "this controller still holds a worker handle for session {0}".format(
                    session_id
                )
            )
        elif prober(stopped.pgid):
            unproven = "process group {0} answers a fresh liveness probe".format(
                stopped.pgid
            )
    if unproven is not None:
        raise LifecycleError(
            REASON_RETIREMENT_UNPROVEN,
            "session {0} was reported retired but {1}; a replacement is refused "
            "rather than bound beside a predecessor that may still be running. "
            "Nothing was launched or reserved.".format(session_id, unproven),
        )

    # Fact five, read here and not before: manager-wide occupancy, over the store
    # as it stands *after* the terminalization. The reduction itself is not
    # performed here -- `reconcile_agent_slots` keeps exactly one production home,
    # in the controller that draws the figure -- so this asks that home for it, at
    # the instant it needs the answer, over records it read itself a line ago. That
    # instant is what makes the ceiling hold across a swap at the limit: the slot
    # the predecessor held was released a moment ago, so a rotation at six
    # occupants authorizes against five rather than refusing itself, and it can
    # never authorize against seven.
    records = store.records()
    slots = read_slots(records)
    # Fact six: the control-plane observation, read now, and turned into a decision
    # here rather than accepted as one. A caller cannot hand this route an
    # authorization at all, which is what keeps a decision taken before the
    # retirement -- when the rail still had a live binding -- from reaching the
    # launch. The accepted authorizer is also the second guard on requirement A: a
    # rail holding a live binding yields a continuation decision or a refusal, never
    # a launch, so `_require_decision` below could not pass one.
    decision = authorize(
        read_observation(),
        project=assignment.project,
        ticket=assignment.ticket,
        rail=assignment.rail,
        role=assignment.role,
        expected_head=assignment.head,
        rail_blob=assignment.iteration.blob,
        slots=slots,
        bindings=records,
        in_flight_session_ids=registry.in_flight(),
    )
    if not decision.authorized or decision.action != ACTION_LAUNCH:
        return ContextReplacement(
            predecessor_session_id=session_id,
            rail=assignment.rail,
            state=REPLACEMENT_REFUSED,
            reason=REASON_REPLACEMENT_NOT_AUTHORIZED,
            detail=(
                "the old context was retired and its slot released, but no "
                "replacement is authorized ({0}: {1}). Nothing was launched or "
                "reserved; the rail holds one fewer agent than before and its "
                "durable handoff is what a later launch resumes from.".format(
                    decision.reason, decision.detail
                )
            ),
            retirement=retirement,
        )
    _require_decision(decision, assignment, action=ACTION_LAUNCH)

    minter = new_session_id if new_session_id is not None else (lambda: str(uuid.uuid4()))

    def successor_id() -> str:
        candidate = minter()
        if candidate == session_id:
            raise LifecycleError(
                REASON_SUCCESSOR_IDENTITY_REUSED,
                "a replacement may not be minted with its predecessor's session id "
                "{0}: the successor is a different agent that never held the "
                "predecessor's context, and reusing the identity would make its "
                "record read as a continuation of work it did not do. Nothing was "
                "reserved.".format(session_id),
            )
        return candidate

    bound = _reserve_and_bind(
        assignment,
        store=store,
        registry=registry,
        reference=reference,
        request_kwargs=request_kwargs,
        package_root=package_root,
        ceiling=decision.ceiling,
        clock=clock if clock is not None else _utc_now,
        mint=successor_id,
        starter=start if start is not None else start_worker,
        environment_source=environment_source,
        ready_timeout=ready_timeout,
    )
    return ContextReplacement(
        predecessor_session_id=session_id,
        rail=assignment.rail,
        state=REPLACEMENT_LAUNCHED,
        reason=REASON_REPLACEMENT_LAUNCHED,
        detail=(
            "session {0} was retired -- process group {1} proven gone and its binding "
            "{2} -- and only then was replacement session {3} reserved and bound to "
            "pid {4} on rail {5}. The successor is a distinct session that holds none "
            "of the predecessor's context, one slot was released before one was "
            "occupied, and nothing has been sent to it: continuing from the durable "
            "handoff is a separate act.".format(
                session_id,
                stopped.pgid,
                stopped.binding.state,
                bound.binding.session_id,
                bound.binding.pid,
                assignment.rail,
            )
        ),
        retirement=retirement,
        replacement=BoundReplacement(
            session_id=bound.binding.session_id,
            binding=bound.binding,
            owned=bound.owned,
            request=bound.request,
        ),
    )
