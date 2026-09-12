"""Both continuation modes, and the chat model unchanged under each.

Contract 4.3 and 6.1. "One agent across three turns and three agents across
three turns produce the same transcript, turn for turn, in the same record
shapes." The mode is a *declared launcher capability*: nothing in the chat or
session layers assumes one, and these tests check what the launcher was actually
asked to do rather than what its declaration said.
"""

from __future__ import annotations

import unittest

import support
from support import CONFIGURATIONS, StoreCheck, expected_transcript, run_three_turns

import launch_boundary as lb
from app import open_harness
from launchers.registry import build_launcher
from launchers.scripted_stub import ScriptedStubLauncher


class TheTranscriptIsIdenticalUnderEveryCombination(unittest.TestCase, StoreCheck):

    def test_every_configuration_produces_the_same_six_turns(self):
        transcripts = {}
        for name, config in CONFIGURATIONS:
            harness = support.deterministic(config, "v")
            self.addCleanup(support.release, harness)
            chat_id = run_three_turns(harness, "Mode invariance")
            transcripts[name] = harness.transcript(chat_id)
            support.end_chat(harness, chat_id)
            self.assert_store_valid(harness.store, "mode-invariance-%s" % name)

        self.assertEqual(len(transcripts), 6)
        for name, rows in transcripts.items():
            self.assertEqual(rows, expected_transcript(), name)

    def test_the_chat_and_message_records_are_the_same_shape(self):
        """Sessions, bindings and packets differ between modes -- they are the
        record of what the integration did. Chats and messages must not."""
        shapes = {}
        for name, config in CONFIGURATIONS:
            harness = support.deterministic(config, "w")
            self.addCleanup(support.release, harness)
            chat_id = run_three_turns(harness, "Same shape")
            support.end_chat(harness, chat_id)
            messages = harness.store.messages(chat_id)
            shapes[name] = [
                (m["sequence"], m["author"], m["content"],
                 m["session_id"] is None, m["source_event_id"] is None)
                for m in messages
            ]
        reference = shapes[CONFIGURATIONS[0][0]]
        for name, shape in shapes.items():
            self.assertEqual(shape, reference, name)

    def test_reopen_behaviour_is_identical_under_every_combination(self):
        import os
        import shutil
        import tempfile
        for name, config in CONFIGURATIONS:
            with self.subTest(configuration=name):
                directory = tempfile.mkdtemp(prefix="dory-mode-")
                self.addCleanup(shutil.rmtree, directory, True)
                path = os.path.join(directory, "records.jsonl")
                harness = support.deterministic(config, "y", store_path=path)
                self.addCleanup(support.release, harness)
                chat_id = run_three_turns(harness, "Reopen")
                support.end_chat(harness, chat_id)
                reopened = support.deterministic(config, "z", store_path=path,
                                                 start=support.RESTARTED)
                self.addCleanup(support.release, reopened)
                self.assertEqual(reopened.transcript(chat_id), expected_transcript())


class TheModeIsWhatTheLauncherWasActuallyAskedToDo(unittest.TestCase, StoreCheck):
    """The label-versus-fact check for this guarantee.

    A harness that quietly relaunched on every turn while recording delivery
    packets would satisfy any test that only reads `continuation` out of the
    store. These read the launcher's own call log.
    """

    def test_persistent_launches_once_and_delivers_the_rest(self):
        launcher = ScriptedStubLauncher({"continuation": "persistent",
                                         "response_shape": "stream",
                                         "end_of_turn": "turn_complete"})
        harness = open_harness({}, launcher=launcher)
        chat_id = run_three_turns(harness, "Persistent")

        self.assertEqual(len(launcher.launch_calls), 1,
                         "a persistent agent is launched once")
        self.assertEqual(len(launcher.deliver_calls), 2,
                         "turns two and three were delivered to the running agent")
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1)
        self.assertEqual(len(harness.store.all_of("launch_request")), 1)
        self.assertEqual(len(harness.store.all_of("delivery_request")), 2)

        session_id = harness.store.sessions_of(chat_id)[0]["session_id"]
        deliveries = harness.store.deliveries_of(session_id)
        self.assertEqual([d["sequence"] for d in deliveries], [1, 2])
        self.assertEqual([d["acknowledged"] for d in deliveries], [True, True])
        self.assertEqual([d["instruction_text"] for d in deliveries],
                         list(support.THREE_TURNS[1:]),
                         "the exact text sent on turn four is preserved as it is on "
                         "turn one")
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "persistent-one-agent-three-turns")

    def test_fresh_binding_launches_three_times_and_delivers_nothing(self):
        launcher = ScriptedStubLauncher({"continuation": "fresh_binding",
                                         "response_shape": "one_shot"})
        harness = open_harness({}, launcher=launcher)
        chat_id = run_three_turns(harness, "Fresh binding")

        self.assertEqual(len(launcher.launch_calls), 3)
        self.assertEqual(launcher.deliver_calls, [])
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 3)
        self.assertEqual(len(harness.store.all_of("launch_request")), 3)
        self.assertEqual(harness.store.all_of("delivery_request"), [])
        self.assert_store_valid(harness.store, "fresh-binding-three-agents-three-turns")

    def test_a_fresh_binding_launcher_refuses_delivery_at_the_seam(self):
        launcher = build_launcher({"launcher": "scripted-stub"})
        instruction = lb.DeliveryInstruction(
            delivery_id="dlv_aaaaaaaa", chat_id="cht_aaaaaaaa",
            session_id="ses_aaaaaaaa", sequence=1,
            created_at="2026-09-12T10:00:00Z",
            instruction_encoding="utf-8", instruction_text="hello")
        with self.assertRaises(lb.LauncherError) as caught:
            launcher.deliver("ses_aaaaaaaa", instruction)
        self.assertEqual(caught.exception.category, "invalid_request")

    def test_a_persistent_launcher_whose_delivery_fails_records_the_truth(self):
        """Declaring `persistent` does not make delivery work. What the store
        records is the acknowledgement that actually came back."""

        class NeverAcknowledges(ScriptedStubLauncher):
            def deliver(self, session_id, instruction):
                raise lb.LauncherError("no_acknowledgement", "nothing came back")

        launcher = NeverAcknowledges({"continuation": "persistent",
                                      "response_shape": "stream",
                                      "end_of_turn": "turn_complete"})
        harness = open_harness({}, launcher=launcher)
        chat_id = harness.create_chat("Delivery that is not acknowledged")
        harness.send_turn(chat_id, support.THREE_TURNS[0])
        harness.send_turn(chat_id, support.THREE_TURNS[1])

        delivery = harness.store.all_of("delivery_request")[0]
        self.assertIsNone(delivery["acknowledged"],
                          "an acknowledgement we never received is unknown, not false")
        self.assertEqual(delivery["instruction_text"], support.THREE_TURNS[1],
                         "the turn was still recorded: a turn the harness sent and did "
                         "not record is a turn nobody can investigate")
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "delivery-not-acknowledged")


class TheComparisonHasTeeth(unittest.TestCase):
    """A guarantee proven only by the test written alongside it is not proven.

    These mutate the system so the invariance claim is genuinely false, and
    require the same comparison to fail. A vacuous comparator passes them.
    """

    def _transcript(self, launcher):
        harness = open_harness({}, launcher=launcher)
        chat_id = run_three_turns(harness, "Mutation")
        return harness.transcript(chat_id)

    def test_a_launcher_that_answers_differently_breaks_the_comparison(self):
        class Different(ScriptedStubLauncher):
            def _produce_turn(self, session, instruction_text):
                ScriptedStubLauncher._produce_turn(
                    self, session, "[persistent] " + instruction_text)

        baseline = self._transcript(ScriptedStubLauncher({}))
        mutated = self._transcript(Different({}))
        self.assertNotEqual(baseline, mutated,
                            "the invariance comparison cannot tell two different "
                            "transcripts apart, so it proves nothing")
        self.assertEqual(baseline, expected_transcript())

    def test_a_launcher_that_drops_a_turn_breaks_the_comparison(self):
        class Drops(ScriptedStubLauncher):
            def __init__(self, options=None):
                ScriptedStubLauncher.__init__(self, options)
                self._turns = 0

            def _produce_turn(self, session, instruction_text):
                self._turns += 1
                if self._turns == 2:
                    self._emit(session, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                               '{"type": "turn_complete"}',
                               interpreted_type=lb.PAYLOAD_TURN_COMPLETE)
                    self._emit(session, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                               '{"type": "session_completed"}',
                               interpreted_type=lb.PAYLOAD_SESSION_COMPLETED)
                    return
                ScriptedStubLauncher._produce_turn(self, session, instruction_text)

        mutated = self._transcript(Drops({}))
        self.assertNotEqual(mutated, expected_transcript())


if __name__ == "__main__":
    unittest.main()
