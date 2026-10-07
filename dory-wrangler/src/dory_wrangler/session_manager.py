"""Chat turns, agent sessions, and bindings: contract sections 4, 5 and 6.

This module drives the chat loop. It holds a `LaunchBoundary` and never learns
anything else about it: it does not import a launcher, does not read a
`launcher_id` to decide anything, and contains no host, transport, or bridge
mechanics. Swapping launchers is a change to the object handed to the
constructor and to nothing else.

**It writes nothing itself.** Every durable record goes through `ChatStore`,
which is the only writer of every record type (decision 0002). This module
decides *what* happens to a chat -- which operation the launcher is asked for,
which transition the lifecycle takes, when a turn is refused -- and the store
decides whether the record of it may exist, fails closed if not, and publishes
it atomically. A lifecycle rule this module relies on and the store also
enforces is therefore stated once, in the store, over the contract's own
tables; it is not restated here.

What is deliberately **not** here, per contract 1 and 5.3: no timer, no
inactivity inference, no liveness interrogation, no stall detection, no retry,
and no automatic recovery. Nothing in this file computes an elapsed duration or
compares one against a threshold. Every state change is caused by a user action,
a harness action, or an explicit launcher observation.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import threading

from .contract import terminal_states
from .notices import NO_SHOWABLE_REPLY
from .errors import (
    ConcurrentLaunchRefused,
    InstructionTooLarge,
    NotPermitted,
    StoreError,
    TurnInFlightRefused,
    ValidationRefused,
)
from .launch_boundary import (
    DeliveryAck,
    DeliveryInstruction,
    EventsPage,
    LaunchBoundaryError,
    LaunchInstruction,
    LaunchResult,
    LauncherError,
    PAYLOAD_SESSION_COMPLETED,
    PAYLOAD_SESSION_FAILED,
    PAYLOAD_STREAM_END,
    PAYLOAD_TURN_COMPLETE,
    FAILURE_INTERNAL_ERROR,
    INTERPRETATION_RECOGNIZED,
    INTERPRETATION_UNRECOGNIZED,
    OUTCOME_ACCEPTED,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    RESPONSE_SHAPE_STREAM,
    SOURCE_LAUNCHER,
    StopAck,
)

# The file, inside a chat's own directory, whose lock is held for the duration
# of one user action on that chat. Not the store's `.lock`: the store takes that
# one for each write, and a second descriptor on the same file in one process
# would wait on itself.
TURN_LOCK_NAME = ".turn-lock"

# The intake checkpoint's one open question, **answered by the human on
# 2026-09-15: preserve the bytes.** The answer stays parked here because both
# behaviours remain pinned by tests, so the record of what was decided and what
# it cost is one line and one comment rather than a reconstruction.
#
# **The tension it resolves.** Contract 7 P1: "Every payload the integration
# produces becomes a `diagnostic_event` whose `raw.body` holds it exactly as
# received." Contract 6.1: "A `diagnostic_event` of `interpreted_type:
# 'stream_end'` on such a session is rejected outright
# (`STREAM_END_UNSUPPORTED`)", where "such a session" is one whose launcher
# declares `response_shape: one_shot`. A one-shot launcher that types a payload
# `stream_end` therefore produces bytes P1 requires preserved and a reading 6.1
# forbids recording. #86 and #87 both resolved it by refusing before preserving,
# so the bytes survived nowhere, and both were accepted that way.
#
# **What each value does.** `True`, the shipped value, preserves the bytes and
# drops only the reading -- the record is written `unrecognized` with
# `interpreted_type: null` (`_preserve(..., attribute=False)`), so P1 is
# satisfied and `STREAM_END_UNSUPPORTED` is never reachable, because both the
# store (`store.py`) and the validator test `interpreted_type` alone and neither
# examines `raw.body`. `False` is the older accepted behaviour: the payload is
# refused before it is written and its bytes are lost. Under either value the
# *claim* is still refused exactly as 6.1 states it: the page raises, the turn
# fails, and no session ever concludes `unknown` from an end of stream a
# one-shot launcher could not have seen. Preserving the bytes is not a step
# towards accepting the assertion and must never become one.
#
# **Why it is `True`.** Under `False` the loss was never one payload. Refusing
# before preserving leaves that `sequence` unwritten, so the next payload is a
# gap the store refuses, and so is every payload after it for the session's
# life: measured on the probe page `[text@1, stream_end@2, text@3]`, `False`
# preserves `[1]` and then refuses a later `[text@4]` on the same session, while
# `True` preserves `[1, 2, 3]` and then `4`. The one thing that stood in the way
# was prose, not a rule: contract 7 P2's `unrecognized` bullet read "well-formed
# but of a type this build does not know", and this build knows `stream_end` --
# what it cannot do is *attribute* it on this session. The human widened that
# bullet on 2026-09-15 to cover "or knows but cannot attribute on this session"
# (#85, carried here by the contract pick-up merge), which is why this is now
# `True`. No rule, error code, fixture or validator check changed with it.
#
# Residual, stated rather than implied: a reader of the store cannot tell a
# genuinely unknown type from a declined one without reading `raw.body`.
PRESERVE_UNATTRIBUTABLE_STREAM_END = True


class TurnOutcome(object):
    """What one user turn produced. Evidence, not narration."""

    __slots__ = ("chat_id", "session_id", "user_message_id", "agent_message_ids",
                 "session_state", "launch_outcome", "failure_category", "delivered")

    def __init__(self, chat_id, session_id, user_message_id, agent_message_ids,
                 session_state, launch_outcome=None, failure_category=None,
                 delivered=False):
        self.chat_id = chat_id
        self.session_id = session_id
        self.user_message_id = user_message_id
        self.agent_message_ids = list(agent_message_ids)
        self.session_state = session_state
        self.launch_outcome = launch_outcome
        self.failure_category = failure_category
        self.delivered = delivered

    def __repr__(self):
        return "TurnOutcome(session=%s, state=%s, outcome=%r, delivered=%s)" % (
            self.session_id, self.session_state, self.launch_outcome, self.delivered
        )


def compose_launch_instruction(chat_id, user_text, store):
    """What a launch packet contains under `fresh_binding`.

    **Deliberately just this turn's text.** Contract 6.1 and
    facts-and-assumptions U1 leave packet composition open for early internal
    dogfood and say explicitly that it is *not* "the whole prior chat by
    default", because carrying prior history would make an unmeasured payload
    bound (U2) part of the architecture. A future composer is a change to this
    one function.

    `store` is accepted, unused, and named so that the decision is visible: the
    prior chat is available here and is deliberately not read.
    """
    return user_text


class _PageTaken(object):
    """What taking one page of payloads produced.

    `turn_end_reported` is whether the launcher reported, on this page, that the
    turn ended: a recognized `turn_complete`, a launcher's terminal lifecycle
    event, or a launcher's end of stream on a session that has one -- read from
    the preserved payload, whether or not a transition it asks for was then
    recorded. `lost` is whether any payload on the page is not held by the store
    exactly as it arrived; a refusal that came after the payload was preserved
    (a claim the harness declines) loses nothing.
    """

    __slots__ = ("turn_complete", "reached_terminal", "stream_end_event_id", "stored_any",
                 "refusal", "turn_end_reported", "lost")

    def __init__(self):
        self.turn_complete = False
        self.reached_terminal = False
        self.stream_end_event_id = None
        self.stored_any = False
        self.refusal = None
        self.turn_end_reported = False
        self.lost = False


class _TurnSeen(object):
    """What one read of a turn observed: whether the turn was seen to end, and
    whether everything read was preserved exactly (decision 0006)."""

    __slots__ = ("ended", "lost")

    def __init__(self):
        self.ended = False
        self.lost = False

    def took(self, taken):
        self.ended = self.ended or taken.turn_end_reported
        self.lost = self.lost or taken.lost


class _ChatTurn(object):
    __slots__ = ("rlock", "depth", "fd")

    def __init__(self):
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd = None


class _Held(object):
    """One hold of a chat's turn lock. `acquired` says whether it was obtained."""

    def __init__(self, turns, chat_id, blocking):
        self._turns = turns
        self._chat_id = chat_id
        self._blocking = blocking
        self._turn = None
        self.acquired = False
        # True when this hold is inside another hold of the same chat on the
        # same thread: a launcher that called back into the loop while a user
        # action on this chat is in flight.
        self.nested = False

    def __enter__(self):
        path = os.path.join(self._turns.store._require_chat_dir(self._chat_id),
                            TURN_LOCK_NAME)
        # The turn-lock file is created in the store, so it is a write, and no
        # write happens without the store-level lock (decision 0002, D1).
        self._turns.store.acquire()
        with self._turns.guard:
            turn = self._turns.table.setdefault(self._chat_id, _ChatTurn())
        if not turn.rlock.acquire(self._blocking):
            return self
        # Review finding R6: everything between taking the in-process lock and
        # holding the file lock can fail -- the open (a permission, a missing
        # directory), the `flock` (ENOLCK on a filesystem that has none) -- and a
        # failure that left the in-process lock held would hang every later
        # request on this chat with no timeout, by design. So any failure gives
        # the in-process lock back, and closes the descriptor if it was opened.
        try:
            if turn.depth == 0:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if self._blocking else fcntl.LOCK_NB))
                except BaseException:
                    os.close(fd)
                    raise
                turn.fd = fd
        except BlockingIOError:
            # Held by another descriptor: not acquired, and nothing is held.
            turn.rlock.release()
            return self
        except BaseException:
            turn.rlock.release()
            raise
        turn.depth += 1
        self._turn = turn
        self.acquired = True
        self.nested = turn.depth > 1
        return self

    def __exit__(self, exc_type, exc, tb):
        turn = self._turn
        if turn is None:
            return False
        turn.depth -= 1
        if turn.depth == 0:
            try:
                fcntl.flock(turn.fd, fcntl.LOCK_UN)
            finally:
                os.close(turn.fd)
                turn.fd = None
        turn.rlock.release()
        return False


class ChatTurnLocks(object):
    """One user action at a time per chat, across threads *and* processes.

    #87's harness held a per-chat in-process lock for a whole turn; #86's store
    is written to be shared by more than one process. The refuse-before-recording
    rule for concurrent turns (decision 0003) is only true if "is this chat
    already served?" and "record this turn and open its session" cannot be
    interleaved by another writer, so the lock is an `flock` on a file in the
    chat's own directory, held for the action, and re-entrant within one thread
    so a launcher that calls back into the chat loop during `launch` is refused
    by the rule rather than deadlocked by the lock.

    A process that dies holding it releases it with its descriptor, so there is
    no stale lock to time out and nothing here measures time.

    **No user action waits for it** (decision 0003; re-review N1, N2). Every user
    action takes it without blocking and is refused, having written nothing, if
    another thread or process holds it: a turn sent during a turn in flight is
    refused rather than queued, and a Stop or Abandon is refused rather than
    deferred. Only the same thread re-entering its own hold -- a launcher calling
    back into the loop -- gets it, and meets the loop's own rules there.
    """

    def __init__(self, store):
        self.store = store
        self.guard = threading.Lock()
        self.table = {}

    def hold(self, chat_id, blocking=True):
        return _Held(self, chat_id, blocking)


class SessionManager(object):
    """The chat loop, the lifecycle, and the one-agent-per-chat rule."""

    def __init__(self, store, boundary, compose=None):
        self._store = store
        self._boundary = boundary
        self._compose = compose or compose_launch_instruction
        self._turns = ChatTurnLocks(store)
        self._terminal = terminal_states()

    # -- properties --------------------------------------------------------

    @property
    def store(self):
        return self._store

    @property
    def capabilities(self):
        return self._boundary.capabilities

    # -- chats -------------------------------------------------------------

    def create_chat(self, title):
        return self._store.create_chat(title)["chat_id"]

    def transcript(self, chat_id):
        """The user-visible history, from durable records alone (contract D1, P4).

        Messages only. `diagnostic_event` records are never rendered here; they
        are reachable through `ChatStore.read_diagnostic_events`, which is a
        bounded out-of-band retrieval that derives nothing.
        """
        return [
            (m["sequence"], m["author"], m["content"]["text"])
            for m in self._store.read_messages(chat_id)
        ]

    # -- the chat loop -----------------------------------------------------

    def send_turn(self, chat_id, text):
        """The user sends a turn. This is the action that opens the next binding.

        Which of the two continuation modes applies is read from the launcher's
        *declared* capability and from nothing else. No harness autonomy, no
        timer, and no inactivity inference is involved in either mode.
        """
        # An empty turn is not a turn. Refused before anything durable is
        # written, because contract 4.2 gives a message no way to say "the user
        # sent nothing" -- recording one would put a record in the store that
        # cannot be read back.
        if not isinstance(text, str) or text == "":
            raise NotPermitted(
                "a user turn must carry text; an empty turn is refused rather than "
                "recorded as a turn that happened")

        with self._user_action(chat_id):
            active = self._active_session(chat_id)
            if active is None:
                return self._launch_turn(chat_id, text)
            return self._continue_turn(chat_id, active, text)

    @contextlib.contextmanager
    def _user_action(self, chat_id):
        """Hold the chat's turn lock for one user action, or refuse the action.

        The one place the in-flight policy lives (decision 0003). The hold is
        taken **without waiting**: if another thread or process holds it, a user
        action is already in flight on this chat, and v0.1 neither queues a turn
        behind it, nor defers a Stop or Abandon until it ends, nor interrupts it.
        The refusal is raised before anything durable is written, so the refused
        action leaves no record, and the user may act again once the turn in
        flight has finished. The same thread re-entering its own hold (a launcher
        calling back into the loop) acquires it and meets the loop's own rules.
        """
        with self._turns.hold(chat_id, blocking=False) as held:
            if not held.acquired:
                raise TurnInFlightRefused(
                    "chat %s has a user action in flight on another thread or "
                    "process; v0.1 refuses this action rather than queueing or "
                    "deferring it, and does not interrupt the action in flight, which "
                    "must finish first (decision 0003)" % (chat_id,))
            yield held

    def _active_session(self, chat_id):
        """The chat's live agent, if it has one.

        This asks about **sessions**, not about bindings. A chat with a
        non-terminal session has an agent that may still be alive even if no
        binding holds it, and contract 4.4 is explicit that the rule bites on
        agents. Open bindings are checked too, because either one alone leaves a
        route through -- and because together they are exactly the two refusals
        `ChatStore.create_session` makes, which is what lets a concurrent turn be
        refused *before* it is recorded rather than by the store after.
        """
        pairs = self._store.list_sessions(chat_id)
        for session, _binding in pairs:
            if session["state"] not in self._terminal:
                return session
        for session, binding in pairs:
            if binding.get("released_at") is None:
                return session
        return None

    def _refusal_reason(self, session):
        state = session["state"]
        mode = session["launcher_capabilities"]["continuation"]
        if state in self._terminal:
            return (
                "chat %s still holds an open binding on session %s; the binding must be "
                "released before another agent may be launched"
                % (session["chat_id"], session["session_id"])
            )
        if mode != "persistent":
            return (
                "chat %s is already served by agent session %s (state %r), and this "
                "launcher declares continuation 'fresh_binding', so this turn cannot be "
                "delivered to it. v0.1 binds one agent per chat: stop or abandon the "
                "current agent before starting another."
                % (session["chat_id"], session["session_id"], state)
            )
        return (
            "chat %s is already served by agent session %s, which is in state %r rather "
            "than 'running'; there is no agent to deliver to, and v0.1 will not start a "
            "second one for the same chat."
            % (session["chat_id"], session["session_id"], state)
        )

    def _refuse_concurrent_turn(self, chat_id, session, text):
        """The one place the concurrent-turn policy lives (decision 0003).

        The default, kept from #87: refuse **before** recording, so nothing is in
        the chat's history as a user turn that was never offered to an agent.
        `text` is unused by this policy and passed so the alternative -- #86's
        record-then-refuse -- is a change to this method alone: append
        `self._store.append_user_message(chat_id, text)` before the raise.
        Both are contract-valid; the decision record says how each was measured.
        """
        raise ConcurrentLaunchRefused(self._refusal_reason(session))

    def _continue_turn(self, chat_id, session, text):
        capabilities = session["launcher_capabilities"]
        if capabilities["continuation"] != "persistent" or session["state"] != "running":
            self._refuse_concurrent_turn(chat_id, session, text)
        return self._deliver_turn(chat_id, session, text)

    # -- launching ---------------------------------------------------------

    def _check_declared_bound(self, instruction_text, bound):
        if bound is None:
            return  # nobody has measured one; this package asserts none
        try:
            size = len(instruction_text.encode("utf-8"))
        except (AttributeError, UnicodeEncodeError):
            # Not text the contract accepts at all. The store's pre-flight,
            # which runs next, refuses it before anything is written.
            return
        if size > bound:
            raise InstructionTooLarge(
                "instruction is %d bytes and this launcher declares a measured bound of "
                "%d; the packet is refused before it is recorded, because a packet that "
                "was never sent must not be stored as though it had been" % (size, bound)
            )

    def _agent_handle(self, session):
        """The address contract 6.1 gives every operation after `launch`.

        `stop`, `events` and `deliver` take the handle the launcher returned and
        nothing else; a launcher is required to remember nothing between calls,
        so a session with no handle is a session the harness has no way to
        reach. Failing closed here is the harness's half of that rule, and it is
        what keeps the harness from being written as though launcher-side memory
        could stand in for the handle.
        """
        handle = session.get("agent_handle")
        if not (isinstance(handle, str) and handle):
            raise LaunchBoundaryError(
                "session %s carries no agent_handle, so there is nothing to "
                "address: contract 6.1 gives `stop`, `events` and `deliver` the "
                "handle the launcher returned and nothing else"
                % (session["session_id"],))
        return handle

    def _launch_turn(self, chat_id, text):
        existing = self._active_session(chat_id)
        if existing is not None:
            self._refuse_concurrent_turn(chat_id, existing, text)
        instruction_text = self._compose(chat_id, text, self._store)
        # Read once. The values the pre-flight checks are the values written:
        # a launcher's attributes are its own and can change between two reads.
        launcher_id = self._boundary.launcher_id
        capabilities = self._boundary.capabilities.as_record()
        # Pre-flight, before anything durable is written (review finding R4). A
        # user turn is recorded only if the session and packet it opens would
        # themselves be accepted, so a refusal here leaves no user message, no
        # session and no launch_request: nothing happened.
        self._check_declared_bound(
            instruction_text,
            capabilities.get("instruction_bound_bytes") if isinstance(capabilities, dict)
            else None)
        self._store.preflight_launch(chat_id, text, launcher_id, capabilities,
                                     instruction_text)

        user_message = self._store.append_user_message(chat_id, text)
        user_message_id = user_message["message_id"]
        # The session and its binding are one write, opened *before* the launch
        # is attempted: a launch failure must be attributable to a durable record
        # (contract 4.3), and a launcher that re-enters `send_turn` during
        # `launch` must find the chat already claimed.
        session, _binding = self._store.create_session(
            chat_id, user_message_id, launcher_id, capabilities)
        session_id = session["session_id"]

        try:
            request = self._store.append_launch_request(chat_id, session_id,
                                                        instruction_text)
            instruction = LaunchInstruction(
                **dict((name, request[name]) for name in LaunchInstruction.FIELDS))
        except (ValidationRefused, LaunchBoundaryError) as exc:
            # The packet could not be formed, so nothing was launched. The
            # session records that the turn was attempted and never reached an
            # agent, and releases the chat in the same write.
            self._transition(session, "launch_failed", "harness", "harness_action", None)
            raise NotPermitted("the instruction packet could not be formed: %s" % exc)
        request_id = request["request_id"]
        self._transition(session, "launching", "harness", "harness_action", request_id)

        result = self._call_launch(instruction)
        self._store.append_launch_result(
            chat_id, request_id, session_id, result.outcome,
            agent_handle=result.agent_handle,
            failure_category=result.failure_category,
            detail=result.detail)
        # Before the transition, because a terminal transition releases the
        # binding and this evidence belongs to the launch (contract 7 P1; 6.1
        # *Known residual*). A launch that issued no handle has no `events` call
        # in its future, so this is the only place its output can be preserved.
        self._preserve_launch_output(session, result.payloads)

        if result.outcome == OUTCOME_FAILED:
            self._transition(session, "launch_failed", "launcher", "launch_result",
                             request_id)
            return TurnOutcome(chat_id, session_id, user_message_id, [],
                               session["state"], result.outcome, result.failure_category)

        if result.outcome == OUTCOME_UNKNOWN:
            # Not a failure. `unknown` is a state, is non-terminal, and keeps its
            # binding open because the agent may still be out there. The user's
            # exit is `abandon`, and the harness never resolves it on its own.
            self._transition(session, "unknown", "launcher", "launch_result", request_id)
            return TurnOutcome(chat_id, session_id, user_message_id, [],
                               session["state"], result.outcome, None)

        # The handle enters the session in the same write as `running`, so no
        # process death can leave a session that ran without the handle it ran
        # with (the window #87's N1 lived in).
        self._transition(session, "running", "launcher", "launch_result", request_id,
                         agent_handle=result.agent_handle)
        agent_messages = self._drain(session)
        return TurnOutcome(chat_id, session_id, user_message_id, agent_messages,
                           session["state"], result.outcome, None)

    def _call_launch(self, instruction):
        """Call the launcher and classify whatever comes back.

        A launcher that raises anything at all still produces a classified,
        durable outcome: the five categories are the distinctions #90 must be
        able to count, so an unclassified crash becomes `internal_error` rather
        than a lost session or an invented success.

        **The guarantee covers the classification too** (review finding F2). An
        earlier revision wrapped only `self._boundary.launch(...)`, so building
        the `LaunchResult` that classified a failure could itself raise --
        out of the `except` block, past the caller, leaving the session stuck in
        `launching` with its binding open and no `launch_result` written at all.
        Everything this method does is now inside the guarantee, and the
        fallback below cannot fail: its category is a module constant and its
        detail is built by this method.
        """
        try:
            return self._classify_launch(instruction)
        except Exception as exc:  # noqa: BLE001 - including our own classification
            return LaunchResult(
                OUTCOME_FAILED, failure_category=FAILURE_INTERNAL_ERROR,
                detail="the launch outcome could not be classified: %s: %s"
                       % (type(exc).__name__, exc),
            )

    def _classify_launch(self, instruction):
        try:
            result = self._boundary.launch(instruction)
        except LauncherError as exc:
            return self._failure_carrying_its_output(exc)
        except Exception as exc:  # noqa: BLE001 - a launcher may fail any way it likes
            return LaunchResult(OUTCOME_FAILED, failure_category=FAILURE_INTERNAL_ERROR,
                                detail="%s: %s" % (type(exc).__name__, exc))
        if not isinstance(result, LaunchResult):
            return LaunchResult(
                OUTCOME_FAILED, failure_category=FAILURE_INTERNAL_ERROR,
                detail="launcher returned %r rather than a LaunchResult"
                       % (type(result).__name__,),
            )
        # Re-stated through the constructor rather than trusted, because the
        # object is the launcher's and its attributes are writable after it was
        # built. A result that no longer satisfies its own conditional-field
        # rules raises here, inside `_call_launch`'s guarantee, and becomes a
        # classified failure instead of a record the store refuses mid-launch
        # with the session left in `launching`.
        #
        #
        # A *failed* result that is sound in every field but its output is the
        # one exception, and it takes the raised path's rule (carried from the
        # intake check; `_failure_carrying_its_output`): the launcher's category
        # and the longest prefix of its output the seam accepts are kept,
        # rather than both being lost to `internal_error`. Returning a failure
        # and raising one are two spellings of the same report, so what they
        # preserve must not depend on which the launcher chose. Any other fault
        # -- a handle on a failure, a category or detail the seam refuses --
        # still raises here, as it always did.
        payloads = getattr(result, "payloads", ())
        try:
            return LaunchResult(result.outcome, agent_handle=result.agent_handle,
                                failure_category=result.failure_category,
                                detail=result.detail,
                                payloads=payloads)
        except LaunchBoundaryError:
            if result.outcome != OUTCOME_FAILED:
                raise
            LaunchResult(OUTCOME_FAILED, agent_handle=result.agent_handle,
                         failure_category=result.failure_category, detail=result.detail)
            return self._failed_keeping_what_crosses(
                result.failure_category, result.detail, payloads)

    def _failure_carrying_its_output(self, exc):
        """A launcher's categorised failure, with whatever output it produced.

        The category is the launcher's honest answer and #90 counts them, so it
        survives however the launcher packed its output. If the output does not
        satisfy the seam's rules -- not launcher-sourced, or not contiguous from
        1 -- the refusal is written into the `detail`, which is durable on the
        `launch_result`. Losing the category to report a packing mistake would
        turn an honest `unavailable` into an `internal_error` and make the
        failure uncountable.

        **One payload's packing mistake does not cost the others** (review
        finding F2; contract 7 P1). The whole set used to be dropped, so a single
        agent-sourced payload among launcher-sourced ones lost every honest
        payload with it. What crosses now is the longest prefix the seam accepts,
        which is the most this seam can carry: its rules are that the output is
        launcher-sourced and is the whole history of a session that never ran,
        contiguous from 1, so a payload dropped from the middle would leave a gap
        the harness has no handle to record an observation about. The prefix is
        found by *asking the seam*, one payload shorter at a time, rather than by
        restating its rules here -- a second copy of the rules is a second thing
        to keep in step with them.
        """
        return self._failed_keeping_what_crosses(
            exc.category, exc.detail, getattr(exc, "payloads", ()))

    def _failed_keeping_what_crosses(self, category, detail, payloads):
        """A failed launch result with the launcher's category and as much of its
        output as the seam accepts: the one rule for a failure that was raised
        and one that was returned. Callers have already established that the
        category and detail cross the seam; only the output is in question.
        """
        payloads = tuple(payloads or ())
        try:
            return LaunchResult(OUTCOME_FAILED, failure_category=category,
                                detail=detail, payloads=payloads)
        except LaunchBoundaryError as refused:
            for kept in range(len(payloads) - 1, -1, -1):
                try:
                    return LaunchResult(
                        OUTCOME_FAILED, failure_category=category,
                        detail="%s [payloads %d..%d could not cross the seam and were "
                               "not preserved: %s]"
                               % (detail or "", kept + 1, len(payloads), refused),
                        payloads=payloads[:kept])
                except LaunchBoundaryError:
                    continue
            return LaunchResult(
                OUTCOME_FAILED, failure_category=category,
                detail="%s [the launcher's own output could not cross the seam and "
                       "was not preserved: %s]" % (detail or "", refused))

    # -- delivering (persistent continuation) ------------------------------

    def _deliver_turn(self, chat_id, session, text):
        session_id = session["session_id"]
        # Resolved before anything durable is written: a `delivery_request` is a
        # record that the harness addressed the agent, and it may not exist on a
        # session with no handle (contract 4.3, ADDRESSED_WITHOUT_HANDLE).
        agent_handle = self._agent_handle(session)
        self._check_declared_bound(text, self._boundary.capabilities.instruction_bound_bytes)
        # The store's own pre-flight of the would-be message and delivery packet,
        # asked before the user's turn is recorded rather than discovered after
        # it (review finding R4). It reads and writes nothing, and it runs the
        # same delivery preconditions (`_require_deliverable`), record check and
        # session-recorded bound that `append_delivery_request` runs, so a turn
        # the store would not deliver is never left in the chat unoffered.
        self._store.preflight_delivery(chat_id, session_id, text, text)
        user_message_id = self._store.append_user_message(chat_id, text)["message_id"]

        # Recorded before the call, with `acknowledged: null`. A turn the harness
        # sent to an agent and did not record is a turn nobody can investigate,
        # and an acknowledgement we never received is unknown rather than false.
        packet = self._store.append_delivery_request(chat_id, session_id, text)
        instruction = DeliveryInstruction(
            **dict((name, packet[name]) for name in DeliveryInstruction.FIELDS))

        try:
            ack = self._boundary.deliver(agent_handle, instruction)
        except LauncherError as exc:
            ack = DeliveryAck(None, detail="%s: %s" % (exc.category, exc.detail))
        if not isinstance(ack, DeliveryAck):
            ack = DeliveryAck(None, detail="launcher returned %r rather than a DeliveryAck"
                                           % (type(ack).__name__,))
        acknowledged = ack.acknowledged
        if acknowledged is not None and not isinstance(acknowledged, bool):
            acknowledged = None  # a launcher-mutated ack says nothing usable
        if acknowledged is not None:
            self._store.record_delivery_acknowledgement(
                chat_id, session_id, packet["delivery_id"], acknowledged)

        agent_messages = self._drain(session)
        return TurnOutcome(chat_id, session_id, user_message_id, agent_messages,
                           session["state"], None, None, delivered=True)

    # -- reading what the agent produced -----------------------------------

    def _drain(self, session):
        """Read this turn's output through `events`, preserve it, and, when the
        turn was seen to end with nothing to show, say so (decision 0006).

        The notice is written from what this read observed and from nothing
        else -- never because time passed -- and only when everything it read
        is held exactly as it arrived, because the notice says so. A read that
        ends in a refusal still writes it when the turn was reported ended and
        nothing was lost (a declined claim loses nothing), and the refusal is
        then raised as before.
        """
        agent_messages = []
        seen = _TurnSeen()
        try:
            self._read_turn(session, agent_messages, seen)
        except (StoreError, LaunchBoundaryError):
            self._notice_if_seen_unanswered(session, seen, refused=True)
            raise
        self._notice_if_seen_unanswered(session, seen)
        return agent_messages

    def _read_turn(self, session, agent_messages, seen, page=None, call_returned=True):
        """Read this turn's output through `events` and preserve all of it.

        Terminates on what the launcher reported and on nothing else: a turn
        boundary, a lifecycle event, an end of stream, a read failure, or a page
        with nothing in it. There is no timeout and no poll interval; an agent
        that is merely quiet blocks here, which is v0.1's honest behaviour and
        #83's gap rather than something to paper over with a timer.

        `seen` records whether the turn was **observed to end** (decision 0006):
        the launcher reported a turn end on a page it read, or -- for a session
        whose launcher declares `one_shot` -- `_one_shot_turn_ended` holds. A
        read failure, a page that is not a page, and an empty page on a session
        with a stream are not turn ends: nothing observed the turn end.

        The drain calls this after the launch or delivery call returned, so
        `call_returned` is true and `page` is None. Re-attachment on a
        `one_shot` session calls it with the page it already read and
        `call_returned` false (review finding F1): it does not know the call
        returned until a page shows output that call produced, and from there
        it reads on exactly as the drain does, so a response served over several
        pages is read to its end, rendered under the turn it answers, and ends
        by the one rule.
        """
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        agent_handle = self._agent_handle(session)
        has_stream = self._session_has_stream(session)

        while True:
            after = self._store.next_event_sequence(chat_id, session_id) - 1
            if page is None:
                try:
                    page = self._boundary.events(agent_handle, after)
                except LauncherError as exc:
                    # A reader-side failure is not an end of stream. It says
                    # nothing about whether the agent is alive (contract 4.7,
                    # 6.1).
                    observation_id = self._record_observation(
                        session, "stream_read_failed",
                        "%s: %s" % (exc.category, exc.detail))
                    if session["state"] == "running":
                        self._transition(session, "unknown", "launcher", "observation",
                                         observation_id)
                    return agent_messages

            if not isinstance(page, EventsPage):
                # Intake drop 4. A non-page carries no payload, so there are no
                # bytes to preserve; what there is, is the fact that a read was
                # made and came back unusable, and 4.7's `stream_read_failed` is
                # exactly that -- a failure on *our* side of the read, saying
                # nothing about whether the agent is alive. Recorded before the
                # refusal, so a turn that ends here is not a turn with no trace.
                self._record_observation(
                    session, "stream_read_failed",
                    "events returned %r rather than an EventsPage, so the read "
                    "produced nothing this harness can preserve"
                    % (type(page).__name__,))
                raise LaunchBoundaryError(
                    "events must return an EventsPage; got %r" % (type(page).__name__,))
            if page.stream_ended and not has_stream:
                # The one-shot form of inferring from silence. Refused as a fact,
                # not merely as a spelling: the typed payload is refused below.
                #
                # The page is taken *first* (contract 7 P1). The payloads on it
                # are ordinary and the launcher's claim about its stream says
                # nothing against them; refusing before reading them discarded a
                # whole page, which is strictly more than the typed-payload cause
                # below discards. What a page does to a chat must not depend on
                # which refusal the page also happens to trip.
                taken = self._take_page(session, page, has_stream, agent_messages)
                seen.took(taken)
                if taken.refusal is not None:
                    raise taken.refusal
                raise LaunchBoundaryError(
                    "launcher declares response_shape 'one_shot' and has no stream, but "
                    "signalled that a stream ended")

            taken = self._take_page(session, page, has_stream, agent_messages)
            seen.took(taken)
            turn_complete, reached_terminal, stream_end_event_id, stored_any = (
                taken.turn_complete, taken.reached_terminal, taken.stream_end_event_id,
                taken.stored_any)
            if taken.refusal is not None:
                # Review finding R5: the rest of the page was preserved first.
                raise taken.refusal

            if reached_terminal:
                # The binding was released in the same write as the terminal
                # transition; there is no second write to make here.
                return agent_messages
            if stream_end_event_id is not None:
                # The stream closing is an observed fact, unlike silence on an
                # open stream (contract 5.3 cause 2).
                if session["state"] == "running":
                    self._transition(session, "unknown", "launcher", "stream_end",
                                     stream_end_event_id)
                return agent_messages
            if self._one_shot_turn_ended(has_stream, call_returned, page):
                seen.ended = True
            if turn_complete or not page.payloads:
                return agent_messages
            if not stored_any:
                # Review finding F1. `events` is resumable *by sequence*: the
                # caller resumes by passing the last sequence it stored, so a
                # page in which every payload was already stored means the
                # launcher did not honour `after_sequence` and the next call
                # would ask the same question and get the same answer. Looping
                # is then unbounded, with the chat lock held and nothing durable
                # written -- a hung UI at full CPU rather than a stated refusal.
                #
                # This is a refusal about a fact the launcher supplied, not an
                # inference from elapsed time: no duration is measured and no
                # threshold compared.
                raise LaunchBoundaryError(
                    "launcher returned %d payload(s) for session %s after sequence "
                    "%d and every one of them was already stored; `events` is "
                    "resumable by sequence (contract 6.1), so a page that does not "
                    "advance means the launcher ignored after_sequence and reading "
                    "again cannot make progress"
                    % (len(page.payloads), session_id, after))
            # This page stored output. On a `one_shot` session that output was
            # produced by a call that has returned -- such a launcher serves
            # nothing else -- so from here an empty page is that call's end.
            call_returned = True
            page = None

    @staticmethod
    def _one_shot_turn_ended(has_stream, call_returned, page):
        """The one definition of "a `one_shot` turn has ended" (review finding F1).

        Contract 6.1: a launcher declaring `one_shot` has no stream; it returns
        the agent's response from the call, and `events` serves that response,
        resumably, by sequence, possibly over several pages. So once the call is
        known to have returned, a page with nothing more on it is the end of what
        it produced. Before that, an empty page is only a read that found
        nothing, which observes nothing -- the harness may have died inside the
        call. A launcher's claim that its stream ended is no such end: it has
        no stream, and the claim is declined wherever it appears.

        The drain knows the call returned, because it reads only after the call
        does. Re-attachment knows it once a page has stored output that call
        produced, and then reads on to the empty page like the drain. Both
        decide here, so neither can end a turn the other would not. The other
        ends -- a reported `turn_complete`, a terminal lifecycle event, a
        stream's `stream_end` -- are what the launcher reported and are read
        from the page (`_PageTaken.turn_end_reported`) the same way on both.
        """
        return not has_stream and call_returned and not page.payloads

    def _take_page(self, session, page, has_stream, agent_messages):
        """Preserve every payload of one page, and act on what the launcher reported.

        Used by the drain and by re-attachment, so what a page does to a chat
        does not depend on which of the two read it.

        **Each payload is offered to the store on its own, whatever refuses
        another one** (review finding R5; #88 intake; contract 7 P1). A refusal
        is kept, the next payload is taken, and the first refusal is handed back
        once the whole page has been offered, so it is still not silent. That
        recovers every payload behind a refusal **that left its own sequence
        written**: a payload that contradicts one already preserved at its
        sequence, a lifecycle transition the store refuses because the wall
        clock stepped back, and a claim the harness declines after preserving
        the bytes (an end of stream from a launcher with no stream, or one
        sourced to the agent). A page `[1, 2, 5, 3]` preserves `[1, 2, 3]`, and
        a one-shot launcher's `[text@1, stream_end@2, text@3]` preserves all
        three (the human's decision of 2026-09-15; see
        `PRESERVE_UNATTRIBUTABLE_STREAM_END`).

        **It does not recover a payload behind a refusal that left a sequence
        unwritten** (carried finding C2). A store refusal of a sequence not yet
        stored -- a write the store fails, or a payload whose own sequence is
        ahead of what is stored -- leaves a gap, and every later payload on the
        session then fails contiguity: contract P3 says a gap is never closed
        silently, so they are offered and refused rather than preserved. That
        is the one payload shape still preserved nowhere; `_preserve_launch_output`
        has the same residual, and the README names it.

        `taken.lost` records whether any payload here is not held by the store
        exactly as it arrived, which is what decision 0006's notice may not be
        written over.
        """
        taken = _PageTaken()
        for payload in page.payloads:
            try:
                self._take_payload(session, payload, has_stream, taken, agent_messages)
            except (StoreError, LaunchBoundaryError) as exc:
                if taken.refusal is None:
                    taken.refusal = exc
                if not self._holds_exactly(session, payload):
                    taken.lost = True
        return taken

    def _holds_exactly(self, session, payload):
        """Whether the store holds this payload's bytes at its sequence, exactly.

        Asked only after a refusal, to tell a refusal that came after the bytes
        were preserved (a declined claim, a refused transition) from one that
        cost them (a gap, a contradicting replay, a failed write). Read back from
        the store rather than inferred from the kind of refusal, so the answer
        is the durable fact the notice's words depend on.
        """
        try:
            found = self._store.read_diagnostic_events(
                session["chat_id"], session["session_id"],
                sequence_from=payload.sequence, sequence_to=payload.sequence, limit=1)
        except StoreError:
            return False
        if len(found) != 1:
            return False
        raw = found[0]["raw"]
        if raw["encoding"] == "base64":
            held = base64.b64decode(raw["body"])
        else:
            held = raw["body"].encode("utf-8")
        return held == payload.raw

    def _take_payload(self, session, payload, has_stream, taken, agent_messages):
        """Preserve one payload, then act on what the launcher said it was.

        Preservation comes first and interpretation second, which is the whole
        of this checkpoint: a payload the seam will refuse to *read* is still a
        payload the integration produced, and P1 is unconditional. The
        end-of-stream refusal below therefore runs after the payload is
        preserved, not before it -- the evidence is kept and the claim is still
        refused. The other end-of-stream refusal, and the decision of
        2026-09-15 behind it, live in
        `_preserve_declining_what_it_cannot_attribute`, which every channel that
        preserves calls.
        """
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        event_id = self._preserve_declining_what_it_cannot_attribute(
            session, payload, has_stream)
        if event_id is None:
            return  # a replayed (session_id, sequence); already stored

        taken.stored_any = True
        if payload.is_chat_text:
            agent_messages.append(
                self._store.append_agent_message(
                    chat_id, session_id, event_id, payload.text)["message_id"])
        if payload.interpretation != INTERPRETATION_RECOGNIZED:
            return
        if payload.interpreted_type == PAYLOAD_TURN_COMPLETE:
            taken.turn_complete = True
            taken.turn_end_reported = True
            return
        if payload.interpreted_type == PAYLOAD_STREAM_END and payload.source != SOURCE_LAUNCHER:
            # Preserved above, and still refused as a *reading*: only the
            # launcher can say its own stream ended (contract 6.1). The store
            # accepts the record -- nothing cites it as evidence -- so the bytes
            # survive and the claim does not.
            raise LaunchBoundaryError(
                "end-of-stream must be signalled by the launcher; this payload is "
                "sourced to %r" % (payload.source,))
        if payload.source != SOURCE_LAUNCHER:
            return
        if payload.interpreted_type == PAYLOAD_SESSION_COMPLETED:
            taken.turn_end_reported = True
            self._transition(session, "completed", "launcher", "event", event_id)
            taken.reached_terminal = True
        elif payload.interpreted_type == PAYLOAD_SESSION_FAILED:
            taken.turn_end_reported = True
            self._transition(session, "failed", "launcher", "event", event_id)
            taken.reached_terminal = True
        elif payload.interpreted_type == PAYLOAD_STREAM_END:
            # Only reachable on a session with a stream: on one without, the
            # reading was declined when the payload was preserved.
            taken.turn_end_reported = True
            taken.stream_end_event_id = event_id

    def _preserve_declining_what_it_cannot_attribute(self, session, payload, has_stream):
        """Preserve one payload, declining only a reading this session forbids.

        **Every channel that preserves calls this**, so the rules are applied by
        construction rather than by each call site remembering them. Review
        finding F1 was exactly that omission: the no-handle `payloads` channel
        was added calling `_preserve` directly, so it inherited neither this
        decision nor the containment around it, and the one payload shape the
        human ruled on was still refused before preservation on it. A rule that
        lives in one function cannot be missed by the next channel; a rule
        copied into each call site can be, and mutation cannot see the gap
        because there is no guard there to mutate.

        `attribute=False` writes the bytes as `unrecognized` / `null`: contract
        7 P2 covers "a type this build does not know, **or knows but cannot
        attribute on this session**" since the 2026-09-15 correction, and a
        `stream_end` from a launcher with no stream is exactly the second case.
        The bytes are kept and the *assertion* is refused exactly as 6.1 states
        it -- nothing recorded the `stream_end` reading, so
        `STREAM_END_UNSUPPORTED` is unreachable rather than suppressed.

        Returns the event id, or None for a replay the store already holds.
        """
        unattributable = (payload.interpreted_type == PAYLOAD_STREAM_END
                          and not has_stream)
        if unattributable and not PRESERVE_UNATTRIBUTABLE_STREAM_END:
            # The older accepted behaviour, kept reachable and pinned so the
            # human's decision stays a one-line change. Not what ships. See the
            # constant.
            raise self._unattributable_stream_end(payload)
        event_id = self._preserve(session, payload, attribute=not unattributable)
        if unattributable:
            raise self._unattributable_stream_end(payload)
        return event_id

    def _session_has_stream(self, session):
        """Whether the session's own recorded launcher declares a stream.

        Read from the session, not from `self._boundary.capabilities` (review
        finding C1). The store decides whether a `stream_end` record may exist
        from `session["launcher_capabilities"]["response_shape"]`, so a
        preservation that asked the *configured* launcher instead could disagree
        with it -- and did, whenever a restart was configured with a launcher of
        a different shape from the one the session was opened with: the payload
        was refused by the store, its bytes lost, and the gap took everything
        behind it on the page. This is the mirror image of the store's own read,
        by deliberate construction, so the two cannot disagree again.

        Defensive in the same way the store is: a record that is not a mapping
        answers no, which preserves the bytes and declines the reading rather
        than asserting something about a session whose capabilities are
        unreadable.
        """
        capabilities = session.get("launcher_capabilities")
        shape = (capabilities.get("response_shape")
                 if isinstance(capabilities, dict) else None)
        return shape == RESPONSE_SHAPE_STREAM

    def _unattributable_stream_end(self, payload):
        return LaunchBoundaryError(
            "launcher declares response_shape 'one_shot', which has no stream to "
            "end, but produced a payload typed %r" % (PAYLOAD_STREAM_END,))

    def _preserve(self, session, payload, attribute=True):
        """Preserve one payload verbatim (contract 7, P1).

        Preservation is unconditional and is especially required when
        interpretation failed, because those are the cases v0.1 exists to
        discover. Returns the event id, or None when this `(session_id,
        sequence)` was already stored with the same evidence -- `events` is
        at-least-once. A *different* payload at a stored sequence is not a
        replay, and the store refuses it (`StoreCorrupt`).

        `attribute=False` keeps the bytes and drops the launcher's reading of
        them: the record is written `unrecognized` with `interpreted_type: null`,
        which contract 7 P2 covers for a type this build "knows but cannot
        attribute on this session". It exists for the one reading the contract
        forbids on a session while still requiring its bytes -- see
        `PRESERVE_UNATTRIBUTABLE_STREAM_END`, which the human set to preserve on
        2026-09-15. The `raw.body` is identical either way, so nothing about the
        payload is lost; only the harness's claim about what it was.
        """
        try:
            body = payload.raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            body = base64.b64encode(payload.raw).decode("ascii")
            encoding = "base64"
        # Contiguity is the launcher's to assign and the harness's to refuse.
        # The store refuses a gap too; this states it as the launcher's misuse
        # of the seam, before the store is asked, so the refusal a launcher
        # author sees names the seam rule it broke.
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        last = self._store.next_event_sequence(chat_id, session_id) - 1
        if payload.sequence > last + 1:
            raise LaunchBoundaryError(
                "launcher returned payload sequence %d for session %s while %d is "
                "stored; an ordered history must be contiguous, and a gap means a "
                "payload was lost rather than that one is still coming"
                % (payload.sequence, session_id, last))
        interpretation = payload.interpretation
        interpreted_type = payload.interpreted_type
        if not attribute:
            interpretation = INTERPRETATION_UNRECOGNIZED
            interpreted_type = None
        record, created = self._store.append_diagnostic_event(
            chat_id, session_id, payload.sequence, payload.source,
            interpretation, interpreted_type, body, encoding=encoding)
        if not created:
            return None
        return record["event_id"]

    def _preserve_launch_output(self, session, payloads):
        """Preserve what a launch produced when it issued no handle (contract 7 P1).

        The intake gap the internal JSONL model exposed: a launch that reads two
        lines and never sees a thread start has nowhere to put them. `events`
        takes the handle and nothing else, and this session has none and never
        will, so the `launch` call itself is the only channel these bytes can
        cross -- which is the reading contract 6.1 states under *Known residual*,
        and is what fixtures `valid/05-launch-failure` and
        `valid/09-launch-outcome-unknown` already are.

        Correlation is the chat and the session that failed to open, at
        sequences 1..N, written **before** the terminal transition: a terminal
        session has released its binding, and evidence about a launch belongs to
        the launch.

        **This channel carries the same kind of data as `events`, so it applies
        the same two rules** (review findings F1 and F2; contract 7 P1). It
        preserves through `_preserve_declining_what_it_cannot_attribute`, so a
        `stream_end`-typed payload from a launcher with no stream keeps its
        bytes and loses only the reading, exactly as it does on the drain; and
        it takes each payload on its own rather than stopping at the first
        refusal. Before that, a `one_shot` launcher's failed launch preserved the
        payloads up to the first `stream_end`-typed one and lost the rest -- the
        single shape the human ruled on, on the one channel the ruling was not
        applied to.

        Taking each payload on its own recovers every payload whose refusal
        leaves its own sequence written, which is all of them the seam refuses.
        It does not recover a payload behind a refusal that left a sequence
        *unwritten* -- a store refusal of a not-yet-stored sequence -- because
        preserving it would close a gap, and contract P3 says a gap is never
        closed silently. That residual is the same on this channel and on
        `_take_page`, and it is the one thing still not preserved anywhere.

        A refusal here still cannot be recorded as an observation -- every
        observation kind that would fit addresses the agent through a handle this
        session does not have -- so it is not swallowed either: the first one is
        raised once every payload has been offered to the store, leaving the
        session in `launching` with its `launch_result` already durable, which is
        the ordinary interrupted-launch shape that `_resolve_interrupted_launch`
        finishes at the next start from the record the launcher itself produced.
        Failing closed into a recoverable shape is better than reporting a launch
        outcome whose evidence was silently dropped.
        """
        has_stream = self._session_has_stream(session)
        refusal = None
        for payload in payloads:
            try:
                self._preserve_declining_what_it_cannot_attribute(
                    session, payload, has_stream)
            except (StoreError, LaunchBoundaryError) as exc:
                if refusal is None:
                    refusal = exc
        if refusal is not None:
            raise refusal

    # -- user actions ------------------------------------------------------

    def stop_agent(self, chat_id, reason):
        """The user stops the agent. Only the user may (contract 5.2).

        An unconfirmed stop is not a dead end and is not a failure: it is an
        observation that leaves liveness undeterminable, which carries the
        session to `unknown`, from which the user can always abandon.

        A Stop that meets a user action in flight on this chat is refused with
        `TurnInFlightRefused` and records nothing (decision 0003): v0.1 does not
        interrupt a turn in flight, and a Stop that could not reach it must never
        be recorded as `terminated` once the turn has ended.
        """
        with self._user_action(chat_id):
            session = self._active_session(chat_id)
            if session is None:
                raise NotPermitted("chat %s has no live agent to stop" % chat_id)
            if session["state"] not in ("running", "unknown"):
                raise NotPermitted(
                    "session %s is in state %r; only a running or unknown agent can be "
                    "stopped" % (session["session_id"], session["state"]))
            handle = session.get("agent_handle")
            if not (isinstance(handle, str) and handle):
                # An `unknown` session whose launch was never accepted never
                # received a handle. `stop` takes the handle and nothing else
                # (6.1), so there is nothing to address and a stop observation
                # here would claim an operation that cannot have happened. The
                # user is not stuck: `abandon` is 5.4's exit from exactly this
                # state, and it needs no handle.
                raise NotPermitted(
                    "session %s never received an agent_handle, so there is nothing "
                    "to stop; abandon it instead (contract 5.4)"
                    % (session["session_id"],))
            self._stop(session, handle, reason)
            return session["state"]

    def _stop(self, session, handle, reason):
        """Issue the user's `stop` and record what came back, whatever came back.

        The launcher may fail any way it likes. A `LauncherError`, any other
        exception, or an answer that is not a usable `StopAck` is a stop the
        boundary did not confirm -- contract 5.3 cause 4 -- and is recorded as
        `stop_unconfirmed` rather than propagating with nothing written (review:
        the first edge of the stop-then-abandon composition).
        """
        try:
            ack = self._boundary.stop(handle, reason)
        except LauncherError as exc:
            ack, detail = None, "%s: %s" % (exc.category, exc.detail)
        except Exception as exc:  # noqa: BLE001 - a launcher may fail any way it likes
            ack, detail = None, "%s: %s" % (type(exc).__name__, exc)
        else:
            if isinstance(ack, StopAck):
                detail = ack.detail if isinstance(ack.detail, str) else None
            else:
                detail = "launcher returned %r rather than a StopAck" % (type(ack).__name__,)
                ack = None
        confirmed = ack is not None and ack.confirmed is True
        observation_id = self._record_observation(
            session, "stop_confirmed" if confirmed else "stop_unconfirmed", detail)
        if confirmed:
            self._transition(session, "terminated", "user", "observation", observation_id)
        elif session["state"] == "running":
            self._transition(session, "unknown", "launcher", "observation", observation_id)

    STOP_REASON = "the user abandoned this agent"

    def abandon(self, chat_id):
        """The one lifecycle action the shell exposes (decisions 0003 and D2).

        It takes the chat's non-terminal session out of every state it can be
        left in, by contract-legal transitions only, and it is always the user's
        action: no timer, no inactivity inference, and the harness never takes
        it on its own.

        ==============  ==============================================================
        `unknown`       `unknown -> abandoned` (user, `user_action`), as before.
        `running`       the user's `stop` (contract 5.2): `running -> terminated` on a
                        confirmed stop -- nothing is then left to abandon, and that is
                        success -- or, on any unconfirmed stop, `running -> unknown`
                        (`stop_unconfirmed`, 5.3 cause 4) then `unknown -> abandoned`.
        `launching`     contract 5.4's resolution of an interrupted launch, for this
                        chat, on demand and from its durable records alone
                        (`_resolve_interrupted_launch`): the `launch_result` decides
                        `running` / `launch_failed` / `unknown`, and with none the
                        `reattach_failed` observation gives `unknown`. Then the row
                        above for the state that yields.
        `pending`       5.4's resolution of a launch never issued:
                        `pending -> launch_failed` (harness, `harness_action`).
        ==============  ==============================================================

        The action never calls `events`: on a live agent that is quiet `events`
        blocks, by design, and the exit must not.

        **A user action in flight is not interrupted, and this is not deferred
        behind it.** The action in flight holds the chat's turn lock, so from
        another thread or process this is refused at once with
        `TurnInFlightRefused`, in every session state and with nothing recorded
        (decision 0003): v0.1 lets a dispatched turn finish, and the user's action
        is never turned into a `terminated` dated after it. The action is
        available again as soon as the turn ends; a turn that never returns is
        released when its process exits, and contract 5.4 re-attachment at the
        next start gives the chat the exits above. A launcher calling back into
        the loop during an action on this chat is refused every exit but
        `unknown -> abandoned`, which it always had.
        """
        with self._user_action(chat_id) as held:
            session = self._active_session(chat_id)
            if session is None:
                raise NotPermitted("chat %s has no session to abandon" % chat_id)
            state = session["state"]
            if state == "unknown":
                self._transition(session, "abandoned", "user", "user_action", None)
                return session["state"]
            if held.nested:
                raise NotPermitted(
                    "session %s is in state %r and an action on chat %s is in flight "
                    "on this thread; it is not abandoned from inside that action"
                    % (session["session_id"], state, chat_id))
            if state == "pending":
                self._transition(session, "launch_failed", "harness", "harness_action", None)
                return session["state"]
            if state == "launching":
                self._resolve_interrupted_launch(session)
                if session["state"] == "unknown":
                    self._transition(session, "abandoned", "user", "user_action", None)
                if session["state"] in self._terminal:
                    return session["state"]
            if session["state"] != "running":
                raise NotPermitted(
                    "session %s is in state %r, which holds no agent to abandon"
                    % (session["session_id"], session["state"]))
            self._stop(session, session["agent_handle"], self.STOP_REASON)
            if session["state"] == "unknown":
                self._transition(session, "abandoned", "user", "user_action", None)
            return session["state"]

    # -- restart (contract 5.4) -------------------------------------------

    def reattach_on_start(self):
        """Re-attach to every non-terminal session, once, at start.

        D1 makes durable records canonical across a restart, so what happens to
        a live session at restart is v0.1's obligation: a durable record that
        survives into a state with no legal exit is not durable, it is a chat
        nobody can use. This happens when the harness starts and never on a
        schedule; everything after it is a user action.

        A chat whose turn lock is held by another hold is left alone: under
        decision D1 no other process can serve this store, so that is another
        chat loop over the same store in this process, serving the chat right
        now. A chat whose records cannot be read -- **including its own chat
        record** (review finding R8) -- or whose re-attachment the store refuses
        to record, or whose launcher misuses the seam while being re-attached, is
        left as it is and the next chat is re-attached: one damaged chat must not
        keep the application from starting for every other one, and every later
        action on it reads the same records and fails closed (contract D3), so
        skipping it here makes nothing look sound. Archived chats are
        re-attached too: an archived chat's agent is still an agent.
        """
        # Decision D1: nothing is re-attached by a process that is not the one
        # serving this store. Refused here, before the loop, so the refusal is
        # not mistaken below for one chat that could not be re-attached.
        self._store.acquire()
        outcomes = []
        for chat_id in self._store.chat_ids():
            try:
                with self._turns.hold(chat_id, blocking=False) as held:
                    if not held.acquired:
                        continue
                    self._store.read_chat(chat_id)
                    for session, _binding in self._store.list_sessions(chat_id):
                        if session["state"] in self._terminal:
                            continue
                        outcomes.append((session["session_id"], self._reattach(session)))
            except (StoreError, LaunchBoundaryError):
                continue
        return outcomes

    def _reattach(self, session):
        state = session["state"]
        chat_id = session["chat_id"]
        session_id = session["session_id"]

        if state == "pending":
            # No launch was ever issued, so there is no agent and nothing to
            # re-attach to. "We never got an agent" is precisely launch_failed.
            self._transition(session, "launch_failed", "harness", "harness_action", None)
            return "launch_failed"

        if state == "launching":
            # `launching` is not a fact about the handle (re-review N1): it means
            # the launch was issued and its outcome was never written as a state.
            # Resolving it first, from the record that says how the launch ended,
            # is what puts every shape in a branch.
            state = self._resolve_interrupted_launch(session)
            if state != "running":
                # `launch_failed` is terminal and `unknown`'s exit is the user's
                # `abandon`; neither has a handle to re-attach through.
                return state

        handle = session.get("agent_handle")
        if not (isinstance(handle, str) and handle):
            # Contract 5.4's second case, stated over the fact rather than over
            # the state name. `events` takes the handle and nothing else, so a
            # session that never received one has nothing to address and the
            # attempt fails without being made. `reattach_failed` is the one
            # observation kind that needs no handle, for exactly this reason.
            observation_id = self._record_observation(
                session, "reattach_failed",
                "session %s carries no agent_handle -- its launch was never accepted, "
                "so no handle was ever issued and there is nothing to re-attach "
                "through" % (session_id,))
            if state != "unknown":
                self._transition(session, "unknown", "launcher", "observation",
                                 observation_id)
            return "unknown"

        try:
            page = self._boundary.events(
                handle, self._store.next_event_sequence(chat_id, session_id) - 1)
        except LauncherError as exc:
            observation_id = self._record_observation(
                session, "reattach_failed", "%s: %s" % (exc.category, exc.detail))
            if state == "running":
                self._transition(session, "unknown", "launcher", "observation",
                                 observation_id)
            # From `unknown` there is no `unknown -> unknown`; the session simply
            # stays unknown, and the user's exit is still `abandon`.
            return session["state"]

        observation_id = self._record_observation(session, "reattached", None)
        if state == "unknown":
            self._transition(session, "running", "launcher", "observation", observation_id)
        if isinstance(page, EventsPage):
            self._take_reattachment_page(session, page)
        else:
            # Intake drop 4, on the re-attachment path. There are no bytes to
            # preserve, and until now there was no record either: the page was
            # dropped in silence and the session was left reading as though
            # re-attachment had found nothing. The read happened and it failed on
            # our side, which is what 4.7's `stream_read_failed` records. The
            # session is not carried anywhere on the strength of it -- a failed
            # read says nothing about whether the agent is alive.
            self._record_observation(
                session, "stream_read_failed",
                "re-attachment's events call returned %r rather than an EventsPage, "
                "so the read produced nothing this harness can preserve"
                % (type(page).__name__,))
        return session["state"]

    def _take_reattachment_page(self, session, page):
        """Preserve what re-attachment read, and act on it as the drain would.

        Re-attachment used to call `events` and discard the page. On a launcher
        that can resume, that page can carry the agent's answer and the report
        that it completed while the harness was down; discarded, a later Stop
        recorded the user terminating an agent that had in fact completed, and
        the evidence it had completed was gone (review, the third edge of the
        stop-then-abandon composition). Preserving it is the minimum truthful
        behaviour, and it is the same page handling the drain uses -- no new
        classification: the launcher classified each payload, as it always has.

        This partly satisfies #88's later intake checkpoint (the review's event
        drop 1). A launcher returning something that is not a page is still
        ignored here, as it was: there are no raw bytes to preserve (drop 4).

        **The notice rule is the drain's** (decision 0006): a turn this read
        shows ended, with nothing lost, gets the notice if it has no reply and
        no notice yet. On a session with a stream, re-attachment reads this one
        page and observes a turn end only when the page reports one -- reading
        on would block start-up on a quiet stream. On a session whose launcher
        declares `one_shot`, it reads on from this page with the drain's own
        loop (review finding F1), which serves only what calls that returned
        produced and so cannot block: a response over several pages is read to
        its end and rendered under the turn it answers, a page the drain would
        refuse is refused here too, and the turn ends by the one definition,
        `_one_shot_turn_ended`. An empty first page observes nothing, so a turn
        whose call never returned stays without a notice, exactly as it stays
        without a reply.
        """
        has_stream = self._session_has_stream(session)
        seen = _TurnSeen()
        try:
            if not has_stream:
                self._read_turn(session, [], seen, page=page, call_returned=False)
            else:
                taken = self._take_page(session, page, has_stream, [])
                seen.took(taken)
                if (taken.stream_end_event_id is not None and not taken.reached_terminal
                        and session["state"] == "running"):
                    self._transition(session, "unknown", "launcher", "stream_end",
                                     taken.stream_end_event_id)
                if taken.refusal is not None:
                    raise taken.refusal
        except (StoreError, LaunchBoundaryError):
            self._notice_if_seen_unanswered(session, seen, refused=True)
            raise
        self._notice_if_seen_unanswered(session, seen)

    def _notice_if_seen_unanswered(self, session, seen, refused=False):
        """The one notice rule, for every path that reads a turn (decision 0006).

        Nothing is written unless the read observed the turn end and lost
        nothing. On a read that is about to raise a refusal the notice is still
        written when both hold -- a declined claim loses no bytes -- but a
        failure to write it does not replace that refusal, which is what the
        turn reports.
        """
        if not seen.ended or seen.lost:
            return None
        if not refused:
            return self._notice_if_unanswered(session)
        try:
            return self._notice_if_unanswered(session)
        except StoreError:
            return None

    def _notice_if_unanswered(self, session):
        """Tell the user that a turn ended with nothing to show, once (decision 0006).

        Called only when the turn was observed to end and everything read was
        preserved exactly; this decides the rest from the chat's durable
        messages alone, so the drain, re-attachment and a restart all apply one
        rule. The turn is the chat's last user message -- a turn is recorded only
        when it is offered to the chat's one live agent (decision 0003), so the
        last one is the turn this session was answering -- and it is answered if
        any agent message follows it. A system message following it is this
        notice, already written: the harness writes no other (`notices`). Either
        way nothing is written, so the notice is idempotent across re-attachment
        and restart, and never shown for a turn that rendered a reply.

        The words are fixed (`notices.NO_SHOWABLE_REPLY`) and carry nothing from
        the integration: no bytes, types, counts or timing. The message has
        `author: "system"` and null provenance, which contract 4.2 already
        permits, and the turn floor ignores it (contract 6.4).
        """
        chat_id = session["chat_id"]
        messages = self._store.read_messages(chat_id)
        last_user = None
        for index, message in enumerate(messages):
            if message["author"] == "user":
                last_user = index
        if last_user is None:
            return None
        for message in messages[last_user + 1:]:
            if message["author"] in ("agent", "system"):
                return None
        return self._store.append_system_message(chat_id, NO_SHOWABLE_REPLY)["message_id"]

    def _resolve_interrupted_launch(self, session):
        """Finish the launch outcome a restart interrupted, from the record of it.

        Contract 5.2's precondition column decides what may resolve `launching`,
        and for every exit but one it names the launcher's own `launch_result`:
        `launching -> running` admits `launch_result` reporting `accepted`,
        `launching -> launch_failed` admits `failed`, `launching -> unknown`
        admits `unknown`. D1 makes that record canonical across a restart, so the
        harness finishes the transition it already holds the evidence for. It
        asks the launcher nothing here.

        On this store the handle is written in the same write as `running`, so
        the shape a restart meets is `launching` with an accepted result and no
        handle on the session; the handle that enters `running` is the one the
        result carries, and the store checks it was issued. A session written by
        #87's own store could also carry that handle already, and resolves the
        same way.

        When no usable `launch_result` survives, nothing can say how the launch
        ended and nothing ever will: that is `unknown`, evidenced by the failed
        re-attachment, which is the one observation kind that needs no handle.
        """
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        requests = self._store.read_launch_requests(chat_id, session_id)
        results = self._store.read_launch_results(chat_id, session_id)
        request_id = requests[0]["request_id"] if requests else None
        result = results[0] if results else None

        if result is not None and request_id is not None:
            outcome = result.get("outcome")
            if outcome == OUTCOME_ACCEPTED and result.get("agent_handle"):
                self._transition(session, "running", "launcher", "launch_result",
                                 request_id, agent_handle=result["agent_handle"])
                return session["state"]
            if outcome == OUTCOME_FAILED:
                self._transition(session, "launch_failed", "launcher", "launch_result",
                                 request_id)
                return session["state"]
            if outcome == OUTCOME_UNKNOWN:
                self._transition(session, "unknown", "launcher", "launch_result",
                                 request_id)
                return session["state"]

        observation_id = self._record_observation(
            session, "reattach_failed",
            "session %s was interrupted in `launching` and no launch_result the "
            "contract can act on survives, so how the launch ended is not "
            "recoverable and no handle was ever issued to re-attach through"
            % (session_id,))
        self._transition(session, "unknown", "launcher", "observation", observation_id)
        return session["state"]

    # -- record plumbing ---------------------------------------------------

    def _transition(self, session, to, owner, evidence_kind, evidence_ref,
                    agent_handle=None):
        """Append a transition through the store and carry its result back.

        The store compares against the state this caller last read, checks the
        pair and its owner against contract 5.2's owner table *and* its
        precondition table, puts a handle into the session only in the write that
        enters `running`, and releases the binding in the same write as a
        terminal state. `session` is updated in place so a caller's view is the
        durable one.
        """
        updated, _binding = self._store.append_transition(
            session["chat_id"], session["session_id"], session["state"], to, owner,
            {"kind": evidence_kind, "ref": evidence_ref}, agent_handle=agent_handle)
        session.clear()
        session.update(updated)

    def _record_observation(self, session, kind, detail):
        # Contract 4.3's addressing rule is the store's: it refuses an
        # observation of an operation that took the handle as its address unless
        # the launcher issued one for this session.
        return self._store.append_session_observation(
            session["chat_id"], session["session_id"], kind, detail)["observation_id"]
