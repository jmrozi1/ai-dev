"""Checkpoint `classify-known-event-types`: one explicit set per wire format.

Three claims, each measured rather than read off the code:

* **Declared once.** Each wire format's recognized set and its mapping are data
  in exactly one place -- `launchers/dev_transport.RECOGNIZED` for the
  development transport that `dev-local` and `scripted-stub` share, and
  `internal_bridge.RECOGNIZED` for the Codex JSONL model -- and every classifier
  of that format reads it. Shown by changing the declaration and watching every
  path that format reaches change with it: both development launchers, and the
  model's launch, resume and failed-launch paths.
* **A function of the preserved bytes.** Re-reading every preserved event's
  `raw.body` with its launcher's classifier reproduces the recorded
  `interpretation` and `interpreted_type` exactly. `reclassify_stores.py` runs
  the same check over every store the suite keeps.
* **Never chat.** An `unrecognized` or `malformed` payload -- including one
  whose bytes look like an answer but lack the field that makes them one --
  yields no transcript message, in the store and over HTTP through
  `run_shell.py` with a shipped launcher chosen by configuration alone.

Nothing here adds a recognized type. Rendering is the next checkpoint and is not
tested here beyond "it is not chat".
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import support
from support import StoreCheck, THREE_TURNS

import internal_bridge
import reclassify_stores
from dory_wrangler import launch_boundary as lb
from dory_wrangler.errors import ConcurrentLaunchRefused
from dory_wrangler.launchers import dev_transport
from dory_wrangler.launchers.dev_local import DEV_AGENT, DevLocalLauncher
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
from dory_wrangler.store import ChatStore
from internal_bridge import InternalBridgeLauncher

# The development agent's probe lines: unparseable, a type nobody declared, and
# two that look like an answer and are not (`--lookalike`).
PROBE_FLAGS = ("--garbage", "--unknown-type", "--lookalike")
LOOKALIKE = "LOOKALIKE"


def dev_local_options(profile):
    return {"profile": profile,
            "command": [sys.executable, DEV_AGENT, "--profile", profile] + list(PROBE_FLAGS)}


def reading(payload):
    return (payload.interpretation, payload.interpreted_type, payload.text)


def instruction(text="hello"):
    return lb.LaunchInstruction(request_id="req_x", chat_id="cht_x", session_id="ses_x",
                                created_at="2026-09-23T00:00:00.000000Z",
                                instruction_encoding="utf-8", instruction_text=text)


def delivery(text="again"):
    return lb.DeliveryInstruction(delivery_id="dlv_x", chat_id="cht_x", session_id="ses_x",
                                  sequence=2, created_at="2026-09-23T00:00:00.000000Z",
                                  instruction_encoding="utf-8", instruction_text=text)


class ModelDirs(object):
    def make_dirs(self):
        base = tempfile.mkdtemp(prefix="dory-classify-")
        self.addCleanup(shutil.rmtree, base, True)
        self.codex_home = os.path.join(base, "codex-home")
        self.spool = os.path.join(base, "spool")
        os.makedirs(self.codex_home)

    def model(self, **options):
        return InternalBridgeLauncher(self.codex_home, self.spool, **options)


class EachWireFormatDeclaresItsSetOnce(unittest.TestCase, ModelDirs):

    def test_the_development_transport_recognizes_exactly_two_types(self):
        self.assertEqual(
            dict(dev_transport.RECOGNIZED),
            {"assistant_text": (lb.PAYLOAD_ASSISTANT_TEXT, "text"),
             "turn_complete": (lb.PAYLOAD_TURN_COMPLETE, None)})

    def test_the_codex_model_recognizes_exactly_the_two_proven_types(self):
        self.assertEqual(
            dict(internal_bridge.RECOGNIZED),
            {("thread.started", None): ("thread.started", ("thread_id",), True, False),
             ("item.completed", "agent_message"):
                 (lb.PAYLOAD_ASSISTANT_TEXT, ("item", "text"), False, True)})

    def test_both_development_launchers_read_the_one_declaration(self):
        """Take `turn_complete` out of the declaration and both launchers stop
        recognizing it; put a new type in and both start. Neither has a set of
        its own that could survive the change."""
        line = b'{"type": "turn_complete"}'
        novel = b'{"type": "agent_thinking"}'

        def dev_local_reads(raw):
            session = type("S", (), {"next_sequence": 1})()
            return reading(DevLocalLauncher._agent_payload(None, session, raw.decode() + "\n"))

        def stub_reads(raw):
            stub = ScriptedStubLauncher({})
            session = type("S", (), {"next_sequence": 1, "payloads": []})()
            return reading(stub._emit_agent_line(session, raw))

        for reads in (dev_local_reads, stub_reads):
            self.assertEqual(reads(line), ("recognized", "turn_complete", None))
            self.assertEqual(reads(novel), ("unrecognized", None, None))
        with mock.patch.dict(dev_transport.RECOGNIZED, clear=False):
            del dev_transport.RECOGNIZED["turn_complete"]
            dev_transport.RECOGNIZED["agent_thinking"] = dev_transport.Recognized("probe", None)
            for reads in (dev_local_reads, stub_reads):
                self.assertEqual(reads(line), ("unrecognized", None, None), reads.__name__)
                self.assertEqual(reads(novel), ("recognized", "probe", None), reads.__name__)

    def test_the_stub_s_own_turn_goes_through_the_declaration(self):
        """Not only a helper: the lines the stub produces for a real turn."""
        with mock.patch.dict(dev_transport.RECOGNIZED, clear=False):
            del dev_transport.RECOGNIZED["assistant_text"]
            stub = ScriptedStubLauncher({"continuation": "persistent", "response_shape": "stream"})
            handle = stub.launch(instruction()).agent_handle
            stub.deliver(handle, delivery())
            page = stub.events(handle, 0)
        self.assertEqual([(p.interpretation, p.interpreted_type) for p in page.payloads],
                         [("unrecognized", None), ("recognized", "turn_complete")] * 2)

    def test_the_codex_model_reads_its_one_declaration_on_every_path(self):
        """Declare the model's synthetic type and every path that reads the
        format -- launch, resume, and a launch that started no thread --
        recognizes it; nothing else in the model has a set of its own."""
        self.make_dirs()
        synthetic = ("synthetic.model-only.not-a-codex-event", None)
        with mock.patch.dict(internal_bridge.RECOGNIZED, clear=False):
            internal_bridge.RECOGNIZED[synthetic] = internal_bridge.Recognized(
                "probe", ("note",), False, False)
            model = self.model(behaviour=("synthetic-unrecognized",))
            handle = model.launch(instruction()).agent_handle
            model.deliver(handle, delivery())
            page = model.events(handle, 0)
            with self.assertRaises(lb.LauncherError) as failed:
                self.model(behaviour=("synthetic-unrecognized", "no-thread-started")).launch(
                    instruction())
        kinds = [p.interpreted_type for p in page.payloads]
        self.assertEqual(kinds, ["thread.started", "probe", lb.PAYLOAD_ASSISTANT_TEXT,
                                 "thread.started", "probe", lb.PAYLOAD_ASSISTANT_TEXT],
                         "launch and resume both read the declaration")
        self.assertEqual([p.interpreted_type for p in failed.exception.payloads],
                         ["probe", lb.PAYLOAD_ASSISTANT_TEXT, None],
                         "a failed launch's lines read it too; the observation stays unknown")
        self.assertEqual(set(p.source for p in failed.exception.payloads), {"launcher"})

    def test_launch_and_resume_read_the_same_line_the_same_way(self):
        """No resumed-turn special case: every line a resume printed reads
        exactly as the same line printed by a launch."""
        self.make_dirs()
        behaviour = ("synthetic-unrecognized", "synthetic-malformed", "synthetic-item",
                     "agent-message-without-text", "synthetic-typeless",
                     "synthetic-not-an-object", "synthetic-padded")
        model = self.model(behaviour=behaviour)
        handle = model.launch(instruction("same")).agent_handle
        launched = len(model.events(handle, 0).payloads)
        model.deliver(handle, delivery("same"))
        page = model.events(handle, 0).payloads
        first, second = page[:launched], page[launched:]
        self.assertEqual([(p.raw, reading(p)) for p in first],
                         [(p.raw, reading(p)) for p in second])
        self.assertEqual(sorted(set(p.interpretation for p in first)),
                         ["malformed", "recognized", "unrecognized"])


class ClassificationIsAFunctionOfThePreservedBytes(unittest.TestCase, StoreCheck, ModelDirs):
    """Every event each launcher in the repository causes to be preserved,
    re-read from `raw.body` by that launcher's classifier, reproduces what was
    recorded. Launcher-synthesised records are counted apart, and checked
    against their own bytes."""

    def reproduce(self, harness, name):
        records = harness.store.export_records()
        sessions = dict((r["session_id"], r) for r in records
                        if r["record_type"] == "agent_session")
        rows = {"reclassified": 0, "synthesised": 0}
        for event in (r for r in records if r["record_type"] == "diagnostic_event"):
            session = sessions[event["session_id"]]
            row, expected = reclassify_stores.row_for(event, session)
            recorded = (event["interpretation"], event["interpreted_type"])
            if row == "reclassified":
                self.assertEqual(reclassify_stores.declined(tuple(expected), session), recorded,
                                 "sequence %d of %s" % (event["sequence"], name))
            else:
                self.assertEqual(row, "synthesised")
                self.assertTrue(reclassify_stores.synthesised_consistent(event, session),
                                (name, event["sequence"], recorded))
            rows[row] += 1
        self.assertGreater(rows["reclassified"], 0)
        self.assert_store_valid(harness.store, name)
        return records

    def interpretations(self, records):
        return set(r["interpretation"] for r in records if r["record_type"] == "diagnostic_event")

    def test_dev_local_in_both_profiles(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                harness = support.harness({"launcher": "dev-local",
                                           "options": dev_local_options(profile)})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Re-read, dev-local %s" % profile)
                for text in THREE_TURNS[:2]:
                    harness.send_turn(chat_id, text)
                records = self.reproduce(harness, "classify-reproduces-dev-local-%s" % profile)
                self.assertEqual(self.interpretations(records),
                                 {"recognized", "unrecognized", "malformed"})

    def test_scripted_stub_under_every_capability_and_ending(self):
        combos = [("fresh_binding", "one_shot", None), ("fresh_binding", "stream", None),
                  ("persistent", "stream", "turn_complete"), ("persistent", "one_shot", None),
                  ("fresh_binding", "stream", "session_failed"),
                  ("fresh_binding", "stream", "stream_end")]
        for continuation, shape, ending in combos:
            with self.subTest(continuation=continuation, shape=shape, ending=ending):
                options = {"continuation": continuation, "response_shape": shape,
                           "garbage": True, "unknown_type": True}
                if ending:
                    options["end_of_turn"] = ending
                harness = support.harness({"launcher": "scripted-stub", "options": options})
                chat_id = harness.create_chat("Re-read, stub")
                harness.send_turn(chat_id, THREE_TURNS[0])
                if ending == "stream_end":
                    # The session is `unknown` now, and the chat refuses a turn.
                    with self.assertRaises(ConcurrentLaunchRefused):
                        harness.send_turn(chat_id, THREE_TURNS[1])
                else:
                    harness.send_turn(chat_id, THREE_TURNS[1])
                self.reproduce(harness, "classify-reproduces-stub-%s-%s-%s"
                               % (continuation, shape, ending))

    def test_a_one_shot_stub_s_declined_end_of_stream_is_the_synthesised_row(self):
        """The one record the harness writes differently from the launcher's
        reading -- the human's 2026-09-15 decision -- is launcher-synthesised,
        and is consistent with its own bytes once that decision is applied."""
        harness = support.harness({"launcher": "scripted-stub",
                                   "options": {"response_shape": "one_shot",
                                               "end_of_turn": "stream_end"}})
        chat_id = harness.create_chat("Declined end of stream")
        with self.assertRaises(lb.LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        records = self.reproduce(harness, "classify-reproduces-declined-stream-end")
        last = [r for r in records if r["record_type"] == "diagnostic_event"][-1]
        self.assertEqual((last["source"], last["interpretation"], last["interpreted_type"],
                          last["raw"]["body"]),
                         ("launcher", "unrecognized", None, '{"type": "stream_end"}'))

    def test_the_codex_model_on_launch_resume_and_a_failed_launch(self):
        self.make_dirs()
        behaviour = ("synthetic-unrecognized", "synthetic-malformed", "synthetic-item",
                     "agent-message-without-text", "synthetic-typeless")
        harness = support.harness({}, launcher=self.model(behaviour=behaviour))
        chat_id = harness.create_chat("Re-read, codex model")
        for text in THREE_TURNS:
            harness.send_turn(chat_id, text)
        failed = support.harness({}, launcher=self.model(
            behaviour=behaviour + ("no-thread-started",)))
        failed_chat = failed.create_chat("Re-read, codex model, no thread")
        failed.send_turn(failed_chat, "hello")
        fresh = support.harness({}, launcher=self.model(
            continuation=lb.CONTINUATION_FRESH_BINDING, behaviour=behaviour))
        fresh_chat = fresh.create_chat("Re-read, codex model, fresh binding")
        fresh.send_turn(fresh_chat, "hello")
        for h, name in ((harness, "persistent"), (failed, "failed-launch"),
                        (fresh, "fresh-binding")):
            records = self.reproduce(h, "classify-reproduces-codex-model-%s" % name)
            self.assertIn("malformed", self.interpretations(records), name)


class UnrecognizedAndMalformedAreNeverChat(unittest.TestCase, StoreCheck, ModelDirs):
    """Contract 4.2 and 7 P4, at the store and over HTTP. Every probe line
    arrives -- each is preserved and can be found -- and none becomes a
    message."""

    def assert_never_chat(self, records, expected_answers):
        events = dict((r["event_id"], r) for r in records if r["record_type"] == "diagnostic_event")
        agent = [r for r in records if r["record_type"] == "message" and r["author"] == "agent"]
        self.assertEqual([m["content"]["text"] for m in agent], expected_answers)
        for message in agent:
            cited = events[message["source_event_id"]]
            self.assertEqual((cited["interpretation"], cited["source"]), ("recognized", "agent"))
        unread = [e for e in events.values() if e["interpretation"] != "recognized"]
        self.assertTrue(unread, "the probe lines must have arrived for this to mean anything")
        for message in (r for r in records if r["record_type"] == "message"):
            self.assertNotIn(LOOKALIKE, message["content"]["text"])
            self.assertNotIn("SYNTHETIC", message["content"]["text"])
            self.assertNotIn("{{{", message["content"]["text"])
        return unread

    def lookalikes(self, unread):
        return [e for e in unread if LOOKALIKE in e["raw"]["body"]]

    def test_at_the_store_through_dev_local(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                harness = support.harness({"launcher": "dev-local",
                                           "options": dev_local_options(profile)})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Never chat, %s" % profile)
                for text in THREE_TURNS[:2]:
                    harness.send_turn(chat_id, text)
                records = harness.store.export_records()
                unread = self.assert_never_chat(
                    records, ["answer to: %s" % t for t in THREE_TURNS[:2]])
                lookalikes = self.lookalikes(unread)
                self.assertEqual(len(lookalikes), 4, "two per turn")
                self.assertEqual(set((e["interpretation"], e["interpreted_type"])
                                     for e in lookalikes), {("malformed", None)})
                self.assert_store_valid(harness.store, "never-chat-dev-local-%s" % profile)

    def test_at_the_store_through_the_codex_model(self):
        self.make_dirs()
        harness = support.harness({}, launcher=self.model(behaviour=(
            "agent-message-without-text", "synthetic-item", "synthetic-typeless",
            "synthetic-unrecognized", "synthetic-malformed")))
        chat_id = harness.create_chat("Never chat, codex model")
        for text in THREE_TURNS[:2]:
            harness.send_turn(chat_id, text)
        unread = self.assert_never_chat(harness.store.export_records(),
                                        ["answer to: %s" % t for t in THREE_TURNS[:2]])
        without_text = [e for e in unread if e["raw"]["body"] == json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message"}})]
        self.assertEqual([e["interpretation"] for e in without_text], ["malformed"] * 2)
        self.assert_store_valid(harness.store, "never-chat-codex-model")

    def test_over_http_with_a_shipped_launcher_chosen_by_configuration(self):
        from shellproc import ShellProcess
        cases = (("dev-local", dev_local_options("one_shot")),
                 ("dev-local", dev_local_options("persistent")),
                 ("scripted-stub", {"garbage": True, "unknown_type": True}))
        for launcher, options in cases:
            with self.subTest(launcher=launcher, options=options.get("profile")):
                root = support.scratch_root()
                shell = ShellProcess(root, launcher=launcher, launcher_options=options)
                self.addCleanup(shell.kill)
                shell.start()
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                for text in THREE_TURNS[:2]:
                    self.assertEqual(shell.raw("POST", path + "/messages", {"text": text})[0],
                                     201)
                status, body = shell.raw("GET", path)
                self.assertEqual(status, 200)
                served = json.loads(body)
                self.assertEqual(
                    [(m["author"], m["text"]) for m in served["messages"]],
                    [row for t in THREE_TURNS[:2]
                     for row in (("user", t), ("agent", "answer to: %s" % t))])
                _status, page = shell.get_page()
                for text in (body, page):
                    for marker in (LOOKALIKE, "{{{", "agent_thinking", "not json"):
                        self.assertNotIn(marker, text)
                shell.kill()
                store = ChatStore(root, read_only=True)
                self.assertEqual(store.verify(), [])
                records = store.export_records()
                unread = self.assert_never_chat(
                    records, ["answer to: %s" % t for t in THREE_TURNS[:2]])
                if launcher == "dev-local":
                    self.assertEqual(len(self.lookalikes(unread)), 4)


if __name__ == "__main__":
    unittest.main()
