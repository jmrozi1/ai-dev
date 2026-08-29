"""Read-only allowance views and the manual `/usage` reading interface."""

from __future__ import annotations

# The estimator says what can honestly be projected and the store holds the
# evidence. Something has to stand between them and a caller that renders, and
# that is this module. It is pure data in both directions: nothing here touches a
# payload, a page, a server, a provider, or a clock of its own.
#
# Three things make it honest.
#
# First, one projection reads exactly one store generation. `projection_inputs`
# derives profile, anchor, workload and ledger cleanliness together, so a result
# recorded midway through a render cannot pair a newer workload total with an
# older cleanliness flag -- the exact combination that overstates coverage.
# Assembling the same view from `profile`, `latest_observation` and
# `workload_units` would read three generations and is refused by construction:
# there is one call, and no second read to disagree with it.
#
# Second, coverage is a conjunction and never an inference. The ledger half
# proves only that what this manager recorded was weighable; work it never
# launched raises the provider percentage without moving the local counter and is
# invisible here. The human half is a separate required argument with no default,
# so a caller that cannot assert it gets `unavailable` rather than a number that
# is wrong in a knowable direction.
#
# Third, a source that refuses is never a number. A malformed store, a mismatched
# meter, an edited total, or a profile whose readings contradict each other comes
# back as an explicit unavailable view carrying the exact refusal reason with
# `source_healthy=False`. It is never zero, never `available`, and never raised
# into a render caller -- a render that crashes and a render that shows a
# confident zero are both worse than one that says the source could not be read.

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence, Tuple

from .claude_allowance import (
    _exact_decimal,
    _exact_epoch,
    AllowanceError,
    CalibrationPoint,
    HEALTH_UNAVAILABLE,
    REASON_INVALID_EPOCH,
    REASON_INVALID_PERCENTAGE,
    REASON_INVALID_WINDOW,
    WINDOW_FIVE_HOUR,
    WINDOW_SEVEN_DAY,
    WINDOWS,
    estimate_current,
)
from .claude_allowance_store import (
    CURRENT_METER,
    AllowanceStore,
    AllowanceStoreError,
    ProjectionInputs,
    REASON_LOCK_LOST,
)

__all__ = [
    "AllowanceViewError",
    "AllowanceWindowView",
    "REASON_INVALID_COVERAGE_ASSERTION",
    "REASON_INVALID_READING",
    "REASON_NO_ANCHOR",
    "project_window",
    "record_usage_reading",
]

# This module's own refusals. Everything else keeps the accepted reason it was
# raised with; wrapping an accepted refusal would give one fact two names. A
# malformed window or clock is refused with the accepted `invalid-window` and
# `invalid-epoch` spellings for exactly that reason -- only the exception type
# changes, to say that the caller asked wrongly rather than that the evidence
# could not be read.
REASON_INVALID_COVERAGE_ASSERTION = "invalid-coverage-assertion"
REASON_INVALID_READING = "invalid-usage-reading"

# Not an exception: a readable store with no reading yet for this window. There
# is nothing to project from, and `estimate_current` cannot be called at all
# because it requires an anchor. Fabricating one would invent evidence.
REASON_NO_ANCHOR = "no-calibration-anchor"

# The order both windows of one `/usage` view are appended in. Fixed so that a
# partial failure is always the same partial failure, and stated once rather than
# spelled out at the call site.
_READING_ORDER = (WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY)


class AllowanceViewError(Exception):
    """A refusal this module itself owns, carrying one stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowanceWindowView:
    """What one window can honestly be shown as right now, including nothing.

    The field set is the whole display contract. There is no field that means
    "exhausted": `bounded` at the top of the scale means the local model clamped,
    which is the local model's opinion and not the provider's confirmation. There
    is no remaining-token count, no raw provider payload, no display string, no
    rounding, no colour and no label, because presentation is a later rail's
    decision and a value that has already been rounded cannot be unrounded.
    """

    window: str
    meter: str
    health: str
    reason: str
    point_percentage: Optional[Decimal]
    lower_percentage: Optional[Decimal]
    upper_percentage: Optional[Decimal]
    bounded: bool
    resets_at: Optional[int]
    newest_calibration_at: Optional[int]
    interval_count: int
    source_healthy: bool


# --------------------------------------------------------------------------
# Exact inputs
# --------------------------------------------------------------------------


def _exact_view_bool(value: object, *, label: str) -> bool:
    """Exactly a `bool`.

    A truthy string or a `1` would let a caller assert complete coverage without
    ever having decided it, which is precisely the assertion that must be
    deliberate. `bool` is an `int` subclass, so this checks the type itself.
    """
    if type(value) is not bool:
        raise AllowanceViewError(
            REASON_INVALID_COVERAGE_ASSERTION,
            "{0} must be an exact bool, got {1!r}".format(label, value),
        )
    return value


def _canonical_window(value: object) -> str:
    """Exactly one of the accepted windows, returned as the package's own `str`.

    Canonicalised rather than merely checked, so the value that reaches a frozen
    field declared `str` is the constant itself and never an equal-but-different
    object. A window this package does not name is the caller asking about the
    wrong thing, which is the caller's fault and not the store's evidence being
    unreadable.
    """
    if isinstance(value, str):
        for window in WINDOWS:
            if value == window:
                return window
    raise AllowanceViewError(
        REASON_INVALID_WINDOW,
        "window must be one of {0}, got {1!r}".format(", ".join(WINDOWS), value),
    )


def _view_epoch(value: object) -> int:
    """Exactly the estimator's own positive epoch, refused as a caller fault.

    The rule is not restated here. `_exact_epoch` is the one the estimator
    applies, so `time.time()` stays invalid and the two cannot drift apart; only
    the category changes. A caller's clock is the caller's, and reporting a bad
    one as an unreadable source sends a human to inspect a healthy ledger.
    """
    try:
        return _exact_epoch(value, label="now")
    except AllowanceError as exc:
        raise AllowanceViewError(REASON_INVALID_EPOCH, exc.detail) from exc


def _reading_pair(value: object, *, window: str) -> Tuple[Any, Any]:
    """Exactly one `(resets_at, used_percentage)` pair.

    Shape only. The values themselves are the accepted store's and
    `CalibrationPoint`'s to judge, and re-checking them here would give one
    refusal two spellings that could drift apart.
    """
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise AllowanceViewError(
            REASON_INVALID_READING,
            "{0} must be a (resets_at, used_percentage) pair, got {1!r}".format(
                window, value
            ),
        )
    return value[0], value[1]


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def _unavailable_view(
    window: str, meter: str, reason: str, *, source_healthy: bool
) -> AllowanceWindowView:
    """One unavailable view. Never zero, and never without a reason."""
    return AllowanceWindowView(
        window=window,
        meter=meter,
        health=HEALTH_UNAVAILABLE,
        reason=reason,
        point_percentage=None,
        lower_percentage=None,
        upper_percentage=None,
        bounded=False,
        resets_at=None,
        newest_calibration_at=None,
        interval_count=0,
        source_healthy=source_healthy,
    )


def _source_unhealthy_view(window: str, error: Exception) -> AllowanceWindowView:
    """A store or profile refusal, said out loud rather than rendered as a number.

    The meter is the one this view is *about*, not a claim about what the store
    holds: the accepted loader refuses any store written for a different meter, so
    a readable store always reports this same meter. The window is the canonical
    one this projection is for; a caller that named something else never reaches
    here, because that is a caller fault and this is only ever a source failure.
    """
    return _unavailable_view(
        window, CURRENT_METER, error.reason, source_healthy=False
    )


def _view_from_inputs(
    inputs: ProjectionInputs,
    *,
    now: int,
    human_complete_coverage_since_anchor: bool,
) -> AllowanceWindowView:
    """Pure: one generation of evidence in, one view out.

    `window` and `meter` come from the inputs and never from the profile or the
    estimate. With no observations `build_profile(())` reports an empty window and
    meter, and an estimate built from it would carry that blank identity into a
    view that is supposed to name what it describes.
    """
    if inputs.anchor is None:
        return _unavailable_view(
            inputs.window, inputs.meter, REASON_NO_ANCHOR, source_healthy=True
        )

    try:
        estimate = estimate_current(
            inputs.profile,
            inputs.anchor,
            now=now,
            workload_units=inputs.workload_units,
            # Two independent facts, and neither one alone is coverage. The store
            # can only see the ledger it wrote; only the human knows whether they
            # used Claude somewhere this manager never launched.
            complete_coverage_since_anchor=(
                inputs.ledger_clean_since_anchor
                and human_complete_coverage_since_anchor
            ),
        )
    except (AllowanceStoreError, AllowanceError) as exc:
        return _source_unhealthy_view(inputs.window, exc)

    return AllowanceWindowView(
        window=inputs.window,
        meter=inputs.meter,
        health=estimate.health,
        reason=estimate.reason,
        # Copied exactly. Every one of these is already the estimator's own
        # `Decimal`, and arithmetic here would be a second opinion about a number
        # that was computed once under a declared context.
        point_percentage=estimate.point_percentage,
        lower_percentage=estimate.lower_percentage,
        upper_percentage=estimate.upper_percentage,
        bounded=estimate.bounded,
        resets_at=estimate.resets_at,
        newest_calibration_at=estimate.newest_calibration_at,
        interval_count=estimate.interval_count,
        # The store answered. An estimate that is unavailable because coverage is
        # incomplete, calibration is stale, the window turned over, or there is no
        # trained interval yet is a healthy source saying it cannot help.
        source_healthy=True,
    )


def project_window(
    store: AllowanceStore,
    *,
    window: object,
    now: object,
    human_complete_coverage_since_anchor: object,
) -> AllowanceWindowView:
    """Project one window from exactly one read of the store.

    `human_complete_coverage_since_anchor` is required and has no default, for the
    same reason `estimate_current` requires its half: the caller is the only thing
    that knows whether Claude was used outside this manager since the anchor
    reading, and a caller that cannot say must get `unavailable` rather than a
    confident number that reads low.

    All three caller inputs -- that assertion, the window, and the clock -- are
    checked before the store is touched, and a caller fault raises
    `AllowanceViewError` rather than returning a view. They are the caller's to
    get right, and answering a malformed question with "the evidence could not be
    read" would accuse a healthy store of a fault that is not its own. What the
    *store* refuses still comes back as an unavailable view, so a render that
    asked properly can always draw something truthful.
    """
    human = _exact_view_bool(
        human_complete_coverage_since_anchor,
        label="human_complete_coverage_since_anchor",
    )
    # Before the read, and in this order, so no caller fault can be answered from
    # the store -- including on a store with no anchor, which would otherwise
    # return early and never look at the clock at all.
    projected_window = _canonical_window(window)
    current_time = _view_epoch(now)
    try:
        # Exactly once. A second read would be a second generation.
        inputs = store.projection_inputs(projected_window)
    except (AllowanceStoreError, AllowanceError) as exc:
        return _source_unhealthy_view(projected_window, exc)
    return _view_from_inputs(
        inputs, now=current_time, human_complete_coverage_since_anchor=human
    )


# --------------------------------------------------------------------------
# Manual `/usage` reading
# --------------------------------------------------------------------------


def _landed_observation(
    store: AllowanceStore,
    *,
    window: str,
    observed_at: object,
    resets_at: object,
    used_percentage: object,
) -> Optional[CalibrationPoint]:
    """The durable reading, but only when it is exactly the attempted one."""
    try:
        # Once. This is a reconciliation, not a poll.
        latest = store.latest_observation(window)
        expected = _exact_decimal(
            used_percentage,
            label="used_percentage",
            reason=REASON_INVALID_PERCENTAGE,
        )
    except (AllowanceStoreError, AllowanceError):
        return None
    if latest is None:
        return None
    if (
        latest.window == window
        and latest.observed_at == observed_at
        and latest.resets_at == resets_at
        and latest.used_percentage == expected
    ):
        return latest
    return None


def _append_reconciled(
    store: AllowanceStore,
    *,
    window: str,
    observed_at: object,
    resets_at: object,
    used_percentage: object,
    human_complete_coverage: bool,
) -> CalibrationPoint:
    """One append, with the one refusal that may already have landed reconciled.

    The accepted store releases its lock in a `finally`, so a `store-lock-lost`
    can surface after the durable write succeeded -- and appends carry no
    idempotency key, so replaying one either duplicates a reading or is refused as
    out of order. Neither is a safe guess, so this reads the durable history once
    and accepts only a reading that is exactly the one it tried to write.

    `store-locked` and `store-lock-malformed` are deliberately not reconciled:
    both leave the store byte-unchanged, so treating either as landed would invent
    a reading that does not exist.
    """
    try:
        return store.append_observation(
            window=window,
            observed_at=observed_at,
            resets_at=resets_at,
            used_percentage=used_percentage,
            human_complete_coverage=human_complete_coverage,
        )
    except AllowanceStoreError as exc:
        if exc.reason != REASON_LOCK_LOST:
            raise
        landed = _landed_observation(
            store,
            window=window,
            observed_at=observed_at,
            resets_at=resets_at,
            used_percentage=used_percentage,
        )
        if landed is None:
            # Absent, different, or unreadable. The original refusal is still the
            # truthful answer: this call cannot prove what happened.
            raise
        return landed


def record_usage_reading(
    store: AllowanceStore,
    *,
    observed_at: object,
    five_hour: Optional[Sequence[Any]] = None,
    seven_day: Optional[Sequence[Any]] = None,
    human_complete_coverage: object,
) -> Dict[str, CalibrationPoint]:
    """Record one human `/usage` view against the ledger, per window.

    One view reports both windows at one instant, and the accepted store's
    predecessor and ordering rules are per window, so both readings may share an
    `observed_at`. Windows are appended in a fixed order, five-hour then
    seven-day, so a partial failure is always the same partial failure.

    An omitted window appends nothing and is absent from the result. It is never
    filled in from the other window: the two windows measure different spans, and
    `CalibrationPoint` has no "unknown percentage" to write.

    `human_complete_coverage` is the human's assertion about the span since their
    previous reading of each window submitted. The store conjoins it with its own
    ledger cleanliness, so a truthful human plus a holed ledger still records
    incomplete coverage.

    Not transactional, and deliberately not pretending to be. The accepted store
    exposes only per-window appends, so if the five-hour reading lands and the
    seven-day reading is refused, the five-hour reading stays -- it is true, and
    there is no append-only way to take it back. The refusal propagates and this
    returns nothing at all, so no caller can read a partial result as a whole one.
    """
    human = _exact_view_bool(
        human_complete_coverage, label="human_complete_coverage"
    )
    # Shape first, both windows, before anything is written. A malformed
    # seven-day pair must not be discovered after the five-hour reading is
    # already durable.
    submitted = {}
    for window, value in ((WINDOW_FIVE_HOUR, five_hour), (WINDOW_SEVEN_DAY, seven_day)):
        if value is None:
            continue
        submitted[window] = _reading_pair(value, window=window)

    recorded = {}
    for window in _READING_ORDER:
        if window not in submitted:
            continue
        window_resets_at, used_percentage = submitted[window]
        recorded[window] = _append_reconciled(
            store,
            window=window,
            observed_at=observed_at,
            resets_at=window_resets_at,
            used_percentage=used_percentage,
            human_complete_coverage=human,
        )
    return recorded
