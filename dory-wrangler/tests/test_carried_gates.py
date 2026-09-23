"""Findings carried into `handle-unsupported-and-malformed-events` from the
rendering review (F1, F2) and the classification check (L1, L2, L3).

* **F1** -- phase 3 fails closed on a session whose launcher no classifier reads,
  unless `reclassify_stores.ADJUDICATED_LAUNCHERS` names it with a reason; and the
  drain's stream-ended writer branch is pinned with a kept store under a checked
  launcher id (the review's mutation R05).
* **L1** -- adjudication entries name one record: store, session, sequence and a
  hash of the record's body.
* **L2** -- `run_tests.py` exits non-zero when phase 3 fails, and so does
  `reclassify_stores.py --json`, each run as a separate program.
* **F2** -- kept stores in both wire formats whose agent text carries `\\r\\n`, a
  decomposed character and only whitespace, so the exact-text gate reads them;
  and the same texts served over HTTP exactly.
* **L3** -- `read_line` takes a line of any length: over 64 KiB and over 1 MiB,
  preserved whole and, as an answer, rendered whole.
* Decision 0006's system-message rule, as phase 3 checks it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import support
from support import StoreCheck

import internal_bridge
import reclassify_stores
from model_launch_agent import TRICKY_TEXTS
from shellproc import ShellProcess
from test_rendering import ModelDirs, model_shell, post
from dory_wrangler import ids
from dory_wrangler import launch_boundary as lb
from dory_wrangler.launchers import dev_transport
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
from dory_wrangler.notices import NO_SHOWABLE_REPLY
from dory_wrangler.store import ChatStore

HERE = os.path.dirname(os.path.abspath(__file__))


def run_gate(test, stores, as_json=False, patches=None):
    """(exit status, output) of the gate over planted stores {file name: records}."""
    base = tempfile.mkdtemp(prefix="dory-gate-")
    test.addCleanup(shutil.rmtree, base, True)
    for name, records in stores.items():
        with open(os.path.join(base, name), "w") as out:
            json.dump({"records": records}, out)
    output = io.StringIO()
    with contextlib.ExitStack() as stack:
        for table, entries in (patches or {}).items():
            stack.enter_context(mock.patch.dict(getattr(reclassify_stores, table), entries))
        stack.enter_context(contextlib.redirect_stdout(output))
        code = reclassify_stores.main([base] + (["--json"] if as_json else []))
    return code, output.getvalue()


def copy(records):
    return json.loads(json.dumps(records))


def stub_store(turns=("hello",), title="Gate"):
    harness = support.harness({"launcher": "scripted-stub", "options": {}})
    chat_id = harness.create_chat(title)
    for text in turns:
        harness.send_turn(chat_id, text)
    return harness.store.export_records()


def by_type(records, kind):
    return [r for r in records if r["record_type"] == kind]


# ---------------------------------------------------------------------------
# F1
# ---------------------------------------------------------------------------

class TheGateFailsClosedOnALauncherItCannotRead(unittest.TestCase):

    def relabelled(self, records, launcher_id):
        changed = copy(records)
        for session in by_type(changed, "agent_session"):
            session["launcher_id"] = launcher_id
        return changed

    def test_an_unlisted_launcher_fails_and_an_adjudicated_one_passes(self):
        records = stub_store()
        self.assertEqual(run_gate(self, {"s.json": records})[0], 0)
        # The review's plant: relabel, change one character, drop a message.
        planted = self.relabelled(records, "future-real-launcher")
        by_type(planted, "message")[-1]["content"]["text"] += "X"
        code, output = run_gate(self, {"s.json": planted})
        self.assertEqual(code, 1)
        self.assertIn("UNREAD LAUNCHER s.json 'future-real-launcher'", output)
        self.assertEqual(run_gate(self, {"s.json": planted}, as_json=True)[0], 1)
        # Unread even with nothing wrong in it: unchecked is not passing.
        self.assertEqual(run_gate(self, {"s.json": self.relabelled(
            records, "future-real-launcher")})[0], 1)
        # A named probe id passes, and only by its name.
        self.assertEqual(run_gate(self, {"s.json": self.relabelled(records, "intake-probe")})[0], 0)
        with mock.patch.dict(reclassify_stores.ADJUDICATED_LAUNCHERS, clear=True):
            self.assertEqual(run_gate(self, {"s.json": self.relabelled(
                records, "intake-probe")})[0], 1)

    def test_every_adjudicated_launcher_has_a_reason(self):
        self.assertEqual(sorted(reclassify_stores.ADJUDICATED_LAUNCHERS),
                         ["intake-probe", "out-of-tree", "replaying"])
        for reason in reclassify_stores.ADJUDICATED_LAUNCHERS.values():
            self.assertGreater(len(reason), 40)


class StreamEndedPage(ScriptedStubLauncher):
    """The scripted stub, keeping its id so phase 3 reads it, whose one-shot pages
    say the stream ended -- which a launcher with no stream cannot say."""

    def events(self, agent_handle, after_sequence):
        page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
        return lb.EventsPage(page.payloads, stream_ended=True)


class TheStreamEndedBranchRendersItsText(unittest.TestCase, StoreCheck):
    """Render review mutation R05: the drain's branch for a page claiming an end
    of stream on a session without one wrote its text only under a probe id the
    gate did not read. Here it runs under `scripted-stub`, so the kept store is
    gated, and the answer is asserted directly as well."""

    def test_the_answer_on_such_a_page_is_rendered_and_the_claim_refused(self):
        for continuation in ("fresh_binding", "persistent"):
            with self.subTest(continuation=continuation):
                harness = support.harness({}, launcher=StreamEndedPage(
                    {"continuation": continuation, "response_shape": "one_shot"}))
                chat_id = harness.create_chat("Stream-ended page %s" % continuation)
                with self.assertRaises(lb.LaunchBoundaryError) as raised:
                    harness.send_turn(chat_id, "hello")
                self.assertIn("signalled that a stream ended", str(raised.exception))
                records = harness.store.export_records()
                self.assertEqual([(m["author"], m["content"]["text"]) for m in
                                  sorted(by_type(records, "message"),
                                         key=lambda m: m["sequence"])],
                                 [("user", "hello"), ("agent", "answer to: hello")])
                self.assert_store_valid(harness.store, "f1-stream-ended-page-%s" % continuation)


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------

class AnAdjudicationNamesOneRecord(unittest.TestCase):

    def mismatch(self, records, sequence=2, session_index=0):
        """Record a reading the event's bytes do not give."""
        changed = copy(records)
        sessions = sorted(by_type(changed, "agent_session"),
                          key=lambda s: s["transitions"][0]["at"])
        session_id = sessions[session_index]["session_id"]
        event = [e for e in by_type(changed, "diagnostic_event")
                 if e["session_id"] == session_id and e["sequence"] == sequence][0]
        event["interpretation"], event["interpreted_type"] = "unrecognized", None
        return changed, event

    def key(self, records, event):
        keys = reclassify_stores.session_keys(records)
        return ("s.json", keys[event["session_id"]], event["sequence"],
                ("unrecognized", None), reclassify_stores.body_hash(event["raw"]["body"]))

    def test_an_event_entry_does_not_cover_another_session_or_another_body(self):
        records = stub_store(turns=("one", "two"))
        planted, event = self.mismatch(records)
        entry = {self.key(planted, event): "planted"}
        self.assertEqual(run_gate(self, {"s.json": planted})[0], 1)
        self.assertEqual(run_gate(self, {"s.json": planted},
                                  patches={"ADJUDICATED": entry})[0], 0)
        # The review's two masks: a second session's record at the same sequence
        # with the same recorded reading, and the covered record's body changed.
        second, _ = self.mismatch(planted, session_index=1)
        self.assertEqual(run_gate(self, {"s.json": second},
                                  patches={"ADJUDICATED": entry})[0], 1)
        body = copy(planted)
        [e for e in by_type(body, "diagnostic_event")
         if e["event_id"] == event["event_id"]][0]["raw"]["body"] = \
            '{"type": "turn_complete", "changed": true}'  # still not unrecognized
        self.assertEqual(run_gate(self, {"s.json": body},
                                  patches={"ADJUDICATED": entry})[0], 1)

    def test_a_message_entry_does_not_cover_another_text(self):
        records = stub_store()
        planted = copy(records)
        message = [m for m in by_type(planted, "message") if m["author"] == "agent"][0]
        message["content"]["text"] = "answer to: hellX"
        keys = reclassify_stores.session_keys(planted)
        entry = {("s.json", keys[message["session_id"]], message["sequence"],
                  reclassify_stores.body_hash("answer to: hellX")): "planted"}
        self.assertEqual(run_gate(self, {"s.json": planted},
                                  patches={"ADJUDICATED_MESSAGES": entry})[0], 0)
        message["content"]["text"] = "answer to: hellY"
        self.assertEqual(run_gate(self, {"s.json": planted},
                                  patches={"ADJUDICATED_MESSAGES": entry})[0], 1)


# ---------------------------------------------------------------------------
# Decision 0006, as phase 3 checks it.
# ---------------------------------------------------------------------------

class SystemMessagesAreTheFixedWordsInTheirPlace(unittest.TestCase):

    def with_system(self, records, after_sequence, text=NO_SHOWABLE_REPLY):
        """Insert a system message after message `after_sequence`, renumbering."""
        changed = copy(records)
        messages = sorted(by_type(changed, "message"), key=lambda m: m["sequence"])
        for message in messages:
            if message["sequence"] > after_sequence:
                message["sequence"] += 1
        template = messages[0]
        changed.append(dict(template, message_id=ids.new_id("msg"),
                            sequence=after_sequence + 1, author="system",
                            session_id=None, source_event_id=None,
                            content={"content_type": "text/plain", "text": text}))
        return changed

    def test_each_misplacement_fails_and_the_notice_in_its_place_passes(self):
        answered = stub_store()  # user@1, agent@2
        cases = {
            "not the fixed words": (self.with_system(answered, 2, "the deploy is safe"),
                                    "not the harness's fixed words"),
            "in an answered turn": (self.with_system(answered, 2),
                                    "in a turn that has an answer"),
            "before any user turn": (self.with_system(answered, 0),
                                     "before any user turn"),
            "an answer after it": (self.with_system(answered, 1),
                                   "an answer after the turn's notice"),
        }
        for name, (records, words) in cases.items():
            with self.subTest(case=name):
                code, output = run_gate(self, {"s.json": records})
                self.assertEqual(code, 1)
                self.assertIn(words, output)
        options = {"continuation": "persistent", "response_shape": "one_shot"}

        class Silent(ScriptedStubLauncher):
            def _produce_turn(self, session, text):
                self._emit_agent_line(session, json.dumps({"type": "turn_complete"}))
        harness = support.harness({}, launcher=Silent(options))
        chat_id = harness.create_chat("A real notice")
        harness.send_turn(chat_id, "hello")
        records = harness.store.export_records()
        self.assertEqual(run_gate(self, {"s.json": records})[0], 0)
        twice = self.with_system(records, 2)
        code, output = run_gate(self, {"s.json": twice})
        self.assertEqual(code, 1)
        self.assertIn("a second system message in one turn", output)


# ---------------------------------------------------------------------------
# L2
# ---------------------------------------------------------------------------

class RunTestsExitsOnAPhaseThreeFailure(unittest.TestCase):
    """`run_tests.py` and `reclassify_stores.py --json`, each a separate program,
    with a store directory of their own so nothing here touches the suite's."""

    def run_program(self, argv, plant):
        base = tempfile.mkdtemp(prefix="dory-l2-")
        self.addCleanup(shutil.rmtree, base, True)
        env = dict(os.environ, DORY_TEST_STORE_DIR=os.path.join(base, "stores"),
                   TMPDIR=base, DORY_GATE_PLANT=plant)
        done = subprocess.run([sys.executable] + argv, cwd=support.REPO, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode("utf-8", "replace"), base

    def test_run_tests_exits_non_zero_when_phase_three_fails(self):
        run_tests = os.path.join(HERE, "run_tests.py")
        code, output, _ = self.run_program([run_tests, "_gate_plant.py"], "none")
        self.assertEqual(code, 0, output)
        self.assertIn("phase 4", output)
        for plant, words in (("text", "text differs from its cited event"),
                             ("system", "not the harness's fixed words"),
                             ("launcher", "UNREAD LAUNCHER")):
            with self.subTest(plant=plant):
                code, output, _ = self.run_program([run_tests, "_gate_plant.py"], plant)
                self.assertEqual(code, 1, output)
                self.assertIn("== phase 3", output)
                self.assertIn(words, output)
                self.assertNotIn("== phase 4", output)

    def test_reclassify_stores_json_exits_non_zero_as_a_program(self):
        run_tests = os.path.join(HERE, "run_tests.py")
        gate = os.path.join(HERE, "reclassify_stores.py")
        for plant, expected in (("none", 0), ("text", 1)):
            with self.subTest(plant=plant):
                _code, _output, base = self.run_program([run_tests, "_gate_plant.py"], plant)
                done = subprocess.run([sys.executable, gate, os.path.join(base, "stores"),
                                       "--json"], stdout=subprocess.PIPE)
                self.assertEqual(done.returncode, expected)
                result = json.loads(done.stdout.decode("utf-8"))
                self.assertEqual(len(result["rendering_unadjudicated"]), expected)


# ---------------------------------------------------------------------------
# F2
# ---------------------------------------------------------------------------

TRICKY_AGENT = r"""
import json, sys
TEXTS = %r
out = sys.stdout.buffer
def turn(instruction):
    for text in TEXTS:
        out.write(json.dumps({"type": "assistant_text", "text": text}).encode() + b"\n")
    out.write(b'{"type": "turn_complete"}\n')
    out.flush()
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read())
else:
    for line in sys.stdin.buffer:
        if line.strip():
            turn(line)
""" % (TRICKY_TEXTS,)


def agent_options(test, profile, source):
    base = tempfile.mkdtemp(prefix="dory-agent-")
    test.addCleanup(shutil.rmtree, base, True)
    path = os.path.join(base, "agent.py")
    with open(path, "w") as handle:
        handle.write(source)
    return {"profile": profile, "command": [sys.executable, path, profile]}


class ExactTextTheGateReads(unittest.TestCase, StoreCheck, ModelDirs):
    """Agent text with `\\r\\n`, a decomposed character and only whitespace, in kept
    stores of both wire formats (so phase 3 compares them) and served over HTTP."""

    def rows(self, messages):
        return [(m["author"], m["text"]) for m in messages]

    def expected(self, turn):
        return [("user", turn)] + [("agent", t) for t in TRICKY_TEXTS]

    def test_the_development_transport(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                options = agent_options(self, profile, TRICKY_AGENT)
                harness = support.harness({"launcher": "dev-local", "options": options})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Tricky %s" % profile)
                harness.send_turn(chat_id, "go")
                self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                                 self.expected("go"))
                self.assert_store_valid(harness.store, "f2-tricky-dev-%s" % profile)
                support.end_chat(harness, chat_id)
                root = support.scratch_root()
                shell = ShellProcess(root, launcher="dev-local", launcher_options=options)
                self.addCleanup(shell.kill)
                shell.start()
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                self.assertEqual(post(shell, path + "/messages", {"text": "go"})[0], 201)
                status, served = shell.raw("GET", path)
                self.assertEqual(self.rows(json.loads(served)["messages"]), self.expected("go"))
                status, listing = shell.raw("GET", "/api/chats")
                self.assertEqual(json.loads(listing)[0]["preview"], TRICKY_TEXTS[-1])
                shell.kill()
                self.keep(ChatStore(root, read_only=True), "f2-tricky-dev-http-%s" % profile)

    def test_the_codex_model(self):
        self.make_dirs()
        harness = support.harness({}, launcher=self.model(behaviour=("tricky-texts",)))
        chat_id = harness.create_chat("Tricky codex")
        harness.send_turn(chat_id, "go")
        self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                         [("user", "go"), ("agent", "answer to: go")]
                         + [("agent", t) for t in TRICKY_TEXTS])
        self.assert_store_valid(harness.store, "f2-tricky-codex")
        support.end_chat(harness, chat_id)
        self.make_dirs()
        root = support.scratch_root()
        shell, kill = model_shell(self, root, self.codex_home, self.spool, ("tricky-texts",))
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        self.assertEqual(post(shell, path + "/messages", {"text": "go"})[0], 201)
        status, served = shell.raw("GET", path)
        self.assertEqual(self.rows(json.loads(served)["messages"]),
                         [("user", "go"), ("agent", "answer to: go")]
                         + [("agent", t) for t in TRICKY_TEXTS])
        kill()
        self.keep(ChatStore(root, read_only=True), "f2-tricky-codex-http")


# ---------------------------------------------------------------------------
# L3
# ---------------------------------------------------------------------------

BIG_AGENT = r"""
import json, sys
out = sys.stdout.buffer
def turn(instruction):
    text = "x" * %d + " end"
    out.write(json.dumps({"type": "assistant_text", "text": text}).encode() + b"\n")
    out.write(b'{"type": "turn_complete"}\n')
    out.flush()
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read())
else:
    for line in sys.stdin.buffer:
        if line.strip():
            turn(line)
"""

BIG = (1 << 20) + 200 * 1024  # over 1 MiB


class ALineOfAnyLength(unittest.TestCase):

    def test_read_line_returns_a_long_line_whole(self):
        for size in (70 * 1024, BIG):
            with self.subTest(size=size):
                line = b"y" * size
                stream = io.BufferedReader(io.BytesIO(line + b"\nnext\n"), buffer_size=4096)
                self.assertEqual(dev_transport.read_line(stream), line)
                self.assertEqual(dev_transport.read_line(stream), b"next")
                self.assertIsNone(dev_transport.read_line(stream))

    def test_an_answer_over_a_mebibyte_is_preserved_and_rendered_whole(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                options = agent_options(self, profile, BIG_AGENT % BIG)
                harness = support.harness({"launcher": "dev-local", "options": options})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Big %s" % profile)
                harness.send_turn(chat_id, "go")
                text = "x" * BIG + " end"
                self.assertEqual([(a, t) for _, a, t in harness.transcript(chat_id)],
                                 [("user", "go"), ("agent", text)])
                session = support.view(harness).sessions_of(chat_id)[0]
                first = harness.store.read_all_events_of_session(
                    chat_id, session["session_id"])[0]
                self.assertEqual(json.loads(first["raw"]["body"])["text"], text)
                support.end_chat(harness, chat_id)


if __name__ == "__main__":
    unittest.main()
