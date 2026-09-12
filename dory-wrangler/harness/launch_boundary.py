"""The launch boundary: contract section 6, as an interface.

This module is the only description of how the harness talks to whatever starts
an agent, and it is the one place a reviewer has to read to know that no
development mechanic can reach chat or session design.

**Portability rule (contract 6).** No operation, input, or output defined here
may contain host, transport, or bridge mechanics. What actually guarantees that
is not the denylist below: it is that the instruction packet is a *closed
schema*. A field name nobody thought to forbid fails exactly as firmly as one
that was. `_MECHANICS_FIELD_NAMES` only improves the diagnosis for the likeliest
mistakes, exactly as contract 6 says of the validator's denylist. Do not extend
it believing it is what holds the seam closed, and do not open the schema
believing it will catch what comes through.

Mechanics hidden inside the *value* of an allowed field are accepted by design:
`agent_handle` is opaque and `detail` is never parsed, so neither can influence
behaviour on this side of the seam.

**There is no `status`, `health`, `poll`, or `describe` operation, and there is
no timer.** Nothing here takes a timeout, a deadline, an interval, or a retry
count. `events` blocks until the launcher has something to report; an agent that
is merely quiet is unresolvable in v0.1 by design, and closing that is #83.
"""

from __future__ import annotations

# --- declared launcher capabilities (contract 4.3) -------------------------
#
# These are declared properties of an implementation. This package asserts none
# of them and assumes none of them: the chat and session model is the same under
# every combination, and that is what makes the seam swappable.

CONTINUATION_PERSISTENT = "persistent"
CONTINUATION_FRESH_BINDING = "fresh_binding"
CONTINUATION_MODES = (CONTINUATION_PERSISTENT, CONTINUATION_FRESH_BINDING)

RESPONSE_SHAPE_STREAM = "stream"
RESPONSE_SHAPE_ONE_SHOT = "one_shot"
RESPONSE_SHAPES = (RESPONSE_SHAPE_STREAM, RESPONSE_SHAPE_ONE_SHOT)

# --- launch outcomes and the closed failure categories (contract 6.3) ------

OUTCOME_ACCEPTED = "accepted"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"
LAUNCH_OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_FAILED, OUTCOME_UNKNOWN)

FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_UNAVAILABLE = "unavailable"
FAILURE_REJECTED = "rejected"
FAILURE_NO_ACKNOWLEDGEMENT = "no_acknowledgement"
FAILURE_INTERNAL_ERROR = "internal_error"
FAILURE_CATEGORIES = (
    FAILURE_INVALID_REQUEST,
    FAILURE_UNAVAILABLE,
    FAILURE_REJECTED,
    FAILURE_NO_ACKNOWLEDGEMENT,
    FAILURE_INTERNAL_ERROR,
)

# --- the payload vocabulary that crosses the seam --------------------------
#
# A launcher parses its own transport and reports what it recognised. These
# names are semantic, not mechanical: they say what happened to the agent, never
# how the launcher was reached. A launcher that sees something outside this
# vocabulary reports it as `unrecognized` or `malformed`, which is a finding
# rather than an error (contract 7, P2).

PAYLOAD_ASSISTANT_TEXT = "assistant_text"
PAYLOAD_TURN_COMPLETE = "turn_complete"
PAYLOAD_SESSION_COMPLETED = "session_completed"
PAYLOAD_SESSION_FAILED = "session_failed"
PAYLOAD_STREAM_END = "stream_end"

INTERPRETATION_RECOGNIZED = "recognized"
INTERPRETATION_UNRECOGNIZED = "unrecognized"
INTERPRETATION_MALFORMED = "malformed"
INTERPRETATIONS = (
    INTERPRETATION_RECOGNIZED,
    INTERPRETATION_UNRECOGNIZED,
    INTERPRETATION_MALFORMED,
)

SOURCE_LAUNCHER = "launcher"
SOURCE_AGENT = "agent"
# `harness` is deliberately absent. A launcher may not source an event to the
# harness: that would let the integration forge the harness's own observations,
# and contract 5.2 keeps those in `session_observation` precisely so the harness
# cannot authorise its own conclusions under the launcher's name.
PAYLOAD_SOURCES = (SOURCE_LAUNCHER, SOURCE_AGENT)


class LaunchBoundaryError(Exception):
    """A launcher misused the seam. Fail closed; never coerce (contract D3)."""


class LauncherError(Exception):
    """A launcher could not complete an operation, in an abstract category.

    `category` is one of FAILURE_CATEGORIES. The concrete cause -- an inactive
    user session, an uninitialised bridge -- stays inside the launcher and
    travels only in `detail`, which is never parsed.
    """

    def __init__(self, category, detail=None):
        if category not in FAILURE_CATEGORIES:
            raise LaunchBoundaryError(
                "failure category %r is not one of %s"
                % (category, "/".join(FAILURE_CATEGORIES))
            )
        Exception.__init__(self, "%s: %s" % (category, detail or ""))
        self.category = category
        self.detail = detail


def _require(condition, message):
    if not condition:
        raise LaunchBoundaryError(message)


class LauncherCapabilities(object):
    """What a launcher declares it can do (contract 4.3).

    `instruction_bound_bytes` is the largest instruction packet this launcher is
    *measured* to accept. `None` means nobody has measured one, which is its only
    honest value for every launcher in this package. Supplying a number here is
    a claim about a measurement; see facts-and-assumptions.md U2.
    """

    __slots__ = ("continuation", "response_shape", "instruction_bound_bytes")

    def __init__(self, continuation, response_shape, instruction_bound_bytes=None):
        _require(
            continuation in CONTINUATION_MODES,
            "continuation must be one of %s" % "/".join(CONTINUATION_MODES),
        )
        _require(
            response_shape in RESPONSE_SHAPES,
            "response_shape must be one of %s" % "/".join(RESPONSE_SHAPES),
        )
        _require(
            instruction_bound_bytes is None
            or (
                isinstance(instruction_bound_bytes, int)
                and not isinstance(instruction_bound_bytes, bool)
                and instruction_bound_bytes >= 1
            ),
            "instruction_bound_bytes must be None (not measured) or an integer >= 1",
        )
        self.continuation = continuation
        self.response_shape = response_shape
        self.instruction_bound_bytes = instruction_bound_bytes

    @property
    def supports_delivery(self):
        return self.continuation == CONTINUATION_PERSISTENT

    @property
    def has_stream(self):
        return self.response_shape == RESPONSE_SHAPE_STREAM

    def as_record(self):
        return {
            "continuation": self.continuation,
            "response_shape": self.response_shape,
            "instruction_bound_bytes": self.instruction_bound_bytes,
        }

    def __repr__(self):
        return "LauncherCapabilities(%s, %s, %r)" % (
            self.continuation,
            self.response_shape,
            self.instruction_bound_bytes,
        )


# Known mechanics names. This is a better error message and nothing more; the
# closed schema below is the rule. Kept identical to the validator's list so the
# two diagnoses agree -- a drift guard asserts that in the test suite.
_MECHANICS_FIELD_NAMES = frozenset(
    (
        "argv", "command", "container", "cwd", "display", "endpoint", "env",
        "environment", "extension_id", "host", "hostname", "path", "pid",
        "port", "script_path", "shell", "socket", "ssh", "terminal_id", "url",
        "vscode_workspace", "workspace_path", "working_directory",
    )
)


class _InstructionPacket(object):
    """Text plus correlation, and nothing else (contract 6.2, 6.4).

    The schema is closed in both directions: every declared field is required,
    and any other field is rejected whatever it is called. A launcher that needs
    configuration obtains it from its own environment, never from here.
    """

    FIELDS = ()

    def __init__(self, **fields):
        self._validate_names(fields)
        for name in self.FIELDS:
            _require(name in fields, "instruction packet is missing %r" % name)
        encoding = fields["instruction_encoding"]
        _require(
            encoding == "utf-8",
            "instruction_encoding is fixed at 'utf-8' in contract v0.1",
        )
        text = fields["instruction_text"]
        _require(
            isinstance(text, str) and len(text.encode("utf-8")) >= 1,
            "instruction_text must be a UTF-8 string of at least one byte",
        )
        for name in self.FIELDS:
            setattr(self, name, fields[name])

    @classmethod
    def _validate_names(cls, fields):
        for name in sorted(fields):
            if name in cls.FIELDS:
                continue
            if name in _MECHANICS_FIELD_NAMES:
                raise LaunchBoundaryError(
                    "field %r encodes host, transport, or bridge mechanics and may "
                    "not cross the launch boundary" % name
                )
            raise LaunchBoundaryError(
                "field %r is not part of the instruction packet; the packet is a "
                "closed schema of %s" % (name, ", ".join(cls.FIELDS))
            )

    @property
    def instruction_bytes(self):
        return len(self.instruction_text.encode("utf-8"))

    def as_record(self):
        return dict((name, getattr(self, name)) for name in self.FIELDS)

    def __repr__(self):
        return "%s(%d bytes of instruction)" % (
            type(self).__name__,
            self.instruction_bytes,
        )


class LaunchInstruction(_InstructionPacket):
    """The bounded instruction packet a `launch` call carries (contract 6.2)."""

    FIELDS = (
        "request_id",
        "chat_id",
        "session_id",
        "created_at",
        "instruction_encoding",
        "instruction_text",
    )


class DeliveryInstruction(_InstructionPacket):
    """The delivery packet a `deliver` call carries (contract 6.4).

    Bounded, closed, and portability-checked identically to the launch packet.
    The seam's guarantees are not weaker on turn four than on turn one, so this
    class deliberately shares every check with `LaunchInstruction`.
    """

    FIELDS = (
        "delivery_id",
        "chat_id",
        "session_id",
        "sequence",
        "created_at",
        "instruction_encoding",
        "instruction_text",
    )


class LaunchResult(object):
    """What a launch attempt produced (contract 6.3).

    The conditional-field rules are enforced here, at the seam, rather than only
    where the record is written: a launcher that reports `accepted` without a
    handle has not accepted anything, and this is where that stops.
    """

    __slots__ = ("outcome", "agent_handle", "failure_category", "detail")

    def __init__(self, outcome, agent_handle=None, failure_category=None, detail=None):
        _require(
            outcome in LAUNCH_OUTCOMES,
            "outcome must be one of %s" % "/".join(LAUNCH_OUTCOMES),
        )
        _require(
            detail is None or isinstance(detail, str),
            "detail must be a string or None",
        )
        if outcome == OUTCOME_ACCEPTED:
            _require(
                isinstance(agent_handle, str) and agent_handle != "",
                "an accepted launch must return a non-empty agent_handle; without "
                "one nothing can stop the agent or re-attach to it",
            )
            _require(
                failure_category is None,
                "an accepted launch must not carry a failure_category",
            )
        elif outcome == OUTCOME_FAILED:
            _require(
                failure_category in FAILURE_CATEGORIES,
                "a failed launch must name a category #90 can count, one of %s"
                % "/".join(FAILURE_CATEGORIES),
            )
            _require(
                agent_handle is None,
                "a failed launch must not carry an agent_handle",
            )
        else:
            _require(
                agent_handle is None and failure_category is None,
                "an 'unknown' outcome carries neither a handle nor a category; it "
                "is the absence of a usable answer, not a failure",
            )
        self.outcome = outcome
        self.agent_handle = agent_handle
        self.failure_category = failure_category
        self.detail = detail

    def __repr__(self):
        return "LaunchResult(%s, handle=%r, category=%r)" % (
            self.outcome,
            self.agent_handle,
            self.failure_category,
        )


class DeliveryAck(object):
    """A `deliver` call's acknowledgement (contract 6.4).

    `acknowledged` is True, False, or None when the launcher could not say.
    """

    __slots__ = ("acknowledged", "detail")

    def __init__(self, acknowledged, detail=None):
        _require(
            acknowledged is None or isinstance(acknowledged, bool),
            "acknowledged must be True, False, or None",
        )
        _require(
            detail is None or isinstance(detail, str),
            "detail must be a string or None",
        )
        self.acknowledged = acknowledged
        self.detail = detail


class StopAck(object):
    """A `stop` call's acknowledgement (contract 6.1).

    An unconfirmed stop is an observation, not a dead end: contract 5.3 cause 4
    carries the session to `unknown`, from which the user can always abandon.
    """

    __slots__ = ("confirmed", "detail")

    def __init__(self, confirmed, detail=None):
        _require(isinstance(confirmed, bool), "confirmed must be True or False")
        _require(
            detail is None or isinstance(detail, str),
            "detail must be a string or None",
        )
        self.confirmed = confirmed
        self.detail = detail


class EventPayload(object):
    """One raw payload from a session, as the launcher reports it (contract 4.5, 6.1).

    The launcher assigns `sequence`, because contract 6.1 makes `events`
    resumable *by* sequence: the caller resumes by passing the last sequence it
    stored, and the store treats a replayed `(session_id, sequence)` as already
    stored. A sequence the harness invented could not survive that.

    `raw` is bytes and is preserved verbatim, especially when interpretation
    failed (contract 7, P1). `text` is the user-visible text the launcher
    extracted from its own transport; it is honoured only for a recognized,
    agent-sourced `assistant_text` payload and ignored everywhere else, so a
    launcher cannot put words in the chat by attaching text to something else.
    """

    __slots__ = ("sequence", "source", "interpretation", "interpreted_type", "raw", "text")

    def __init__(self, sequence, source, interpretation, raw, interpreted_type=None, text=None):
        _require(
            isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 1,
            "payload sequence must be an integer >= 1",
        )
        _require(
            source in PAYLOAD_SOURCES,
            "payload source must be one of %s; a launcher may not source an event "
            "to the harness" % "/".join(PAYLOAD_SOURCES),
        )
        _require(
            interpretation in INTERPRETATIONS,
            "interpretation must be one of %s" % "/".join(INTERPRETATIONS),
        )
        _require(isinstance(raw, bytes), "raw payload must be bytes, preserved verbatim")
        if interpretation == INTERPRETATION_RECOGNIZED:
            _require(
                isinstance(interpreted_type, str) and interpreted_type != "",
                "a recognized payload must name its interpreted_type",
            )
        else:
            _require(
                interpreted_type is None,
                "interpreted_type must be None when interpretation is %r" % interpretation,
            )
        _require(text is None or isinstance(text, str), "text must be a string or None")
        self.sequence = sequence
        self.source = source
        self.interpretation = interpretation
        self.interpreted_type = interpreted_type
        self.raw = raw
        self.text = text

    @property
    def is_chat_text(self):
        """Whether this payload may become a user-visible agent message.

        All three conditions are load-bearing and are the seam's half of
        contract 4.2 / 7 P2a: the agent's own output, actually recognized, of the
        type that carries user-visible text. Launcher and harness output is
        diagnostic, never chat.
        """
        return (
            self.source == SOURCE_AGENT
            and self.interpretation == INTERPRETATION_RECOGNIZED
            and self.interpreted_type == PAYLOAD_ASSISTANT_TEXT
            and isinstance(self.text, str)
            and self.text != ""
        )

    def __repr__(self):
        return "EventPayload(#%d, %s, %s, %r)" % (
            self.sequence,
            self.source,
            self.interpretation,
            self.interpreted_type,
        )


class EventsPage(object):
    """What one `events` call returned (contract 6.1).

    `stream_ended` is the launcher signalling that its stream has ended, which
    is an observed fact rather than silence. A launcher declaring
    `response_shape: one_shot` has no stream and must never set it; the session
    manager rejects it if one does, and rejects a payload typed `stream_end`
    from such a launcher as well. Blocking only one of those two spellings would
    check the citation rather than the fact.

    A reader-side failure is **not** an end of stream: a launcher that cannot
    read raises `LauncherError`, which the harness records as a
    `stream_read_failed` observation.
    """

    __slots__ = ("payloads", "stream_ended")

    def __init__(self, payloads, stream_ended=False):
        payloads = list(payloads)
        for payload in payloads:
            _require(
                isinstance(payload, EventPayload),
                "events must return EventPayload objects; a launcher does not get "
                "to hand the store an arbitrary mapping",
            )
        _require(isinstance(stream_ended, bool), "stream_ended must be True or False")
        self.payloads = payloads
        self.stream_ended = stream_ended


class LaunchBoundary(object):
    """The interface contract section 6.1 specifies. Implementations live under
    `launchers/`; nothing in this module knows how an agent is started.

    Operations, and there are exactly these:

    ==============  ================================  ==============================
    `launch`        `LaunchInstruction`               `LaunchResult`
    `stop`          session id, reason string         `StopAck`, or `LauncherError`
    `events`        session id, after_sequence        `EventsPage`, or `LauncherError`
    `deliver`       session id, `DeliveryInstruction` `DeliveryAck`  -- **if declared**
    ==============  ================================  ==============================

    There is no `status`, `health`, `poll`, or `describe`. v0.1 learns what the
    agent is doing only from what the boundary reports.
    """

    launcher_id = None

    @property
    def capabilities(self):
        raise NotImplementedError

    def launch(self, instruction):
        raise NotImplementedError

    def stop(self, session_id, reason):
        raise NotImplementedError

    def events(self, session_id, after_sequence):
        raise NotImplementedError

    def deliver(self, session_id, instruction):
        """Send further text to an agent that is already running.

        Available **only** when the launcher declares
        `continuation: "persistent"`. A launcher declaring `fresh_binding` is
        not a degraded launcher; it is the other first-class mode, and under it
        a later turn is served by a new launch that the user's own turn opened.
        """
        raise LauncherError(
            FAILURE_INVALID_REQUEST,
            "this launcher declares continuation %r and cannot deliver to a "
            "running agent" % self.capabilities.continuation,
        )


# The launcher-id shape contract 4.3 fixes. It is opaque: no behaviour in this
# package branches on its value, and a drift guard in the test suite proves it.
LAUNCHER_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
