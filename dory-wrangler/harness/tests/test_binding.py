"""One chat, one agent -- enforced about agents, not about bookkeeping.

Contract 1 and 4.4. The rule has three parts and all three are needed: at most
one open binding per chat, at most one non-terminal session per chat, and every
non-terminal session held by exactly one open binding. A test that only checks
the binding ledger is checking the label on the rule rather than the fact.
"""

from __future__ import annotations

import threading
import unittest

import support
from support import StoreCheck

from errors import ConcurrentLaunchRefused, NotPermitted


def live_agent_harness(salt="b", continuation="fresh_binding"):
    """A launcher whose agent stays running after the turn, so a chat really does
    hold a live agent when the next turn arrives."""
    return support.deterministic(
        {"launcher": "scripted-stub",
         "options": {"continuation": continuation, "response_shape": "stream",
                     "end_of_turn": "turn_complete"}}, salt)


class ASecondConcurrentLaunchIsRefused(unittest.TestCase, StoreCheck):

    def test_a_second_turn_to_a_fresh_binding_launcher_with_a_live_agent(self):
        harness = live_agent_harness()
        chat_id = harness.create_chat("One agent")
        harness.send_turn(chat_id, "first")
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1)

        with self.assertRaises(ConcurrentLaunchRefused) as caught:
            harness.send_turn(chat_id, "second")
        reason = caught.exception.reason
        self.assertIn("already served by agent session", reason)
        self.assertIn("one agent per chat", reason)

        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1,
                         "the refusal must not have created a second session")
        self.assertEqual(len(harness.store.messages(chat_id)), 2,
                         "a refused turn is refused, not queued and not recorded as a "
                         "turn the agent went on to answer")
        self.assert_store_valid(harness.store, "second-launch-refused")

    def test_the_refusal_is_stated_and_never_silent(self):
        harness = live_agent_harness("c")
        chat_id = harness.create_chat("Stated reason")
        harness.send_turn(chat_id, "first")
        try:
            harness.send_turn(chat_id, "second")
        except ConcurrentLaunchRefused as exc:
            self.assertTrue(exc.reason and len(exc.reason) > 40)
            self.assertIn(chat_id, exc.reason)
        else:
            self.fail("the second launch was permitted")

    def test_a_released_binding_does_not_unlock_a_second_agent(self):
        """The probe that matters. A rule enforced over open bindings alone lets
        a chat whose binding was released too early start a second live agent
        while the ledger still reads as one-agent-per-chat."""
        harness = live_agent_harness("d")
        chat_id = harness.create_chat("Released too early")
        harness.send_turn(chat_id, "first")
        session_id = harness.store.non_terminal_sessions(chat_id)[0]["session_id"]

        binding = harness.store.binding_for_session(session_id)
        binding["released_at"] = harness._clock.now()
        harness.store.put(binding)
        self.assertEqual(harness.store.open_bindings(chat_id), [],
                         "the ledger now reads as no agent bound")

        with self.assertRaises(ConcurrentLaunchRefused) as caught:
            harness.send_turn(chat_id, "second")
        self.assertIn("already served by agent session", caught.exception.reason)
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1)

        # And the doctored store is itself rejected, by the rule about agents.
        found = self.assert_store_rejected_for(harness.store, "UNBOUND_ACTIVE_SESSION")
        self.assertIn("BINDING_RELEASED_BEFORE_TERMINAL", found)

    def test_an_open_binding_on_a_finished_session_also_refuses(self):
        """The mirror image: the session is terminal but the ledger still holds
        it. A rule enforced over sessions alone would launch a second agent here."""
        harness = live_agent_harness("e")
        chat_id = harness.create_chat("Stale binding")
        harness.send_turn(chat_id, "first")
        session = harness.store.non_terminal_sessions(chat_id)[0]
        session["state"] = "completed"
        session["transitions"].append({
            "from": "running", "to": "completed", "owner": "launcher",
            "at": harness._clock.now(),
            "evidence": {"kind": "event", "ref": harness.store.events_of(
                session["session_id"])[0]["event_id"]}})
        harness.store.put(session)

        with self.assertRaises(ConcurrentLaunchRefused) as caught:
            harness.send_turn(chat_id, "second")
        self.assertIn("still holds an open binding", caught.exception.reason)
        self.assert_store_rejected_for(harness.store, "BINDING_OPEN_ON_TERMINAL_SESSION")

    def test_a_launcher_that_re_enters_the_chat_loop_is_refused(self):
        """A launcher calling back into `send_turn` during its own `launch`. The
        chat is claimed before the harness calls out, so the re-entrant call
        finds a session already recorded. A binding opened after the launch
        returned would let this through."""
        results = {}

        def on_launch(instruction):
            try:
                harness.send_turn(instruction.chat_id, "re-entrant")
            except ConcurrentLaunchRefused as exc:
                results["refused"] = exc.reason

        harness = support.deterministic(
            {"launcher": "scripted-stub", "options": {"on_launch": on_launch}}, "f")
        chat_id = harness.create_chat("Re-entrancy")
        harness.send_turn(chat_id, "first")

        self.assertIn("refused", results, "the re-entrant launch was permitted")
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1)
        self.assert_store_valid(harness.store, "reentrant-launch-refused")

    def test_concurrent_threads_produce_exactly_one_agent(self):
        harness = live_agent_harness("g")
        chat_id = harness.create_chat("Threads")
        outcomes = []
        lock = threading.Lock()

        def attempt(n):
            try:
                harness.send_turn(chat_id, "turn %d" % n)
            except ConcurrentLaunchRefused:
                with lock:
                    outcomes.append("refused")
            else:
                with lock:
                    outcomes.append("launched")

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("launched"), 1, outcomes)
        self.assertEqual(outcomes.count("refused"), 5, outcomes)
        self.assertEqual(len(harness.store.sessions_of(chat_id)), 1)
        self.assert_store_valid(harness.store, "concurrent-threads-one-agent")


class SequentialRebindingIsPermitted(unittest.TestCase, StoreCheck):
    """This is a concurrency rule, not a lifetime rule (contract 4.4)."""

    def test_a_chat_binds_many_agents_one_after_another(self):
        harness = support.deterministic(
            {"launcher": "dev-local", "options": {"profile": "one_shot"}}, "h")
        self.addCleanup(support.release, harness)
        chat_id = support.run_three_turns(harness, "Sequential rebinding")
        sessions = harness.store.sessions_of(chat_id)
        self.assertEqual(len(sessions), 3)
        self.assertEqual([s["state"] for s in sessions], ["completed"] * 3)
        self.assertEqual(harness.store.open_bindings(chat_id), [])
        self.assertEqual(len(harness.store.all_of("agent_binding")), 3)
        self.assert_store_valid(harness.store, "sequential-rebinding")

    def test_each_later_binding_is_opened_by_the_users_own_turn(self):
        """Contract 4.4: no harness autonomy, no timer, no inactivity inference.
        Each session's creation transition cites the user message that caused it,
        and every one of those messages is a distinct user turn."""
        harness = support.deterministic(
            {"launcher": "scripted-stub"}, "i")
        chat_id = support.run_three_turns(harness, "User opens each binding")
        openers = []
        for session in harness.store.sessions_of(chat_id):
            creation = session["transitions"][0]
            self.assertIsNone(creation["from"])
            self.assertEqual(creation["owner"], "user")
            self.assertEqual(creation["evidence"]["kind"], "user_action")
            message = harness.store.get("message", creation["evidence"]["ref"])
            self.assertIsNotNone(message)
            self.assertEqual(message["author"], "user")
            openers.append(message["message_id"])
        self.assertEqual(len(set(openers)), 3, "one turn opens at most one agent")


class UserActionsAreTheOnlyWayOut(unittest.TestCase, StoreCheck):

    def test_only_the_user_may_stop_a_running_agent(self):
        harness = live_agent_harness("j")
        chat_id = harness.create_chat("Stop")
        harness.send_turn(chat_id, "first")
        self.assertEqual(harness.stop_agent(chat_id, "the user said so"), "terminated")
        session = harness.store.sessions_of(chat_id)[0]
        last = session["transitions"][-1]
        self.assertEqual((last["from"], last["to"], last["owner"]),
                         ("running", "terminated", "user"))
        self.assertEqual(harness.store.open_bindings(chat_id), [])
        self.assert_store_valid(harness.store, "user-stop-confirmed")

    def test_abandon_is_available_only_from_unknown(self):
        harness = live_agent_harness("k")
        chat_id = harness.create_chat("Abandon")
        harness.send_turn(chat_id, "first")
        with self.assertRaises(NotPermitted):
            harness.abandon(chat_id)


if __name__ == "__main__":
    unittest.main()
