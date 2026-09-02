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
#
# Fifth, every item carries two further facts that arrive already projected: what
# the work is currently doing, and who owes it attention. They are independent by
# construction -- `attention_projection` computes each from its own durable
# evidence and neither from the other -- and this module never derives, repairs or
# reconciles one into the other. What it does enforce is the accepted equality:
# the Waiting set is exactly the human-owned set, no more and no less. Waiting
# still comes only from a published decision record, so that equality is a
# cross-check between two independently produced facts rather than a rule that
# manufactures either of them. A row shows neither: activity reaches a person
# through the operational filters and the detail pane, because a dense row that can
# carry one more badge is no longer a dense row.
#
# Sixth, a human-owned item must be actionable without leaving it. Decision D8
# names nine things a person needs before they can clear a permission,
# configuration, capability, credential or environment obstacle. Three of them are
# durable routing facts this module already held and simply never showed --
# project, ticket and the durable rail -- and six more arrive as one validated
# block, `ActionableBlocker`, or do not arrive at all. There is no partial
# version and no synthesised one: a field this module cannot source from the
# durable record is reported as unsourced, by name, rather than paraphrased into
# something that reads like an instruction. All of it lives in the selected
# detail. `QueueRow` gained no field, because a dense row that can carry one more
# useful thing is no longer a dense row, and `OperationalAgent` gained none
# either, because an agent-owned item does not become human work by being stuck.

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from .attention_projection import (
    OPERATIONAL_ACTIVITY_STATES,
    OWNER_AGENT,
    OWNER_HUMAN,
    AttentionError,
    require_activity,
    require_attention_owner,
)
from .session_lifecycle import (
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    SessionProjection,
)

__all__ = [
    "ActionableBlocker",
    "BLOCKER_KINDS",
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
# The D8 blocker strings. 240 is `control_plane.MAX_DECISION_BLOCKER_STRING`, the
# bound the publisher already enforces, restated rather than imported: this module
# is pure, and importing the control plane to borrow an integer would give a
# clock-free, file-free projection a dependency on Git and the filesystem. Restated
# constants drift, so a test asserts the two are equal rather than trusting this
# comment to be read.
MAX_BLOCKER_TEXT = 240
# This module's own sentence about why an actionable field could not be sourced.
# Deliberately larger than one blocker string and far smaller than an explanation:
# it names fields and says where they were looked for, and it is never a place to
# put what the blocker would have said.
MAX_BLOCKER_NOTICE = 400

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
REASON_ACTIVITY_CONTRADICTS_STATE = "activity-contradicts-state"
REASON_WAITING_NOT_HUMAN_OWNED = "waiting-is-not-the-human-owned-set"
REASON_INVALID_BLOCKER = "invalid-blocker"
REASON_UNSUPPORTED_BLOCKER_KIND = "unsupported-blocker-kind"
REASON_BLOCKER_STATE_NOT_EXPLICIT = "blocker-state-changed-not-explicit"
REASON_BLOCKER_DOUBLE_ANSWER = "blocker-both-carried-and-unsourced"

# The five failure kinds D8 names, and no sixth. Restated from
# `control_plane.DECISION_BLOCKER_KINDS` for the reason the bound above is, and
# checked against it by a test rather than by this comment.
BLOCKER_KINDS = ("permission", "configuration", "capability", "credential", "environment")


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


def _activity(value: object) -> str:
    """Exactly one modeled activity, refused through this module's own exception.

    The vocabulary belongs to `attention_projection`; only the spelling of the
    refusal belongs here, so a caller of this module never has to catch two
    exception types to learn that one field was wrong.
    """
    try:
        return require_activity(value)
    except AttentionError as exc:
        raise QueueError(exc.reason, exc.detail) from exc


def _owner(value: object) -> str:
    """Exactly `human` or `agent`, refused the same way."""
    try:
        return require_attention_owner(value)
    except AttentionError as exc:
        raise QueueError(exc.reason, exc.detail) from exc


def _references(value: object, *, label: str) -> Tuple["EvidenceReference", ...]:
    """A bounded tuple of pointers, and never the thing pointed at.

    One rule, applied to both kinds of item. A decision's published evidence and an
    operational item's transport evidence are different facts about different
    things, but the bound is the same bound and stating it twice would be two
    bounds to keep in step.
    """
    if type(value) is not tuple:
        raise QueueError(
            REASON_INVALID_EVIDENCE,
            "{0} must be a tuple of EvidenceReference, got {1!r}".format(
                label, type(value).__name__
            ),
        )
    if len(value) > MAX_EVIDENCE_REFERENCES:
        raise QueueError(
            REASON_TOO_MUCH_EVIDENCE,
            "{0} references {1} entries, exceeding the bound of {2}".format(
                label, len(value), MAX_EVIDENCE_REFERENCES
            ),
        )
    for entry in value:
        if type(entry) is not EvidenceReference:
            raise QueueError(
                REASON_INVALID_EVIDENCE,
                "{0} entries must be EvidenceReference, got {1!r}".format(
                    label, type(entry).__name__
                ),
            )
    return value


def _blocker(blocker: object, unavailable: object) -> None:
    """At most one answer about D8's actionable half, and each one well formed.

    Split out rather than inlined because the pair is one rule, not two fields.
    `ActionableBlocker` already refuses an incomplete blocker when it is
    constructed; what is left to check here is that a caller did not hand this
    item both a blocker and a statement that no blocker could be sourced.
    """
    if blocker is not None and type(blocker) is not ActionableBlocker:
        raise QueueError(
            REASON_INVALID_BLOCKER,
            "blocker must be an ActionableBlocker, got {0!r}".format(type(blocker).__name__),
        )
    if unavailable is not None:
        _text(unavailable, label="blocker_unavailable", limit=MAX_BLOCKER_NOTICE)
        if blocker is not None:
            raise QueueError(
                REASON_BLOCKER_DOUBLE_ANSWER,
                "an item carries an actionable blocker and also reports that one could "
                "not be sourced; a person reading both learns neither",
            )


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
class ActionableBlocker:
    """The six failure facts D8 requires, complete or absent. There is no partial one.

    D8 names nine things a human-attention item must state. Three of them --
    project, ticket and the durable rail -- are routing facts the item already
    carries, and they are not restated here: one fact stored twice is two facts
    that can disagree, and this projection has spent five other rules on making
    disagreement impossible rather than plausible. The remaining six live here.

    Every field is required, with no default, so an incomplete blocker cannot be
    constructed at all. That is the whole design: a half-described obstacle is the
    kind of item a person re-reads, fails to act on, and leaves in the queue. The
    publisher already enforces the same all-or-nothing rule
    (`control_plane._decision_blocker`); this is the second, independent
    enforcement at the projection boundary, so a record that reached this module
    without passing that one still cannot produce a plausible-looking half answer.

    `agent` is the affected agent, and it is the one field with no dedicated
    durable home. It is sourced from the durable rail's own published assignment
    by `queue_source`, which is the only statement of who works a rail that
    survives the transport being replaced. It is carried verbatim and validated
    only as bounded text: a rail may legitimately name an assignment this product
    models no session role for, and refusing it here would be this module deciding
    what a durable record is allowed to say.

    `state_changed` is an exact `bool`. "The worktree may have changed" is the one
    answer a person cannot act on, so there is no third value and no string that
    could carry one.
    """

    kind: str
    what_failed: str
    agent: str
    missing_capability: str
    human_change: str
    state_changed: bool
    next_action: str

    def __post_init__(self) -> None:
        if self.kind not in BLOCKER_KINDS:
            raise QueueError(
                REASON_UNSUPPORTED_BLOCKER_KIND,
                "blocker kind must be one of {0}; got {1!r}".format(
                    ", ".join(BLOCKER_KINDS), self.kind
                ),
            )
        _text(self.what_failed, label="blocker what_failed", limit=MAX_BLOCKER_TEXT)
        _text(self.agent, label="blocker agent", limit=MAX_BLOCKER_TEXT)
        _text(
            self.missing_capability, label="blocker missing_capability", limit=MAX_BLOCKER_TEXT
        )
        _text(self.human_change, label="blocker human_change", limit=MAX_BLOCKER_TEXT)
        _text(self.next_action, label="blocker next_action", limit=MAX_BLOCKER_TEXT)
        # `type(...) is bool` rather than `isinstance`: `1` and `0` are `int`
        # values that would pass an `isinstance(..., int)` check and print as
        # something other than a decision, and a truthy string would pass any
        # check that only asked whether it was set.
        if type(self.state_changed) is not bool:
            raise QueueError(
                REASON_BLOCKER_STATE_NOT_EXPLICIT,
                "blocker state_changed must be exactly True or False, got {0!r}; "
                "'it may have changed' is the one answer a person cannot act "
                "on".format(self.state_changed),
            )


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
    """One genuine human decision an orchestrator published. The only source of Waiting.

    `activity` and `attention_owner` are required with no default, exactly as every
    other durable fact here is. `attention_owner` can only ever be `human` -- and it
    is still stated rather than assumed, because the caller that proved a decision
    record exists is the same caller stating who owes the item attention, and a
    field that filled itself in would make the two impossible to disagree and so
    impossible to check.

    `activity` is deliberately unconstrained by the state. A Waiting item's state
    says a person must act; its activity says what the work itself is doing, and
    those genuinely differ -- a rail can be awaiting a human decision while its
    session is live, or while its execution ownership can no longer be proved at
    all. Pinning one to the other here would be the derivation this checkpoint
    exists to remove.

    `blocker` and `blocker_unavailable` are D8's actionable half, and exactly one
    of them may be set. A published record that carries no blocker block sets
    neither: not every decision a person is asked to make is a failure, and an
    item asking which of two approaches to take has no "what failed" to state.
    A record that does carry one either produces a complete `ActionableBlocker`
    or sets `blocker_unavailable` to the reason it could not, naming the field
    that had no durable source. Both at once is refused, because "here is the
    blocker, and also it could not be sourced" is not something a person can read.
    """

    decision_id: str
    project: str
    ticket: str
    rail: str
    raised_at: str
    title: str
    explanation: str
    elapsed_seconds: int
    activity: str
    attention_owner: str
    evidence: Tuple[EvidenceReference, ...] = ()
    blocker: Optional["ActionableBlocker"] = None
    blocker_unavailable: Optional[str] = None

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
        _activity(self.activity)
        _owner(self.attention_owner)
        if self.attention_owner != OWNER_HUMAN:
            raise QueueError(
                REASON_WAITING_NOT_HUMAN_OWNED,
                "a published decision is human-owned by definition; '{0}' would put a "
                "Waiting row outside the human-owned set".format(self.attention_owner),
            )
        _references(self.evidence, label="evidence")
        _blocker(self.blocker, self.blocker_unavailable)

    @property
    def item_id(self) -> str:
        return _identity(KIND_DECISION, self.project, self.ticket, self.rail, self.decision_id)

    @property
    def state(self) -> str:
        return STATE_WAITING


@dataclass(frozen=True)
class OperationalAgent:
    """One durable rail's work in progress. Visible when asked for, and never Waiting.

    The unit is the rail, not the session. `evidence` is where transport identity
    lives -- session id, role, pid, pid domain -- as bounded Details data supplied
    by the caller that reconciled it. A rail whose worker rotated keeps one item and
    one identity; what changes is which session appears in that list, and `item_id`
    already excludes every one of those fields.

    `attention_owner` can only ever be `agent` here, and is still required rather
    than assumed for the reason the decision side states its own: the equality
    between Waiting and the human-owned set is only checkable when both sides are
    stated independently.
    """

    project: str
    ticket: str
    rail: str
    title: str
    projection: SessionProjection
    activity: str
    attention_owner: str
    evidence: Tuple[EvidenceReference, ...] = ()

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
        _activity(self.activity)
        _owner(self.attention_owner)
        if self.attention_owner != OWNER_AGENT:
            raise QueueError(
                REASON_WAITING_NOT_HUMAN_OWNED,
                "an operational item can never be Waiting, so it can never be "
                "human-owned; got '{0}'".format(self.attention_owner),
            )
        # A subscript, not a lookup with a default: `_activity` already proved the
        # value is a modeled activity, and the table names every one of them.
        if state not in OPERATIONAL_ACTIVITY_STATES[self.activity]:
            # Two facts read apart and then required to agree, the same way a
            # decision record and its rail are. A running row claiming recovery, or
            # a disconnected row claiming an executor is working, is a contradiction
            # in the evidence rather than a row to draw.
            raise QueueError(
                REASON_ACTIVITY_CONTRADICTS_STATE,
                "activity '{0}' cannot describe a '{1}' operational item".format(
                    self.activity, state
                ),
            )
        _references(self.evidence, label="transport evidence")

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
    """The right pane's data. No title -- the row already said it.

    `activity` and `attention_owner` live here rather than on `QueueRow` on
    purpose. The accepted contract is that richer activity states reach a person
    through the operational filters and this pane, never as a per-row badge, so
    there is deliberately nowhere on a row to put either of them.

    `project`, `ticket` and `rail` are three of D8's nine, and they are required
    on both kinds of item rather than optional on one. They were always known --
    `item_id` encodes all three -- but that encoding is a routing key, not
    something a person reads, so a human-owned item genuinely did not state which
    durable rail it was about. They are stated for operational items too, because
    an item that names its rail only in its title is naming it by coincidence.

    `blocker` and `blocker_unavailable` are the other half, and only a decision
    item ever carries either. An operational item is refused them at the one place
    details are built: being blocked or disconnected is agent-owned work, and
    giving it human-attention fields would put it in front of a person who has
    nothing to do about it.
    """

    item_id: str
    state: str
    project: str
    ticket: str
    rail: str
    activity: str
    attention_owner: str
    explanation: Optional[str] = None
    evidence: Tuple[EvidenceReference, ...] = ()
    blocker: Optional[ActionableBlocker] = None
    blocker_unavailable: Optional[str] = None

    def __post_init__(self) -> None:
        _text(self.project, label="detail project", limit=MAX_LABEL)
        _text(self.ticket, label="detail ticket", limit=MAX_LABEL)
        _text(self.rail, label="detail rail", limit=MAX_LOCATOR)
        _blocker(self.blocker, self.blocker_unavailable)


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
                project=entry.project,
                ticket=entry.ticket,
                rail=entry.rail,
                activity=entry.activity,
                attention_owner=entry.attention_owner,
                explanation=entry.explanation,
                evidence=entry.evidence,
                # D8's actionable half reaches a person here and nowhere else.
                # Carried across exactly as it arrived: this pane is where the
                # published text is read, so summarising it on the way would be
                # the screen writing the sentence a person is meant to act on.
                blocker=entry.blocker,
                blocker_unavailable=entry.blocker_unavailable,
            )
        # An operational item has no human-decision explanation, and inventing a
        # plausible one is how a screen starts asking for decisions nobody raised.
        # Its evidence is a different kind entirely: the transport sessions behind
        # this rail, bounded, carried here because Details is the one place session
        # identity is allowed to be seen at all.
        # No blocker and no unavailability notice, and not because an operational
        # item happens to have neither to give: `OperationalAgent` has no field
        # for either, so there is nothing here to pass even by accident. An item
        # whose agent is stuck is still the agent's to unstick.
        return SelectedDetail(
            item_id=entry.item_id,
            state=entry.state,
            project=entry.project,
            ticket=entry.ticket,
            rail=entry.rail,
            activity=entry.activity,
            attention_owner=entry.attention_owner,
            evidence=entry.evidence,
        )

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

    _require_waiting_is_the_human_owned_set(items)
    return DecisionQueue(items)


def _require_waiting_is_the_human_owned_set(items: Sequence) -> None:
    """The accepted equality, checked over the assembled queue rather than assumed.

    Each input type already refuses the owner the other kind carries, so this can
    only fail if a future change loosens one of those. That is exactly why it is
    here: the property the product owes a person is about the *set* the default
    view shows, not about one item at a time, and a set property that is only ever
    implied by two local rules is one refactor away from being nobody's.

    Both directions are checked. A human-owned item outside Waiting would be a
    decision reachable only through an operational filter, and a Waiting item that
    is not human-owned would be an interruption nobody was asked for.
    """
    waiting = {entry.item_id for entry in items if entry.state == STATE_WAITING}
    human = {entry.item_id for entry in items if entry.attention_owner == OWNER_HUMAN}
    if waiting != human:
        raise QueueError(
            REASON_WAITING_NOT_HUMAN_OWNED,
            "the Waiting set and the human-owned set must be identical; waiting-only "
            "{0}, human-only {1}".format(
                sorted(waiting - human), sorted(human - waiting)
            ),
        )
