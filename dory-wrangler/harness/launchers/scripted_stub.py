"""A second, non-production launcher, selected only by configuration.

It exists so that swappability is proven rather than asserted: the whole chat
loop runs against this and against `dev-local` with nothing changed above the
seam but the configuration dictionary. It is also how every branch the internal
path might take gets exercised on a VM where the internal path is unreachable --
each of the five failure categories, an `unknown` launch outcome, an
unconfirmed stop, a failed and a successful re-attachment, a stream that ends
without a lifecycle event, and output that cannot be parsed.

It is **not** production, and nothing about it is a claim about the internal
bridge. Every capability combination it can declare is declared because the
stub can genuinely behave that way, not because the combination is believed to
exist internally.
"""

from __future__ import annotations

import json
import threading

from launch_boundary import (
    DeliveryAck,
    EventPayload,
    EventsPage,
    FAILURE_CATEGORIES,
    INTERPRETATION_MALFORMED,
    INTERPRETATION_RECOGNIZED,
    INTERPRETATION_UNRECOGNIZED,
    LaunchBoundary,
    LaunchResult,
    LauncherCapabilities,
    LauncherError,
    OUTCOME_ACCEPTED,
    OUTCOME_UNKNOWN,
    PAYLOAD_ASSISTANT_TEXT,
    PAYLOAD_SESSION_COMPLETED,
    PAYLOAD_SESSION_FAILED,
    PAYLOAD_STREAM_END,
    PAYLOAD_TURN_COMPLETE,
    SOURCE_AGENT,
    SOURCE_LAUNCHER,
    StopAck,
)

# What a turn's answer looks like. Identical to the development agent's, so that
# a transcript produced through this launcher and a transcript produced through
# a real local process can be compared turn for turn.
def answer(instruction):
    return "answer to: %s" % instruction.strip()


class _Session(object):
    def __init__(self, handle):
        self.handle = handle
        self.payloads = []
        self.next_sequence = 1
        self.stream_ended = False
        self.terminated = False


class ScriptedStubLauncher(LaunchBoundary):
    """Programmed, deterministic, in-process.

    Options:

    ``continuation``, ``response_shape``, ``instruction_bound_bytes``
        the capabilities this instance declares.
    ``launch_outcomes``
        a list consumed one per launch. Each item is ``"accepted"``,
        ``"unknown"``, a failure category name, or ``"raise:<category>"`` to
        raise instead of returning, or ``"crash"`` to fail in an unclassified
        way. Exhausted entries fall back to ``"accepted"``.
    ``stop_confirms``
        ``True`` (default), ``False`` for an unconfirmed stop, or ``"raise"``.
    ``end_of_turn``
        ``"turn_complete"`` (default), ``"session_completed"``,
        ``"session_failed"``, or ``"stream_end"`` for a stream that ends with no
        lifecycle event.
    ``garbage`` / ``unknown_type``
        also emit an unparseable payload, or one of a type this build does not
        know, before each answer.
    ``events_raise``
        raise on the next ``events`` call, so a reader-side failure can be
        distinguished from an end of stream.
    ``resume_sessions``
        session ids this instance can re-attach to, as a launcher with durable
        state of its own would after a harness restart.
    ``on_launch``
        a callable invoked inside ``launch``, for re-entrancy probes.
    """

    launcher_id = "scripted-stub"

    def __init__(self, options=None):
        options = dict(options or {})
        self._capabilities = LauncherCapabilities(
            options.get("continuation", "fresh_binding"),
            options.get("response_shape", "one_shot"),
            options.get("instruction_bound_bytes"),
        )
        self._launch_outcomes = list(options.get("launch_outcomes") or [])
        self._stop_confirms = options.get("stop_confirms", True)
        # None means "whatever this launcher's declared continuation implies":
        # a fresh_binding launcher's agent is done when the turn is, so its
        # session reaches a terminal state and releases its binding; a persistent
        # agent stays running for the next turn.
        self._end_of_turn = options.get("end_of_turn")
        self._garbage = bool(options.get("garbage"))
        self._unknown_type = bool(options.get("unknown_type"))
        self._events_raise = options.get("events_raise")
        self._on_launch = options.get("on_launch")
        self._sessions = {}
        for session_id in options.get("resume_sessions") or []:
            self._sessions[session_id] = _Session("stub-resumed-%s" % session_id)
        self._lock = threading.RLock()
        self._counter = 0
        # Observable facts a probe can check, so a claim about what this launcher
        # was asked to do never has to be taken on trust.
        self.launch_calls = []
        self.deliver_calls = []
        self.stop_calls = []

    @classmethod
    def from_options(cls, options):
        return cls(options)

    @property
    def capabilities(self):
        return self._capabilities

    # -- launch ------------------------------------------------------------

    def launch(self, instruction):
        self.launch_calls.append(instruction)
        if self._on_launch is not None:
            self._on_launch(instruction)

        outcome = self._launch_outcomes.pop(0) if self._launch_outcomes else "accepted"
        if outcome == "crash":
            raise RuntimeError("the stub failed in a way it does not classify")
        if isinstance(outcome, str) and outcome.startswith("raise:"):
            raise LauncherError(outcome.split(":", 1)[1], "the stub was told to raise")
        if outcome == OUTCOME_UNKNOWN:
            return LaunchResult(OUTCOME_UNKNOWN,
                                detail="the stub returned no usable acknowledgement")
        if outcome in FAILURE_CATEGORIES:
            return LaunchResult("failed", failure_category=outcome,
                                detail="the stub was told to fail as %r" % outcome)

        with self._lock:
            self._counter += 1
            handle = "stub-agent-%04d" % self._counter
        session = _Session(handle)
        with self._lock:
            self._sessions[instruction.session_id] = session
        self._produce_turn(session, instruction.instruction_text)
        return LaunchResult(OUTCOME_ACCEPTED, agent_handle=handle)

    # -- deliver -----------------------------------------------------------

    def deliver(self, session_id, instruction):
        if not self._capabilities.supports_delivery:
            # Not a fallback and not a degradation: this launcher declared
            # `fresh_binding`, so a later turn is served by a new launch that
            # the user's own turn opened.
            return LaunchBoundary.deliver(self, session_id, instruction)
        self.deliver_calls.append(instruction)
        session = self._session(session_id)
        self._produce_turn(session, instruction.instruction_text)
        return DeliveryAck(True)

    # -- events ------------------------------------------------------------

    def events(self, session_id, after_sequence):
        if self._events_raise:
            category = self._events_raise if self._events_raise in FAILURE_CATEGORIES \
                else "internal_error"
            self._events_raise = None
            raise LauncherError(category, "the stub could not read the stream")
        session = self._session(session_id)
        ready = [p for p in session.payloads if p.sequence > after_sequence]
        ended = session.stream_ended and self._capabilities.has_stream
        return EventsPage(ready, stream_ended=ended)

    # -- stop --------------------------------------------------------------

    def stop(self, session_id, reason):
        self.stop_calls.append((session_id, reason))
        self._session(session_id)
        if self._stop_confirms == "raise":
            raise LauncherError("internal_error", "the stub could not reach the agent")
        return StopAck(bool(self._stop_confirms),
                       detail="the stub was configured to %sconfirm"
                              % ("" if self._stop_confirms else "not "))

    # -- private -----------------------------------------------------------

    def _session(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise LauncherError(
                "unavailable",
                "this launcher has no record of session %s" % session_id)
        return session

    def _emit(self, session, source, interpretation, body, interpreted_type=None,
              text=None):
        payload = EventPayload(session.next_sequence, source, interpretation,
                               body if isinstance(body, bytes) else body.encode("utf-8"),
                               interpreted_type=interpreted_type, text=text)
        session.next_sequence += 1
        session.payloads.append(payload)
        return payload

    def _produce_turn(self, session, instruction_text):
        if self._garbage:
            self._emit(session, SOURCE_AGENT, INTERPRETATION_MALFORMED,
                       b"this is not json at all {{{")
        if self._unknown_type:
            self._emit(session, SOURCE_AGENT, INTERPRETATION_UNRECOGNIZED,
                       json.dumps({"type": "agent_thinking"}).encode("utf-8"))
        reply = answer(instruction_text)
        self._emit(session, SOURCE_AGENT, INTERPRETATION_RECOGNIZED,
                   json.dumps({"type": PAYLOAD_ASSISTANT_TEXT, "text": reply}),
                   interpreted_type=PAYLOAD_ASSISTANT_TEXT, text=reply)

        ending = self._end_of_turn
        if ending is None:
            ending = (PAYLOAD_TURN_COMPLETE if self._capabilities.supports_delivery
                      else PAYLOAD_SESSION_COMPLETED)

        # The turn boundary is always delimited, whatever else follows it.
        self._emit(session, SOURCE_AGENT, INTERPRETATION_RECOGNIZED,
                   json.dumps({"type": PAYLOAD_TURN_COMPLETE}),
                   interpreted_type=PAYLOAD_TURN_COMPLETE)

        if ending == PAYLOAD_TURN_COMPLETE:
            return
        if ending in (PAYLOAD_SESSION_COMPLETED, PAYLOAD_SESSION_FAILED):
            self._emit(session, SOURCE_LAUNCHER, INTERPRETATION_RECOGNIZED,
                       json.dumps({"type": ending}), interpreted_type=ending)
            session.stream_ended = True
        elif ending == PAYLOAD_STREAM_END:
            # A stream that ended with no terminal lifecycle event. The stream
            # *closing* is an observed fact; contract 5.3 cause 2 admits it, and
            # it carries the session to `unknown`.
            self._emit(session, SOURCE_LAUNCHER, INTERPRETATION_RECOGNIZED,
                       json.dumps({"type": PAYLOAD_STREAM_END}),
                       interpreted_type=PAYLOAD_STREAM_END)
            session.stream_ended = True
        else:
            raise ValueError("unknown end_of_turn %r" % (ending,))
