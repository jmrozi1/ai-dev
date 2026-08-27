"""`claude_allowance` estimates honestly, and refuses rather than guessing."""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path
import ast
import unittest

from ai_dev_flow import claude_allowance as allowance
from ai_dev_flow.claude_allowance import (
    STALE_AFTER_SECONDS,
    WINDOWS,
    AllowanceError,
    AllowanceEstimate,
    CalibrationPoint,
    CalibrationProfile,
    build_profile,
    estimate_current,
)

WINDOW = "five_hour"
METER = "claude-max-primary"
RESET = 10 ** 9
FAR_RESET = 2 * 10 ** 9


def point(**overrides) -> CalibrationPoint:
    base = dict(
        window=WINDOW,
        observed_at=1_000,
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
    first = point(observed_at=1_000, used_percentage=Decimal("10"),
                  workload_units=Decimal("100"), complete_coverage=False)
    later = dict(observed_at=2_000, used_percentage=Decimal("30"),
                 workload_units=Decimal("300"), complete_coverage=True)
    later.update(later_overrides)
    return first, point(**later)


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

    def test_it_works_without_the_agent_sdk_installed(self) -> None:
        """The estimator is pure arithmetic; the SDK is a later integration seam."""
        with self.assertRaises(ImportError):
            __import__("claude_agent_sdk")
        first, later = trained_pair()
        profile = build_profile([first, later])
        self.assertEqual(len(profile.intervals), 1)


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
        self.assertEqual(build_profile([first, later]).intervals, ())

    def test_two_readings_across_a_reset_do_not_form_an_interval(self) -> None:
        """The crossed reading satisfies every other rule, so only the reset stops it.

        A lower percentage would be refused by the monotonic check instead, and the
        test would pass without exercising reset separation at all.
        """
        first, later = trained_pair()
        crossed = point(observed_at=3_000, resets_at=FAR_RESET,
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
        other_open = point(observed_at=3_000, resets_at=FAR_RESET,
                           used_percentage=Decimal("5"), workload_units=Decimal("1000"),
                           complete_coverage=False)
        other_close = point(observed_at=4_000, resets_at=FAR_RESET,
                            used_percentage=Decimal("15"), workload_units=Decimal("1200"))
        profile = build_profile([first, later, other_open, other_close])
        self.assertEqual(len(profile.intervals), 2)
        self.assertEqual(profile.minimum_rate, Decimal("0.05"))
        self.assertEqual(profile.maximum_rate, Decimal("0.1"))

    def test_a_profile_covers_one_window_and_one_meter(self) -> None:
        first, later = trained_pair()
        for stray in (point(observed_at=3_000, window="seven_day"),
                      point(observed_at=3_000, meter="another-meter")):
            with self.subTest(stray=stray.window + "/" + stray.meter):
                with self.assertRaises(AllowanceError) as caught:
                    build_profile([first, later, stray])
                self.assertEqual(caught.exception.reason, allowance.REASON_MIXED_PROFILE)

    def test_input_order_does_not_change_the_result(self) -> None:
        first, later = trained_pair()
        third = point(observed_at=3_000, used_percentage=Decimal("40"),
                      workload_units=Decimal("400"))
        forward = build_profile([first, later, third])
        backward = build_profile([third, later, first])
        self.assertEqual([i.rate for i in forward.intervals],
                         [i.rate for i in backward.intervals])
        self.assertEqual(forward.newest_observed_at, backward.newest_observed_at)

    def test_two_readings_at_the_same_instant_are_refused(self) -> None:
        """Whichever is the predecessor changes the rate, so neither may be used."""
        first, later = trained_pair()
        conflicting = point(observed_at=2_000, used_percentage=Decimal("55"),
                            workload_units=Decimal("900"))
        with self.assertRaises(AllowanceError) as caught:
            build_profile([first, later, conflicting])
        self.assertEqual(caught.exception.reason, allowance.REASON_DUPLICATE_OBSERVATION)

    def test_nonmonotonic_units_or_percentage_do_not_train(self) -> None:
        first, _ = trained_pair()
        for later in (point(observed_at=2_000, used_percentage=Decimal("30"),
                            workload_units=Decimal("100")),
                      point(observed_at=2_000, used_percentage=Decimal("30"),
                            workload_units=Decimal("50")),
                      point(observed_at=2_000, used_percentage=Decimal("5"),
                            workload_units=Decimal("300"))):
            with self.subTest(units=str(later.workload_units),
                              pct=str(later.used_percentage)):
                self.assertEqual(build_profile([first, later]).intervals, ())

    def test_a_non_point_input_is_refused(self) -> None:
        with self.assertRaises(AllowanceError) as caught:
            build_profile([point(), object()])
        self.assertEqual(caught.exception.reason, allowance.REASON_INVALID_POINT)


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        first, later = trained_pair(resets_at=FAR_RESET)
        self.anchor = later
        self.profile = build_profile([point(observed_at=1_000, resets_at=FAR_RESET,
                                            complete_coverage=False), later])

    def test_the_thirty_five_day_boundary_is_exact(self) -> None:
        self.assertEqual(STALE_AFTER_SECONDS, 35 * 24 * 60 * 60)
        newest = self.profile.newest_observed_at
        self.assertFalse(self.profile.is_stale(newest + STALE_AFTER_SECONDS - 1))
        self.assertTrue(self.profile.is_stale(newest + STALE_AFTER_SECONDS))

    def test_stale_calibration_produces_no_estimate(self) -> None:
        newest = self.profile.newest_observed_at
        result = estimate_current(self.profile, self.anchor,
                                  now=newest + STALE_AFTER_SECONDS,
                                  workload_units=Decimal("500"))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, allowance.REASON_STALE)
        self.assertIsNone(result.point_percentage)

    def test_an_empty_profile_is_stale_rather_than_fresh(self) -> None:
        self.assertTrue(build_profile([]).is_stale(1_000))


# --------------------------------------------------------------------------
# Current estimate
# --------------------------------------------------------------------------


class CurrentEstimateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first, self.anchor = trained_pair()
        self.profile = build_profile([self.first, self.anchor])

    def test_one_interval_gives_an_explicitly_provisional_point(self) -> None:
        result = estimate_current(self.profile, self.anchor, now=3_000,
                                  workload_units=Decimal("500"))
        self.assertTrue(result.available)
        self.assertEqual(result.health, allowance.HEALTH_PROVISIONAL)
        self.assertEqual(result.reason, allowance.REASON_PROVISIONAL_SINGLE_INTERVAL)
        self.assertEqual(result.interval_count, 1)
        self.assertEqual(result.point_percentage, Decimal("50.0"))
        self.assertIsNone(result.lower_percentage)
        self.assertIsNone(result.upper_percentage)

    def test_two_intervals_give_the_observed_min_max_range_and_no_statistics(self) -> None:
        other_open = point(observed_at=3_000, resets_at=FAR_RESET,
                           used_percentage=Decimal("5"), workload_units=Decimal("1000"),
                           complete_coverage=False)
        other_close = point(observed_at=4_000, resets_at=FAR_RESET,
                            used_percentage=Decimal("15"), workload_units=Decimal("1200"))
        profile = build_profile([self.first, self.anchor, other_open, other_close])
        result = estimate_current(profile, self.anchor, now=5_000,
                                  workload_units=Decimal("500"))
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
        result = estimate_current(bare, self.first, now=2_000, workload_units=Decimal("500"))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, allowance.REASON_NO_INTERVAL)
        self.assertIsNone(result.point_percentage)
        self.assertIsNone(result.lower_percentage)
        self.assertIsNone(result.upper_percentage)

    def test_the_anchor_must_match_the_profile_window_and_meter(self) -> None:
        other_window = point(window="seven_day", observed_at=2_500)
        self.assertEqual(
            estimate_current(self.profile, other_window, now=3_000,
                             workload_units=Decimal("500")).reason,
            allowance.REASON_ANCHOR_WINDOW_MISMATCH)
        other_meter = point(meter="another-meter", observed_at=2_500)
        self.assertEqual(
            estimate_current(self.profile, other_meter, now=3_000,
                             workload_units=Decimal("500")).reason,
            allowance.REASON_ANCHOR_METER_MISMATCH)

    def test_time_and_workload_may_not_precede_the_anchor(self) -> None:
        self.assertEqual(
            estimate_current(self.profile, self.anchor, now=1_500,
                             workload_units=Decimal("500")).reason,
            allowance.REASON_TIME_PRECEDES_ANCHOR)
        self.assertEqual(
            estimate_current(self.profile, self.anchor, now=3_000,
                             workload_units=Decimal("50")).reason,
            allowance.REASON_WORKLOAD_PRECEDES_ANCHOR)

    def test_after_the_reset_it_refuses_rather_than_assuming_a_fresh_zero(self) -> None:
        for now in (RESET, RESET + 1):
            with self.subTest(now=now):
                result = estimate_current(self.profile, self.anchor, now=now,
                                          workload_units=Decimal("500"))
                self.assertFalse(result.available)
                self.assertEqual(result.reason, allowance.REASON_WINDOW_RESET)
                self.assertIsNone(result.point_percentage)

    def test_no_delta_returns_the_anchor_value_itself(self) -> None:
        result = estimate_current(self.profile, self.anchor, now=2_000,
                                  workload_units=self.anchor.workload_units)
        self.assertEqual(result.point_percentage, self.anchor.used_percentage)

    def test_an_estimate_at_the_ceiling_is_still_only_an_estimate(self) -> None:
        result = estimate_current(self.profile, self.anchor, now=3_000,
                                  workload_units=Decimal("5000"))
        self.assertEqual(result.point_percentage, Decimal("100"))
        self.assertTrue(result.bounded)
        self.assertTrue(result.available)
        self.assertFalse(result.confirmed_exhausted)
        self.assertNotIn("exhaust", result.reason)

    def test_the_result_carries_its_own_provenance(self) -> None:
        result = estimate_current(self.profile, self.anchor, now=3_000,
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

    def test_the_same_inputs_always_produce_the_same_result(self) -> None:
        first = estimate_current(self.profile, self.anchor, now=3_000,
                                 workload_units=Decimal("500"))
        second = estimate_current(self.profile, self.anchor, now=3_000,
                                  workload_units=Decimal("500"))
        self.assertEqual(first, second)

    def test_invalid_current_inputs_are_refused_outright(self) -> None:
        for now in (True, 1.0, "3000", None):
            with self.subTest(now=now):
                with self.assertRaises(AllowanceError):
                    estimate_current(self.profile, self.anchor, now=now,
                                     workload_units=Decimal("500"))
        for units in (float("nan"), -1, True):
            with self.subTest(units=units):
                with self.assertRaises(AllowanceError):
                    estimate_current(self.profile, self.anchor, now=3_000,
                                     workload_units=units)
        with self.assertRaises(AllowanceError):
            estimate_current(object(), self.anchor, now=3_000, workload_units=Decimal("500"))


if __name__ == "__main__":
    unittest.main()
