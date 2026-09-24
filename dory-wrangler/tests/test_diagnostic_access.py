"""Checkpoint `expose-bounded-diagnostic-access`: the findings carried into it.

* **A. Carried from the malformed-handling review.**
  - F1: one definition of "a `one_shot` turn has ended", used by the drain and
    by re-attachment (`SessionManager._one_shot_turn_ended`): a declined claim
    with no `turn_complete`, a page that says its stream ended, and a response
    served over several pages are each read alike on both paths, and a paged
    reply renders under the turn it answers.
  - F2: the phase-3 adjudication key names a session by chat id, so a
    same-titled twin chat is no longer covered by its twin's entry.
  - F3: `_holds_exactly`'s read-failure branch -- a payload whose read-back
    fails is not taken for one the store holds, so no notice claims it was
    preserved (the review's mutation V06).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import support
from support import StoreCheck

from shellproc import ShellProcess
from test_carried_gates import by_type, copy, run_gate
from test_rendering import ModelDirs
from test_unsupported_and_malformed import NOTICE, THINKING, Scripted, transcript
from dory_wrangler import atomic
from dory_wrangler import launch_boundary as lb
from dory_wrangler import webapp
from dory_wrangler.errors import StoreCorrupt, ValidationRefused
from dory_wrangler.session_manager import SessionManager
from dory_wrangler.store import DIAGNOSTIC_PAGE_DEFAULT, DIAGNOSTIC_PAGE_MAX, ChatStore

import reclassify_stores

TOOL = os.path.join(support.DORY, "diagnostics.py")
SRC_PACKAGE = os.path.join(support.SRC, "dory_wrangler")

ANSWER_HELLO = ("agent", json.dumps({"type": "assistant_text", "text": "hello"}).encode())
TURN_COMPLETE = ("agent", b'{"type": "turn_complete"}')
LAUNCHER_STREAM_END = ("launcher", lb.PAYLOAD_STREAM_END, b'{"type": "stream_end"}')


def run_tool(*args):
    """(exit status, stdout, stderr) of `diagnostics.py` as its own process."""
    completed = subprocess.run([sys.executable, TOOL] + [str(a) for a in args],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return completed.returncode, completed.stdout, completed.stderr.decode("ascii")


def records_of(stdout):
    """The tool's standard output, one JSON object per line."""
    return [json.loads(line) for line in stdout.decode("ascii").splitlines()]


def tree_hash(root):
    """SHA-256 over every path under `root` -- names, kinds and file bytes."""
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(dirs + files):
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).encode("utf-8", "surrogateescape")
            if os.path.isdir(path):
                digest.update(b"D " + rel + b"\n")
            else:
                with open(path, "rb") as handle:
                    body = handle.read()
                digest.update(b"F " + rel + b" %d\n" % len(body) + body)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# A. F1 -- one definition of a one-shot turn's end, on both reading paths.
# ---------------------------------------------------------------------------

class Paging(Scripted):
    """The scripted stub, keeping its id, serving its turn's payloads `page_size`
    at a time, optionally saying on every page that a stream ended (which a
    `one_shot` launcher cannot see), and failing its first `fail_reads` reads."""

    page_size = None
    flag_stream_ended = False
    fail_reads = 0

    def events(self, agent_handle, after_sequence):
        if self.fail_reads:
            self.fail_reads -= 1
            raise lb.LauncherError("unavailable", "the probe's read failed")
        session = self._session(agent_handle)
        ready = [p for p in session.payloads if p.sequence > after_sequence]
        if self.page_size:
            ready = ready[:self.page_size]
        return lb.EventsPage(ready, stream_ended=self.flag_stream_ended)


def paging(script, page_size=None, flag=False, continuation="persistent"):
    launcher = Paging({"continuation": continuation, "response_shape": "one_shot"})
    launcher.script = script
    launcher.page_size = page_size
    launcher.flag_stream_ended = flag
    return launcher


# Each shape: (name, script, page size, page says its stream ended, the first
# turn's transcript, which is the same on both paths).
ONE_SHOT_SHAPES = (
    ("thinking only", [THINKING], None, False, [("user", "first"), NOTICE]),
    ("declined stream_end, no turn_complete", [THINKING, LAUNCHER_STREAM_END], None, False,
     [("user", "first")]),
    ("a page that says its stream ended", [THINKING], None, True, [("user", "first")]),
    ("turn_complete, then a declined stream_end",
     [TURN_COMPLETE, LAUNCHER_STREAM_END], None, False, [("user", "first"), NOTICE]),
    ("an answer over two pages", [THINKING, ANSWER_HELLO], 1, False,
     [("user", "first"), ("agent", "hello")]),
    ("nothing to show over three pages", [THINKING, THINKING, THINKING], 1, False,
     [("user", "first"), NOTICE]),
)


class OneDefinitionOfAOneShotTurnEnd(unittest.TestCase, StoreCheck):
    """Malformed review F1. The first turn of each shape is read once by the
    drain, and once by re-attachment after the drain's first read failed; both
    paths leave the same transcript. At `1591a1d` re-attachment wrote the notice
    for the second and third shapes, where the drain wrote none, and for the
    fifth, where the reply then rendered under the next turn."""

    def by_drain(self, launcher):
        manager = SessionManager(ChatStore(support.scratch_root()), launcher)
        chat_id = manager.create_chat("One-shot end, drain")
        try:
            manager.send_turn(chat_id, "first")
        except lb.LaunchBoundaryError:
            pass
        return manager, chat_id

    def by_reattachment(self, launcher):
        root = support.scratch_root()
        launcher.fail_reads = 1
        manager = SessionManager(ChatStore(root), launcher)
        chat_id = manager.create_chat("One-shot end, re-attachment")
        manager.send_turn(chat_id, "first")
        self.assertEqual(transcript(manager.store.export_records()), [("user", "first")],
                         "the drain's only read failed, so nothing was concluded")
        manager.store.close()
        manager = SessionManager(ChatStore(root), launcher)
        manager.reattach_on_start()
        return manager, chat_id

    def test_the_drain_and_re_attachment_read_each_shape_alike(self):
        for name, script, page_size, flag, expected in ONE_SHOT_SHAPES:
            for continuation in ("persistent", "fresh_binding"):
                with self.subTest(shape=name, continuation=continuation):
                    drained, drained_chat = self.by_drain(
                        paging(script, page_size, flag, continuation))
                    reattached, reattached_chat = self.by_reattachment(
                        paging(script, page_size, flag, continuation))
                    self.assertEqual(transcript(drained.store.export_records()), expected)
                    self.assertEqual(transcript(reattached.store.export_records()), expected)
                    self.assertEqual(drained.store.verify(), [])
                    self.assertEqual(reattached.store.verify(), [])

    def test_a_paged_reply_renders_under_the_turn_it_answers(self):
        """The review's case (b): at `1591a1d` the transcript was
        [first, NOTICE, second, hello, hello]."""
        launcher = paging([THINKING, ANSWER_HELLO], page_size=1)
        manager, chat_id = self.by_reattachment(launcher)
        manager.send_turn(chat_id, "second")
        self.assertEqual(transcript(manager.store.export_records()),
                         [("user", "first"), ("agent", "hello"),
                          ("user", "second"), ("agent", "hello")])
        # Re-attachment read to the empty page: after `reattached`, its reads
        # returned sequence 1, then 2, then nothing.
        self.assert_store_valid(manager.store, "f1-paged-reply-at-reattachment")

    def test_a_declined_claim_without_a_turn_end_gets_no_notice_on_either_path(self):
        """The review's case (a), kept: nothing reported the turn ended, and the
        read that would have observed it was refused, on both paths."""
        manager, chat_id = self.by_reattachment(paging([THINKING, LAUNCHER_STREAM_END]))
        records = manager.store.export_records()
        self.assertEqual(transcript(records), [("user", "first")])
        self.assertEqual([(e["interpretation"], e["interpreted_type"])
                          for e in sorted(by_type(records, "diagnostic_event"),
                                          key=lambda e: e["sequence"])],
                         [("unrecognized", None), ("unrecognized", None)],
                         "both payloads preserved; the claim declined")
        self.assert_store_valid(manager.store, "f1-declined-claim-at-reattachment")

    def test_a_page_that_does_not_advance_is_not_a_turn_end(self):
        """A one-shot launcher that ignores `after_sequence` hands back what is
        already stored. The read is refused as before, and a page with payloads
        on it is not the empty page that ends a turn, on either path."""
        class Replays(Paging):
            def events(self, agent_handle, after_sequence):
                page = Paging.events(self, agent_handle, after_sequence)
                session = self._session(agent_handle)
                return lb.EventsPage(page.payloads or session.payloads[:1],
                                     stream_ended=False)
        for path in (self.by_drain, self.by_reattachment):
            with self.subTest(path=path.__name__):
                launcher = Replays({"continuation": "persistent", "response_shape": "one_shot"})
                launcher.script = [THINKING]
                manager, chat_id = path(launcher)
                self.assertEqual(transcript(manager.store.export_records()), [("user", "first")])

    def test_re_attachment_reads_one_page_of_a_stream_and_stops(self):
        """Reading on is for `one_shot` sessions only: on a stream it would block
        start-up on a quiet agent. A stream's re-attachment reads one page."""
        launcher = Paging({"continuation": "persistent", "response_shape": "stream"})
        launcher.script = [THINKING, THINKING, TURN_COMPLETE]
        launcher.page_size = 1
        launcher.fail_reads = 1
        root = support.scratch_root()
        manager = SessionManager(ChatStore(root), launcher)
        chat_id = manager.create_chat("Stream, one page")
        manager.send_turn(chat_id, "first")
        manager.store.close()
        manager = SessionManager(ChatStore(root), launcher)
        manager.reattach_on_start()
        records = manager.store.export_records()
        self.assertEqual([e["sequence"] for e in by_type(records, "diagnostic_event")], [1])
        self.assertEqual(transcript(records), [("user", "first")])


# ---------------------------------------------------------------------------
# A. F2 -- the adjudication key is unique within a store.
# ---------------------------------------------------------------------------

class TheAdjudicationKeyIsUniqueWithinAStore(unittest.TestCase):

    def twins(self):
        harness = support.harness({"launcher": "scripted-stub", "options": {}})
        for _ in range(2):
            harness.send_turn(harness.create_chat("Twin"), "hello")
        records = copy(harness.store.export_records())
        events = [e for e in by_type(records, "diagnostic_event") if e["sequence"] == 1]
        self.assertEqual(len(events), 2)
        for event in events:
            event["interpretation"], event["interpreted_type"] = "unrecognized", None
        return records, events

    def entry(self, records, event):
        keys = reclassify_stores.session_keys(records)
        return {("s.json", keys[event["session_id"]], 1, ("unrecognized", None),
                 reclassify_stores.body_hash(event["raw"]["body"])): "planted"}

    def test_a_same_titled_twin_chat_is_not_covered_by_its_twin_s_entry(self):
        """At `1591a1d` both sessions were keyed "Twin/1", so one entry covered
        both and the gate exited 0."""
        records, (one, two) = self.twins()
        keys = reclassify_stores.session_keys(records)
        self.assertNotEqual(keys[one["session_id"]], keys[two["session_id"]])
        self.assertEqual(run_gate(self, {"s.json": records},
                                  patches={"ADJUDICATED": self.entry(records, one)})[0], 1)
        both = self.entry(records, one)
        both.update(self.entry(records, two))
        self.assertEqual(run_gate(self, {"s.json": records},
                                  patches={"ADJUDICATED": both})[0], 0)

    def test_every_kept_session_key_is_unique_in_its_store(self):
        for name in sorted(os.listdir(support.FIXTURE_OUT)) if os.path.isdir(
                support.FIXTURE_OUT) else []:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(support.FIXTURE_OUT, name)) as handle:
                records = json.load(handle)["records"]
            keys = list(reclassify_stores.session_keys(records).values())
            self.assertEqual(len(keys), len(set(keys)), name)


# ---------------------------------------------------------------------------
# A. F3 -- a read-back that fails is not a payload held.
# ---------------------------------------------------------------------------

class ReadBackFails(ChatStore):
    """`_holds_exactly` asks for one event by its sequence; that read fails."""

    def read_diagnostic_events(self, chat_id, session_id=None, sequence_from=None,
                               sequence_to=None, limit=DIAGNOSTIC_PAGE_DEFAULT):
        if limit == 1 and sequence_from is not None and sequence_from == sequence_to:
            raise StoreCorrupt("the read-back failed")
        return ChatStore.read_diagnostic_events(self, chat_id, session_id, sequence_from,
                                                sequence_to, limit)


class AReadBackThatFailsIsNotAPayloadHeld(unittest.TestCase):
    """Malformed review F3 and its mutation V06. The turn reports its end, then a
    one-shot launcher's `stream_end` is declined after its bytes were kept. The
    notice is written only when the read-back shows those bytes held."""

    def turn(self, store_class):
        launcher = Scripted({"continuation": "persistent", "response_shape": "one_shot"})
        launcher.script = [TURN_COMPLETE, LAUNCHER_STREAM_END]
        manager = SessionManager(store_class(support.scratch_root()), launcher)
        chat_id = manager.create_chat("Read-back")
        with self.assertRaises(lb.LaunchBoundaryError):
            manager.send_turn(chat_id, "first")
        return transcript(manager.store.export_records())

    def test_the_notice_needs_the_read_back(self):
        self.assertEqual(self.turn(ChatStore), [("user", "first"), NOTICE])
        self.assertEqual(self.turn(ReadBackFails), [("user", "first")])


if __name__ == "__main__":
    unittest.main()
