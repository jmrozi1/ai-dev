"""`claude_allowance` estimates honestly, and refuses rather than guessing."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal, getcontext, ROUND_UP
from pathlib import Path
import ast
import importlib.util
import sys
import unittest

from ai_dev_flow import claude_allowance as allowance
from ai_dev_flow.claude_allowance import (
    STALE_AFTER_SECONDS,
    WINDOWS,
    WINDOW_SECONDS,
    AllowanceError,
    AllowanceEstimate,
    CalibrationPoint,
    CalibrationProfile,
    build_profile,
    estimate_current,
)

WINDOW = "five_hour"
METER = "claude-max-primary"

FIVE_HOUR = 5 * 60 * 60
SEVEN_DAY = 7 * 24 * 60 * 60

# Every fixture epoch is a real one. A window resets within its own length of any
# reading inside it, so `RESET` sits exactly five hours after the opening reading
# and `SECOND_RESET` belongs to a genuinely separate later window. Fixtures that
# could not physically occur are not evidence about behaviour that can.
BASE = 1_700_000_000
RESET = BASE + FIVE_HOUR
SECOND_BASE = BASE + 100_000
SECOND_RESET = SECOND_BASE + FIVE_HOUR


def point(**overrides) -> CalibrationPoint:
    base = dict(
        window=WINDOW,
        observed_at=BASE,
        resets_at=RESET,
        used_percentage=Decimal("10"),
        workload_units=Decimal("100"),
        meter=METER,
        complete_coverage=True,
    )
    base.update(overrides)
    return CalibrationPoint(**base)


def trained_pair(**later_overrides):
    """The accepted shape: an opening reading, then a fully covered second one."""
    first = point(observed_at=BASE, used_percentage=Decimal("10"),
                  workload_units=Decimal("100"), complete_coverage=False)
    later = dict(observed_at=BASE + 1_000, used_percentage=Decimal("30"),
                 workload_units=Decimal("300"), complete_coverage=True)
    later.update(later_overrides)
    return first, point(**later)


def estimate(profile, anchor, *, now, workload_units, covered=True):
    """Every estimate states its own current coverage; there is no default."""
    return estimate_current(profile, anchor, now=now, workload_units=workload_units,
                            complete_coverage_since_anchor=covered)


# --------------------------------------------------------------------------
# Evidence records
# --------------------------------------------------------------------------


class CalibrationPointTests(unittest.TestCase):
    def test_the_accepted_reading_builds(self) -> None:
        entry = point()
        self.assertEqual(entry.window, WINDOW)
        self.assertEqual(entry.reset_identity, (WINDOW, METER, RESET))

    def test_only_the_two_supported_windows_are_accepted(self) -> None:
        self.assertEqual(WINDOWS, ("five_hour", "seven_day"))
        for window in ("five_hour", "seven_day"):
            with self.subTest(window=window):
                self.assertEqual(point(window=window).window, window)
        for window in ("hourly", "monthly", "", None, 5):
            with self.subTest(window=window):
                with self.assertRaises(AllowanceError) as caught:
                    point(window=window)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_WINDOW)

    def test_epochs_must_be_exact_positive_ints(self) -> None:
        for value in (True, False, 1.0, "1000", None, Decimal("1000"), -1, 0):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(observed_at=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_EPOCH)

    def test_a_reading_at_or_after_its_reset_is_refused(self) -> None:
        for observed in (RESET, RESET + 1):
            with self.subTest(observed=observed):
                with self.assertRaises(AllowanceError) as caught:
                    point(observed_at=observed)
                self.assertEqual(caught.exception.reason,
                                 allowance.REASON_OBSERVATION_AFTER_RESET)

    def test_percentage_must_be_exact_and_within_the_scale(self) -> None:
        for value in (Decimal("0"), Decimal("100"), Decimal("33.75"), 42, "12.5"):
            with self.subTest(value=value):
                self.assertIsInstance(point(used_percentage=value).used_percentage, Decimal)
        for value in (Decimal("-0.1"), Decimal("100.1"), -1, 101):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(used_percentage=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_PERCENTAGE)

    def test_a_float_percentage_is_refused_because_it_is_not_exact(self) -> None:
        """The same reading must not produce different arithmetic by spelling."""
        for value in (10.0, 33.75, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(used_percentage=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_PERCENTAGE)

    def test_a_bool_is_never_a_number(self) -> None:
        for field_name, reason in (("used_percentage", allowance.REASON_INVALID_PERCENTAGE),
                                   ("workload_units", allowance.REASON_INVALID_WORKLOAD)):
            for value in (True, False):
                with self.subTest(field=field_name, value=value):
                    with self.assertRaises(AllowanceError) as caught:
                        point(**{field_name: value})
                    self.assertEqual(caught.exception.reason, reason)

    def test_non_finite_and_negative_workload_is_refused(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(workload_units=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_WORKLOAD)
        with self.assertRaises(AllowanceError) as caught:
            point(workload_units=Decimal("-1"))
        self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_WORKLOAD)
        self.assertEqual(point(workload_units=Decimal("0")).workload_units, Decimal("0"))

    def test_the_meter_identity_must_be_exact_non_empty_text(self) -> None:
        for value in ("", "   ", None, 7, True):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(meter=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_METER)

    def test_the_coverage_flag_must_be_an_actual_bool(self) -> None:
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    point(complete_coverage=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_COVERAGE)

    def test_the_reset_must_fit_the_window_the_reading_names(self) -> None:
        """A window is as long as its name says, so its reset cannot be further."""
        self.assertEqual(WINDOW_SECONDS,
                         (("five_hour", 5 * 60 * 60), ("seven_day", 7 * 24 * 60 * 60)))
        for window, horizon in WINDOW_SECONDS:
            with self.subTest(window=window, boundary="exact"):
                entry = point(window=window, observed_at=BASE, resets_at=BASE + horizon)
                self.assertEqual(entry.resets_at - entry.observed_at, horizon)
            with self.subTest(window=window, boundary="one past"):
                with self.assertRaises(AllowanceError) as caught:
                    point(window=window, observed_at=BASE, resets_at=BASE + horizon + 1)
                self.assertEqual(caught.exception.reason,
                                 allowance.REASON_RESET_BEYOND_WINDOW)

    def test_readings_are_immutable(self) -> None:
        with self.assertRaises(Exception):
            point().used_percentage = Decimal("99")


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


class PrivacyTests(unittest.TestCase):
    def test_a_reading_has_nowhere_to_put_identity_or_content(self) -> None:
        self.assertEqual(
            {f.name for f in fields(CalibrationPoint)},
            {"window", "observed_at", "resets_at", "used_percentage", "workload_units",
             "meter", "complete_coverage"},
        )

    def test_no_public_record_carries_account_session_or_provider_content(self) -> None:
        forbidden = ("account", "session", "prompt", "response", "transcript", "credential",
                     "token", "api_key", "cookie", "email", "user", "log", "path")
        for record in (CalibrationPoint, allowance.EvidenceInterval, CalibrationProfile,
                       AllowanceEstimate):
            for name in {f.name for f in fields(record)}:
                for banned in forbidden:
                    with self.subTest(record=record.__name__, field=name, banned=banned):
                        self.assertNotIn(banned, name)

    def test_the_module_imports_only_the_standard_library(self) -> None:
        source = Path(allowance.__file__).read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * (node.level or 0)) + (node.module or ""))
        self.assertEqual(names, {"__future__", "dataclasses", "decimal", "typing"})

    def test_it_works_with_the_agent_sdk_unavailable(self) -> None:
        """The estimator is pure arithmetic; the SDK is a later integration seam.

        The module is executed fresh with `claude_agent_sdk` forced to be
        unimportable, so this proves independence whether or not the SDK happens
        to be installed here. Asserting the environment lacks it would prove only
        that, and would error rather than fail once it is installed.
        """
        absent = object()
        previous = sys.modules.get("claude_agent_sdk", absent)
        sys.modules["claude_agent_sdk"] = None  # any import of it now raises
        try:
            with self.assertRaises(ImportError):
                __import__("claude_agent_sdk")
            name = "_claude_allowance_without_sdk"
            spec = importlib.util.spec_from_file_location(name, Path(allowance.__file__))
            fresh = importlib.util.module_from_spec(spec)
            # `dataclasses` resolves annotations through `sys.modules`, so the
            # fresh copy has to be registered while it executes. It is removed
            # again below; the installed module is never rebound.
            sys.modules[name] = fresh
            try:
                spec.loader.exec_module(fresh)
            finally:
                del sys.modules[name]
            first = fresh.CalibrationPoint(
                window=WINDOW, observed_at=BASE, resets_at=RESET,
                used_percentage=Decimal("10"), workload_units=Decimal("100"),
                meter=METER, complete_coverage=False)
            later = fresh.CalibrationPoint(
                window=WINDOW, observed_at=BASE + 1_000, resets_at=RESET,
                used_percentage=Decimal("30"), workload_units=Decimal("300"),
                meter=METER, complete_coverage=True)
            profile = fresh.build_profile([first, later])
            self.assertEqual(len(profile.intervals), 1)
            self.assertEqual(profile.intervals[0].rate, Decimal("0.1"))
        finally:
            if previous is absent:
                del sys.modules["claude_agent_sdk"]
            else:
                sys.modules["claude_agent_sdk"] = previous


# --------------------------------------------------------------------------
# Training intervals
# --------------------------------------------------------------------------


class CalibrationProfileTests(unittest.TestCase):
    def test_no_evidence_yields_no_interval_and_never_zero(self) -> None:
        empty = build_profile([])
        self.assertEqual(empty.intervals, ())
        self.assertIsNone(empty.minimum_rate)
        self.assertIsNone(empty.maximum_rate)
        self.assertIsNone(empty.newest_observed_at)

    def test_the_interval_rate_is_exact(self) -> None:
        first, later = trained_pair()
        profile = build_profile([first, later])
        self.assertEqual(len(profile.intervals), 1)
        interval = profile.intervals[0]
        self.assertEqual(interval.percentage_delta, Decimal("20"))
        self.assertEqual(interval.units_delta, Decimal("200"))
        self.assertEqual(interval.rate, Decimal("0.1"))
        self.assertIsInstance(interval.rate, Decimal)

    def test_incomplete_coverage_never_trains_a_rate(self) -> None:
        """Use elsewhere raised the provider percentage for work never counted here."""
        first, later = trained_pair(complete_coverage=False)
        self.assertGreater(later.observed_at, first.observed_at)
        self.assertGreater(later.workload_units, first.workload_units)
        self.assertGreater(later.used_percentage, first.used_percentage)
        self.assertEqual(first.reset_identity, later.reset_identity)
        self.assertEqual(build_profile([first, later]).intervals, ())

    def test_two_readings_across_a_reset_do_not_form_an_interval(self) -> None:
        """The crossed reading satisfies every other rule, so only the reset stops it.

        A lower percentage would be refused by the monotonic check instead, and the
        test would pass without exercising reset separation at all.
        """
        first, later = trained_pair()
        crossed = point(observed_at=SECOND_BASE, resets_at=SECOND_RESET,
                        used_percentage=Decimal("40"), workload_units=Decimal("400"))
        self.assertGreater(crossed.observed_at, later.observed_at)
        self.assertGreater(crossed.workload_units, later.workload_units)
        self.assertGreaterEqual(crossed.used_percentage, later.used_percentage)
        self.assertTrue(crossed.complete_coverage)
        self.assertNotEqual(crossed.reset_identity, later.reset_identity)

        profile = build_profile([first, later, crossed])
        self.assertEqual(len(profile.intervals), 1)
        self.assertEqual(profile.intervals[0].resets_at, RESET)

    def test_independently_observed_windows_together_give_a_range(self) -> None:
        first, later = trained_pair()
        other_open = point(observed_at=SECOND_BASE, resets_at=SECOND_RESET,
                           used_percentage=Decimal("5"), workload_units=Decimal("1000"),
                           complete_coverage=False)
        other_close = point(observed_at=SECOND_BASE + 1_000, resets_at=SECOND_RESET,
                            used_percentage=Decimal("15"), workload_units=Decimal("1200"))
        profile = build_profile([first, later, other_open, other_close])
        self.assertEqual(len(profile.intervals), 2)
        self.assertEqual(profile.minimum_rate, Decimal("0.05"))
        self.assertEqual(profile.maximum_rate, Decimal("0.1"))

    def test_a_profile_covers_one_window_and_one_meter(self) -> None:
        first, later = trained_pair()
        for stray in (point(observed_at=BASE + 2_000, window="seven_day"),
                      point(observed_at=BASE + 2_000, meter="another-meter")):
            with self.subTest(stray=stray.window + "/" + stray.meter):
                with self.assertRaises(AllowanceError) as caught:
                    build_profile([first, later, stray])
                self.assertEqual(caught.exception.reason, allowance.REASON_MIXED_PROFILE)

    def test_input_order_does_not_change_the_result(self) -> None:
        first, later = trained_pair()
        third = point(observed_at=BASE + 2_000, used_percentage=Decimal("40"),
                      workload_units=Decimal("400"))
        forward = build_profile([first, later, third])
        backward = build_profile([third, later, first])
        self.assertEqual([i.rate for i in forward.intervals],
                         [i.rate for i in backward.intervals])
        self.assertEqual(forward.newest_observed_at, backward.newest_observed_at)

    def test_two_readings_at_the_same_instant_are_refused(self) -> None:
        """Whichever is the predecessor changes the rate, so neither may be used."""
        first, later = trained_pair()
        conflicting = point(observed_at=BASE + 1_000, used_percentage=Decimal("55"),
                            workload_units=Decimal("900"))
        with self.assertRaises(AllowanceError) as caught:
            build_profile([first, later, conflicting])
        self.assertEqual(caught.exception.reason, allowance.REASON_DUPLICATE_OBSERVATION)

    def test_nonmonotonic_units_do_not_train(self) -> None:
        first, _ = trained_pair()
        for later in (point(observed_at=BASE + 1_000, used_percentage=Decimal("30"),
                            workload_units=Decimal("100")),
                      point(observed_at=BASE + 1_000, used_percentage=Decimal("30"),
                            workload_units=Decimal("50"))):
            with self.subTest(units=str(later.workload_units)):
                # Every other rule holds, so only the workload rule can refuse it.
                self.assertEqual(first.reset_identity, later.reset_identity)
                self.assertTrue(later.complete_coverage)
                self.assertGreater(later.observed_at, first.observed_at)
                self.assertGreater(later.used_percentage, first.used_percentage)
                self.assertEqual(build_profile([first, later]).intervals, ())

    def test_an_unchanged_provider_percentage_trains_nothing(self) -> None:
        """The reproduced defect: 23% -> 23% over 800 units once trained a zero rate.

        The percentage is read by a person from a rounded display, so equality is
        consistent with consumption below the display quantum. It cannot establish
        that the work was free.
        """
        first = point(observed_at=BASE, used_percentage=Decimal("23"),
                      workload_units=Decimal("100"), complete_coverage=False)
        later = point(observed_at=BASE + 1_000, used_percentage=Decimal("23"),
                      workload_units=Decimal("900"), complete_coverage=True)
        # Every other eligibility rule is satisfied, so only the zero delta can refuse.
        self.assertEqual(first.reset_identity, later.reset_identity)
        self.assertTrue(later.complete_coverage)
        self.assertGreater(later.observed_at, first.observed_at)
        self.assertGreater(later.workload_units, first.workload_units)
        self.assertEqual(later.used_percentage, first.used_percentage)

        profile = build_profile([first, later])
        self.assertEqual(profile.intervals, ())
        self.assertIsNone(profile.newest_observed_at)

    def test_a_zero_delta_pair_leaves_the_estimate_unavailable_not_zero(self) -> None:
        first = point(observed_at=BASE, used_percentage=Decimal("23"),
                      workload_units=Decimal("100"), complete_coverage=False)
        later = point(observed_at=BASE + 1_000, used_percentage=Decimal("23"),
                      workload_units=Decimal("900"))
        profile = build_profile([first, later])
        result = estimate(profile, later, now=BASE + 2_000,
                          workload_units=Decimal("100000"))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, allowance.REASON_NO_INTERVAL)
        # Not the anchor value reused, and not an implied zero.
        self.assertIsNone(result.point_percentage)
        self.assertIsNone(result.lower_percentage)
        self.assertIsNone(result.upper_percentage)
        self.assertEqual(result.interval_count, 0)

    def test_the_smallest_exact_positive_delta_still_trains(self) -> None:
        """Strict comparison, with no invented epsilon, floor, or display quantum."""
        first = point(observed_at=BASE, used_percentage=Decimal("23"),
                      workload_units=Decimal("100"), complete_coverage=False)
        later = point(observed_at=BASE + 1_000, used_percentage=Decimal("23.000001"),
                      workload_units=Decimal("900"))
        profile = build_profile([first, later])
        self.assertEqual(len(profile.intervals), 1)
        self.assertEqual(profile.intervals[0].percentage_delta, Decimal("0.000001"))
        self.assertGreater(profile.intervals[0].rate, Decimal("0"))


# --------------------------------------------------------------------------
# Flat readings carry no information, in any position
# --------------------------------------------------------------------------


def _flat_case(position: str):
    """An opening reading, a fully covered flat reading, and a reading that moved.

    `position` places the flat reading before, between, or after the pair that
    actually trains, so exclusion is tested as a property of the reading rather
    than of where it happens to sit.
    """
    opening = point(observed_at=BASE, used_percentage=Decimal("10"),
                    workload_units=Decimal("100"), complete_coverage=False)
    moved = point(observed_at=BASE + 2_000, used_percentage=Decimal("30"),
                  workload_units=Decimal("1000"))
    if position == "between":
        flat = point(observed_at=BASE + 1_000, used_percentage=Decimal("10"),
                     workload_units=Decimal("900"))
    elif position == "after":
        flat = point(observed_at=BASE + 3_000, used_percentage=Decimal("30"),
                     workload_units=Decimal("5000"))
    else:
        raise AssertionError(position)
    return opening, flat, moved


class FlatReadingTests(unittest.TestCase):
    def _assert_inert(self, without, with_flat, flat) -> None:
        self.assertEqual(with_flat.rates, without.rates)
        self.assertEqual(len(with_flat.intervals), len(without.intervals))
        self.assertEqual(with_flat.minimum_rate, without.minimum_rate)
        self.assertEqual(with_flat.maximum_rate, without.maximum_rate)
        self.assertEqual(with_flat.newest_observed_at, without.newest_observed_at)
        self.assertNotEqual(with_flat.newest_observed_at, flat.observed_at)
        boundary = without.newest_observed_at + STALE_AFTER_SECONDS
        self.assertEqual(with_flat.is_stale(boundary), without.is_stale(boundary))
        self.assertEqual(with_flat.is_stale(boundary - 1), without.is_stale(boundary - 1))

    def test_a_flat_reading_is_inert_wherever_it_sits(self) -> None:
        """The review's A -> flat -> C reproduction must equal plain A -> C."""
        for position in ("between", "after"):
            with self.subTest(position=position):
                opening, flat, moved = _flat_case(position)
                # The flat reading satisfies every rule except a percentage move.
                neighbour = opening if position == "between" else moved
                self.assertEqual(flat.reset_identity, neighbour.reset_identity)
                self.assertTrue(flat.complete_coverage)
                self.assertEqual(flat.used_percentage, neighbour.used_percentage)
                self.assertGreater(flat.workload_units, neighbour.workload_units)

                without = build_profile([opening, moved])
                with_flat = build_profile([opening, flat, moved])
                self._assert_inert(without, with_flat, flat)
                self.assertEqual(with_flat.rates, (Decimal("20") / Decimal("900"),))

                anchor = moved
                self.assertEqual(
                    estimate(with_flat, anchor, now=BASE + 4_000,
                             workload_units=Decimal("2000")),
                    estimate(without, anchor, now=BASE + 4_000,
                             workload_units=Decimal("2000")),
                )

    def test_many_flat_readings_stay_inert_while_every_one_is_covered(self) -> None:
        opening = point(observed_at=BASE, used_percentage=Decimal("10"),
                        workload_units=Decimal("100"), complete_coverage=False)
        flats = [point(observed_at=BASE + step, used_percentage=Decimal("10"),
                       workload_units=Decimal(str(100 + step)))
                 for step in (500, 1_000, 1_500)]
        moved = point(observed_at=BASE + 2_000, used_percentage=Decimal("30"),
                      workload_units=Decimal("1000"))
        for flat in flats:
            self.assertTrue(flat.complete_coverage)
            self.assertEqual(flat.used_percentage, opening.used_percentage)

        without = build_profile([opening, moved])
        with_flats = build_profile([opening] + flats + [moved])
        self._assert_inert(without, with_flats, flats[-1])

    def test_an_incomplete_coverage_reading_breaks_the_chain(self) -> None:
        """Unseen use really did happen there, so it is a legitimate boundary."""
        opening = point(observed_at=BASE, used_percentage=Decimal("10"),
                        workload_units=Decimal("100"), complete_coverage=False)
        covered_flat = point(observed_at=BASE + 500, used_percentage=Decimal("10"),
                             workload_units=Decimal("300"))
        uncovered_flat = point(observed_at=BASE + 1_000, used_percentage=Decimal("10"),
                               workload_units=Decimal("600"), complete_coverage=False)
        moved = point(observed_at=BASE + 2_000, used_percentage=Decimal("30"),
                      workload_units=Decimal("1000"))

        chained = build_profile([opening, covered_flat, moved])
        broken = build_profile([opening, covered_flat, uncovered_flat, moved])
        self.assertEqual(chained.rates, (Decimal("20") / Decimal("900"),))
        # The chain restarts at the uncovered reading, so only its span trains.
        self.assertEqual(broken.rates, (Decimal("20") / Decimal("400"),))
        self.assertEqual(len(broken.intervals), 1)
        self.assertEqual(broken.intervals[0].started_at, uncovered_flat.observed_at)

    def test_zero_delta_exclusion_does_not_depend_on_input_order(self) -> None:
        opening, flat, moved = _flat_case("between")
        forward = build_profile([opening, flat, moved])
        shuffled = build_profile([flat, moved, opening])
        self.assertEqual(forward.rates, shuffled.rates)
        self.assertEqual(forward.newest_observed_at, shuffled.newest_observed_at)
        self.assertEqual(len(shuffled.intervals), 1)

    def test_a_percentage_that_falls_inside_one_window_is_refused(self) -> None:
        """Contradictory evidence, not a pair to skip and carry on from."""
        opening = point(observed_at=BASE, used_percentage=Decimal("10"),
                        workload_units=Decimal("100"), complete_coverage=False)
        fell = point(observed_at=BASE + 1_000, used_percentage=Decimal("5"),
                     workload_units=Decimal("900"))
        moved = point(observed_at=BASE + 2_000, used_percentage=Decimal("30"),
                      workload_units=Decimal("1000"))
        self.assertEqual(fell.reset_identity, opening.reset_identity)
        self.assertLess(fell.used_percentage, opening.used_percentage)
        for entries in ([opening, fell], [opening, fell, moved]):
            with self.subTest(count=len(entries)):
                with self.assertRaises(AllowanceError) as caught:
                    build_profile(entries)
                self.assertEqual(caught.exception.reason,
                                 allowance.REASON_PERCENTAGE_DECREASED)

    def test_a_lower_reading_in_a_later_window_is_not_a_decrease(self) -> None:
        """A new window legitimately starts lower; only one window is monotonic."""
        first, later = trained_pair()
        fresh_window = point(observed_at=SECOND_BASE, resets_at=SECOND_RESET,
                             used_percentage=Decimal("5"), workload_units=Decimal("400"),
                             complete_coverage=False)
        self.assertLess(fresh_window.used_percentage, later.used_percentage)
        self.assertNotEqual(fresh_window.reset_identity, later.reset_identity)
        self.assertEqual(len(build_profile([first, later, fresh_window]).intervals), 1)

    def test_a_non_point_input_is_refused(self) -> None:
        with self.assertRaises(AllowanceError) as caught:
            build_profile([point(), object()])
        self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_POINT)


# --------------------------------------------------------------------------
# Arithmetic
# --------------------------------------------------------------------------


class ArithmeticTests(unittest.TestCase):
    """The same evidence must produce the same numbers whatever else shares the process."""

    def _repeating(self):
        """A rate of 20/900, which no finite decimal represents exactly."""
        opening = point(observed_at=BASE, used_percentage=Decimal("10"),
                        workload_units=Decimal("100"), complete_coverage=False)
        moved = point(observed_at=BASE + 1_000, used_percentage=Decimal("30"),
                      workload_units=Decimal("1000"))
        second_open = point(observed_at=SECOND_BASE, resets_at=SECOND_RESET,
                            used_percentage=Decimal("5"), workload_units=Decimal("2000"),
                            complete_coverage=False)
        second_close = point(observed_at=SECOND_BASE + 1_000, resets_at=SECOND_RESET,
                             used_percentage=Decimal("12"), workload_units=Decimal("2300"))
        return opening, moved, second_open, second_close

    def _observe(self):
        opening, moved, second_open, second_close = self._repeating()
        provisional_profile = build_profile([opening, moved])
        calibrated_profile = build_profile([opening, moved, second_open, second_close])
        provisional = estimate(provisional_profile, moved, now=BASE + 2_000,
                               workload_units=Decimal("1700"))
        calibrated = estimate(calibrated_profile, moved, now=BASE + 2_000,
                              workload_units=Decimal("1700"))
        self.assertEqual(provisional.health, allowance.HEALTH_PROVISIONAL)
        self.assertEqual(calibrated.health, allowance.HEALTH_CALIBRATED)
        return provisional_profile.rates, provisional, calibrated_profile.rates, calibrated

    def test_results_ignore_the_ambient_decimal_context(self) -> None:
        expected = self._observe()
        # A non-terminating rate is the only case where precision can show.
        self.assertNotEqual(expected[0][0], expected[0][0].quantize(Decimal("0.0001")))
        context = getcontext()
        original_precision, original_rounding = context.prec, context.rounding
        try:
            for precision, rounding in ((3, ROUND_UP), (6, ROUND_UP), (50, ROUND_UP)):
                with self.subTest(prec=precision, rounding=rounding):
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(self._observe(), expected)
        finally:
            context.prec = original_precision
            context.rounding = original_rounding

    def test_the_same_inputs_always_produce_the_same_result(self) -> None:
        self.assertEqual(self._observe(), self._observe())


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


def _stale_fixture():
    """Old calibration, and a fresh anchor in a window that has not reset.

    Both facts are needed at once: staleness is measured from the newest trained
    observation, but the estimate refuses a window that already reset first, so
    the anchor has to belong to a live window well over a month later.
    """
    first, later = trained_pair()
    profile = build_profile([first, later])
    anchor_at = later.observed_at + STALE_AFTER_SECONDS
    anchor = point(observed_at=anchor_at, resets_at=anchor_at + FIVE_HOUR,
                   used_percentage=Decimal("30"), workload_units=Decimal("300"))
    return profile, anchor


class FreshnessTests(unittest.TestCase):
    def test_the_thirty_five_day_boundary_is_exact(self) -> None:
        profile, _ = _stale_fixture()
        self.assertEqual(STALE_AFTER_SECONDS, 35 * 24 * 60 * 60)
        newest = profile.newest_observed_at
        self.assertFalse(profile.is_stale(newest + STALE_AFTER_SECONDS - 1))
        self.assertTrue(profile.is_stale(newest + STALE_AFTER_SECONDS))

    def test_stale_calibration_produces_no_estimate(self) -> None:
        profile, anchor = _stale_fixture()
        now = anchor.observed_at
        # The window is live, so only staleness can refuse this.
        self.assertLess(now, anchor.resets_at)
        self.assertEqual(now - profile.newest_observed_at, STALE_AFTER_SECONDS)
        result = estimate(profile, anchor, now=now, workload_units=Decimal("500"))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, allowance.REASON_STALE)
        self.assertIsNone(result.point_percentage)

    def test_an_empty_profile_is_stale_rather_than_fresh(self) -> None:
        self.assertTrue(build_profile([]).is_stale(BASE))

    def test_public_time_fails_closed(self) -> None:
        """A malformed clock must never come back as fresh, or as a vaguer refusal."""
        profile, anchor = _stale_fixture()
        for now in (True, False, 1.0, "3000", None, 0, -1):
            with self.subTest(now=now, surface="is_stale"):
                with self.assertRaises(AllowanceError) as caught:
                    profile.is_stale(now)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_EPOCH)
            with self.subTest(now=now, surface="estimate_current"):
                with self.assertRaises(AllowanceError) as caught:
                    estimate(profile, anchor, now=now, workload_units=Decimal("500"))
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_EPOCH)


# --------------------------------------------------------------------------
# Current estimate
# --------------------------------------------------------------------------


class CurrentEstimateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first, self.anchor = trained_pair()
        self.profile = build_profile([self.first, self.anchor])
        self.now = BASE + 2_000

    def test_one_interval_gives_an_explicitly_provisional_point(self) -> None:
        result = estimate(self.profile, self.anchor, now=self.now,
                          workload_units=Decimal("500"))
        self.assertTrue(result.available)
        self.assertEqual(result.health, allowance.HEALTH_PROVISIONAL)
        self.assertEqual(result.reason, allowance.REASON_PROVISIONAL_SINGLE_INTERVAL)
        self.assertEqual(result.interval_count, 1)
        self.assertEqual(result.point_percentage, Decimal("50.0"))
        self.assertIsNone(result.lower_percentage)
        self.assertIsNone(result.upper_percentage)

    def test_two_intervals_give_the_observed_min_max_range_and_no_statistics(self) -> None:
        other_open = point(observed_at=SECOND_BASE, resets_at=SECOND_RESET,
                           used_percentage=Decimal("5"), workload_units=Decimal("1000"),
                           complete_coverage=False)
        other_close = point(observed_at=SECOND_BASE + 1_000, resets_at=SECOND_RESET,
                            used_percentage=Decimal("15"), workload_units=Decimal("1200"))
        profile = build_profile([self.first, self.anchor, other_open, other_close])
        result = estimate(profile, self.anchor, now=self.now, workload_units=Decimal("500"))
        self.assertEqual(result.health, allowance.HEALTH_CALIBRATED)
        self.assertEqual(result.reason, allowance.REASON_CALIBRATED_RANGE)
        self.assertEqual(result.interval_count, 2)
        self.assertEqual(result.lower_percentage, Decimal("40.00"))
        self.assertEqual(result.upper_percentage, Decimal("50.0"))
        self.assertIsNone(result.point_percentage)
        for name in {f.name for f in fields(AllowanceEstimate)}:
            self.assertNotIn("confidence", name)
            self.assertNotIn("stddev", name)

    def test_no_interval_means_unavailable_not_zero(self) -> None:
        bare = build_profile([self.first])
        result = estimate(bare, self.first, now=BASE + 1_000, workload_units=Decimal("500"))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, allowance.REASON_NO_INTERVAL)
        self.assertIsNone(result.point_percentage)
        self.assertIsNone(result.lower_percentage)
        self.assertIsNone(result.upper_percentage)

    def test_the_anchor_must_match_the_profile_window_and_meter(self) -> None:
        other_window = point(window="seven_day", observed_at=BASE + 1_500)
        self.assertEqual(
            estimate(self.profile, other_window, now=self.now,
                     workload_units=Decimal("500")).reason,
            allowance.REASON_ANCHOR_WINDOW_MISMATCH)
        other_meter = point(meter="another-meter", observed_at=BASE + 1_500)
        self.assertEqual(
            estimate(self.profile, other_meter, now=self.now,
                     workload_units=Decimal("500")).reason,
            allowance.REASON_ANCHOR_METER_MISMATCH)

    def test_time_and_workload_may_not_precede_the_anchor(self) -> None:
        self.assertEqual(
            estimate(self.profile, self.anchor, now=BASE + 500,
                     workload_units=Decimal("500")).reason,
            allowance.REASON_TIME_PRECEDES_ANCHOR)
        self.assertEqual(
            estimate(self.profile, self.anchor, now=self.now,
                     workload_units=Decimal("50")).reason,
            allowance.REASON_WORKLOAD_PRECEDES_ANCHOR)

    def test_after_the_reset_it_refuses_rather_than_assuming_a_fresh_zero(self) -> None:
        for now in (RESET, RESET + 1):
            with self.subTest(now=now):
                result = estimate(self.profile, self.anchor, now=now,
                                  workload_units=Decimal("500"))
                self.assertFalse(result.available)
                self.assertEqual(result.reason, allowance.REASON_WINDOW_RESET)
                self.assertIsNone(result.point_percentage)

    def test_a_reset_window_is_named_before_stale_calibration(self) -> None:
        """Both are true here; the window being gone is the more specific fact."""
        now = self.anchor.observed_at + STALE_AFTER_SECONDS
        self.assertGreaterEqual(now, self.anchor.resets_at)
        self.assertTrue(self.profile.is_stale(now))
        self.assertEqual(
            estimate(self.profile, self.anchor, now=now,
                     workload_units=Decimal("500")).reason,
            allowance.REASON_WINDOW_RESET)

    def test_current_coverage_must_be_stated_and_is_never_inferred(self) -> None:
        """The anchor's own flag describes the span before it, not the span since."""
        covered = estimate(self.profile, self.anchor, now=self.now,
                           workload_units=Decimal("500"), covered=True)
        self.assertTrue(covered.available)
        self.assertEqual(covered.point_percentage, Decimal("50.0"))

        uncovered = estimate(self.profile, self.anchor, now=self.now,
                             workload_units=Decimal("500"), covered=False)
        self.assertFalse(uncovered.available)
        self.assertEqual(uncovered.reason, allowance.REASON_CURRENT_COVERAGE_INCOMPLETE)
        self.assertIsNone(uncovered.point_percentage)
        self.assertIsNone(uncovered.lower_percentage)
        self.assertIsNone(uncovered.upper_percentage)

        # An anchor that itself closed a fully covered span still has to be told.
        self.assertTrue(self.anchor.complete_coverage)
        self.assertEqual(
            estimate(self.profile, self.anchor, now=self.now,
                     workload_units=Decimal("500"), covered=False).reason,
            allowance.REASON_CURRENT_COVERAGE_INCOMPLETE)

    def test_current_coverage_has_no_default_and_must_be_a_bool(self) -> None:
        with self.assertRaises(TypeError):
            estimate_current(self.profile, self.anchor, now=self.now,
                             workload_units=Decimal("500"))
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceError) as caught:
                    estimate_current(self.profile, self.anchor, now=self.now,
                                     workload_units=Decimal("500"),
                                     complete_coverage_since_anchor=value)
                self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_COVERAGE)

    def test_no_delta_returns_the_anchor_value_itself(self) -> None:
        result = estimate(self.profile, self.anchor, now=self.anchor.observed_at,
                          workload_units=self.anchor.workload_units)
        self.assertEqual(result.point_percentage, self.anchor.used_percentage)

    def test_an_estimate_at_the_ceiling_is_still_only_an_estimate(self) -> None:
        result = estimate(self.profile, self.anchor, now=self.now,
                          workload_units=Decimal("5000"))
        self.assertEqual(result.point_percentage, Decimal("100"))
        self.assertTrue(result.bounded)
        self.assertTrue(result.available)
        self.assertFalse(result.confirmed_exhausted)
        self.assertNotIn("exhaust", result.reason)

    def test_the_result_carries_its_own_provenance(self) -> None:
        result = estimate(self.profile, self.anchor, now=self.now,
                          workload_units=Decimal("500"))
        self.assertEqual(result.window, WINDOW)
        self.assertEqual(result.meter, METER)
        self.assertEqual(result.resets_at, RESET)
        self.assertEqual(result.newest_calibration_at, self.profile.newest_observed_at)

    def test_it_never_forecasts_to_the_end_of_the_window(self) -> None:
        """Time-based projection needs a rate over time this cannot establish."""
        names = {f.name for f in fields(AllowanceEstimate)}
        for forecast in ("projected", "forecast", "end_of_window", "eta", "burn_rate"):
            self.assertNotIn(forecast, names)
        source = Path(allowance.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def project", source)

    def test_invalid_current_inputs_are_refused_outright(self) -> None:
        for units in (float("nan"), -1, True):
            with self.subTest(units=units):
                with self.assertRaises(AllowanceError):
                    estimate(self.profile, self.anchor, now=self.now, workload_units=units)
        with self.assertRaises(AllowanceError):
            estimate(object(), self.anchor, now=self.now, workload_units=Decimal("500"))

if __name__ == "__main__":
    unittest.main()
