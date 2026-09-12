"""A divergence between contract 6.4's prose and its executable form.

**This test asserts a defect, not a guarantee.** It exists so the divergence is
reproducible, attributable, and loud, and so that it fails the moment #85 closes
it. #87 does not amend the contract or work around it.

What the prose says (contract 6.4):

    the number of instruction packets ... must be at least the number of user
    messages **the agent went on to answer**

What the validator computes:

    answered = len([s for s in bucket["user"] if s < bucket["last_agent"]])

-- every user message before the last agent message, whether any agent answered
it or not. Combined with the rule directly above it, that only packets on
sessions which reached `running` count, the two together reject a store the
prose accepts:

* the user sends a turn; the launch fails, or returns `unknown` and the user
  abandons it. That turn was answered by nobody, and its packet correctly does
  not count.
* the user sends another turn; this one is served and answered.
* the first turn is now "before the last agent message", so it is counted as
  answered, and the chat is one packet short.

The consequence is product-level and contradicts contract 5.4 in prose: **a chat
can never recover from a launch failure.** 5.4 states that abandoning is terminal,
"the binding is released, and the chat can bind a new agent. The user is never
stuck" -- and the store that flow produces does not validate.

Both stores below are produced by the harness behaving exactly as sections 4.3,
5.2 and 5.4 require. Neither can be avoided without degrading the product:
contract 4.3 requires the session to exist before the launch is attempted "so
launch failures are attributable to a durable record rather than being lost".

This is #85's to settle. A faithful `answered` would exclude user turns that
opened a session which never reached `running`, but that interacts with the
decoy-padding rule in the same paragraph, so the fix is a contract decision
rather than an edit #87 may make.
"""

from __future__ import annotations

import unittest

import support
from support import StoreCheck, codes


class ARecoveredChatDoesNotValidate(unittest.TestCase, StoreCheck):

    def _chat_that_recovered(self, salt, first_outcome, abandon):
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"launch_outcomes": [first_outcome]}}, salt)
        chat_id = harness.create_chat("Recovery after a failed start")
        harness.send_turn(chat_id, "hello")
        if abandon:
            harness.abandon(chat_id)
        harness.send_turn(chat_id, "let us try that again")
        return harness, chat_id

    def test_a_launch_failure_followed_by_a_served_turn(self):
        harness, chat_id = self._chat_that_recovered("k1", "unavailable", False)
        self.keep_divergent(
            harness.store, "divergent-launch-failure-then-served-turn",
            "Accurate. The user's first turn was never sent to any agent (the "
            "launch failed as 'unavailable'), so no agent answered it; the second "
            "turn was launched, sent and answered. One packet reached an agent and "
            "one turn was answered, which satisfies contract 6.4's prose. The "
            "validator counts the unanswered first turn as answered because it "
            "precedes the last agent message.")
        self.assertEqual(
            [state for state in
             (s["state"] for s in harness.store.sessions_of(chat_id))],
            ["launch_failed", "completed"])
        self.assertEqual(len(harness.transcript(chat_id)), 3,
                         "one unanswered turn, one answered turn, one agent reply")
        self.assertIn("TURN_INSTRUCTION_MISSING", codes(harness.store.snapshot()),
                      "if this no longer fires, #85 has closed the divergence and "
                      "this characterization test should be deleted")

    def test_the_abandon_and_rebind_flow_contract_5_4_promises(self):
        harness, chat_id = self._chat_that_recovered("k2", "unknown", True)
        self.keep_divergent(
            harness.store, "divergent-abandon-then-rebind",
            "Accurate. The first launch returned 'unknown', the user abandoned it, "
            "and the chat bound a new agent -- the exact flow contract 5.4 promises "
            "in prose ('the chat can bind a new agent. The user is never stuck'). "
            "No agent ever answered the first turn.")
        self.assertEqual(
            [s["state"] for s in harness.store.sessions_of(chat_id)],
            ["abandoned", "completed"],
            "contract 5.4: abandoning is terminal, the binding is released, and "
            "the chat can bind a new agent")
        self.assertIn("TURN_INSTRUCTION_MISSING", codes(harness.store.snapshot()))

    def test_the_divergence_is_confined_to_this_one_rule(self):
        """Everything else about the recovered store is correct, which is what
        makes this a defect in the rule rather than in the harness."""
        harness, _ = self._chat_that_recovered("k3", "unavailable", False)
        self.assertEqual(codes(harness.store.snapshot()), ["TURN_INSTRUCTION_MISSING"])

    def test_the_same_chat_without_the_earlier_failure_validates(self):
        """The control. Remove only the failed first turn and the store is clean,
        so nothing else in the harness's output is responsible."""
        harness = support.deterministic({"launcher": "scripted-stub"}, "k4")
        chat_id = harness.create_chat("No earlier failure")
        harness.send_turn(chat_id, "hello")
        harness.send_turn(chat_id, "let us try that again")
        self.assert_store_valid(harness.store, "recovery-control-no-earlier-failure")


class TheShapeChatUiWorkWillHit(unittest.TestCase, StoreCheck):
    """#86's instance of the same defect, reproduced here so the fix has it.

    #87 refuses a concurrent turn *before* recording it, so this shape does not
    come out of this harness -- see the handoff, where that decision and its
    interaction with this rule are disclosed. A chat shell that records the
    user's turn and then shows a refusal produces exactly this: two user turns,
    one agent answer, one launch, nothing misrecorded.
    """

    def test_a_recorded_but_unanswered_turn_trips_the_same_rule(self):
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "fresh_binding", "response_shape": "stream",
                         "end_of_turn": "turn_complete"}}, "k5")
        chat_id = harness.create_chat("A turn that was refused")
        harness.send_turn(chat_id, "hello")

        # The turn a chat shell would record and then refuse: no session, no
        # packet, no agent ever saw it.
        harness._append_message(chat_id, "user", "are you still there?")
        support.end_chat(harness, chat_id)
        harness._append_message(chat_id, "user", "let us try again")
        second = support.deterministic(
            {"launcher": "scripted-stub"}, "k6")

        self.assertEqual([a for _, a, _ in harness.transcript(chat_id)],
                         ["user", "agent", "user", "user"])
        self.assertEqual(len(harness.store.all_of("launch_request")), 1)
        self.assertEqual(codes(harness.store.snapshot()), [],
                         "an unanswered turn at the end of a chat is fine; the rule "
                         "only fires once a later turn is answered")

        # Now a later turn is answered, and the same accurate store is rejected.
        harness2 = support.deterministic(
            {"launcher": "scripted-stub"}, "k7")
        chat2 = harness2.create_chat("A turn that was refused, then answered")
        harness2.send_turn(chat2, "hello")
        harness2._append_message(chat2, "user", "are you still there?")
        harness2.send_turn(chat2, "let us try again")
        self.assertEqual(len(harness2.store.all_of("launch_request")), 2)
        self.assertIn("TURN_INSTRUCTION_MISSING", codes(harness2.store.snapshot()))
        self.keep_divergent(
            harness2.store, "divergent-recorded-but-unanswered-turn",
            "Accurate. Three user turns, two of which were launched and answered, "
            "and one of which no agent ever received -- the shape a chat shell "
            "produces when it records the user's turn and then refuses it. Two "
            "packets reached agents and two turns were answered.")


if __name__ == "__main__":
    unittest.main()
