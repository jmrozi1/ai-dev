"""What the recorded progress facts may honestly be shown as, including nothing."""

from __future__ import annotations

# The store holds the facts and something has to decide what they come to. That
# is this module, and it is pure data in both directions: it touches no payload,
# no page, no server, no repository and no clock of its own. The instant is the
# caller's, exactly as it is for the accepted allowance projection, because a
# clock read here would be an instant no caller could pin.
#
# It is also, deliberately, the end of the line. `ProgressView` is a frozen record
# of things to draw. It has no method, no ordering, no comparison, and no
# threshold; nothing here returns a verdict, a decision, a priority, a slot, an
# admission or a trigger, and no function in this module takes anything an
# authorization, acceptance, review, remediation or scheduling path would hand it.
# Accepted decision D11 makes progress telemetry observability for the human, and
# the way that is kept true is that there is nothing here for a control path to
# call.
#
# Four things make the measure honest.
#
# First, the numerator is an acceptance or it is nothing. It is read from the
# store's acceptance records, which are the only records there are; a checkpoint
# that was published, pushed, branched, or handed off has left no trace here to
# be read. There is no code path on which the numerator is anything but the last
# accepted numeric checkpoint.
#
# Second, progress consumes the estimate and only a new estimate moves it. Each
# projection was recorded against a basis, so the remaining count still standing
# now is that entry's remaining less the checkpoints accepted since. Two
# checkpoints accepted after a projection shrink the remaining count by two and
# leave the projected final exactly where it was -- which is what makes a
# denominator that was revised distinguishable from one that progress ate into.
#
# Third, an estimate reality has overtaken says so. If more checkpoints have been
# accepted than the standing projection left room for, the remaining count would
# be negative and the percentage would exceed 100. That is not clamped to a
# reassuring number: the measure becomes unavailable with its reason, and the
# accepted facts around it are still shown, because a stale estimate is a thing
# the human needs to see rather than a thing to round away.
#
# Fourth, a delta with too little history behind it is absent rather than zero.
# "No checkpoints accepted in 48 hours" and "this store is four hours old" are
# different facts, and a bare 0 would read as the first when it meant the second.

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Tuple

from .progress_store import (
    ProgressFacts,
    ProgressStore,
    ProgressStoreError,
    Projection,
)

__all__ = [
    "DELTA_WINDOWS",
    "ProgressView",
    "REASON_HISTORY_AFTER_NOW",
    "REASON_INSUFFICIENT_HISTORY",
    "REASON_INVALID_INSTANT",
    "REASON_NO_ACCEPTANCE",
    "REASON_NO_PROJECTION",
    "REASON_PROJECTION_OVERTAKEN",
    "ProgressViewError",
    "project_progress",
]

# Not exceptions: readable evidence that does not add up to a percentage yet.
REASON_NO_ACCEPTANCE = "no-accepted-checkpoint"
REASON_NO_PROJECTION = "no-recorded-projection"
REASON_PROJECTION_OVERTAKEN = "projection-overtaken"
REASON_INSUFFICIENT_HISTORY = "insufficient-timestamped-history"
REASON_HISTORY_AFTER_NOW = "history-after-this-instant"

# This module's own refusal, for a caller that asked wrongly.
REASON_INVALID_INSTANT = "invalid-instant"

# The two windows accepted decision D11 names, in seconds, longest last so the
# 48-hour figure is never quietly derived from the 24-hour one.
DELTA_WINDOWS = (24 * 3600, 48 * 3600)


class ProgressViewError(Exception):
    """A refusal this module owns: the caller asked wrongly."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressView:
    """The complete D11 recorded surface for one instant, or as much as is known.

    The field set is the display contract. There is no rounded percentage, no
    label, no colour and no phrase, because presentation is the page's decision
    and a figure that has already been rounded cannot be unrounded. There is
    equally no elapsed time, no handoff count, no session, no token figure and no
    velocity: accepted decision D11 states those are management signals for the
    human alone, and a field here is a field something could be built on.

    `available` is about the percentage alone. Every other field is filled in as
    far as the recorded facts allow, so an estimate that has gone stale still
    shows the accepted checkpoint and the instant it was accepted rather than
    blanking the whole surface.
    """

    available: bool
    reason: Optional[str]
    source_healthy: bool

    named_checkpoint: Optional[int]
    named_total: Optional[int]
    named_completed_at: Optional[str]

    accepted_checkpoint: Optional[int]
    accepted_at: Optional[str]

    projected_remaining: Optional[int]
    projected_final: Optional[int]
    percentage: Optional[Decimal]
    confidence: Optional[str]

    delta_24h: Optional[int]
    delta_48h: Optional[int]
    delta_reason: Optional[str]

    revision_at: Optional[str]
    revision_from: Optional[int]
    revision_to: Optional[int]
    revision_note: Optional[str]
    preserved_count: int


def _blank(
    reason: str,
    *,
    source_healthy: bool = True,
    named: Tuple[Optional[int], Optional[int], Optional[str]] = (None, None, None),
    accepted: Tuple[Optional[int], Optional[str]] = (None, None),
    deltas: Tuple[Optional[int], Optional[int], Optional[str]] = (None, None, None),
    revision: Tuple[Optional[str], Optional[int], Optional[int], Optional[str]] = (
        None,
        None,
        None,
        None,
    ),
    preserved_count: int = 0,
) -> ProgressView:
    """A view with no percentage, carrying its reason and whatever else is known."""
    return ProgressView(
        available=False,
        reason=reason,
        source_healthy=source_healthy,
        named_checkpoint=named[0],
        named_total=named[1],
        named_completed_at=named[2],
        accepted_checkpoint=accepted[0],
        accepted_at=accepted[1],
        projected_remaining=None,
        projected_final=None,
        percentage=None,
        confidence=None,
        delta_24h=deltas[0],
        delta_48h=deltas[1],
        delta_reason=deltas[2],
        revision_at=revision[0],
        revision_from=revision[1],
        revision_to=revision[2],
        revision_note=revision[3],
        preserved_count=preserved_count,
    )


# --------------------------------------------------------------------------
# Exact inputs
# --------------------------------------------------------------------------


def _view_instant(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProgressViewError(
            REASON_INVALID_INSTANT,
            "a projection instant is whole seconds since the epoch, got {0!r}".format(value),
        )
    return value


def _epoch(instant: str) -> int:
    """One recorded instant as whole seconds, from the text git itself emitted.

    The store already refused anything that is not `git log -1 --format=%cI`
    output, so this parses a shape that has been validated rather than trusting
    the file. The offset is part of that shape and is honoured, which is why two
    acceptances recorded in two timezones still order correctly.

    Git prints `Z` rather than `+00:00` for UTC, and the supported interpreter's
    `fromisoformat` does not read `Z`. It is rewritten to the offset it means --
    the one form to the other, not a guess -- because a UTC acceptance is the
    common case on a build host and silently failing to parse it would drop the
    exact history the deltas are counted from.
    """
    text = instant[:-1] + "+00:00" if instant.endswith("Z") else instant
    return int(datetime.fromisoformat(text).timestamp())


# --------------------------------------------------------------------------
# The parts of the measure
# --------------------------------------------------------------------------


def _implied_final(entry: Projection) -> int:
    """The projected final this entry stated: its basis plus what it left to do."""
    return entry.basis + entry.remaining


def _revision(projections: Tuple[Projection, ...]):
    """The latest change to the projected total, and how often it was preserved.

    The first entry establishes an estimate rather than revising one, so it is
    never a revision. Every entry after it either moved the projected final --
    which is the revision D11 asks to be shown, so a percentage that drops cannot
    read as lost work -- or restated it, which is the orchestrator having
    reconsidered and preserved, and is counted separately.
    """
    latest = None
    preserved = 0
    for previous, entry in zip(projections, projections[1:]):
        before = _implied_final(previous)
        after = _implied_final(entry)
        if before == after:
            preserved += 1
        else:
            latest = (entry.recorded_at, before, after, entry.note or None)
    return (latest or (None, None, None, None)), preserved


def _deltas(facts: ProgressFacts, now: int):
    """Numeric checkpoints accepted in each window, when the history reaches back.

    The delta is a count of accepted numeric checkpoints, which is the one
    quantity every window can be answered in from acceptance facts alone. A
    percentage-point delta would need the denominator that stood at each past
    instant, and would move when the estimate was revised -- which is precisely
    the confusion D11 asks this surface to prevent.
    """
    if not facts.acceptances:
        return (None, None, REASON_INSUFFICIENT_HISTORY)
    stamps = [_epoch(entry.accepted_at) for entry in facts.acceptances]
    if max(stamps) > now:
        # The recorded history runs past the instant being served. Counting into
        # a window from here would answer a question about the future.
        return (None, None, REASON_HISTORY_AFTER_NOW)
    reach = now - min(stamps)
    counts = []
    reasons = []
    for window in DELTA_WINDOWS:
        if reach < window:
            counts.append(None)
            reasons.append(REASON_INSUFFICIENT_HISTORY)
        else:
            counts.append(sum(1 for stamp in stamps if stamp > now - window))
    return (counts[0], counts[1], reasons[0] if reasons else None)


def _named(facts: ProgressFacts):
    """The current named checkpoint, derived from the completed prefix.

    Named checkpoints complete in order and the store enforces that, so the one
    in progress is the one after the highest completed. Before any completion is
    recorded there is nothing to derive it from and nothing is claimed -- naming
    checkpoint 1 there would be an inference, not a fact.
    """
    if not facts.named:
        return (None, None, None)
    latest = facts.named[-1]
    current = latest.checkpoint + 1 if latest.checkpoint < latest.total else None
    return (current, latest.total, latest.completed_at)


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------


def project_progress(store: ProgressStore, *, now: int) -> ProgressView:
    """What the recorded facts in one store come to at one instant.

    Read-only and durable-free: this opens the store's file to read it and writes
    nothing, takes no lock, creates no directory and leaves no trace. A store that
    refuses comes back as an unavailable view carrying that exact refusal reason
    with `source_healthy=False` -- never raised into a render caller, and never a
    confident zero, because a page that crashes and a page that shows 0% are both
    worse than one that says the evidence could not be read.
    """
    instant = _view_instant(now)
    if type(store) is not ProgressStore:
        raise ProgressViewError(
            REASON_INVALID_INSTANT,
            "progress is projected from a ProgressStore, got {0!r}".format(
                type(store).__name__
            ),
        )
    try:
        facts = store.facts()
    except ProgressStoreError as error:
        return _blank(error.reason, source_healthy=False)

    named = _named(facts)
    revision, preserved = _revision(facts.projections)

    if not facts.acceptances:
        return _blank(
            REASON_NO_ACCEPTANCE,
            named=named,
            deltas=_deltas(facts, instant),
            revision=revision,
            preserved_count=preserved,
        )

    accepted = facts.acceptances[-1]
    accepted_pair = (accepted.checkpoint, accepted.accepted_at)
    deltas = _deltas(facts, instant)

    if not facts.projections:
        return _blank(
            REASON_NO_PROJECTION,
            named=named,
            accepted=accepted_pair,
            deltas=deltas,
            revision=revision,
            preserved_count=preserved,
        )

    standing = facts.projections[-1]
    # Every checkpoint accepted since this projection was recorded has already
    # been done, so it comes out of what remains rather than being added to the
    # total. The projected final therefore holds still while progress is made.
    remaining = standing.remaining - (accepted.checkpoint - standing.basis)
    if remaining < 0:
        return _blank(
            REASON_PROJECTION_OVERTAKEN,
            named=named,
            accepted=accepted_pair,
            deltas=deltas,
            revision=revision,
            preserved_count=preserved,
        )

    final = accepted.checkpoint + remaining
    return ProgressView(
        available=True,
        reason=None,
        source_healthy=True,
        named_checkpoint=named[0],
        named_total=named[1],
        named_completed_at=named[2],
        accepted_checkpoint=accepted.checkpoint,
        accepted_at=accepted.accepted_at,
        projected_remaining=remaining,
        projected_final=final,
        percentage=(Decimal(accepted.checkpoint) * 100) / Decimal(final),
        confidence=standing.confidence,
        delta_24h=deltas[0],
        delta_48h=deltas[1],
        delta_reason=deltas[2],
        revision_at=revision[0],
        revision_from=revision[1],
        revision_to=revision[2],
        revision_note=revision[3],
        preserved_count=preserved,
    )
