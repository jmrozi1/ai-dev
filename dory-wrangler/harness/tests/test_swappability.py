"""Is the seam genuinely swappable, or only named?

The weak claim is "there are two launcher classes and a registry". These tests
try to make that claim false: they run the whole loop against an implementation
defined in this file and registered nowhere, and they check as a *fact* that the
chat and session layers never load, name, or branch on a launcher.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
import unittest

import support
from support import StoreCheck, expected_transcript, run_three_turns

import launch_boundary as lb
import session_manager
import store as store_module
from app import open_harness
from launchers.registry import UnknownLauncher, build_launcher


class OutOfTreeLauncher(lb.LaunchBoundary):
    """A launcher this repository has never seen, written against the interface
    alone. If the seam is real, the whole chat loop runs on it unchanged."""

    launcher_id = "out-of-tree"

    def __init__(self, continuation="fresh_binding", response_shape="one_shot",
                 handle_prefix="opaque"):
        self._capabilities = lb.LauncherCapabilities(continuation, response_shape, None)
        self._handle_prefix = handle_prefix
        self._sessions = {}
        self._counter = 0
        self.addressed = []

    @property
    def capabilities(self):
        return self._capabilities

    def _emit(self, session, source, interpretation, obj, kind=None, text=None):
        import json
        payload = lb.EventPayload(
            len(session) + 1, source, interpretation,
            json.dumps(obj).encode("utf-8"), interpreted_type=kind, text=text)
        session.append(payload)

    def _turn(self, session, text):
        reply = "answer to: %s" % text.strip()
        self._emit(session, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                   {"type": "assistant_text", "text": reply},
                   kind=lb.PAYLOAD_ASSISTANT_TEXT, text=reply)
        self._emit(session, lb.SOURCE_AGENT, lb.INTERPRETATION_RECOGNIZED,
                   {"type": "turn_complete"}, kind=lb.PAYLOAD_TURN_COMPLETE)
        if not self._capabilities.supports_delivery:
            self._emit(session, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                       {"type": "session_completed"}, kind=lb.PAYLOAD_SESSION_COMPLETED)

    def launch(self, instruction):
        self._counter += 1
        handle = "%s-%d" % (self._handle_prefix, self._counter)
        session = []
        # Keyed on the handle: contract 6.1 gives the three addressing
        # operations the handle and nothing else.
        self._sessions[handle] = session
        self._turn(session, instruction.instruction_text)
        return lb.LaunchResult(lb.OUTCOME_ACCEPTED, agent_handle=handle)

    def deliver(self, agent_handle, instruction):
        if not self._capabilities.supports_delivery:
            return lb.LaunchBoundary.deliver(self, agent_handle, instruction)
        self.addressed.append(("deliver", agent_handle))
        self._turn(self._sessions[agent_handle], instruction.instruction_text)
        return lb.DeliveryAck(True)

    def events(self, agent_handle, after_sequence):
        self.addressed.append(("events", agent_handle))
        session = self._sessions.get(agent_handle)
        if session is None:
            raise lb.LauncherError("unavailable", "no such agent")
        return lb.EventsPage([p for p in session if p.sequence > after_sequence])

    def stop(self, agent_handle, reason):
        self.addressed.append(("stop", agent_handle))
        return lb.StopAck(True)


class SelectionIsConfigurationOnly(unittest.TestCase, StoreCheck):

    def test_the_registry_builds_each_configured_launcher(self):
        for name, config in support.CONFIGURATIONS:
            with self.subTest(configuration=name):
                launcher = build_launcher(config)
                self.assertIsInstance(launcher, lb.LaunchBoundary)
                self.assertTrue(re.match(lb.LAUNCHER_ID_PATTERN, launcher.launcher_id))
                releaser = getattr(launcher, "release_all", None)
                if releaser:
                    releaser()

    def test_an_unconfigured_launcher_fails_closed(self):
        with self.assertRaises(UnknownLauncher):
            build_launcher({"launcher": "internal-bridge"})

    def test_the_same_loop_runs_on_a_launcher_registered_nowhere(self):
        """The strongest form of the claim: an implementation defined in this
        test file, never added to the registry, never imported by the package."""
        for continuation, shape in (("fresh_binding", "one_shot"),
                                    ("persistent", "stream")):
            with self.subTest(continuation=continuation, response_shape=shape):
                harness = open_harness({}, launcher=OutOfTreeLauncher(continuation, shape))
                chat_id = run_three_turns(harness, "Out of tree")
                self.assertEqual(harness.transcript(chat_id), expected_transcript())
                support.end_chat(harness, chat_id)
                self.assert_store_valid(
                    harness.store, "out-of-tree-%s-%s" % (continuation, shape),
                    "The full loop against a launcher the package does not know.")

    def test_a_second_registry_can_replace_the_table_entirely(self):
        harness = open_harness({"launcher": "anything-at-all"},
                               builders={"anything-at-all": lambda o: OutOfTreeLauncher()})
        chat_id = run_three_turns(harness, "Replaced registry")
        self.assertEqual(harness.transcript(chat_id), expected_transcript())


class NothingAboveTheSeamKnowsALauncher(unittest.TestCase):
    """Facts, not naming conventions."""

    CORE = ("launch_boundary", "session_manager", "store", "identity", "errors")

    def test_importing_the_core_loads_no_launcher_and_no_subprocess(self):
        program = (
            "import sys; sys.path.insert(0, %r);"
            "import session_manager, store, launch_boundary;"
            "bad=[m for m in sys.modules if m.startswith('launchers') "
            "or m in ('subprocess','socket','shutil')];"
            "print(sorted(bad))" % support.HARNESS
        )
        out = subprocess.check_output([sys.executable, "-c", program],
                                      universal_newlines=True).strip()
        self.assertEqual(out, "[]",
                         "importing the chat and session layers pulled in %s" % out)

    def test_the_core_sources_never_name_a_launcher(self):
        names = ("dev-local", "dev_local", "scripted-stub", "scripted_stub",
                 "DevLocal", "ScriptedStub", "launch_agent.sh", "vscode", "subprocess",
                 "Popen", "argv", "os.environ")
        for module_name in self.CORE:
            module = __import__(module_name)
            source = inspect.getsource(module)
            # The seam's denylist legitimately *enumerates* mechanics names in
            # order to refuse them. Excluding that one literal is not excusing an
            # exception: everything else in the module is still checked, and the
            # closed schema -- not the denylist -- is what the seam relies on.
            source = re.sub(r"_MECHANICS_FIELD_NAMES = frozenset\((?:.|\n)*?\n\)",
                            "", source)
            for name in names:
                self.assertFalse(
                    name in source,
                    "%s names %r, so the chat and session layers are not "
                    "launcher-independent" % (module_name, name))

    # The two arms of the `launcher_id` experiment. They differ in the id and in
    # the shape of the handles, and in nothing else.
    ARMS = (("out-of-tree", "opaque"),
            ("other-launcher", "host-buildbox-07-pid-3319"))

    def _run_arm(self, launcher_id, handle_prefix, manager=None):
        launcher = OutOfTreeLauncher(handle_prefix=handle_prefix)
        launcher.launcher_id = launcher_id
        if manager is None:
            harness = open_harness({}, launcher=launcher)
        else:
            harness = manager(store_module.Store(None), launcher)
        chat_id = run_three_turns(harness, "Opaque")
        sessions = harness.store.sessions_of(chat_id)
        return {
            "transcript": harness.transcript(chat_id),
            "states": [s["state"] for s in sessions],
            "recorded_ids": [s["launcher_id"] for s in sessions],
            "handles": [s["agent_handle"] for s in sessions],
            "packets": [p["instruction_text"]
                        for p in harness.store.all_of("launch_request")],
        }

    def test_no_behaviour_branches_on_a_launcher_id(self):
        """`launcher_id` is opaque (contract 4.3). Two launchers that differ in
        id, and in what their handles look like, must produce identical chats.

        **An earlier revision of this test was review finding F4**: it assigned
        `launcher.launcher_id = "out-of-tree"` inside *both* arms of its loop,
        which was already `OutOfTreeLauncher`'s own value, so the assignment was
        a no-op and the arms differed only in the handle prefix. The claim it
        made was true -- `launcher_id` is written into the session record and
        read nowhere -- but the evidence offered for it tested the label. The
        repair is not "vary it and hope": the varied thing is now *asserted to
        have actually differed*, and to have differed **in the store**, before
        anything is concluded from the arms agreeing.
        """
        arms = [self._run_arm(launcher_id, prefix)
                for launcher_id, prefix in self.ARMS]

        # 1. The input really varied.
        self.assertNotEqual(self.ARMS[0][0], self.ARMS[1][0])
        # 2. It reached the store, so the experiment exercised a real difference
        #    rather than two runs of the same configuration.
        self.assertEqual(arms[0]["recorded_ids"],
                         [self.ARMS[0][0]] * len(arms[0]["recorded_ids"]))
        self.assertEqual(arms[1]["recorded_ids"],
                         [self.ARMS[1][0]] * len(arms[1]["recorded_ids"]))
        self.assertTrue(arms[0]["recorded_ids"])
        self.assertNotEqual(arms[0]["recorded_ids"], arms[1]["recorded_ids"])
        # 3. The handles differed too, so nothing downstream could have been
        #    matching on those instead.
        self.assertNotEqual(arms[0]["handles"], arms[1]["handles"])
        # 4. And with all of that varied, the chat is identical.
        self.assertEqual(arms[0]["transcript"], arms[1]["transcript"])
        self.assertEqual(arms[0]["transcript"], expected_transcript())
        self.assertEqual(arms[0]["states"], arms[1]["states"])
        self.assertEqual(arms[0]["packets"], arms[1]["packets"])

    def test_the_comparison_fails_against_a_harness_that_does_branch_on_it(self):
        """The teeth. Without this, an all-passing comparison is consistent with
        a comparison that cannot tell anything apart -- which is exactly how F4
        survived a 37-probe suite written against this defect class."""

        class BranchesOnTheLauncherId(session_manager.SessionManager):
            def _launch_turn(self, chat_id, text):
                if self._boundary.launcher_id == "other-launcher":
                    text = "[%s] %s" % (self._boundary.launcher_id, text)
                return session_manager.SessionManager._launch_turn(self, chat_id, text)

        arms = [self._run_arm(launcher_id, prefix, manager=BranchesOnTheLauncherId)
                for launcher_id, prefix in self.ARMS]
        self.assertNotEqual(arms[0]["transcript"], arms[1]["transcript"],
                            "the comparison cannot tell two different chats apart, "
                            "so it proves nothing")
        self.assertNotEqual(arms[0]["packets"], arms[1]["packets"])

    def test_the_repaired_evidence_would_fail_on_the_defective_version(self):
        """And the specific defect F4 named: holding the id constant across the
        arms must now be caught by the test itself, not by a reviewer."""
        arms = [self._run_arm("out-of-tree", prefix)
                for _, prefix in self.ARMS]
        self.assertEqual(arms[0]["recorded_ids"], arms[1]["recorded_ids"],
                         "this is the no-op assignment F4 described")
        with self.assertRaises(AssertionError):
            self.assertNotEqual(arms[0]["recorded_ids"], arms[1]["recorded_ids"])

    def test_the_session_manager_takes_its_launcher_by_injection(self):
        parameters = inspect.signature(session_manager.SessionManager.__init__).parameters
        self.assertIn("boundary", parameters)
        source = inspect.getsource(session_manager)
        self.assertNotIn("import launchers", source)
        self.assertNotIn("build_launcher", source)


if __name__ == "__main__":
    unittest.main()
