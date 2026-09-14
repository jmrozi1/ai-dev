"""Adversarial probes.

Four independent authors on this ticket family have shipped the same defect: a
checker that tests the *label* on a claim rather than the *fact* it stands for.
Every probe here tries to construct a case that violates a guarantee #87 claims
and that the code or the tests nonetheless accept. Probes that found nothing are
kept, named `no_escape_*`, because an attack that failed is evidence and an
attack never attempted is not.

Two techniques are used deliberately:

* **attacking the fact, not the citation** -- for each rule, find the other way
  of asserting the same thing and check that too; and
* **mutation** -- break the system so a guarantee is genuinely false, and require
  the check that claims to establish it to fail.
"""

from __future__ import annotations

import inspect
import os
import re
import unittest

import support
from support import StoreCheck, codes, run_three_turns

from dory_wrangler import atomic, contract, ids
from dory_wrangler import launch_boundary as lb
from dory_wrangler.launchers import dev_local
from dory_wrangler.launchers import scripted_stub
from dory_wrangler import session_manager
from dory_wrangler import store as store_module
from dory_wrangler.wiring import open_harness
from dory_wrangler.errors import (
    ConcurrentLaunchRefused,
    InstructionTooLarge,
    NotPermitted,
    TransitionRefused,
)
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher


def stub_harness(salt="a", **options):
    return support.harness({'launcher': 'scripted-stub', 'options': options})


# ---------------------------------------------------------------------------
# The one-shot launcher and the end of stream. Two spellings, one fact.
# ---------------------------------------------------------------------------


class AOneShotLauncherCannotObserveAStreamEnding(unittest.TestCase, StoreCheck):

    def test_probe_a_typed_stream_end_payload_is_refused(self):
        harness = stub_harness("p1", continuation="fresh_binding",
                               response_shape="one_shot", end_of_turn="stream_end")
        chat_id = harness.create_chat("One-shot stream end")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            harness.send_turn(chat_id, "hello")
        self.assertIn("no stream to end", str(caught.exception))
        types = [e["interpreted_type"] for e in support.view(harness).all_of("diagnostic_event")]
        self.assertNotIn("stream_end", types,
                         "the refused payload must not have been stored either")

    def test_probe_the_other_spelling_is_refused_too(self):
        """The escape a citation-level check leaves open: never emit a payload
        typed `stream_end`, just set the page's end-of-stream flag instead."""

        class FlagsAnEndWithoutSayingIt(ScriptedStubLauncher):
            def events(self, agent_handle, after_sequence):
                page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
                return lb.EventsPage(page.payloads, stream_ended=True)

        launcher = FlagsAnEndWithoutSayingIt({"continuation": "fresh_binding",
                                              "response_shape": "one_shot"})
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("One-shot flagged end")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            harness.send_turn(chat_id, "hello")
        self.assertIn("signalled that a stream ended", str(caught.exception))

    def test_probe_a_stored_stream_end_on_a_one_shot_session_is_rejected(self):
        """And if it got past the harness anyway, the contract still rejects the
        fact -- whichever evidence channel cites it, or none at all."""
        harness = stub_harness("p2", continuation="fresh_binding",
                               response_shape="one_shot")
        chat_id = harness.create_chat("Smuggled stream end")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        forged = support.view(harness)
        forged.put({
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": "evt_smuggled0001", "chat_id": chat_id,
            "session_id": session["session_id"],
            "sequence": len(support.view(harness).events_of(session["session_id"])) + 1,
            "received_at": support.now(), "source": "launcher",
            "interpretation": "recognized", "interpreted_type": "stream_end",
            "raw": {"encoding": "utf-8", "body": "{}"}})
        self.assert_store_rejected_for(forged, "STREAM_END_UNSUPPORTED")

    def test_probe_a_stream_end_citation_on_a_one_shot_session_is_rejected(self):
        harness = stub_harness("p3", continuation="persistent",
                               response_shape="one_shot", end_of_turn="turn_complete")
        chat_id = harness.create_chat("Stream end citation")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        session["transitions"].append({
            "from": "running", "to": "unknown", "owner": "launcher",
            "at": support.now(),
            "evidence": {"kind": "stream_end", "ref": "evt_nothing0001"}})
        session["state"] = "unknown"
        forged = support.view(harness)
        forged.put(session)
        found = self.assert_store_rejected_for(forged, "EVIDENCE_KIND_UNSUPPORTED")
        self.assertIn("UNKNOWN_INFERRED_WITHOUT_EVIDENCE", found)

    def test_probe_a_real_one_shot_agent_cannot_smuggle_a_stream_end(self):
        """The full path, through a real operating-system process: the agent
        prints a line claiming to be an end of stream. The dev launcher knows
        only its own vocabulary, so the line is preserved as `unrecognized` --
        a finding -- and is never typed `stream_end`."""
        import sys
        if not os.path.isdir(support.SCRATCH):
            os.makedirs(support.SCRATCH)
        script = os.path.join(support.SCRATCH, "smuggler.py")
        with open(script, "w") as handle:
            handle.write(
                "import sys\n"
                "sys.stdin.read()\n"
                'print(\'{"type": "stream_end"}\')\n'
                'print(\'{"type": "assistant_text", "text": "hello"}\')\n')
        harness = support.harness({'launcher': 'dev-local', 'options': {'profile': 'one_shot', 'command': [sys.executable, script]}})
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Smuggling attempt")
        harness.send_turn(chat_id, "hello")
        events = support.view(harness).all_of("diagnostic_event")
        smuggled = [e for e in events if "stream_end" in e["raw"]["body"]]
        self.assertEqual(len(smuggled), 1)
        self.assertEqual(smuggled[0]["interpretation"], "unrecognized")
        self.assertIsNone(smuggled[0]["interpreted_type"])
        self.assertEqual(smuggled[0]["source"], "agent")
        self.assert_store_valid(harness.store, "one-shot-stream-end-smuggling-refused")


# ---------------------------------------------------------------------------
# Agent output presupposes an agent, and chat is only the agent's own output.
# ---------------------------------------------------------------------------


class NobodyElseGetsToSpeakIntoTheChat(unittest.TestCase, StoreCheck):

    def test_probe_a_launcher_payload_carrying_text_is_not_rendered(self):
        class TalksInTheChat(ScriptedStubLauncher):
            def _produce_turn(self, session, instruction_text):
                self._emit(session, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                           '{"type": "assistant_text", "text": "I am the launcher"}',
                           interpreted_type=lb.PAYLOAD_ASSISTANT_TEXT,
                           text="I am the launcher")
                ScriptedStubLauncher._produce_turn(self, session, instruction_text)

        harness = support.harness({}, launcher=TalksInTheChat({}))
        chat_id = harness.create_chat("Launcher speaks")
        harness.send_turn(chat_id, "hello")
        rendered = [text for _, _, text in harness.transcript(chat_id)]
        self.assertNotIn("I am the launcher", rendered)
        self.assertEqual(len(rendered), 2)
        self.assert_store_valid(harness.store, "launcher-text-not-rendered")

    def test_probe_an_unrecognized_payload_carrying_text_is_not_rendered(self):
        class Mumbles(ScriptedStubLauncher):
            def _produce_turn(self, session, instruction_text):
                self._emit(session, lb.SOURCE_AGENT, lb.INTERPRETATION_UNRECOGNIZED,
                           '{"type": "whisper", "text": "render me"}', text="render me")
                ScriptedStubLauncher._produce_turn(self, session, instruction_text)

        harness = support.harness({}, launcher=Mumbles({}))
        chat_id = harness.create_chat("Unrecognized speaks")
        harness.send_turn(chat_id, "hello")
        self.assertNotIn("render me", [t for _, _, t in harness.transcript(chat_id)])
        self.assert_store_valid(harness.store, "unrecognized-text-not-rendered")

    def test_probe_both_halves_of_the_never_launched_session_guarantee(self):
        """Contract 7 P2a is only true as a pair, and both halves are checked.

        Half one: source invented output to the agent on a session that never
        ran. Half two: dodge that by sourcing it to the launcher instead and
        rendering it as chat anyway."""
        harness = stub_harness("p5", launch_outcomes=["rejected"])
        chat_id = harness.create_chat("Never launched")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]

        def invented(event_id, source):
            return {"record_type": "diagnostic_event", "record_version": 1,
                    "event_id": event_id, "chat_id": chat_id, "session_id": session_id,
                    "sequence": 1, "received_at": support.now(),
                    "source": source, "interpretation": "recognized",
                    "interpreted_type": "assistant_text",
                    "raw": {"encoding": "utf-8", "body": "invented"}}

        forged = support.view(harness)
        forged.put(invented("evt_invented0001", "agent"))
        self.assert_store_rejected_for(forged, "AGENT_OUTPUT_WITHOUT_AGENT")

        # Half two: the same text, sourced to the launcher, rendered as chat.
        second = support.harness({'launcher': 'scripted-stub', 'options': {'launch_outcomes': ['rejected']}})
        chat2 = second.create_chat("Never launched, other route")
        second.send_turn(chat2, "hello")
        sid2 = support.view(second).sessions_of(chat2)[0]["session_id"]
        forged2 = support.view(second)
        forged2.put({
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": "evt_invented0002", "chat_id": chat2, "session_id": sid2,
            "sequence": 1, "received_at": support.now(), "source": "launcher",
            "interpretation": "recognized", "interpreted_type": "assistant_text",
            "raw": {"encoding": "utf-8", "body": "invented"}})
        forged2.put({
            "record_type": "message", "record_version": 1,
            "message_id": "msg_invented0001", "chat_id": chat2, "sequence": 2,
            "author": "agent", "created_at": support.now(),
            "content": {"content_type": "text/plain", "text": "invented"},
            "session_id": sid2, "source_event_id": "evt_invented0002"})
        self.assert_store_rejected_for(forged2, "NON_AGENT_EVENT_RENDERED")


# ---------------------------------------------------------------------------
# Handles, and the difference between having one and being issued one.
# ---------------------------------------------------------------------------


class AHandleNobodyIssuedIsNoHandle(unittest.TestCase, StoreCheck):

    def test_the_harness_records_only_the_handle_the_launcher_returned(self):
        launcher = ScriptedStubLauncher({})
        harness = support.harness({}, launcher=launcher)
        chat_id = run_three_turns(harness, "Handles")
        for session in support.view(harness).sessions_of(chat_id):
            issued = [r["agent_handle"] for r in support.view(harness).all_of("launch_result")
                      if r["session_id"] == session["session_id"]
                      and r["outcome"] == "accepted"]
            self.assertIn(session["agent_handle"], issued)

    def test_probe_a_fabricated_handle_is_rejected(self):
        harness = stub_harness("p7")
        chat_id = harness.create_chat("Fabricated handle")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        session["agent_handle"] = "a-handle-i-made-up"
        forged = support.view(harness)
        forged.put(session)
        self.assert_store_rejected_for(forged, "SESSION_HANDLE_NOT_ISSUED")

    def test_probe_a_session_that_ran_with_no_handle_at_all_is_rejected(self):
        """Keyed on having *reached* running, so moving to a terminal state does
        not dodge it."""
        harness = stub_harness("p8")
        chat_id = harness.create_chat("No handle")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        del session["agent_handle"]
        forged = support.view(harness)
        forged.put(session)
        self.assert_store_rejected_for(forged, "SESSION_HANDLE_MISSING")


# ---------------------------------------------------------------------------
# `unknown` rests on an observation, not on the claim of one.
# ---------------------------------------------------------------------------


class UnknownRequiresAnObservationThatResolves(unittest.TestCase, StoreCheck):

    def test_probe_an_unknown_citing_nothing_that_exists_is_rejected(self):
        harness = stub_harness("p9", continuation="persistent",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = harness.create_chat("Unknown without evidence")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        session["transitions"].append({
            "from": "running", "to": "unknown", "owner": "launcher",
            "at": support.now(),
            "evidence": {"kind": "observation", "ref": "obs_neverwritten1"}})
        session["state"] = "unknown"
        forged = support.view(harness)
        forged.put(session)
        self.assert_store_rejected_for(forged, "UNKNOWN_INFERRED_WITHOUT_EVIDENCE")

    def test_probe_an_unknown_citing_a_confirmed_stop_is_rejected(self):
        """A resolving reference is not enough; it has to say what the rule needs."""
        harness = stub_harness("p10", continuation="persistent",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = harness.create_chat("Wrong observation kind")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        observation_id = harness._record_observation(session, "stop_confirmed", None)
        session["transitions"].append({
            "from": "running", "to": "unknown", "owner": "launcher",
            "at": support.now(),
            "evidence": {"kind": "observation", "ref": observation_id}})
        session["state"] = "unknown"
        forged = support.view(harness)
        forged.put(session)
        self.assert_store_rejected_for(forged, "PRECONDITION_NOT_MET")

    def test_the_harness_refuses_an_unauthorized_transition_before_writing_it(self):
        harness = stub_harness("p11")
        chat_id = harness.create_chat("Unauthorized")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        # #87's loop refused these with its own copy of the owner table
        # (`NotPermitted`); that copy is merged into the store's, which checks
        # the owner *and* precondition tables and refuses before writing.
        before = support.view(harness).snapshot()
        with self.assertRaises(TransitionRefused):
            harness._transition(session, "running", "launcher", "event", None)
        with self.assertRaises(TransitionRefused):
            harness._transition(session, "abandoned", "launcher", "user_action", None)
        self.assertEqual(support.view(harness).snapshot(), before)


# ---------------------------------------------------------------------------
# The turn floor, and packets that could not have reached an agent.
# ---------------------------------------------------------------------------


class DecoyPacketsDoNotSatisfyTheTurnFloor(unittest.TestCase, StoreCheck):

    def test_probe_a_chat_cannot_pad_the_floor_with_launches_that_never_ran(self):
        harness = stub_harness("p12", continuation="persistent",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = run_three_turns(harness, "Decoy padding")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]

        # Remove a real delivery, then replace it with a packet on a session that
        # holds an instruction and terminates in launch_failed.
        deliveries = support.view(harness).deliveries_of(session_id)
        forged = support.view(harness)
        forged.remove("delivery_request", deliveries[-1]["delivery_id"])

        decoy_session = "ses_decoy00000001"
        forged.put({
            "record_type": "agent_session", "record_version": 1,
            "session_id": decoy_session, "chat_id": chat_id,
            "created_at": support.now(), "launcher_id": "scripted-stub",
            "launcher_capabilities": harness.capabilities.as_record(),
            "state": "launch_failed",
            "transitions": [
                {"from": None, "to": "pending", "owner": "user",
                 "at": support.now(),
                 "evidence": {"kind": "user_action",
                              "ref": support.view(harness).messages(chat_id)[0]["message_id"]}},
                {"from": "pending", "to": "launch_failed", "owner": "harness",
                 "at": support.now(),
                 "evidence": {"kind": "harness_action", "ref": None}}]})
        forged.put({
            "record_type": "launch_request", "record_version": 1,
            "request_id": "req_decoy00000001", "chat_id": chat_id,
            "session_id": decoy_session, "created_at": support.now(),
            "instruction_encoding": "utf-8", "instruction_text": "a packet nobody sent"})
        self.assert_store_rejected_for(forged, "TURN_INSTRUCTION_MISSING")

    def test_probe_one_user_turn_cannot_open_two_agents_that_ran(self):
        harness = stub_harness("p13")
        chat_id = run_three_turns(harness, "Two agents, one turn")
        sessions = support.view(harness).sessions_of(chat_id)
        first_opener = sessions[0]["transitions"][0]["evidence"]["ref"]
        sessions[1]["transitions"][0]["evidence"]["ref"] = first_opener
        forged = support.view(harness)
        forged.put(sessions[1])
        self.assert_store_rejected_for(forged, "TURN_ALREADY_SERVED")

    def test_the_harness_never_opens_two_agents_from_one_turn(self):
        harness = stub_harness("p14")
        chat_id = run_three_turns(harness, "One turn, one agent")
        openers = [s["transitions"][0]["evidence"]["ref"]
                   for s in support.view(harness).sessions_of(chat_id)]
        self.assertEqual(len(openers), len(set(openers)))


# ---------------------------------------------------------------------------
# Delivery, and what a declaration does not buy.
# ---------------------------------------------------------------------------


class DeliveryIsBoundedByWhatWasDeclaredAndWhatHappened(unittest.TestCase, StoreCheck):

    def test_probe_a_delivery_on_a_fresh_binding_session_is_rejected(self):
        harness = stub_harness("p15")
        chat_id = harness.create_chat("Delivery not supported")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        forged = support.view(harness)
        forged.put({
            "record_type": "delivery_request", "record_version": 1,
            "delivery_id": "dlv_forbidden0001", "chat_id": chat_id,
            "session_id": session_id, "sequence": 1,
            "created_at": support.now(), "instruction_encoding": "utf-8",
            "instruction_text": "a turn this launcher cannot deliver",
            "acknowledged": True})
        self.assert_store_rejected_for(forged, "DELIVERY_NOT_SUPPORTED")

    def test_probe_a_delivery_after_the_agent_exited_is_rejected(self):
        harness = stub_harness("p16", continuation="persistent",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = harness.create_chat("Delivery after exit")
        harness.send_turn(chat_id, "hello")
        harness.stop_agent(chat_id, "the user said so")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "terminated")
        forged = support.view(harness)
        forged.put({
            "record_type": "delivery_request", "record_version": 1,
            "delivery_id": "dlv_toolate00001", "chat_id": chat_id,
            "session_id": session["session_id"], "sequence": 1,
            "created_at": support.now(), "instruction_encoding": "utf-8",
            "instruction_text": "are you still there", "acknowledged": True})
        self.assert_store_rejected_for(forged, "DELIVERY_AFTER_AGENT_EXIT")

    def test_no_escape_delivering_to_a_session_that_is_not_running(self):
        harness = stub_harness("p17", continuation="persistent",
                               response_shape="stream", launch_outcomes=["unknown"])
        chat_id = harness.create_chat("Deliver to unknown")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(support.view(harness).sessions_of(chat_id)[0]["state"], "unknown")
        with self.assertRaises(ConcurrentLaunchRefused) as caught:
            harness.send_turn(chat_id, "still there?")
        self.assertIn("rather than 'running'", caught.exception.reason)
        self.assertEqual(support.view(harness).all_of("delivery_request"), [])


# ---------------------------------------------------------------------------
# The instruction packet: no asserted bound, and no prior chat by default.
# ---------------------------------------------------------------------------


class NoPayloadBoundIsAsserted(unittest.TestCase, StoreCheck):

    def test_every_launcher_in_this_package_declares_no_measured_bound(self):
        for name, config in support.CONFIGURATIONS:
            launcher = support.harness(config)
            self.addCleanup(support.release, launcher)
            self.assertIsNone(launcher.capabilities.instruction_bound_bytes, name)

    def test_probe_no_source_file_carries_an_invented_bound(self):
        for module in (lb, session_manager, store_module, dev_local, scripted_stub,
                       atomic, contract, ids):
            source = inspect.getsource(module)
            code = re.sub(r'"""(?:.|\n)*?"""', "", source)
            code = "\n".join(line.split("#")[0] for line in code.splitlines())
            self.assertNotIn("65536", code, module.__name__)
            self.assertEqual(
                re.findall(r"\b(?:MAX_INSTRUCTION|INSTRUCTION_LIMIT|"
                           r"INSTRUCTION_BOUND|PAYLOAD_LIMIT)\w*", code),
                [], module.__name__)

    def test_the_mechanism_is_live_when_a_launcher_does_declare_one(self):
        harness = stub_harness("p18", instruction_bound_bytes=16)
        chat_id = harness.create_chat("Declared bound")
        with self.assertRaises(InstructionTooLarge) as caught:
            harness.send_turn(chat_id, "x" * 17)
        self.assertIn("16", caught.exception.reason)

    def test_probe_the_refused_packet_is_not_recorded_as_though_it_were_sent(self):
        harness = stub_harness("p19", instruction_bound_bytes=16)
        chat_id = harness.create_chat("Refused packet")
        before = len(support.view(harness).snapshot())
        with self.assertRaises(InstructionTooLarge):
            harness.send_turn(chat_id, "x" * 17)
        self.assertEqual(len(support.view(harness).snapshot()), before,
                         "nothing happened, so nothing may be recorded")
        self.assertEqual(support.view(harness).all_of("launch_request"), [])
        self.assertEqual(support.view(harness).messages(chat_id), [])
        harness.send_turn(chat_id, "short")
        self.assert_store_valid(harness.store, "declared-bound-respected")

    def test_the_bound_applies_identically_to_a_delivery(self):
        """The seam's guarantees are not weaker on turn four than on turn one."""
        harness = stub_harness("p20", continuation="persistent", response_shape="stream",
                               end_of_turn="turn_complete", instruction_bound_bytes=16)
        chat_id = harness.create_chat("Bound on delivery")
        harness.send_turn(chat_id, "short")
        before = len(support.view(harness).snapshot())
        with self.assertRaises(InstructionTooLarge):
            harness.send_turn(chat_id, "y" * 17)
        self.assertEqual(len(support.view(harness).snapshot()), before)
        self.assertEqual(support.view(harness).all_of("delivery_request"), [])


class TheFreshBindingPacketCarriesNoPriorChat(unittest.TestCase, StoreCheck):

    def test_each_launch_packet_holds_only_that_turn(self):
        launcher = ScriptedStubLauncher({})
        harness = support.harness({}, launcher=launcher)
        chat_id = run_three_turns(harness, "No history")
        packets = [r["instruction_text"] for r in support.view(harness).all_of("launch_request")]
        self.assertEqual(packets, list(support.THREE_TURNS))
        for i, packet in enumerate(packets):
            for earlier in support.THREE_TURNS[:i]:
                self.assertNotIn(earlier, packet,
                                 "packet composition under fresh_binding is deferred to "
                                 "internal dogfood and is explicitly not 'the whole "
                                 "prior chat by default'")

    def test_probe_the_check_would_notice_a_composer_that_carried_history(self):
        """Mutation. A composer that concatenates the prior chat must make the
        check above fail; otherwise the check proves nothing."""

        def carries_history(chat_id, user_text, store):
            prior = " ".join(m["content"]["text"] for m in store.read_messages(chat_id))
            return (prior + " " + user_text).strip()

        harness = support.harness({}, launcher=ScriptedStubLauncher({}),
                               compose=carries_history)
        chat_id = run_three_turns(harness, "History carried")
        packets = [r["instruction_text"] for r in support.view(harness).all_of("launch_request")]
        self.assertTrue(any(support.THREE_TURNS[0] in p for p in packets[1:]),
                        "the mutation did not actually carry history, so the probe "
                        "establishes nothing")
        self.assertNotEqual(packets, list(support.THREE_TURNS))


# ---------------------------------------------------------------------------
# Portability: what may and may not cross, and what nothing branches on.
# ---------------------------------------------------------------------------


class MechanicsDoNotCrossAndAreNotRead(unittest.TestCase, StoreCheck):

    def test_probe_mechanics_in_a_handle_change_nothing(self):
        transcripts = []
        for handle in ("opaque-1", "ssh://buildbox:22/pid/3319",
                       "/home/jtmrozi/scripts/launch_agent.sh#7"):
            class FixedHandle(ScriptedStubLauncher):
                def launch(self, instruction):
                    result = ScriptedStubLauncher.launch(self, instruction)
                    if result.outcome != "accepted":
                        return result
                    session = self._sessions[instruction.session_id]
                    session.handle = handle
                    return lb.LaunchResult("accepted", agent_handle=handle)

            harness = support.harness({}, launcher=FixedHandle({}))
            chat_id = harness.create_chat("Opaque handle")
            harness.send_turn(chat_id, "hello")
            transcripts.append(harness.transcript(chat_id))
            self.assert_store_valid(
                harness.store,
                "opaque-handle-%s" % re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-"))
        self.assertEqual(len(set(map(tuple, transcripts))), 1)

    def test_probe_mechanics_in_the_instruction_text_change_nothing(self):
        harness = stub_harness("p21")
        chat_id = harness.create_chat("Mechanics in text")
        text = "run ~/scripts/launch_agent.sh --host buildbox --pid 3319"
        harness.send_turn(chat_id, text)
        packet = support.view(harness).all_of("launch_request")[0]
        self.assertEqual(packet["instruction_text"], text)
        self.assertEqual(sorted(packet),
                         sorted(list(lb.LaunchInstruction.FIELDS)
                                + ["record_type", "record_version"]))
        self.assert_store_valid(harness.store, "mechanics-inside-a-value")

    def test_probe_no_record_the_harness_writes_carries_an_unlisted_field(self):
        harness = stub_harness("p22", continuation="persistent",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = run_three_turns(harness, "Closed records")
        support.end_chat(harness, chat_id)
        specs = support.VALIDATOR.RECORD_SPECS
        for record in support.view(harness).snapshot():
            allowed = set(specs[record["record_type"]]) | {"record_type", "record_version"}
            self.assertEqual(set(record) - allowed, set(), record["record_type"])


# ---------------------------------------------------------------------------
# Probes that found nothing. Kept, because an attack that failed is evidence.
# ---------------------------------------------------------------------------


class NoEscapeFound(unittest.TestCase, StoreCheck):

    def test_no_escape_abandoning_a_running_agent(self):
        harness = stub_harness("n1", continuation="persistent", response_shape="stream",
                               end_of_turn="turn_complete")
        chat_id = harness.create_chat("Abandon a running agent")
        harness.send_turn(chat_id, "hello")
        with self.assertRaises(NotPermitted):
            harness.abandon(chat_id)

    def test_no_escape_stopping_a_session_that_already_finished(self):
        harness = stub_harness("n2")
        chat_id = harness.create_chat("Stop a finished agent")
        harness.send_turn(chat_id, "hello")
        with self.assertRaises(NotPermitted):
            harness.stop_agent(chat_id, "too late")

    def test_no_escape_a_second_launch_while_a_launch_is_still_unresolved(self):
        harness = stub_harness("n3", launch_outcomes=["unknown"])
        chat_id = harness.create_chat("Unresolved launch")
        harness.send_turn(chat_id, "hello")
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "again")
        self.assertEqual(len(support.view(harness).sessions_of(chat_id)), 1)

    def test_no_escape_reattaching_twice_does_not_multiply_observations(self):
        harness = stub_harness("n4", continuation="persistent", response_shape="stream",
                               end_of_turn="turn_complete")
        chat_id = harness.create_chat("Reattach twice")
        harness.send_turn(chat_id, "hello")
        first = harness.reattach_on_start()
        second = harness.reattach_on_start()
        self.assertEqual(first, second)
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        kinds = [o["kind"] for o in support.view(harness).observations_of(session_id)]
        self.assertEqual(kinds, ["reattached", "reattached"])
        self.assert_store_valid(harness.store, "reattach-is-idempotent")

    def test_no_escape_an_archived_chat_does_not_release_the_rule(self):
        harness = stub_harness("n5", continuation="fresh_binding",
                               response_shape="stream", end_of_turn="turn_complete")
        chat_id = harness.create_chat("Archived")
        harness.send_turn(chat_id, "hello")
        harness.store.archive_chat(chat_id)
        self.assertEqual(harness.store.read_chat(chat_id)["state"], "archived")
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "again")

    def test_no_escape_a_second_launch_request_on_one_session(self):
        harness = stub_harness("n6")
        chat_id = harness.create_chat("Two packets, one session")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        forged = support.view(harness)
        forged.put({
            "record_type": "launch_request", "record_version": 1,
            "request_id": "req_second000001", "chat_id": chat_id,
            "session_id": session_id, "created_at": support.now(),
            "instruction_encoding": "utf-8", "instruction_text": "a retry in disguise"})
        self.assert_store_rejected_for(forged, "DUPLICATE_LAUNCH_REQUEST")

    def test_no_escape_a_launcher_cannot_forge_a_harness_observation(self):
        """A launcher that tries to source an event to the harness cannot build
        the payload at all, and the failure is classified rather than swallowed:
        the launch is `internal_error`, and no harness-sourced event reaches the
        store. What the harness itself observed stays a `session_observation`."""

        class Forger(ScriptedStubLauncher):
            def _produce_turn(self, session, instruction_text):
                lb.EventPayload(1, "harness", "recognized", b"{}",
                                interpreted_type="reattached")

        harness = support.harness({}, launcher=Forger({}))
        chat_id = harness.create_chat("Forgery")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.failure_category, "internal_error")
        self.assertEqual(
            [e for e in support.view(harness).all_of("diagnostic_event")
             if e["source"] == "harness"], [])
        self.assert_store_valid(harness.store, "harness-source-forgery-refused")

    def test_no_escape_a_replayed_page_does_not_duplicate_a_chat_message(self):
        harness = stub_harness("n7", continuation="persistent", response_shape="stream",
                               end_of_turn="turn_complete")
        chat_id = harness.create_chat("Replay")
        harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        harness._drain(session)
        harness._drain(session)
        self.assertEqual(len(harness.transcript(chat_id)), 2)
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "replayed-page-no-duplicate-message")

    def test_no_escape_the_store_refuses_a_duplicate_message_sequence(self):
        """#87 drove its store's `append_message` with a sequence already used.
        The converged store has no call that takes a sequence at all -- it
        allocates one by exclusive creation -- so the probe is put to the thing
        that allocates: a second writer of a taken message name is refused, and
        two stores over one root interleaving sends leave one contiguous
        history."""
        harness = stub_harness("n8")
        chat_id = harness.create_chat("Duplicate sequence")
        harness.send_turn(chat_id, "hello")
        import inspect as _inspect
        self.assertNotIn("sequence",
                         _inspect.signature(harness.store.append_user_message).parameters)
        taken = os.path.join(harness.store.chats_dir, chat_id, "messages", "00000001.json")
        with self.assertRaises(FileExistsError):
            atomic.create_exclusive(taken, b"{}")
        other = store_module.ChatStore(harness.store.root)
        for i in range(3):
            (harness.store if i % 2 else other).append_user_message(chat_id, "again %d" % i)
        self.assertEqual([m["sequence"] for m in harness.store.read_messages(chat_id)],
                         [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Late probes: things found by asking "what else could write a bad store?"
# ---------------------------------------------------------------------------


class TheHarnessRefusesToWriteAnUnreadableHistory(unittest.TestCase, StoreCheck):

    def test_probe_a_launcher_that_skips_a_sequence_is_refused(self):
        """Found by attacking the store rather than the seam: nothing in the
        contract stops a launcher from numbering its payloads 1, 3. The harness
        preserves what it is given, so without this the launcher's counting bug
        becomes a store whose ordered history cannot be read back."""

        class Skips(ScriptedStubLauncher):
            def _emit(self, session, *args, **kwargs):
                payload = ScriptedStubLauncher._emit(self, session, *args, **kwargs)
                session.next_sequence += 1
                return payload

        harness = support.harness({}, launcher=Skips({}))
        chat_id = harness.create_chat("Skipped sequence")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            harness.send_turn(chat_id, "hello")
        self.assertIn("contiguous", str(caught.exception))
        self.assertEqual(len(support.view(harness).all_of("diagnostic_event")), 1,
                         "the payload before the gap is kept; the gap is refused")

    def test_probe_a_store_with_a_gap_is_rejected_by_the_contract_too(self):
        harness = stub_harness("p23")
        chat_id = harness.create_chat("Gap")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        forged = support.view(harness)
        forged.put({
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": "evt_gapped00000a", "chat_id": chat_id,
            "session_id": session_id,
            "sequence": len(support.view(harness).events_of(session_id)) + 3,
            "received_at": support.now(), "source": "launcher",
            "interpretation": "unrecognized", "interpreted_type": None,
            "raw": {"encoding": "utf-8", "body": "{}"}})
        self.assert_store_rejected_for(forged, "SEQUENCE_GAP")

    def test_probe_an_empty_turn_is_refused_before_anything_is_written(self):
        """Found by probing the `pending -> launch_failed` path. An empty turn
        used to be recorded as a `message` whose content the contract rejects:
        contract 4.2 gives a message no way to say the user sent nothing, so a
        store containing one cannot be read back."""
        harness = stub_harness("p24")
        chat_id = harness.create_chat("Empty turn")
        before = len(support.view(harness).snapshot())
        with self.assertRaises(NotPermitted) as caught:
            harness.send_turn(chat_id, "")
        self.assertIn("empty turn is refused", caught.exception.reason)
        self.assertEqual(len(support.view(harness).snapshot()), before)
        self.assertEqual(support.view(harness).messages(chat_id), [])
        self.assertEqual(support.view(harness).sessions_of(chat_id), [])
        harness.send_turn(chat_id, "hello")
        self.assert_store_valid(harness.store, "empty-turn-refused")

    def test_probe_a_packet_the_seam_refuses_still_leaves_a_durable_record(self):
        """Contract 4.3: a launch failure must be attributable to a durable
        record rather than being lost -- including when nothing was ever sent.

        **Expectation changed by review finding R4** (decision 0003's invariant,
        widened). This probe used to compose an empty packet and expect the
        user's turn to be recorded and its session carried to `launch_failed`.
        The loop now puts the would-be session and packet to the store's own
        checks *before* the turn is written, so an unformable packet is refused
        with nothing recorded at all: no turn that was never offered, and no
        launch to attribute. The durable-record half of the probe is kept for the
        one route that still reaches it -- a packet refused *after* the turn and
        its session were written, which a clock step or an I/O error can cause --
        by injecting that refusal."""
        from dory_wrangler.errors import ValidationRefused

        def composes_nothing(chat_id, user_text, store):
            return ""

        harness = support.harness({}, launcher=ScriptedStubLauncher({}),
                               compose=composes_nothing)
        chat_id = harness.create_chat("Unformable packet")
        before = harness.store.export_records()
        with self.assertRaises(ValidationRefused):
            harness.send_turn(chat_id, "hello")
        self.assertEqual(harness.store.export_records(), before,
                         "a turn whose packet cannot be formed was recorded")

        harness = support.harness({}, launcher=ScriptedStubLauncher({}))
        chat_id = harness.create_chat("Packet refused after the turn was written")

        def refuses(*_args, **_kwargs):
            raise ValidationRefused("the packet write was refused")

        harness.store.append_launch_request = refuses
        with self.assertRaises(NotPermitted):
            harness.send_turn(chat_id, "hello")
        del harness.store.append_launch_request
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "launch_failed")
        self.assertEqual(session["transitions"][-1]["owner"], "harness",
                         "nothing was launched, so no launcher authorised anything")
        self.assertIsNone(session["transitions"][-1]["evidence"]["ref"])
        self.assertEqual(support.view(harness).all_of("launch_request"), [],
                         "a packet that was never formed must not be recorded")
        self.assertEqual(support.view(harness).open_bindings(chat_id), [])
        self.assert_store_valid(harness.store, "packet-refused-at-the-seam")

    def test_probe_a_launcher_cannot_attribute_a_payload_to_another_session(self):
        """It cannot try: payloads carry no session id, so the harness attributes
        every payload to the session it asked about. Whether the *launcher* can
        confuse two agents internally is assumption A7 and is not settleable from
        this VM; it is recorded for #90 rather than claimed here."""
        harness = stub_harness("p25")
        chat_id = run_three_turns(harness, "Attribution")
        for event in support.view(harness).all_of("diagnostic_event"):
            session = support.view(harness).get("agent_session", event["session_id"])
            self.assertEqual(event["chat_id"], session["chat_id"])
        self.assert_store_valid(harness.store, "payload-attribution")


class ALauncherThatCannotResumeIsRefusedRatherThanSpunOn(unittest.TestCase, StoreCheck):
    """Review finding F1, and the shape it hid behind.

    `events` is resumable **by sequence** (contract 6.1): the caller resumes by
    passing the last sequence it stored. A launcher that ignores `after_sequence`
    and replays its whole page therefore answers every read with the same page.
    `_drain` used to fail closed on a sequence *gap* but not on a non-advancing
    *replay*, so it looped: review measured 2001 `events` calls in under twenty
    seconds, the chat lock held throughout and nothing durable written -- a hung
    UI at full CPU rather than a stated refusal.

    The existing probe, `test_replaying_the_whole_stream_stores_nothing_twice`,
    is green and always would have been: it tests replay for *duplication*
    against a launcher that does honour `after_sequence`. Duplication and
    termination are different guarantees, and the second was checked at the
    label of the first.

    Whether the internal bridge can resume at all is **unknown**
    (facts-and-assumptions C5), which is why this is the likeliest internal
    failure mode rather than a curiosity.

    Nothing here measures a duration or compares a threshold. The refusal is
    about a fact the launcher supplied -- this page advanced nothing -- and the
    tests below bound the number of *calls*, never the time they take.
    """

    class _Replaying(lb.LaunchBoundary):
        """Correct in every respect but one: it ignores `after_sequence`."""

        launcher_id = "replaying"

        def __init__(self, terminal=False):
            self._terminal = terminal
            self._payloads = {}
            self.events_calls = 0

        @property
        def capabilities(self):
            return lb.LauncherCapabilities("fresh_binding", "one_shot", None)

        def launch(self, instruction):
            import json
            reply = "answer to: %s" % instruction.instruction_text.strip()
            payloads = [lb.EventPayload(
                1, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                json.dumps({"type": "assistant_text", "text": reply}).encode("utf-8"),
                interpreted_type=lb.PAYLOAD_ASSISTANT_TEXT, text=reply)]
            if self._terminal:
                payloads.append(lb.EventPayload(
                    2, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                    json.dumps({"type": "session_completed"}).encode("utf-8"),
                    interpreted_type=lb.PAYLOAD_SESSION_COMPLETED))
            self._payloads["replay-agent-1"] = payloads
            return lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle="replay-agent-1")

        def events(self, agent_handle, after_sequence):
            self.events_calls += 1
            if self.events_calls > 50:
                raise AssertionError(
                    "the drain did not terminate: %d events calls" % self.events_calls)
            return lb.EventsPage(list(self._payloads[agent_handle]))

        def stop(self, agent_handle, reason):
            return lb.StopAck(True)

    def test_probe_a_launcher_that_ignores_after_sequence_is_refused(self):
        launcher = self._Replaying()
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("A launcher that cannot resume")
        with self.assertRaises(lb.LaunchBoundaryError) as caught:
            harness.send_turn(chat_id, "hello")
        self.assertIn("ignored after_sequence", str(caught.exception))

    def test_the_refusal_is_immediate_and_not_a_slower_spin(self):
        """The property that matters is termination, so it is asserted over the
        number of calls the launcher received rather than over elapsed time. A
        fix that merely made the loop slower would pass a wall-clock test."""
        launcher = self._Replaying()
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Bounded")
        with self.assertRaises(lb.LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        self.assertEqual(launcher.events_calls, 2,
                         "one read that advanced, one that did not, then the "
                         "refusal -- there is nothing further to ask")

    def test_the_chat_is_usable_afterwards_rather_than_locked(self):
        """What made F1 severe was not the loop but the held lock: nothing else
        could reach the chat. A refusal must leave the lock released."""
        launcher = self._Replaying()
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Still usable")
        with self.assertRaises(lb.LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running",
                         "the refusal says nothing about whether the agent is alive")
        # The lock is free, so the user's own exits still work.
        harness.stop_agent(chat_id, "the launcher cannot resume")
        self.assertIn(support.view(harness).get("agent_session", session["session_id"])["state"],
                      ("terminated", "unknown"))
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "replay-refused-chat-still-usable")

    def test_a_replaying_launcher_that_does_reach_a_terminal_event_is_not_refused(self):
        """The control. The refusal must be about non-advancement, not about
        replay: a launcher that replays but whose first page ends the session
        never asks a second time, and must be served normally."""
        launcher = self._Replaying(terminal=True)
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Replay that terminates")
        harness.send_turn(chat_id, "hello")
        self.assertEqual(launcher.events_calls, 1)
        self.assertEqual(support.view(harness).sessions_of(chat_id)[0]["state"], "completed")
        self.assert_store_valid(harness.store, "replay-that-terminates-is-served")

    def test_a_launcher_that_honours_after_sequence_is_untouched(self):
        """The other control: the ordinary path must not have been narrowed."""
        harness = stub_harness("f1c", continuation="persistent",
                               response_shape="stream")
        chat_id = run_three_turns(harness, "Resumable, as specified")
        self.assertEqual(len(harness.transcript(chat_id)), 6)
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "after-sequence-honoured-control")
