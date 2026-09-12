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
        session = []
        self._sessions[instruction.session_id] = session
        self._turn(session, instruction.instruction_text)
        return lb.LaunchResult(lb.OUTCOME_ACCEPTED,
                               agent_handle="%s-%d" % (self._handle_prefix, self._counter))

    def deliver(self, session_id, instruction):
        if not self._capabilities.supports_delivery:
            return lb.LaunchBoundary.deliver(self, session_id, instruction)
        self._turn(self._sessions[session_id], instruction.instruction_text)
        return lb.DeliveryAck(True)

    def events(self, session_id, after_sequence):
        session = self._sessions.get(session_id)
        if session is None:
            raise lb.LauncherError("unavailable", "no such session")
        return lb.EventsPage([p for p in session if p.sequence > after_sequence])

    def stop(self, session_id, reason):
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

    def test_no_behaviour_branches_on_a_launcher_id(self):
        """`launcher_id` is opaque (contract 4.3). Two launchers that differ only
        in id and in what their handles look like must produce identical chats."""
        transcripts = []
        states = []
        for prefix in ("opaque", "host-buildbox-07-pid-3319"):
            launcher = OutOfTreeLauncher(handle_prefix=prefix)
            launcher.launcher_id = "out-of-tree"
            harness = open_harness({}, launcher=launcher)
            chat_id = run_three_turns(harness, "Opaque")
            transcripts.append(harness.transcript(chat_id))
            states.append([s["state"] for s in harness.store.sessions_of(chat_id)])
        self.assertEqual(transcripts[0], transcripts[1])
        self.assertEqual(states[0], states[1])

    def test_the_session_manager_takes_its_launcher_by_injection(self):
        parameters = inspect.signature(session_manager.SessionManager.__init__).parameters
        self.assertIn("boundary", parameters)
        source = inspect.getsource(session_manager)
        self.assertNotIn("import launchers", source)
        self.assertNotIn("build_launcher", source)


if __name__ == "__main__":
    unittest.main()
