"""Checkpoint `expose-bounded-diagnostic-access` (decision 0007).

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
* **B. The command-line retrieval**, `dory-wrangler/diagnostics.py`, always run
  as a separate program: verbatim records, bounded, addressed as contract P4
  says, refusals in fixed words, the store unchanged byte for byte by every
  invocation, against a live `run_shell.py` store and a stopped one, and not
  reachable from the served application.
* **C. #88's minimum from the tool's output alone:** what event types occurred,
  which were rendered, which were `unrecognized` or `malformed`, and where the
  stream stopped -- derived by this test from the tool's standard output, and
  compared with what the launchers were given.
* **D. Carried probes.** D6: a staged directory or temp file under the events
  tree is never returned, by the library or by the tool. X14:
  `set_agent_handle`'s type check.
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


# ---------------------------------------------------------------------------
# D. D6 and X14.
# ---------------------------------------------------------------------------

def stub_chat(script=None, turns=("one", "two"), root=None, title="Diagnostics", stop=False):
    """(root, chat_id, manager): a closed store holding one chat of scripted-stub
    turns, its agent stopped by the user when `stop` (a confirmed stop, so the
    session carries an observation)."""
    root = root or support.scratch_root()
    launcher = Scripted({"continuation": "persistent", "response_shape": "stream"})
    launcher.script = script or [THINKING, ("agent", json.dumps(
        {"type": "assistant_text", "text": "an answer"}).encode()), TURN_COMPLETE]
    manager = SessionManager(ChatStore(root), launcher)
    chat_id = manager.create_chat(title)
    for text in turns:
        manager.send_turn(chat_id, text)
    if stop:
        manager.stop_agent(chat_id, "the user is done")
    manager.store.close()
    return root, chat_id, manager


class AStagedDirectoryIsNeverReturned(unittest.TestCase):
    """D6. A directory or file under the events tree whose name is the atomic
    writer's temp prefix is staging, never a preserved record."""

    def test_through_the_library_and_the_tool(self):
        root, chat_id, _manager = stub_chat(turns=("one",))
        store = ChatStore(root, read_only=True)
        real = store.read_diagnostic_events(chat_id)
        session_id = real[0]["session_id"]
        chat_events = os.path.join(root, "diagnostics", chat_id)
        with open(os.path.join(chat_events, session_id, "00000001.json"), "rb") as handle:
            body = handle.read()
        staged = os.path.join(chat_events, atomic.TEMP_PREFIX + "5taged")
        os.makedirs(staged)
        with open(os.path.join(staged, "00000001.json"), "wb") as handle:
            handle.write(body)
        with open(os.path.join(chat_events, session_id,
                               atomic.TEMP_PREFIX + "half.json"), "wb") as handle:
            handle.write(body)
        self.assertEqual(store.read_diagnostic_events(chat_id), real)
        self.assertEqual(store.read_diagnostic_events(chat_id, session_id=session_id), real)
        status, out, _err = run_tool(root, chat_id)
        self.assertEqual((status, records_of(out)), (0, real))
        status, out, _err = run_tool(root, chat_id, "--session", session_id)
        self.assertEqual((status, records_of(out)), (0, real))


class TheHandleTypeCheck(unittest.TestCase):
    """X14. `set_agent_handle` is called by nothing in the product, and is still
    a public store method. Its type check is what refuses a handle that is not a
    string before the store is touched: without it an unhashable value raises
    `TypeError` out of the store rather than a refusal."""

    def test_a_handle_that_is_not_a_string_is_refused_and_nothing_changes(self):
        root, chat_id, _manager = stub_chat(turns=("one",))
        store = ChatStore(root)
        session_id = store.list_sessions(chat_id)[0][0]["session_id"]
        before = tree_hash(root)
        for handle in (["stub-agent-0001"], {"h": 1}, 7, None, b"stub-agent-0001", ""):
            with self.subTest(handle=handle):
                with self.assertRaises(ValidationRefused):
                    store.set_agent_handle(chat_id, session_id, handle)
        store.close()
        self.assertEqual(tree_hash(root), before)


# ---------------------------------------------------------------------------
# B. The command-line retrieval.
# ---------------------------------------------------------------------------

# Lines whose bytes test what reaches a terminal: not UTF-8, an escape sequence
# inside a line that is therefore not JSON, and non-ASCII text.
AWKWARD = [
    ("agent", b"\xff\xfe not utf-8 {{{"),
    ("agent", b'{"type": "agent_thinking", "note": "\x1b[31mred\x1b[0m"}'),
    ("agent", json.dumps({"type": "assistant_text", "text": "café   ok"},
                         ensure_ascii=False).encode("utf-8")),
    TURN_COMPLETE,
]


class TheCommandLineRetrieval(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root, cls.chat_id, _manager = stub_chat(AWKWARD, turns=("one", "two", "three"),
                                                    stop=True)
        store = ChatStore(cls.root, read_only=True)
        cls.events = store.read_diagnostic_events(cls.chat_id, limit=DIAGNOSTIC_PAGE_MAX)
        cls.messages = store.read_messages(cls.chat_id)
        cls.session_id = cls.events[0]["session_id"]
        session = store.read_session(cls.chat_id, cls.session_id)
        cls.lifecycle = ([session] + store.read_launch_results(cls.chat_id, cls.session_id)
                         + store.read_session_observations(cls.chat_id, cls.session_id))
        assert [r["record_type"] for r in cls.lifecycle] == [
            "agent_session", "launch_result", "session_observation"], cls.lifecycle

    def unchanged(self, *args):
        """Run the tool; assert it left the store exactly as it found it."""
        before = tree_hash(self.root)
        result = run_tool(*args)
        self.assertEqual(tree_hash(self.root), before, args)
        return result

    def test_every_mode_prints_the_preserved_records_verbatim(self):
        for args, expected in (
                ((), self.events),
                (("--records", "events", "--session", self.session_id), self.events),
                (("--records", "messages"), self.messages),
                (("--records", "lifecycle", "--session", self.session_id), self.lifecycle),
                (("--records", "lifecycle"), self.lifecycle)):
            with self.subTest(args=args):
                status, out, err = self.unchanged(self.root, self.chat_id, *args)
                self.assertEqual((status, err), (0, ""))
                self.assertEqual(records_of(out), expected)
                self.assertTrue(all(32 <= b < 127 or b == 10 for b in out),
                                "nothing but printable ASCII reaches the terminal")
        undecodable = [e for e in self.events if e["raw"]["encoding"] == "base64"]
        self.assertEqual([base64.b64decode(e["raw"]["body"]) for e in undecodable],
                         [AWKWARD[0][1]] * 3, "one per turn, kept as base64")
        status, out, _err = run_tool(self.root, self.chat_id)
        self.assertIn(b"\\u001b[31m", out)
        self.assertNotIn(b"\x1b", out)

    def test_addressing_by_session_and_sequence(self):
        status, out, _err = self.unchanged(self.root, self.chat_id, "--session",
                                           self.session_id, "--from", 2, "--to", 3)
        self.assertEqual(records_of(out), [e for e in self.events if 2 <= e["sequence"] <= 3])
        status, out, _err = self.unchanged(self.root, self.chat_id, "--records", "messages",
                                           "--from", 2, "--to", 4)
        self.assertEqual(records_of(out), self.messages[1:4])

    def follow(self, args, limit):
        """Every record reached by following the truncation statements."""
        collected, statements = [], []
        mode = list(args[args.index("--records"):][:2]) if "--records" in args else []
        while True:
            status, out, err = self.unchanged(self.root, self.chat_id, *(
                list(args) + ["--limit", str(limit)]))
            self.assertEqual(status, 0)
            collected.extend(records_of(out))
            if not err:
                return collected, statements
            statements.append(err)
            self.assertTrue(err.startswith("truncated: the bound of %d record(s) was "
                                           "reached and more are preserved; the next is "
                                           % limit), err)
            args = err.split("ask again with ", 1)[1].split(";")[0].strip().split() + mode

    def test_a_bound_says_it_truncated_and_how_to_ask_for_the_rest(self):
        collected, statements = self.follow(["--session", self.session_id], 3)
        self.assertEqual(collected, self.events)
        self.assertEqual(len(statements), (len(self.events) - 1) // 3)
        for args, expected in (
                (["--session", self.session_id, "--from", "2", "--to", "9"],
                 [e for e in self.events if 2 <= e["sequence"] <= 9]),
                (["--records", "messages"], self.messages),
                (["--records", "messages", "--from", "2", "--to", "5"], self.messages[1:5]),
                (["--records", "lifecycle"], self.lifecycle),
                (["--records", "lifecycle", "--from", "2"], self.lifecycle[1:])):
            for limit in (1, 2):
                with self.subTest(args=args, limit=limit):
                    self.assertEqual(self.follow(args, limit)[0], expected)
        status, out, err = self.unchanged(self.root, self.chat_id, "--records", "messages",
                                          "--limit", "4")
        self.assertEqual(err, "truncated: the bound of 4 record(s) was reached and more are "
                              "preserved; the next is message sequence 5; ask again with "
                              "--from 5\n")
        status, out, err = self.unchanged(self.root, self.chat_id, "--records", "lifecycle",
                                          "--limit", "1")
        self.assertEqual(err, "truncated: the bound of 1 record(s) was reached and more are "
                              "preserved; the next is line 2; ask again with --from 2\n")
        self.assertEqual(records_of(out), self.lifecycle[:1])

    def test_refusals_are_fixed_words_with_a_non_zero_exit(self):
        tool = _tool_module()
        missing = os.path.join(self.root, "no-such-dir")
        for args, words, code in (
                ((self.root, "../chats"), tool.REFUSED_CHAT_ID, 2),
                ((self.root, "cht_" + "0" * 24), tool.REFUSED_NO_CHAT, 2),
                ((missing, self.chat_id), tool.REFUSED_STORE, 2),
                ((self.root, self.chat_id, "--session", "../x"), tool.REFUSED_SESSION_ID, 2),
                ((self.root, self.chat_id, "--session", "ses_" + "0" * 24),
                 tool.REFUSED_NO_SESSION, 2),
                ((self.root, self.chat_id, "--limit", "0"), tool.REFUSED_LIMIT, 2),
                ((self.root, self.chat_id, "--limit", "-3"), tool.REFUSED_LIMIT, 2),
                ((self.root, self.chat_id, "--limit", "2.5"), tool.REFUSED_LIMIT, 2),
                ((self.root, self.chat_id, "--limit", "ten"), tool.REFUSED_LIMIT, 2),
                # A digit int() does not read, and one it does that is not ASCII.
                ((self.root, self.chat_id, "--limit", "\u00b2"), tool.REFUSED_LIMIT, 2),
                ((self.root, self.chat_id, "--limit", "\uff13"), tool.REFUSED_LIMIT, 2),
                ((self.root, self.chat_id, "--from", "0"), tool.REFUSED_RANGE, 2),
                ((self.root, self.chat_id, "--to", "x"), tool.REFUSED_RANGE, 2),
                ((self.root, self.chat_id, "--records", "counts"), tool.REFUSED_ARGUMENTS, 2),
                ((self.root, self.chat_id, "--records", "messages", "--session",
                  self.session_id), tool.REFUSED_MESSAGES_BY_SESSION, 2),
                ((self.root,), tool.REFUSED_ARGUMENTS, 2)):
            with self.subTest(args=args[1:]):
                status, out, err = self.unchanged(*args)
                self.assertEqual((status, out, err), (code, b"", words + "\n"))
                self.assertNotIn(self.root, err)

    def test_an_unreadable_store_is_refused_without_its_reason(self):
        root = tempfile.mkdtemp(prefix="dory-diag-")
        self.addCleanup(shutil.rmtree, root, True)
        shutil.rmtree(root)
        shutil.copytree(self.root, root)
        path = os.path.join(root, "diagnostics", self.chat_id, self.session_id, "00000002.json")
        with open(path, "wb") as handle:
            handle.write(b"{ not a record")
        before = tree_hash(root)
        status, out, err = run_tool(root, self.chat_id)
        self.assertEqual((status, out, err), (3, b"", _tool_module().UNREADABLE + "\n"))
        self.assertEqual(tree_hash(root), before)

    def test_the_tool_opens_the_store_read_only(self):
        """Its reads never reach a gated write, so a writable store would change
        nothing either (sweep row T01); the flag is what makes a write in a
        later edit of the tool a refusal rather than a lock taken on a served
        store, so it is pinned by what the tool asks for."""
        tool = _tool_module()
        opened = []

        class Recording(ChatStore):
            def __init__(self, root, sweep=True, read_only=False):
                opened.append(read_only)
                ChatStore.__init__(self, root, sweep=sweep, read_only=read_only)
        tool.ChatStore = Recording
        self.assertEqual(tool.retrieve([self.root, self.chat_id])[0], self.events)
        self.assertEqual(opened, [True])

    def test_every_mode_is_capped_by_the_store_s_bound(self):
        root, chat_id, _manager = stub_chat(turns=("one",))
        store = ChatStore(root)
        for index in range(DIAGNOSTIC_PAGE_MAX):
            store.append_user_message(chat_id, "message %d" % index)
        store.close()
        status, out, err = run_tool(root, chat_id, "--records", "messages",
                                    "--limit", DIAGNOSTIC_PAGE_MAX * 5)
        self.assertEqual((status, len(records_of(out))), (0, DIAGNOSTIC_PAGE_MAX))
        self.assertIn("the next is message sequence %d" % (DIAGNOSTIC_PAGE_MAX + 1), err)

    def test_the_default_bound_and_the_cap_are_the_store_s(self):
        root, chat_id, _manager = stub_chat(
            [THINKING] * (DIAGNOSTIC_PAGE_MAX + 1) + [TURN_COMPLETE], turns=("one",))
        status, out, err = run_tool(root, chat_id)
        self.assertEqual((status, len(records_of(out))), (0, DIAGNOSTIC_PAGE_DEFAULT))
        self.assertIn("the bound of %d record(s)" % DIAGNOSTIC_PAGE_DEFAULT, err)
        status, out, err = run_tool(root, chat_id, "--limit", DIAGNOSTIC_PAGE_MAX * 5)
        self.assertEqual((status, len(records_of(out))), (0, DIAGNOSTIC_PAGE_MAX))
        self.assertIn("the next is session %s sequence %d"
                      % (records_of(out)[0]["session_id"], DIAGNOSTIC_PAGE_MAX + 1), err)


def _tool_module():
    """The tool's fixed words, read from the tool itself."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dory_diagnostics_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhileTheShellServesTheStore(unittest.TestCase):
    """Read-only against a live `run_shell.py`: the server holds the store lock
    throughout, the tool changes nothing, and the server goes on serving."""

    def test_the_tool_reads_a_served_store_and_changes_nothing(self):
        root = support.scratch_root()
        shell = ShellProcess(root)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        self.assertEqual(shell.post("/api/chats/%s/messages" % chat_id, {"text": "one"})[0], 201)
        for args in ((), ("--records", "messages"), ("--records", "lifecycle"),
                     ("--limit", "1"), ("--limit", "0")):
            with self.subTest(args=args):
                before = tree_hash(root)
                status, out, _err = run_tool(root, chat_id, *args)
                self.assertEqual(tree_hash(root), before)
                self.assertEqual(status, 2 if "0" in args else 0)
        self.assertEqual(shell.post("/api/chats/%s/messages" % chat_id, {"text": "two"})[0], 201)
        _status, out, _err = run_tool(root, chat_id, "--records", "messages")
        self.assertEqual([m["content"]["text"] for m in records_of(out)],
                         ["one", "answer to: one", "two", "answer to: two"])
        served = shell.get("/api/chats/%s" % chat_id)[1]
        self.assertEqual([m["text"] for m in served["messages"]],
                         [m["content"]["text"] for m in records_of(out)])


class NotReachableFromTheServedApplication(unittest.TestCase):

    def test_no_module_of_the_package_imports_it(self):
        for name in sorted(os.listdir(SRC_PACKAGE)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(SRC_PACKAGE, name)) as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                for imported in names:
                    self.assertNotIn("diagnostics", imported, name)

    def test_the_served_modules_load_without_it_and_never_retrieve(self):
        code = ("import sys; sys.path.insert(0, %r); "
                "import dory_wrangler.serve, dory_wrangler.webapp, dory_wrangler.service; "
                "print(sorted(m for m in sys.modules if 'diagnostic' in m))" % support.SRC)
        out = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE,
                             check=True).stdout
        self.assertEqual(out.strip(), b"[]")
        for name in ("webapp.py", "service.py", "serve.py", "wiring.py"):
            with open(os.path.join(SRC_PACKAGE, name)) as handle:
                tree = ast.parse(handle.read())
            used = set(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
            used |= set(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
            self.assertEqual(sorted(u for u in used if "diagnostic" in u), [], name)

    def test_no_route_serves_it(self):
        root = support.scratch_root()
        shell = ShellProcess(root)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        for path in ("/diagnostics", "/api/diagnostics", "/api/events",
                     "/api/chats/%s/diagnostics" % chat["chat_id"],
                     "/api/chats/%s/events" % chat["chat_id"],
                     "/api/chats/%s/sessions" % chat["chat_id"]):
            with self.subTest(path=path):
                status, body = shell.raw("GET", path)
                self.assertEqual((status, json.loads(body)),
                                 (404, {"error": webapp.NO_SUCH_ROUTE}))


# ---------------------------------------------------------------------------
# C. #88's minimum, derived from the tool's output alone.
# ---------------------------------------------------------------------------

# A development-transport agent that logs every line it writes (base64, one per
# line, to argv[2]) before writing it: the ground truth is what the launcher was
# given, read from that log. "close" closes its stdout and stays alive.
LOGGING_AGENT = r"""
import base64, json, os, sys
out = sys.stdout.buffer
log = sys.argv[2]
LINES = [
    b'{"type": "agent_thinking", "note": "never chat"}',
    b"not json at all {{{",
    b"\xff\xfe not utf-8 {{{",
    b'{"type": "assistant_text", "text": ["LOOKALIKE never chat"]}',
]
def emit(line):
    with open(log, "a") as handle:
        handle.write(base64.b64encode(line).decode("ascii") + "\n")
    out.write(line + b"\n")
    out.flush()
def turn(instruction):
    instruction = instruction.strip()
    if instruction == "close":
        out.flush()
        os.close(1)  # sys.stdout does not own fd 1: closing it would leave 1 open
        return
    for line in LINES:
        emit(line)
    emit(json.dumps({"type": "assistant_text", "text": "answer to: " + instruction}).encode())
    emit(b'{"type": "turn_complete"}')
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read().decode("utf-8"))
else:
    for line in sys.stdin.buffer:
        if line.strip():
            turn(line.decode("utf-8"))
"""

# What each line the logging agent writes is, by construction.
AGENT_TRUTH = {
    b'{"type": "agent_thinking", "note": "never chat"}': ("unrecognized", None),
    b"not json at all {{{": ("malformed", None),
    b"\xff\xfe not utf-8 {{{": ("malformed", None),
    b'{"type": "assistant_text", "text": ["LOOKALIKE never chat"]}': ("malformed", None),
    b'{"type": "turn_complete"}': ("recognized", "turn_complete"),
}


def answer_line(instruction):
    """The bytes of the logging agent's answer to `instruction`."""
    return json.dumps({"type": "assistant_text", "text": "answer to: " + instruction}).encode()


def raw_of(event):
    raw = event["raw"]
    return base64.b64decode(raw["body"]) if raw["encoding"] == "base64" \
        else raw["body"].encode("utf-8")


def derive(store_root, chat_id):
    """#88's four answers for one chat, from the tool's standard output alone.

    Returns {session_id: {"opened_by": user text, "events": [(sequence, source,
    reading, raw bytes)], "rendered": [sequence], "unknown": [sequence],
    "types": set of readings, "last_sequence", "state", "last_observation"}}.
    """
    outputs = {}
    for mode in ("events", "messages", "lifecycle"):
        status, out, err = run_tool(store_root, chat_id, "--records", mode,
                                    "--limit", DIAGNOSTIC_PAGE_MAX)
        assert (status, err) == (0, ""), (mode, status, err)
        outputs[mode] = records_of(out)
    by_id = dict((m["message_id"], m) for m in outputs["messages"])
    rendered_ids = set(m["source_event_id"] for m in outputs["messages"]
                       if m["author"] == "agent")
    derived = {}
    for record in outputs["lifecycle"]:
        if record["record_type"] == "agent_session":
            derived[record["session_id"]] = {
                "opened_by": by_id[record["transitions"][0]["evidence"]["ref"]]
                ["content"]["text"],
                "state": record["state"], "events": [], "rendered": [], "unknown": [],
                "types": set(), "last_sequence": None, "last_observation": None}
        elif record["record_type"] == "session_observation":
            derived[record["session_id"]]["last_observation"] = record["kind"]
    for event in outputs["events"]:
        row = derived[event["session_id"]]
        reading = (event["interpretation"], event["interpreted_type"])
        row["events"].append((event["sequence"], event["source"], reading, raw_of(event)))
        row["types"].add(reading)
        if event["event_id"] in rendered_ids:
            row["rendered"].append(event["sequence"])
        if event["interpretation"] in ("unrecognized", "malformed"):
            row["unknown"].append(event["sequence"])
        row["last_sequence"] = event["sequence"]
    return dict((row["opened_by"], row) for row in derived.values())


class TheMinimumFromTheToolAlone(unittest.TestCase, StoreCheck, ModelDirs):
    """For each launcher, the ground truth is what the agent wrote, read from
    the agent's own log (development transport) or the model's `calls.jsonl`
    (Codex model), plus what the launcher itself reports; the derivation reads
    only `diagnostics.py`'s standard output."""

    def check(self, derived, opened_by, truth, rendered, state, last_observation):
        """`truth`: [(source, (interpretation, type), raw bytes)] in order;
        `rendered`: the raw bytes of the lines whose text the chat shows."""
        row = derived[opened_by]
        self.assertEqual([(source, reading, raw) for _seq, source, reading, raw in row["events"]],
                         truth)
        self.assertEqual(row["types"], set(reading for _s, reading, _r in truth))
        self.assertEqual([raw for seq, _s, _r, raw in row["events"] if seq in row["rendered"]],
                         rendered)
        self.assertEqual(row["unknown"], [i + 1 for i, (_s, reading, _r) in enumerate(truth)
                                          if reading[0] in ("unrecognized", "malformed")])
        self.assertEqual((row["last_sequence"], row["state"], row["last_observation"]),
                         (len(truth), state, last_observation))

    def agent_lines(self, log, start=0):
        with open(log) as handle:
            return [base64.b64decode(line) for line in handle.read().split()][start:]

    def dev_truth(self, lines):
        return [("agent", AGENT_TRUTH.get(raw, ("recognized", "assistant_text")), raw)
                for raw in lines]

    def test_the_development_transport_both_profiles(self):
        base = tempfile.mkdtemp(prefix="dory-derive-")
        self.addCleanup(shutil.rmtree, base, True)
        agent = os.path.join(base, "logging_agent.py")
        with open(agent, "w") as handle:
            handle.write(LOGGING_AGENT)
        for profile in ("one_shot", "persistent"):
            with self.subTest(profile=profile):
                log = os.path.join(base, "%s.log" % profile)
                harness = support.harness({"launcher": "dev-local", "options": {
                    "profile": profile, "command": [sys.executable, agent, profile, log]}})
                self.addCleanup(support.release, harness)
                chat_id = harness.create_chat("Derive %s" % profile)
                harness.send_turn(chat_id, "first")
                first = self.agent_lines(log)
                if profile == "one_shot":
                    harness.send_turn(chat_id, "second")
                    second = self.agent_lines(log, len(first))
                else:
                    harness.send_turn(chat_id, "close")
                root = harness.store.root
                derived = derive(root, chat_id)
                completed = ("launcher", ("recognized", "session_completed"))
                if profile == "one_shot":
                    # One session per turn; each call returned, and the launcher
                    # reports the process's exit as the session's end.
                    for opened_by, lines in (("first", first), ("second", second)):
                        end = [e for e in derived[opened_by]["events"]][-1]
                        self.check(derived, opened_by,
                                   self.dev_truth(lines) + [completed + (end[3],)],
                                   [answer_line(opened_by)], "completed", None)
                        self.assertEqual(json.loads(end[3].decode())["type"],
                                         "session_completed")
                else:
                    # One agent across both turns; "close" wrote nothing and
                    # closed its stream while alive, which the launcher reports
                    # as the stream's end, and the session is `unknown`.
                    end = derived["first"]["events"][-1]
                    self.check(derived, "first",
                               self.dev_truth(first) + [
                                   ("launcher", ("recognized", "stream_end"), end[3])],
                               [answer_line("first")], "unknown", None)
                    self.assertEqual(self.agent_lines(log, len(first)), [])
                    self.assertEqual(json.loads(end[3].decode())["type"], "stream_end")
                self.assert_store_valid(harness.store, "derive-dev-%s" % profile)
                support.end_chat(harness, chat_id)

    def test_the_codex_model(self):
        self.make_dirs()
        launcher = self.model()
        harness = support.harness({}, launcher=launcher)
        chat_id = harness.create_chat("Derive Codex")
        with open(os.path.join(self.codex_home, "behaviour-once"), "w") as handle:
            handle.write("synthetic-unrecognized,synthetic-malformed,synthetic-not-utf8")
        harness.send_turn(chat_id, "first")

        def read_fails(_handle, _after):
            raise lb.LauncherError("unavailable", "the model's read failed")
        launcher.events = read_fails
        harness.send_turn(chat_id, "second")
        calls = self.calls()
        self.assertEqual(len(calls), 2, "the second call ran; nothing read its output")

        def reading(line):
            if line.startswith("SYNTHETIC MODEL-ONLY LINE"):
                return ("malformed", None)
            event = json.loads(line)
            if event["type"] == "thread.started":
                return ("recognized", "thread.started")
            if event["type"] == "item.completed":
                return ("recognized", "assistant_text")
            return ("unrecognized", None)
        derived = derive(harness.store.root, chat_id)
        row = derived["first"]
        truth = [("agent", reading(line), raw)
                 for line, (_seq, _source, _reading, raw) in zip(calls[0]["stdout"],
                                                                 row["events"])]
        self.assertEqual([raw.decode("utf-8", "backslashreplace") for _s, _so, _r, raw
                          in row["events"]], calls[0]["stdout"],
                         "the preserved bytes are the first call's lines, in order")
        self.check(derived, "first", truth,
                   [raw for _s, r, raw in truth if r == ("recognized", "assistant_text")],
                   "unknown", "stream_read_failed")
        self.assertEqual(len(row["rendered"]), 1)
        self.assertEqual([r for _s, r, _b in truth if r[0] != "recognized"],
                         [("unrecognized", None), ("malformed", None), ("malformed", None)])
        self.assert_store_valid(harness.store, "derive-codex-stopped")


if __name__ == "__main__":
    unittest.main()
