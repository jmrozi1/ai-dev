from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import authorization, context_lifecycle
from ai_dev_flow.context_lifecycle import (
    CONTEXT_POLICY_FRESH,
    CONTEXT_POLICY_PERSISTENT,
    DEFAULT_ROTATION_THRESHOLD,
    EVENT_COMPACTION_OBSERVED,
    EVENT_COMPACTION_UNIDENTIFIABLE,
    OBSERVATION_HEALTHY,
    OBSERVATION_UNAVAILABLE,
    OBSERVATION_UNHEALTHY,
    ContextLifecycleError,
    ContextLifecycleLedger,
    SessionContextLifecycle,
    decode_lifecycle_event,
    resolve_rotation_threshold,
)


SESSION = "1a2b3c4d-0057-4000-8000-0000000000c8"
OTHER_SESSION = "1a2b3c4d-0057-4000-8000-0000000000c9"
BOUNDARY = "83e1ea6e-7979-4dca-9cd8-85d26656f905"
OTHER_BOUNDARY = "0f0f0f0f-1111-4222-8333-444444444444"

MODULE_PATH = Path(context_lifecycle.__file__)


class SystemMessage(object):
    """Shaped like the SDK's `SystemMessage`: a typed subtype, a `data` dict.

    Deliberately carries no `session_id` and no `uuid` attribute, because the real
    one does not either -- both read absent on a real `compact_boundary` while the
    dict carried the identity. A fake that grew the attributes would make the one
    mistake this checkpoint exists to prevent untestable.
    """

    def __init__(self, subtype, data=None):
        self.subtype = subtype
        self.data = {} if data is None else data


class ResultMessage(object):
    """The message that really does carry `session_id` as an attribute."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.subtype = "success"


def boundary(session_id=SESSION, uuid=BOUNDARY, **extra):
    data = {"session_id": session_id, "uuid": uuid}
    data.update(extra)
    return SystemMessage("compact_boundary", data)


def observed(session_id=SESSION, uuid=BOUNDARY):
    return {"event": EVENT_COMPACTION_OBSERVED, "session_id": session_id, "uuid": uuid}


class DecodingTests(unittest.TestCase):
    def test_a_compact_boundary_decodes_to_one_identified_compaction(self) -> None:
        self.assertEqual(
            decode_lifecycle_event(boundary()),
            {"event": EVENT_COMPACTION_OBSERVED, "session_id": SESSION, "uuid": BOUNDARY},
        )

    def test_identity_is_read_from_the_data_dict_and_never_from_an_attribute(self) -> None:
        """The one mistake that would count zero forever while every run looked healthy."""
        message = boundary()
        # The trap: the SDK's SystemMessage genuinely has neither attribute, so a
        # decoder written in the shape of the ResultMessage reduction reads nothing.
        self.assertFalse(hasattr(message, "session_id"))
        self.assertFalse(hasattr(message, "uuid"))
        event = decode_lifecycle_event(message)
        self.assertEqual(event["session_id"], SESSION)
        self.assertEqual(event["uuid"], BOUNDARY)

    def test_an_attribute_never_supplies_an_identity_the_data_dict_lacks(self) -> None:
        message = SystemMessage("compact_boundary", {"trigger": "manual"})
        message.session_id = SESSION
        message.uuid = BOUNDARY
        self.assertEqual(
            decode_lifecycle_event(message)["event"], EVENT_COMPACTION_UNIDENTIFIABLE
        )

    def test_an_attribute_never_overrides_the_identity_the_data_dict_carries(self) -> None:
        message = boundary()
        message.session_id = OTHER_SESSION
        message.uuid = OTHER_BOUNDARY
        event = decode_lifecycle_event(message)
        self.assertEqual((event["session_id"], event["uuid"]), (SESSION, BOUNDARY))

    def test_a_compacting_status_is_not_a_completed_compaction(self) -> None:
        # It fires even when the compaction fails, so it is never countable.
        self.assertIsNone(
            decode_lifecycle_event(
                SystemMessage("status", {"status": "compacting", "session_id": SESSION})
            )
        )

    def test_compact_progress_is_not_a_completed_compaction(self) -> None:
        self.assertIsNone(
            decode_lifecycle_event(
                SystemMessage(
                    "compact_progress", {"session_id": SESSION, "uuid": BOUNDARY}
                )
            )
        )

    def test_a_result_message_is_never_a_compaction(self) -> None:
        self.assertIsNone(decode_lifecycle_event(ResultMessage(SESSION)))

    def test_other_system_messages_are_never_compactions(self) -> None:
        for subtype in ("init", "thinking_tokens", "status"):
            with self.subTest(subtype=subtype):
                self.assertIsNone(
                    decode_lifecycle_event(SystemMessage(subtype, {"session_id": SESSION}))
                )

    def test_a_boundary_without_a_trustworthy_identity_is_unidentifiable(self) -> None:
        for data in ({"uuid": BOUNDARY}, {"session_id": SESSION}, {"session_id": "", "uuid": " "}):
            with self.subTest(data=data):
                event = decode_lifecycle_event(SystemMessage("compact_boundary", data))
                self.assertEqual(event["event"], EVENT_COMPACTION_UNIDENTIFIABLE)

    def test_a_boundary_with_no_data_mapping_is_unidentifiable(self) -> None:
        event = decode_lifecycle_event(SystemMessage("compact_boundary", data=["not", "a", "map"]))
        self.assertEqual(event["event"], EVENT_COMPACTION_UNIDENTIFIABLE)

    def test_metadata_is_not_identity_and_does_not_travel(self) -> None:
        event = decode_lifecycle_event(
            boundary(
                trigger="manual",
                pre_tokens=15254,
                post_tokens=2253,
                cumulative_dropped_tokens=13001,
            )
        )
        self.assertEqual(sorted(event), ["event", "session_id", "uuid"])

    def test_the_decoder_answers_rather_than_raising_on_anything_unexpected(self) -> None:
        for message in (object(), None, "not a message", 7):
            with self.subTest(message=message):
                self.assertIsNone(decode_lifecycle_event(message))


class CountingTests(unittest.TestCase):
    def context(self, **overrides):
        arguments = {"role": "executor", "observed_from_start": True}
        arguments.update(overrides)
        return SessionContextLifecycle(SESSION, **arguments)

    def test_a_valid_boundary_increments_exactly_once(self) -> None:
        context = self.context()
        self.assertTrue(context.observe(observed()))
        self.assertEqual(context.observed, 1)
        self.assertEqual(context.reading().count, 1)
        self.assertEqual(context.reading().health, OBSERVATION_HEALTHY)

    def test_a_duplicate_identity_pair_does_not_count_twice(self) -> None:
        context = self.context()
        self.assertTrue(context.observe(observed()))
        self.assertFalse(context.observe(observed()))
        self.assertFalse(context.observe(observed()))
        self.assertEqual(context.observed, 1)

    def test_two_boundaries_on_one_session_are_two_compactions(self) -> None:
        context = self.context()
        context.observe(observed())
        context.observe(observed(uuid=OTHER_BOUNDARY))
        self.assertEqual(context.observed, 2)

    def test_the_same_uuid_under_another_session_cannot_contaminate_this_one(self) -> None:
        context = self.context()
        context.observe(observed())
        with self.assertRaises(ContextLifecycleError) as caught:
            context.observe(observed(session_id=OTHER_SESSION))
        self.assertEqual(caught.exception.reason, context_lifecycle.REASON_SESSION_MISMATCH)
        self.assertEqual(context.observed, 1)

    def test_a_mismatched_session_id_fails_closed_rather_than_counting(self) -> None:
        context = self.context()
        with self.assertRaises(ContextLifecycleError) as caught:
            context.observe(observed(session_id=OTHER_SESSION, uuid=OTHER_BOUNDARY))
        self.assertEqual(caught.exception.reason, context_lifecycle.REASON_SESSION_MISMATCH)
        self.assertEqual(context.observed, 0)
        self.assertEqual(context.reading().count, 0)

    def test_an_unidentifiable_boundary_does_not_increment_and_is_visible(self) -> None:
        context = self.context()
        context.observe(observed())
        context.observe({"event": EVENT_COMPACTION_UNIDENTIFIABLE, "detail": "no identity"})
        reading = context.reading()
        self.assertEqual(reading.observed, 1)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    def test_an_identityless_compaction_event_marks_partial_without_counting(self) -> None:
        context = self.context()
        context.observe({"event": EVENT_COMPACTION_OBSERVED, "session_id": SESSION})
        self.assertEqual(context.observed, 0)
        self.assertEqual(context.reading().health, OBSERVATION_UNHEALTHY)

    def test_an_unsupported_event_is_refused_rather_than_ignored(self) -> None:
        context = self.context()
        for event in ({"event": "rotation-complete"}, {}, "not an event"):
            with self.subTest(event=event):
                with self.assertRaises(ContextLifecycleError) as caught:
                    context.observe(event)
                self.assertEqual(
                    caught.exception.reason, context_lifecycle.REASON_UNSUPPORTED_EVENT
                )

    def test_partial_observation_never_heals(self) -> None:
        context = self.context()
        context.observe({"event": EVENT_COMPACTION_UNIDENTIFIABLE, "detail": "no identity"})
        context.observe(observed())
        self.assertEqual(context.reading().health, OBSERVATION_UNHEALTHY)


class HealthTests(unittest.TestCase):
    def test_a_fresh_healthy_zero_is_distinguishable_from_unknown_prior_history(self) -> None:
        fresh = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        adopted = SessionContextLifecycle(SESSION, role="executor")

        self.assertEqual(fresh.reading().health, OBSERVATION_HEALTHY)
        self.assertEqual(fresh.reading().count, 0)

        self.assertEqual(adopted.reading().health, OBSERVATION_UNAVAILABLE)
        self.assertIsNone(adopted.reading().count)
        self.assertEqual(adopted.reading().observed, 0)

    def test_unknown_prior_history_is_never_rendered_as_zero(self) -> None:
        adopted = SessionContextLifecycle(SESSION, role="executor")
        rendered = adopted.reading().to_dict()
        self.assertIsNone(rendered["count"])
        self.assertNotEqual(rendered["count"], 0)
        self.assertIn("floor rather than a count", rendered["detail"])

    def test_a_partial_count_is_never_presented_as_complete(self) -> None:
        context = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        context.observe(observed())
        context.observe({"event": EVENT_COMPACTION_UNIDENTIFIABLE, "detail": "torn"})
        reading = context.reading()
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.observed, 1)

    def test_the_three_health_states_stay_distinct(self) -> None:
        healthy = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        unavailable = SessionContextLifecycle(SESSION, role="executor")
        unhealthy = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        unhealthy.observe({"event": EVENT_COMPACTION_UNIDENTIFIABLE, "detail": "torn"})
        self.assertEqual(
            [healthy.health, unavailable.health, unhealthy.health],
            [OBSERVATION_HEALTHY, OBSERVATION_UNAVAILABLE, OBSERVATION_UNHEALTHY],
        )


class ThresholdTests(unittest.TestCase):
    def counted(self, boundaries, **overrides):
        arguments = {"role": "executor", "observed_from_start": True}
        arguments.update(overrides)
        context = SessionContextLifecycle(SESSION, **arguments)
        for index in range(boundaries):
            context.observe(observed(uuid="{0:08d}-0000-4000-8000-000000000000".format(index)))
        return context

    def test_five_observed_compactions_do_not_mark_and_six_do(self) -> None:
        self.assertIs(self.counted(5).reading().rotation_marked, False)
        self.assertIs(self.counted(6).reading().rotation_marked, True)

    def test_the_default_threshold_is_six(self) -> None:
        self.assertEqual(DEFAULT_ROTATION_THRESHOLD, 6)
        self.assertEqual(self.counted(0).reading().threshold, 6)

    def test_the_threshold_is_configurable(self) -> None:
        self.assertIs(self.counted(2, threshold=3).reading().rotation_marked, False)
        self.assertIs(self.counted(3, threshold=3).reading().rotation_marked, True)
        self.assertIs(self.counted(9, threshold=10).reading().rotation_marked, False)

    def test_an_invalid_threshold_is_refused(self) -> None:
        for value in (0, -1, 2.5, "6", True):
            with self.subTest(value=value):
                with self.assertRaises(ContextLifecycleError) as caught:
                    resolve_rotation_threshold(value)
                self.assertEqual(
                    caught.exception.reason, context_lifecycle.REASON_INVALID_THRESHOLD
                )

    def test_an_unproven_history_under_the_threshold_is_undetermined_not_unreached(self) -> None:
        reading = self.counted(5, observed_from_start=False).reading()
        self.assertIsNone(reading.rotation_marked)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.observed, 5)

    def test_an_unproven_history_that_already_reaches_the_threshold_still_marks(self) -> None:
        # The observed number is a floor, so reaching the threshold is provable even
        # when the total is not. Nothing is inferred about the unobserved history.
        reading = self.counted(6, observed_from_start=False).reading()
        self.assertIs(reading.rotation_marked, True)
        self.assertIsNone(reading.count)

    def test_a_partial_observation_under_the_threshold_is_undetermined(self) -> None:
        context = self.counted(2)
        context.observe({"event": EVENT_COMPACTION_UNIDENTIFIABLE, "detail": "torn"})
        self.assertIsNone(context.reading().rotation_marked)


class CeilingIndependenceTests(unittest.TestCase):
    """The identical default six is a coincidence, and must stay one."""

    def test_the_module_imports_nothing_from_authorization(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("authorization", imported)
        self.assertNotIn(".authorization", imported)

    def test_moving_the_concurrency_ceiling_does_not_move_the_rotation_threshold(self) -> None:
        with patch.object(authorization, "CONCURRENCY_CEILING_DEFAULT", 2):
            self.assertEqual(resolve_rotation_threshold(), 6)
            context = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
            self.assertEqual(context.threshold, 6)
            for index in range(5):
                context.observe(observed(uuid="{0:08d}-0000-4000-8000-000000000000".format(index)))
            self.assertIs(context.reading().rotation_marked, False)

    def test_moving_the_rotation_threshold_does_not_move_the_concurrency_ceiling(self) -> None:
        ledger = ContextLifecycleLedger(threshold=2)
        self.assertEqual(ledger.rotation_threshold, 2)
        self.assertEqual(authorization.CONCURRENCY_CEILING_DEFAULT, 6)


class LedgerTests(unittest.TestCase):
    def ledger(self, **overrides):
        ledger = ContextLifecycleLedger(**overrides)
        ledger.begin(SESSION, role="executor", observed_from_start=True)
        return ledger

    def test_events_are_folded_into_exactly_the_session_they_arrived_for(self) -> None:
        ledger = self.ledger()
        context = ledger.observe(SESSION, [observed(), observed(uuid=OTHER_BOUNDARY)])
        self.assertEqual(context.observed, 2)

    def test_an_event_for_an_unobserved_session_is_refused_rather_than_attributed(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(ContextLifecycleError) as caught:
            ledger.observe(OTHER_SESSION, [observed(session_id=OTHER_SESSION)])
        self.assertEqual(caught.exception.reason, context_lifecycle.REASON_UNKNOWN_CONTEXT)

    def test_a_foreign_event_increments_nothing_and_marks_the_channel_unhealthy(self) -> None:
        ledger = self.ledger()
        ledger.begin(OTHER_SESSION, role="executor", observed_from_start=True)
        ledger.observe(SESSION, [observed(), observed(session_id=OTHER_SESSION)])

        self.assertEqual(ledger.get(SESSION).observed, 1)
        self.assertEqual(ledger.get(SESSION).reading().health, OBSERVATION_UNHEALTHY)
        # The session the stray event named is no evidence about anything either.
        self.assertEqual(ledger.get(OTHER_SESSION).observed, 0)
        self.assertEqual(ledger.get(OTHER_SESSION).reading().health, OBSERVATION_HEALTHY)

    def test_taking_ownership_again_never_resets_a_count_or_upgrades_a_history(self) -> None:
        ledger = ContextLifecycleLedger()
        ledger.begin(SESSION, role="executor")
        ledger.observe(SESSION, [observed()])
        ledger.begin(SESSION, role="executor", observed_from_start=True)
        reading = ledger.get(SESSION).reading()
        self.assertEqual(reading.observed, 1)
        self.assertEqual(reading.health, OBSERVATION_UNAVAILABLE)

    def test_forgetting_a_session_drops_its_whole_deduplication_memory(self) -> None:
        ledger = self.ledger()
        ledger.observe(SESSION, [observed()])
        ledger.forget(SESSION)
        self.assertIsNone(ledger.get(SESSION))
        self.assertEqual(ledger.readings(), {})

    def test_a_failed_invocation_keeps_its_events_and_degrades_the_claim(self) -> None:
        ledger = self.ledger()
        context = ledger.observe_failure(SESSION, "the invocation failed", [observed()])
        self.assertEqual(context.observed, 1)
        reading = context.reading()
        self.assertIsNone(reading.count)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    def test_a_failed_invocation_refuses_the_same_events_a_good_one_would(self) -> None:
        ledger = self.ledger()
        context = ledger.observe_failure(
            SESSION, "the invocation failed",
            [observed(session_id=OTHER_SESSION), {"event": "nonsense"}, "junk"],
        )
        self.assertEqual(context.observed, 0)
        self.assertEqual(context.reading().health, OBSERVATION_UNHEALTHY)

    def test_a_failure_for_a_session_this_ledger_dropped_raises_nothing(self) -> None:
        # It runs while an error is already on its way out; a session that is gone is
        # nothing to misrepresent, and must not replace the failure the caller sees.
        ledger = self.ledger()
        self.assertIsNone(ledger.observe_failure(OTHER_SESSION, "the invocation failed"))

    def test_the_role_context_policy_is_recorded_for_every_session(self) -> None:
        ledger = ContextLifecycleLedger()
        ledger.begin(SESSION, role="executor", observed_from_start=True)
        ledger.begin(OTHER_SESSION, role="reviewer", observed_from_start=True)
        readings = ledger.readings()
        self.assertEqual(readings[SESSION]["contextPolicy"], CONTEXT_POLICY_PERSISTENT)
        self.assertEqual(readings[OTHER_SESSION]["contextPolicy"], CONTEXT_POLICY_FRESH)

    def test_marked_sessions_are_reported_and_nothing_else_is(self) -> None:
        ledger = ContextLifecycleLedger(threshold=2)
        ledger.begin(SESSION, role="executor", observed_from_start=True)
        ledger.begin(OTHER_SESSION, role="executor", observed_from_start=True)
        ledger.observe(SESSION, [observed(), observed(uuid=OTHER_BOUNDARY)])
        ledger.observe(OTHER_SESSION, [observed(session_id=OTHER_SESSION)])
        self.assertEqual(ledger.rotation_marked_session_ids(), (SESSION,))


class InstalledSdkFidelityTests(unittest.TestCase):
    """Decode the real SDK type, not only the fake shaped like it.

    Every other test here builds its own `SystemMessage`, so every other test would
    keep passing if the real one were shaped differently. This is the one that would
    fail -- and the trap it guards is precisely a decoder that reads a plausible
    attribute and observes nothing forever.
    """

    def setUp(self) -> None:
        try:
            from claude_agent_sdk import SystemMessage as InstalledSystemMessage
        except ImportError:
            self.skipTest("claude-agent-sdk is not installed on this interpreter")
        self.SystemMessage = InstalledSystemMessage

    def test_the_real_system_message_carries_identity_only_in_its_data(self) -> None:
        message = self.SystemMessage(
            subtype="compact_boundary",
            data={
                "session_id": SESSION,
                "uuid": BOUNDARY,
                "compact_metadata": {"trigger": "manual", "pre_tokens": 15254},
            },
        )
        self.assertFalse(hasattr(message, "session_id"))
        self.assertFalse(hasattr(message, "uuid"))
        self.assertEqual(
            decode_lifecycle_event(message),
            {"event": EVENT_COMPACTION_OBSERVED, "session_id": SESSION, "uuid": BOUNDARY},
        )

    def test_a_real_compacting_status_message_is_still_not_countable(self) -> None:
        self.assertIsNone(
            decode_lifecycle_event(
                self.SystemMessage(
                    subtype="status", data={"status": "compacting", "session_id": SESSION}
                )
            )
        )

    def test_the_real_result_message_is_the_one_that_has_the_attribute(self) -> None:
        """Why the two reductions cannot share a shape, stated against the real types."""
        import dataclasses

        from claude_agent_sdk import ResultMessage as InstalledResultMessage

        system_fields = {f.name for f in dataclasses.fields(self.SystemMessage)}
        result_fields = {f.name for f in dataclasses.fields(InstalledResultMessage)}
        self.assertEqual(system_fields, {"subtype", "data"})
        self.assertIn("session_id", result_fields)


class NotAnEventLogTests(unittest.TestCase):
    """Structural: this is manager lifecycle state, not history."""

    def test_only_identity_pairs_are_retained_per_session(self) -> None:
        context = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        context.observe(observed())
        self.assertEqual(context.identities(), ((SESSION, BOUNDARY),))

    def test_no_transcript_or_provider_metadata_is_retained(self) -> None:
        context = SessionContextLifecycle(SESSION, role="executor", observed_from_start=True)
        context.observe(observed())
        rendered = repr(sorted(vars(context).items()))
        for leaked in ("trigger", "pre_tokens", "post_tokens", "cumulative", "transcript"):
            self.assertNotIn(leaked, rendered)


if __name__ == "__main__":
    unittest.main()
