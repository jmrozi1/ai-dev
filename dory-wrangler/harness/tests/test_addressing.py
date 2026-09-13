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
from errors import ConcurrentLaunchRefused, NotPermitted
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


class ProcessDeath(BaseException):
    """A process ending. Nothing in the harness catches it, and every durable
    write already made is already on disk -- which is the whole point: the shape
    under test is one only a crash can produce."""


class ASessionInterruptedInLaunchingAlwaysHasAnExit(unittest.TestCase, StoreCheck):
    """Re-review N1, and the enumeration that finds defects of its kind.

    `_launch_turn` persists the handle and the `running` transition as two
    durable writes, so a process death between them leaves a session `launching`
    **with** a handle. Generalising the restart path from the two state names to
    the handle put that shape in no branch: it passed the handle guard, matched
    neither outcome gate, and `return session["state"]` left it `launching` on
    every subsequent restart, with stop, abandon and every turn refused -- a chat
    dead for the life of the store.

    `launching` is not a fact about the handle. It means the launch was issued
    and its outcome was never written as a state, and contract 5.2 names the
    launcher's own `launch_result` as the precondition for every exit out of it
    but one. This class enumerates every way a restart can find a session in
    `launching` and requires each to reach a state a user can act on, with a
    store the contract accepts.
    """

    def setUp(self):
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="dory-launching-")
        self.addCleanup(shutil.rmtree, directory, True)
        self.path = os.path.join(directory, "records.jsonl")

    STUB = {"launcher": "scripted-stub",
            "options": {"continuation": "fresh_binding", "response_shape": "one_shot"}}

    def _die_in_the_window(self, salt, options=None, at="running", chat_id=None):
        """Crash where the shape is actually made, rather than hand-building the
        records. `at` names the transition out of `launching` the process does not
        survive, or `"in_launch"` for a death inside the launcher call itself,
        before its report was written."""
        options = dict(options or {})
        if at == "in_launch":
            def die_inside(instruction):
                raise ProcessDeath("the process ended inside the launcher call")
            options["on_launch"] = die_inside
        config = {"launcher": "scripted-stub",
                  "options": dict(self.STUB["options"], **options)}
        harness = support.deterministic(config, salt, store_path=self.path)
        real = session_manager.SessionManager._transition

        def die(manager, session, to, owner, kind, ref):
            if session["state"] == "launching" and to == at:
                raise ProcessDeath("the process ended before the %s transition" % at)
            return real(manager, session, to, owner, kind, ref)

        if at != "in_launch":
            session_manager.SessionManager._transition = die
            self.addCleanup(setattr, session_manager.SessionManager, "_transition", real)
        if chat_id is None:
            chat_id = harness.create_chat("Interrupted in launching")
        try:
            harness.send_turn(chat_id, "hello")
        except ProcessDeath:
            pass
        else:
            self.fail("the crash never happened, so nothing was interrupted")
        session_manager.SessionManager._transition = real
        support.release(harness)
        session = harness.store.non_terminal_sessions(chat_id)[0]
        self.assertEqual(session["state"], "launching",
                         "the crash must leave the session mid-launch")
        return chat_id, session["session_id"], session.get("agent_handle")

    def _leaving_launching(self, harness, session_id):
        """The evidence cited by the transition that left `launching` -- which is
        the one this repair decides, and not necessarily the last one."""
        session = harness.store.get("agent_session", session_id)
        for transition in session["transitions"]:
            if transition["from"] == "launching":
                return transition["evidence"]
        self.fail("the session never left `launching`")

    def _restart(self, salt, options=None, start=support.RESTARTED):
        config = {"launcher": "scripted-stub",
                  "options": dict(self.STUB["options"], **(options or {}))}
        reopened = support.deterministic(config, salt, store_path=self.path,
                                         start=start)
        self.addCleanup(support.release, reopened)
        return reopened

    # -- the defect itself -------------------------------------------------

    def test_the_bricked_chat_is_gone_and_the_user_can_act(self):
        """The reproduction, run forwards. An ordinary stateless launcher, the
        one shape contract 6.1 mandates, and two restarts."""
        chat_id, session_id, handle = self._die_in_the_window("l1")
        self.assertTrue(handle, "the handle reached the store before the crash")

        first = self._restart("l2")
        self.assertEqual(first.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual(first.store.get("agent_session", session_id)["state"],
                         "unknown",
                         "a session no branch transitions is a chat with no exit")
        # And `unknown`'s exit works, on the first restart, without a second one.
        first.abandon(chat_id)
        self.assertEqual(first.store.get("agent_session", session_id)["state"],
                         "abandoned")
        self.assertEqual(first.store.open_bindings(chat_id), [])
        outcome = first.send_turn(chat_id, "are you there?")
        self.assertEqual(outcome.session_state, "completed",
                         "the chat is usable again, which is what 'has an exit' "
                         "means")
        self.assert_store_valid(
            first.store, "launching-interrupted-then-abandoned",
            "A process death between persisting the agent handle and the "
            "`running` transition, then a restart on an ordinary stateless "
            "launcher. The interrupted launch outcome is finished from the "
            "launcher's own launch_result, the re-attachment then fails as it "
            "must, and the user's abandon frees the chat.")

    def test_a_second_restart_finds_nothing_left_to_re_attach_to(self):
        """The defect was permanent across restarts, so the closure is checked
        across restarts too."""
        chat_id, session_id, _ = self._die_in_the_window("l3")
        self._restart("l4").reattach_on_start()
        second = self._restart("l5", start="2026-09-13T12:00:00.000000Z")
        self.assertEqual(second.reattach_on_start(), [(session_id, "unknown")])
        second.abandon(chat_id)
        third = self._restart("l6", start="2026-09-14T12:00:00.000000Z")
        self.assertEqual(third.reattach_on_start(), [],
                         "an abandoned session is terminal and is not re-attached")
        self.assert_store_valid(third.store, "launching-interrupted-two-restarts")

    def test_the_old_shape_bricks_the_chat_and_is_rejected_by_the_contract(self):
        """The negative control. Restore the routing this rail removed -- fall
        through to the handle-keyed branches with the state untouched -- and the
        session is stuck, every user action is refused, and the store does not
        even validate."""
        chat_id, session_id, _ = self._die_in_the_window("l7")
        reopened = self._restart("l8")
        reopened._resolve_interrupted_launch = lambda session: session["state"]

        for _ in range(2):
            self.assertEqual(reopened.reattach_on_start(), [(session_id, "launching")])
            self.assertEqual(reopened.store.get("agent_session", session_id)["state"],
                             "launching")
        for action in (lambda: reopened.stop_agent(chat_id, "the user pressed Stop"),
                       lambda: reopened.abandon(chat_id),
                       lambda: reopened.send_turn(chat_id, "are you there?")):
            with self.assertRaises((NotPermitted, ConcurrentLaunchRefused)):
                action()
        self.assertIn("LAUNCH_OUTCOME_MISMATCH", codes(reopened.store.snapshot()),
                      "an accepted launch whose session never entered `running` is "
                      "not merely a stuck chat, it is a store the contract rejects")

    def test_widening_the_two_outcome_gates_is_not_the_fix(self):
        """The other candidate, measured rather than argued about: admit
        `launching` into the two gates keyed on the outcome of `events`.

        It restores an exit and produces a store the contract rejects either way.
        `launching -> running` does not admit an `observation` at all, and on the
        failure side an accepted launch whose session never entered `running` is
        a `LAUNCH_OUTCOME_MISMATCH`. This is why the repair reads the
        `launch_result` instead.
        """
        chat_id, session_id, handle = self._die_in_the_window("l9")

        def widened(manager, session):
            """`_reattach` with the two gates widened and nothing else changed."""
            observation_id = None
            try:
                manager._boundary.events(
                    session["agent_handle"],
                    manager.store.last_event_sequence(session_id))
            except lb.LauncherError as exc:
                observation_id = manager._record_observation(
                    session, "reattach_failed", "%s: %s" % (exc.category, exc.detail))
                manager._transition(session, "unknown", "launcher", "observation",
                                    observation_id)
                return
            observation_id = manager._record_observation(session, "reattached", None)
            manager._transition(session, "running", "launcher", "observation",
                                observation_id)

        stateless = self._restart("l10")
        widened(stateless, stateless.store.get("agent_session", session_id))
        self.assertEqual(stateless.store.get("agent_session", session_id)["state"],
                         "unknown")
        self.assertIn("LAUNCH_OUTCOME_MISMATCH", codes(stateless.store.snapshot()))

        resumable = self._restart("l11", options={"resume_handles": [handle]})
        session = resumable.store.get("agent_session", session_id)
        session["state"] = "launching"
        session["transitions"] = [t for t in session["transitions"]
                                  if t["to"] != "unknown"]
        resumable.store.put(session)
        widened(resumable, resumable.store.get("agent_session", session_id))
        self.assertEqual(resumable.store.get("agent_session", session_id)["state"],
                         "running")
        self.assertIn("PRECONDITION_NOT_MET", codes(resumable.store.snapshot()))

    # -- every input the restart path can be handed in `launching` ---------

    def test_a_launcher_that_can_resume_reaches_running_with_a_valid_store(self):
        """The shape a launcher with durable state produces, which the widening
        candidate above turns into an invalid store."""
        chat_id, session_id, handle = self._die_in_the_window("l12")
        reopened = self._restart("l13", options={"resume_handles": [handle]})
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "running")])
        self.assertEqual([o["kind"] for o in reopened.store.observations_of(session_id)],
                         ["reattached"])
        self.assertEqual([address for _, address in reopened._boundary.addressed],
                         [handle], "the re-attachment addressed something other "
                                   "than the recorded handle")
        end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "launching-interrupted-then-resumed")

    def test_no_launch_result_at_all_is_unknown_evidenced_by_the_failed_attempt(self):
        """The death before the launcher's report was written -- which is also
        `launching` without a handle. Nothing can say how the launch ended and
        nothing ever will, and `reattach_failed` is the one observation kind that
        needs no handle."""
        chat_id, session_id, handle = self._die_in_the_window("l14", at="in_launch")
        self.assertIsNone(handle)
        self.assertEqual(self._restart("l15").store.launch_result_of(session_id), None)

        reopened = self._restart("l16")
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual([o["kind"] for o in reopened.store.observations_of(session_id)],
                         ["reattach_failed"])
        self.assertEqual([address for _, address in reopened._boundary.addressed], [],
                         "there was nothing to pass, so the launcher must not have "
                         "been called")
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "launching-interrupted-before-the-result")

    def test_a_failed_launch_result_reaches_launch_failed_and_frees_the_chat(self):
        """A death after the launcher reported a failure. `launch_failed` is
        terminal, the binding is released, and the chat needs no abandon at all
        -- and the store the old routing produced here was rejected too."""
        chat_id, session_id, _ = self._die_in_the_window(
            "l17", options={"launch_outcomes": ["unavailable"]}, at="launch_failed")
        reopened = self._restart("l18")
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "launch_failed")])
        self.assertEqual(reopened.store.open_bindings(chat_id), [],
                         "a terminal session releases its binding")
        self.assertEqual(reopened.store.observations_of(session_id), [],
                         "nothing was re-attached to, so nothing is recorded as if "
                         "it had been")
        self.assertEqual(reopened.send_turn(chat_id, "try again").session_state, "completed")
        self.assert_store_valid(reopened.store, "launching-interrupted-after-a-failure")

    def test_an_unknown_launch_result_reaches_unknown_and_the_user_abandons(self):
        chat_id, session_id, _ = self._die_in_the_window(
            "l19", options={"launch_outcomes": ["unknown"]}, at="unknown")
        reopened = self._restart("l20")
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual([address for _, address in reopened._boundary.addressed], [],
                         "an `unknown` outcome issued no handle, so there is "
                         "nothing to address")
        # The launcher already said how this launch ended, and that report is
        # what the transition cites. Manufacturing a `reattach_failed` instead
        # would record an attempt that was never made, on a session whose
        # outcome was never in doubt.
        self.assertEqual(reopened.store.observations_of(session_id), [])
        self.assertEqual(self._leaving_launching(reopened, session_id),
                         {"kind": "launch_result",
                          "ref": reopened.store.launch_request_of(session_id)["request_id"]})
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "launching-interrupted-after-unknown")

    def test_the_result_read_back_is_this_session_s_and_not_some_other_turn_s(self):
        """A chat has one launch_result per turn, so resolving `launching` from
        `the` launch_result is only right if it is read back by session. An
        earlier turn's report is a report about a different agent."""
        first = support.deterministic(self.STUB, "l23", store_path=self.path)
        chat_id = first.create_chat("An earlier turn, then a crash")
        first.send_turn(chat_id, "the turn that completed")
        finished = first.store.sessions_of(chat_id)[0]["session_id"]
        support.release(first)

        # The chat is free again, and the next turn is the one that crashes.
        crashed_chat, session_id, handle = self._die_in_the_window(
            "l24", chat_id=chat_id)
        self.assertEqual(crashed_chat, chat_id)
        self.assertNotEqual(session_id, finished)
        self.assertEqual(len(first.store.all_of("launch_result")), 1)

        reopened = self._restart("l25")
        self.assertEqual(len(reopened.store.all_of("launch_result")), 2)
        self.assertEqual(reopened.store.launch_result_of(session_id)["session_id"],
                         session_id,
                         "the launch_result read back belongs to another session")
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual(
            self._leaving_launching(reopened, session_id)["ref"],
            reopened.store.launch_request_of(session_id)["request_id"],
            "the transition cited an earlier turn's launch request")
        reopened.abandon(chat_id)
        self.assert_store_valid(reopened.store, "launching-interrupted-after-an-earlier-turn")

    def test_no_launch_outcome_at_all_leaves_a_session_in_launching(self):
        """The enumeration itself, mechanised.

        N1 was a residue: an input the old guard covered, the new one did not,
        and no test could pin because the branch had been deleted. The defence
        against another one is to enumerate rather than to spot-check. Contract
        6.3 has exactly three launch outcomes, plus the case where the crash beat
        the report; this walks all four and requires each to leave `launching`
        for a state a user can act on. An outcome added later and not handled
        fails here rather than bricking a chat.
        """
        cases = [(outcome, target, options) for outcome, target, options in (
            ("accepted", "running", {}),
            ("failed", "launch_failed", {"launch_outcomes": ["unavailable"]}),
            ("unknown", "unknown", {"launch_outcomes": ["unknown"]}),
        )]
        self.assertEqual([outcome for outcome, _, _ in cases], list(lb.LAUNCH_OUTCOMES),
                         "a launch outcome exists that this enumeration does not "
                         "walk, so nothing says what a restart does with it")
        cases.append((None, "in_launch", {}))

        import os
        import tempfile
        for i, (outcome, target, options) in enumerate(cases):
            with self.subTest(outcome=outcome):
                self.path = os.path.join(tempfile.mkdtemp(prefix="dory-enum-"),
                                         "records.jsonl")
                chat_id, session_id, _ = self._die_in_the_window(
                    "e%d" % i, options=options,
                    at="in_launch" if outcome is None else target)
                reopened = self._restart("f%d" % i)
                resolved = reopened.reattach_on_start()
                self.assertEqual([sid for sid, _ in resolved], [session_id])
                state = reopened.store.get("agent_session", session_id)["state"]
                self.assertNotEqual(state, "launching",
                                    "a restart left the session in `launching`, "
                                    "which no user action can leave")
                # And the exit is real, not merely a different label.
                if state == "unknown":
                    reopened.abandon(chat_id)
                    state = reopened.store.get("agent_session", session_id)["state"]
                self.assertIn(state, session_manager.TERMINAL_SESSION_STATES | {"running"})
                if state in session_manager.TERMINAL_SESSION_STATES:
                    self.assertEqual(
                        reopened.send_turn(chat_id, "and now?").session_state,
                        "completed", "the chat did not become usable again")
                self.assertEqual(support.violations(reopened.store.snapshot()), [])

    def test_an_accepted_result_with_no_handle_is_refused_rather_than_addressed(self):
        """The shape `LaunchResult` forbids and only a hand-written store can
        hold. It must still leave the session somewhere the user can act, and it
        must not manufacture an address."""
        chat_id, session_id, _ = self._die_in_the_window("l21")
        reopened = self._restart("l22")
        session = reopened.store.get("agent_session", session_id)
        del session["agent_handle"]
        reopened.store.put(session)
        result = reopened.store.launch_result_of(session_id)
        result["agent_handle"] = None
        reopened.store.put(result)

        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual([o["kind"] for o in reopened.store.observations_of(session_id)],
                         ["reattach_failed"])
        self.assertEqual([address for _, address in reopened._boundary.addressed], [])
        self.assertNotIn(
            "running",
            [t["to"] for t in
             reopened.store.get("agent_session", session_id)["transitions"]],
            "`running` was concluded from a report with no handle in it, which "
            "leaves the session in a state the harness cannot address and which "
            "contract 5.2's precondition for `launching -> running` rejects")
        reopened.abandon(chat_id)
        self.assertEqual(reopened.store.get("agent_session", session_id)["state"],
                         "abandoned")


if __name__ == "__main__":
    unittest.main()
