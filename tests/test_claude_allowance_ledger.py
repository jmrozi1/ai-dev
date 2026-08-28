"""`claude_allowance_ledger` records one dispatched invocation exactly once."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import ast
import json
import tempfile
import unittest
import uuid

from ai_dev_flow import claude_allowance_ledger as ledger_module
from ai_dev_flow.claude_allowance_ledger import (
    DEFAULT_ATTEMPTS,
    INVOCATION_KINDS,
    KIND_CONTINUE,
    KIND_LAUNCH,
    WORKER_PRE_DISPATCH_REASONS,
    AllowanceLedger,
    AllowanceLedgerError,
    InvocationIdentity,
    dispatch_occurred,
    missing_result,
    result_from_message,
)
from ai_dev_flow.claude_allowance_store import (
    REASON_LOCK_LOST,
    REASON_LOCK_MALFORMED,
    REASON_STORE_LOCKED,
    AllowanceStore,
    AllowanceStoreError,
)
from ai_dev_flow.claude_runtime import (
    MODE_LAUNCH,
    MODE_RESUME,
    REASON_ASSET_MISSING,
    REASON_RESULT_SESSION_MISMATCH,
    REASON_SDK_MISSING,
    ClaudeRuntimeError,
    RuntimeResult,
)
from ai_dev_flow.claude_worker import (
    MESSAGE_RESULT,
    PROTOCOL_VERSION,
    REASON_COMMAND_TIMEOUT,
    REASON_PROTOCOL_VIOLATION,
    REASON_SPAWN_FAILED,
    REASON_WORKER_EXITED,
    REASON_WORKER_FATAL,
    ClaudeWorkerError,
)
from ai_dev_flow.session_binding import REASON_NOT_RESERVED, SessionBindingError
from ai_dev_flow.session_lifecycle import (
    REASON_HANDLE_MISSING,
    REASON_LAUNCH_FAILED,
    REASON_NOT_AUTHORIZED,
    LifecycleError,
)

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"
# Letters on purpose: the canonical-form rule only bites on a spelling that can vary.
MIXED_CASE_SESSION = "abcdef01-2345-6789-abcd-ef0123456789"

FIVE_HOUR = 5 * 60 * 60
BASE = 1_700_000_000
RESET = BASE + FIVE_HOUR


def message(
    *,
    session_id: str = SESSION,
    mode: str = MODE_LAUNCH,
    cost=0.25,
    omit_cost: bool = False,
    subtype: str = "success",
    is_error: bool = False,
    **overrides,
) -> dict:
    """A worker result message, exactly as `run_request` returns one."""
    payload = {
        "type": MESSAGE_RESULT,
        "protocol": PROTOCOL_VERSION,
        "mode": mode,
        "session_id": session_id,
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": 2,
        "total_cost_usd": cost,
        "markers": {"handoff-published": True},
    }
    if omit_cost:
        del payload["total_cost_usd"]
    payload.update(overrides)
    return payload


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="allowance-ledger-"))
        self.addCleanup(self._remove_root)
        self.path = self.root / "workload.json"
        self.store = AllowanceStore(self.path)
        self.slept = []
        self.ledger = AllowanceLedger(self.store, sleep=self.slept.append)

    def _remove_root(self) -> None:
        for item in sorted(self.root.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        self.root.rmdir()

    def payload(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def results(self):
        return [] if not self.path.exists() else self.payload()["results"]

    def raw(self):
        return self.path.read_bytes() if self.path.exists() else None

    def identity(self, kind: str = KIND_LAUNCH, session_id: str = SESSION):
        return self.ledger.next_identity(session_id, kind)

    def observe(self, offset: int, percentage: str, *, human: bool = True):
        return self.store.append_observation(
            window="five_hour",
            observed_at=BASE + offset,
            resets_at=RESET,
            used_percentage=Decimal(percentage),
            human_complete_coverage=human,
        )


# --------------------------------------------------------------------------
# Identity: one invocation, not one session
# --------------------------------------------------------------------------


class IdentityTests(LedgerTestCase):
    def test_the_first_launch_of_a_session_is_named_exactly(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.assertEqual(identity.key, "{0}:launch:1".format(SESSION))
        self.assertEqual(identity.runtime_mode, MODE_LAUNCH)

    def test_a_continuation_carries_the_resume_request_mode(self) -> None:
        """Lifecycle vocabulary in the key, request vocabulary in the reconstruction."""
        identity = self.identity(KIND_CONTINUE)
        self.assertEqual(identity.key, "{0}:continue:1".format(SESSION))
        self.assertEqual(identity.runtime_mode, MODE_RESUME)
        self.assertNotEqual(identity.kind, identity.runtime_mode)

    def test_two_continuations_of_one_session_are_two_ledger_entries(self) -> None:
        """The exact reason a session UUID alone cannot be the key."""
        first = self.identity(KIND_CONTINUE)
        second = self.identity(KIND_CONTINUE)
        self.assertEqual((first.ordinal, second.ordinal), (1, 2))
        self.assertNotEqual(first.key, second.key)

        self.assertEqual(self.ledger.record_completed(first, message(mode=MODE_RESUME, cost=0.25)),
                         Decimal("0.25"))
        self.assertEqual(self.ledger.record_completed(second, message(mode=MODE_RESUME, cost=0.5)),
                         Decimal("0.75"))

        recorded = self.results()
        self.assertEqual([entry["key"] for entry in recorded],
                         [first.key, second.key])
        self.assertEqual([entry["ordinal"] for entry in recorded], [1, 2])
        self.assertEqual(len({entry["key"] for entry in recorded}), 2)

    def test_a_session_id_alone_would_have_collapsed_them(self) -> None:
        """Stated as a property of the keys, so the control has a named assertion."""
        first = self.identity(KIND_CONTINUE)
        second = self.identity(KIND_CONTINUE)
        self.assertEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.key, second.key)

    def test_ordinals_are_monotonic_across_both_kinds_of_one_session(self) -> None:
        launch = self.identity(KIND_LAUNCH)
        following = self.identity(KIND_CONTINUE)
        self.assertEqual((launch.ordinal, following.ordinal), (1, 2))
        self.assertEqual(following.key, "{0}:continue:2".format(SESSION))

    def test_each_session_numbers_itself(self) -> None:
        first = self.identity(KIND_LAUNCH, SESSION)
        other = self.identity(KIND_LAUNCH, OTHER_SESSION)
        self.assertEqual((first.ordinal, other.ordinal), (1, 1))
        self.assertNotEqual(first.key, other.key)

    def test_a_minted_identity_that_never_dispatched_only_leaves_a_gap(self) -> None:
        """Ordinals must be unique, not gapless; an unused one records nothing."""
        self.identity(KIND_LAUNCH)
        used = self.identity(KIND_CONTINUE)
        self.ledger.record_completed(used, message(mode=MODE_RESUME, cost=0.25))
        self.assertEqual([entry["key"] for entry in self.results()], [used.key])
        self.assertEqual(used.ordinal, 2)

    def test_one_session_cannot_be_spelled_two_ways_in_two_keys(self) -> None:
        """The accepted canonical check is what keeps one session one key prefix."""
        padded = self.ledger.next_identity("  " + MIXED_CASE_SESSION + "  ", KIND_LAUNCH)
        plain = self.ledger.next_identity(MIXED_CASE_SESSION, KIND_CONTINUE)
        self.assertEqual(padded.session_id, MIXED_CASE_SESSION)
        self.assertEqual((padded.ordinal, plain.ordinal), (1, 2))

    def test_the_key_fits_the_store_key_grammar(self) -> None:
        for kind in INVOCATION_KINDS:
            with self.subTest(kind=kind):
                identity = InvocationIdentity(session_id=SESSION, kind=kind, ordinal=987)
                self.store.record_result(
                    RuntimeResult(session_id=SESSION, mode=identity.runtime_mode,
                                  subtype="success", is_error=False, num_turns=1,
                                  total_cost_usd=0.1),
                    idempotency_key=identity.key,
                )
        self.assertEqual(len(self.results()), 2)

    def test_it_refuses_an_unusable_kind_session_or_ordinal(self) -> None:
        for kind in ("resume", "", None, "LAUNCH"):
            with self.subTest(kind=kind):
                with self.assertRaises(AllowanceLedgerError) as caught:
                    self.ledger.next_identity(SESSION, kind)
                self.assertEqual(caught.exception.reason, ledger_module.REASON_INVALID_KIND)
        for session_id in ("not-a-uuid", "", MIXED_CASE_SESSION.upper(), 7, None):
            with self.subTest(session_id=session_id):
                with self.assertRaises(SessionBindingError):
                    self.ledger.next_identity(session_id, KIND_LAUNCH)
        for ordinal in (0, -1, True, "1", 1.0):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(AllowanceLedgerError) as caught:
                    InvocationIdentity(session_id=SESSION, kind=KIND_LAUNCH, ordinal=ordinal)
                self.assertEqual(caught.exception.reason, ledger_module.REASON_INVALID_ORDINAL)


# --------------------------------------------------------------------------
# Reconstruction: the controller never receives a RuntimeResult
# --------------------------------------------------------------------------


class ReconstructionTests(LedgerTestCase):
    def test_a_completed_launch_records_the_reported_cost_under_its_key(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.assertEqual(self.ledger.record_completed(identity, message(cost=0.25)),
                         Decimal("0.25"))
        self.assertEqual(
            self.results(),
            [{"key": "{0}:launch:1".format(SESSION), "ordinal": 1, "cost": "0.25"}],
        )

    def test_the_cost_crosses_through_the_accepted_decimal_string_boundary(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.ledger.record_completed(identity, message(cost=0.1))
        self.assertEqual(self.results()[0]["cost"], "0.1")
        self.assertEqual(self.store.workload_units(), Decimal("0.1"))
        self.assertNotEqual(self.store.workload_units(), Decimal(0.1))

    def test_a_provider_error_result_with_a_cost_records_that_cost(self) -> None:
        """An errored turn still consumed allowance; the store weighs cost, not success."""
        identity = self.identity(KIND_LAUNCH)
        total = self.ledger.record_completed(
            identity, message(cost=0.4, subtype="error_during_execution", is_error=True)
        )
        self.assertEqual(total, Decimal("0.4"))
        self.assertEqual(self.results()[0]["cost"], "0.4")

    def test_a_reconstructed_result_is_the_worker_reduced_value(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        reconstructed = result_from_message(identity, message(cost=0.25, num_turns=7))
        self.assertEqual(
            reconstructed,
            RuntimeResult(session_id=SESSION, mode=MODE_LAUNCH, subtype="success",
                          is_error=False, num_turns=7, total_cost_usd=0.25),
        )

    def test_a_result_for_another_session_is_refused_by_the_accepted_contract(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            result_from_message(identity, message(session_id=OTHER_SESSION))
        self.assertEqual(caught.exception.reason, REASON_RESULT_SESSION_MISMATCH)
        self.assertIsNone(self.raw())

    def test_a_resume_answer_cannot_be_recorded_under_a_launch_identity(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        with self.assertRaises(AllowanceLedgerError) as caught:
            self.ledger.record_completed(identity, message(mode=MODE_RESUME))
        self.assertEqual(caught.exception.reason, ledger_module.REASON_MESSAGE_MODE_MISMATCH)
        self.assertIsNone(self.raw())

        continuation = self.identity(KIND_CONTINUE)
        with self.assertRaises(AllowanceLedgerError) as caught:
            self.ledger.record_completed(continuation, message(mode=MODE_LAUNCH))
        self.assertEqual(caught.exception.reason, ledger_module.REASON_MESSAGE_MODE_MISMATCH)
        self.assertIsNone(self.raw())

    def test_an_envelope_that_is_not_a_worker_result_is_refused(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        cases = (
            ("not a mapping", ["result"]),
            ("wrong type", message(type="error")),
            ("wrong protocol", message(protocol=PROTOCOL_VERSION + 1)),
            ("no type", {k: v for k, v in message().items() if k != "type"}),
            ("no mode", {k: v for k, v in message().items() if k != "mode"}),
            ("no session", {k: v for k, v in message().items() if k != "session_id"}),
        )
        for label, payload in cases:
            with self.subTest(case=label):
                with self.assertRaises(AllowanceLedgerError) as caught:
                    result_from_message(identity, payload)
                self.assertEqual(caught.exception.reason, ledger_module.REASON_INVALID_MESSAGE)
        self.assertIsNone(self.raw())


# --------------------------------------------------------------------------
# Absence is never zero
# --------------------------------------------------------------------------


class MissingCostTests(LedgerTestCase):
    def test_an_absent_or_null_cost_records_a_hole_rather_than_zero(self) -> None:
        for label, payload in (
            ("absent key", message(omit_cost=True)),
            ("explicit None", message(cost=None)),
        ):
            with self.subTest(case=label):
                store = AllowanceStore(self.root / ("hole-%s.json" % label.replace(" ", "-")))
                ledger = AllowanceLedger(store, sleep=self.slept.append)
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                self.assertEqual(ledger.record_completed(identity, payload), Decimal(0))
                recorded = json.loads(store.path.read_text(encoding="utf-8"))["results"]
                self.assertEqual(len(recorded), 1)
                self.assertIsNone(recorded[0]["cost"])
                self.assertNotEqual(recorded[0]["cost"], "0")
                self.assertNotEqual(recorded[0]["cost"], 0)

    def test_a_hole_makes_the_next_calibration_span_incomplete(self) -> None:
        """The consequence that separates a hole from a zero-cost result."""
        for label, payload, expect_complete in (
            ("absent key", message(omit_cost=True), False),
            ("explicit None", message(cost=None), False),
            ("real zero", message(cost=0.0), True),
        ):
            with self.subTest(case=label):
                store = AllowanceStore(self.root / ("span-%s.json" % label.replace(" ", "-")))
                ledger = AllowanceLedger(store, sleep=self.slept.append)
                store.append_observation(
                    window="five_hour", observed_at=BASE, resets_at=RESET,
                    used_percentage=Decimal("10"), human_complete_coverage=True,
                )
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                ledger.record_completed(identity, payload)
                point = store.append_observation(
                    window="five_hour", observed_at=BASE + 60, resets_at=RESET,
                    used_percentage=Decimal("20"), human_complete_coverage=True,
                )
                self.assertEqual(point.complete_coverage, expect_complete)
                self.assertEqual(store.workload_units(), Decimal("0.0")
                                 if label == "real zero" else Decimal(0))

    def test_a_hole_leaves_the_projection_span_dirty_until_the_next_reading(self) -> None:
        self.observe(0, "10")
        identity = self.identity(KIND_LAUNCH)
        self.ledger.record_completed(identity, message(cost=None))
        self.assertFalse(self.store.projection_inputs("five_hour").ledger_clean_since_anchor)

    def test_a_dispatched_invocation_with_no_result_records_exactly_one_hole(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        error = LifecycleError(REASON_LAUNCH_FAILED, "bound to pid 12 but its send failed")
        self.assertEqual(self.ledger.record_failure(identity, error), Decimal(0))
        recorded = self.results()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["key"], identity.key)
        self.assertIsNone(recorded[0]["cost"])

    def test_the_hole_a_dispatched_failure_records_is_an_errored_result(self) -> None:
        identity = self.identity(KIND_CONTINUE)
        self.assertEqual(
            missing_result(identity),
            RuntimeResult(session_id=SESSION, mode=MODE_RESUME, subtype=None,
                          is_error=True, num_turns=None, total_cost_usd=None),
        )


# --------------------------------------------------------------------------
# The burden of proof runs one way
# --------------------------------------------------------------------------


class DispatchClassificationTests(LedgerTestCase):
    PRE_DISPATCH = (
        ("worker spawn", ClaudeWorkerError(REASON_SPAWN_FAILED, "no process")),
        ("worker sdk", ClaudeWorkerError("sdk-unavailable", "no claude-agent-sdk")),
        ("worker selector", ClaudeWorkerError("credential-selector-present", "ANTHROPIC_API_KEY")),
        ("worker binding", ClaudeWorkerError("binding-not-reserved", "not reserved")),
        ("worker readiness", ClaudeWorkerError("readiness-failed", "no ready message")),
        ("worker iteration", ClaudeWorkerError("iteration-mismatch", "other blob")),
        ("runtime sdk", ClaudeRuntimeError(REASON_SDK_MISSING, "not installed")),
        ("runtime request asset", ClaudeRuntimeError(REASON_ASSET_MISSING, "no prompt file")),
        ("binding refusal", SessionBindingError(REASON_NOT_RESERVED, "already bound")),
        ("lifecycle authorization", LifecycleError(REASON_NOT_AUTHORIZED, "refused")),
        ("lifecycle handle", LifecycleError(REASON_HANDLE_MISSING, "no owned handle")),
    )

    DISPATCHED = (
        ("lifecycle launch-failed", LifecycleError(REASON_LAUNCH_FAILED, "send failed")),
        ("worker exited", ClaudeWorkerError(REASON_WORKER_EXITED, "cannot write to the worker")),
        ("worker timeout", ClaudeWorkerError(REASON_COMMAND_TIMEOUT, "no answer in time")),
        ("worker protocol", ClaudeWorkerError(REASON_PROTOCOL_VIOLATION, "expected a result")),
        ("worker fatal", ClaudeWorkerError(REASON_WORKER_FATAL, "provider raised")),
        ("runtime result session", ClaudeRuntimeError(REASON_RESULT_SESSION_MISMATCH, "stranger")),
        ("unknown failure", RuntimeError("something nobody classified")),
        ("ledger message refusal", AllowanceLedgerError("invalid-result-message", "no type")),
    )

    def test_a_proven_pre_dispatch_refusal_records_nothing(self) -> None:
        for label, error in self.PRE_DISPATCH:
            with self.subTest(case=label):
                store = AllowanceStore(self.root / "pre.json")
                ledger = AllowanceLedger(store, sleep=self.slept.append)
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                self.assertFalse(dispatch_occurred(error))
                self.assertIsNone(ledger.record_failure(identity, error))
                self.assertFalse(store.path.exists(), "a refusal before dispatch wrote a store")

    def test_a_pre_dispatch_refusal_leaves_an_existing_store_byte_identical(self) -> None:
        self.ledger.record_completed(self.identity(KIND_LAUNCH), message(cost=0.25))
        before = self.raw()
        for label, error in self.PRE_DISPATCH:
            with self.subTest(case=label):
                identity = self.identity(KIND_CONTINUE)
                self.assertIsNone(self.ledger.record_failure(identity, error))
                self.assertEqual(self.raw(), before)

    def test_every_other_failure_records_a_hole(self) -> None:
        for label, error in self.DISPATCHED:
            with self.subTest(case=label):
                store = AllowanceStore(self.root / ("post-%s.json" % label.replace(" ", "-")))
                ledger = AllowanceLedger(store, sleep=self.slept.append)
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                self.assertTrue(dispatch_occurred(error))
                self.assertEqual(ledger.record_failure(identity, error), Decimal(0))
                recorded = json.loads(store.path.read_text(encoding="utf-8"))["results"]
                self.assertEqual(len(recorded), 1)
                self.assertIsNone(recorded[0]["cost"])

    def test_an_unknown_failure_is_dispatched_by_default(self) -> None:
        """A wrong `False` understates consumption forever; a wrong `True` recovers."""
        for error in (RuntimeError("?"), OSError("?"), Exception("?")):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(dispatch_occurred(error))

    def test_the_worker_pre_dispatch_set_is_exactly_the_startup_refusals(self) -> None:
        self.assertEqual(
            sorted(WORKER_PRE_DISPATCH_REASONS),
            ["binding-not-reserved", "credential-selector-present", "iteration-mismatch",
             "readiness-failed", "sdk-unavailable", "spawn-failed"],
        )
        for reason in (REASON_WORKER_EXITED, REASON_COMMAND_TIMEOUT,
                       REASON_PROTOCOL_VIOLATION, REASON_WORKER_FATAL):
            with self.subTest(reason=reason):
                self.assertNotIn(reason, WORKER_PRE_DISPATCH_REASONS)


# --------------------------------------------------------------------------
# Contention is bounded, explicit, and never routed around
# --------------------------------------------------------------------------


class _RefusingStore:
    """A store that refuses a fixed number of times, recording every key it sees."""

    def __init__(self, reason: str, *, refusals: int, total=Decimal("1.5")) -> None:
        self.reason = reason
        self.refusals = refusals
        self.total = total
        self.keys = []

    def record_result(self, result, *, idempotency_key: str):
        self.keys.append(idempotency_key)
        if len(self.keys) <= self.refusals:
            raise AllowanceStoreError(self.reason, "refusal {0}".format(len(self.keys)))
        return self.total


class ContentionTests(LedgerTestCase):
    def test_a_held_lock_is_retried_on_the_same_key_then_fails_loudly(self) -> None:
        self.ledger.record_completed(self.identity(KIND_LAUNCH), message(cost=0.25))
        before = self.raw()

        identity = self.identity(KIND_CONTINUE)
        self.store.lock_path.write_text("held by another writer\n", encoding="utf-8")
        self.addCleanup(self.store.lock_path.unlink)

        attempts = []
        original = self.store.record_result

        def counting(result, *, idempotency_key):
            attempts.append(idempotency_key)
            return original(result, idempotency_key=idempotency_key)

        self.store.record_result = counting
        with self.assertRaises(AllowanceLedgerError) as caught:
            self.ledger.record_hole(identity)

        self.assertEqual(caught.exception.reason, ledger_module.REASON_LEDGER_CONTENDED)
        self.assertIn(REASON_STORE_LOCKED, caught.exception.detail)
        self.assertEqual(len(attempts), DEFAULT_ATTEMPTS)
        self.assertEqual(set(attempts), {identity.key})
        self.assertEqual(len(self.slept), DEFAULT_ATTEMPTS - 1)
        self.assertEqual(self.raw(), before, "a refused write changed the store")

    def test_a_malformed_lock_takes_the_same_bounded_path(self) -> None:
        store = _RefusingStore(REASON_LOCK_MALFORMED, refusals=DEFAULT_ATTEMPTS)
        ledger = AllowanceLedger(store, sleep=self.slept.append)
        identity = ledger.next_identity(SESSION, KIND_LAUNCH)
        with self.assertRaises(AllowanceLedgerError) as caught:
            ledger.record_hole(identity)
        self.assertEqual(caught.exception.reason, ledger_module.REASON_LEDGER_CONTENDED)
        self.assertEqual(store.keys, [identity.key] * DEFAULT_ATTEMPTS)
        self.assertEqual(len(self.slept), DEFAULT_ATTEMPTS - 1)

    def test_contention_that_clears_within_the_bound_succeeds_on_the_same_key(self) -> None:
        store = _RefusingStore(REASON_STORE_LOCKED, refusals=DEFAULT_ATTEMPTS - 1)
        ledger = AllowanceLedger(store, sleep=self.slept.append)
        identity = ledger.next_identity(SESSION, KIND_LAUNCH)
        self.assertEqual(ledger.record_hole(identity), Decimal("1.5"))
        self.assertEqual(store.keys, [identity.key] * DEFAULT_ATTEMPTS)

    def test_an_exhausted_lost_lock_says_it_could_not_reconcile(self) -> None:
        store = _RefusingStore(REASON_LOCK_LOST, refusals=DEFAULT_ATTEMPTS)
        ledger = AllowanceLedger(store, sleep=self.slept.append)
        identity = ledger.next_identity(SESSION, KIND_LAUNCH)
        with self.assertRaises(AllowanceLedgerError) as caught:
            ledger.record_hole(identity)
        self.assertEqual(caught.exception.reason, ledger_module.REASON_LEDGER_UNRECONCILED)
        self.assertEqual(store.keys, [identity.key] * DEFAULT_ATTEMPTS)

    def test_the_attempt_bound_is_configurable_and_still_fixed(self) -> None:
        for attempts in (1, 2, 5):
            with self.subTest(attempts=attempts):
                slept = []
                store = _RefusingStore(REASON_STORE_LOCKED, refusals=attempts)
                ledger = AllowanceLedger(store, attempts=attempts, sleep=slept.append)
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                with self.assertRaises(AllowanceLedgerError):
                    ledger.record_hole(identity)
                self.assertEqual(len(store.keys), attempts)
                self.assertEqual(len(slept), attempts - 1)

    def test_a_conflicting_replay_fails_loudly_and_never_mints_a_fresh_key(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.ledger.record_completed(identity, message(cost=0.25))
        with self.assertRaises(AllowanceStoreError) as caught:
            self.ledger.record_completed(identity, message(cost=0.5))
        self.assertEqual(caught.exception.reason, "duplicate-key-conflict")
        self.assertEqual(len(self.results()), 1)

    def test_corruption_and_write_failures_are_not_retried_into_silence(self) -> None:
        for reason in ("malformed-store", "unreadable-store", "store-write-failed",
                       "meter-mismatch", "workload-overflow"):
            with self.subTest(reason=reason):
                store = _RefusingStore(reason, refusals=DEFAULT_ATTEMPTS)
                ledger = AllowanceLedger(store, sleep=self.slept.append)
                identity = ledger.next_identity(SESSION, KIND_LAUNCH)
                with self.assertRaises(AllowanceStoreError) as caught:
                    ledger.record_hole(identity)
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(store.keys, [identity.key], "a non-contention refusal was retried")

    def test_an_identical_replay_of_a_landed_write_adds_no_second_ordinal(self) -> None:
        """`_release` raises after `_save` landed; the identical key reconciles it."""
        identity = self.identity(KIND_LAUNCH)
        original_release = self.store._release
        raised = []

        def losing(generation):
            original_release(generation)
            if not raised:
                raised.append(generation)
                raise AllowanceStoreError(REASON_LOCK_LOST, "lock vanished before release")

        self.store._release = losing
        total = self.ledger.record_completed(identity, message(cost=0.25))

        self.assertEqual(len(raised), 1, "the lost-lock path did not run")
        self.assertEqual(total, Decimal("0.25"))
        self.assertEqual(
            self.results(),
            [{"key": identity.key, "ordinal": 1, "cost": "0.25"}],
        )
        self.assertEqual(self.store.workload_units(), Decimal("0.25"))

    def test_a_replay_of_a_lost_lock_whose_write_did_not_land_records_it(self) -> None:
        """The other half of the same reconciliation: nothing landed, so the replay writes."""
        identity = self.identity(KIND_LAUNCH)
        original_save = self.store._save
        original_release = self.store._release
        calls = []

        def dropping_save(state):
            calls.append("save")
            if len(calls) == 1:
                return None  # the write never reaches disk
            return original_save(state)

        def losing_release(generation):
            original_release(generation)
            if calls == ["save"]:
                raise AllowanceStoreError(REASON_LOCK_LOST, "lock vanished before release")

        self.store._save = dropping_save
        self.store._release = losing_release
        total = self.ledger.record_completed(identity, message(cost=0.25))

        self.assertEqual(calls, ["save", "save"], "the replay did not re-attempt the write")
        self.assertEqual(total, Decimal("0.25"))
        self.assertEqual(
            self.results(),
            [{"key": identity.key, "ordinal": 1, "cost": "0.25"}],
        )

    def test_a_landed_hole_replay_stays_one_hole(self) -> None:
        identity = self.identity(KIND_CONTINUE)
        original_release = self.store._release
        raised = []

        def losing(generation):
            original_release(generation)
            if not raised:
                raised.append(generation)
                raise AllowanceStoreError(REASON_LOCK_LOST, "lock vanished before release")

        self.store._release = losing
        self.assertEqual(self.ledger.record_hole(identity), Decimal(0))
        recorded = self.results()
        self.assertEqual(len(recorded), 1)
        self.assertIsNone(recorded[0]["cost"])

    def test_it_never_touches_the_lock_itself(self) -> None:
        source = Path(ledger_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("lock_path", "unlink", "os.remove", "acquired", "generation"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


# --------------------------------------------------------------------------
# What the ledger is allowed to leave behind
# --------------------------------------------------------------------------


class PrivacyTests(LedgerTestCase):
    def test_only_the_key_and_the_cost_reach_durable_state(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.ledger.record_completed(
            identity,
            message(
                cost=0.25,
                markers={"executor-published-handoff": True, "secret-marker": True},
                result="the assistant said something confidential",
                prompt="the directive the orchestrator sent",
                account="acct_12345",
                transcript="/home/someone/.claude/projects/x/session.jsonl",
                model="claude-opus-5",
            ),
        )
        text = self.path.read_text(encoding="utf-8")
        for leaked in ("secret-marker", "confidential", "directive", "acct_12345",
                       "session.jsonl", "claude-opus-5", "executor-published-handoff",
                       "prompt", "transcript", "account", "model", "markers", "num_turns",
                       "subtype"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, text)

        entry = self.results()[0]
        self.assertEqual(sorted(entry), ["cost", "key", "ordinal"])
        self.assertEqual(entry["key"], identity.key)

    def test_the_key_carries_only_the_session_the_controller_assigned(self) -> None:
        identity = self.identity(KIND_LAUNCH)
        self.assertEqual(identity.key.split(":"), [SESSION, "launch", "1"])
        self.assertEqual(str(uuid.UUID(identity.key.split(":")[0])), SESSION)

    def test_a_hole_leaves_nothing_but_the_key(self) -> None:
        identity = self.identity(KIND_CONTINUE)
        self.ledger.record_failure(
            identity, ClaudeWorkerError(REASON_WORKER_FATAL, "api_error: rate limited for acct_9"),
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("acct_9", text)
        self.assertNotIn("rate limited", text)
        self.assertEqual(sorted(self.results()[0]), ["cost", "key", "ordinal"])


# --------------------------------------------------------------------------
# Conventions this repository enforces everywhere
# --------------------------------------------------------------------------


class ConventionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = [
            Path(ledger_module.__file__),
            Path(__file__).resolve().parents[1] / "ai_dev_flow" / "claude_allowance_store.py",
        ]

    def test_both_modules_parse_under_the_minimum_python(self) -> None:
        for path in self.paths:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_postponed_annotations_stay_in_the_header(self) -> None:
        for path in self.paths:
            with self.subTest(path=path.name):
                header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
                self.assertIn("from __future__ import annotations", header)

    def test_the_ledger_imports_only_the_accepted_seams(self) -> None:
        source = Path(ledger_module.__file__).read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * (node.level or 0)) + (node.module or ""))
        self.assertEqual(
            names,
            {"__future__", "dataclasses", "decimal", "time", "typing",
             ".claude_allowance_store", ".claude_runtime", ".claude_worker",
             ".session_binding", ".session_lifecycle"},
        )

    def test_the_store_import_set_did_not_change(self) -> None:
        """The composition method must add no dependency to the accepted store."""
        source = self.paths[1].read_text(encoding="utf-8")
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

    def test_it_neither_wires_nor_bypasses_the_production_lifecycle(self) -> None:
        """This rail delivers the boundary; wiring is the next one, with no silent bypass.

        Checked over identifiers rather than raw text, because naming the accepted
        seams in prose is how the boundary explains itself; calling one is the
        thing this rail may not do.
        """
        tree = ast.parse(Path(ledger_module.__file__).read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    used.add(alias.name)
        for forbidden in ("launch_session", "continue_session", "invoke_orchestrator",
                          "SessionRegistry", "run_request", "stop_session",
                          "start_worker", "shutdown_worker"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, used)


if __name__ == "__main__":
    unittest.main()
