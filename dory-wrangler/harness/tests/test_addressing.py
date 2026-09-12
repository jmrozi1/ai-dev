"""The handle is the address, and a launcher remembers nothing between calls.

Contract 4.3 and 6.1, as corrected at `4ff8b63`. `stop`, `events` and `deliver`
each take the `agent_handle` the launcher returned from its own `launch`, and
nothing else on the seam names an agent. `session_id` is the harness's identity
for a run: it travels inside the instruction packets as correlation and is never
an address.

Two kinds of evidence are needed here and neither substitutes for the other.

* The **signature** is an interface property, so no store can show it. It is
  checked directly, by reading what the launcher was actually handed.
* The **fact the signature exists to guarantee** -- that the harness only ever
  addressed an agent whose handle it had durably recorded -- is checked against
  the store by the validator, and is enforced a second time by the harness
  itself before the record is written.

Every positive test here is paired with a mutation that makes its claim false,
because a test that reads an address log would pass just as happily against a
harness that passed the session id if it never compared the two.
"""

from __future__ import annotations

import inspect
import unittest

import support
from support import StoreCheck, VALIDATOR, codes, end_chat, expected_transcript, \
    run_three_turns

import launch_boundary as lb
import session_manager
from app import open_harness
from errors import NotPermitted
from launchers.scripted_stub import ScriptedStubLauncher


class TheSignatureTakesTheHandle(unittest.TestCase):
    """Read off the interface itself, not off the prose that describes it."""

    def test_the_three_addressing_operations_name_the_handle(self):
        for name in ("stop", "events", "deliver"):
            parameters = list(
                inspect.signature(getattr(lb.LaunchBoundary, name)).parameters)
            self.assertEqual(parameters[1], "agent_handle",
                             "%s's address parameter is %r" % (name, parameters[1]))
            self.assertNotIn("session_id", parameters,
                             "%s still takes a session_id" % name)

    def test_launch_is_the_only_operation_that_sees_a_session_id(self):
        """`session_id` is correlation inside the packet, never an address."""
        self.assertIn("session_id", lb.LaunchInstruction.FIELDS)
        self.assertIn("session_id", lb.DeliveryInstruction.FIELDS)


class TheHandleIsWhatIsActuallyPassed(unittest.TestCase, StoreCheck):
    """The check the contract explicitly leaves to #87: read the call, not the
    record. A launcher that logs every address it was handed makes this a fact."""

    def _addresses(self, harness, chat_id):
        return harness._boundary.addressed

    def test_every_address_is_the_handle_the_store_recorded(self):
        for continuation, shape, expected in (
                ("fresh_binding", "one_shot", {"events"}),
                ("persistent", "stream", {"events", "deliver", "stop"})):
            with self.subTest(continuation=continuation):
                launcher = ScriptedStubLauncher({"continuation": continuation,
                                                 "response_shape": shape})
                harness = open_harness({}, launcher=launcher)
                chat_id = run_three_turns(harness, "Addressing")
                end_chat(harness, chat_id)

                handles = set(s.get("agent_handle")
                              for s in harness.store.sessions_of(chat_id))
                session_ids = set(s["session_id"]
                                  for s in harness.store.sessions_of(chat_id))
                addressed = [address for _, address in launcher.addressed]

                self.assertTrue(addressed, "nothing addressed the agent at all")
                self.assertTrue(handles.isdisjoint(session_ids),
                                "the fixture cannot distinguish the two if a "
                                "handle ever equals a session id")
                for address in addressed:
                    self.assertIn(address, handles,
                                  "the launcher was handed %r, which is not a "
                                  "handle it issued" % (address,))
                    self.assertNotIn(address, session_ids)
                # Not merely "no session id got through": the claim is stated
                # over the operations this configuration actually issues, and a
                # configuration that stopped issuing one would fail here rather
                # than pass vacuously.
                self.assertEqual(
                    set(operation for operation, _ in launcher.addressed), expected)
                self.assert_store_valid(
                    harness.store, "addressing-by-handle-%s" % continuation)

    def test_the_check_fails_against_a_harness_that_passes_the_session_id(self):
        """The mutation. Without this, the test above would pass unchanged
        against the pre-`4ff8b63` harness, which is how F4 happened."""

        class AddressesBySessionId(session_manager.SessionManager):
            def _agent_handle(self, session):
                return session["session_id"]

        launcher = ScriptedStubLauncher({})
        harness = AddressesBySessionId(
            session_manager.Store(None), launcher)
        chat_id = harness.create_chat("Wrong address")
        harness.send_turn(chat_id, "hello")

        handles = set(s.get("agent_handle") for s in harness.store.sessions_of(chat_id)
                      if s.get("agent_handle"))
        addressed = [address for _, address in launcher.addressed]
        self.assertTrue(addressed)
        self.assertFalse(set(addressed) <= handles,
                         "the comparison cannot tell a session id from a handle, "
                         "so it proves nothing")


class ALauncherMustRememberNothingBetweenCalls(unittest.TestCase, StoreCheck):
    """Contract 6.1's normative consequence 2, exercised across a restart.

    The reopened launcher is a different object with no memory of the first. All
    it is given is the handle the store recorded, and that has to be enough.
    """

    def setUp(self):
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="dory-addressing-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.path = os.path.join(directory, "records.jsonl")

    def test_a_restart_re_attaches_through_the_stored_handle_alone(self):
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream"}},
            "a1", store_path=self.path)
        chat_id = harness.create_chat("Restart by handle")
        harness.send_turn(chat_id, "are you there?")
        live = harness.store.non_terminal_sessions(chat_id)[0]
        handle, session_id = live["agent_handle"], live["session_id"]

        # A launcher that knows this handle and has never seen this session id.
        reopened = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "resume_handles": [handle]}},
            "a2", store_path=self.path, start=support.RESTARTED)
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "running")])
        self.assertEqual([kind for kind in
                          (o["kind"] for o in reopened.store.observations_of(session_id))],
                         ["reattached"])
        self.assertEqual([address for _, address in reopened._boundary.addressed],
                         [handle], "re-attachment passed something other than the handle")
        end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "restart-addressed-by-handle")

    def test_a_launcher_that_knows_only_the_session_id_cannot_resume(self):
        """The mirror. A launcher holding the *session id* is no use, which is
        what makes the handle load-bearing rather than decorative."""
        harness = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream"}},
            "a3", store_path=self.path)
        chat_id = harness.create_chat("Restart by session id")
        harness.send_turn(chat_id, "are you there?")
        session_id = harness.store.non_terminal_sessions(chat_id)[0]["session_id"]

        reopened = support.deterministic(
            {"launcher": "scripted-stub",
             "options": {"continuation": "persistent", "response_shape": "stream",
                         "resume_handles": [session_id]}},
            "a4", store_path=self.path, start=support.RESTARTED)
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "restart-session-id-is-not-an-address")


class NothingIsAddressedWithoutAHandle(unittest.TestCase, StoreCheck):
    """Contract 4.3's two new rules, enforced by the harness before the record
    exists rather than only by a validator run afterwards."""

    def _unknown_launch(self, salt):
        harness = support.deterministic(
            {"launcher": "scripted-stub", "options": {"launch_outcomes": ["unknown"]}},
            salt)
        chat_id = harness.create_chat("No handle was ever issued")
        harness.send_turn(chat_id, "hello")
        session = harness.store.sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "unknown")
        self.assertIsNone(session.get("agent_handle"),
                          "an `unknown` outcome carries no handle (contract 6.3)")
        return harness, chat_id, session

    def test_stop_is_refused_on_a_session_that_never_got_a_handle(self):
        harness, chat_id, session = self._unknown_launch("n1")
        with self.assertRaises(NotPermitted) as caught:
            harness.stop_agent(chat_id, "the user pressed Stop")
        self.assertIn("never received an agent_handle", str(caught.exception))
        self.assertEqual(harness.store.observations_of(session["session_id"]), [],
                         "a refused stop must leave no observation claiming the "
                         "agent was addressed")
        # The user is not stuck: 5.4's exit needs no handle.
        harness.abandon(chat_id)
        self.assertEqual(
            harness.store.get("agent_session", session["session_id"])["state"],
            "abandoned")
        self.assert_store_valid(harness.store, "no-handle-stop-refused-then-abandoned")

    def test_a_restart_records_reattach_failed_without_calling_the_launcher(self):
        """`reattach_failed` is the one addressing-adjacent kind that needs no
        handle, because the attempt fails without being made (contract 5.4)."""
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="dory-nohandle-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "records.jsonl")

        harness = support.deterministic(
            {"launcher": "scripted-stub", "options": {"launch_outcomes": ["unknown"]}},
            "n2", store_path=path)
        chat_id = harness.create_chat("Unknown, then a restart")
        harness.send_turn(chat_id, "hello")
        session_id = harness.store.sessions_of(chat_id)[0]["session_id"]

        reopened = support.deterministic({"launcher": "scripted-stub"}, "n3",
                                         store_path=path, start=support.RESTARTED)
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual([address for _, address in reopened._boundary.addressed], [],
                         "the launcher must not have been called at all: there was "
                         "nothing to pass it")
        kinds = [o["kind"] for o in reopened.store.observations_of(session_id)]
        self.assertEqual(kinds, ["reattach_failed"])
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "no-handle-restart-reattach-failed")

    def test_the_harness_refuses_to_write_an_addressing_observation_with_no_handle(self):
        """The guard itself, reached directly. Everything above routes around it;
        this requires it to exist."""
        harness, chat_id, session = self._unknown_launch("n4")
        for kind in sorted(session_manager.ADDRESSING_OBSERVATION_KINDS):
            with self.subTest(kind=kind):
                with self.assertRaises(lb.LaunchBoundaryError):
                    harness._record_observation(session, kind, "forged")
        # ... and the exempt kind is genuinely exempt.
        harness._record_observation(session, "reattach_failed", "honest")
        self.assertEqual([o["kind"] for o in
                          harness.store.observations_of(session["session_id"])],
                         ["reattach_failed"])

    def test_a_store_that_gets_past_the_harness_is_still_refused_by_the_contract(self):
        """Defence at rest, so the guarantee does not rest on this harness being
        the only writer."""
        harness, chat_id, session = self._unknown_launch("n5")
        harness.store.put({
            "record_type": "session_observation", "record_version": 1,
            "observation_id": "obs_forgedstop01", "chat_id": chat_id,
            "session_id": session["session_id"],
            "observed_at": harness._clock.now(),
            "kind": "stop_confirmed", "detail": "forged past the harness"})
        self.assert_store_rejected_for(harness.store, "ADDRESSED_WITHOUT_HANDLE")


class DriftGuardsForTheAddressingRules(unittest.TestCase, StoreCheck):
    """Two copies of one rule is the divergence this contract exists to prevent.

    The two new violation codes are not exported as symbols by the validator, so
    guarding them by name would be checking a label. These build the store shape
    each code is about and require the executable contract to emit it.
    """

    def test_the_addressing_observation_kinds_match_the_validators(self):
        self.assertEqual(set(session_manager.ADDRESSING_OBSERVATION_KINDS),
                         set(VALIDATOR.ADDRESSING_OBSERVATION_KINDS))

    def test_reattach_failed_is_exempt_in_both_copies(self):
        self.assertNotIn("reattach_failed", session_manager.ADDRESSING_OBSERVATION_KINDS)
        self.assertNotIn("reattach_failed", VALIDATOR.ADDRESSING_OBSERVATION_KINDS)
        self.assertIn("reattach_failed", VALIDATOR.OBSERVATION_KINDS)

    def test_every_addressing_kind_is_one_the_validator_recognises(self):
        for kind in session_manager.ADDRESSING_OBSERVATION_KINDS:
            self.assertIn(kind, VALIDATOR.OBSERVATION_KINDS)

    def _base_store(self, handle, issued_at, observed_at):
        return [
            {"record_type": "chat", "record_version": 1, "chat_id": "cht_aaaaaaaa",
             "title": "Addressing", "created_at": "2026-09-12T10:00:00Z",
             "updated_at": "2026-09-12T10:00:09Z"},
            {"record_type": "agent_session", "record_version": 1,
             "session_id": "ses_aaaaaaaa", "chat_id": "cht_aaaaaaaa",
             "created_at": "2026-09-12T10:00:01Z", "state": "unknown",
             "launcher_id": "scripted-stub", "agent_handle": handle,
             "launcher_capabilities": {"continuation": "fresh_binding",
                                       "response_shape": "one_shot",
                                       "instruction_bound_bytes": None},
             "opening_message_id": None, "transitions": []},
            {"record_type": "launch_result", "record_version": 1,
             "request_id": "req_aaaaaaaa", "session_id": "ses_aaaaaaaa",
             "observed_at": issued_at, "outcome": "accepted",
             "agent_handle": handle, "failure_category": None, "detail": None},
            {"record_type": "session_observation", "record_version": 1,
             "observation_id": "obs_aaaaaaaa", "chat_id": "cht_aaaaaaaa",
             "session_id": "ses_aaaaaaaa", "observed_at": observed_at,
             "kind": "stop_unconfirmed", "detail": "addressed"},
        ]

    def test_the_validator_emits_addressed_without_handle(self):
        records = self._base_store(None, "2026-09-12T10:00:02Z", "2026-09-12T10:00:05Z")
        self.assertIn("ADDRESSED_WITHOUT_HANDLE", [c for c, _ in support.violations(records)])

    def test_the_validator_emits_addressed_before_handle_issued(self):
        records = self._base_store("agent-1", "2026-09-12T10:00:08Z",
                                   "2026-09-12T10:00:05Z")
        self.assertIn("ADDRESSED_BEFORE_HANDLE_ISSUED",
                      [c for c, _ in support.violations(records)])

    def test_the_same_store_with_the_handle_in_hand_emits_neither(self):
        """The control: both codes above must be about the addressing rule and
        not about some unrelated defect in the constructed store."""
        records = self._base_store("agent-1", "2026-09-12T10:00:02Z",
                                   "2026-09-12T10:00:05Z")
        found = [c for c, _ in support.violations(records)]
        self.assertNotIn("ADDRESSED_WITHOUT_HANDLE", found)
        self.assertNotIn("ADDRESSED_BEFORE_HANDLE_ISSUED", found)


if __name__ == "__main__":
    unittest.main()
