"""`claude_allowance_view` shows what is true, says why when nothing is, and never guesses."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import ast
import dataclasses
import json
import tempfile
import unittest

from ai_dev_flow import claude_allowance_view as view_module
from ai_dev_flow.claude_allowance import (
    HEALTH_CALIBRATED,
    HEALTH_PROVISIONAL,
    HEALTH_UNAVAILABLE,
    REASON_CURRENT_COVERAGE_INCOMPLETE,
    REASON_NO_INTERVAL,
    REASON_PERCENTAGE_DECREASED,
    REASON_STALE,
    REASON_WINDOW_RESET,
    AllowanceError,
)
from ai_dev_flow.claude_allowance_store import (
    CURRENT_METER,
    AllowanceStore,
    AllowanceStoreError,
    REASON_INVALID_WINDOW,
    REASON_LOCK_LOST,
    REASON_LOCK_MALFORMED,
    REASON_MALFORMED_STORE,
    REASON_METER_MISMATCH,
    REASON_OBSERVATION_OUT_OF_ORDER,
    REASON_RESET_EPOCH_REGRESSED,
    REASON_STORE_LOCKED,
)
from ai_dev_flow.claude_allowance_view import (
    REASON_INVALID_COVERAGE_ASSERTION,
    REASON_INVALID_READING,
    REASON_NO_ANCHOR,
    AllowanceViewError,
    AllowanceWindowView,
    project_window,
    record_usage_reading,
)
from ai_dev_flow.claude_runtime import RuntimeResult

FIVE_HOUR_SECONDS = 5 * 60 * 60
SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60
STALE_SECONDS = 35 * 24 * 60 * 60
BASE = 1_700_000_000
RESET = BASE + FIVE_HOUR_SECONDS
SEVEN_RESET = BASE + SEVEN_DAY_SECONDS


def result(cost) -> RuntimeResult:
    """A reduced runtime result; only its cost is ever read."""
    return RuntimeResult(
        session_id="11111111-2222-3333-4444-555555555555",
        mode="launch",
        subtype="success",
        is_error=False,
        num_turns=1,
        total_cost_usd=cost,
    )


class ViewTestCase(unittest.TestCase):
    """Every fixture builds its own store; none reaches into another's."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="allowance-view-"))
        self.addCleanup(self._remove_root)
        self.path = self.root / "workload.json"
        self.store = AllowanceStore(self.path)

    def _remove_root(self) -> None:
        for item in sorted(self.root.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.root.rmdir()

    # -- fixture builders -------------------------------------------------

    def spend(self, cost, key: str) -> None:
        self.store.record_result(result(cost), idempotency_key=key)

    def read(self, offset: int, percentage: str, *, human: bool = True) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE + offset,
            five_hour=(RESET, Decimal(percentage)),
            human_complete_coverage=human,
        )

    def one_interval(self) -> None:
        """Two covered readings that moved, so exactly one rate is trained."""
        self.spend(1.0, "k1")
        self.read(0, "10")
        self.spend(2.0, "k2")
        self.read(60, "30")

    def two_intervals(self) -> None:
        self.one_interval()
        self.spend(1.0, "k3")
        self.read(120, "45")

    def project(self, *, window: str = "five_hour", now: int = BASE + 180, human: bool = True):
        return project_window(
            self.store,
            window=window,
            now=now,
            human_complete_coverage_since_anchor=human,
        )

    def payload(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. Nothing to project from is said out loud, not rendered as zero
# --------------------------------------------------------------------------


class NoAnchorTests(ViewTestCase):
    def test_an_unread_window_is_unavailable_and_source_healthy(self) -> None:
        view = self.project()
        self.assertEqual(view.health, HEALTH_UNAVAILABLE)
        self.assertEqual(view.reason, REASON_NO_ANCHOR)
        self.assertIs(view.source_healthy, True)

    def test_it_names_the_window_and_meter_that_were_asked_for(self) -> None:
        self.assertEqual(self.project(window="seven_day").window, "seven_day")
        self.assertEqual(self.project(window="seven_day").meter, CURRENT_METER)
        self.assertEqual(self.project(window="five_hour").window, "five_hour")

    def test_no_percentage_is_invented(self) -> None:
        view = self.project()
        self.assertIsNone(view.point_percentage)
        self.assertIsNone(view.lower_percentage)
        self.assertIsNone(view.upper_percentage)
        self.assertIsNone(view.resets_at)
        self.assertIsNone(view.newest_calibration_at)
        self.assertEqual(view.interval_count, 0)
        self.assertIs(view.bounded, False)

    def test_zero_is_not_the_same_answer_as_nothing(self) -> None:
        """A rendered 0% would say the allowance is untouched, which is not known."""
        self.assertNotEqual(self.project().point_percentage, Decimal("0"))

    def test_one_window_being_read_leaves_the_other_unanchored(self) -> None:
        self.read(0, "10")
        self.assertEqual(self.project(window="seven_day").reason, REASON_NO_ANCHOR)
        self.assertNotEqual(self.project(window="five_hour").reason, REASON_NO_ANCHOR)

    def test_without_an_anchor_the_clock_is_never_consulted(self) -> None:
        """No reading means no projection, whatever time the caller thinks it is."""
        view = project_window(
            self.store,
            window="five_hour",
            now="not-a-clock",
            human_complete_coverage_since_anchor=True,
        )
        self.assertEqual(view.reason, REASON_NO_ANCHOR)
        self.assertIs(view.source_healthy, True)


# --------------------------------------------------------------------------
# 2. An available estimate is copied exactly, never recomputed
# --------------------------------------------------------------------------


class AvailableProjectionTests(ViewTestCase):
    def test_one_interval_projects_a_provisional_point(self) -> None:
        self.one_interval()
        view = self.project()
        self.assertEqual(view.health, HEALTH_PROVISIONAL)
        self.assertEqual(view.point_percentage, Decimal("30"))
        self.assertEqual(view.interval_count, 1)
        self.assertEqual(view.resets_at, RESET)
        self.assertEqual(view.newest_calibration_at, BASE + 60)
        self.assertIs(view.bounded, False)
        self.assertIs(view.source_healthy, True)
        self.assertIsNone(view.lower_percentage)
        self.assertIsNone(view.upper_percentage)

    def test_two_intervals_project_a_calibrated_range(self) -> None:
        self.two_intervals()
        self.spend(2.0, "k4")
        view = self.project()
        self.assertEqual(view.health, HEALTH_CALIBRATED)
        self.assertEqual(view.interval_count, 2)
        self.assertIsNone(view.point_percentage)
        self.assertLess(view.lower_percentage, view.upper_percentage)

    def test_every_estimator_field_is_carried_through_unchanged(self) -> None:
        """The view is a copy, not a second opinion about the same evidence."""
        from ai_dev_flow.claude_allowance import estimate_current

        self.two_intervals()
        self.spend(2.0, "k4")
        inputs = self.store.projection_inputs("five_hour")
        estimate = estimate_current(
            inputs.profile,
            inputs.anchor,
            now=BASE + 180,
            workload_units=inputs.workload_units,
            complete_coverage_since_anchor=True,
        )
        view = self.project()
        for field in (
            "health",
            "reason",
            "point_percentage",
            "lower_percentage",
            "upper_percentage",
            "bounded",
            "resets_at",
            "newest_calibration_at",
            "interval_count",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(view, field), getattr(estimate, field))

    def test_exact_decimals_survive_with_no_rounding(self) -> None:
        """Presentation granularity belongs to a later rail, and rounding is one-way."""
        self.spend(1.0, "k1")
        self.read(0, "10")
        self.spend(3.0, "k2")
        self.read(60, "17")
        self.spend(1.0, "k3")
        view = self.project()
        # 17 + 1 * (7/3): a repeating decimal the estimator computed once.
        self.assertEqual(view.point_percentage, Decimal("17") + Decimal(7) / Decimal(3))
        self.assertNotEqual(view.point_percentage, Decimal("19"))
        self.assertGreater(len(str(view.point_percentage).split(".")[1]), 6)

    def test_a_clamped_projection_is_not_confirmed_exhaustion(self) -> None:
        self.spend(1.0, "k1")
        self.read(0, "10")
        self.spend(1.0, "k2")
        self.read(60, "60")
        self.spend(5.0, "k3")
        view = self.project(now=BASE + 120)
        self.assertEqual(view.point_percentage, Decimal("100"))
        self.assertIs(view.bounded, True)
        self.assertEqual(view.health, HEALTH_PROVISIONAL)
        self.assertNotEqual(view.health, HEALTH_UNAVAILABLE)

    def test_the_view_carries_no_exhausted_field_at_all(self) -> None:
        """There is nowhere to put a claim only the provider could make."""
        self.one_interval()
        view = self.project()
        self.assertFalse(hasattr(view, "confirmed_exhausted"))
        names = {field.name for field in dataclasses.fields(view)}
        self.assertEqual(
            names & {"confirmed_exhausted", "exhausted", "remaining_tokens", "label"},
            set(),
        )


# --------------------------------------------------------------------------
# 3-5. Coverage is a conjunction, and both halves carry weight
# --------------------------------------------------------------------------


class CoverageConjunctionTests(ViewTestCase):
    def test_a_clean_ledger_and_a_truthful_human_project(self) -> None:
        self.one_interval()
        self.assertEqual(self.project(human=True).health, HEALTH_PROVISIONAL)

    def test_a_human_who_cannot_assert_coverage_gets_nothing(self) -> None:
        """The ledger is spotless; the human is the only thing that changed."""
        self.one_interval()
        view = self.project(human=False)
        self.assertEqual(view.health, HEALTH_UNAVAILABLE)
        self.assertEqual(view.reason, REASON_CURRENT_COVERAGE_INCOMPLETE)
        self.assertIsNone(view.point_percentage)

    def test_a_holed_ledger_beats_a_truthful_human(self) -> None:
        """A result recorded without a cost is work this manager cannot weigh."""
        self.one_interval()
        self.spend(None, "k-hole")
        view = self.project(human=True)
        self.assertEqual(view.health, HEALTH_UNAVAILABLE)
        self.assertEqual(view.reason, REASON_CURRENT_COVERAGE_INCOMPLETE)

    def test_each_half_alone_is_enough_to_withhold_the_number(self) -> None:
        """Neither input is decorative: flipping either one alone changes the answer."""
        self.one_interval()
        clean_and_asserted = self.project(human=True)
        clean_not_asserted = self.project(human=False)
        self.assertEqual(clean_and_asserted.health, HEALTH_PROVISIONAL)
        self.assertEqual(clean_not_asserted.health, HEALTH_UNAVAILABLE)

        self.spend(None, "k-hole")
        holed_and_asserted = self.project(human=True)
        holed_not_asserted = self.project(human=False)
        self.assertEqual(holed_and_asserted.health, HEALTH_UNAVAILABLE)
        self.assertEqual(holed_not_asserted.health, HEALTH_UNAVAILABLE)

    def test_coverage_is_never_inferred_from_the_ledger_alone(self) -> None:
        """A store this manager filled itself still cannot vouch for the human half."""
        self.one_interval()
        inputs = self.store.projection_inputs("five_hour")
        self.assertIs(inputs.ledger_clean_since_anchor, True)
        self.assertEqual(self.project(human=False).reason, REASON_CURRENT_COVERAGE_INCOMPLETE)

    def test_the_assertion_is_required_and_has_no_default(self) -> None:
        with self.assertRaises(TypeError):
            project_window(self.store, window="five_hour", now=BASE)


# --------------------------------------------------------------------------
# 6. Blank profile identity cannot leak into the view
# --------------------------------------------------------------------------


class ViewIdentityTests(ViewTestCase):
    def test_an_empty_profile_reports_a_blank_window_and_meter(self) -> None:
        """The condition the view has to survive, stated as a fact about the source."""
        inputs = self.store.projection_inputs("seven_day")
        self.assertEqual(inputs.profile.window, "")
        self.assertEqual(inputs.profile.meter, "")

    def test_the_view_still_names_what_it_describes(self) -> None:
        for window in ("five_hour", "seven_day"):
            with self.subTest(window=window):
                view = self.project(window=window)
                self.assertEqual(view.window, window)
                self.assertEqual(view.meter, CURRENT_METER)
                self.assertNotEqual(view.window, "")
                self.assertNotEqual(view.meter, "")

    def test_identity_survives_a_source_refusal_too(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        view = self.project(window="seven_day")
        self.assertEqual(view.window, "seven_day")
        self.assertEqual(view.meter, CURRENT_METER)


# --------------------------------------------------------------------------
# 7. A readable source that cannot help says exactly why
# --------------------------------------------------------------------------


class ReadableButUnavailableTests(ViewTestCase):
    def test_a_window_that_turned_over_is_not_a_source_failure(self) -> None:
        self.one_interval()
        view = self.project(now=RESET)
        self.assertEqual(view.reason, REASON_WINDOW_RESET)
        self.assertIs(view.source_healthy, True)
        self.assertIsNone(view.point_percentage)

    def test_stale_calibration_is_not_a_source_failure(self) -> None:
        """A fresh reading in a new window over calibration nobody has refreshed."""
        self.one_interval()
        later = BASE + 60 + STALE_SECONDS
        record_usage_reading(
            self.store,
            observed_at=later,
            five_hour=(later + 3600, Decimal("5")),
            human_complete_coverage=True,
        )
        view = self.project(now=later + 60)
        self.assertEqual(view.reason, REASON_STALE)
        self.assertIs(view.source_healthy, True)
        self.assertEqual(view.newest_calibration_at, BASE + 60)

    def test_a_single_reading_trains_nothing_yet(self) -> None:
        self.read(0, "10")
        view = self.project(now=BASE + 60)
        self.assertEqual(view.reason, REASON_NO_INTERVAL)
        self.assertIs(view.source_healthy, True)
        self.assertEqual(view.interval_count, 0)
        self.assertEqual(view.resets_at, RESET)

    def test_no_readable_unavailable_reason_implies_exhaustion(self) -> None:
        """Unavailable means 'cannot say', which is not 'spent'."""
        self.one_interval()
        for now, human in ((RESET, True), (BASE + 180, False)):
            with self.subTest(now=now, human=human):
                view = self.project(now=now, human=human)
                self.assertEqual(view.health, HEALTH_UNAVAILABLE)
                self.assertIsNone(view.point_percentage)
                self.assertIs(view.bounded, False)
                self.assertIs(view.source_healthy, True)

    def test_a_bounded_hundred_is_reported_as_an_available_estimate(self) -> None:
        """Bounded is the local model clamping, not the provider confirming."""
        self.spend(1.0, "k1")
        self.read(0, "10")
        self.spend(1.0, "k2")
        self.read(60, "60")
        self.spend(5.0, "k3")
        view = self.project(now=BASE + 120)
        self.assertIs(view.source_healthy, True)
        self.assertIs(view.bounded, True)
        self.assertEqual(view.health, HEALTH_PROVISIONAL)


# --------------------------------------------------------------------------
# 8. A source that refuses is never a number
# --------------------------------------------------------------------------


class SourceUnhealthyTests(ViewTestCase):
    def assert_unhealthy(self, view, reason) -> None:
        self.assertIs(view.source_healthy, False)
        self.assertEqual(view.reason, reason)
        self.assertEqual(view.health, HEALTH_UNAVAILABLE)
        self.assertIsNone(view.point_percentage)
        self.assertIsNone(view.lower_percentage)
        self.assertIsNone(view.upper_percentage)
        self.assertNotEqual(view.point_percentage, Decimal("0"))

    def test_an_unreadable_store_is_reported_not_raised(self) -> None:
        self.one_interval()
        self.path.write_text("{not json", encoding="utf-8")
        self.assert_unhealthy(self.project(), REASON_MALFORMED_STORE)

    def test_a_store_written_for_another_meter_is_refused(self) -> None:
        self.one_interval()
        payload = self.payload()
        payload["meter"] = "some-other-meter-v9"
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.assert_unhealthy(self.project(), REASON_METER_MISMATCH)

    def test_an_edited_total_contradicting_its_ledger_is_refused(self) -> None:
        self.one_interval()
        payload = self.payload()
        payload["workloadUnits"] = "999.00"
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.assert_unhealthy(self.project(), REASON_MALFORMED_STORE)

    def test_contradicting_readings_make_the_whole_profile_unusable(self) -> None:
        self.spend(1.0, "k1")
        self.read(0, "50")
        self.spend(1.0, "k2")
        self.read(60, "20")
        self.assert_unhealthy(self.project(now=BASE + 120), REASON_PERCENTAGE_DECREASED)

    def test_a_refusal_never_reaches_a_render_caller(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        view = self.project()
        self.assertIsInstance(view, AllowanceWindowView)

    def test_source_unhealthy_is_distinguishable_from_readable_unavailable(self) -> None:
        """Both are unavailable; only one of them means the evidence could not be read."""
        self.one_interval()
        readable = self.project(human=False)
        self.path.write_text("{not json", encoding="utf-8")
        unreadable = self.project(human=False)
        self.assertEqual(readable.health, unreadable.health)
        self.assertIs(readable.source_healthy, True)
        self.assertIs(unreadable.source_healthy, False)
        self.assertNotEqual(readable.reason, unreadable.reason)

    def test_an_unusable_clock_cannot_crash_a_render(self) -> None:
        self.one_interval()
        view = project_window(
            self.store,
            window="five_hour",
            now="not-a-clock",
            human_complete_coverage_since_anchor=True,
        )
        self.assertIs(view.source_healthy, False)
        self.assertIsNone(view.point_percentage)

    def test_an_unknown_window_is_the_accepted_store_refusal(self) -> None:
        """No duplicate window check here: the store already owns that refusal."""
        view = self.project(window="ten_minute")
        self.assert_unhealthy(view, REASON_INVALID_WINDOW)
        self.assertEqual(view.window, "ten_minute")


# --------------------------------------------------------------------------
# 9. One projection reads exactly one generation
# --------------------------------------------------------------------------


class SingleGenerationStore:
    """Every accepted read except `projection_inputs` is a defect here."""

    def __init__(self, inner: AllowanceStore) -> None:
        self.inner = inner
        self.windows = []

    def projection_inputs(self, window):
        self.windows.append(window)
        return self.inner.projection_inputs(window)

    def __getattr__(self, name):
        raise AssertionError("the projection read {0}".format(name))


class RacingStore(SingleGenerationStore):
    """A second generation would disagree with the first, so reading one at all fails."""

    def projection_inputs(self, window):
        self.windows.append(window)
        if len(self.windows) > 1:
            raise AssertionError("a second store generation was read")
        inputs = self.inner.projection_inputs(window)
        # Between two reads, a result lands. A composed projection would now pair
        # this newer total with the older cleanliness flag.
        self.inner.record_result(result(9.0), idempotency_key="k-race")
        return inputs


class OneGenerationTests(ViewTestCase):
    def test_projection_inputs_is_called_exactly_once(self) -> None:
        self.one_interval()
        counting = SingleGenerationStore(self.store)
        view = project_window(
            counting,
            window="five_hour",
            now=BASE + 180,
            human_complete_coverage_since_anchor=True,
        )
        self.assertEqual(counting.windows, ["five_hour"])
        self.assertEqual(view.health, HEALTH_PROVISIONAL)

    def test_no_other_store_read_is_composed_in(self) -> None:
        """`profile`, `latest_observation` and `workload_units` are three generations."""
        self.one_interval()
        counting = SingleGenerationStore(self.store)
        project_window(
            counting,
            window="five_hour",
            now=BASE + 180,
            human_complete_coverage_since_anchor=True,
        )
        for name in ("profile", "latest_observation", "workload_units", "observations"):
            with self.subTest(name=name):
                with self.assertRaises(AssertionError):
                    getattr(counting, name)

    def test_a_result_landing_mid_render_cannot_be_read_into_the_view(self) -> None:
        self.one_interval()
        racing = RacingStore(self.store)
        view = project_window(
            racing,
            window="five_hour",
            now=BASE + 180,
            human_complete_coverage_since_anchor=True,
        )
        self.assertEqual(len(racing.windows), 1)
        self.assertEqual(view.point_percentage, Decimal("30"))


# --------------------------------------------------------------------------
# 10. The assertion is checked before the store is touched
# --------------------------------------------------------------------------


class ExplodingStore:
    def __getattr__(self, name):
        raise AssertionError("the store was touched: {0}".format(name))


class CoverageAssertionTests(ViewTestCase):
    def test_a_non_bool_assertion_refuses_before_any_read(self) -> None:
        for value in (1, 0, "true", "", None, [], Decimal("1")):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceViewError) as caught:
                    project_window(
                        ExplodingStore(),
                        window="five_hour",
                        now=BASE,
                        human_complete_coverage_since_anchor=value,
                    )
                self.assertEqual(caught.exception.reason, REASON_INVALID_COVERAGE_ASSERTION)

    def test_a_truthy_one_is_not_an_assertion(self) -> None:
        """`1` is what a form field yields; asserting coverage must be deliberate."""
        self.one_interval()
        with self.assertRaises(AllowanceViewError):
            self.project(human=1)
        self.assertEqual(self.project(human=True).health, HEALTH_PROVISIONAL)

    def test_the_refusal_carries_a_stable_reason(self) -> None:
        with self.assertRaises(AllowanceViewError) as caught:
            project_window(
                ExplodingStore(),
                window="five_hour",
                now=BASE,
                human_complete_coverage_since_anchor="yes",
            )
        self.assertEqual(caught.exception.reason, "invalid-coverage-assertion")
        self.assertIn("human_complete_coverage_since_anchor", caught.exception.detail)


# --------------------------------------------------------------------------
# 11-13. What was submitted is what is written
# --------------------------------------------------------------------------


class SubmittedWindowTests(ViewTestCase):
    def recorded_windows(self):
        return [entry["window"] for entry in self.payload()["observations"]]

    def test_both_windows_of_one_view(self) -> None:
        points = record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        self.assertEqual(sorted(points), ["five_hour", "seven_day"])
        self.assertEqual(points["five_hour"].used_percentage, Decimal("10"))
        self.assertEqual(points["seven_day"].used_percentage, Decimal("4"))

    def test_five_hour_only(self) -> None:
        points = record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.assertEqual(list(points), ["five_hour"])
        self.assertEqual(self.recorded_windows(), ["five_hour"])
        self.assertIsNone(self.store.latest_observation("seven_day"))

    def test_seven_day_only(self) -> None:
        points = record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        self.assertEqual(list(points), ["seven_day"])
        self.assertEqual(self.recorded_windows(), ["seven_day"])
        self.assertIsNone(self.store.latest_observation("five_hour"))

    def test_neither_window_writes_nothing_at_all(self) -> None:
        points = record_usage_reading(
            self.store, observed_at=BASE, human_complete_coverage=True
        )
        self.assertEqual(points, {})
        self.assertFalse(self.path.exists())

    def test_an_omitted_window_is_never_filled_in_from_the_other(self) -> None:
        """The two windows measure different spans; one is not evidence for the other."""
        record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("77")),
            human_complete_coverage=True,
        )
        self.assertEqual(self.store.observations("seven_day"), ())
        for entry in self.payload()["observations"]:
            self.assertNotEqual(entry["window"], "seven_day")

    def test_both_readings_of_one_view_share_an_instant(self) -> None:
        points = record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        self.assertEqual(points["five_hour"].observed_at, BASE)
        self.assertEqual(points["seven_day"].observed_at, BASE)

    def test_the_append_order_is_five_hour_then_seven_day(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        self.assertEqual(self.recorded_windows(), ["five_hour", "seven_day"])

    def test_the_order_holds_when_the_keywords_are_given_the_other_way_round(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("4")),
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.assertEqual(self.recorded_windows(), ["five_hour", "seven_day"])


# --------------------------------------------------------------------------
# 14. One human flag, two windows, and a store that can still say no
# --------------------------------------------------------------------------


class ReadingCoverageTests(ViewTestCase):
    def submit(self, *, human):
        return record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=human,
        )

    def test_the_same_assertion_reaches_both_windows(self) -> None:
        self.assertEqual(
            [entry["humanCoverage"] for entry in self.payload_after(self.submit(human=False))],
            [False, False],
        )

    def payload_after(self, _points):
        return self.payload()["observations"]

    def test_a_truthful_human_is_recorded_as_asserted(self) -> None:
        self.submit(human=True)
        self.assertEqual(
            [entry["humanCoverage"] for entry in self.payload()["observations"]],
            [True, True],
        )

    def test_a_holed_ledger_overrides_a_truthful_human_on_both_windows(self) -> None:
        self.store.record_result(result(None), idempotency_key="k-hole")
        points = self.submit(human=True)
        self.assertIs(points["five_hour"].complete_coverage, False)
        self.assertIs(points["seven_day"].complete_coverage, False)
        self.assertEqual(
            [entry["humanCoverage"] for entry in self.payload()["observations"]],
            [True, True],
        )

    def test_a_clean_ledger_and_a_truthful_human_record_complete_coverage(self) -> None:
        points = self.submit(human=True)
        self.assertIs(points["five_hour"].complete_coverage, True)
        self.assertIs(points["seven_day"].complete_coverage, True)

    def test_the_holes_are_per_window_spans_the_store_owns(self) -> None:
        """A hole after the five-hour anchor only, so the two windows disagree."""
        record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.store.record_result(result(None), idempotency_key="k-hole")
        points = record_usage_reading(
            self.store,
            observed_at=BASE + 60,
            five_hour=(RESET, Decimal("20")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        self.assertIs(points["five_hour"].complete_coverage, False)
        self.assertIs(points["seven_day"].complete_coverage, False)

    def test_a_non_bool_assertion_refuses_before_any_write(self) -> None:
        for value in (1, "yes", None, 0):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceViewError) as caught:
                    record_usage_reading(
                        ExplodingStore(),
                        observed_at=BASE,
                        five_hour=(RESET, Decimal("10")),
                        human_complete_coverage=value,
                    )
                self.assertEqual(caught.exception.reason, REASON_INVALID_COVERAGE_ASSERTION)
        self.assertFalse(self.path.exists())

    def test_the_assertion_is_required_and_has_no_default(self) -> None:
        with self.assertRaises(TypeError):
            record_usage_reading(
                self.store, observed_at=BASE, five_hour=(RESET, Decimal("10"))
            )


# --------------------------------------------------------------------------
# 15. Every accepted refusal still refuses, with its own reason
# --------------------------------------------------------------------------


class CarriedRefusalTests(ViewTestCase):
    def submit(self, offset, resets_at, percentage, *, window="five_hour"):
        return record_usage_reading(
            self.store,
            observed_at=BASE + offset,
            human_complete_coverage=True,
            **{window: (resets_at, percentage)}
        )

    def test_a_reset_that_moved_backwards_is_refused(self) -> None:
        self.submit(0, RESET, Decimal("10"))
        with self.assertRaises(AllowanceStoreError) as caught:
            self.submit(60, RESET - 1, Decimal("12"))
        self.assertEqual(caught.exception.reason, REASON_RESET_EPOCH_REGRESSED)

    def test_a_reading_that_does_not_advance_is_refused(self) -> None:
        self.submit(0, RESET, Decimal("10"))
        with self.assertRaises(AllowanceStoreError) as caught:
            self.submit(0, RESET, Decimal("12"))
        self.assertEqual(caught.exception.reason, REASON_OBSERVATION_OUT_OF_ORDER)

    def test_a_float_percentage_is_refused_by_the_accepted_point(self) -> None:
        with self.assertRaises(AllowanceError) as caught:
            self.submit(0, RESET, 12.5)
        self.assertEqual(caught.exception.reason, "invalid-percentage")

    def test_a_percentage_outside_the_scale_is_refused(self) -> None:
        with self.assertRaises(AllowanceError) as caught:
            self.submit(0, RESET, Decimal("101"))
        self.assertEqual(caught.exception.reason, "invalid-percentage")

    def test_a_reset_beyond_the_named_window_is_refused(self) -> None:
        with self.assertRaises(AllowanceError) as caught:
            self.submit(0, BASE + SEVEN_DAY_SECONDS, Decimal("10"))
        self.assertEqual(caught.exception.reason, "reset-beyond-named-window")

    def test_a_corrupt_store_refuses_the_write(self) -> None:
        self.submit(0, RESET, Decimal("10"))
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(AllowanceStoreError) as caught:
            self.submit(60, RESET, Decimal("20"))
        self.assertEqual(caught.exception.reason, REASON_MALFORMED_STORE)

    def test_a_malformed_pair_is_this_module_own_refusal(self) -> None:
        for shape in ((RESET,), (RESET, Decimal("1"), 3), "ab", {"a": 1}, 5, ()):
            with self.subTest(shape=shape):
                with self.assertRaises(AllowanceViewError) as caught:
                    record_usage_reading(
                        self.store,
                        observed_at=BASE,
                        five_hour=shape,
                        human_complete_coverage=True,
                    )
                self.assertEqual(caught.exception.reason, REASON_INVALID_READING)
        self.assertFalse(self.path.exists())

    def test_a_malformed_second_pair_refuses_before_the_first_is_written(self) -> None:
        """Shape is checked for both windows before either one is appended."""
        with self.assertRaises(AllowanceViewError):
            record_usage_reading(
                self.store,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET,),
                human_complete_coverage=True,
            )
        self.assertFalse(self.path.exists())

    def test_no_correction_or_deletion_surface_exists(self) -> None:
        public = {name for name in dir(view_module) if not name.startswith("_")}
        for forbidden in ("correct", "delete", "migrate", "backfill", "amend", "purge"):
            with self.subTest(forbidden=forbidden):
                self.assertEqual(
                    {name for name in public if forbidden in name.lower()}, set()
                )


# --------------------------------------------------------------------------
# 16-18. A lost lock is reconciled, never guessed
# --------------------------------------------------------------------------


class LockLostAfterWriteStore:
    """The accepted store releases in a `finally`, so this is what that looks like."""

    def __init__(self, inner: AllowanceStore, *, latest_error=None, distort=None) -> None:
        self.inner = inner
        self.latest_error = latest_error
        self.distort = distort
        self.appends = 0
        self.latest_reads = 0

    def append_observation(self, **kwargs):
        self.appends += 1
        landed = dict(kwargs)
        if self.distort is not None:
            landed.update(self.distort)
        self.inner.append_observation(**landed)
        raise AllowanceStoreError(REASON_LOCK_LOST, "lock replaced before release")

    def latest_observation(self, window):
        self.latest_reads += 1
        if self.latest_error is not None:
            raise self.latest_error
        return self.inner.latest_observation(window)


class LockLostBeforeWriteStore:
    """The refusal surfaced, and nothing landed."""

    def __init__(self, inner: AllowanceStore) -> None:
        self.inner = inner
        self.latest_reads = 0

    def append_observation(self, **kwargs):
        raise AllowanceStoreError(REASON_LOCK_LOST, "lock vanished before release")

    def latest_observation(self, window):
        self.latest_reads += 1
        return self.inner.latest_observation(window)


class HeldLockStore:
    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.latest_reads = 0

    def append_observation(self, **kwargs):
        raise AllowanceStoreError(self.reason, "the lock is not ours")

    def latest_observation(self, window):
        self.latest_reads += 1
        raise AssertionError("a byte-unchanged store was reconciled")


class LockLostTests(ViewTestCase):
    def test_an_exactly_matching_durable_reading_is_accepted(self) -> None:
        lossy = LockLostAfterWriteStore(self.store)
        points = record_usage_reading(
            lossy,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.assertEqual(points["five_hour"].used_percentage, Decimal("10"))
        self.assertEqual(points["five_hour"].observed_at, BASE)
        self.assertEqual(points["five_hour"].resets_at, RESET)

    def test_the_append_is_never_replayed(self) -> None:
        lossy = LockLostAfterWriteStore(self.store)
        record_usage_reading(
            lossy,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.assertEqual(lossy.appends, 1)
        self.assertEqual(len(self.store.observations("five_hour")), 1)

    def test_the_durable_history_is_read_exactly_once(self) -> None:
        lossy = LockLostAfterWriteStore(self.store)
        record_usage_reading(
            lossy,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            human_complete_coverage=True,
        )
        self.assertEqual(lossy.latest_reads, 1)

    def test_an_int_percentage_reconciles_against_the_stored_decimal(self) -> None:
        lossy = LockLostAfterWriteStore(self.store)
        points = record_usage_reading(
            lossy, observed_at=BASE, five_hour=(RESET, 10), human_complete_coverage=True
        )
        self.assertEqual(points["five_hour"].used_percentage, Decimal("10"))

    def test_an_absent_reading_re_raises_the_original_refusal(self) -> None:
        missing = LockLostBeforeWriteStore(self.store)
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                missing,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_LOCK_LOST)
        self.assertEqual(missing.latest_reads, 1)
        self.assertIsNone(self.store.latest_observation("five_hour"))

    def test_a_different_percentage_is_not_this_call_write(self) -> None:
        lossy = LockLostAfterWriteStore(self.store, distort={"used_percentage": Decimal("99")})
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                lossy,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_LOCK_LOST)

    def test_a_different_observation_time_is_not_this_call_write(self) -> None:
        lossy = LockLostAfterWriteStore(self.store, distort={"observed_at": BASE + 5})
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                lossy,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_LOCK_LOST)

    def test_a_different_reset_time_is_not_this_call_write(self) -> None:
        lossy = LockLostAfterWriteStore(self.store, distort={"resets_at": RESET - 5})
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                lossy,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_LOCK_LOST)

    def test_an_unreadable_history_re_raises_the_original_refusal(self) -> None:
        lossy = LockLostAfterWriteStore(
            self.store, latest_error=AllowanceStoreError(REASON_MALFORMED_STORE, "edited")
        )
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                lossy,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_LOCK_LOST)

    def test_a_held_or_malformed_lock_is_never_treated_as_landed(self) -> None:
        for reason in (REASON_STORE_LOCKED, REASON_LOCK_MALFORMED):
            with self.subTest(reason=reason):
                held = HeldLockStore(reason)
                with self.assertRaises(AllowanceStoreError) as caught:
                    record_usage_reading(
                        held,
                        observed_at=BASE,
                        five_hour=(RESET, Decimal("10")),
                        human_complete_coverage=True,
                    )
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(held.latest_reads, 0)

    def test_only_the_lost_lock_is_reconciled(self) -> None:
        """Contention leaves the store byte-unchanged; a lost lock may not have."""
        held = HeldLockStore(REASON_STORE_LOCKED)
        with self.assertRaises(AllowanceStoreError):
            record_usage_reading(
                held,
                observed_at=BASE,
                five_hour=(RESET, Decimal("10")),
                human_complete_coverage=True,
            )
        self.assertFalse(self.path.exists())


# --------------------------------------------------------------------------
# 19. A partial write stays truthful and is never claimed as a whole one
# --------------------------------------------------------------------------


class PartialWriteTests(ViewTestCase):
    def test_a_landed_five_hour_survives_a_refused_seven_day(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        with self.assertRaises(AllowanceStoreError) as caught:
            record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET - 1, Decimal("11")),
                human_complete_coverage=True,
            )
        self.assertEqual(caught.exception.reason, REASON_RESET_EPOCH_REGRESSED)

        five = self.store.observations("five_hour")
        self.assertEqual(len(five), 1)
        self.assertEqual(five[0].used_percentage, Decimal("10"))
        self.assertEqual(five[0].observed_at, BASE + 60)

    def test_nothing_is_rolled_back(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        with self.assertRaises(AllowanceStoreError):
            record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET - 1, Decimal("11")),
                human_complete_coverage=True,
            )
        self.assertEqual(
            [entry["window"] for entry in self.payload()["observations"]],
            ["seven_day", "five_hour"],
        )

    def test_the_refused_window_recorded_nothing(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        with self.assertRaises(AllowanceStoreError):
            record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET - 1, Decimal("11")),
                human_complete_coverage=True,
            )
        seven = self.store.observations("seven_day")
        self.assertEqual(len(seven), 1)
        self.assertEqual(seven[0].used_percentage, Decimal("9"))

    def test_no_partial_result_is_returned_as_a_whole_one(self) -> None:
        """The refusal propagates; there is no mapping a caller could mistake for success."""
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        returned = None
        try:
            returned = record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET - 1, Decimal("11")),
                human_complete_coverage=True,
            )
        except AllowanceStoreError:
            pass
        self.assertIsNone(returned)

    def test_a_refused_five_hour_stops_the_seven_day_append(self) -> None:
        """Fixed order, so the first refusal is the whole story for the rest."""
        record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        with self.assertRaises(AllowanceStoreError):
            record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET - 1, Decimal("10")),
                seven_day=(SEVEN_RESET, Decimal("4")),
                human_complete_coverage=True,
            )
        self.assertEqual(self.store.observations("seven_day"), ())

    def test_the_surviving_reading_is_visible_to_a_projection(self) -> None:
        """It is durable evidence, not a phantom: the next read sees it."""
        record_usage_reading(
            self.store,
            observed_at=BASE,
            seven_day=(SEVEN_RESET, Decimal("9")),
            human_complete_coverage=True,
        )
        with self.assertRaises(AllowanceStoreError):
            record_usage_reading(
                self.store,
                observed_at=BASE + 60,
                five_hour=(RESET, Decimal("10")),
                seven_day=(SEVEN_RESET - 1, Decimal("11")),
                human_complete_coverage=True,
            )
        anchor = self.store.projection_inputs("five_hour").anchor
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.used_percentage, Decimal("10"))


# --------------------------------------------------------------------------
# 20. This rail added a module and changed nothing else
# --------------------------------------------------------------------------


class BoundaryTests(ViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module_path = Path(view_module.__file__)
        self.source = self.module_path.read_text(encoding="utf-8")

    def test_the_view_field_set_is_exactly_the_display_contract(self) -> None:
        self.assertEqual(
            [field.name for field in dataclasses.fields(AllowanceWindowView)],
            [
                "window",
                "meter",
                "health",
                "reason",
                "point_percentage",
                "lower_percentage",
                "upper_percentage",
                "bounded",
                "resets_at",
                "newest_calibration_at",
                "interval_count",
                "source_healthy",
            ],
        )

    def test_the_view_is_frozen(self) -> None:
        view = self.project()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.health = HEALTH_PROVISIONAL

    def test_it_imports_only_the_accepted_seams(self) -> None:
        """No queue, no page, no server, no provider, no runtime."""
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * (node.level or 0)) + (node.module or ""))
        self.assertEqual(
            names,
            {
                "__future__",
                "dataclasses",
                "decimal",
                "typing",
                ".claude_allowance",
                ".claude_allowance_store",
            },
        )

    def test_it_touches_no_display_or_transport_surface(self) -> None:
        used = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
        for forbidden in ("build_payload", "QueueView", "SelectedDetail", "render", "html"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, used)

    def test_the_public_surface_is_exactly_what_was_declared(self) -> None:
        self.assertEqual(
            sorted(view_module.__all__),
            [
                "AllowanceViewError",
                "AllowanceWindowView",
                "REASON_INVALID_COVERAGE_ASSERTION",
                "REASON_INVALID_READING",
                "REASON_NO_ANCHOR",
                "project_window",
                "record_usage_reading",
            ],
        )

    def test_there_is_exactly_one_projection_entry_point(self) -> None:
        """A second one would let a caller pick the one without the assertion."""
        defined = {
            node.name
            for node in ast.parse(self.source).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
        self.assertEqual(
            {name for name in defined if "project" in name.lower()}, {"project_window"}
        )

    def test_the_store_schema_is_unchanged_by_this_module(self) -> None:
        record_usage_reading(
            self.store,
            observed_at=BASE,
            five_hour=(RESET, Decimal("10")),
            seven_day=(SEVEN_RESET, Decimal("4")),
            human_complete_coverage=True,
        )
        payload = self.payload()
        self.assertEqual(
            sorted(payload),
            ["meter", "observations", "results", "version", "workloadUnits"],
        )
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["meter"], CURRENT_METER)
        for entry in payload["observations"]:
            self.assertEqual(
                sorted(entry),
                [
                    "completeCoverage",
                    "humanCoverage",
                    "ledgerOrdinal",
                    "observedAt",
                    "resetsAt",
                    "usedPercentage",
                    "window",
                    "workloadUnits",
                ],
            )

    def test_the_accepted_estimate_still_reports_no_confirmed_exhaustion(self) -> None:
        from ai_dev_flow.claude_allowance import estimate_current

        self.one_interval()
        inputs = self.store.projection_inputs("five_hour")
        estimate = estimate_current(
            inputs.profile,
            inputs.anchor,
            now=BASE + 180,
            workload_units=inputs.workload_units,
            complete_coverage_since_anchor=True,
        )
        self.assertIs(estimate.confirmed_exhausted, False)

    def test_it_parses_under_the_minimum_python(self) -> None:
        for path in (self.module_path, Path(__file__).resolve()):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_postponed_annotations_stay_in_the_header(self) -> None:
        for path in (self.module_path, Path(__file__).resolve()):
            with self.subTest(path=path.name):
                header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
                self.assertIn("from __future__ import annotations", header)


if __name__ == "__main__":
    unittest.main()
