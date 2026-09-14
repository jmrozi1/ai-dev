"""Launch outcomes, the five failure categories, and `unknown` as a state.

Contract 5.1, 5.3 and 6.3. `failed` and `launch_failed` are deliberately
distinct, and `unknown` is not a failure: #90 must be able to count them apart.
Every case here is checked by what the durable record actually says, never by
what the launcher called it.
"""

from __future__ import annotations

import sys
import unittest

import support
from support import StoreCheck

import launch_boundary as lb
import session_manager
from app import open_harness
from errors import NotPermitted
from launchers.dev_local import DEV_AGENT


def stub(salt, **options):
    return support.deterministic({"launcher": "scripted-stub", "options": options}, salt)


class EveryFailureCategoryIsDurablyRecorded(unittest.TestCase, StoreCheck):

    def _launch_failing_as(self, salt, outcome):
        harness = stub(salt, launch_outcomes=[outcome])
        chat_id = harness.create_chat("Failure: %s" % outcome)
        result = harness.send_turn(chat_id, "hello")
        return harness, chat_id, result

    def test_each_of_the_five_categories_round_trips(self):
        for i, category in enumerate(lb.FAILURE_CATEGORIES):
            with self.subTest(category=category):
                harness, chat_id, outcome = self._launch_failing_as("f%d" % i, category)
                self.assertEqual(outcome.launch_outcome, "failed")
                self.assertEqual(outcome.failure_category, category)

                session = harness.store.sessions_of(chat_id)[0]
                self.assertEqual(session["state"], "launch_failed",
                                 "we never got an agent, which is launch_failed and not "
                                 "failed")
                self.assertIsNone(session.get("agent_handle"))
                results = harness.store.all_of("launch_result")
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["outcome"], "failed")
                self.assertEqual(results[0]["failure_category"], category)
                self.assertIsNone(results[0]["agent_handle"])
                self.assertEqual(harness.store.open_bindings(chat_id), [],
                                 "a terminal session must not hold the chat hostage")
                self.assert_store_valid(harness.store, "launch-failed-%s" % category)

    def test_a_raised_launcher_error_is_classified_not_lost(self):
        for i, category in enumerate(lb.FAILURE_CATEGORIES):
            with self.subTest(category=category):
                harness, chat_id, outcome = self._launch_failing_as(
                    "r%d" % i, "raise:%s" % category)
                self.assertEqual(outcome.failure_category, category)
                self.assertEqual(harness.store.sessions_of(chat_id)[0]["state"],
                                 "launch_failed")

    def test_an_unclassified_crash_becomes_internal_error(self):
        harness, chat_id, outcome = self._launch_failing_as("x", "crash")
        self.assertEqual(outcome.failure_category, "internal_error")
        result = harness.store.all_of("launch_result")[0]
        self.assertIn("RuntimeError", result["detail"])
        self.assert_store_valid(harness.store, "launch-failed-unclassified-crash")

    def test_a_launcher_returning_something_that_is_not_a_result_fails_closed(self):
        class Liar(lb.LaunchBoundary):
            launcher_id = "liar"

            @property
            def capabilities(self):
                return lb.LauncherCapabilities("fresh_binding", "one_shot", None)

            def launch(self, instruction):
                return {"outcome": "accepted", "agent_handle": "h"}

            def events(self, agent_handle, after_sequence):
                return lb.EventsPage([])

            def stop(self, agent_handle, reason):
                return lb.StopAck(True)

        harness = open_harness({}, launcher=Liar())
        chat_id = harness.create_chat("A mapping is not a result")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.launch_outcome, "failed")
        self.assertEqual(outcome.failure_category, "internal_error")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "launch_failed")
        self.assertNotIn("running", [t["to"] for t in session["transitions"]])
        self.assert_store_valid(harness.store, "launcher-result-not-a-result")

    def test_a_real_process_that_says_nothing_is_no_acknowledgement(self):
        harness = support.deterministic(
            {"launcher": "dev-local",
             "options": {"profile": "one_shot",
                         "command": [sys.executable, DEV_AGENT, "--profile",
                                     "one_shot", "--silent"]}}, "s")
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Silent process")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.failure_category, "no_acknowledgement")
        self.assert_store_valid(harness.store, "real-process-no-acknowledgement")

    def test_a_process_that_cannot_start_is_unavailable(self):
        harness = support.deterministic(
            {"launcher": "dev-local",
             "options": {"profile": "one_shot",
                         "command": ["/nonexistent/launch_agent.sh"]}}, "u")
        chat_id = harness.create_chat("Nothing to start")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.failure_category, "unavailable",
                         "internally this same category covers an inactive user "
                         "session and an uninitialised bridge")
        self.assert_store_valid(harness.store, "real-process-unavailable")


class AgentFailureIsNotLaunchFailure(unittest.TestCase, StoreCheck):

    def test_an_agent_that_ran_and_went_wrong_is_failed(self):
        harness = stub("af", end_of_turn="session_failed")
        chat_id = harness.create_chat("Agent failure")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "failed")
        self.assertIn("running", [t["to"] for t in session["transitions"]])
        self.assertTrue(session["agent_handle"])
        self.assert_store_valid(harness.store, "agent-failed-after-running")

    def test_a_real_process_exiting_non_zero_is_failed(self):
        harness = support.deterministic(
            {"launcher": "dev-local",
             "options": {"profile": "one_shot",
                         "command": [sys.executable, DEV_AGENT, "--profile",
                                     "one_shot", "--fail-exit"]}}, "afr")
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Agent failure, for real")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "failed")
        self.assertEqual(len(harness.transcript(chat_id)), 2,
                         "the agent answered before it failed, and that answer stands")
        self.assert_store_valid(harness.store, "real-agent-failed")


class UnknownIsAStateNotAFailure(unittest.TestCase, StoreCheck):
    """Contract D4 and 5.1. `unknown` is representable, non-terminal, entered
    only on an explicit observation, and never inferred from silence."""

    def test_an_unknown_launch_outcome_is_not_a_launch_failure(self):
        harness = stub("un", launch_outcomes=["unknown"])
        chat_id = harness.create_chat("Unknown launch")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.launch_outcome, "unknown")
        self.assertIsNone(outcome.failure_category)

        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        self.assertNotIn("launch_failed", [t["to"] for t in session["transitions"]])
        result = harness.store.all_of("launch_result")[0]
        self.assertEqual(result["outcome"], "unknown")
        self.assertIsNone(result["failure_category"])
        self.assertIsNone(result["agent_handle"])
        self.assertEqual(len(harness.store.open_bindings(chat_id)), 1,
                         "unknown is not terminal; the agent may still be out there")
        self.assert_store_valid(harness.store, "launch-outcome-unknown")

    def test_the_user_can_always_abandon_an_unknown_session(self):
        harness = stub("ab", launch_outcomes=["unknown"])
        chat_id = harness.create_chat("Abandon")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(harness.abandon(chat_id), "abandoned")
        self.assertEqual(harness.store.open_bindings(chat_id), [])
        last = harness.store.sessions_of(chat_id)[0]["transitions"][-1]
        self.assertEqual((last["owner"], last["evidence"]["kind"], last["evidence"]["ref"]),
                         ("user", "user_action", None),
                         "abandoning is a decision, not an observation")
        self.assert_store_valid(harness.store, "unknown-then-abandoned")

    def test_an_unconfirmed_stop_leads_to_unknown_rather_than_terminated(self):
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "turn_complete", "stop_confirms": False}}, "us")
        chat_id = harness.create_chat("Unconfirmed stop")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(harness.stop_agent(chat_id, "please stop"), "unknown")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        self.assertNotIn("terminated", [t["to"] for t in session["transitions"]])
        kinds = [o["kind"] for o in harness.store.observations_of(session["session_id"])]
        self.assertEqual(kinds, ["stop_unconfirmed"])
        self.assert_store_valid(harness.store, "stop-unconfirmed-then-unknown")
        harness.abandon(chat_id)
        self.assert_store_valid(harness.store, "stop-unconfirmed-then-abandoned")

    def test_a_stop_that_raises_is_also_an_unconfirmed_stop(self):
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "turn_complete", "stop_confirms": "raise"}}, "sr")
        chat_id = harness.create_chat("Stop raises")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(harness.stop_agent(chat_id, "please stop"), "unknown")

    def test_a_reader_side_failure_is_not_an_end_of_stream(self):
        """Contract 4.7 and 6.1. Two implementations that disagree on this drive
        healthy sessions to `unknown` on a transport hiccup, so the record says
        what actually happened: our read failed."""
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "turn_complete",
                         "events_raise": "internal_error"}}, "rs")
        chat_id = harness.create_chat("Read failure")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        kinds = [o["kind"] for o in harness.store.observations_of(session["session_id"])]
        self.assertEqual(kinds, ["stream_read_failed"])
        types = [e["interpreted_type"] for e in
                 harness.store.events_of(session["session_id"])]
        self.assertNotIn("stream_end", types,
                         "a read failure on our side is not the agent's stream closing")
        self.assert_store_valid(harness.store, "stream-read-failed-then-unknown")

    def test_a_stream_that_ends_without_a_lifecycle_event_leads_to_unknown(self):
        """Contract 5.3 cause 2: the stream *closing* is an observed fact."""
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "stream_end"}}, "se")
        chat_id = harness.create_chat("End of stream")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        last = session["transitions"][-1]
        self.assertEqual(last["evidence"]["kind"], "stream_end")
        cited = harness.store.get("diagnostic_event", last["evidence"]["ref"])
        self.assertEqual((cited["source"], cited["interpretation"],
                          cited["interpreted_type"]),
                         ("launcher", "recognized", "stream_end"))
        self.assert_store_valid(harness.store, "stream-end-then-unknown")

    def test_a_real_process_whose_stream_ends_while_it_lives(self):
        """The same thing with a real operating-system process: stdout closes and
        the agent is still alive, so all the launcher observed is an end of
        stream."""
        harness = support.deterministic(
            {"launcher": "dev-local",
             "options": {"profile": "persistent",
                         "command": [sys.executable, DEV_AGENT, "--profile",
                                     "persistent", "--close-stdout"]}}, "sr2")
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Real end of stream")
        harness.send_turn(chat_id, "hello")
        # The agent answered turn one and then closed its stream. The harness had
        # no reason to read further at the time -- reading on and concluding
        # something would have been an inference from silence -- so the end of
        # stream surfaces on the next turn the user actually sends.
        self.assertEqual(harness.store.sessions_of(chat_id)[0]["state"], "running")
        harness.send_turn(chat_id, "still there?")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        types = [e["interpreted_type"] for e in
                 harness.store.events_of(session["session_id"])]
        self.assertIn("stream_end", types)
        self.assert_store_valid(harness.store, "real-stream-end-then-unknown")
        harness.abandon(chat_id)


class TerminationIsDistinguishable(unittest.TestCase, StoreCheck):

    def test_the_four_terminal_outcomes_are_told_apart_in_durable_state(self):
        states = {}
        states["launch_failed"] = self._state(stub("t1", launch_outcomes=["rejected"]))
        states["failed"] = self._state(stub("t2", end_of_turn="session_failed"))
        states["completed"] = self._state(stub("t3"))
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "end_of_turn": "turn_complete"}}, "t4")
        chat_id = harness.create_chat("Terminated")
        harness.send_turn(chat_id, "hello")
        harness.stop_agent(chat_id, "the user said so")
        states["terminated"] = harness.store.sessions_of(chat_id)[0]["state"]
        self.assertEqual(states,
                         {"launch_failed": "launch_failed", "failed": "failed",
                          "completed": "completed", "terminated": "terminated"})

    def _state(self, harness):
        chat_id = harness.create_chat("Outcome")
        harness.send_turn(chat_id, "hello")
        return harness.store.sessions_of(chat_id)[0]["state"]


if __name__ == "__main__":
    unittest.main()


class EveryFailureShapeStillProducesADurableOutcome(unittest.TestCase, StoreCheck):
    """Review finding F2: the classification is inside the guarantee it makes.

    `_call_launch`'s docstring promises that "a launcher that raises anything at
    all still produces a classified, durable outcome". The earlier revision
    enforced that with an `except` around `self._boundary.launch(...)` and
    nothing around the classification that followed it, so building the
    `LaunchResult` that recorded a failure could itself raise -- straight out of
    the `except` block and past the caller. The one method whose entire purpose
    is that guarantee checked it at its label.

    The concrete trigger is the most natural mistake the seam invites:
    `LauncherError` validated `category` and not `detail`, while `LaunchResult`
    requires `detail` to be a string, so a launcher that put the exception object
    in `detail` -- which is exactly what the seam tells authors `detail` is for --
    bricked the chat. Measured before the fix: session stuck in `launching`,
    binding still open, **zero** `launch_result` records, stop and abandon and
    every further turn refused, and a store the contract validator calls valid.

    Two independent repairs, tested separately because they close different
    halves: `LauncherError` now coerces `detail` where it crosses, and
    `_call_launch` wraps its own classification.
    """

    class _BadDetail(lb.LaunchBoundary):
        launcher_id = "bad-detail"

        def __init__(self, detail):
            self._detail = detail

        @property
        def capabilities(self):
            return lb.LauncherCapabilities("fresh_binding", "one_shot", None)

        def launch(self, instruction):
            raise lb.LauncherError(lb.FAILURE_UNAVAILABLE, detail=self._detail)

        def events(self, agent_handle, after_sequence):
            return lb.EventsPage([])

        def stop(self, agent_handle, reason):
            return lb.StopAck(True)

    DETAILS = (
        OSError("the bridge socket is gone"),   # the natural mistake: the exception
        object(),
        b"bytes from a transport",
        {"code": 7},
        17,
    )

    def _turn_with_detail(self, salt, detail):
        harness = support.deterministic({}, salt, launcher=self._BadDetail(detail))
        chat_id = harness.create_chat("A launcher that failed while failing")
        outcome = harness.send_turn(chat_id, "hello")
        return harness, chat_id, outcome

    def test_every_shape_of_bad_detail_still_reaches_a_terminal_state(self):
        for i, detail in enumerate(self.DETAILS):
            with self.subTest(detail=type(detail).__name__):
                harness, chat_id, outcome = self._turn_with_detail("d%d" % i, detail)
                session = harness.store.sessions_of(chat_id)[0]
                self.assertEqual(session["state"], "launch_failed",
                                 "the session must not be left in `launching`")
                self.assertEqual(len(harness.store.all_of("launch_result")), 1,
                                 "the attempt must be recorded whatever the "
                                 "launcher put in `detail`")
                self.assertEqual(outcome.launch_outcome, "failed")

    def test_the_category_the_launcher_chose_survives_the_coercion(self):
        """Rejecting a bad `detail` would have been the wrong repair: it turns a
        launcher's honest, correctly categorised failure into an unclassified
        crash, and #90 counts these categories apart."""
        harness, chat_id, outcome = self._turn_with_detail(
            "d9", OSError("the bridge socket is gone"))
        self.assertEqual(outcome.failure_category, "unavailable",
                         "not internal_error: the launcher classified this itself")
        result = harness.store.all_of("launch_result")[0]
        self.assertEqual(result["failure_category"], "unavailable")
        self.assertIsInstance(result["detail"], str)
        self.assertIn("the bridge socket is gone", result["detail"],
                      "the concrete cause must survive into the record")

    def test_the_binding_is_released_and_the_chat_is_usable(self):
        """What made F2 severe: the chat was unusable for the life of the
        process, with no stop, no abandon and no further turn."""
        harness, chat_id, _ = self._turn_with_detail("d10", OSError("gone"))
        session = harness.store.sessions_of(chat_id)[0]
        binding = harness.store.binding_for_session(session["session_id"])
        self.assertIsNotNone(binding["released_at"], "the binding must be released")
        self.assertEqual(harness.store.open_bindings(chat_id), [])
        # The chat takes another turn, which is the whole point.
        second = support.deterministic({"launcher": "scripted-stub"}, "d11")
        self.assertTrue(second)
        self.assert_store_valid(harness.store, "bad-detail-launch-failed-recorded")

    def test_launcher_error_coerces_a_non_string_detail_at_the_seam(self):
        """Repair one, at the seam. `detail` is rendered and never parsed, so
        coercion loses nothing."""
        exc = lb.LauncherError(lb.FAILURE_REJECTED, detail=OSError("boom"))
        self.assertIsInstance(exc.detail, str)
        self.assertIn("boom", exc.detail)
        self.assertEqual(exc.category, "rejected")
        # A LaunchResult can now be built from it, which is what used to raise.
        result = lb.LaunchResult(lb.OUTCOME_FAILED, failure_category=exc.category,
                                 detail=exc.detail)
        self.assertIsInstance(result.detail, str)

    def test_a_string_detail_and_none_are_passed_through_untouched(self):
        self.assertEqual(lb.LauncherError(lb.FAILURE_REJECTED, "plain").detail, "plain")
        self.assertIsNone(lb.LauncherError(lb.FAILURE_REJECTED).detail)

    def test_classification_that_fails_anyway_still_becomes_internal_error(self):
        """Repair two, independent of repair one. A `LauncherError` subclass that
        gets past the coercion entirely -- the class of mistake nobody enumerated
        -- must still produce a durable outcome rather than escape the method."""

        class Sneaky(lb.LauncherError):
            def __init__(self):
                lb.LauncherError.__init__(self, lb.FAILURE_REJECTED, "fine")
                self.detail = OSError("set after construction")
                self.category = "not-a-category-at-all"

        class Raises(self._BadDetail):
            def launch(self, instruction):
                raise Sneaky()

        harness = support.deterministic({}, "d12", launcher=Raises(None))
        chat_id = harness.create_chat("Classification that cannot succeed")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.launch_outcome, "failed")
        self.assertEqual(outcome.failure_category, "internal_error")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "launch_failed")
        self.assertEqual(len(harness.store.all_of("launch_result")), 1)
        self.assert_store_valid(harness.store, "classification-failure-is-internal-error")

    def test_the_probe_would_have_failed_before_the_repair(self):
        """The negative control for both repairs at once: restore the old
        behaviour and require these assertions to fail, so a vacuous probe is
        not mistaken for a closed defect."""

        class OldCallLaunch(session_manager.SessionManager):
            def _call_launch(self, instruction):
                # Verbatim the pre-repair shape: the guard is around the call
                # and not around the classification.
                try:
                    result = self._boundary.launch(instruction)
                except lb.LauncherError as exc:
                    detail = exc.__dict__.get("raw_detail", exc.detail)
                    return lb.LaunchResult(lb.OUTCOME_FAILED,
                                           failure_category=exc.category, detail=detail)
                return result

        class RawDetail(self._BadDetail):
            def launch(self, instruction):
                exc = lb.LauncherError(lb.FAILURE_UNAVAILABLE, "placeholder")
                exc.raw_detail = OSError("the bridge socket is gone")
                raise exc

        harness = OldCallLaunch(session_manager.Store(None), RawDetail(None))
        chat_id = harness.create_chat("The defect, restored")
        with self.assertRaises(lb.LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "launching",
                         "this is the state F2 left the session in")
        self.assertEqual(harness.store.all_of("launch_result"), [])
        self.assertEqual(support.codes(harness.store.snapshot()), [],
                         "and the store validated, which is why nothing caught it")
