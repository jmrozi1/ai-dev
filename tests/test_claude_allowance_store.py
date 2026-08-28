"""`claude_allowance_store` records evidence exactly once and never invents any."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import ast
import json
import os
import tempfile
import unittest

from ai_dev_flow import claude_allowance_store as store_module
from ai_dev_flow.claude_allowance import (
    AllowanceError,
    CalibrationPoint,
    build_profile,
    estimate_current,
)
from ai_dev_flow.claude_allowance_store import (
    CURRENT_METER,
    SCHEMA_VERSION,
    AllowanceStore,
    AllowanceStoreError,
    allowance_store_path,
    result_workload,
)
from ai_dev_flow.claude_runtime import RuntimeResult

FIVE_HOUR = 5 * 60 * 60
SEVEN_DAY = 7 * 24 * 60 * 60
BASE = 1_700_000_000
RESET = BASE + FIVE_HOUR
SEVEN_RESET = BASE + SEVEN_DAY


def result(cost, *, is_error: bool = False) -> RuntimeResult:
    """A reduced runtime result; only its cost is ever read."""
    return RuntimeResult(
        session_id="11111111-2222-3333-4444-555555555555",
        mode="launch",
        subtype="error" if is_error else "success",
        is_error=is_error,
        num_turns=1,
        total_cost_usd=cost,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="allowance-store-"))
        self.addCleanup(self._remove_root)
        self.path = self.root / "workload.json"
        self.store = AllowanceStore(self.path)

    def _remove_root(self) -> None:
        for item in sorted(self.root.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.root.rmdir()

    def payload(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def rewrite(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        self.path.write_text(text, encoding="utf-8")

    def observe(self, offset: int, percentage: str, *, human: bool = True, window="five_hour"):
        return self.store.append_observation(
            window=window,
            observed_at=BASE + offset,
            resets_at=RESET,
            used_percentage=Decimal(percentage),
            human_complete_coverage=human,
        )


# --------------------------------------------------------------------------
# The one conversion boundary
# --------------------------------------------------------------------------


class ConversionTests(StoreTestCase):
    def test_the_runtime_float_converts_through_its_decimal_string(self) -> None:
        """`Decimal(value)` would bake in binary noise nobody reported."""
        self.assertNotEqual(Decimal(0.1), Decimal("0.1"))
        self.assertEqual(result_workload(0.1), Decimal("0.1"))
        self.assertNotEqual(result_workload(0.1), Decimal(0.1))
        for value, expected in ((0.3, "0.3"), (1.005, "1.005"), (2, "2"), (0.0, "0.0")):
            with self.subTest(value=value):
                self.assertEqual(result_workload(value), Decimal(expected))

    def test_a_missing_cost_is_not_a_number_at_all(self) -> None:
        self.assertIsNone(result_workload(None))

    def test_costs_that_are_not_exact_non_negative_numbers_are_refused(self) -> None:
        for value in (True, False, "0.1", float("nan"), float("inf"), -0.5, Decimal("1")):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceStoreError) as caught:
                    result_workload(value)
                self.assertEqual(caught.exception.reason, store_module.REASON_INVALID_COST)

    def test_only_a_reduced_runtime_result_is_a_workload_event(self) -> None:
        for value in (object(), {"total_cost_usd": 0.1}, None, 0.1):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.record_result(value, idempotency_key="k1")
                self.assertEqual(caught.exception.reason, store_module.REASON_INVALID_RESULT)


# --------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------


class AccumulationTests(StoreTestCase):
    def test_an_absent_store_has_recorded_nothing(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.workload_units(), Decimal("0"))

    def test_each_result_contributes_its_own_cost_once(self) -> None:
        self.assertEqual(self.store.record_result(result(0.1), idempotency_key="a"),
                         Decimal("0.1"))
        self.assertEqual(self.store.record_result(result(0.2), idempotency_key="b"),
                         Decimal("0.3"))
        self.assertEqual(self.store.workload_units(), Decimal("0.3"))

    def test_an_error_result_with_a_numeric_cost_still_counts(self) -> None:
        """Unsuccessful work still consumed allowance."""
        self.store.record_result(result(0.1), idempotency_key="ok")
        failed = result(0.25, is_error=True)
        self.assertTrue(failed.is_error)
        self.assertEqual(self.store.record_result(failed, idempotency_key="bad"),
                         Decimal("0.35"))

    def test_a_missing_cost_adds_nothing_and_is_not_zero_work(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.assertEqual(self.store.record_result(result(None), idempotency_key="gap"),
                         Decimal("0.1"))
        recorded = self.payload()["results"]
        self.assertEqual([entry["cost"] for entry in recorded], ["0.1", None])
        # It is recorded as a hole, not dropped and not counted as free work.
        self.assertEqual(len(recorded), 2)

    def test_a_persisted_ledger_that_cannot_be_summed_fails_closed(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.store.record_result(result(0.2), idempotency_key="b")
        payload = self.payload()
        payload["results"][0]["cost"] = "9E+999999"
        payload["results"][1]["cost"] = "9E+999999"
        payload["workloadUnits"] = "0"
        self.rewrite(payload)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.workload_units()
        self.assertEqual(caught.exception.reason, store_module.REASON_WORKLOAD_OVERFLOW)


# --------------------------------------------------------------------------
# Exactly once
# --------------------------------------------------------------------------


class IdempotencyTests(StoreTestCase):
    def test_an_identical_retry_is_a_no_op_across_a_restart(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.store.record_result(result(0.2), idempotency_key="b")
        before = self.path.read_bytes()

        # A brand-new store object reads only what is on disk.
        restarted = AllowanceStore(self.path)
        self.assertEqual(restarted.record_result(result(0.1), idempotency_key="a"),
                         Decimal("0.3"))
        self.assertEqual(restarted.workload_units(), Decimal("0.3"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_missing_cost_retry_is_also_a_no_op(self) -> None:
        self.store.record_result(result(None), idempotency_key="gap")
        before = self.path.read_bytes()
        AllowanceStore(self.path).record_result(result(None), idempotency_key="gap")
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_same_key_with_different_evidence_fails_closed(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        for conflicting in (result(0.2), result(None)):
            with self.subTest(cost=conflicting.total_cost_usd):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.record_result(conflicting, idempotency_key="a")
                self.assertEqual(caught.exception.reason, store_module.REASON_KEY_CONFLICT)
        self.assertEqual(self.store.workload_units(), Decimal("0.1"))

    def test_ordinals_are_assigned_by_the_store_in_append_order(self) -> None:
        """A replay cannot obtain a second ordinal, in any arrival order."""
        for index, key in enumerate(("a", "b", "c"), start=1):
            self.store.record_result(result(0.1), idempotency_key=key)
            self.assertEqual(self.payload()["results"][index - 1]["ordinal"], index)
        # Re-presenting the first event does not append a fourth ordinal.
        self.store.record_result(result(0.1), idempotency_key="a")
        self.assertEqual([entry["ordinal"] for entry in self.payload()["results"]], [1, 2, 3])

    def test_the_key_must_be_an_opaque_bounded_token(self) -> None:
        for value in ("", "   ", None, 7, True, "/home/u/.claude/sessions/abc.jsonl",
                      "has space", "x" * 129, "-leading-punctuation"):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.record_result(result(0.1), idempotency_key=value)
                self.assertEqual(caught.exception.reason, store_module.REASON_INVALID_KEY)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class PersistenceTests(StoreTestCase):
    def _populate(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.observe(0, "10", human=False)
        self.store.record_result(result(0.2), idempotency_key="b")
        self.observe(1_000, "30")

    def test_the_serialized_form_is_deterministic(self) -> None:
        self._populate()
        first = self.path.read_bytes()
        twin = AllowanceStore(self.root / "twin.json")
        twin.record_result(result(0.1), idempotency_key="a")
        twin.append_observation(window="five_hour", observed_at=BASE, resets_at=RESET,
                                used_percentage=Decimal("10"), human_complete_coverage=False)
        twin.record_result(result(0.2), idempotency_key="b")
        twin.append_observation(window="five_hour", observed_at=BASE + 1_000, resets_at=RESET,
                                used_percentage=Decimal("30"), human_complete_coverage=True)
        self.assertEqual(first, twin.path.read_bytes())

    def test_the_schema_declares_its_version_and_meter(self) -> None:
        self._populate()
        payload = self.payload()
        self.assertEqual(payload["version"], SCHEMA_VERSION)
        self.assertEqual(payload["meter"], CURRENT_METER)
        # The meter names its source, its accumulation semantics, and its version.
        self.assertIn("total-cost-usd", CURRENT_METER)
        self.assertTrue(CURRENT_METER.endswith("-v1"))

    def test_a_store_written_for_another_meter_is_never_mixed_in(self) -> None:
        self._populate()
        payload = self.payload()
        payload["meter"] = "claude-agent-sdk-result-total-cost-usd-v2"
        self.rewrite(payload)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.workload_units()
        self.assertEqual(caught.exception.reason, store_module.REASON_METER_MISMATCH)

    def test_every_recorded_reading_carries_the_store_meter(self) -> None:
        self._populate()
        for point in self.store.observations("five_hour"):
            self.assertEqual(point.meter, CURRENT_METER)

    def test_unreadable_state_fails_closed_and_is_never_read_as_empty(self) -> None:
        cases = {
            "truncated": '{"version": 1, "met',
            "empty": "",
            "not an object": "[]",
            "unknown version": None,
            "unknown key": None,
            "missing key": None,
            "cumulative disagrees with the ledger": None,
            "renumbered ordinal": None,
            "removed ledger entry": None,
            "repeated key": None,
            "negative cost": None,
            "cost that is not a string": None,
            "tampered coverage flag": None,
            "impossible observation epoch": None,
            "observation out of order": None,
        }
        for label in cases:
            with self.subTest(case=label):
                # Each case owns its own store, so a failing assertion cannot
                # leave durable state that decides the next case's outcome.
                self.store = AllowanceStore(
                    self.root / ("corrupt-%s.json" % label.replace(" ", "-")))
                self.path = self.store.path
                self._populate()
                if cases[label] is not None:
                    self.rewrite(cases[label])
                else:
                    payload = self.payload()
                    if label == "unknown version":
                        payload["version"] = 2
                    elif label == "unknown key":
                        payload["extra"] = 1
                    elif label == "missing key":
                        payload.pop("results")
                    elif label == "cumulative disagrees with the ledger":
                        payload["workloadUnits"] = "99"
                    elif label == "renumbered ordinal":
                        payload["results"][1]["ordinal"] = 7
                    elif label == "removed ledger entry":
                        payload["results"].pop(0)
                    elif label == "repeated key":
                        payload["results"][1]["key"] = payload["results"][0]["key"]
                    elif label == "negative cost":
                        payload["results"][0]["cost"] = "-1"
                    elif label == "cost that is not a string":
                        payload["results"][0]["cost"] = 0.1
                    elif label == "tampered coverage flag":
                        payload["observations"][1]["completeCoverage"] = False
                    elif label == "impossible observation epoch":
                        payload["observations"][0]["observedAt"] = 0
                    elif label == "observation out of order":
                        payload["observations"][1]["observedAt"] = BASE - 1
                    self.rewrite(payload)
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.workload_units()
                self.assertEqual(caught.exception.reason, store_module.REASON_MALFORMED_STORE)

    def test_the_default_location_is_inside_the_workspace_state_directory(self) -> None:
        self.assertEqual(allowance_store_path(Path("/repo")),
                         Path("/repo/.ai-dev/allowance/workload.json"))


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


class PrivacyTests(StoreTestCase):
    FORBIDDEN = ("account", "session", "prompt", "response", "transcript", "credential",
                 "token", "api_key", "cookie", "email", "user", "log", "path")

    def test_the_schema_has_nowhere_to_put_identity_or_content(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.observe(0, "10")
        payload = self.payload()
        names = set(payload)
        for entry in payload["results"]:
            names |= set(entry)
        for entry in payload["observations"]:
            names |= set(entry)
        for name in names:
            for banned in self.FORBIDDEN:
                with self.subTest(field=name, banned=banned):
                    self.assertNotIn(banned, name.lower())

    def test_the_runtime_session_id_never_reaches_the_store(self) -> None:
        recorded = result(0.1)
        self.assertTrue(recorded.session_id)
        self.store.record_result(recorded, idempotency_key="a")
        self.assertNotIn(recorded.session_id, self.path.read_text(encoding="utf-8"))

    def test_the_module_imports_only_the_standard_library_and_this_package(self) -> None:
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * (node.level or 0)) + (node.module or ""))
        self.assertEqual(
            names,
            {"__future__", "decimal", "errno", "json", "os", "pathlib", "re", "time",
             "typing", ".claude_allowance", ".claude_runtime", ".json_files"},
        )

    def test_both_modules_parse_under_the_minimum_python(self) -> None:
        for module in (store_module, __import__("ai_dev_flow.claude_allowance", fromlist=["x"])):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                ast.parse(source, feature_version=(3, 8))


# --------------------------------------------------------------------------
# Calibration observations
# --------------------------------------------------------------------------


class ObservationTests(StoreTestCase):
    def test_the_store_supplies_workload_and_meter_not_the_caller(self) -> None:
        self.store.record_result(result(0.4), idempotency_key="a")
        point = self.observe(0, "10")
        self.assertEqual(point.workload_units, Decimal("0.4"))
        self.assertEqual(point.meter, CURRENT_METER)
        for injected in ({"workload_units": Decimal("99")}, {"meter": "other"}):
            with self.subTest(field=sorted(injected)[0]):
                with self.assertRaises(TypeError):
                    self.store.append_observation(
                        window="five_hour", observed_at=BASE + 1_000, resets_at=RESET,
                        used_percentage=Decimal("20"), human_complete_coverage=True,
                        **injected)

    def test_coverage_is_the_conjunction_of_the_human_and_the_ledger(self) -> None:
        """Every truth-table case, with the other input held fixed."""
        cases = (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        )
        for human, ledger_clean, expected in cases:
            with self.subTest(human=human, ledger_clean=ledger_clean):
                store = AllowanceStore(self.root / "case-{0}-{1}.json".format(human, ledger_clean))
                store.record_result(result(0.1), idempotency_key="opening")
                store.append_observation(
                    window="five_hour", observed_at=BASE, resets_at=RESET,
                    used_percentage=Decimal("10"), human_complete_coverage=True)
                store.record_result(result(0.2), idempotency_key="inside")
                if not ledger_clean:
                    store.record_result(result(None), idempotency_key="hole")
                point = store.append_observation(
                    window="five_hour", observed_at=BASE + 1_000, resets_at=RESET,
                    used_percentage=Decimal("30"), human_complete_coverage=human)
                self.assertEqual(point.complete_coverage, expected)

    def test_a_gap_only_affects_the_span_that_contains_it(self) -> None:
        self.store.record_result(result(None), idempotency_key="hole")
        first = self.observe(0, "10")
        self.store.record_result(result(0.2), idempotency_key="clean")
        second = self.observe(1_000, "30")
        self.assertFalse(first.complete_coverage)
        self.assertTrue(second.complete_coverage)

    def test_coverage_is_never_inferred_from_a_neighbouring_reading(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        covered = self.observe(0, "10", human=True)
        self.store.record_result(result(0.2), idempotency_key="b")
        uncovered = self.observe(1_000, "30", human=False)
        self.store.record_result(result(0.3), idempotency_key="c")
        recovered = self.observe(2_000, "50", human=True)
        self.assertEqual([covered.complete_coverage, uncovered.complete_coverage,
                          recovered.complete_coverage], [True, False, True])

    def test_the_human_coverage_statement_has_no_default(self) -> None:
        with self.assertRaises(TypeError):
            self.store.append_observation(window="five_hour", observed_at=BASE,
                                          resets_at=RESET, used_percentage=Decimal("10"))
        for value in (1, 0, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.append_observation(
                        window="five_hour", observed_at=BASE, resets_at=RESET,
                        used_percentage=Decimal("10"), human_complete_coverage=value)
                self.assertEqual(caught.exception.reason, store_module.REASON_INVALID_COVERAGE)

    def test_readings_append_in_strict_chronological_order(self) -> None:
        self.observe(1_000, "10")
        # A backfilled reading whose own reset horizon is valid, so only the
        # chronology rule can refuse it.
        for offset, label in ((999, "earlier"), (1_000, "identical")):
            with self.subTest(case=label):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.append_observation(
                        window="five_hour", observed_at=BASE + offset,
                        resets_at=RESET, used_percentage=Decimal("20"),
                        human_complete_coverage=True)
                self.assertEqual(caught.exception.reason,
                                 store_module.REASON_OBSERVATION_OUT_OF_ORDER)
        self.assertEqual(len(self.store.observations("five_hour")), 1)

    def test_a_reading_the_estimator_would_refuse_is_refused_here_too(self) -> None:
        for overrides, reason in (
            ({"window": "hourly"}, "invalid-window"),
            ({"observed_at": 0}, "invalid-epoch"),
            ({"resets_at": BASE + FIVE_HOUR + 1}, "reset-beyond-named-window"),
            ({"used_percentage": Decimal("101")}, "invalid-percentage"),
            ({"used_percentage": 10.0}, "invalid-percentage"),
        ):
            with self.subTest(overrides=sorted(overrides)):
                call = dict(window="five_hour", observed_at=BASE, resets_at=RESET,
                            used_percentage=Decimal("10"), human_complete_coverage=True)
                call.update(overrides)
                with self.assertRaises(AllowanceError) as caught:
                    self.store.append_observation(**call)
                self.assertEqual(caught.exception.reason, reason)

    def test_the_public_history_is_every_recorded_reading_of_that_window(self) -> None:
        """Coverage is predecessor-relative, so a filtered history would lie.

        Asserted as behaviour rather than as a parameter list: what matters is
        that nothing a caller can do drops a recorded reading or lets another
        window's readings change this one's answer.
        """
        appended = []
        for offset, percentage in ((0, "10"), (1_000, "30"), (2_000, "50")):
            appended.append(self.observe(offset, percentage, human=False))
            self.store.record_result(
                result(0.1), idempotency_key="k%d" % offset)
        recorded = self.store.observations("five_hour")
        self.assertEqual(recorded, tuple(appended))
        self.assertEqual(self.store.profile("five_hour"), build_profile(recorded))

        # Readings in the other window are invisible here and change nothing.
        baseline = self.store.profile("five_hour")
        self.store.append_observation(
            window="seven_day", observed_at=BASE + 3_000, resets_at=SEVEN_RESET,
            used_percentage=Decimal("15"), human_complete_coverage=True)
        self.assertEqual(self.store.observations("five_hour"), recorded)
        self.assertEqual(self.store.profile("five_hour"), baseline)

    def test_an_unsupported_window_is_refused(self) -> None:
        for name in ("hourly", "", None, 5):
            with self.subTest(window=name):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.observations(name)
                self.assertEqual(caught.exception.reason, store_module.REASON_INVALID_WINDOW)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


class RoundTripTests(StoreTestCase):
    def _history(self):
        self.store.record_result(result(0.1), idempotency_key="a")
        opening = self.observe(0, "10", human=False)
        self.store.record_result(result(0.3), idempotency_key="b")
        middle = self.observe(1_000, "30")
        self.store.record_result(result(0.6), idempotency_key="c")
        closing = self.observe(2_000, "50")
        return opening, middle, closing

    def test_the_whole_history_survives_a_restart_exactly(self) -> None:
        expected = self._history()
        restarted = AllowanceStore(self.path)
        self.assertEqual(restarted.observations("five_hour"), expected)
        for original, reloaded in zip(expected, restarted.observations("five_hour")):
            self.assertEqual(original.used_percentage, reloaded.used_percentage)
            self.assertEqual(original.workload_units, reloaded.workload_units)
            self.assertEqual(original.complete_coverage, reloaded.complete_coverage)
            self.assertEqual(original.observed_at, reloaded.observed_at)

    def test_the_same_profile_and_estimate_before_and_after_reload(self) -> None:
        self._history()
        restarted = AllowanceStore(self.path)
        before = self.store.profile("five_hour")
        after = restarted.profile("five_hour")
        self.assertEqual(before, after)
        self.assertEqual(len(before.intervals), 2)

        anchor = restarted.latest_observation("five_hour")
        self.assertEqual(anchor, self.store.latest_observation("five_hour"))
        arguments = dict(now=BASE + 3_000, workload_units=restarted.workload_units(),
                         complete_coverage_since_anchor=True)
        self.assertEqual(estimate_current(before, anchor, **arguments),
                         estimate_current(after, anchor, **arguments))
        self.assertTrue(estimate_current(after, anchor, **arguments).available)

    def test_the_profile_is_built_from_the_complete_recorded_history(self) -> None:
        self._history()
        recorded = self.store.observations("five_hour")
        self.assertEqual(self.store.profile("five_hour"), build_profile(recorded))
        self.assertEqual(len(recorded), 3)

    def test_a_window_with_no_history_is_empty_rather_than_absent(self) -> None:
        self._history()
        self.assertEqual(self.store.observations("seven_day"), ())
        self.assertIsNone(self.store.latest_observation("seven_day"))
        self.assertEqual(self.store.profile("seven_day").intervals, ())

    def test_a_restart_does_not_turn_absence_into_zero_or_coverage(self) -> None:
        self.store.record_result(result(None), idempotency_key="hole")
        point = self.observe(0, "10", human=True)
        self.assertFalse(point.complete_coverage)
        restarted = AllowanceStore(self.path)
        self.assertEqual(restarted.workload_units(), Decimal("0"))
        self.assertFalse(restarted.observations("five_hour")[0].complete_coverage)
        self.assertEqual(restarted.profile("five_hour").intervals, ())


# --------------------------------------------------------------------------
# Cross-process exclusion
# --------------------------------------------------------------------------


class ExclusionTests(StoreTestCase):
    def test_two_processes_never_silently_lose_recorded_work(self) -> None:
        """Released from one barrier, every attempt either persists or refuses.

        The assertion is deterministic whatever the interleaving: recorded plus
        explicitly refused must account for every attempt. Silent loss is the
        defect, and it shows up as a shortfall no matter who wins the races.
        """
        attempts_each = 40
        read_end, write_end = os.pipe()
        children = []
        for worker in range(2):
            pid = os.fork()
            if pid == 0:  # pragma: no cover - child process
                os.close(write_end)
                os.read(read_end, 1)
                os.close(read_end)
                store = AllowanceStore(self.path)
                refused = 0
                for index in range(attempts_each):
                    try:
                        store.record_result(
                            result(0.01),
                            idempotency_key="w%d-%03d" % (worker, index))
                    except AllowanceStoreError as exc:
                        if exc.reason != store_module.REASON_STORE_LOCKED:
                            os._exit(255)
                        refused += 1
                os._exit(refused)
            children.append(pid)
        os.close(read_end)
        os.write(write_end, b"gg")
        os.close(write_end)
        refusals = 0
        for pid in children:
            status = os.waitpid(pid, 0)[1]
            code = status >> 8
            self.assertNotEqual(code, 255, "a child saw an unexpected refusal reason")
            refusals += code

        recorded = self.payload()["results"]
        self.assertEqual(len(recorded) + refusals, 2 * attempts_each)
        self.assertGreater(len(recorded), 0)
        # Whatever persisted is exactly consistent, and its total is its ledger.
        self.assertEqual(self.store.workload_units(),
                         Decimal("0.01") * len(recorded))
        self.assertEqual([e["ordinal"] for e in recorded],
                         list(range(1, len(recorded) + 1)))
        self.assertEqual(len({e["key"] for e in recorded}), len(recorded))

    def test_a_held_lock_refuses_and_leaves_the_store_byte_identical(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        before_bytes = self.path.read_bytes()
        self.store.lock_path.write_text(json.dumps(
            {"version": 1, "generation": "someone-else", "pid": 1,
             "acquiredAt": "2026-01-01T00:00:00+0000", "operation": "record_result"}),
            encoding="utf-8")
        calls = (
            ("record_result", lambda: self.store.record_result(
                result(0.2), idempotency_key="b")),
            ("append_observation", lambda: self.observe(0, "10")),
        )
        for label, call in calls:
            with self.subTest(call=label):
                with self.assertRaises(AllowanceStoreError) as caught:
                    call()
                self.assertEqual(caught.exception.reason, store_module.REASON_STORE_LOCKED)
        self.assertEqual(self.path.read_bytes(), before_bytes)

    def test_a_malformed_lock_is_held_not_stale(self) -> None:
        """An owner that cannot be proven is exactly when guessing is expensive."""
        self.store.lock_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.record_result(result(0.1), idempotency_key="a")
        self.assertEqual(caught.exception.reason, store_module.REASON_STORE_LOCKED)
        self.assertTrue(self.store.lock_path.exists(), "the lock was broken")

    def test_a_refused_call_is_safely_retryable_under_the_same_key(self) -> None:
        self.store.lock_path.write_text(json.dumps(
            {"version": 1, "generation": "someone-else", "pid": 1,
             "acquiredAt": "2026-01-01T00:00:00+0000", "operation": "record_result"}),
            encoding="utf-8")
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.record_result(result(0.25), idempotency_key="same")
        self.assertEqual(caught.exception.reason, store_module.REASON_STORE_LOCKED)
        self.store.lock_path.unlink()

        self.assertEqual(self.store.record_result(result(0.25), idempotency_key="same"),
                         Decimal("0.25"))
        self.assertEqual(self.store.record_result(result(0.25), idempotency_key="same"),
                         Decimal("0.25"))
        self.assertEqual(len(self.payload()["results"]), 1)

    def test_the_lock_is_released_on_success_and_on_refusal(self) -> None:
        self.store.record_result(result(0.1), idempotency_key="a")
        self.assertFalse(self.store.lock_path.exists())
        with self.assertRaises(AllowanceStoreError):
            self.store.record_result(result(0.2), idempotency_key="a")  # conflicting
        self.assertFalse(self.store.lock_path.exists(),
                         "the lock survived an error path")
        self.assertEqual(self.store.record_result(result(0.3), idempotency_key="c"),
                         Decimal("0.4"))

    def test_only_the_acquired_generation_is_released(self) -> None:
        generation = self.store._acquire("probe")
        self.store.lock_path.write_text(json.dumps(
            {"version": 1, "generation": "a-different-generation", "pid": 1,
             "acquiredAt": "2026-01-01T00:00:00+0000", "operation": "probe"}),
            encoding="utf-8")
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store._release(generation)
        self.assertEqual(caught.exception.reason, store_module.REASON_LOCK_LOST)
        self.assertTrue(self.store.lock_path.exists())
        self.store.lock_path.unlink()

    def test_lock_metadata_carries_no_identity_or_content(self) -> None:
        generation = self.store._acquire("record_result")
        try:
            payload = json.loads(self.store.lock_path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload),
                             {"version", "generation", "pid", "acquiredAt", "operation"})
            serialized = json.dumps(payload).lower()
            for banned in PrivacyTests.FORBIDDEN:
                with self.subTest(banned=banned):
                    self.assertNotIn(banned, serialized)
        finally:
            self.store._release(generation)


# --------------------------------------------------------------------------
# Two windows from one human reading
# --------------------------------------------------------------------------


class WindowRelativeTests(StoreTestCase):
    def _reading(self, offset, percentage, *, order, human=True):
        """One human `/usage` view recorded into both windows at one instant."""
        pair = [("five_hour", RESET), ("seven_day", SEVEN_RESET)]
        if order == "seven_first":
            pair.reverse()
        return [
            self.store.append_observation(
                window=window, observed_at=BASE + offset, resets_at=reset,
                used_percentage=Decimal(percentage), human_complete_coverage=human)
            for window, reset in pair
        ]

    def test_both_windows_of_one_reading_may_share_an_instant(self) -> None:
        for order in ("five_first", "seven_first"):
            with self.subTest(order=order):
                store = AllowanceStore(self.root / ("shared-%s.json" % order))
                self.store, saved = store, self.store
                try:
                    points = self._reading(0, "10", order=order)
                    self.assertEqual({p.observed_at for p in points}, {BASE})
                    self.assertEqual({p.window for p in points},
                                     {"five_hour", "seven_day"})
                finally:
                    self.store = saved

    def test_a_dirty_span_is_seen_by_both_windows_in_either_order(self) -> None:
        """The review's reproduction: same ledger hole, same answer both windows."""
        for order in ("five_first", "seven_first"):
            with self.subTest(order=order):
                store = AllowanceStore(self.root / ("dirty-%s.json" % order))
                self.store, saved = store, self.store
                try:
                    store.record_result(result(1.0), idempotency_key="open")
                    self._reading(0, "10", order=order)
                    store.record_result(result(None), idempotency_key="hole")
                    store.record_result(result(4.0), idempotency_key="seen")
                    second = self._reading(100, "25", order=order)
                    self.assertEqual([p.complete_coverage for p in second], [False, False])
                    for window in ("five_hour", "seven_day"):
                        self.assertEqual(store.profile(window).intervals, ())
                finally:
                    self.store = saved

    def test_a_clean_span_stays_clean_for_both_windows(self) -> None:
        self.store.record_result(result(1.0), idempotency_key="open")
        self._reading(0, "10", order="five_first")
        self.store.record_result(result(4.0), idempotency_key="seen")
        second = self._reading(100, "25", order="seven_first")
        self.assertEqual([p.complete_coverage for p in second], [True, True])
        for window in ("five_hour", "seven_day"):
            with self.subTest(window=window):
                self.assertEqual(len(self.store.profile(window).intervals), 1)

    def test_each_window_keeps_its_own_workload_and_predecessor(self) -> None:
        """A gap before one window's reading does not follow it into the other."""
        self.store.record_result(result(1.0), idempotency_key="open")
        self.observe(0, "10", human=True)                       # five_hour only
        self.store.record_result(result(None), idempotency_key="hole")
        seven = self.store.append_observation(
            window="seven_day", observed_at=BASE + 50, resets_at=SEVEN_RESET,
            used_percentage=Decimal("12"), human_complete_coverage=True)
        # seven_day has no predecessor, so its span starts at ordinal 0 and
        # traverses the missing cost.
        self.assertFalse(seven.complete_coverage)
        self.store.record_result(result(2.0), idempotency_key="after")
        five = self.observe(100, "20", human=True)
        self.assertFalse(five.complete_coverage)

    def test_same_window_equal_time_and_backfill_still_fail_closed(self) -> None:
        self.observe(1_000, "10")
        for offset, label in ((1_000, "identical"), (999, "earlier")):
            with self.subTest(case=label):
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.append_observation(
                        window="five_hour", observed_at=BASE + offset, resets_at=RESET,
                        used_percentage=Decimal("20"), human_complete_coverage=True)
                self.assertEqual(caught.exception.reason,
                                 store_module.REASON_OBSERVATION_OUT_OF_ORDER)
        self.assertEqual(len(self.store.observations("five_hour")), 1)

    def test_interleaved_two_window_history_reloads_identically(self) -> None:
        self.store.record_result(result(1.0), idempotency_key="a")
        self._reading(0, "10", order="five_first")
        self.store.record_result(result(3.0), idempotency_key="b")
        self._reading(1_000, "25", order="seven_first")
        before_bytes = self.path.read_bytes()

        restarted = AllowanceStore(self.path)
        for window in ("five_hour", "seven_day"):
            with self.subTest(window=window):
                self.assertEqual(restarted.observations(window),
                                 self.store.observations(window))
                self.assertEqual(restarted.profile(window), self.store.profile(window))
        self.assertEqual(self.path.read_bytes(), before_bytes)


# --------------------------------------------------------------------------
# Reload re-derivation
# --------------------------------------------------------------------------


class ReDerivationTests(StoreTestCase):
    def _seed(self):
        """The hole falls between the two readings, so nothing legitimately trains."""
        self.observe(0, "10")
        self.store.record_result(result(None), idempotency_key="hole")
        self.store.record_result(result(4.0), idempotency_key="seen")
        self.observe(100, "30")

    def test_a_tampered_observation_workload_is_refused(self) -> None:
        for label, value in (("inflated", "400"), ("deflated", "0.01")):
            with self.subTest(case=label):
                # Each case owns its own store, for the same reason.
                self.store = AllowanceStore(self.root / ("tampered-%s.json" % label))
                self.path = self.store.path
                self._seed()
                payload = self.payload()
                payload["observations"][1]["workloadUnits"] = value
                self.rewrite(payload)
                with self.assertRaises(AllowanceStoreError) as caught:
                    self.store.profile("five_hour")
                self.assertEqual(caught.exception.reason, store_module.REASON_MALFORMED_STORE)

    def test_downshifted_ordinals_cannot_manufacture_a_clean_span(self) -> None:
        """The sharp case: lowering ordinals used to invent training evidence."""
        self._seed()
        untouched = self.store.profile("five_hour")
        self.assertEqual(untouched.intervals, ())
        payload = self.payload()
        for observation in payload["observations"]:
            observation["ledgerOrdinal"] = 0
            observation["completeCoverage"] = True
        self.rewrite(payload)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.profile("five_hour")
        self.assertEqual(caught.exception.reason, store_module.REASON_MALFORMED_STORE)

    def test_a_valid_store_still_reloads(self) -> None:
        self._seed()
        self.assertEqual(AllowanceStore(self.path).observations("five_hour"),
                         self.store.observations("five_hour"))


# --------------------------------------------------------------------------
# Reset ordering within a window
# --------------------------------------------------------------------------

# A window's second reset identity is one whole window later, and its readings
# necessarily fall inside it -- a five-hour reading cannot sit five hours before
# a reset two windows away, and the record refuses that outright.
WINDOW_SPANS = {"five_hour": (FIVE_HOUR, RESET), "seven_day": (SEVEN_DAY, SEVEN_RESET)}


def window_reading(window, identity, step):
    """An `(observed_at, resets_at)` pair inside the given reset identity."""
    span, first_reset = WINDOW_SPANS[window]
    reset = first_reset + identity * span
    return reset - span + 100 * (step + 1), reset


def trained_spans_with_holes(store, window):
    """Every trained interval, checked against the ledger it actually spans.

    Deliberately ignores the stored `completeCoverage` flag and reads the durable
    file instead. A flag computed against a shorter recorded span must not be able
    to vouch for an interval that really covers more, so the oracle re-derives each
    span from the two readings' own ledger ordinals.
    """
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    costs = {entry["ordinal"]: entry["cost"] for entry in payload["results"]}
    ordinal_of = {
        (entry["resetsAt"], entry["observedAt"]): entry["ledgerOrdinal"]
        for entry in payload["observations"] if entry["window"] == window
    }
    offenders = []
    for interval in store.profile(window).intervals:
        start = ordinal_of[(interval.resets_at, interval.started_at)]
        end = ordinal_of[(interval.resets_at, interval.ended_at)]
        holes = [o for o in range(start + 1, end + 1) if costs.get(o) is None]
        if holes:
            offenders.append((interval.started_at, interval.ended_at, holes))
    return offenders


class ResetOrderTests(StoreTestCase):
    def _reading(self, store, window, identity, step, percentage):
        observed_at, reset = window_reading(window, identity, step)
        return store.append_observation(
            window=window, observed_at=observed_at, resets_at=reset,
            used_percentage=Decimal(percentage), human_complete_coverage=True)

    def test_an_unchanged_reset_epoch_is_accepted(self) -> None:
        """Successive readings inside one reset identity are the normal case."""
        first = self._reading(self.store, "five_hour", 0, 0, "10")
        second = self._reading(self.store, "five_hour", 0, 1, "20")
        self.assertEqual(first.resets_at, second.resets_at)
        self.assertGreater(second.observed_at, first.observed_at)
        self.assertEqual(len(self.store.observations("five_hour")), 2)

    def test_an_advancing_reset_epoch_is_accepted(self) -> None:
        """The provider moving on to a later window is also normal."""
        first = self._reading(self.store, "five_hour", 0, 0, "10")
        later = self._reading(self.store, "five_hour", 1, 0, "5")
        self.assertGreater(later.resets_at, first.resets_at)
        self.assertNotEqual(later.reset_identity, first.reset_identity)
        self.assertEqual(len(self.store.observations("five_hour")), 2)

    def test_a_regressing_reset_epoch_is_refused(self) -> None:
        first = self._reading(self.store, "five_hour", 1, 0, "10")
        before_bytes = self.path.read_bytes()
        # Later in time, but claiming a reset behind the one already recorded, with
        # its own horizon valid so only the ordering rule can object.
        observed_at = first.observed_at + 100
        regressing = observed_at + 60
        self.assertLess(regressing, first.resets_at)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.append_observation(
                window="five_hour", observed_at=observed_at, resets_at=regressing,
                used_percentage=Decimal("20"), human_complete_coverage=True)
        self.assertEqual(caught.exception.reason, store_module.REASON_RESET_EPOCH_REGRESSED)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertFalse(self.store.lock_path.exists())
        self.assertEqual(len(self.store.observations("five_hour")), 1)

    def test_reset_order_is_independent_between_windows(self) -> None:
        """Each window carries its own reset history; neither constrains the other."""
        five = self._reading(self.store, "five_hour", 1, 0, "10")
        # five_hour has already advanced to its second identity. That constrains
        # nothing in seven_day, whose own first reading is accepted regardless.
        seven = self._reading(self.store, "seven_day", 0, 0, "10")
        self.assertNotEqual(seven.resets_at, five.resets_at)
        self.assertEqual(len(self.store.observations("seven_day")), 1)
        self.assertEqual(len(self.store.observations("five_hour")), 1)
        # The identical shape inside five_hour is still refused.
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.append_observation(
                window="five_hour", observed_at=five.observed_at + 100,
                resets_at=five.observed_at + 160,
                used_percentage=Decimal("20"), human_complete_coverage=True)
        self.assertEqual(caught.exception.reason, store_module.REASON_RESET_EPOCH_REGRESSED)

    def test_interleaved_two_window_histories_survive_in_both_orders(self) -> None:
        for order in ("five_first", "seven_first"):
            with self.subTest(order=order):
                store = AllowanceStore(self.root / ("interleaved-%s.json" % order))
                windows = ["five_hour", "seven_day"]
                if order == "seven_first":
                    windows.reverse()
                for step in (0, 1):
                    store.record_result(result(1.0),
                                        idempotency_key="k-%s-%d" % (order, step))
                    for window in windows:
                        self._reading(store, window, 0, step, str(10 + 10 * step))
                for window in ("five_hour", "seven_day"):
                    self.assertEqual(len(store.observations(window)), 2)
                reloaded = AllowanceStore(store.path)
                for window in ("five_hour", "seven_day"):
                    self.assertEqual(reloaded.observations(window), store.observations(window))

    def test_persisted_reset_order_tampering_is_refused_on_reload(self) -> None:
        self._reading(self.store, "five_hour", 0, 0, "10")
        self._reading(self.store, "five_hour", 0, 1, "20")
        payload = self.payload()
        # Raise the first reading's reset so the second now reads as a regression,
        # keeping every horizon valid so only the ordering rule can object.
        payload["observations"][0]["resetsAt"] += 50
        self.assertLess(payload["observations"][1]["resetsAt"],
                        payload["observations"][0]["resetsAt"])
        self.assertLessEqual(
            payload["observations"][0]["resetsAt"] - payload["observations"][0]["observedAt"],
            FIVE_HOUR)
        self.rewrite(payload)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.observations("five_hour")
        self.assertEqual(caught.exception.reason, store_module.REASON_MALFORMED_STORE)

    def test_equal_and_advancing_histories_reload_unchanged(self) -> None:
        self.store.record_result(result(1.0), idempotency_key="a")
        self._reading(self.store, "five_hour", 0, 0, "10")
        self.store.record_result(result(2.0), idempotency_key="b")
        self._reading(self.store, "five_hour", 0, 1, "20")
        self.store.record_result(result(3.0), idempotency_key="c")
        self._reading(self.store, "five_hour", 1, 0, "5")
        identities = {p.resets_at for p in self.store.observations("five_hour")}
        self.assertEqual(len(identities), 2)
        reloaded = AllowanceStore(self.path)
        self.assertEqual(reloaded.observations("five_hour"), self.store.observations("five_hour"))
        self.assertEqual(reloaded.profile("five_hour"), self.store.profile("five_hour"))

    def test_the_reviewer_reproduction_cannot_be_recorded(self) -> None:
        """A/B/A: an earlier-reset reading recorded between two later-reset ones.

        On checkpoint 24 this history was writable, and `build_profile` paired the
        two later-reset readings across a span containing a missing cost. The guard
        now refuses the middle reading, so that history cannot exist.
        """
        self.store.record_result(result(1.0), idempotency_key="k1")
        first = self._reading(self.store, "five_hour", 1, 0, "10")
        self.store.record_result(result(None), idempotency_key="hole")
        self.store.record_result(result(4.0), idempotency_key="k2")
        observed_at, earlier = window_reading("five_hour", 0, 1)
        self.assertLess(earlier, first.resets_at)
        with self.assertRaises(AllowanceStoreError) as caught:
            self.store.append_observation(
                window="five_hour", observed_at=first.observed_at + 10,
                resets_at=first.observed_at + 10 + 60,
                used_percentage=Decimal("15"), human_complete_coverage=True)
        self.assertEqual(caught.exception.reason, store_module.REASON_RESET_EPOCH_REGRESSED)

        # The premise really was adversarial: a hole sits in the ledger, and only
        # the one surviving reset identity remains, so nothing can pair across it.
        payload = self.payload()
        self.assertIn(None, [entry["cost"] for entry in payload["results"]])
        self.assertEqual({entry["resetsAt"] for entry in payload["observations"]},
                         {first.resets_at})
        self.assertEqual(trained_spans_with_holes(self.store, "five_hour"), [])

    def test_no_trained_interval_ever_spans_a_missing_cost(self) -> None:
        """A bounded public-API corpus, judged by the ledger rather than by flags.

        Every case deliberately attempts the adversarial shape: a reading whose
        reset sits behind the one already recorded for that window, arriving later
        in time so nothing but the reset rule can object. Whatever the store
        accepts, no trained interval may span a ledger entry whose cost is missing.
        """
        windows = ("five_hour", "seven_day")
        spans = {"five_hour": FIVE_HOUR, "seven_day": SEVEN_DAY}
        intervals_seen = regressions_refused = holes_seen = advanced = 0
        for case in range(96):
            store = AllowanceStore(self.root / ("corpus-%02d.json" % case))
            last = {w: None for w in windows}
            identities = {w: set() for w in windows}
            for step in range(5):
                missing = (case + step) % 3 == 0
                holes_seen += 1 if missing else 0
                store.record_result(result(None if missing else 1.0 + step),
                                    idempotency_key="c%02d-%d" % (case, step))
                window = windows[(case >> step) & 1]
                span = spans[window]
                previous = last[window]
                observed = BASE + 100 if previous is None else previous[0] + 100
                if previous is None:
                    reset = observed + span          # this window's first identity
                elif (case >> (step + 1)) & 1:
                    reset = previous[1]              # another reading, same identity
                elif (case >> (step + 2)) & 1:
                    reset = observed + span          # the provider moved on
                else:
                    reset = observed + 60            # a reset behind the recorded one
                try:
                    store.append_observation(
                        window=window, observed_at=observed, resets_at=reset,
                        used_percentage=Decimal(str(10 + 5 * step)),
                        human_complete_coverage=bool((case >> step) & 1))
                    last[window] = (observed, reset)
                    identities[window].add(reset)
                except AllowanceStoreError as exc:
                    self.assertEqual(exc.reason, store_module.REASON_RESET_EPOCH_REGRESSED)
                    regressions_refused += 1
                    # A refused reading must leave the recorded history untouched.
                    last[window] = previous
            for window in windows:
                offenders = trained_spans_with_holes(store, window)
                self.assertEqual(offenders, [], "case %d %s trained across a hole: %s"
                                 % (case, window, offenders))
                intervals_seen += len(store.profile(window).intervals)
                advanced += 1 if len(identities[window]) > 1 else 0
        # The corpus must exercise what it claims.
        self.assertGreater(intervals_seen, 0, "no interval was ever trained")
        self.assertGreater(holes_seen, 0, "no missing cost was ever recorded")
        self.assertGreater(regressions_refused, 0, "no regressing reset was ever attempted")
        self.assertGreater(advanced, 0, "no window ever held two reset identities")


if __name__ == "__main__":
    unittest.main()
