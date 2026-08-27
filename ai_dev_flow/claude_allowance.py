"""An estimate of Claude allowance consumption, honest about being an estimate."""

from __future__ import annotations

# The manager knows exactly how much work it sent. It does not know how Anthropic
# converts that work into a subscription allowance percentage, and no supported
# local source tells it. This module bridges the two the only honest way
# available: a human reads `/usage`, and those readings calibrate the local
# workload measure against the provider's own numbers.
#
# Four boundaries hold it honest.
#
# First, the workload unit is a weighted score, not money and not a percentage.
# It derives later from the Agent SDK's per-query `total_cost_usd`, which is
# documented as a client-side list-price estimate rather than billing truth. It
# is useful here only because it moves in proportion to work done.
#
# Second, a conversion rate may be trained only from an interval this manager
# fully covered. If the person used Claude elsewhere between two readings, the
# provider percentage rose for work the local counter never saw, and any rate
# derived from that pair would be wrong in a direction nothing here can detect.
# Incomplete coverage is therefore excluded from training outright.
#
# Third, one interval is one observation. It yields a `provisional` point and
# says so. Two or more yield the empirical min/max of the rates actually seen --
# a range, not a confidence interval, because nothing here has the sample size or
# the error model to justify statistics.
#
# Fourth, missing evidence stays missing. Absent, stale, cross-window,
# cross-reset, cross-meter, or partially covered evidence produces `unavailable`
# with a stable reason. It never becomes zero, and an estimate that reaches the
# top of the scale is still an estimate rather than confirmed exhaustion.

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence, Tuple

__all__ = [
    "AllowanceError",
    "AllowanceEstimate",
    "CalibrationPoint",
    "CalibrationProfile",
    "EvidenceInterval",
    "STALE_AFTER_SECONDS",
    "WINDOWS",
    "build_profile",
    "estimate_current",
]

WINDOW_FIVE_HOUR = "five_hour"
WINDOW_SEVEN_DAY = "seven_day"
WINDOWS = (WINDOW_FIVE_HOUR, WINDOW_SEVEN_DAY)

# Calibration older than this describes a conversion that may no longer hold.
# Thirty-five days, expressed in seconds; the boundary itself is stale.
STALE_AFTER_SECONDS = 35 * 24 * 60 * 60

PERCENT_FLOOR = Decimal("0")
PERCENT_CEILING = Decimal("100")

# Health of an estimate.
HEALTH_PROVISIONAL = "available-provisional"
HEALTH_CALIBRATED = "available-calibrated"
HEALTH_UNAVAILABLE = "unavailable"

# Stable reasons. Every refusal names exactly one.
REASON_PROVISIONAL_SINGLE_INTERVAL = "provisional-single-interval"
REASON_CALIBRATED_RANGE = "calibrated-interval-range"
REASON_NO_INTERVAL = "no-valid-training-interval"
REASON_STALE = "calibration-stale"
REASON_WINDOW_RESET = "window-already-reset"
REASON_ANCHOR_WINDOW_MISMATCH = "anchor-window-mismatch"
REASON_ANCHOR_METER_MISMATCH = "anchor-meter-mismatch"
REASON_TIME_PRECEDES_ANCHOR = "time-precedes-anchor"
REASON_WORKLOAD_PRECEDES_ANCHOR = "workload-precedes-anchor"

# Validation refusals.
REASON_INVALID_WINDOW = "invalid-window"
REASON_INVALID_EPOCH = "invalid-epoch"
REASON_OBSERVATION_AFTER_RESET = "observation-at-or-after-reset"
REASON_INVALID_PERCENTAGE = "invalid-percentage"
REASON_INVALID_WORKLOAD = "invalid-workload"
REASON_INVALID_METER = "invalid-meter"
REASON_INVALID_COVERAGE = "invalid-coverage-flag"
REASON_INVALID_POINT = "invalid-calibration-point"
REASON_DUPLICATE_OBSERVATION = "duplicate-observation-epoch"
REASON_MIXED_PROFILE = "mixed-window-or-meter"


class AllowanceError(Exception):
    """A refusal to accept evidence, carrying the exact reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__("{0}: {1}".format(reason, detail))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Exact values
# --------------------------------------------------------------------------


def _exact_int(value: object, *, label: str, reason: str) -> int:
    """Exactly an `int`.

    `bool` is an `int` subclass and is refused: `True` seconds is not an epoch,
    and silently accepting it would put a nonsense value into arithmetic that
    later reads as a real measurement.
    """
    if type(value) is not int:
        raise AllowanceError(
            reason, "{0} must be an exact int, got {1!r}".format(label, value)
        )
    return value


def _exact_decimal(value: object, *, label: str, reason: str) -> Decimal:
    """An exact decimal from an int, a string, or a Decimal.

    `float` is refused deliberately. A binary float cannot represent most decimal
    percentages exactly, so accepting one would make the same evidence produce
    different arithmetic depending on how the caller happened to spell it.
    """
    if type(value) is bool or isinstance(value, float):
        raise AllowanceError(
            reason,
            "{0} must be an exact int, str, or Decimal, got {1!r}".format(label, value),
        )
    if type(value) is int:
        return Decimal(value)
    if type(value) is str:
        try:
            converted = Decimal(value)
        except Exception:
            raise AllowanceError(reason, "{0} is not a number: {1!r}".format(label, value))
    elif type(value) is Decimal:
        converted = value
    else:
        raise AllowanceError(
            reason,
            "{0} must be an exact int, str, or Decimal, got {1!r}".format(label, value),
        )
    if not converted.is_finite():
        raise AllowanceError(reason, "{0} must be finite, got {1!r}".format(label, value))
    return converted


def _exact_text(value: object, *, label: str, reason: str) -> str:
    if type(value) is not str or not value.strip():
        raise AllowanceError(
            reason, "{0} must be exact non-empty text, got {1!r}".format(label, value)
        )
    return value


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationPoint:
    """One human reading of Claude `/usage`, paired with the local counter.

    The field set is the whole privacy contract: there is nowhere to put an
    account identifier, a prompt, a response, a transcript path, a log, or a
    credential, because no such field exists.
    """

    window: str
    observed_at: int
    resets_at: int
    used_percentage: Decimal
    workload_units: Decimal
    meter: str
    complete_coverage: bool

    def __post_init__(self) -> None:
        if self.window not in WINDOWS:
            raise AllowanceError(
                REASON_INVALID_WINDOW,
                "window '{0}' is not one of {1}".format(self.window, ", ".join(WINDOWS)),
            )
        observed = _exact_int(self.observed_at, label="observed_at", reason=REASON_INVALID_EPOCH)
        resets = _exact_int(self.resets_at, label="resets_at", reason=REASON_INVALID_EPOCH)
        if observed <= 0 or resets <= 0:
            raise AllowanceError(REASON_INVALID_EPOCH, "epochs must be positive")
        if observed >= resets:
            # A reading taken at or after the reset belongs to a window that has
            # already turned over, so it identifies nothing usable.
            raise AllowanceError(
                REASON_OBSERVATION_AFTER_RESET,
                "observed_at {0} is not before resets_at {1}".format(observed, resets),
            )

        percentage = _exact_decimal(
            self.used_percentage, label="used_percentage", reason=REASON_INVALID_PERCENTAGE
        )
        if percentage < PERCENT_FLOOR or percentage > PERCENT_CEILING:
            raise AllowanceError(
                REASON_INVALID_PERCENTAGE,
                "used_percentage {0} is outside 0-100".format(percentage),
            )
        units = _exact_decimal(
            self.workload_units, label="workload_units", reason=REASON_INVALID_WORKLOAD
        )
        if units < 0:
            raise AllowanceError(
                REASON_INVALID_WORKLOAD, "workload_units {0} is negative".format(units)
            )

        _exact_text(self.meter, label="meter", reason=REASON_INVALID_METER)
        if type(self.complete_coverage) is not bool:
            raise AllowanceError(
                REASON_INVALID_COVERAGE,
                "complete_coverage must be a bool, got {0!r}".format(self.complete_coverage),
            )

        object.__setattr__(self, "used_percentage", percentage)
        object.__setattr__(self, "workload_units", units)

    @property
    def reset_identity(self) -> Tuple[str, str, int]:
        """What makes two readings belong to the same provider window."""
        return (self.window, self.meter, self.resets_at)


@dataclass(frozen=True)
class EvidenceInterval:
    """Two readings this manager fully covered, and the rate between them."""

    window: str
    meter: str
    resets_at: int
    started_at: int
    ended_at: int
    units_delta: Decimal
    percentage_delta: Decimal
    rate: Decimal


def _sorted_points(points: Sequence[CalibrationPoint]) -> Tuple[CalibrationPoint, ...]:
    """A deterministic order, refusing anything that makes predecessors ambiguous."""
    ordered = sorted(
        points, key=lambda p: (p.window, p.meter, p.resets_at, p.observed_at)
    )
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.reset_identity != later.reset_identity:
            continue
        if earlier.observed_at == later.observed_at:
            # Two readings of the same window at the same instant: whichever is
            # the predecessor changes the trained rate, so neither may be used.
            raise AllowanceError(
                REASON_DUPLICATE_OBSERVATION,
                "two readings of {0} share observation epoch {1}".format(
                    earlier.reset_identity, earlier.observed_at
                ),
            )
    return tuple(ordered)


def _interval(
    earlier: CalibrationPoint, later: CalibrationPoint
) -> Optional[EvidenceInterval]:
    """The rate between two adjacent readings, or nothing when they cannot train one."""
    if earlier.reset_identity != later.reset_identity:
        # Never span a reset: the provider percentage restarts, so the pair
        # describes no single window's consumption.
        return None
    if not later.complete_coverage:
        # The flag describes the span since the preceding reading. Without it,
        # unseen use inflated the provider percentage and would inflate the rate.
        return None
    if later.observed_at <= earlier.observed_at:
        return None
    if later.workload_units <= earlier.workload_units:
        return None
    if later.used_percentage < earlier.used_percentage:
        return None

    units_delta = later.workload_units - earlier.workload_units
    percentage_delta = later.used_percentage - earlier.used_percentage
    return EvidenceInterval(
        window=later.window,
        meter=later.meter,
        resets_at=later.resets_at,
        started_at=earlier.observed_at,
        ended_at=later.observed_at,
        units_delta=units_delta,
        percentage_delta=percentage_delta,
        rate=percentage_delta / units_delta,
    )


@dataclass(frozen=True)
class CalibrationProfile:
    """Every rate this manager has actually observed, for one window and meter."""

    window: str
    meter: str
    intervals: Tuple[EvidenceInterval, ...] = ()
    newest_observed_at: Optional[int] = None

    @property
    def rates(self) -> Tuple[Decimal, ...]:
        return tuple(interval.rate for interval in self.intervals)

    @property
    def minimum_rate(self) -> Optional[Decimal]:
        return min(self.rates) if self.intervals else None

    @property
    def maximum_rate(self) -> Optional[Decimal]:
        return max(self.rates) if self.intervals else None

    def is_stale(self, now: int) -> bool:
        """Calibration expires; the boundary itself counts as stale."""
        if self.newest_observed_at is None:
            return True
        return (now - self.newest_observed_at) >= STALE_AFTER_SECONDS


def build_profile(points: Sequence[CalibrationPoint]) -> CalibrationProfile:
    """Reduce readings to the intervals that may honestly train a conversion.

    Every reading must describe the same window and meter; a profile spanning two
    of either would average rates that were never comparable. Reset identities
    may differ, because independently observed windows are exactly what turns one
    provisional rate into a range.
    """
    entries = tuple(points)
    for entry in entries:
        if type(entry) is not CalibrationPoint:
            raise AllowanceError(
                REASON_INVALID_POINT,
                "calibration points must be CalibrationPoint, got {0!r}".format(
                    type(entry).__name__
                ),
            )
    if not entries:
        return CalibrationProfile(window="", meter="", intervals=(), newest_observed_at=None)

    windows = {entry.window for entry in entries}
    meters = {entry.meter for entry in entries}
    if len(windows) > 1 or len(meters) > 1:
        raise AllowanceError(
            REASON_MIXED_PROFILE,
            "a profile covers one window and one meter; got windows {0} meters {1}".format(
                sorted(windows), sorted(meters)
            ),
        )

    ordered = _sorted_points(entries)
    intervals = []
    for earlier, later in zip(ordered, ordered[1:]):
        found = _interval(earlier, later)
        if found is not None:
            intervals.append(found)

    newest = max(interval.ended_at for interval in intervals) if intervals else None
    return CalibrationProfile(
        window=ordered[0].window,
        meter=ordered[0].meter,
        intervals=tuple(intervals),
        newest_observed_at=newest,
    )


# --------------------------------------------------------------------------
# Estimate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowanceEstimate:
    """What can honestly be said right now, including that nothing can be."""

    health: str
    reason: str
    window: str
    meter: str
    resets_at: Optional[int] = None
    newest_calibration_at: Optional[int] = None
    interval_count: int = 0
    point_percentage: Optional[Decimal] = None
    lower_percentage: Optional[Decimal] = None
    upper_percentage: Optional[Decimal] = None
    bounded: bool = False

    @property
    def available(self) -> bool:
        return self.health in (HEALTH_PROVISIONAL, HEALTH_CALIBRATED)

    @property
    def confirmed_exhausted(self) -> bool:
        """Always false.

        An estimate reaching the top of the scale means the local model says the
        allowance is spent, not that the provider said so. Only the provider can
        confirm exhaustion, and this module never reads it.
        """
        return False


def _unavailable(reason: str, *, profile: CalibrationProfile, anchor=None) -> AllowanceEstimate:
    return AllowanceEstimate(
        health=HEALTH_UNAVAILABLE,
        reason=reason,
        window=profile.window,
        meter=profile.meter,
        resets_at=anchor.resets_at if anchor is not None else None,
        newest_calibration_at=profile.newest_observed_at,
        interval_count=len(profile.intervals),
    )


def _bounded(value: Decimal) -> Tuple[Decimal, bool]:
    if value < PERCENT_FLOOR:
        return PERCENT_FLOOR, True
    if value > PERCENT_CEILING:
        return PERCENT_CEILING, True
    return value, False


def estimate_current(
    profile: CalibrationProfile,
    anchor: CalibrationPoint,
    *,
    now: int,
    workload_units: object,
) -> AllowanceEstimate:
    """Project the anchor forward by locally observed work, or refuse and say why.

    Never forecasts to the end of the window: that needs a rate over time, which
    one current estimate cannot establish. This answers "how much is spent now",
    and nothing else.
    """
    if type(profile) is not CalibrationProfile or type(anchor) is not CalibrationPoint:
        raise AllowanceError(
            REASON_INVALID_POINT, "an estimate consumes a CalibrationProfile and a CalibrationPoint"
        )
    current_time = _exact_int(now, label="now", reason=REASON_INVALID_EPOCH)
    current_units = _exact_decimal(
        workload_units, label="workload_units", reason=REASON_INVALID_WORKLOAD
    )
    if current_units < 0:
        raise AllowanceError(REASON_INVALID_WORKLOAD, "workload_units is negative")

    if not profile.intervals:
        return _unavailable(REASON_NO_INTERVAL, profile=profile, anchor=anchor)
    if anchor.window != profile.window:
        return _unavailable(REASON_ANCHOR_WINDOW_MISMATCH, profile=profile, anchor=anchor)
    if anchor.meter != profile.meter:
        return _unavailable(REASON_ANCHOR_METER_MISMATCH, profile=profile, anchor=anchor)
    if current_time < anchor.observed_at:
        return _unavailable(REASON_TIME_PRECEDES_ANCHOR, profile=profile, anchor=anchor)
    if current_units < anchor.workload_units:
        return _unavailable(REASON_WORKLOAD_PRECEDES_ANCHOR, profile=profile, anchor=anchor)
    if current_time >= anchor.resets_at:
        # The window turned over. The new one did not necessarily start at zero
        # for this account, and nothing here observed it, so say nothing.
        return _unavailable(REASON_WINDOW_RESET, profile=profile, anchor=anchor)
    if profile.is_stale(current_time):
        return _unavailable(REASON_STALE, profile=profile, anchor=anchor)

    delta = current_units - anchor.workload_units
    if len(profile.intervals) == 1:
        point, bounded = _bounded(anchor.used_percentage + delta * profile.intervals[0].rate)
        return AllowanceEstimate(
            health=HEALTH_PROVISIONAL,
            reason=REASON_PROVISIONAL_SINGLE_INTERVAL,
            window=profile.window,
            meter=profile.meter,
            resets_at=anchor.resets_at,
            newest_calibration_at=profile.newest_observed_at,
            interval_count=1,
            point_percentage=point,
            bounded=bounded,
        )

    lower, lower_bounded = _bounded(anchor.used_percentage + delta * profile.minimum_rate)
    upper, upper_bounded = _bounded(anchor.used_percentage + delta * profile.maximum_rate)
    return AllowanceEstimate(
        health=HEALTH_CALIBRATED,
        reason=REASON_CALIBRATED_RANGE,
        window=profile.window,
        meter=profile.meter,
        resets_at=anchor.resets_at,
        newest_calibration_at=profile.newest_observed_at,
        interval_count=len(profile.intervals),
        lower_percentage=lower,
        upper_percentage=upper,
        bounded=lower_bounded or upper_bounded,
    )
