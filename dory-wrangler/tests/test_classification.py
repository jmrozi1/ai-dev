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

import base64
import io
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
            line = dev_transport.read_line(io.BytesIO(raw + b"\n"))
            return reading(DevLocalLauncher._agent_payload(None, session, line))

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


class TheBoundaryReadsEveryShapeTheSameWayEveryTime(unittest.TestCase):
    """The `recognized` / `unrecognized` / `malformed` boundary of decision
    0004, line by line, for both wire formats. Every odd shape a transport could
    print has a legal reading and none of them raises: a classifier that raised
    on a line would lose the payload, where P1 requires it preserved."""

    DEV = [
        (b'{"type": "assistant_text", "text": "caf\xc3\xa9 \xe2\x9c\x93"}',
         ("recognized", "assistant_text", "caf\u00e9 \u2713")),
        (b'{"type": "assistant_text", "text": ""}', ("recognized", "assistant_text", "")),
        (b'{"type": "turn_complete", "text": 7}', ("recognized", "turn_complete", None)),
        (b'{"type": "assistant_text"}', ("malformed", None, None)),
        (b'{"type": "assistant_text", "text": null}', ("malformed", None, None)),
        (b'{"type": "assistant_text", "text": ["x"]}', ("malformed", None, None)),
        (b'{"type": "assistant_text", "text": 3}', ("malformed", None, None)),
        (b'{"type": ["assistant_text"], "text": "x"}', ("unrecognized", None, None)),
        (b'{"type": {"k": 1}}', ("unrecognized", None, None)),
        (b'{"type": 3}', ("unrecognized", None, None)),
        (b'{"text": "x"}', ("unrecognized", None, None)),
        (b'["assistant_text", "x"]', ("unrecognized", None, None)),
        (b'"assistant_text"', ("unrecognized", None, None)),
        (b'null', ("unrecognized", None, None)),
        (b'{"type": "Assistant_Text", "text": "x"}', ("unrecognized", None, None)),
        # The type string is matched exactly: no whitespace is trimmed from it.
        (b'{"type": "assistant_text ", "text": "x"}', ("unrecognized", None, None)),
        (b'{"type": " assistant_text", "text": "x"}', ("unrecognized", None, None)),
        (b'{"type": "turn_complete\\n"}', ("unrecognized", None, None)),
        (b'not json {{{', ("malformed", None, None)),
        (b'\xff\xfe{"type": "turn_complete"}', ("malformed", None, None)),
        (b'{"type": "assistant_text", "text": "\xff"}', ("malformed", None, None)),
        (b'', ("malformed", None, None)),
    ]

    CODEX = [
        (b'{"type": "thread.started", "thread_id": "t1"}', ("recognized", "thread.started", None)),
        (b'{"type": "thread.started", "thread_id": ""}', ("malformed", None, None)),
        # Non-empty means non-empty, not non-blank: a whitespace handle is a handle.
        (b'{"type": "thread.started", "thread_id": " "}', ("recognized", "thread.started", None)),
        (b'{"type": "thread.started", "thread_id": "\\t"}', ("recognized", "thread.started", None)),
        # The type strings are matched exactly: no whitespace is trimmed from them.
        (b'{"type": "thread.started ", "thread_id": "t1"}', ("unrecognized", None, None)),
        (b'{"type": " thread.started", "thread_id": "t1"}', ("unrecognized", None, None)),
        (b'{"type": "item.completed ", "item": {"type": "agent_message", "text": "x"}}',
         ("unrecognized", None, None)),
        (b'{"type": "item.completed", "item": {"type": "agent_message ", "text": "x"}}',
         ("unrecognized", None, None)),
        (b'{"type": "thread.started", "thread_id": 5}', ("malformed", None, None)),
        (b'{"type": "thread.started"}', ("malformed", None, None)),
        (b'{"type": "item.completed", "item": {"type": "agent_message", "text": ""}}',
         ("recognized", "assistant_text", "")),
        (b'{"type": "item.completed", "item": {"type": "agent_message", "text": "caf\xc3\xa9"}}',
         ("recognized", "assistant_text", "caf\u00e9")),
        (b'{"type": "item.completed", "item": {"type": "agent_message"}}', ("malformed", None, None)),
        (b'{"type": "item.completed", "item": {"type": "agent_message", "text": ["x"]}}',
         ("malformed", None, None)),
        (b'{"type": "item.completed", "item": {"type": "reasoning", "text": "x"}}',
         ("unrecognized", None, None)),
        (b'{"type": "item.completed", "item": {"type": ["agent_message"], "text": "x"}}',
         ("unrecognized", None, None)),
        (b'{"type": "item.completed", "item": {"type": {"k": 1}}}', ("unrecognized", None, None)),
        (b'{"type": "item.completed", "item": "agent_message"}', ("unrecognized", None, None)),
        (b'{"type": "item.completed", "item": ["agent_message"]}', ("unrecognized", None, None)),
        (b'{"type": "item.completed"}', ("unrecognized", None, None)),
        (b'{"type": "item.completed", "type_": "agent_message", "text": "x"}',
         ("unrecognized", None, None)),
        (b'{"type": "agent_message", "text": "x"}', ("unrecognized", None, None)),
        (b'{"type": ["thread.started"], "thread_id": "t1"}', ("unrecognized", None, None)),
        (b'{"type": {"k": 1}}', ("unrecognized", None, None)),
        (b'{"thread_id": "t1"}', ("unrecognized", None, None)),
        (b'[{"type": "thread.started", "thread_id": "t1"}]', ("unrecognized", None, None)),
        (b'answer to: plain text', ("malformed", None, None)),
        (b'\xff{"type": "thread.started", "thread_id": "t1"}', ("malformed", None, None)),
    ]

    def check(self, classify, table):
        for raw, expected in table:
            with self.subTest(raw=raw):
                payload = classify(4, raw)
                self.assertEqual(reading(payload), expected)
                self.assertEqual((payload.sequence, payload.source, payload.raw),
                                 (4, "agent", raw), "the bytes are the bytes given")
                self.assertEqual(classify(4, raw, source="launcher").source, "launcher")
                self.assertEqual(reading(classify(4, raw, source="launcher")), expected,
                                 "the source changes nothing about the reading")

    def test_the_development_transport(self):
        self.check(dev_transport.classify, self.DEV)

    def test_the_codex_model(self):
        self.check(internal_bridge.classify, self.CODEX)

    # The development transport's framing (`dev_transport.read_line`), which
    # `dev-local` reads both profiles through: wire bytes -> the raw bytes of
    # each line the classifier is handed. Split on b"\n" only; nothing decoded,
    # trimmed or translated.
    FRAMING = [
        (b'{"type": "assistant_text", "text": "caf\xc3\xa9"}\n',
         [b'{"type": "assistant_text", "text": "caf\xc3\xa9"}']),
        # trailing whitespace stays in `raw` (review F4)
        (b'{"type": "agent_thinking"}  \t \n', [b'{"type": "agent_thinking"}  \t ']),
        (b'  {"type": "turn_complete"}\n', [b'  {"type": "turn_complete"}']),
        # a CR is part of the line, not its end
        (b'{"type": "agent_thinking"}\r\n', [b'{"type": "agent_thinking"}\r']),
        (b'{"type": "agent_thinking"}\rX\n', [b'{"type": "agent_thinking"}\rX']),
        # U+2028, U+0085 and the other characters str.splitlines splits on do not
        # end a line
        ('{"type": "assistant_text", "text": "l\u2028r"}\n'.encode("utf-8"),
         ['{"type": "assistant_text", "text": "l\u2028r"}'.encode("utf-8")]),
        ('{"type": "assistant_text", "text": "l\u0085r\u2029\x0b\x0c\x1c"}\n'.encode("utf-8"),
         ['{"type": "assistant_text", "text": "l\u0085r\u2029\x0b\x0c\x1c"}'.encode("utf-8")]),
        # bytes that are not UTF-8 are not decoded, so they arrive
        (b'{"type": "agent_thinking", "x": "\xff"}\n', [b'{"type": "agent_thinking", "x": "\xff"}']),
        # blank lines carry no event; the last line needs no b"\n"
        (b'\n\r\n \t\na\n\nb', [b'a', b'b']),
        (b'', []),
    ]

    def test_the_framing_splits_bytes_on_newline_only(self):
        for wire, lines in self.FRAMING:
            with self.subTest(wire=wire):
                stream = io.BytesIO(wire)
                self.assertEqual(list(iter(lambda: dev_transport.read_line(stream), None)),
                                 lines)

    def test_dev_local_reads_a_line_as_its_bytes_without_the_newline(self):
        cases = [
            (b'{"type": "assistant_text", "text": "caf\xc3\xa9"}\n',
             ("recognized", "assistant_text", "caf\u00e9")),
            (b'{"type": "agent_thinking"}  \t \n', ("unrecognized", None, None)),
            (b'{"type": "turn_complete"}\r\n', ("recognized", "turn_complete", None)),
            ('{"type": "assistant_text", "text": "l\u2028r"}\n'.encode("utf-8"),
             ("recognized", "assistant_text", "l\u2028r")),
            (b'{"type": "assistant_text", "text": "\xff"}\n', ("malformed", None, None)),
        ]
        for wire, expected in cases:
            with self.subTest(wire=wire):
                session = type("S", (), {"next_sequence": 3})()
                line = dev_transport.read_line(io.BytesIO(wire))
                payload = DevLocalLauncher._agent_payload(None, session, line)
                self.assertEqual((payload.sequence, payload.raw, reading(payload)),
                                 (3, wire[:-1], expected))
                self.assertEqual(session.next_sequence, 4)


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

    def test_the_whole_store_re_read_fails_on_a_disagreement(self):
        """Review F2: `reclassify_stores.py` -- phase 3 of `run_tests.py` -- exits
        non-zero on a record that does not reproduce and on a synthesised record
        inconsistent with its own bytes, unless `ADJUDICATED` names it."""
        import contextlib
        harness = support.harness({"launcher": "scripted-stub",
                                   "options": {"unknown_type": True}})
        chat_id = harness.create_chat("Re-read gate")
        harness.send_turn(chat_id, "hello")
        records = harness.store.export_records()
        events = [r for r in records if r["record_type"] == "diagnostic_event"]
        unknown = [e for e in events if e["source"] == "agent"
                   and e["interpretation"] == "unrecognized"][0]
        lifecycle = [e for e in events if e["source"] == "launcher"][0]

        def run(records, adjudicate=()):
            base = tempfile.mkdtemp(prefix="dory-reread-")
            self.addCleanup(shutil.rmtree, base, True)
            with open(os.path.join(base, "planted.json"), "w") as out:
                json.dump({"records": records}, out)
            with mock.patch.dict(reclassify_stores.ADJUDICATED,
                                 dict((key, "planted") for key in adjudicate)), \
                    contextlib.redirect_stdout(io.StringIO()):
                return reclassify_stores.main([base])

        def planted(event, **fields):
            changed = json.loads(json.dumps(records))
            target = [r for r in changed if r.get("event_id") == event["event_id"]][0]
            for name, value in fields.items():
                if name == "body":
                    target["raw"]["body"] = value
                else:
                    target[name] = value
            return changed

        self.assertEqual(run(records), 0)
        not_reproduced = planted(unknown, body='{"type": "turn_complete"}')
        self.assertEqual(run(not_reproduced), 1)
        self.assertEqual(run(not_reproduced, [("planted.json", unknown["sequence"],
                                               ("unrecognized", None))]), 0)
        inconsistent = planted(lifecycle, interpreted_type="session_failed")
        self.assertEqual(run(inconsistent), 1)
        self.assertEqual(run(inconsistent, [("planted.json", lifecycle["sequence"],
                                             ("recognized", "session_failed"))]), 0)

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



# An agent that writes, before each answer, the lines a text-mode reader used to
# lose or split (review F1): an answer with a raw U+2028 in its text, a line with
# a raw U+0085, a line that is not UTF-8, a CR-terminated line, and a line with
# trailing whitespace. Then a normal answer and `turn_complete`.
ODD_LINES = [
    '{"type": "assistant_text", "text": "left\u2028right"}'.encode("utf-8"),
    '{"type": "agent_thinking", "x": "a\u0085b"}'.encode("utf-8"),
    b'{"type": "agent_thinking", "x": "\xff"}',
    b'{"type": "agent_thinking", "cr": true}\r',
    b'{"type": "agent_thinking", "ws": true}  \t ',
]
ODD_READINGS = [("recognized", "assistant_text"), ("unrecognized", None), ("malformed", None),
                ("unrecognized", None), ("unrecognized", None)]
ODD_AGENT = r"""
import sys
LINES = [bytes.fromhex(h) for h in sys.argv[2].split(",")]
out = sys.stdout.buffer
def turn(instruction):
    for line in LINES:
        out.write(line + b"\n")
    out.write(b'{"type": "assistant_text", "text": "answer to: '
              + instruction.strip().encode("utf-8") + b'"}\n{"type": "turn_complete"}\n')
    out.flush()
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read().decode("utf-8"))
else:
    for line in sys.stdin.buffer:
        turn(line.decode("utf-8"))
"""


class DevLocalFramesTheWireBytesTheSameInBothProfiles(unittest.TestCase, StoreCheck):
    """Review F1, closed by reading `dev-local`'s stdout as bytes and framing on
    b"\n" only in both profiles (`dev_transport.read_line`). The same wire bytes
    are the same events under `one_shot` and `persistent`; a line that is not
    UTF-8 is preserved `malformed` with its bytes intact and costs nothing else;
    a CR and trailing whitespace stay in `raw`. In process and over HTTP."""

    def options(self, profile):
        base = tempfile.mkdtemp(prefix="dory-framing-")
        self.addCleanup(shutil.rmtree, base, True)
        agent = os.path.join(base, "odd_agent.py")
        with open(agent, "w") as handle:
            handle.write(ODD_AGENT)
        return {"profile": profile,
                "command": [sys.executable, agent, profile,
                            ",".join(line.hex() for line in ODD_LINES)]}

    def raw_of(self, event):
        body = event["raw"]["body"]
        return base64.b64decode(body) if event["raw"]["encoding"] == "base64" else body.encode("utf-8")

    def check(self, records, profile, texts):
        events = [r for r in records if r["record_type"] == "diagnostic_event"]
        agent = [e for e in events if e["source"] == "agent"]
        expected = []
        for text in texts:
            answer = ('{"type": "assistant_text", "text": "answer to: %s"}' % text).encode("utf-8")
            expected += [(line, reading) for line, reading in zip(ODD_LINES, ODD_READINGS)]
            expected += [(answer, ("recognized", "assistant_text")),
                         (b'{"type": "turn_complete"}', ("recognized", "turn_complete"))]
        self.assertEqual([(self.raw_of(e), (e["interpretation"], e["interpreted_type"]))
                          for e in agent], expected)
        not_utf8 = [e for e in agent if self.raw_of(e) == ODD_LINES[2]]
        self.assertEqual([e["raw"]["encoding"] for e in not_utf8], ["base64"] * len(texts))
        messages = [(r["author"], r["content"]["text"]) for r in records
                    if r["record_type"] == "message"]
        self.assertEqual(messages, [row for t in texts for row in (
            ("user", t), ("agent", "left\u2028right"), ("agent", "answer to: %s" % t))])
        states = [r["state"] for r in records if r["record_type"] == "agent_session"]
        self.assertEqual(states, ["completed"] * len(texts) if profile == "one_shot"
                         else ["running"], "no session is stranded")

    def test_in_process(self):
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                harness = support.harness({"launcher": "dev-local",
                                           "options": self.options(profile)})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Framing, %s" % profile)
                for text in THREE_TURNS[:2]:
                    harness.send_turn(chat_id, text)
                self.check(harness.store.export_records(), profile, THREE_TURNS[:2])
                self.assert_store_valid(harness.store, "framing-dev-local-%s" % profile)

    def test_over_http_through_run_shell(self):
        from shellproc import ShellProcess
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                root = support.scratch_root()
                shell = ShellProcess(root, launcher="dev-local",
                                     launcher_options=self.options(profile))
                self.addCleanup(shell.kill)
                shell.start()
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                for text in THREE_TURNS[:2]:
                    self.assertEqual(shell.raw("POST", path + "/messages", {"text": text})[0],
                                     201)
                status, body = shell.raw("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(
                    [(m["author"], m["text"]) for m in json.loads(body)["messages"]],
                    [row for t in THREE_TURNS[:2] for row in (
                        ("user", t), ("agent", "left\u2028right"), ("agent", "answer to: %s" % t))])
                shell.kill()
                store = ChatStore(root, read_only=True)
                self.assertEqual(store.verify(), [])
                self.check(store.export_records(), profile, THREE_TURNS[:2])


if __name__ == "__main__":
    unittest.main()
