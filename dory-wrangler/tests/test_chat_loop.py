"""The full chat loop, against every launcher and every capability combination.

Contract sections 4, 5, 7 and 8. Each of these produces a real store and hands
it to the contract validator; none of them asserts a local opinion about what
the contract permits.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import support
from support import CONFIGURATIONS, StoreCheck, end_chat, expected_transcript, run_three_turns

from dory_wrangler.errors import NotPermitted


class FullLoopAgainstEveryLauncher(unittest.TestCase, StoreCheck):
    """Every configuration, run as two tests: the `scripted-stub` half is
    portable and the `dev-local` half needs the development host
    (`tests/categories.py`). Together they are every configuration, which the
    first test holds."""

    def test_the_two_halves_are_every_configuration(self):
        self.assertEqual(sorted(support.STUB_CONFIGURATIONS + support.DEV_LOCAL_CONFIGURATIONS),
                         sorted(CONFIGURATIONS))
        self.assertEqual(len(support.STUB_CONFIGURATIONS), 4)
        self.assertEqual(len(support.DEV_LOCAL_CONFIGURATIONS), 2)

    def three_turns_under(self, configurations):
        for name, config in configurations:
            with self.subTest(configuration=name):
                harness = support.harness(config)
                self.addCleanup(support.release, harness)
                chat_id = run_three_turns(harness, "Full loop: %s" % name)
                self.assertEqual(harness.transcript(chat_id), expected_transcript())
                end_chat(harness, chat_id)
                self.assert_store_valid(
                    harness.store, "chat-loop-%s" % name,
                    "Three turns through the %s configuration, produced by the "
                    "harness rather than written by hand." % name)

    def test_three_turns_under_every_stub_configuration(self):
        self.three_turns_under(support.STUB_CONFIGURATIONS)

    def test_three_turns_under_every_dev_local_configuration(self):
        self.three_turns_under(support.DEV_LOCAL_CONFIGURATIONS)

    def capabilities_recorded_under(self, configurations):
        """The store records what the launcher declared, and the declaration is
        the launcher's, not the contract's."""
        for name, config in configurations:
            with self.subTest(configuration=name):
                harness = support.harness(config)
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Declared capabilities")
                harness.send_turn(chat_id, "hello")
                session = support.view(harness).sessions_of(chat_id)[0]
                self.assertEqual(session["launcher_capabilities"],
                                 harness.capabilities.as_record())
                self.assertIsNone(
                    session["launcher_capabilities"]["instruction_bound_bytes"],
                    "no instruction-payload bound has been measured anywhere")
                end_chat(harness, chat_id)

    def test_every_stub_configuration_records_the_capabilities_it_declared(self):
        self.capabilities_recorded_under(support.STUB_CONFIGURATIONS)

    def test_every_dev_local_configuration_records_the_capabilities_it_declared(self):
        self.capabilities_recorded_under(support.DEV_LOCAL_CONFIGURATIONS)


class DurableRecordsAreCanonical(unittest.TestCase, StoreCheck):
    """Contract D1. Chat history is readable from durable records alone."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="dory-store-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "records.jsonl")

    def test_a_chat_reopens_after_a_restart_with_no_live_anything(self):
        config = {"launcher": "dev-local", "options": {"profile": "one_shot"}}
        first = support.harness(config, store_path=self.path)
        self.addCleanup(support.release, first)
        chat_id = run_three_turns(first, "Reopen after restart")
        expected = first.transcript(chat_id)

        # A different process would do this; a different Store object over the
        # same file is the same claim, with no live agent and no live launcher.
        reopened = support.harness(config, store_path=self.path)
        self.assertEqual(reopened.transcript(chat_id), expected)
        self.assertEqual(support.view(reopened).non_terminal_sessions(chat_id), [])
        self.assertEqual(support.view(reopened).open_bindings(chat_id), [])
        self.assert_store_valid(reopened.store, "reopen-after-restart",
                                "Read back from disk by a second harness.")

    def test_a_live_session_survives_a_restart_and_the_user_is_never_stuck(self):
        """Contract 5.4. A development agent process does not survive the harness
        that started it, so re-attachment genuinely fails here; the session goes
        to `unknown`, the binding stays open, and the user's exit is `abandon`."""
        config = {"launcher": "dev-local", "options": {"profile": "persistent"}}
        first = support.harness(config, store_path=self.path)
        self.addCleanup(support.release, first)
        chat_id = first.create_chat("Restart with a live agent")
        first.send_turn(chat_id, "are you there?")
        session_id = support.view(first).non_terminal_sessions(chat_id)[0]["session_id"]
        self.assertEqual(support.view(first).get("agent_session", session_id)["state"], "running")

        reopened = support.harness(config, store_path=self.path)
        outcomes = reopened.reattach_on_start()
        self.assertEqual(outcomes, [(session_id, "unknown")])
        observations = [o["kind"] for o in support.view(reopened).observations_of(session_id)]
        self.assertEqual(observations, ["reattach_failed"])
        self.assertEqual(len(support.view(reopened).open_bindings(chat_id)), 1,
                         "an `unknown` session keeps its binding; the agent may still "
                         "be out there")
        self.assert_store_valid(reopened.store, "restart-reattach-failed",
                                "A real agent process that did not survive its harness.")

        reopened.abandon(chat_id)
        self.assertEqual(support.view(reopened).get("agent_session", session_id)["state"],
                         "abandoned")
        self.assertEqual(support.view(reopened).open_bindings(chat_id), [],
                         "abandoning is terminal, so the chat is free to bind again")
        self.assert_store_valid(reopened.store, "restart-then-abandoned",
                                "The user's exit from an unresolvable session.")

    def test_a_reattachable_launcher_resumes_the_session(self):
        """The other half of contract 5.4, with a launcher that does keep durable
        state of its own across a restart."""
        harness = support.harness({'launcher': 'scripted-stub', 'options': {'continuation': 'persistent', 'response_shape': 'stream'}}, store_path=self.path)
        chat_id = harness.create_chat("Restart, recovered")
        harness.send_turn(chat_id, "are you there?")
        live = support.view(harness).non_terminal_sessions(chat_id)[0]
        session_id = live["session_id"]
        # The address the reopened launcher is given is the handle the store
        # recorded, which is the whole of what the harness carries across a
        # restart (contract 5.4, 6.1).
        handle = live["agent_handle"]

        reopened = support.harness({'launcher': 'scripted-stub', 'options': {'continuation': 'persistent', 'response_shape': 'stream', 'resume_handles': [handle]}}, store_path=self.path)
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "running")])
        kinds = [o["kind"] for o in support.view(reopened).observations_of(session_id)]
        self.assertEqual(kinds, ["reattached"])
        end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "restart-reattach-recovered")

    def test_an_interrupted_launch_is_recoverable_rather_than_stuck(self):
        """Contract 5.4's second case: a session interrupted in `launching`."""
        harness = support.harness({'launcher': 'scripted-stub'}, store_path=self.path)
        chat_id = harness.create_chat("Interrupted launch")
        # The records a turn has written when the process dies after the launch
        # was issued and before its result was: every one of them written by the
        # store, through the calls `_launch_turn` makes, in its order.
        store = harness.store
        user_message = store.append_user_message(chat_id, "hello")
        session, _binding = store.create_session(
            chat_id, user_message["message_id"], "scripted-stub",
            harness.capabilities.as_record())
        request = store.append_launch_request(chat_id, session["session_id"], "hello")
        harness._transition(session, "launching", "harness", "harness_action",
                            request["request_id"])

        reopened = support.harness({'launcher': 'scripted-stub'}, store_path=self.path)
        self.assertEqual(reopened.reattach_on_start(),
                         [(session["session_id"], "unknown")])
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "interrupted-launch-abandoned")


class DiagnosticsStayOutOfTheChat(unittest.TestCase, StoreCheck):
    """Contract P1, P2, P4."""

    def test_unparseable_and_unknown_output_is_preserved_and_never_rendered(self):
        harness = support.harness({'launcher': 'scripted-stub', 'options': {'garbage': True, 'unknown_type': True}})
        chat_id = harness.create_chat("Preservation")
        harness.send_turn(chat_id, "hello")

        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        events = support.view(harness).events_of(session_id)
        interpretations = [e["interpretation"] for e in events]
        self.assertIn("malformed", interpretations)
        self.assertIn("unrecognized", interpretations)
        for event in events:
            self.assertTrue(event["raw"]["body"], "raw evidence must be preserved")
            if event["interpretation"] != "recognized":
                self.assertIsNone(event["interpreted_type"])

        rendered = [text for _, _, text in harness.transcript(chat_id)]
        self.assertNotIn("this is not json at all {{{", rendered)
        self.assertEqual(len(rendered), 2)
        self.assert_store_valid(harness.store, "preserved-uninterpretable-output")

    def test_the_same_holds_through_a_real_process(self):
        harness = support.harness({'launcher': 'dev-local', 'options': {'profile': 'one_shot', 'command': None}})
        # Reconfigure the real agent to emit both kinds of unusable output.
        import sys
        from dory_wrangler.launchers.dev_local import DEV_AGENT
        self.addCleanup(support.release, harness)
        harness._boundary._command = [sys.executable, DEV_AGENT, "--profile",
                                      "one_shot", "--garbage", "--unknown-type"]
        chat_id = harness.create_chat("Preservation, for real")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        interpretations = set(e["interpretation"]
                              for e in support.view(harness).events_of(session_id))
        self.assertEqual(interpretations, {"malformed", "unrecognized", "recognized"})
        self.assertEqual(len(harness.transcript(chat_id)), 2)
        self.assert_store_valid(harness.store, "preserved-uninterpretable-output-real")

    def test_diagnostics_are_retrieved_out_of_band_and_bounded(self):
        harness = support.harness({'launcher': 'scripted-stub'})
        chat_id = run_three_turns(harness, "Bounded retrieval")
        all_events = harness.store.read_diagnostic_events(chat_id)
        self.assertTrue(all_events)
        self.assertEqual(len(harness.store.read_diagnostic_events(chat_id, limit=2)), 2)
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        narrowed = harness.store.read_diagnostic_events(chat_id, session_id=session_id,
                                                      sequence_from=1, sequence_to=1)
        self.assertEqual([e["sequence"] for e in narrowed], [1])
        # The transcript renders messages and nothing else.
        self.assertEqual(len(harness.transcript(chat_id)), 6)


class EventsAreResumable(unittest.TestCase, StoreCheck):
    """Contract 6.1: delivery is at-least-once with respect to `sequence`, so a
    replayed payload is a no-op rather than a DUPLICATE_SEQUENCE violation."""

    def test_replaying_the_whole_stream_stores_nothing_twice(self):
        harness = support.harness({'launcher': 'scripted-stub', 'options': {'continuation': 'persistent', 'response_shape': 'stream'}})
        chat_id = harness.create_chat("Resumable")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        before = support.view(harness).events_of(session["session_id"])

        page = harness._boundary.events(session["agent_handle"], 0)
        self.assertTrue(page.payloads, "the launcher must replay from sequence 1")
        for payload in page.payloads:
            self.assertIsNone(harness._preserve(session, payload),
                              "a replayed (session_id, sequence) is already stored")
        self.assertEqual(support.view(harness).events_of(session["session_id"]), before)
        self.assertEqual(len(harness.transcript(chat_id)), 2,
                         "a replayed payload must not produce a second chat message")
        end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "replayed-events-are-a-no-op")


if __name__ == "__main__":
    unittest.main()
