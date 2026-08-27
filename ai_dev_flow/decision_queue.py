"""The dominant screen's data: genuine human decisions first, everything else optional."""

from __future__ import annotations

# Checkpoint 4 makes the human's decision queue the product. This module is the
# data behind that screen and nothing else -- no store, no parser, no transport,
# no rendering. It takes records that are already durable facts and returns the
# rows and selected detail a view would draw.
#
# Four boundaries hold it honest.
#
# First, Waiting has exactly one source: an explicit pending human-decision
# record that an orchestrator published. It is never inferred. A blocked rail, a
# missing process handle, an error string, a long elapsed time, or an
# explanation that reads like a question are all things that have historically
# been mistaken for "the human is needed", and every one of them would manufacture
# an interruption nobody asked for. An operational input that arrives already
# claiming Waiting is refused rather than trusted -- including one the accepted
# lifecycle itself projected that way, because that projection answers a
# different question than "is there a decision for a person to make".
#
# Second, a row is deliberately poor. Identity, state, title, project/ticket, and
# elapsed seconds. No explanation, no evidence, no lifecycle reason or detail, no
# session id, no status type. A dense list stops being dense the moment a row can
# carry one more useful thing, and detail has a place: the selected item.
#
# Third, elapsed seconds arrive already derived. This module never reads a clock,
# so two calls on the same records always produce the same list, and age can
# never become a trigger. Age orders the list and is displayed; it authorizes
# nothing, escalates nothing, and filters nothing.
#
# Fourth, this module is pure. It touches no file, no Git, no network, no
# process, no provider, and no control-plane artifact. Everything it knows was
# handed to it.

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from .session_lifecycle import (
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    SessionProjection,
)

__all__ = [
    "DEFAULT_FILTERS",
    "DecisionQueue",
    "EvidenceReference",
    "KIND_AGENT",
    "KIND_DECISION",
    "OperationalAgent",
    "PendingDecision",
    "QUEUE_STATES",
    "QueueError",
    "QueueRow",
    "QueueView",
    "SelectedDetail",
    "build_queue",
]

# The three states this screen knows, reusing the accepted lifecycle vocabulary
# rather than inventing a second one.
QUEUE_STATES = (STATE_WAITING, STATE_RUNNING, STATE_DISCONNECTED)

# Only states an operational input may carry. Waiting is absent on purpose.
OPERATIONAL_STATES = (STATE_RUNNING, STATE_DISCONNECTED)

# What the screen shows before anyone touches a control. Genuine decisions only.
DEFAULT_FILTERS = (STATE_WAITING,)

# Bounds. Evidence is required to be bounded; the text bounds exist so a
# transcript, a log dump, or a pasted response cannot arrive wearing the
# explanation field as a disguise.
MAX_EVIDENCE_REFERENCES = 8
MAX_LABEL = 80
MAX_LOCATOR = 200
MAX_TITLE = 120
MAX_EXPLANATION = 2000

# Stable refusal reasons.
REASON_INVALID_TEXT = "invalid-text"
REASON_TEXT_TOO_LONG = "text-too-long"
REASON_INVALID_ELAPSED = "invalid-elapsed"
REASON_UNSUPPORTED_STATE = "unsupported-state"
REASON_OPERATIONAL_WAITING = "operational-cannot-wait"
REASON_INVALID_PROJECTION = "invalid-projection"
REASON_FACT_MISMATCH = "fact-mismatch"
REASON_TOO_MUCH_EVIDENCE = "evidence-unbounded"
REASON_INVALID_EVIDENCE = "invalid-evidence"
REASON_DUPLICATE_ITEM = "duplicate-item-identity"
REASON_INVALID_FILTER = "invalid-filter"


class QueueError(Exception):
    """A refusal to project, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


def _text(value: object, *, label: str, limit: int) -> str:
    """Exactly a non-empty `str` within its bound.

    `type(value) is str` rather than `isinstance`: a subclass compares equal and
    formats identically while carrying whatever else its author attached, and a
    projection that refuses provider payloads cannot accept a value it has not
    actually seen the shape of.
    """
    if type(value) is not str or not value.strip():
        raise QueueError(
            REASON_INVALID_TEXT, "{0} must be exact non-empty text, got {1!r}".format(label, value)
        )
    if len(value) > limit:
        raise QueueError(
            REASON_TEXT_TOO_LONG,
            "{0} is {1} characters; this projection carries at most {2}".format(
                label, len(value), limit
            ),
        )
    return value


def _elapsed(value: object, *, label: str = "elapsed_seconds") -> int:
    """A non-negative whole number of seconds someone else already derived."""
    if type(value) is not int:
        # `bool` is an `int` subclass; `True` seconds is not a duration.
        raise QueueError(
            REASON_INVALID_ELAPSED, "{0} must be an exact int, got {1!r}".format(label, value)
        )
    if value < 0:
        raise QueueError(
            REASON_INVALID_ELAPSED, "{0} cannot be negative, got {1}".format(label, value)
        )
    return value


# The two item kinds. They are the first encoded component, so a decision and an
# operational agent can never produce the same identity even when every other
# durable fact matches.
KIND_DECISION = "decision"
KIND_AGENT = "agent"


def _identity(kind: str, *parts: str) -> str:
    """A length-delimited encoding of one item's complete durable routing scope.

    Joining components with a separator is not injective: ("a|b", "c") and
    ("a", "b|c") produce the same string. Rail slugs and decision identifiers are
    not proven unique across projects and tickets, so two legitimate items could
    then land on one identity -- refused as a duplicate, or worse, silently
    selecting and later routing to the wrong one. Prefixing each component with
    its length makes the result decodable, and a decodable encoding cannot
    collide.

    Only durable routing facts are encoded. Session id, elapsed time, state,
    title, explanation, evidence, and lifecycle detail are all excluded: identity
    must survive every one of them changing.
    """
    return "|".join("{0}:{1}".format(len(part), part) for part in (kind,) + parts)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceReference:
    """A pointer to evidence, never the evidence itself."""

    label: str
    locator: str

    def __post_init__(self) -> None:
        _text(self.label, label="evidence label", limit=MAX_LABEL)
        _text(self.locator, label="evidence locator", limit=MAX_LOCATOR)


@dataclass(frozen=True)
class PendingDecision:
    """One genuine human decision an orchestrator published. The only source of Waiting."""

    decision_id: str
    project: str
    ticket: str
    rail: str
    raised_at: str
    title: str
    explanation: str
    elapsed_seconds: int
    evidence: Tuple[EvidenceReference, ...] = ()

    def __post_init__(self) -> None:
        _text(self.decision_id, label="decision_id", limit=MAX_LABEL)
        _text(self.project, label="project", limit=MAX_LABEL)
        _text(self.ticket, label="ticket", limit=MAX_LABEL)
        _text(self.rail, label="rail", limit=MAX_LOCATOR)
        # An opaque identity for when this was raised, carried for later routing
        # and detail. Ordering deliberately does not read it: the rail's rule is
        # elapsed seconds descending, and interpreting a timestamp here would be
        # this module deriving an age it was told not to compute.
        _text(self.raised_at, label="raised_at", limit=MAX_LABEL)
        _text(self.title, label="title", limit=MAX_TITLE)
        _text(self.explanation, label="explanation", limit=MAX_EXPLANATION)
        _elapsed(self.elapsed_seconds)
        if type(self.evidence) is not tuple:
            raise QueueError(
                REASON_INVALID_EVIDENCE,
                "evidence must be a tuple of EvidenceReference, got {0!r}".format(
                    type(self.evidence).__name__
                ),
            )
        if len(self.evidence) > MAX_EVIDENCE_REFERENCES:
            raise QueueError(
                REASON_TOO_MUCH_EVIDENCE,
                "{0} evidence references exceeds the bound of {1}".format(
                    len(self.evidence), MAX_EVIDENCE_REFERENCES
                ),
            )
        for entry in self.evidence:
            if type(entry) is not EvidenceReference:
                raise QueueError(
                    REASON_INVALID_EVIDENCE,
                    "evidence entries must be EvidenceReference, got {0!r}".format(
                        type(entry).__name__
                    ),
                )

    @property
    def item_id(self) -> str:
        return _identity(KIND_DECISION, self.project, self.ticket, self.rail, self.decision_id)

    @property
    def state(self) -> str:
        return STATE_WAITING


@dataclass(frozen=True)
class OperationalAgent:
    """Work in progress. Visible only when someone asks for it, and never Waiting."""

    project: str
    ticket: str
    rail: str
    title: str
    projection: SessionProjection

    def __post_init__(self) -> None:
        _text(self.project, label="project", limit=MAX_LABEL)
        _text(self.ticket, label="ticket", limit=MAX_LABEL)
        _text(self.rail, label="rail", limit=MAX_LOCATOR)
        _text(self.title, label="title", limit=MAX_TITLE)
        if type(self.projection) is not SessionProjection:
            raise QueueError(
                REASON_INVALID_PROJECTION,
                "an operational input carries the accepted SessionProjection, got {0!r}".format(
                    type(self.projection).__name__
                ),
            )
        state = self.projection.state
        if state == STATE_WAITING:
            # The lifecycle projects Waiting for a blocked rail that records a
            # pending decision. That answers "is this session progressing", not
            # "is there a decision for a person to make". Only a published
            # decision record answers the second one.
            raise QueueError(
                REASON_OPERATIONAL_WAITING,
                "an operational input may not claim '{0}'; Waiting comes only from a "
                "published pending decision".format(STATE_WAITING),
            )
        if state not in OPERATIONAL_STATES:
            raise QueueError(
                REASON_UNSUPPORTED_STATE,
                "operational state '{0}' is not one of {1}".format(
                    state, ", ".join(OPERATIONAL_STATES)
                ),
            )
        if self.projection.rail != self.rail:
            raise QueueError(
                REASON_FACT_MISMATCH,
                "input names rail '{0}'; its session projection names '{1}'".format(
                    self.rail, self.projection.rail
                ),
            )
        _elapsed(self.projection.elapsed_seconds, label="projection elapsed_seconds")

    @property
    def item_id(self) -> str:
        # The rail, never the session id: a row must be routable without carrying
        # provider session identity into the list. Project and ticket are part of
        # the identity because a rail slug is only unique within its own scope.
        return _identity(KIND_AGENT, self.project, self.ticket, self.rail)

    @property
    def state(self) -> str:
        return self.projection.state

    @property
    def elapsed_seconds(self) -> int:
        return self.projection.elapsed_seconds


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueRow:
    """Everything a dense row may show, and there is no field for anything else."""

    item_id: str
    state: str
    title: str
    project: str
    ticket: str
    elapsed_seconds: int


@dataclass(frozen=True)
class SelectedDetail:
    """The right pane's data. No title -- the row already said it."""

    item_id: str
    state: str
    explanation: Optional[str] = None
    evidence: Tuple[EvidenceReference, ...] = ()


@dataclass(frozen=True)
class QueueView:
    """One rendering's worth of state: what is visible, under which filters, and what is selected."""

    rows: Tuple[QueueRow, ...]
    filters: Tuple[str, ...]
    selected_id: Optional[str] = None
    detail: Optional[SelectedDetail] = None


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def _sort_key(entry) -> Tuple[int, str]:
    """Oldest first: the largest already-derived age wins, identity breaks ties.

    Deterministic by construction -- no clock is read, so the same records always
    produce the same order.
    """
    return (-entry.elapsed_seconds, entry.item_id)


class DecisionQueue:
    """A validated, immutable set of queue items. Projection only; it decides nothing."""

    def __init__(self, items: Sequence) -> None:
        self._items = tuple(items)

    @property
    def items(self) -> Tuple:
        return self._items

    def _visible(self, filters: Tuple[str, ...]):
        return sorted(
            (entry for entry in self._items if entry.state in filters), key=_sort_key
        )

    def _detail(self, entry) -> SelectedDetail:
        if entry.state == STATE_WAITING:
            return SelectedDetail(
                item_id=entry.item_id,
                state=entry.state,
                explanation=entry.explanation,
                evidence=entry.evidence,
            )
        # An operational item has no human-decision explanation, and inventing a
        # plausible one is how a screen starts asking for decisions nobody raised.
        return SelectedDetail(item_id=entry.item_id, state=entry.state)

    def view(
        self,
        *,
        filters: Optional[Iterable[str]] = None,
        selected_id: Optional[str] = None,
    ) -> QueueView:
        """Rows, filters, and a selection that stays put whenever it still can."""
        active = _normalize_filters(filters)
        visible = self._visible(active)

        rows = tuple(
            QueueRow(
                item_id=entry.item_id,
                state=entry.state,
                title=entry.title,
                project=entry.project,
                ticket=entry.ticket,
                elapsed_seconds=entry.elapsed_seconds,
            )
            for entry in visible
        )

        chosen = None
        if selected_id is not None:
            for entry in visible:
                if entry.item_id == selected_id:
                    chosen = entry
                    break
        if chosen is None and visible:
            # The prior selection is gone. Land on the oldest remaining row rather
            # than on nothing, so the screen keeps showing the most pressing item.
            chosen = visible[0]

        return QueueView(
            rows=rows,
            filters=active,
            selected_id=chosen.item_id if chosen is not None else None,
            detail=self._detail(chosen) if chosen is not None else None,
        )


def _normalize_filters(filters: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Default to Waiting only. Any nonempty combination of the three is allowed."""
    if filters is None:
        return DEFAULT_FILTERS
    requested = tuple(filters)
    if not requested:
        raise QueueError(
            REASON_INVALID_FILTER,
            "an empty filter set shows nothing; omit filters for the default instead",
        )
    seen = []
    for entry in requested:
        if entry not in QUEUE_STATES:
            raise QueueError(
                REASON_INVALID_FILTER,
                "filter '{0}' is not one of {1}".format(entry, ", ".join(QUEUE_STATES)),
            )
        if entry not in seen:
            seen.append(entry)
    # Ordered by the canonical state order so two equivalent requests are equal.
    return tuple(state for state in QUEUE_STATES if state in seen)


def build_queue(
    decisions: Sequence[PendingDecision] = (),
    agents: Sequence[OperationalAgent] = (),
) -> DecisionQueue:
    """Validate and combine the two input kinds into one projectable queue."""
    items = []
    seen = set()
    for entry in tuple(decisions):
        if type(entry) is not PendingDecision:
            raise QueueError(
                REASON_INVALID_PROJECTION,
                "decisions must be PendingDecision, got {0!r}".format(type(entry).__name__),
            )
        items.append(entry)
    for entry in tuple(agents):
        if type(entry) is not OperationalAgent:
            raise QueueError(
                REASON_INVALID_PROJECTION,
                "agents must be OperationalAgent, got {0!r}".format(type(entry).__name__),
            )
        items.append(entry)

    for entry in items:
        if entry.item_id in seen:
            raise QueueError(
                REASON_DUPLICATE_ITEM,
                "item identity '{0}' appears twice; selection and routing would be "
                "ambiguous".format(entry.item_id),
            )
        seen.add(entry.item_id)

    return DecisionQueue(items)
