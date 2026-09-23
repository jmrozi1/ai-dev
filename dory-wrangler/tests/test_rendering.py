"""Checkpoint `render-useful-events-in-chat`: what the user sees, from what arrived.

The rule (decision 0005): each recognized, agent-sourced event carrying non-empty
text becomes exactly one agent message whose text is exactly that event's text,
in event order; every other event renders nothing and is still preserved. No
new content type, author, record kind, or `system` message.

Measured, not read off the code, for both wire formats -- the development
transport (`dev-local`, a real process, with an agent program written here and
chosen by the launcher's `command` option) and the Codex JSONL model
(`internal_bridge.py`):

* **A6, exact text.** A message's text is byte-for-byte the text the format's
  classifier extracts from the cited event's preserved bytes -- leading and
  trailing whitespace and newlines included. `reclassify_stores.py` enforces the
  same over every kept store as phase 3 of `run_tests.py`; this file shows that
  gate failing on a planted store.
* **Zero or several text events per turn** render that many messages, in event
  order, each citing its own event, and fabricate nothing -- in process and over
  HTTP (`run_shell.py` for `dev-local`; `model_shell.py`, the product's own
  `build_server` hosting the model, for Codex, because the model is registered
  nowhere and `run_shell.py` cannot select it). The turn still completes, and a
  second turn sent while one is in flight is still refused, not queued.
* **The four acceptance cases** of #88 -- a normal text response, a non-text
  response type, an unknown type, and a malformed event -- as one named group.
* **The served page** inserts message text as text, never as HTML, and keeps its
  whitespace. There is no browser on this host (headless Chrome is disallowed),
  so this is proven from the page's served bytes and the JSON the page renders.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import support
from support import StoreCheck

import internal_bridge
import reclassify_stores
from dory_wrangler.errors import TurnInFlightRefused
from dory_wrangler.launchers import dev_transport
from dory_wrangler.launchers.dev_local import DEV_AGENT
from dory_wrangler.store import ChatStore
from dory_wrangler import webapp
from internal_bridge import InternalBridgeLauncher

HERE = os.path.dirname(os.path.abspath(__file__))

# A development-transport agent whose every turn says how many text events it
# produces: an instruction "N: words" gets N `assistant_text` lines, then
# `turn_complete`. Each text keeps whitespace and newlines around and inside it,
# which are part of the text. An instruction containing "[empty]" first emits one
# `assistant_text` whose text is "". An instruction containing "[park]" first creates
# `<gate>.parked` and waits up to a minute for `<gate>` to exist, so a test can
# act while that turn is in flight.
COUNTING_AGENT = r"""
import json, os, sys, time
out = sys.stdout.buffer
gate = sys.argv[2]
def part(i, words):
    return "\n  part %d of: %s\t\n  kept as written  " % (i, words)
def turn(instruction):
    instruction = instruction.strip()
    if "[park]" in instruction:
        open(gate + ".parked", "a").close()
        deadline = time.time() + 60
        while not os.path.exists(gate) and time.time() < deadline:
            time.sleep(0.02)
    count, words = instruction.split(":", 1)
    if "[empty]" in instruction:
        out.write(b'{"type": "assistant_text", "text": ""}\n')
    for i in range(1, int(count) + 1):
        out.write(json.dumps({"type": "assistant_text", "text": part(i, words)}).encode("utf-8")
                  + b"\n")
    out.write(b'{"type": "turn_complete"}\n')
    out.flush()
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read().decode("utf-8"))
else:
    for line in sys.stdin.buffer:
        if line.strip():
            turn(line.decode("utf-8"))
"""


def part(i, words):
    return "\n  part %d of: %s\t\n  kept as written  " % (i, words)


def expected_parts(instruction):
    """What `COUNTING_AGENT` answers to one user turn, which reaches it as sent."""
    count, words = instruction.strip().split(":", 1)
    return [part(i, words) for i in range(1, int(count) + 1)]


def raw_bytes(event):
    raw = event["raw"]
    if raw["encoding"] == "base64":
        return base64.b64decode(raw["body"])
    return raw["body"].encode("utf-8")


def agent_messages(records):
    return sorted((r for r in records if r["record_type"] == "message"
                   and r["author"] == "agent"), key=lambda m: m["sequence"])


def transcript(records):
    return [(m["author"], m["content"]["text"]) for m in sorted(
        (r for r in records if r["record_type"] == "message"), key=lambda m: m["sequence"])]


class Rendering(object):
    """The rule, checked over a whole store's records against a format's classifier."""

    def assert_rendered_exactly(self, records, interpret):
        """Every agent message cites an agent event whose bytes re-read as
        recognized `assistant_text` with exactly the message's text; every such
        event with non-empty text is cited by exactly one message; a session's
        messages cite its events in event order; nothing else is cited."""
        events = dict((r["event_id"], r) for r in records
                      if r["record_type"] == "diagnostic_event")
        cited = {}
        last = {}
        for message in agent_messages(records):
            event = events[message["source_event_id"]]
            self.assertEqual(event["source"], "agent")
            interpretation, interpreted_type, text = interpret(raw_bytes(event))
            self.assertEqual((interpretation, interpreted_type),
                             ("recognized", "assistant_text"))
            self.assertEqual(message["content"]["text"], text, "A6: not the event's text")
            self.assertEqual(message["content"]["content_type"], "text/plain")
            self.assertGreater(event["sequence"], last.get(event["session_id"], 0),
                               "messages out of event order")
            last[event["session_id"]] = event["sequence"]
            cited[event["event_id"]] = cited.get(event["event_id"], 0) + 1
        for event in events.values():
            if event["source"] != "agent":
                continue
            interpretation, interpreted_type, text = interpret(raw_bytes(event))
            if (interpretation, interpreted_type) == ("recognized", "assistant_text") and text:
                self.assertEqual(cited.get(event["event_id"]), 1,
                                 "a text event did not render exactly once: %r" % text)
            else:
                self.assertNotIn(event["event_id"], cited)


class ModelDirs(object):
    def make_dirs(self):
        base = tempfile.mkdtemp(prefix="dory-render-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    def model(self, **options):
        return InternalBridgeLauncher(self.codex_home, self.spool, **options)

    def calls(self):
        path = os.path.join(self.codex_home, "calls.jsonl")
        with open(path) as handle:
            return [json.loads(line) for line in handle]


def counting_options(test, profile):
    base = tempfile.mkdtemp(prefix="dory-counting-")
    test.addCleanup(shutil.rmtree, base, True)
    agent = os.path.join(base, "counting_agent.py")
    with open(agent, "w") as handle:
        handle.write(COUNTING_AGENT)
    gate = os.path.join(base, "gate")
    return {"profile": profile, "command": [sys.executable, agent, profile, gate]}, gate


def wait_for(path, seconds=30):
    deadline = time.time() + seconds
    while not os.path.exists(path):
        if time.time() > deadline:
            raise AssertionError("%s never appeared" % path)
        time.sleep(0.02)


def in_thread(action):
    """Start `action` in a thread; return a join that yields (value, error)."""
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as exc:  # noqa: BLE001 - returned to the caller
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join(seconds=60):
        thread.join(seconds)
        if thread.is_alive():
            raise AssertionError("the turn did not finish within %ss" % seconds)
        return result.get("value"), result.get("error")
    return join


def model_shell(test, root, codex_home, spool, behaviour):
    """The served application hosting the Codex model, as its own process."""
    from shellproc import ShellProcess
    port_file = os.path.join(root, ".model-port")
    process = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "model_shell.py"), "--root", root,
         "--port-file", port_file, "--codex-home", codex_home, "--spool", spool,
         "--behaviour", ",".join(behaviour)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def kill():
        if process.poll() is None:
            process.kill()
        process.wait()
    test.addCleanup(kill)
    wait_for(port_file)
    shell = ShellProcess(root)
    shell.process = process
    with open(port_file) as handle:
        shell.port = int(handle.read())
    return shell, kill


def post(shell, path, payload):
    """(status, decoded body) for a POST, an error status included."""
    status, body = shell.raw("POST", path, payload)
    return status, json.loads(body)


# ---------------------------------------------------------------------------
# C. Zero or several text events per turn.
# ---------------------------------------------------------------------------

class ZeroOrSeveralTextEventsPerTurn(unittest.TestCase, StoreCheck, Rendering, ModelDirs):
    """Several text events render that many messages, in event order, each citing
    its own event; none renders none and fabricates nothing; the turn completes
    either way, and a turn in flight still refuses the next rather than queueing
    it."""

    TURNS = ("2: first question", "0: a turn that says nothing", "3: third question")

    def expected(self, turns):
        rows = []
        for text in turns:
            rows.append(("user", text))
            rows.extend(("agent", t) for t in expected_parts(text))
        return rows

    def test_the_development_transport_in_process(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                options, _gate = counting_options(self, profile)
                harness = support.harness({"launcher": "dev-local", "options": options})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Counting, %s" % profile)
                for text in self.TURNS:
                    outcome = harness.send_turn(chat_id, text)
                    self.assertEqual(len(outcome.agent_message_ids), len(expected_parts(text)))
                records = harness.store.export_records()
                self.assertEqual(transcript(records), self.expected(self.TURNS))
                self.assert_rendered_exactly(records, dev_transport.interpret)
                states = [s["state"] for s in support.view(harness).sessions_of(chat_id)]
                self.assertEqual(states, ["completed"] * 3 if profile == "one_shot"
                                 else ["running"], "every turn completed")
                self.assert_store_valid(harness.store, "render-counting-dev-local-%s" % profile)
                support.end_chat(harness, chat_id)

    def test_an_event_with_empty_text_is_preserved_and_renders_nothing(self):
        """"Text-bearing" means non-empty text: a recognized `assistant_text`
        whose text is "" is kept, recognized, and cited by nothing -- the store
        refuses a message with no text, and an empty message would show the user
        nothing. Both formats read it that way; the Codex one from the classifier."""
        options, _gate = counting_options(self, "one_shot")
        harness = support.harness({"launcher": "dev-local", "options": options})
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Empty text")
        outcome = harness.send_turn(chat_id, "1: [empty] one answer")
        self.assertEqual(len(outcome.agent_message_ids), 1)
        records = harness.store.export_records()
        self.assertEqual(transcript(records), self.expected(("1: [empty] one answer",)))
        empty = [e for e in records if e["record_type"] == "diagnostic_event"
                 and raw_bytes(e) == b'{"type": "assistant_text", "text": ""}']
        self.assertEqual([(e["interpretation"], e["interpreted_type"]) for e in empty],
                         [("recognized", "assistant_text")])
        self.assert_rendered_exactly(records, dev_transport.interpret)
        self.assertNotIn(empty[0]["event_id"],
                         set(m["source_event_id"] for m in agent_messages(records)))
        codex = internal_bridge.classify(
            1, b'{"type": "item.completed", "item": {"type": "agent_message", "text": ""}}')
        self.assertEqual((codex.interpretation, codex.interpreted_type, codex.text),
                         ("recognized", "assistant_text", ""))
        self.assertFalse(codex.is_chat_text)
        self.assert_store_valid(harness.store, "render-empty-text-event")

    def test_the_codex_model_in_process(self):
        """The model reads its behaviour from its environment on every call, so
        changing it between turns is changing what the next call's agent does."""
        self.make_dirs()
        launcher = self.model(behaviour=("two-messages", "padded-message"))
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Codex, several and none")
        first = harness.send_turn(chat_id, "first")
        launcher._behaviour = ("no-agent-message",)
        second = harness.send_turn(chat_id, "second")
        launcher._behaviour = ("two-messages",)
        third = harness.send_turn(chat_id, "third")
        self.assertEqual([len(o.agent_message_ids) for o in (first, second, third)], [2, 0, 2])
        records = harness.store.export_records()
        self.assertEqual(transcript(records), [
            ("user", "first"), ("agent", "\n  \tanswer to: first  \n\n \t"),
            ("agent", "and a second message"),
            ("user", "second"),
            ("user", "third"), ("agent", "answer to: third"),
            ("agent", "and a second message")])
        self.assert_rendered_exactly(records, internal_bridge.interpret)
        thread_id = self.calls()[0]["thread_id"]
        self.assertEqual([c["argv"][0] for c in self.calls()],
                         ["first", "--resumeID=%s" % thread_id, "--resumeID=%s" % thread_id],
                         "the turn with no text was delivered and the next resumed after it")
        self.assertEqual([s["state"] for s in support.view(harness).sessions_of(chat_id)],
                         ["running"])
        self.assert_store_valid(harness.store, "render-codex-several-and-none")
        support.end_chat(harness, chat_id)

    def test_a_turn_with_no_text_in_flight_still_refuses_the_next_in_process(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                options, gate = counting_options(self, profile)
                harness = support.harness({"launcher": "dev-local", "options": options})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("In flight, %s" % profile)
                join = in_thread(lambda: harness.send_turn(chat_id, "0: [park] quiet"))
                wait_for(gate + ".parked")
                before = harness.store.export_records()
                _value, error = in_thread(lambda: harness.send_turn(chat_id, "2: refused"))(20)
                self.assertIsInstance(error, TurnInFlightRefused)
                self.assertEqual(harness.store.export_records(), before, "a refused turn wrote")
                open(gate, "w").close()
                outcome, error = join()
                self.assertIsNone(error)
                self.assertEqual(outcome.agent_message_ids, [])
                harness.send_turn(chat_id, "2: after")
                self.assertEqual(transcript(harness.store.export_records()),
                                 self.expected(("0: [park] quiet", "2: after")))
                support.end_chat(harness, chat_id)

    def test_the_development_transport_over_http_through_run_shell(self):
        from shellproc import ShellProcess
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                options, gate = counting_options(self, profile)
                root = support.scratch_root()
                shell = ShellProcess(root, launcher="dev-local", launcher_options=options)
                self.addCleanup(shell.kill)
                shell.start()
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                turns = ("2: first question", "0: [park] a turn that says nothing",
                         "3: third question")
                self.assertEqual(post(shell, path + "/messages", {"text": turns[0]})[0], 201)
                join = in_thread(lambda: post(shell, path + "/messages", {"text": turns[1]}))
                wait_for(gate + ".parked")
                reader = ChatStore(root, read_only=True)
                before = reader.export_records()
                self.assertEqual(post(shell, path + "/messages", {"text": "2: refused"}),
                                 (409, {"error": webapp.REFUSED_IN_FLIGHT, "refused": True}))
                self.assertEqual(reader.export_records(), before, "a refused turn wrote")
                open(gate, "w").close()
                (status, body), error = join()
                self.assertIsNone(error)
                self.assertEqual(status, 201)
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                 self.expected(turns[:2]))
                status, body = post(shell, path + "/messages", {"text": turns[2]})
                self.assertEqual(status, 201)
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                 self.expected(turns))
                status, served = shell.raw("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual([(m["author"], m["text"])
                                  for m in json.loads(served)["messages"]], self.expected(turns))
                # The list's preview is the last message's text exactly, too.
                status, listing = shell.raw("GET", "/api/chats")
                self.assertEqual(json.loads(listing)[0]["preview"],
                                 expected_parts(turns[2])[-1])
                shell.kill()
                store = ChatStore(root, read_only=True)
                self.assertEqual(store.verify(), [])
                records = store.export_records()
                self.assert_rendered_exactly(records, dev_transport.interpret)
                self.assertEqual([r["state"] for r in records
                                  if r["record_type"] == "agent_session"],
                                 ["completed"] * 3 if profile == "one_shot" else ["running"])
                self.keep(store, "render-counting-http-dev-local-%s" % profile)

    def test_the_codex_model_over_http(self):
        cases = (("no-agent-message", "park"), ("two-messages", "padded-message"))
        for behaviour in cases:
            with self.subTest(behaviour=behaviour):
                self.make_dirs()
                root = support.scratch_root()
                shell, kill = model_shell(self, root, self.codex_home, self.spool, behaviour)
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                if "park" in behaviour:
                    join = in_thread(lambda: post(shell, path + "/messages", {"text": "first"}))
                    wait_for(os.path.join(self.codex_home, "parked"))
                    reader = ChatStore(root, read_only=True)
                    before = reader.export_records()
                    self.assertEqual(post(shell, path + "/messages", {"text": "refused"}),
                                     (409, {"error": webapp.REFUSED_IN_FLIGHT, "refused": True}))
                    self.assertEqual(reader.export_records(), before, "a refused turn wrote")
                    open(os.path.join(self.codex_home, "release"), "w").close()
                    (status, body), error = join()
                    self.assertIsNone(error)
                else:
                    status, body = post(shell, path + "/messages", {"text": "first"})
                self.assertEqual(status, 201)
                status, body = post(shell, path + "/messages", {"text": "second"})
                self.assertEqual(status, 201)
                if "no-agent-message" in behaviour:
                    expected = [("user", "first"), ("user", "second")]
                else:
                    expected = [row for t in ("first", "second") for row in (
                        ("user", t), ("agent", "\n  \tanswer to: %s  \n\n \t" % t),
                        ("agent", "and a second message"))]
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]], expected)
                thread_id = self.calls()[0]["thread_id"]
                self.assertEqual([c["argv"][0] for c in self.calls()],
                                 ["first", "--resumeID=%s" % thread_id])
                kill()
                store = ChatStore(root, read_only=True)
                self.assertEqual(store.verify(), [])
                records = store.export_records()
                self.assertEqual(transcript(records), expected)
                self.assert_rendered_exactly(records, internal_bridge.interpret)
                self.assertEqual([r["state"] for r in records
                                  if r["record_type"] == "agent_session"], ["running"])
                self.keep(store, "render-codex-http-%s" % "-".join(behaviour))


# ---------------------------------------------------------------------------
# D. The four acceptance cases.
# ---------------------------------------------------------------------------

class TheFourAcceptanceCases(unittest.TestCase, StoreCheck, Rendering, ModelDirs):
    """#88's acceptance criterion: a normal text response, a non-text response
    type, an unknown type, and a malformed event, each on the same turn as a text
    answer, in both wire formats. Only the answer renders; every one of them is
    preserved, byte for byte, with the reading its bytes give.

    Where else each case already lives: normal text in every transcript test
    (`test_mode_invariance`); non-text `turn_complete` and `thread.started` in
    `test_classification.ClassificationIsAFunctionOfThePreservedBytes`; unknown
    and malformed in `test_classification.UnrecognizedAndMalformedAreNeverChat`.
    """

    def dev_turn(self):
        harness = support.harness({"launcher": "dev-local", "options": {
            "profile": "one_shot",
            "command": [sys.executable, DEV_AGENT, "--profile", "one_shot",
                        "--garbage", "--unknown-type"]}})
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Four cases, development transport")
        harness.send_turn(chat_id, "hello")
        return harness.store.export_records(), dev_transport.interpret, "answer to: hello"

    def codex_turn(self):
        self.make_dirs()
        harness = support.harness({}, launcher=self.model(
            behaviour=("synthetic-unrecognized", "synthetic-malformed")))
        chat_id = harness.create_chat("Four cases, Codex model")
        harness.send_turn(chat_id, "hello")
        records = harness.store.export_records()
        support.end_chat(harness, chat_id)
        return records, internal_bridge.interpret, "answer to: hello"

    def formats(self):
        return (("development transport", self.dev_turn),
                ("codex model", self.codex_turn))

    def agent_events(self, records):
        return sorted((r for r in records if r["record_type"] == "diagnostic_event"
                       and r["source"] == "agent"), key=lambda e: e["sequence"])

    def only_the_answer(self, records, interpret, answer):
        self.assertEqual(transcript(records), [("user", "hello"), ("agent", answer)])
        self.assert_rendered_exactly(records, interpret)
        for event in self.agent_events(records):
            self.assertEqual((event["interpretation"], event["interpreted_type"]),
                             tuple(interpret(raw_bytes(event))[:2]),
                             "preserved with the reading its bytes give")

    def cited(self, records):
        return set(m["source_event_id"] for m in agent_messages(records))

    def test_a_normal_text_response(self):
        for name, turn in self.formats():
            with self.subTest(format=name):
                records, interpret, answer = turn()
                self.only_the_answer(records, interpret, answer)
                texts = [e for e in self.agent_events(records)
                         if e["interpreted_type"] == "assistant_text"]
                self.assertEqual(len(texts), 1)
                self.assertEqual(self.cited(records), {texts[0]["event_id"]})

    def test_a_non_text_response_type(self):
        non_text = {"development transport": ("turn_complete", b'{"type": "turn_complete"}'),
                    "codex model": ("thread.started", None)}
        for name, turn in self.formats():
            with self.subTest(format=name):
                records, interpret, answer = turn()
                self.only_the_answer(records, interpret, answer)
                kind, body = non_text[name]
                found = [e for e in self.agent_events(records) if e["interpreted_type"] == kind]
                self.assertEqual(len(found), 1, "preserved, recognized")
                self.assertEqual(found[0]["interpretation"], "recognized")
                if body is not None:
                    self.assertEqual(raw_bytes(found[0]), body)
                self.assertNotIn(found[0]["event_id"], self.cited(records))

    def test_an_unknown_type(self):
        for name, turn in self.formats():
            with self.subTest(format=name):
                records, interpret, answer = turn()
                self.only_the_answer(records, interpret, answer)
                found = [e for e in self.agent_events(records)
                         if e["interpretation"] == "unrecognized"]
                self.assertEqual(len(found), 1, "preserved")
                self.assertIn(b"agent_thinking" if name == "development transport"
                              else b"synthetic.model-only.not-a-codex-event",
                              raw_bytes(found[0]))
                self.assertNotIn(found[0]["event_id"], self.cited(records))

    def test_a_malformed_event(self):
        for name, turn in self.formats():
            with self.subTest(format=name):
                records, interpret, answer = turn()
                self.only_the_answer(records, interpret, answer)
                found = [e for e in self.agent_events(records)
                         if e["interpretation"] == "malformed"]
                self.assertEqual(len(found), 1, "preserved")
                self.assertEqual(raw_bytes(found[0]),
                                 b"this is not json at all {{{" if name == "development transport"
                                 else b"SYNTHETIC MODEL-ONLY LINE: deliberately not JSON {{{")
                self.assertNotIn(found[0]["event_id"], self.cited(records))

    def test_all_four_over_http(self):
        from shellproc import ShellProcess
        root = support.scratch_root()
        shell = ShellProcess(root, launcher="dev-local", launcher_options={
            "profile": "one_shot",
            "command": [sys.executable, DEV_AGENT, "--profile", "one_shot",
                        "--garbage", "--unknown-type"]})
        self.addCleanup(shell.kill)
        shell.start()
        self.make_dirs()
        codex_root = support.scratch_root()
        codex, kill = model_shell(self, codex_root, self.codex_home, self.spool,
                                  ("synthetic-unrecognized", "synthetic-malformed"))
        for name, served, store_root, interpret in (
                ("development transport", shell, root, dev_transport.interpret),
                ("codex model", codex, codex_root, internal_bridge.interpret)):
            with self.subTest(format=name):
                _status, chat = served.post("/api/chats", {})
                status, body = post(served, "/api/chats/%s/messages" % chat["chat_id"],
                                    {"text": "hello"})
                self.assertEqual(status, 201)
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                 [("user", "hello"), ("agent", "answer to: hello")])
                store = ChatStore(store_root, read_only=True)
                records = store.export_records()
                self.only_the_answer(records, interpret, "answer to: hello")
                readings = sorted((e["interpretation"], e["interpreted_type"] or "")
                                  for e in self.agent_events(records))
                non_text = "turn_complete" if name == "development transport" else "thread.started"
                self.assertEqual(readings, sorted([
                    ("recognized", "assistant_text"), ("recognized", non_text),
                    ("unrecognized", ""), ("malformed", "")]))


# ---------------------------------------------------------------------------
# B. The A6 gate fails on a planted store.
# ---------------------------------------------------------------------------

class TheWholeStoreRenderingGate(unittest.TestCase, ModelDirs):
    """`reclassify_stores.py`, phase 3 of `run_tests.py`, exits 1 when an agent
    message's text is not exactly its cited event's text -- by one character, or
    by trailing whitespace alone -- and when a text event renders never or twice,
    in both wire formats; unmodified, it exits 0."""

    def run_gate(self, records, adjudicate=(), as_json=False):
        base = tempfile.mkdtemp(prefix="dory-render-gate-")
        self.addCleanup(shutil.rmtree, base, True)
        with open(os.path.join(base, "planted.json"), "w") as out:
            json.dump({"records": records}, out)
        output = io.StringIO()
        from unittest import mock
        with mock.patch.dict(reclassify_stores.ADJUDICATED_MESSAGES,
                             dict((key, "planted") for key in adjudicate)), \
                contextlib.redirect_stdout(output):
            return (reclassify_stores.main([base] + (["--json"] if as_json else [])),
                    output.getvalue())

    def stores(self):
        dev = support.harness({"launcher": "scripted-stub", "options": {}})
        chat_id = dev.create_chat("Gate, development transport")
        dev.send_turn(chat_id, "hello")
        self.make_dirs()
        codex = support.harness({}, launcher=self.model(behaviour=("padded-message",)))
        codex_chat = codex.create_chat("Gate, codex model")
        codex.send_turn(codex_chat, "hello")
        records = codex.store.export_records()
        support.end_chat(codex, codex_chat)
        return (("development transport", dev.store.export_records()),
                ("codex model", records))

    @staticmethod
    def with_text(records, change):
        changed = json.loads(json.dumps(records))
        message = agent_messages(changed)[0]
        message["content"]["text"] = change(message["content"]["text"])
        return changed, message

    def test_it_fails_on_one_character_and_on_trailing_whitespace_alone(self):
        for name, records in self.stores():
            with self.subTest(format=name):
                self.assertEqual(self.run_gate(records)[0], 0)
                one_char, message = self.with_text(records, lambda t: t[:-1] + (
                    "X" if t[-1] != "X" else "Y"))
                code, output = self.run_gate(one_char)
                self.assertEqual(code, 1)
                self.assertIn("text differs from its cited event", output)
                trailing, _ = self.with_text(records, lambda t: t + " ")
                self.assertEqual(self.run_gate(trailing)[0], 1)
                # The --json form exits the same way, and names the disagreement.
                code, output = self.run_gate(trailing, as_json=True)
                self.assertEqual(code, 1)
                self.assertEqual(len(json.loads(output)["rendering_unadjudicated"]), 1)
                self.assertEqual(self.run_gate(records, as_json=True)[0], 0)
                stripped, _ = self.with_text(records, lambda t: t.strip() or t + "\n")
                self.assertEqual(self.run_gate(stripped)[0],
                                 0 if name == "development transport" else 1,
                                 "the codex answer is padded, so stripping it is a change")
                self.assertEqual(
                    self.run_gate(one_char, [("planted.json", message["sequence"],
                                              agent_messages(one_char)[0]["content"]["text"])])[0],
                    0, "a name covers exactly its record")
                self.assertEqual(
                    self.run_gate(one_char, [("planted.json", message["sequence"] + 1,
                                              agent_messages(one_char)[0]["content"]["text"])])[0],
                    1)

    def test_it_fails_on_a_text_event_rendered_never_or_twice(self):
        for name, records in self.stores():
            with self.subTest(format=name):
                message = agent_messages(records)[0]
                never = [r for r in records if r.get("message_id") != message["message_id"]]
                code, output = self.run_gate(never)
                self.assertEqual(code, 1)
                self.assertIn("rendered 0 time(s)", output)
                twice = json.loads(json.dumps(records))
                copy = json.loads(json.dumps(message))
                copy["message_id"] = "msg_" + "0" * 26
                copy["sequence"] = max(r["sequence"] for r in twice
                                       if r["record_type"] == "message") + 1
                twice.append(copy)
                code, output = self.run_gate(twice)
                self.assertEqual(code, 1)
                self.assertIn("rendered 2 time(s)", output)
                self.assertIn("out of event order", output)


# ---------------------------------------------------------------------------
# E. The served page shows message text as plain text.
# ---------------------------------------------------------------------------

MARKUP = '<script>alert("x")</script> &amp; <b>bold</b>'


class TheServedPageShowsMessageTextAsText(unittest.TestCase):
    """From the page's served bytes and the JSON it renders: no browser here."""

    def test_the_page_inserts_message_text_only_through_text_content(self):
        page = webapp.PAGE
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                    "createContextualFragment", "DOMParser", "srcdoc", "eval("):
            self.assertNotIn(api, page)
        element = re.search(r"function element\(tag, className, text\) \{.*?\n\}", page, re.S)
        self.assertIsNotNone(element)
        self.assertIn("node.textContent = text;", element.group(0))
        # Both places message text reaches the DOM go through `element`: the
        # transcript (initial load, after a send, after Abandon -- all call
        # renderChat with the chat the server returned) and the list preview.
        self.assertIn('turn.appendChild(element("div", "bubble", message.text));', page)
        self.assertIn('item.appendChild(element("span", "chat-preview", chat.preview '
                      '|| "No messages yet"));', page)
        self.assertEqual(page.count("message.text"), 1)
        self.assertEqual(page.count("chat.preview"), 1)
        self.assertEqual(len(re.findall(r"renderChat\(chat\)", page)), 4)
        # Whitespace is kept: pre-wrap keeps runs of spaces, tabs and newlines.
        self.assertRegex(page, r"\.bubble \{ white-space: pre-wrap;")

    def test_markup_and_whitespace_are_served_literally(self):
        from shellproc import ShellProcess
        root = support.scratch_root()
        shell = ShellProcess(root, launcher="scripted-stub")
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        status, _body = shell.raw("POST", path + "/messages", {"text": MARKUP})
        self.assertEqual(status, 201)
        status, body = shell.raw("GET", path)
        self.assertEqual(status, 200)
        # The JSON carries the characters themselves: `json.dumps` escapes none
        # of < > &, and the page puts the decoded string into textContent.
        self.assertIn('"text": "answer to: <script>alert(\\"x\\")</script> &amp; <b>bold</b>"',
                      body)
        self.assertEqual([m["text"] for m in json.loads(body)["messages"]],
                         [MARKUP, "answer to: " + MARKUP])
        status, listing = shell.raw("GET", "/api/chats")
        self.assertEqual(json.loads(listing)[0]["preview"], "answer to: " + MARKUP)
        _status, page = shell.get_page()
        self.assertEqual(page, webapp.PAGE, "the page is static; no message text is in it")


if __name__ == "__main__":
    unittest.main()
