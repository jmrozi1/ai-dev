"""Both continuation modes, and the chat model unchanged under each.

Contract 4.3 and 6.1. "One agent across three turns and three agents across
three turns produce the same transcript, turn for turn, in the same record
shapes." The mode is a *declared launcher capability*: nothing in the chat or
session layers assumes one, and these tests check what the launcher was actually
asked to do rather than what its declaration said.

**What this experiment establishes, stated accurately (review finding F5).** The
suite runs **six configurations**, which declare **four** distinct capability
combinations -- `{persistent, fresh_binding} x {stream, one_shot}` is four, not
six, and an earlier handoff said six. Instrumented at the boundary, those six
configurations produce exactly **two** distinct observable behaviours, and the
partition is by continuation mode alone:

    fresh_binding  ->  launch, events | launch, events | launch, events
    persistent     ->  launch, events | deliver, events | deliver, events

So what is genuinely proven here is **continuation-mode invariance**, and it is
proven well: the comparison reads the launcher's own call log rather than the
`continuation` label in the store, and review's quiet-relaunch mutation fails it
`3 != 1`.

`response_shape` **changes no harness path in this experiment**, because every
launcher here reaches a turn boundary or a terminal lifecycle payload before the
end-of-stream flag is ever consulted. Response-shape invariance is therefore
*declarative here* and is not established by this file. The shape's only real
effect is the two end-of-stream guards, and those are exercised directly, and
adversarially, in `test_adversarial.AOneShotLauncherCannotObserveAStreamEnding`
-- both spellings of the attack, each breaking a different test.

That statement is not left as prose. `TheExperimentIsWhatItIsAndNotMore` below
asserts the count of declared combinations, the count of distinct behaviours, and
the partition, so a configuration that stopped being distinct -- or that started
being distinct on `response_shape` -- fails here rather than quietly making this
paragraph wrong.
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

    def test_every_configuration_produces_the_same_three_turn_transcript(self):
        transcripts = {}
        for name, config in CONFIGURATIONS:
            harness = support.deterministic(config, "v")
            self.addCleanup(support.release, harness)
            chat_id = run_three_turns(harness, "Mode invariance")
            transcripts[name] = harness.transcript(chat_id)
            support.end_chat(harness, chat_id)
            self.assert_store_valid(harness.store, "mode-invariance-%s" % name)

        self.assertEqual(len(transcripts), len(CONFIGURATIONS))
        self.assertEqual(len(expected_transcript()), 6,
                         "three user turns and three agent answers")
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
            launcher.deliver("stub-agent-0001", instruction)
        self.assertEqual(caught.exception.category, "invalid_request")

    def test_a_persistent_launcher_whose_delivery_fails_records_the_truth(self):
        """Declaring `persistent` does not make delivery work. What the store
        records is the acknowledgement that actually came back."""

        class NeverAcknowledges(ScriptedStubLauncher):
            def deliver(self, agent_handle, instruction):
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


class _CallLog(lb.LaunchBoundary):
    """A recording proxy. It adds no behaviour and declares whatever the
    launcher underneath declares; it exists so the *fact* of what crossed the
    boundary is readable for any launcher, including one that starts real
    processes and keeps no log of its own."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    launcher_id = "call-log"

    @property
    def capabilities(self):
        return self._inner.capabilities

    def launch(self, instruction):
        self.calls.append("launch")
        return self._inner.launch(instruction)

    def events(self, agent_handle, after_sequence):
        self.calls.append("events")
        return self._inner.events(agent_handle, after_sequence)

    def deliver(self, agent_handle, instruction):
        self.calls.append("deliver")
        return self._inner.deliver(agent_handle, instruction)

    def stop(self, agent_handle, reason):
        self.calls.append("stop")
        return self._inner.stop(agent_handle, reason)

    def release_all(self):
        releaser = getattr(self._inner, "release_all", None)
        if releaser is not None:
            releaser()

    @property
    def shape(self):
        """The behaviour, with repeated reads collapsed: what distinguishes the
        modes is which operation carried each turn, not how many pages a stream
        happened to arrive in."""
        collapsed = []
        for call in self.calls:
            if call == "stop":
                continue
            if not (collapsed and collapsed[-1] == call):
                collapsed.append(call)
        return tuple(collapsed)


class TheExperimentIsWhatItIsAndNotMore(unittest.TestCase):
    """Review finding F5, corrected into a checked claim.

    The defect was an evidence one: "six combinations of 2x2" was four
    combinations declared across six configurations, producing two behaviours.
    Restating it in prose would have repeated the mistake one level up, so the
    arithmetic and the partition are asserted here instead.
    """

    def _shapes(self):
        shapes = {}
        for name, config in CONFIGURATIONS:
            proxy = _CallLog(build_launcher(config))
            harness = open_harness({}, launcher=proxy)
            self.addCleanup(proxy.release_all)
            chat_id = run_three_turns(harness, "Instrumented")
            support.end_chat(harness, chat_id)
            shapes[name] = (proxy.capabilities.continuation,
                            proxy.capabilities.response_shape,
                            proxy.shape)
        return shapes

    def test_six_configurations_declare_four_combinations(self):
        declared = set()
        for name, config in CONFIGURATIONS:
            launcher = build_launcher(config)
            releaser = getattr(launcher, "release_all", None)
            if releaser:
                self.addCleanup(releaser)
            declared.add((launcher.capabilities.continuation,
                          launcher.capabilities.response_shape))
        self.assertEqual(len(CONFIGURATIONS), 6)
        self.assertEqual(len(declared), 4,
                         "2 continuation modes x 2 response shapes is four, and the "
                         "six configurations cover all four: %s" % sorted(declared))
        self.assertEqual(
            declared,
            set((c, r) for c in lb.CONTINUATION_MODES for r in lb.RESPONSE_SHAPES))

    def test_those_four_combinations_produce_exactly_two_behaviours(self):
        shapes = self._shapes()
        distinct = set(shape for _, _, shape in shapes.values())
        self.assertEqual(len(distinct), 2,
                         "instrumented at the boundary the configurations produce "
                         "%d distinct behaviours: %s" % (len(distinct), sorted(distinct)))

    def test_the_partition_is_by_continuation_mode_alone(self):
        """The substantive half. If `response_shape` ever does change a path,
        this fails and the docstring above stops being true at the same moment."""
        shapes = self._shapes()
        by_continuation = {}
        by_response_shape = {}
        for continuation, response_shape, shape in shapes.values():
            by_continuation.setdefault(continuation, set()).add(shape)
            by_response_shape.setdefault(response_shape, set()).add(shape)

        for continuation, seen in by_continuation.items():
            self.assertEqual(len(seen), 1,
                             "%s produced more than one behaviour: %s"
                             % (continuation, sorted(seen)))
        self.assertEqual(by_continuation["fresh_binding"],
                         {("launch", "events", "launch", "events", "launch", "events")})
        self.assertEqual(by_continuation["persistent"],
                         {("launch", "events", "deliver", "events", "deliver", "events")})
        # And the negative half, stated as plainly as the positive one: grouping
        # by response_shape separates nothing, which is why response-shape
        # invariance is declarative here rather than established.
        for response_shape, seen in by_response_shape.items():
            self.assertEqual(len(seen), 2,
                             "%s no longer spans both behaviours, so grouping by "
                             "response_shape has started to mean something and this "
                             "file's claim must be restated" % response_shape)

    def test_response_shape_is_exercised_where_it_actually_matters(self):
        """Not an excuse for the above: the guards `response_shape` really does
        control exist, are load-bearing, and are attacked in both spellings.

        This used to discharge that claim with `hasattr` on two test-method names
        in `test_adversarial.py` -- a label check inside the repair of an evidence
        defect, and one that stayed green if either of those tests were gutted.
        It runs the refusals itself now. The guard is the fact; the other file's
        method names are not.
        """
        one_shot = {"launcher": "scripted-stub",
                    "options": {"continuation": "fresh_binding",
                                "response_shape": "one_shot"}}

        # Spelling one: a payload typed `stream_end` from a launcher that
        # declared it returns a response rather than a stream.
        typed = dict(one_shot["options"], end_of_turn="stream_end")
        harness = open_harness({"launcher": "scripted-stub", "options": typed})
        chat_id = harness.create_chat("One-shot stream end")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            harness.send_turn(chat_id, "hello")
        self.assertIn("no stream to end", str(caught.exception))
        self.assertNotIn("stream_end",
                         [e["interpreted_type"]
                          for e in harness.store.all_of("diagnostic_event")],
                         "the refused payload must not have been stored either")

        # Spelling two: never emit the payload, just set the page's flag.
        class FlagsAnEndWithoutSayingIt(ScriptedStubLauncher):
            def events(self, agent_handle, after_sequence):
                page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
                return lb.EventsPage(page.payloads, stream_ended=True)

        flagged = open_harness(
            {}, launcher=FlagsAnEndWithoutSayingIt(dict(one_shot["options"])))
        chat_id = flagged.create_chat("One-shot flagged end")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            flagged.send_turn(chat_id, "hello")
        self.assertIn("signalled that a stream ended", str(caught.exception))

        # And the capability both guards read, so a launcher that declares
        # `stream` is not caught by either of them.
        self.assertFalse(lb.LauncherCapabilities("fresh_binding", "one_shot").has_stream)
        self.assertTrue(lb.LauncherCapabilities("persistent", "stream").has_stream)
        streaming = open_harness(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "stream_end"}})
        chat_id = streaming.create_chat("A stream may end")
        streaming.send_turn(chat_id, "hello")
        self.assertIn("stream_end",
                      [e["interpreted_type"]
                       for e in streaming.store.all_of("diagnostic_event")],
                      "the guards must key on the declared response shape, not "
                      "refuse a stream end outright")


if __name__ == "__main__":
    unittest.main()
