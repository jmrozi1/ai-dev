"""Manager-owned context lifecycle: the compactions this controller actually observed."""

from __future__ import annotations

# Decision D9 makes context lifecycle the manager's, and it turns on one number:
# how many times a persistent session has already been compacted. Reaching the
# configured threshold marks that session for graceful rotation later. This module
# is the number and the mark, and deliberately nothing after them -- nothing here
# terminates a context, generates a handoff, launches a replacement, or interrupts
# a turn.
#
# The number is only worth having if it is true, so almost everything below is
# about refusing to state one that is not.
#
# There is exactly one supported completed-compaction signal: a `SystemMessage`
# whose subtype is `compact_boundary`. `status: "compacting"` fires even when a
# compaction *fails*, `compact_progress` fires several times per compaction, and
# `cumulative_dropped_tokens` is a token total rather than a history -- so none of
# them is countable, and neither is a turn count, an elapsed time, a transcript
# size, or a context estimate. Counting anything cheaper than the boundary would
# produce a number that looks like evidence and is not.
#
# Identity comes from `message.data["session_id"]` and `message.data["uuid"]`, and
# from nowhere else. The SDK's `SystemMessage` carries no typed `session_id` or
# `uuid` attribute at all -- both read absent on a real boundary while the dict
# carried the identity -- so a decoder written in the shape of the `ResultMessage`
# reduction next door would count zero forever while every run looked healthy.
# `decode_lifecycle_event` therefore reads the mapping and never an attribute for
# identity, and a boundary that arrives without a trustworthy pair is reported as
# unidentifiable rather than counted.
#
# Three health states stay distinguishable because a count means three different
# things depending on where observation began. Observed from the session's own
# start, zero truthfully means zero. Observed from partway through a session whose
# earlier history cannot be proven, zero means *unknown* -- exact resume does not
# replay prior boundaries, so nothing can recover that history, and rendering it as
# 0 would be a fabrication. And once an event arrives that cannot be trusted, the
# observed number is a floor rather than a count and must not be presented as
# complete. So `count` is present only when observation is complete from the start;
# `observed` is always the honest floor; and they are never the same field.
#
# The rotation threshold is stated here and shares nothing with the concurrency
# ceiling. Both default to six and the identical number is a coincidence: D6 makes
# six a human-owned concurrency policy and D9 makes six a human-owned rotation
# policy, and moving one must not move the other. This module imports nothing from
# `authorization` for exactly that reason.
#
# Finally, this is manager lifecycle state and stays that shape. Dedup memory is
# the identity pairs of one live session and dies with it; no transcript, no event
# log, no durable history, and nothing here reaches control-plane state.

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .session_binding import ROLE_EXECUTOR


__all__ = [
    "COMPACT_BOUNDARY_SUBTYPE",
    "CONTEXT_POLICY_FRESH",
    "CONTEXT_POLICY_PERSISTENT",
    "ContextLifecycleError",
    "ContextLifecycleLedger",
    "ContextReading",
    "DEFAULT_ROTATION_THRESHOLD",
    "EVENT_COMPACTION_OBSERVED",
    "EVENT_COMPACTION_UNIDENTIFIABLE",
    "OBSERVATION_HEALTHY",
    "OBSERVATION_UNAVAILABLE",
    "OBSERVATION_UNHEALTHY",
    "SessionContextLifecycle",
    "context_policy_for_role",
    "decode_lifecycle_event",
    "resolve_rotation_threshold",
]


# The only completed-compaction signal, and the only message class carrying it.
SYSTEM_MESSAGE_TYPE_NAME = "SystemMessage"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

# What a decoded provider message becomes on the wire to the controller.
EVENT_COMPACTION_OBSERVED = "compaction-observed"
EVENT_COMPACTION_UNIDENTIFIABLE = "compaction-unidentifiable"
LIFECYCLE_EVENT_KINDS = (EVENT_COMPACTION_OBSERVED, EVENT_COMPACTION_UNIDENTIFIABLE)

# The three readings a count can have. They are separate values rather than a
# boolean plus a number because "zero" and "unknown" are different answers.
OBSERVATION_HEALTHY = "healthy-complete-from-session-start"
OBSERVATION_UNAVAILABLE = "unavailable-prior-history-unknown"
OBSERVATION_UNHEALTHY = "unhealthy-partial"
OBSERVATION_STATES = (OBSERVATION_HEALTHY, OBSERVATION_UNAVAILABLE, OBSERVATION_UNHEALTHY)

# D10's role policy, restated as the one fact this module needs from it.
CONTEXT_POLICY_PERSISTENT = "persistent"
CONTEXT_POLICY_FRESH = "fresh"

# D9's human-owned rotation policy. Six here and six in `authorization` are two
# separate human decisions that happen to agree; nothing imports one from the
# other, and a test proves neither moves the other.
DEFAULT_ROTATION_THRESHOLD = 6


class ContextLifecycleError(Exception):
    """A fail-closed context-lifecycle refusal carrying one machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


REASON_INVALID_THRESHOLD = "invalid-rotation-threshold"
REASON_UNSUPPORTED_EVENT = "unsupported-lifecycle-event"
REASON_SESSION_MISMATCH = "lifecycle-session-mismatch"
REASON_UNKNOWN_CONTEXT = "unknown-session-context"


def _text(value: Any) -> Optional[str]:
    """A non-empty string, or nothing. Blank is absent, not a valid identity."""
    return value if isinstance(value, str) and value.strip() else None


def resolve_rotation_threshold(value: Any = None) -> int:
    """The stated rotation threshold, or D9's default. Never derived from a ceiling."""
    if value is None:
        return DEFAULT_ROTATION_THRESHOLD
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContextLifecycleError(
            REASON_INVALID_THRESHOLD,
            "a rotation threshold must be a positive integer; got {0!r}.".format(value),
        )
    return value


def context_policy_for_role(role: Any) -> str:
    """D10's policy for one role: executors persist, everyone else runs fresh."""
    return CONTEXT_POLICY_PERSISTENT if role == ROLE_EXECUTOR else CONTEXT_POLICY_FRESH


# ---------------------------------------------------------------------------
# Decoding: one provider message -> one structured lifecycle event
# ---------------------------------------------------------------------------


def decode_lifecycle_event(message: Any) -> Optional[Dict[str, str]]:
    """Decode one provider message into a lifecycle event, or `None` for the rest.

    Total by construction: it returns an event or nothing and never raises, because
    it runs inside the worker's provider loop and a decoder that could kill that
    loop would cost a turn to observe a count.

    Identity is read out of `data` and out of nothing else. That is the whole point
    of this function existing separately from the `ResultMessage` reduction beside
    it: `ResultMessage` really does carry `session_id` as an attribute, `SystemMessage`
    genuinely does not, and a decoder that reached for the attribute out of symmetry
    would silently observe nothing at all.

    A boundary that arrives without a trustworthy `(session_id, uuid)` pair becomes
    an *unidentifiable* event rather than a countable one. It is reported precisely
    so the session it arrived on can stop presenting its number as complete -- being
    more permissive than the provider, which drops boundaries carrying no compaction
    metadata, would mean inventing an identity to count against.
    """
    if type(message).__name__ != SYSTEM_MESSAGE_TYPE_NAME:
        return None
    data = getattr(message, "data", None)
    subtype = _text(getattr(message, "subtype", None))
    if subtype is None and isinstance(data, Mapping):
        subtype = _text(data.get("subtype"))
    # `status` (including `status: "compacting"`, which fires even when a compaction
    # fails) and `compact_progress` leave here as `None`, which is the whole reason
    # the comparison is against one exact subtype rather than a family of them.
    if subtype != COMPACT_BOUNDARY_SUBTYPE:
        return None
    if not isinstance(data, Mapping):
        return {
            "event": EVENT_COMPACTION_UNIDENTIFIABLE,
            "detail": "a compact_boundary arrived carrying no data mapping to identify it.",
        }
    session_id = _text(data.get("session_id"))
    identity = _text(data.get("uuid"))
    if session_id is None or identity is None:
        return {
            "event": EVENT_COMPACTION_UNIDENTIFIABLE,
            "detail": (
                "a compact_boundary arrived without a trustworthy "
                "(session_id, uuid) identity in its data."
            ),
        }
    # `trigger` and `pre_tokens` travel no further: they are metadata about a
    # compaction, not identity, and this protocol carries identity.
    return {
        "event": EVENT_COMPACTION_OBSERVED,
        "session_id": session_id,
        "uuid": identity,
    }


# ---------------------------------------------------------------------------
# State: what one session's count means
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextReading:
    """One session's compaction state, said in a way that cannot overstate itself.

    `count` and `observed` are separate fields on purpose. `observed` is how many
    unique boundaries this controller itself saw, which is always a truthful floor.
    `count` is the session's total, and it exists only when observation is complete
    from the session's start -- everywhere else it is `None`, because there is no
    honest total to print and `0` would be a claim nobody can make.

    `rotation_marked` is three-valued for the same reason. `True` is a mark, `False`
    is a proof the threshold has not been reached, and `None` is the case where the
    observed floor is under the threshold but the history is not complete: the
    threshold may or may not have been reached, and pretending it was not would be
    exactly the fabrication this whole module refuses.
    """

    session_id: str
    role: str
    context_policy: str
    health: str
    observed: int
    count: Optional[int]
    threshold: int
    rotation_marked: Optional[bool]
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "role": self.role,
            "contextPolicy": self.context_policy,
            "health": self.health,
            "observed": self.observed,
            "count": self.count,
            "threshold": self.threshold,
            "rotationMarked": self.rotation_marked,
            "detail": self.detail,
        }


class SessionContextLifecycle:
    """One session's observed-compaction state. Manager lifecycle, nothing durable.

    `observed_from_start` is the only thing that separates a truthful zero from an
    unknown history, so it is stated by the one caller that can prove it -- the
    launch that minted the session id, reserved it before any process existed, and
    started the process itself -- and defaults to false everywhere else. Observation
    that began partway through a session cannot recover what it missed: exact resume
    does not replay prior boundaries, and no token total is a history.
    """

    def __init__(
        self,
        session_id: str,
        *,
        role: str,
        observed_from_start: bool = False,
        threshold: Any = None,
    ) -> None:
        self.session_id = session_id
        self.role = role
        self.context_policy = context_policy_for_role(role)
        self.threshold = resolve_rotation_threshold(threshold)
        self._observed_from_start = bool(observed_from_start)
        self._partial_detail = ""
        # Exactly the identity pairs of this one live session. It is bounded to the
        # session lifecycle and dropped with it, because six is the number this has
        # to survive to -- not because an event log was trimmed.
        self._seen = set()

    @property
    def health(self) -> str:
        if self._partial_detail:
            return OBSERVATION_UNHEALTHY
        if not self._observed_from_start:
            return OBSERVATION_UNAVAILABLE
        return OBSERVATION_HEALTHY

    @property
    def observed(self) -> int:
        """Unique boundaries this controller saw. A floor, never presented as a total."""
        return len(self._seen)

    @property
    def complete(self) -> bool:
        return self.health == OBSERVATION_HEALTHY

    def identities(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(self._seen))

    def mark_partial(self, detail: str) -> None:
        """Record that this session's observation can no longer be called complete.

        One way only: partial never heals. A later well-formed boundary says nothing
        about the one that could not be trusted, so re-establishing completeness
        would be re-asserting a history nobody re-observed.
        """
        if not self._partial_detail:
            self._partial_detail = detail

    def observe(self, event: Any) -> bool:
        """Fold one decoded lifecycle event in. Returns whether it was a new compaction.

        Fails closed on an event naming another session rather than counting it here.
        A compaction belongs to exactly the session its boundary names, and this
        object is the wrong place to decide that some other session plausibly meant
        this one.
        """
        if not isinstance(event, Mapping):
            raise ContextLifecycleError(
                REASON_UNSUPPORTED_EVENT,
                "a lifecycle event must be a mapping; got {0!r}.".format(type(event).__name__),
            )
        kind = event.get("event")
        if kind == EVENT_COMPACTION_UNIDENTIFIABLE:
            self.mark_partial(
                _text(event.get("detail"))
                or "a compact_boundary arrived without a trustworthy identity."
            )
            return False
        if kind != EVENT_COMPACTION_OBSERVED:
            raise ContextLifecycleError(
                REASON_UNSUPPORTED_EVENT,
                "{0!r} is not a supported lifecycle event.".format(kind),
            )
        session_id = _text(event.get("session_id"))
        identity = _text(event.get("uuid"))
        if session_id is None or identity is None:
            # Defence in depth: the decoder already refuses this shape, and an event
            # that reached here without an identity is a transport this session can
            # no longer call complete.
            self.mark_partial(
                "a compaction event arrived without a trustworthy (session_id, uuid) identity."
            )
            return False
        if session_id != self.session_id:
            raise ContextLifecycleError(
                REASON_SESSION_MISMATCH,
                "a compaction for session {0} cannot be counted against session {1}.".format(
                    session_id, self.session_id
                ),
            )
        pair = (session_id, identity)
        if pair in self._seen:
            # One unique (session_id, uuid) is one compaction however many times it
            # is observed. The pair is the key rather than the uuid alone, so the
            # same uuid under another session could not collide here even if the
            # mismatch refusal above were somehow bypassed.
            return False
        self._seen.add(pair)
        return True

    def reading(self) -> ContextReading:
        """What this controller may honestly say about this session right now."""
        health = self.health
        observed = self.observed
        complete = health == OBSERVATION_HEALTHY
        if observed >= self.threshold:
            # The observed number is a floor, so reaching the threshold is provable
            # even when the history is not complete -- and a mark is exactly what D9
            # says happens then. Nothing is terminated, replaced, or interrupted.
            marked = True
            detail = (
                "{0} observed compactions reach the rotation threshold of {1}; this "
                "session is marked for graceful rotation at a later safe boundary."
            ).format(observed, self.threshold)
        elif complete:
            marked = False
            detail = "{0} of {1} observed compactions, counted from this session's start.".format(
                observed, self.threshold
            )
        else:
            marked = None
            detail = (
                "{0} observed compactions is a floor rather than a count ({1}), so "
                "whether the rotation threshold of {2} was reached is undetermined."
            ).format(
                observed,
                self._partial_detail
                or "this session's earlier compaction history cannot be proven",
                self.threshold,
            )
        return ContextReading(
            session_id=self.session_id,
            role=self.role,
            context_policy=self.context_policy,
            health=health,
            observed=observed,
            count=observed if complete else None,
            threshold=self.threshold,
            rotation_marked=marked,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# The ledger a controller holds
# ---------------------------------------------------------------------------


class ContextLifecycleLedger:
    """Compaction state for the sessions one controller currently holds.

    Deliberately as non-durable as the registry it lives beside. A count that
    survived restart would be a claim about a session this process can no longer
    prove anything about, and the honest reading after restart is that the history
    is unavailable -- which is what a controller that never began observing gets.
    """

    def __init__(self, *, threshold: Any = None) -> None:
        self._threshold = resolve_rotation_threshold(threshold)
        self._contexts = {}

    @property
    def rotation_threshold(self) -> int:
        return self._threshold

    def begin(
        self, session_id: str, *, role: str, observed_from_start: bool = False
    ) -> SessionContextLifecycle:
        """Start observing one session, or return the observation already running.

        Re-taking ownership of a session never resets its count and never upgrades
        its history to complete. Both would discard something observed in favour of
        something merely re-asserted.
        """
        existing = self._contexts.get(session_id)
        if existing is not None:
            return existing
        context = SessionContextLifecycle(
            session_id,
            role=role,
            observed_from_start=observed_from_start,
            threshold=self._threshold,
        )
        self._contexts[session_id] = context
        return context

    def get(self, session_id: str) -> Optional[SessionContextLifecycle]:
        return self._contexts.get(session_id)

    def forget(self, session_id: str) -> None:
        """Drop one session's state with the session. This is the whole dedup bound."""
        self._contexts.pop(session_id, None)

    def observe(
        self, session_id: str, events: Optional[Iterable] = None
    ) -> SessionContextLifecycle:
        """Fold one invocation's events into exactly the session they arrived for."""
        context = self._contexts.get(session_id)
        if context is None:
            raise ContextLifecycleError(
                REASON_UNKNOWN_CONTEXT,
                "this controller observes no context for session {0}, so a compaction "
                "has no session it may be counted against.".format(session_id),
            )
        for event in events or ():
            try:
                context.observe(event)
            except ContextLifecycleError as exc:
                if exc.reason != REASON_SESSION_MISMATCH:
                    raise
                # An event naming another session arrived on this session's channel.
                # Nothing is incremented -- not here, and not on the session it
                # names, which this channel is no evidence about. What is recorded
                # is that this session's observation is no longer complete.
                context.mark_partial(exc.detail)
        return context

    def readings(self) -> Dict[str, Dict[str, Any]]:
        return {
            session_id: self._contexts[session_id].reading().to_dict()
            for session_id in sorted(self._contexts)
        }

    def rotation_marked_session_ids(self) -> Tuple[str, ...]:
        return tuple(
            session_id
            for session_id in sorted(self._contexts)
            if self._contexts[session_id].reading().rotation_marked is True
        )
