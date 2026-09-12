"""Chat turns, agent sessions, and bindings: contract sections 4, 5 and 6.

This module drives the chat loop. It holds a `LaunchBoundary` and never learns
anything else about it: it does not import a launcher, does not read a
`launcher_id` to decide anything, and contains no host, transport, or bridge
mechanics. Swapping launchers is a change to the object handed to the
constructor and to nothing else.

What is deliberately **not** here, per contract 1 and 5.3: no timer, no
inactivity inference, no liveness interrogation, no stall detection, no retry,
and no automatic recovery. Nothing in this file computes an elapsed duration or
compares one against a threshold. Every state change is caused by a user action,
a harness action, or an explicit launcher observation.
"""

from __future__ import annotations

import base64

from errors import ConcurrentLaunchRefused, InstructionTooLarge, NotPermitted
from identity import Clock, IdFactory
from launch_boundary import (
    DeliveryAck,
    DeliveryInstruction,
    EventsPage,
    LaunchBoundaryError,
    LaunchInstruction,
    LaunchResult,
    LauncherError,
    PAYLOAD_ASSISTANT_TEXT,
    PAYLOAD_SESSION_COMPLETED,
    PAYLOAD_SESSION_FAILED,
    PAYLOAD_STREAM_END,
    PAYLOAD_TURN_COMPLETE,
    FAILURE_INTERNAL_ERROR,
    INTERPRETATION_RECOGNIZED,
    OUTCOME_ACCEPTED,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    SOURCE_AGENT,
    SOURCE_LAUNCHER,
)
from store import TERMINAL_SESSION_STATES, Store

# Contract 5.2's owner table. It is repeated here because this module must fail
# closed before writing an unauthorized transition rather than rely on a
# validator run afterwards. A drift guard in the test suite asserts this is
# identical to the validator's table, since two copies of one table is exactly
# the divergence this contract exists to prevent.
AUTHORIZED_TRANSITIONS = {
    (None, "pending"): "user",
    ("pending", "launching"): "harness",
    ("pending", "launch_failed"): "harness",
    ("launching", "running"): "launcher",
    ("launching", "launch_failed"): "launcher",
    ("launching", "unknown"): "launcher",
    ("running", "completed"): "launcher",
    ("running", "failed"): "launcher",
    ("running", "terminated"): "user",
    ("running", "unknown"): "launcher",
    ("unknown", "running"): "launcher",
    ("unknown", "completed"): "launcher",
    ("unknown", "failed"): "launcher",
    ("unknown", "terminated"): "user",
    ("unknown", "abandoned"): "user",
}


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
    bound (U2) part of the architecture. #87 does not settle it, and a future
    composer is a change to this one function.

    `store` is accepted, unused, and named so that the decision is visible: the
    prior chat is available here and is deliberately not read.
    """
    return user_text


class SessionManager(object):
    """The chat loop, the lifecycle, and the one-agent-per-chat rule."""

    def __init__(self, store, boundary, ids=None, clock=None, compose=None):
        self._store = store
        self._boundary = boundary
        self._ids = ids or IdFactory()
        self._clock = clock or Clock()
        self._compose = compose or compose_launch_instruction

    # -- properties --------------------------------------------------------

    @property
    def store(self):
        return self._store

    @property
    def capabilities(self):
        return self._boundary.capabilities

    # -- chats -------------------------------------------------------------

    def create_chat(self, title):
        now = self._clock.now()
        chat_id = self._ids("cht")
        self._store.put({
            "record_type": "chat", "record_version": 1,
            "chat_id": chat_id, "title": title,
            "created_at": now, "updated_at": now, "state": "open",
        })
        return chat_id

    def transcript(self, chat_id):
        """The user-visible history, from durable records alone (contract D1, P4).

        Messages only. `diagnostic_event` records are never rendered here; they
        are reachable through `store.retrieve_diagnostics`, which is a bounded
        out-of-band retrieval that derives nothing.
        """
        return [
            (m["sequence"], m["author"], m["content"]["text"])
            for m in self._store.messages(chat_id)
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

        with self._store.chat_lock(chat_id):
            active = self._active_session(chat_id)
            if active is None:
                return self._launch_turn(chat_id, text)
            return self._continue_turn(chat_id, active, text)

    def _active_session(self, chat_id):
        """The chat's live agent, if it has one.

        This asks about **sessions**, not about bindings. A chat with a
        non-terminal session has an agent that may still be alive even if no
        binding holds it, and contract 4.4 is explicit that the rule bites on
        agents: without this, a released-too-early binding lets a second live
        agent speak into a chat while the ledger still reads as one-agent-per-
        chat. Open bindings are checked too, because either one alone leaves a
        route through.
        """
        non_terminal = self._store.non_terminal_sessions(chat_id)
        if non_terminal:
            return non_terminal[0]
        open_bindings = self._store.open_bindings(chat_id)
        if open_bindings:
            held = self._store.get("agent_session", open_bindings[0]["session_id"])
            if held is not None:
                return held
        return None

    def _refusal_reason(self, session):
        state = session["state"]
        mode = session["launcher_capabilities"]["continuation"]
        if state in TERMINAL_SESSION_STATES:
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

    def _continue_turn(self, chat_id, session, text):
        capabilities = session["launcher_capabilities"]
        if capabilities["continuation"] != "persistent" or session["state"] != "running":
            raise ConcurrentLaunchRefused(self._refusal_reason(session))
        return self._deliver_turn(chat_id, session, text)

    # -- launching ---------------------------------------------------------

    def _check_declared_bound(self, instruction_text):
        bound = self._boundary.capabilities.instruction_bound_bytes
        if bound is None:
            return  # nobody has measured one; this package asserts none
        size = len(instruction_text.encode("utf-8"))
        if size > bound:
            raise InstructionTooLarge(
                "instruction is %d bytes and this launcher declares a measured bound of "
                "%d; the packet is refused before it is recorded, because a packet that "
                "was never sent must not be stored as though it had been" % (size, bound)
            )

    def _launch_turn(self, chat_id, text):
        existing = self._active_session(chat_id)
        if existing is not None:
            raise ConcurrentLaunchRefused(self._refusal_reason(existing))
        instruction_text = self._compose(chat_id, text, self._store)
        # Pre-flight, before anything durable is written. A refused packet leaves
        # no session, no launch_request and no user message: nothing happened.
        self._check_declared_bound(instruction_text)

        user_message_id = self._append_message(chat_id, "user", text)
        session = self._open_session(chat_id, user_message_id)
        session_id = session["session_id"]

        request_id = self._ids("req")
        now = self._clock.now()
        try:
            instruction = LaunchInstruction(
                request_id=request_id,
                chat_id=chat_id,
                session_id=session_id,
                created_at=now,
                instruction_encoding="utf-8",
                instruction_text=instruction_text,
            )
        except LaunchBoundaryError as exc:
            # Nothing was launched, and no packet exists to point at.
            self._transition(session, "launch_failed", "harness", "harness_action", None)
            self._release_binding(session_id)
            raise NotPermitted("the instruction packet could not be formed: %s" % exc)

        packet = dict(instruction.as_record())
        packet.update({"record_type": "launch_request", "record_version": 1})
        self._store.put(packet)
        self._transition(session, "launching", "harness", "harness_action", request_id)

        result = self._call_launch(instruction)
        self._store.put({
            "record_type": "launch_result", "record_version": 1,
            "request_id": request_id, "session_id": session_id,
            "observed_at": self._clock.now(),
            "outcome": result.outcome,
            "agent_handle": result.agent_handle,
            "failure_category": result.failure_category,
            "detail": result.detail,
        })

        if result.outcome == OUTCOME_FAILED:
            self._transition(session, "launch_failed", "launcher", "launch_result", request_id)
            self._release_binding(session_id)
            return TurnOutcome(chat_id, session_id, user_message_id, [],
                               session["state"], result.outcome, result.failure_category)

        if result.outcome == OUTCOME_UNKNOWN:
            # Not a failure. `unknown` is a state, is non-terminal, and keeps its
            # binding open because the agent may still be out there. The user's
            # exit is `abandon`, and the harness never resolves it on its own.
            self._transition(session, "unknown", "launcher", "launch_result", request_id)
            return TurnOutcome(chat_id, session_id, user_message_id, [],
                               session["state"], result.outcome, None)

        session["agent_handle"] = result.agent_handle
        self._store.put(session)
        self._transition(session, "running", "launcher", "launch_result", request_id)
        agent_messages = self._drain(session)
        return TurnOutcome(chat_id, session_id, user_message_id, agent_messages,
                           session["state"], result.outcome, None)

    def _call_launch(self, instruction):
        """Call the launcher and classify whatever comes back.

        A launcher that raises anything at all still produces a classified,
        durable outcome: the five categories are the distinctions #90 must be
        able to count, so an unclassified crash becomes `internal_error` rather
        than a lost session or an invented success.
        """
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
        return result

    # -- delivering (persistent continuation) ------------------------------

    def _deliver_turn(self, chat_id, session, text):
        session_id = session["session_id"]
        self._check_declared_bound(text)
        user_message_id = self._append_message(chat_id, "user", text)

        delivery_id = self._ids("dlv")
        sequence = len(self._store.deliveries_of(session_id)) + 1
        instruction = DeliveryInstruction(
            delivery_id=delivery_id,
            chat_id=chat_id,
            session_id=session_id,
            sequence=sequence,
            created_at=self._clock.now(),
            instruction_encoding="utf-8",
            instruction_text=text,
        )
        # Recorded before the call, with `acknowledged: null`. A turn the harness
        # sent to an agent and did not record is a turn nobody can investigate,
        # and an acknowledgement we never received is unknown rather than false.
        packet = dict(instruction.as_record())
        packet.update({
            "record_type": "delivery_request", "record_version": 1,
            "acknowledged": None,
        })
        self._store.put(packet)

        try:
            ack = self._boundary.deliver(session_id, instruction)
        except LauncherError as exc:
            ack = DeliveryAck(None, detail="%s: %s" % (exc.category, exc.detail))
        if not isinstance(ack, DeliveryAck):
            ack = DeliveryAck(None, detail="launcher returned %r rather than a DeliveryAck"
                                           % (type(ack).__name__,))
        packet["acknowledged"] = ack.acknowledged
        self._store.put(packet)

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
        session_id = session["session_id"]
        capabilities = self._boundary.capabilities
        agent_messages = []

        while True:
            after = self._store.last_event_sequence(session_id)
            try:
                page = self._boundary.events(session_id, after)
            except LauncherError as exc:
                # A reader-side failure is not an end of stream. It says nothing
                # about whether the agent is alive (contract 4.7, 6.1).
                observation_id = self._record_observation(
                    session, "stream_read_failed",
                    "%s: %s" % (exc.category, exc.detail))
                if session["state"] in ("running", "launching"):
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

            turn_complete = False
            reached_terminal = False
            stream_end_event_id = None

            for payload in page.payloads:
                if payload.interpreted_type == PAYLOAD_STREAM_END:
                    self._reject_unsupported_stream_end(payload, capabilities)
                event_id = self._preserve(session, payload)
                if event_id is None:
                    continue  # a replayed (session_id, sequence); already stored
                if payload.is_chat_text:
                    agent_messages.append(
                        self._append_message(session["chat_id"], "agent", payload.text,
                                             session_id=session_id,
                                             source_event_id=event_id))
                if payload.interpretation != INTERPRETATION_RECOGNIZED:
                    continue
                if payload.interpreted_type == PAYLOAD_TURN_COMPLETE:
                    turn_complete = True
                elif payload.source != SOURCE_LAUNCHER:
                    continue
                elif payload.interpreted_type == PAYLOAD_SESSION_COMPLETED:
                    self._transition(session, "completed", "launcher", "event", event_id)
                    reached_terminal = True
                elif payload.interpreted_type == PAYLOAD_SESSION_FAILED:
                    self._transition(session, "failed", "launcher", "event", event_id)
                    reached_terminal = True
                elif payload.interpreted_type == PAYLOAD_STREAM_END:
                    stream_end_event_id = event_id

            if reached_terminal:
                self._release_binding(session_id)
                return agent_messages
            if stream_end_event_id is not None:
                # The stream closing is an observed fact, unlike silence on an
                # open stream (contract 5.3 cause 2).
                if session["state"] in ("running",):
                    self._transition(session, "unknown", "launcher", "stream_end",
                                     stream_end_event_id)
                return agent_messages
            if turn_complete or not page.payloads:
                return agent_messages

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
        sequence)` was already stored -- `events` is at-least-once.
        """
        try:
            body = payload.raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            body = base64.b64encode(payload.raw).decode("ascii")
            encoding = "base64"
        # Contiguity is the launcher's to assign and the harness's to refuse.
        # `events` is resumable by sequence, so a gap means a payload was lost
        # rather than that one is still coming, and contract 4.5 makes an ordered
        # history contiguous from 1. Failing closed here keeps a launcher with a
        # counting bug from writing a store nobody can read back in order.
        last = self._store.last_event_sequence(session["session_id"])
        if payload.sequence > last + 1:
            raise LaunchBoundaryError(
                "launcher returned payload sequence %d for session %s while %d is "
                "stored; an ordered history must be contiguous, and a gap means a "
                "payload was lost rather than that one is still coming"
                % (payload.sequence, session["session_id"], last))
        event_id = self._ids("evt")
        record = {
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": event_id,
            "chat_id": session["chat_id"],
            "session_id": session["session_id"],
            "sequence": payload.sequence,
            "received_at": self._clock.now(),
            "source": payload.source,
            "interpretation": payload.interpretation,
            "interpreted_type": payload.interpreted_type,
            "raw": {"encoding": encoding, "body": body},
        }
        if not self._store.append_event(record):
            return None
        return event_id

    # -- user actions ------------------------------------------------------

    def stop_agent(self, chat_id, reason):
        """The user stops the agent. Only the user may (contract 5.2).

        An unconfirmed stop is not a dead end and is not a failure: it is an
        observation that leaves liveness undeterminable, which carries the
        session to `unknown`, from which the user can always abandon.
        """
        with self._store.chat_lock(chat_id):
            session = self._active_session(chat_id)
            if session is None:
                raise NotPermitted("chat %s has no live agent to stop" % chat_id)
            if session["state"] not in ("running", "unknown"):
                raise NotPermitted(
                    "session %s is in state %r; only a running or unknown agent can be "
                    "stopped" % (session["session_id"], session["state"]))
            try:
                ack = self._boundary.stop(session["session_id"], reason)
            except LauncherError as exc:
                ack = None
                detail = "%s: %s" % (exc.category, exc.detail)
            else:
                detail = ack.detail
            confirmed = bool(ack is not None and ack.confirmed)
            observation_id = self._record_observation(
                session, "stop_confirmed" if confirmed else "stop_unconfirmed", detail)
            if confirmed:
                self._transition(session, "terminated", "user", "observation",
                                 observation_id)
                self._release_binding(session["session_id"])
            elif session["state"] == "running":
                self._transition(session, "unknown", "launcher", "observation",
                                 observation_id)
            return session["state"]

    def abandon(self, chat_id):
        """The user gives up on an `unknown` session (contract 5.1, 5.4).

        This is a decision, not an observation, and it is the reason the user is
        never stuck behind a silent agent. The harness never makes it.
        """
        with self._store.chat_lock(chat_id):
            session = self._active_session(chat_id)
            if session is None:
                raise NotPermitted("chat %s has no session to abandon" % chat_id)
            if session["state"] != "unknown":
                raise NotPermitted(
                    "session %s is in state %r; only an 'unknown' session may be "
                    "abandoned" % (session["session_id"], session["state"]))
            self._transition(session, "abandoned", "user", "user_action", None)
            self._release_binding(session["session_id"])
            return session["state"]

    # -- restart (contract 5.4) -------------------------------------------

    def reattach_on_start(self):
        """Re-attach to every non-terminal session, once, at start.

        D1 makes durable records canonical across a restart, so what happens to
        a live session at restart is v0.1's obligation: a durable record that
        survives into a state with no legal exit is not durable, it is a chat
        nobody can use. This happens when the harness starts and never on a
        schedule; everything after it is a user action.
        """
        outcomes = []
        for session in self._store.all_of("agent_session"):
            if session["state"] in TERMINAL_SESSION_STATES:
                continue
            outcomes.append((session["session_id"], self._reattach(session)))
        return outcomes

    def _reattach(self, session):
        state = session["state"]
        session_id = session["session_id"]

        if state == "pending":
            # No launch was ever issued, so there is no agent and nothing to
            # re-attach to. "We never got an agent" is precisely launch_failed.
            self._transition(session, "launch_failed", "harness", "harness_action", None)
            self._release_binding(session_id)
            return "launch_failed"

        if state == "launching":
            # Contract 5.4's second case. The launch outcome was never recorded,
            # so it cannot be recovered and there is no handle to address.
            observation_id = self._record_observation(
                session, "reattach_failed",
                "the launch outcome was not recorded before the restart, so there is no "
                "issued handle to re-attach through")
            self._transition(session, "unknown", "launcher", "observation", observation_id)
            return "unknown"

        try:
            self._boundary.events(session_id, self._store.last_event_sequence(session_id))
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
        return session["state"]

    # -- record plumbing ---------------------------------------------------

    def _append_message(self, chat_id, author, text, session_id=None, source_event_id=None):
        message_id = self._ids("msg")
        now = self._clock.now()
        self._store.append_message({
            "record_type": "message", "record_version": 1,
            "message_id": message_id, "chat_id": chat_id,
            "sequence": self._store.next_message_sequence(chat_id),
            "author": author, "created_at": now,
            "content": {"content_type": "text/plain", "text": text},
            "session_id": session_id, "source_event_id": source_event_id,
        })
        chat = self._store.get("chat", chat_id)
        if chat is not None:
            chat["updated_at"] = now
            self._store.put(chat)
        return message_id

    def _open_session(self, chat_id, opening_message_id):
        """Create the session and open its binding *before* the launch is attempted.

        Two reasons, and both matter. Contract 4.3: a launch failure must be
        attributable to a durable record rather than being lost. And the
        one-agent-per-chat rule is only real if the chat is claimed before the
        harness calls out: a launcher that re-enters `send_turn` during `launch`
        finds a non-terminal session already recorded and is refused, which a
        binding opened after the call would not do.
        """
        existing = self._active_session(chat_id)
        if existing is not None:
            raise ConcurrentLaunchRefused(self._refusal_reason(existing))

        now = self._clock.now()
        session_id = self._ids("ses")
        session = {
            "record_type": "agent_session", "record_version": 1,
            "session_id": session_id, "chat_id": chat_id, "created_at": now,
            "launcher_id": self._boundary.launcher_id,
            "launcher_capabilities": self._boundary.capabilities.as_record(),
            "state": "pending",
            "transitions": [{
                "from": None, "to": "pending", "owner": "user", "at": now,
                "evidence": {"kind": "user_action", "ref": opening_message_id},
            }],
        }
        self._store.put(session)
        self._store.put({
            "record_type": "agent_binding", "record_version": 1,
            "binding_id": self._ids("bnd"), "chat_id": chat_id,
            "session_id": session_id, "bound_at": now, "released_at": None,
        })
        return session

    def _transition(self, session, to, owner, evidence_kind, evidence_ref):
        frm = session["state"]
        expected = AUTHORIZED_TRANSITIONS.get((frm, to))
        if expected is None:
            raise NotPermitted("%s -> %s is not an authorized transition" % (frm, to))
        if expected != owner:
            raise NotPermitted(
                "%s -> %s is owned by %r, not %r" % (frm, to, expected, owner))
        session["transitions"].append({
            "from": frm, "to": to, "owner": owner, "at": self._clock.now(),
            "evidence": {"kind": evidence_kind, "ref": evidence_ref},
        })
        session["state"] = to
        self._store.put(session)

    def _release_binding(self, session_id):
        binding = self._store.binding_for_session(session_id)
        if binding is None or binding["released_at"] is not None:
            return
        binding["released_at"] = self._clock.now()
        self._store.put(binding)

    def _record_observation(self, session, kind, detail):
        observation_id = self._ids("obs")
        self._store.put({
            "record_type": "session_observation", "record_version": 1,
            "observation_id": observation_id,
            "chat_id": session["chat_id"], "session_id": session["session_id"],
            "observed_at": self._clock.now(), "kind": kind, "detail": detail,
        })
        return observation_id
