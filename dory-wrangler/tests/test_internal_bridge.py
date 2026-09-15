"""The converged product, hosting a model of the internal launcher's proven transport.

Internally, `launch_agent.sh` runs `codex exec --json` for a fresh agent and
`codex exec resume --json` for a later turn, both emitting JSONL in one shape:
the resume ID is `thread.started.thread_id` and the reply is an
`item.completed` / `agent_message` event with its text in `item.text`
(`internal_bridge.py` has the record). This file is the evidence that the
product hosts that shape **unchanged** -- nothing in `src/` was touched for it:

* persistent continuation is the normal case: one launch, then deliveries that
  resume the same thread, shown by the script's own call log and by an answer
  only the resumed thread could give;
* the handle is the `thread_id` in the output, never made up;
* every output line of launch and of deliver is preserved verbatim, in order,
  and goes through one classification, with no plain-text resume special case;
* a real restart of the shell (SIGKILL, new process) resumes the same thread;
* fresh binding is still a supported declared capability;
* every store is validated -- here through `validate_store.py` as a separate
  program where the test is about the shell, and by `run_tests.py`'s phase 2 for
  every store kept.

Nothing here is a claim about the internal bridge's *behaviour* beyond the
transport shape above. It replaces the model of 2026-09-12, which assumed the
script issued no handle, synthesised one, and started a fresh agent per turn;
the internal result overturned all three.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import support
from support import StoreCheck, expected_transcript, run_three_turns

from dory_wrangler import launch_boundary as lb
from dory_wrangler.errors import ConcurrentLaunchRefused
from dory_wrangler.launchers.registry import UnknownLauncher, build_launcher
from internal_bridge import InternalBridgeLauncher, classify
from model_launch_agent import RECALL_PROMPT

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(HERE)


def raw_bytes(event):
    raw = event["raw"]
    if raw["encoding"] == "base64":
        return base64.b64decode(raw["body"])
    return raw["body"].encode("utf-8")


class ModelDirs(object):
    """A "Codex" home and a launcher spool, outside the repository."""

    def make_dirs(self):
        base = tempfile.mkdtemp(prefix="dory-internal-bridge-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    def launcher(self, **options):
        return InternalBridgeLauncher(self.codex_home, self.spool, **options)

    def calls(self):
        """The script's own log of every invocation: argv, thread, stdout, exit."""
        path = os.path.join(self.codex_home, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [json.loads(line) for line in handle]

    def emitted_lines(self):
        return [line.encode("utf-8") for call in self.calls() for line in call["stdout"]]


class TheProductHostsTheJsonlModelUnchanged(unittest.TestCase, StoreCheck, ModelDirs):

    def setUp(self):
        self.make_dirs()

    def test_several_turns_resume_one_thread(self):
        harness = support.harness({}, launcher=self.launcher())
        chat_id = run_three_turns(harness, "Internal bridge, persistent")
        harness.send_turn(chat_id, RECALL_PROMPT)

        sessions = support.view(harness).sessions_of(chat_id)
        self.assertEqual([s["state"] for s in sessions], ["running"],
                         "one agent serves every turn and is still there for the next")
        session = sessions[0]
        calls = self.calls()
        thread_id = calls[0]["thread_id"]
        self.assertEqual([c["argv"][0] for c in calls],
                         [support.THREE_TURNS[0]]
                         + ["--resumeID=%s" % thread_id] * 3,
                         "one fresh launch, then every later turn resumes that thread")
        self.assertEqual(session["agent_handle"], thread_id)

        events = harness.store.read_all_events_of_session(chat_id, session["session_id"])
        first = json.loads(raw_bytes(events[0]).decode("utf-8"))
        self.assertEqual((first["type"], first["thread_id"]), ("thread.started", thread_id),
                         "the handle is the thread_id the output carried")

        view = support.view(harness)
        self.assertEqual([p["instruction_text"] for p in view.all_of("launch_request")],
                         [support.THREE_TURNS[0]])
        self.assertEqual([d["instruction_text"] for d in view.deliveries_of(session["session_id"])],
                         list(support.THREE_TURNS[1:]) + [RECALL_PROMPT])
        self.assertEqual(harness.transcript(chat_id)[:6], expected_transcript())
        self.assertEqual(harness.transcript(chat_id)[6:],
                         [(7, "user", RECALL_PROMPT),
                          (8, "agent", "you first said: %s" % support.THREE_TURNS[0])],
                         "only the resumed thread knows the first turn: the delivery "
                         "carried the recall question alone")
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-persistent-four-turns",
                                "The JSONL internal-bridge model: one launch, three "
                                "resumes of the same thread_id, persistent/one_shot.")
        support.end_chat(harness, chat_id)

    def test_every_output_line_of_launch_and_deliver_is_preserved_verbatim_in_order(self):
        launcher = self.launcher(behaviour=("thread-started-without-id-first",
                                            "synthetic-unrecognized", "synthetic-malformed",
                                            "synthetic-not-an-object", "synthetic-item",
                                            "agent-message-without-text",
                                            "synthetic-typeless"))
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Raw JSONL preserved")
        harness.send_turn(chat_id, "first")
        harness.send_turn(chat_id, "second")
        session = support.view(harness).sessions_of(chat_id)[0]
        events = harness.store.read_all_events_of_session(chat_id, session["session_id"])

        self.assertEqual([raw_bytes(e) for e in events], self.emitted_lines(),
                         "every line the script printed, launch and resume alike, "
                         "byte for byte and in order")
        per_turn = [("agent", "malformed", None),                   # thread.started, no id
                    ("agent", "recognized", "thread.started"),
                    ("agent", "unrecognized", None),                # synthetic type
                    ("agent", "malformed", None),                   # not JSON
                    ("agent", "unrecognized", None),                # JSON, not an object
                    ("agent", "unrecognized", None),                # an item nothing has shown
                    ("agent", "malformed", None),                   # agent_message, no text
                    ("agent", "unrecognized", None),                # an object with no type
                    ("agent", "recognized", "assistant_text")]
        self.assertEqual([(e["source"], e["interpretation"], e["interpreted_type"])
                          for e in events], per_turn * 2,
                         "a resumed turn is classified exactly as a launched one")
        self.assertEqual(support.view(harness).sessions_of(chat_id)[0]["agent_handle"],
                         self.calls()[0]["thread_id"],
                         "the handle is the first *usable* thread.started, not the first line "
                         "typed thread.started")
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                         [("user", "first"), ("agent", "answer to: first"),
                          ("user", "second"), ("agent", "answer to: second")],
                         "nothing unrecognized or malformed reaches the chat")
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-synthetic-lines")
        support.end_chat(harness, chat_id)

    def test_a_resume_that_prints_plain_text_is_malformed_like_any_other_line(self):
        """The old model's guess -- a resume prints bare text -- gets no special
        case. It is not JSON, so it is `malformed`, preserved, and not chat."""
        harness = support.harness({}, launcher=self.launcher(behaviour=("plain-text-on-resume",)))
        chat_id = harness.create_chat("No plain-text resume")
        harness.send_turn(chat_id, "first")
        harness.send_turn(chat_id, "second")
        session = support.view(harness).sessions_of(chat_id)[0]
        events = harness.store.read_all_events_of_session(chat_id, session["session_id"])
        self.assertEqual((events[-1]["interpretation"], raw_bytes(events[-1])),
                         ("malformed", b"answer to: second"))
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                         [("user", "first"), ("agent", "answer to: first"),
                          ("user", "second")])
        self.assertEqual(classify(1, b"answer to: second").interpretation,
                         lb.INTERPRETATION_MALFORMED)
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-plain-text-resume")
        support.end_chat(harness, chat_id)

    def test_two_agent_messages_in_one_turn_are_both_the_chat_and_neither_is_invented(self):
        """Several `agent_message` items per turn are unproven internally; the
        model can print two, and each becomes the message citing its own event."""
        harness = support.harness({}, launcher=self.launcher(behaviour=("two-messages",)))
        chat_id = harness.create_chat("Two messages")
        harness.send_turn(chat_id, "first")
        harness.send_turn(chat_id, "second")
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                         [("user", "first"), ("agent", "answer to: first"),
                          ("agent", "and a second message"), ("user", "second"),
                          ("agent", "answer to: second"), ("agent", "and a second message")])
        session = support.view(harness).sessions_of(chat_id)[0]
        events = dict((e["event_id"], e) for e in
                      harness.store.read_all_events_of_session(chat_id, session["session_id"]))
        for message in harness.store.read_messages(chat_id):
            if message["author"] == "agent":
                cited = json.loads(raw_bytes(events[message["source_event_id"]]).decode("utf-8"))
                self.assertEqual(cited["item"]["text"], message["content"]["text"])
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-two-messages")
        support.end_chat(harness, chat_id)

    def test_the_launcher_remembers_nothing_between_calls(self):
        """Contract 6.1, checked rather than read off the code: a new launcher
        instance for every single operation serves the same chat identically."""
        dirs = self

        class FreshEveryCall(lb.LaunchBoundary):
            launcher_id = "internal-bridge"
            capabilities = dirs.launcher().capabilities

            def launch(self, instruction):
                return dirs.launcher().launch(instruction)

            def deliver(self, agent_handle, instruction):
                return dirs.launcher().deliver(agent_handle, instruction)

            def events(self, agent_handle, after_sequence):
                return dirs.launcher().events(agent_handle, after_sequence)

            def stop(self, agent_handle, reason):
                return dirs.launcher().stop(agent_handle, reason)

        harness = support.harness({}, launcher=FreshEveryCall())
        chat_id = run_three_turns(harness, "A launcher per call")
        self.assertEqual(harness.transcript(chat_id), expected_transcript())
        thread_id = self.calls()[0]["thread_id"]
        self.assertEqual([c["argv"][0] for c in self.calls()[1:]],
                         ["--resumeID=%s" % thread_id] * 2)
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-launcher-per-call")
        support.end_chat(harness, chat_id)

    def test_the_handle_is_taken_from_the_output_and_never_made_up(self):
        harness = support.harness({}, launcher=self.launcher(behaviour=("no-thread-started",)))
        chat_id = harness.create_chat("No thread started")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual((outcome.launch_outcome, outcome.failure_category),
                         ("failed", "no_acknowledgement"))
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "launch_failed")
        self.assertIsNone(session.get("agent_handle"),
                          "the script ran a thread but printed no thread.started, and "
                          "no handle was invented for it")
        self.assertIsNotNone(self.calls()[0]["thread_id"])
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-no-thread-started")

        # And two launches carry exactly the two thread_ids their outputs carried.
        other = support.harness({}, launcher=self.launcher(continuation="fresh_binding"))
        for i in range(2):
            other.send_turn(other.create_chat("Handle %d" % i), "hello")
        handles = sorted(s["agent_handle"] for s in support.view(other).all_of("agent_session"))
        self.assertEqual(handles, sorted(c["thread_id"] for c in self.calls()[1:]))
        self.assertEqual(len(set(handles)), 2)

    def test_the_seam_needed_nothing_added_for_it(self):
        implemented = set(name for name in vars(InternalBridgeLauncher)
                          if not name.startswith("_") and name != "launcher_id")
        self.assertEqual(implemented, {"launch", "deliver", "events", "stop", "capabilities"})
        capabilities = self.launcher().capabilities
        self.assertEqual(capabilities.as_record(),
                         {"continuation": "persistent", "response_shape": "one_shot",
                          "instruction_bound_bytes": None})

    def test_it_is_not_a_registered_launcher(self):
        with self.assertRaises(UnknownLauncher):
            build_launcher({"launcher": "internal-bridge"})


class FreshBindingIsStillADeclaredCapability(unittest.TestCase, StoreCheck, ModelDirs):

    def setUp(self):
        self.make_dirs()

    def test_three_turns_three_fresh_threads(self):
        harness = support.harness({}, launcher=self.launcher(continuation="fresh_binding"))
        chat_id = run_three_turns(harness, "Internal bridge, fresh binding")
        self.assertEqual(harness.transcript(chat_id), expected_transcript())
        self.assertEqual([s["state"] for s in support.view(harness).sessions_of(chat_id)],
                         ["completed"] * 3)
        self.assertEqual([c["argv"] for c in self.calls()],
                         [[text] for text in support.THREE_TURNS],
                         "no turn resumed anything")
        self.assertEqual(len(set(c["thread_id"] for c in self.calls())), 3)
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-fresh-binding")


class ARestartOfTheShellResumesTheSameThread(unittest.TestCase, ModelDirs):
    """A real restart: the shell process is SIGKILLed and a new one serves the store."""

    def setUp(self):
        self.make_dirs()
        self.root = support.scratch_root()
        self.processes = []

    def start(self):
        from shellproc import ShellProcess
        port_file = os.path.join(self.root, ".model-port")
        if os.path.exists(port_file):
            os.unlink(port_file)
        errors = os.path.join(self.root, ".model-stderr")
        with open(errors, "w") as err:
            process = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "model_shell.py"), "--root", self.root,
                 "--port-file", port_file, "--codex-home", self.codex_home,
                 "--spool", self.spool],
                stdout=subprocess.DEVNULL, stderr=err)
        self.addCleanup(self._kill, process)
        for _ in range(400):
            if os.path.exists(port_file):
                break
            if process.poll() is not None:
                with open(errors) as err:
                    self.fail("the model shell exited: %s" % err.read())
            time.sleep(0.05)
        shell = ShellProcess(self.root)
        shell.process = process
        with open(port_file) as handle:
            shell.port = int(handle.read())
        return shell

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            process.kill()
        process.wait()

    def validate(self):
        done = subprocess.run([sys.executable, os.path.join(PRODUCT, "validate_store.py"),
                               self.root], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(done.returncode, 0, done.stdout.decode("utf-8", "replace"))

    def test_a_killed_shell_is_restarted_and_the_chat_resumes_on_the_same_thread(self):
        first = self.start()
        _status, chat = first.post("/api/chats")
        chat_id = chat["chat_id"]
        self.assertEqual(first.post("/api/chats/%s/messages" % chat_id, {"text": "hello"})[0], 201)
        self.assertEqual(first.post("/api/chats/%s/messages" % chat_id,
                                    {"text": "and again"})[0], 201)
        os.kill(first.process.pid, signal.SIGKILL)
        first.process.wait()
        self.validate()

        second = self.start()
        status, after = second.post("/api/chats/%s/messages" % chat_id, {"text": RECALL_PROMPT})
        self.assertEqual(status, 201)
        self.assertEqual([(m["author"], m["text"]) for m in after["messages"]][-2:],
                         [("user", RECALL_PROMPT), ("agent", "you first said: hello")])

        thread_id = self.calls()[0]["thread_id"]
        self.assertEqual([c["argv"][0] for c in self.calls()],
                         ["hello", "--resumeID=%s" % thread_id, "--resumeID=%s" % thread_id],
                         "the restarted shell resumed the thread the first one started")
        from dory_wrangler.store import ChatStore
        store = ChatStore(self.root, read_only=True)
        sessions = store.list_sessions(chat_id)
        self.assertEqual([(s["state"], s["agent_handle"]) for s, _ in sessions],
                         [("running", thread_id)])
        self.assertEqual([o["kind"] for o in store.read_session_observations(
            chat_id, sessions[0][0]["session_id"])], ["reattached"])
        self.validate()


class TheOneActionIsTheExitOnThisPath(unittest.TestCase, StoreCheck, ModelDirs):
    """No stop operation has been shown for the script, so a stop is never
    confirmed; the shell's one action still takes the chat out, and the next
    turn starts a new thread."""

    def setUp(self):
        self.make_dirs()

    def test_an_unconfirmed_stop_then_abandon_then_a_new_thread(self):
        harness = support.harness({}, launcher=self.launcher())
        chat_id = harness.create_chat("Stop")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        self.assertEqual(harness.abandon(chat_id), "abandoned")
        self.assertEqual([o["kind"] for o in support.view(harness).observations_of(session_id)],
                         ["stop_unconfirmed"])
        harness.send_turn(chat_id, "again")
        self.assertEqual([c["argv"] for c in self.calls()], [["hello"], ["again"]],
                         "after the abandon the next turn is a fresh thread, not a resume")
        for session in support.view(harness).sessions_of(chat_id):
            self.assertNotIn("terminated", [t["to"] for t in session["transitions"]])
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-abandoned")
        support.end_chat(harness, chat_id)

    def test_a_restart_that_finds_no_output_of_the_thread_leaves_the_one_action(self):
        root = support.scratch_root()
        harness = support.harness({}, store_path=root, launcher=self.launcher())
        chat_id = harness.create_chat("Spool gone")
        harness.send_turn(chat_id, "hello")
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        harness.store.close()
        shutil.rmtree(self.spool)

        reopened = support.harness({}, store_path=root, launcher=self.launcher())
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        with self.assertRaises(ConcurrentLaunchRefused):
            reopened.send_turn(chat_id, "again")
        self.assertEqual(reopened.abandon(chat_id), "abandoned")
        self.assertEqual(reopened.send_turn(chat_id, "again").session_state, "running")
        self.assert_store_valid(reopened.store, "internal-bridge-jsonl-restart-spool-gone")
        support.end_chat(reopened, chat_id)


class ADeadBridgeIsDiscoverableOnlyByAttemptingATurn(unittest.TestCase, StoreCheck, ModelDirs):

    def setUp(self):
        self.make_dirs()

    def test_the_turn_is_answered_by_nothing_and_a_later_turn_recovers(self):
        launcher = self.launcher(behaviour=("no-output",))
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("The bridge is down")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual((outcome.launch_outcome, outcome.failure_category),
                         ("failed", "unavailable"))
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                         [("user", "hello")])
        launcher._behaviour = ()
        harness.send_turn(chat_id, "are you there now?")
        self.assertEqual([s["state"] for s in support.view(harness).sessions_of(chat_id)],
                         ["launch_failed", "running"])
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-recovered")
        support.end_chat(harness, chat_id)

    def test_a_resume_the_script_refuses_is_unacknowledged_and_fabricates_nothing(self):
        harness = support.harness({}, launcher=self.launcher())
        chat_id = harness.create_chat("Thread gone")
        harness.send_turn(chat_id, "hello")
        shutil.rmtree(os.path.join(self.codex_home, "threads"))
        outcome = harness.send_turn(chat_id, "still there?")
        self.assertEqual(outcome.agent_message_ids, [])
        session_id = support.view(harness).sessions_of(chat_id)[0]["session_id"]
        self.assertEqual([d["acknowledged"] for d in
                          support.view(harness).deliveries_of(session_id)], [False])
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)][-1],
                         ("user", "still there?"))
        self.assert_store_valid(harness.store, "internal-bridge-jsonl-resume-refused")
        support.end_chat(harness, chat_id)


class TheHandleIsWhatSelectsTheThread(unittest.TestCase, StoreCheck, ModelDirs):

    def setUp(self):
        self.make_dirs()

    def test_each_handle_serves_its_own_thread_and_an_unknown_one_is_refused(self):
        launcher = self.launcher()
        harness = support.harness({}, launcher=launcher)
        chats = [harness.create_chat("Thread %d" % i) for i in range(2)]
        for i, chat_id in enumerate(chats):
            harness.send_turn(chat_id, "question %d" % i)
        one, two = [support.view(harness).sessions_of(c)[0]["agent_handle"] for c in chats]
        self.assertNotEqual(one, two)
        self.assertEqual([p.text for p in launcher.events(one, 0).payloads if p.text],
                         ["answer to: question 0"])
        self.assertEqual([p.text for p in launcher.events(two, 0).payloads if p.text],
                         ["answer to: question 1"])
        for operation in (lambda h: launcher.events(h, 0),
                          lambda h: launcher.stop(h, "the user pressed Stop")):
            with self.assertRaises(lb.LauncherError) as caught:
                operation(one + "-not-this-one")
            self.assertEqual(caught.exception.category, "unavailable")
        for chat_id in chats:
            support.end_chat(harness, chat_id)


if __name__ == "__main__":
    unittest.main()
