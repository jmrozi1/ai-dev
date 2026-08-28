"""`claude_allowance_store` records evidence exactly once and never invents any."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import ast
import json
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
BASE = 1_700_000_000
RESET = BASE + FIVE_HOUR


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
                self.path.unlink()

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
            {"__future__", "decimal", "json", "pathlib", "re", "typing",
             ".claude_allowance", ".claude_runtime", ".json_files"},
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

    def test_there_is_no_way_to_ask_for_a_subset_of_the_history(self) -> None:
        """Coverage is predecessor-relative, so a filtered history would lie."""
        import inspect

        for name in ("profile", "observations", "latest_observation"):
            with self.subTest(method=name):
                parameters = list(
                    inspect.signature(getattr(AllowanceStore, name)).parameters)
                self.assertEqual(parameters, ["self", "window"])

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


if __name__ == "__main__":
    unittest.main()
