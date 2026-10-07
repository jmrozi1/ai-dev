"""The stores the old turn-instruction floor rejected, now as accept-regressions.

#87 filed a divergence between contract 6.4's prose and its executable form: the
floor counted *every user message preceding the last agent message*, whether any
agent answered it or not, so a chat that recovered from a failed launch could
never validate. That contradicted contract 5.4's promise in prose -- "the binding
is released, and the chat can bind a new agent. **The user is never stuck**".

#85 closed it at `4ff8b63`: the floor now counts *occasions on which the agent
answered*, one per maximal run of consecutive agent messages.

This file is the other side of that fix. Every store here was an `expect: accept`
regression case exported by #87's earlier rail, produced by the harness behaving
exactly as sections 4.3, 5.2 and 5.4 require. Each one used to be rejected and
each one must now be accepted. **No workaround was kept**: the harness was not
changed for any of this, and the separate divergence phase of the test runner is
gone, so these stores are validated in the same phase as every other store the
suite produces.

The three shapes, which are contract fixtures `valid/16`, `valid/17` and
`valid/18` upstream:

* a launch that failed, so the turn reached nobody, followed by a later turn that
  was served and answered;
* the abandon-then-rebind flow 5.4 promises;
* a turn recorded by a chat shell and then refused, so no agent ever received it.
"""

from __future__ import annotations

import unittest

import support
from support import StoreCheck, codes

from dory_wrangler.errors import ProvenanceRefused


class AChatThatRecoveredNowValidates(unittest.TestCase, StoreCheck):

    def _chat_that_recovered(self, salt, first_outcome, abandon):
        harness = support.harness({'launcher': 'scripted-stub', 'options': {'launch_outcomes': [first_outcome]}})
        chat_id = harness.create_chat("Recovery after a failed start")
        harness.send_turn(chat_id, "hello")
        if abandon:
            harness.abandon(chat_id)
        harness.send_turn(chat_id, "let us try that again")
        return harness, chat_id

    def test_a_launch_failure_followed_by_a_served_turn(self):
        harness, chat_id = self._chat_that_recovered("k1", "unavailable", False)
        self.assertEqual(
            [s["state"] for s in support.view(harness).sessions_of(chat_id)],
            ["launch_failed", "completed"])
        self.assertEqual(len(harness.transcript(chat_id)), 3,
                         "one unanswered turn, one answered turn, one agent reply")
        self.assertEqual(len(support.view(harness).all_of("launch_request")), 2)
        self.assert_store_valid(
            harness.store, "turn-floor-launch-failure-then-served-turn",
            "The user's first turn was never sent to any agent (the launch failed "
            "as 'unavailable'), so no agent answered it; the second turn was "
            "launched, sent and answered. One packet reached an agent and the "
            "agent answered on one occasion. Rejected by the pre-4ff8b63 floor.")

    def test_the_abandon_and_rebind_flow_contract_5_4_promises(self):
        harness, chat_id = self._chat_that_recovered("k2", "unknown", True)
        self.assertEqual(
            [s["state"] for s in support.view(harness).sessions_of(chat_id)],
            ["abandoned", "completed"],
            "contract 5.4: abandoning is terminal, the binding is released, and "
            "the chat can bind a new agent")
        self.assert_store_valid(
            harness.store, "turn-floor-abandon-then-rebind",
            "The first launch returned 'unknown', the user abandoned it, and the "
            "chat bound a new agent -- the exact flow contract 5.4 promises. No "
            "agent ever answered the first turn. Rejected by the pre-4ff8b63 floor.")

    def test_no_other_rule_fires_on_the_recovered_store(self):
        """What made this a defect in the rule rather than in the harness: the
        recovered store was correct in every other respect, and still is."""
        harness, _ = self._chat_that_recovered("k3", "unavailable", False)
        self.assertEqual(codes(support.view(harness).snapshot()), [])

    def test_the_same_chat_without_the_earlier_failure_validates(self):
        """The control. It validated before the fix and must still validate, so a
        rule that had simply been deleted would not be mistaken for a rule that
        was corrected -- see `TheFloorStillRefusesUnderRecording` below."""
        harness = support.harness({'launcher': 'scripted-stub'})
        chat_id = harness.create_chat("No earlier failure")
        harness.send_turn(chat_id, "hello")
        harness.send_turn(chat_id, "let us try that again")
        self.assert_store_valid(harness.store, "turn-floor-control-no-earlier-failure")


class ARecordedButUnansweredTurnNowValidates(unittest.TestCase, StoreCheck):
    """#86's instance of the same defect.

    #87 refuses a concurrent turn *before* recording it, so this shape does not
    come out of this harness. A chat shell that records the user's turn and then
    shows a refusal produces exactly this: three user turns, two answered, one
    launch per answered turn, nothing misrecorded.
    """

    def test_a_recorded_but_unanswered_turn_validates(self):
        harness = support.harness({'launcher': 'scripted-stub'})
        chat_id = harness.create_chat("A turn that was refused, then answered")
        harness.send_turn(chat_id, "hello")
        # The recorded-then-refused turn, written the way #86's shell wrote it.
        harness.store.append_user_message(chat_id, "are you still there?")
        harness.send_turn(chat_id, "let us try again")

        self.assertEqual([a for _, a, _ in harness.transcript(chat_id)],
                         ["user", "agent", "user", "user", "agent"])
        self.assertEqual(len(support.view(harness).all_of("launch_request")), 2)
        self.assert_store_valid(
            harness.store, "turn-floor-recorded-but-unanswered-turn",
            "Three user turns, two of which were launched and answered, and one "
            "of which no agent ever received -- the shape a chat shell produces "
            "when it records the user's turn and then refuses it. Two packets "
            "reached agents and the agent answered on two occasions. Rejected by "
            "the pre-4ff8b63 floor.")


class TheFloorStillRefusesUnderRecording(unittest.TestCase, StoreCheck):
    """The corrected rule is not a deleted rule.

    Accepting the three shapes above would be satisfied just as well by removing
    `TURN_INSTRUCTION_MISSING` altogether, so accepting them proves nothing on
    its own. These construct the under-recording the floor exists to catch and
    require it to still be caught.
    """

    def test_an_answer_with_no_instruction_packet_at_all_is_refused(self):
        harness = support.harness({'launcher': 'scripted-stub'})
        chat_id = harness.create_chat("An answer nobody asked for")
        harness.send_turn(chat_id, "hello")
        # A second answered turn whose instruction packet was never written: the
        # under-recording the floor is for. The store refuses to write that
        # answer at all, so the shape is forged onto a snapshot of the real store
        # and handed to the contract, which must refuse it for the floor.
        harness.store.append_user_message(chat_id, "and again?")
        forged = support.view(harness)
        session_id = forged.sessions_of(chat_id)[0]["session_id"]
        first_answer = [m for m in forged.messages(chat_id) if m["author"] == "agent"][0]
        forged.put(dict(first_answer, message_id="msg_forgedanswer01", sequence=4,
                        created_at=support.now(),
                        content={"content_type": "text/plain",
                                 "text": "answer to: and again?"},
                        session_id=session_id))
        self.assertEqual(codes(forged.snapshot()), ["TURN_INSTRUCTION_MISSING"])
        with self.assertRaises(ProvenanceRefused):
            harness.store.append_agent_message(
                chat_id, session_id, first_answer["source_event_id"],
                "answer to: and again?")

    def test_the_agent_speaking_first_is_still_refused(self):
        harness = support.harness({'launcher': 'scripted-stub'})
        chat_id = harness.create_chat("The agent speaks first")
        forged = support.view(harness)
        forged.put({
            "record_type": "message", "record_version": 1,
            "message_id": "msg_agentfirst001", "chat_id": chat_id, "sequence": 1,
            "author": "agent", "created_at": support.now(),
            "content": {"content_type": "text/plain",
                        "text": "hello, did you want something?"},
            "session_id": None, "source_event_id": None})
        self.assert_store_rejected_for(forged, "TURN_INSTRUCTION_MISSING")


if __name__ == "__main__":
    unittest.main()
