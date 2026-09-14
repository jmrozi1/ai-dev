"""The seam's own guarantees, and the drift guards that keep them honest.

Contract section 6. These tests attack the boundary directly rather than through
the chat loop, because the boundary's promises have to hold for an implementation
this repository has never seen.
"""

from __future__ import annotations

import inspect
import os
import re
import unittest

import support  # noqa: F401  (puts the harness on sys.path)

import launch_boundary as lb
import session_manager
import store as store_module
from support import VALIDATOR


class ClosedPacketSchema(unittest.TestCase):
    """Contract 6: what holds the seam closed is the closed schema, not the denylist."""

    BASE = dict(request_id="req_aaaaaaaa", chat_id="cht_aaaaaaaa",
                session_id="ses_aaaaaaaa", created_at="2026-09-12T10:00:00Z",
                instruction_encoding="utf-8", instruction_text="hello")

    def test_the_declared_packet_is_accepted(self):
        packet = lb.LaunchInstruction(**self.BASE)
        self.assertEqual(sorted(packet.as_record()), sorted(self.BASE))

    def test_a_denylisted_mechanics_name_is_refused(self):
        for name in ("command", "cwd", "host", "vscode_workspace", "script_path"):
            with self.assertRaises(lb.LaunchBoundaryError) as caught:
                lb.LaunchInstruction(**dict(self.BASE, **{name: "x"}))
            self.assertIn("mechanics", str(caught.exception))

    def test_a_name_nobody_forbade_is_refused_just_as_firmly(self):
        """The probe that matters. A denylist-only guard passes this test's
        opposite: these names are mechanics by any reading and appear on no list."""
        for name in ("quux_transport", "bridge_channel", "ide_handle",
                     "remote_desktop_id", "launch_agent_sh", "tty"):
            with self.assertRaises(lb.LaunchBoundaryError) as caught:
                lb.LaunchInstruction(**dict(self.BASE, **{name: "x"}))
            self.assertIn("closed schema", str(caught.exception))

    def test_the_delivery_packet_is_closed_identically(self):
        base = dict(delivery_id="dlv_aaaaaaaa", chat_id="cht_aaaaaaaa",
                    session_id="ses_aaaaaaaa", sequence=1,
                    created_at="2026-09-12T10:00:00Z",
                    instruction_encoding="utf-8", instruction_text="hello")
        lb.DeliveryInstruction(**base)
        for name in ("command", "quux_transport"):
            with self.assertRaises(lb.LaunchBoundaryError):
                lb.DeliveryInstruction(**dict(base, **{name: "x"}))

    def test_an_empty_instruction_is_refused(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchInstruction(**dict(self.BASE, instruction_text=""))

    def test_another_encoding_fails_closed(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchInstruction(**dict(self.BASE, instruction_encoding="utf-16"))

    def test_mechanics_inside_a_value_are_accepted_by_design(self):
        """Contract 6 says so explicitly. The guarantee is that nothing on this
        side of the seam parses them, which `test_no_behaviour_branches_on_a_handle`
        checks as a fact."""
        packet = lb.LaunchInstruction(
            **dict(self.BASE, instruction_text="run /home/x/scripts/launch_agent.sh"))
        self.assertIn("launch_agent.sh", packet.instruction_text)


class LaunchResultConsistency(unittest.TestCase):
    """Contract 6.3's conditional fields, enforced at the seam rather than only
    at the store: a launcher that reports `accepted` without a handle has not
    accepted anything, and this is where that stops."""

    def test_accepted_requires_a_handle(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("accepted")
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("accepted", agent_handle="")

    def test_accepted_forbids_a_category(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("accepted", agent_handle="h", failure_category="rejected")

    def test_failed_requires_a_category_from_the_closed_set(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("failed")
        for bogus in ("looks_ok", "timeout", "stalled", "unknown", ""):
            with self.assertRaises(lb.LaunchBoundaryError):
                lb.LaunchResult("failed", failure_category=bogus)
        for good in lb.FAILURE_CATEGORIES:
            lb.LaunchResult("failed", failure_category=good)

    def test_failed_forbids_a_handle(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("failed", failure_category="rejected", agent_handle="h")

    def test_unknown_carries_neither(self):
        lb.LaunchResult("unknown")
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("unknown", agent_handle="h")
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.LaunchResult("unknown", failure_category="rejected")


class PayloadConstruction(unittest.TestCase):
    def test_a_launcher_may_not_source_an_event_to_the_harness(self):
        """Contract 5.2: what the harness observed is a session_observation. A
        launcher that could source to `harness` could authorise the harness's own
        conclusions under the launcher's name."""
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.EventPayload(1, "harness", "recognized", b"{}", interpreted_type="x")

    def test_recognized_must_name_a_type_and_others_must_not(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.EventPayload(1, "agent", "recognized", b"{}")
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.EventPayload(1, "agent", "malformed", b"{}", interpreted_type="x")
        lb.EventPayload(1, "agent", "malformed", b"{}")

    def test_raw_must_be_bytes_so_it_can_be_preserved_verbatim(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.EventPayload(1, "agent", "malformed", "not bytes")

    def test_only_a_recognized_agent_assistant_text_payload_is_chat(self):
        """All four conditions are load-bearing (contract 4.2, 7 P2a)."""
        def payload(**kw):
            base = dict(sequence=1, source="agent", interpretation="recognized",
                        raw=b"{}", interpreted_type="assistant_text", text="hello")
            base.update(kw)
            return lb.EventPayload(**base)

        self.assertTrue(payload().is_chat_text)
        self.assertFalse(payload(source="launcher").is_chat_text)
        self.assertFalse(payload(interpreted_type="turn_complete").is_chat_text)
        self.assertFalse(payload(interpretation="unrecognized", interpreted_type=None,
                                 text="hello").is_chat_text)
        self.assertFalse(payload(text=None).is_chat_text)

    def test_events_must_return_payload_objects(self):
        with self.assertRaises(lb.LaunchBoundaryError):
            lb.EventsPage([{"sequence": 1}])


class BoundaryShape(unittest.TestCase):
    """There is no status, health, poll or describe operation, and no timer."""

    OPERATIONS = ("launch", "stop", "events", "deliver")

    def test_the_operation_set_is_exactly_the_contracts(self):
        public = set(
            name for name in dir(lb.LaunchBoundary)
            if not name.startswith("_") and callable(getattr(lb.LaunchBoundary, name))
        )
        self.assertEqual(public, set(self.OPERATIONS))

    def test_there_is_no_interrogation_operation(self):
        for forbidden in ("status", "health", "poll", "describe", "ping", "is_alive"):
            self.assertFalse(hasattr(lb.LaunchBoundary, forbidden),
                             "the boundary grew a %r operation" % forbidden)

    def test_no_operation_takes_a_timeout_deadline_or_retry(self):
        for name in self.OPERATIONS:
            parameters = inspect.signature(getattr(lb.LaunchBoundary, name)).parameters
            for bad in ("timeout", "deadline", "interval", "retries", "retry",
                        "wait", "poll_interval"):
                self.assertNotIn(bad, parameters,
                                 "%s grew a %r parameter" % (name, bad))

    def test_nothing_above_the_seam_measures_elapsed_time(self):
        """A source scan over the three environment-independent modules. It is a
        lint, not the guarantee -- the guarantee is the operation set above and
        contract 5.2's evidence rules, which no timer can satisfy."""
        banned = re.compile(
            r"\b(sleep|monotonic|perf_counter|time\.time|timeout|Timer|deadline)\b")
        for module in (lb, session_manager, store_module):
            source = inspect.getsource(module)
            # Strip prose: the words appear in comments explaining their absence.
            code = "\n".join(
                line.split("#")[0] for line in source.splitlines()
                if not line.strip().startswith("#"))
            code = re.sub(r'"""(?:.|\n)*?"""', "", code)
            found = banned.findall(code)
            self.assertEqual(found, [], "%s references %s" % (module.__name__, found))


class DriftGuards(unittest.TestCase):
    """Two copies of one table is the divergence this contract exists to prevent."""

    def test_the_transition_table_matches_the_validators(self):
        self.assertEqual(session_manager.AUTHORIZED_TRANSITIONS,
                         VALIDATOR.AUTHORIZED_TRANSITIONS)

    def test_the_mechanics_denylist_matches_the_validators(self):
        self.assertEqual(set(lb._MECHANICS_FIELD_NAMES),
                         set(VALIDATOR.BRIDGE_SPECIFIC_FIELDS))

    def test_the_launch_packet_fields_match_the_validators_record_spec(self):
        spec = set(VALIDATOR.RECORD_SPECS["launch_request"])
        self.assertEqual(set(lb.LaunchInstruction.FIELDS), spec)

    def test_the_delivery_packet_fields_match_the_validators_record_spec(self):
        spec = set(VALIDATOR.RECORD_SPECS["delivery_request"]) - {"acknowledged"}
        self.assertEqual(set(lb.DeliveryInstruction.FIELDS), spec)

    def test_the_failure_categories_match_the_validators(self):
        self.assertEqual(set(lb.FAILURE_CATEGORIES),
                         set(VALIDATOR.LAUNCH_FAILURE_CATEGORIES))

    def test_the_capability_vocabularies_match_the_validators(self):
        self.assertEqual(set(lb.CONTINUATION_MODES), set(VALIDATOR.CONTINUATION_MODES))
        self.assertEqual(set(lb.RESPONSE_SHAPES), set(VALIDATOR.RESPONSE_SHAPES))
        self.assertEqual(lb.PAYLOAD_STREAM_END, VALIDATOR.STREAM_END_TYPE)

    def test_terminal_states_match_the_validators(self):
        self.assertEqual(set(store_module.TERMINAL_SESSION_STATES),
                         set(VALIDATOR.TERMINAL_SESSION_STATES))


if __name__ == "__main__":
    unittest.main()
