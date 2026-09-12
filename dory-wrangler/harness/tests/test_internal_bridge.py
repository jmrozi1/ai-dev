"""The seam, run against a model of the one internal path that is proven.

The release's largest open question was whether an internal-bridge launcher can
drop in against contract 6.1's four operations unchanged. `internal_bridge.py`
answers it by being that launcher, in shape, and this file is the evidence: the
whole chat loop, three turns, a transcript compared against the same reference
every other launcher is compared against, and a store handed to the contract
validator.

Nothing here is a claim about the internal bridge's *behaviour* -- that is
unreachable from this VM and stays an assumption. What is claimed, and checked,
is that the seam hosts this shape with no operation, field or capability added,
and what that shape forces on the harness and on #86/#88/#90 when it does.

The four scenarios are the checkpoint review's B1-B4, preserved.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import support
from support import StoreCheck, end_chat, expected_transcript, run_three_turns

import launch_boundary as lb
import session_manager
from app import open_harness
from errors import ConcurrentLaunchRefused, NotPermitted
from internal_bridge import InternalBridgeLauncher
from launchers.registry import UnknownLauncher, build_launcher


class TheSeamHostsTheModelledInternalPath(unittest.TestCase, StoreCheck):
    """B1. Three turns, and the same reference every other launcher meets."""

    def test_three_turns_produce_the_reference_transcript(self):
        launcher = InternalBridgeLauncher()
        harness = support.deterministic({}, "ib1", launcher=launcher)
        chat_id = run_three_turns(harness, "Internal bridge")

        self.assertEqual(harness.transcript(chat_id), expected_transcript(),
                         "the modelled internal path must produce the same chat "
                         "as every other launcher, turn for turn")
        self.assertEqual([s["state"] for s in harness.store.sessions_of(chat_id)],
                         ["completed", "completed", "completed"])
        self.assert_store_valid(
            harness.store, "internal-bridge-three-turns",
            "The whole chat loop against a faithful model of the proven internal "
            "path: one-shot, a blocking launch that returns the response with it, "
            "a synthesised handle, no state across a restart, no measured bound.")

    def test_each_launch_packet_carries_only_that_turn(self):
        """The agent has no memory of the chat, deliberately (U1, U2). On this
        path that means the chat is multi-turn and the agent is not -- a product
        property #86 and #88 must surface rather than inherit silently."""
        harness = support.deterministic({}, "ib2", launcher=InternalBridgeLauncher())
        chat_id = run_three_turns(harness, "No memory")
        self.assertEqual([p["instruction_text"]
                          for p in harness.store.all_of("launch_request")],
                         list(support.THREE_TURNS))

    def test_the_seam_needed_nothing_added_for_it(self):
        """The decisive claim, checked rather than asserted: this launcher
        implements the contract's four operations and declares the ordinary
        capability vocabulary. No fifth operation, no new field, no new
        capability."""
        implemented = set(name for name in vars(InternalBridgeLauncher)
                          if not name.startswith("_") and name != "launcher_id")
        self.assertTrue(implemented <= {"launch", "events", "stop", "capabilities"},
                        "the model added %s to the boundary"
                        % sorted(implemented - {"launch", "events", "stop",
                                                "capabilities"}))
        capabilities = InternalBridgeLauncher().capabilities
        self.assertIn(capabilities.continuation, lb.CONTINUATION_MODES)
        self.assertIn(capabilities.response_shape, lb.RESPONSE_SHAPES)
        self.assertIsNone(capabilities.instruction_bound_bytes,
                          "no bound has been measured internally either (U2)")

    def test_the_synthesised_handle_is_what_addresses_the_agent(self):
        """The script issues no handle, so the launcher makes one up. That is
        legal because handles are opaque -- and it is the practical face of C1:
        the handle addresses the launcher's record of the run, not the agent."""
        launcher = InternalBridgeLauncher()
        harness = support.deterministic({}, "ib3", launcher=launcher)
        chat_id = harness.create_chat("Synthesised handle")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertTrue(session["agent_handle"])
        self.assertNotEqual(session["agent_handle"], session["session_id"])
        self.assertEqual([address for _, address in launcher.addressed],
                         [session["agent_handle"]])

    def test_it_is_not_a_registered_launcher(self):
        """It is a model kept as test material. The product does not ship it and
        the package never imports it."""
        with self.assertRaises(UnknownLauncher):
            build_launcher({"launcher": "internal-bridge"})


class AChatThatCannotReportAnExitNeedsAStopBetweenTurns(unittest.TestCase, StoreCheck):
    """B4's first half, and launcher-authoring obligation 3.

    A bridge that can report the agent's text and nothing about its exit leaves
    the session `running` forever. This is the difference between a working chat
    and one that needs a manual Stop between every turn, and it belongs in the
    launcher rather than anywhere above the seam.
    """

    def test_without_a_lifecycle_event_the_second_turn_is_refused(self):
        launcher = InternalBridgeLauncher(report_completion=False)
        harness = support.deterministic({}, "ib4", launcher=launcher)
        chat_id = harness.create_chat("No lifecycle event")
        harness.send_turn(chat_id, "hello")
        self.assertEqual([s["state"] for s in harness.store.sessions_of(chat_id)],
                         ["running"],
                         "the response alone never ends the session")
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "and again?")
        end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "internal-bridge-no-lifecycle-event")

    def test_with_the_lifecycle_event_the_chat_is_multi_turn(self):
        """The control, so the obligation is attributed to the missing event and
        not to something else about this launcher."""
        harness = support.deterministic({}, "ib5", launcher=InternalBridgeLauncher())
        chat_id = harness.create_chat("Lifecycle event emitted")
        harness.send_turn(chat_id, "hello")
        harness.send_turn(chat_id, "and again?")
        self.assertEqual(len(harness.transcript(chat_id)), 4)


class ADeadBridgeIsDiscoverableOnlyByAttemptingATurn(unittest.TestCase, StoreCheck):
    """B2. There is no `status` operation by design, so an unmet prerequisite
    costs the user a turn. #86 owes this a presentation."""

    def test_an_uninitialised_bridge_leaves_the_turn_in_the_chat_unanswered(self):
        harness = support.deterministic(
            {}, "ib6", launcher=InternalBridgeLauncher(bridge_ready=False))
        chat_id = harness.create_chat("The bridge is down")
        outcome = harness.send_turn(chat_id, "hello")

        self.assertEqual(outcome.launch_outcome, "failed")
        self.assertEqual(outcome.failure_category, "unavailable")
        self.assertEqual(harness.store.sessions_of(chat_id)[0]["state"],
                         "launch_failed")
        self.assertEqual([(author, text) for _, author, text
                          in harness.transcript(chat_id)],
                         [("user", "hello")],
                         "the user's message is in the chat with no answer")
        self.assert_store_valid(harness.store, "internal-bridge-not-initialised")

    def test_an_inactive_user_session_is_the_same_shape(self):
        harness = support.deterministic(
            {}, "ib7", launcher=InternalBridgeLauncher(user_session_active=False))
        chat_id = harness.create_chat("No user session")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.failure_category, "unavailable")
        self.assert_store_valid(harness.store, "internal-bridge-no-user-session")

    def test_a_later_turn_recovers(self):
        """And the chat recovers, which is what #85's turn-floor correction made
        representable. Before `4ff8b63` this store did not validate."""
        launcher = InternalBridgeLauncher(bridge_ready=False)
        harness = support.deterministic({}, "ib8", launcher=launcher)
        chat_id = harness.create_chat("The bridge comes back")
        harness.send_turn(chat_id, "hello")
        launcher._ready = True
        harness.send_turn(chat_id, "are you there now?")
        self.assertEqual([s["state"] for s in harness.store.sessions_of(chat_id)],
                         ["launch_failed", "completed"])
        self.assert_store_valid(harness.store, "internal-bridge-recovered")


class TheUsersStopCannotReachATurnInFlight(unittest.TestCase, StoreCheck):
    """B3, and the property #86, #88 and #90 are now bound by.

    `launch` blocks for the whole agent run and `send_turn` holds the chat lock
    across both `launch` and the drain, so by the time any Stop can be processed
    the session has already reached `completed`. Contract 5.2's user-owned
    `running -> terminated` transition is unreachable on this launcher shape.

    This is a whole-harness property rather than an internal-path quirk, and it
    is asserted here over the *state*, never over elapsed time.
    """

    def test_a_stop_after_the_turn_finds_nothing_running(self):
        harness = support.deterministic({}, "ib9", launcher=InternalBridgeLauncher())
        chat_id = harness.create_chat("Stop")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(harness.store.sessions_of(chat_id)[0]["state"], "completed")
        with self.assertRaises(NotPermitted):
            harness.stop_agent(chat_id, "the user pressed Stop")

    def test_no_session_on_this_path_ever_reaches_terminated(self):
        harness = support.deterministic({}, "ib10", launcher=InternalBridgeLauncher())
        chat_id = run_three_turns(harness, "Never terminated")
        for session in harness.store.sessions_of(chat_id):
            self.assertNotIn("terminated", [t["to"] for t in session["transitions"]],
                             "5.2's running -> terminated is unreachable here")


class ARestartWithALiveSessionNeedsAnAbandonAffordance(unittest.TestCase, StoreCheck):
    """B4's second half. The one-shot bridge keeps no state, so after a restart
    `events` raises, the harness records `reattach_failed`, the session becomes
    `unknown`, and the **only** exit is the user's `abandon`. #86 owes a control."""

    def setUp(self):
        directory = tempfile.mkdtemp(prefix="dory-internal-bridge-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.path = os.path.join(directory, "records.jsonl")

    def test_a_restart_ends_in_unknown_and_the_user_abandons(self):
        harness = support.deterministic(
            {}, "ib11", store_path=self.path,
            launcher=InternalBridgeLauncher(report_completion=False))
        chat_id = harness.create_chat("Restart")
        harness.send_turn(chat_id, "hello")
        session_id = harness.store.non_terminal_sessions(chat_id)[0]["session_id"]

        # A fresh launcher: a new process, remembering nothing. The harness has
        # only the handle it recorded, and on this path that is not enough.
        reopened = support.deterministic(
            {}, "ib12", store_path=self.path, start=support.RESTARTED,
            launcher=InternalBridgeLauncher(report_completion=False))
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual([o["kind"] for o in reopened.store.observations_of(session_id)],
                         ["reattach_failed"])
        self.assertEqual(len(reopened.store.open_bindings(chat_id)), 1,
                         "an `unknown` session keeps its binding; the agent may "
                         "still be out there")

        # The user's Stop is attempted -- the harness has the handle, so it has
        # something to pass -- and the fresh launcher cannot reach it. An
        # unconfirmed stop is an observation, not a resolution: the session stays
        # `unknown` and `abandon` is still the only exit.
        self.assertEqual(reopened.stop_agent(chat_id, "the user pressed Stop"),
                         "unknown")
        self.assertEqual([o["kind"] for o in reopened.store.observations_of(session_id)],
                         ["reattach_failed", "stop_unconfirmed"])
        reopened.abandon(chat_id)
        self.assertEqual(
            reopened.store.get("agent_session", session_id)["state"], "abandoned")
        self.assertEqual(reopened.store.open_bindings(chat_id), [],
                         "abandoning is terminal, so the chat is free to bind again")
        self.assert_store_valid(
            reopened.store, "internal-bridge-restart-abandoned",
            "A harness restart with a live session on a launcher that keeps no "
            "state across one. The session reaches `unknown` and the user's "
            "abandon is the only exit, which is the affordance #86 owes.")

    def test_the_stop_after_a_restart_is_refused_for_the_right_reason(self):
        """Not because the state is wrong, but because the handle addresses
        nothing the new launcher knows -- the practical face of C1."""
        launcher = InternalBridgeLauncher()
        harness = support.deterministic({}, "ib13", store_path=self.path,
                                        launcher=launcher)
        chat_id = harness.create_chat("Stop after restart")
        harness.send_turn(chat_id, "hello")
        handle = harness.store.sessions_of(chat_id)[0]["agent_handle"]

        fresh = InternalBridgeLauncher()
        with self.assertRaises(lb.LauncherError) as caught:
            fresh.stop(handle, "the user pressed Stop")
        self.assertEqual(caught.exception.category, "unavailable")


if __name__ == "__main__":
    unittest.main()
