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
import fcntl
import os
import threading

from .contract import terminal_states
from .errors import (
    ConcurrentLaunchRefused,
    InstructionTooLarge,
    NotPermitted,
    StoreError,
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
    OUTCOME_ACCEPTED,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    SOURCE_LAUNCHER,
    StopAck,
)

# The file, inside a chat's own directory, whose lock is held for the duration
# of one user action on that chat. Not the store's `.lock`: the store takes that
# one for each write, and a second descriptor on the same file in one process
# would wait on itself.
TURN_LOCK_NAME = ".turn-lock"


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
    """What taking one page of payloads produced."""

    __slots__ = ("turn_complete", "reached_terminal", "stream_end_event_id", "stored_any",
                 "refusal")

    def __init__(self):
        self.turn_complete = False
        self.reached_terminal = False
        self.stream_end_event_id = None
        self.stored_any = False
        self.refusal = None


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

        with self._turns.hold(chat_id):
            active = self._active_session(chat_id)
            if active is None:
                return self._launch_turn(chat_id, text)
            return self._continue_turn(chat_id, active, text)

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
            return LaunchResult(OUTCOME_FAILED, failure_category=exc.category,
                                detail=exc.detail)
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
        return LaunchResult(result.outcome, agent_handle=result.agent_handle,
                            failure_category=result.failure_category,
                            detail=result.detail)

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
        """Read this turn's output through `events` and preserve all of it.

        Terminates on what the launcher reported and on nothing else: a turn
        boundary, a lifecycle event, an end of stream, a read failure, or a page
        with nothing in it. There is no timeout and no poll interval; an agent
        that is merely quiet blocks here, which is v0.1's honest behaviour and
        #83's gap rather than something to paper over with a timer.
        """
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        agent_handle = self._agent_handle(session)
        capabilities = self._boundary.capabilities
        agent_messages = []

        while True:
            after = self._store.next_event_sequence(chat_id, session_id) - 1
            try:
                page = self._boundary.events(agent_handle, after)
            except LauncherError as exc:
                # A reader-side failure is not an end of stream. It says nothing
                # about whether the agent is alive (contract 4.7, 6.1).
                observation_id = self._record_observation(
                    session, "stream_read_failed",
                    "%s: %s" % (exc.category, exc.detail))
                if session["state"] == "running":
                    self._transition(session, "unknown", "launcher", "observation",
                                     observation_id)
                return agent_messages

            if not isinstance(page, EventsPage):
                raise LaunchBoundaryError(
                    "events must return an EventsPage; got %r" % (type(page).__name__,))
            if page.stream_ended and not capabilities.has_stream:
                # The one-shot form of inferring from silence. Refused as a fact,
                # not merely as a spelling: the typed payload is refused below.
                raise LaunchBoundaryError(
                    "launcher declares response_shape 'one_shot' and has no stream, but "
                    "signalled that a stream ended")

            taken = self._take_page(session, page, capabilities, agent_messages)
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

    def _take_page(self, session, page, capabilities, agent_messages):
        """Preserve every payload of one page, and act on what the launcher reported.

        Used by the drain and by re-attachment, so what a page does to a chat
        does not depend on which of the two read it.

        **A store refusal of one payload does not cost the payloads after it**
        (review finding R5; contract 7, P1). A payload that contradicts one
        already preserved at its sequence, or a lifecycle transition the store
        refuses because the wall clock stepped back, used to raise out of the
        loop and drop every honest payload later on the same page -- #87
        preserved them, convergence did not. Each payload is now taken on its
        own: a `StoreError` it meets is kept, the next payload is taken, and the
        first refusal is handed back once the whole page has been preserved, so
        it is still not silent.

        The seam's own refusals -- a sequence gap, an end of stream from a
        launcher with no stream -- still stop the page where they are met; the
        loss they cause is #88's intake checkpoint's, not this one's.
        """
        chat_id = session["chat_id"]
        session_id = session["session_id"]
        taken = _PageTaken()
        for payload in page.payloads:
            if payload.interpreted_type == PAYLOAD_STREAM_END:
                self._reject_unsupported_stream_end(payload, capabilities)
            try:
                event_id = self._preserve(session, payload)
                if event_id is None:
                    continue  # a replayed (session_id, sequence); already stored
                taken.stored_any = True
                if payload.is_chat_text:
                    agent_messages.append(
                        self._store.append_agent_message(
                            chat_id, session_id, event_id, payload.text)["message_id"])
                if payload.interpretation != INTERPRETATION_RECOGNIZED:
                    continue
                if payload.interpreted_type == PAYLOAD_TURN_COMPLETE:
                    taken.turn_complete = True
                elif payload.source != SOURCE_LAUNCHER:
                    continue
                elif payload.interpreted_type == PAYLOAD_SESSION_COMPLETED:
                    self._transition(session, "completed", "launcher", "event", event_id)
                    taken.reached_terminal = True
                elif payload.interpreted_type == PAYLOAD_SESSION_FAILED:
                    self._transition(session, "failed", "launcher", "event", event_id)
                    taken.reached_terminal = True
                elif payload.interpreted_type == PAYLOAD_STREAM_END:
                    taken.stream_end_event_id = event_id
            except StoreError as exc:
                if taken.refusal is None:
                    taken.refusal = exc
        return taken

    def _reject_unsupported_stream_end(self, payload, capabilities):
        if not capabilities.has_stream:
            raise LaunchBoundaryError(
                "launcher declares response_shape 'one_shot', which has no stream to "
                "end, but produced a payload typed %r" % (PAYLOAD_STREAM_END,))
        if payload.source != SOURCE_LAUNCHER:
            raise LaunchBoundaryError(
                "end-of-stream must be signalled by the launcher; this payload is "
                "sourced to %r" % (payload.source,))
        if payload.interpretation != INTERPRETATION_RECOGNIZED:
            raise LaunchBoundaryError(
                "the end-of-stream signal must be recognized; this payload is %r"
                % (payload.interpretation,))

    def _preserve(self, session, payload):
        """Preserve one payload verbatim (contract 7, P1).

        Preservation is unconditional and is especially required when
        interpretation failed, because those are the cases v0.1 exists to
        discover. Returns the event id, or None when this `(session_id,
        sequence)` was already stored with the same evidence -- `events` is
        at-least-once. A *different* payload at a stored sequence is not a
        replay, and the store refuses it (`StoreCorrupt`).
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
        record, created = self._store.append_diagnostic_event(
            chat_id, session_id, payload.sequence, payload.source,
            payload.interpretation, payload.interpreted_type, body, encoding=encoding)
        if not created:
            return None
        return record["event_id"]

    # -- user actions ------------------------------------------------------

    def stop_agent(self, chat_id, reason):
        """The user stops the agent. Only the user may (contract 5.2).

        An unconfirmed stop is not a dead end and is not a failure: it is an
        observation that leaves liveness undeterminable, which carries the
        session to `unknown`, from which the user can always abandon.
        """
        with self._turns.hold(chat_id):
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

        **A user action in flight is not changed.** It holds the chat's turn lock,
        so from another thread this waits for it to finish, as it always did; and
        a launcher calling back into the loop during an action on this chat is
        refused every exit but `unknown -> abandoned`, which it always had.
        """
        with self._turns.hold(chat_id) as held:
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
        """
        capabilities = self._boundary.capabilities
        taken = self._take_page(session, page, capabilities, [])
        if (taken.stream_end_event_id is not None and not taken.reached_terminal
                and session["state"] == "running"):
            self._transition(session, "unknown", "launcher", "stream_end",
                             taken.stream_end_event_id)
        if taken.refusal is not None:
            raise taken.refusal

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
