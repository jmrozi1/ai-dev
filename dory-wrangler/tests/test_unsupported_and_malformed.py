"""Checkpoint `handle-unsupported-and-malformed-events`: the conversation survives
what cannot be shown, and the user is told so honestly (decision 0006).

**B. The conversation survives.** For each turn shape that renders nothing -- only
`unrecognized` events; only `malformed` lines, one of them not UTF-8; a
recognized non-text type only; an answer-shaped line missing its text; an
answer whose text is empty; and events arriving after the turn ended -- in both
wire formats, in process and over HTTP (`run_shell.py` with `dev-local` in both
profiles; `model_shell.py` hosting the Codex model): nothing crashes or stalls,
nothing is fabricated as agent text, every payload is preserved with the reading
its bytes give, the turn completes, and the next send on the same chat succeeds
and renders its answer. A one-shot `stream_end`-typed payload is a third
launcher's shape (`scripted-stub`, by configuration): the claim is still refused
exactly as before (`PRESERVE_UNATTRIBUTABLE_STREAM_END`), and the chat's exits
are measured.

**C. The notice.** Exactly one `author: "system"` message in fixed words
(`notices.NO_SHOWABLE_REPLY`) for a turn observed to end with no agent message;
none for a turn with an answer; none for a turn that never ends; none over a
payload that was lost; once across re-attachment and restarts; served and shown
as a system message.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

import pagemodel
import support
from support import StoreCheck

import internal_bridge
from shellproc import ShellProcess
from test_rendering import ModelDirs, in_thread, model_shell, post, wait_for
from dory_wrangler import launch_boundary as lb
from dory_wrangler import webapp
from dory_wrangler.launchers import dev_transport
from dory_wrangler.launchers.dev_local import DEV_AGENT
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
from dory_wrangler.notices import NO_SHOWABLE_REPLY, SYSTEM_TEXTS
from dory_wrangler.session_manager import SessionManager
from dory_wrangler.errors import StoreCorrupt, StoreError
from dory_wrangler.store import ChatStore

NOTICE = ("system", NO_SHOWABLE_REPLY)

# A development-transport agent. An instruction "<case>: words" makes it write
# that case's lines, then `turn_complete`; any other instruction is answered.
# "late" writes lines after its `turn_complete`; "hang" writes one line, creates
# `<gate>.parked` and waits for `<gate>` before finishing.
CASE_AGENT = r"""
import json, os, sys, time
out = sys.stdout.buffer
gate = sys.argv[2]
CASES = {
    "unrecognized": [b'{"type": "agent_thinking", "note": "never chat"}'],
    "malformed": [b"not json at all {{{", b"\xff\xfe not utf-8 {{{"],
    "nontext": [],
    "lookalike": [b'{"type": "assistant_text", "txt": "LOOKALIKE never chat"}'],
    "empty": [b'{"type": "assistant_text", "text": ""}'],
    "late": [],
    "hang": [],
}
def turn(instruction):
    instruction = instruction.strip()
    case = instruction.split(":", 1)[0]
    if case == "hang":
        out.write(b'{"type": "agent_thinking", "note": "before hanging"}\n')
        out.flush()
        open(gate + ".parked", "a").close()
        deadline = time.time() + 120
        while not os.path.exists(gate) and time.time() < deadline:
            time.sleep(0.05)
    lines = CASES.get(case)
    if lines is None:
        lines = [json.dumps({"type": "assistant_text", "text": "answer to: " + instruction}).encode()]
    for line in lines:
        out.write(line + b"\n")
    out.write(b'{"type": "turn_complete"}\n')
    if case == "late":
        out.write(b'{"type": "agent_thinking", "note": "after the turn ended"}\n')
        out.write(b"late and not json {{{\n")
    out.flush()
if sys.argv[1] == "one_shot":
    turn(sys.stdin.buffer.read().decode("utf-8"))
else:
    for line in sys.stdin.buffer:
        if line.strip():
            turn(line.decode("utf-8"))
"""

# Each development-transport case: the agent lines it preserves, as their
# readings, in order (before `turn_complete` unless marked late).
DEV_CASES = {
    "unrecognized": [("unrecognized", None)],
    "malformed": [("malformed", None), ("malformed", None)],
    "nontext": [],
    "lookalike": [("malformed", None)],
    "empty": [("recognized", "assistant_text")],
    "late": [],
}

# Each Codex-model case: the `behaviour-once` switches of its one turn.
MODEL_CASES = {
    "unrecognized": "synthetic-unrecognized,no-agent-message",
    "malformed": "synthetic-malformed,synthetic-not-utf8,no-agent-message",
    "nontext": "no-agent-message",
    "lookalike": "agent-message-without-text,no-agent-message",
    "empty": "empty-agent-message",
}


def case_options(test, profile):
    base = tempfile.mkdtemp(prefix="dory-cases-")
    test.addCleanup(shutil.rmtree, base, True)
    agent = os.path.join(base, "case_agent.py")
    with open(agent, "w") as handle:
        handle.write(CASE_AGENT)
    gate = os.path.join(base, "gate")
    return {"profile": profile, "command": [sys.executable, agent, profile, gate]}, gate


def raw_bytes(event):
    import base64
    raw = event["raw"]
    return base64.b64decode(raw["body"]) if raw["encoding"] == "base64" \
        else raw["body"].encode("utf-8")


def transcript(records, chat_id=None):
    return [(m["author"], m["content"]["text"]) for m in sorted(
        (r for r in records if r["record_type"] == "message"
         and (chat_id is None or r["chat_id"] == chat_id)), key=lambda m: m["sequence"])]


class Checks(StoreCheck):

    def assert_every_event_reads_as_its_bytes(self, records, interpret):
        """Every agent-sourced event is preserved with the reading its own bytes
        give; nothing it carries is chat unless it is recognized text."""
        agent_text = set(r["source_event_id"] for r in records
                         if r["record_type"] == "message" and r["author"] == "agent")
        for event in (r for r in records if r["record_type"] == "diagnostic_event"
                      and r["source"] == "agent"):
            reading = interpret(raw_bytes(event))
            self.assertEqual((event["interpretation"], event["interpreted_type"]),
                             tuple(reading[:2]))
            if event["event_id"] in agent_text:
                self.assertEqual(reading[:2], ("recognized", "assistant_text"))
                self.assertTrue(reading[2])

    def assert_system_only_fixed_words(self, records):
        for message in (r for r in records if r["record_type"] == "message"
                        and r["author"] == "system"):
            self.assertIn(message["content"]["text"], SYSTEM_TEXTS)
            self.assertIsNone(message["session_id"])
            self.assertIsNone(message["source_event_id"])


# ---------------------------------------------------------------------------
# B and C together, for the two wire formats.
# ---------------------------------------------------------------------------

class TheConversationSurvivesWhatCannotBeShown(unittest.TestCase, Checks, ModelDirs):
    """Each shape renders nothing, completes, is told once in fixed words, keeps
    every payload, and leaves the chat answering the next turn."""

    def expected_dev_readings(self, case):
        first = DEV_CASES[case] + [("recognized", "turn_complete")]
        if case == "late":
            first += [("unrecognized", None), ("malformed", None)]
        return first + [("recognized", "assistant_text"), ("recognized", "turn_complete")]

    def test_the_development_transport_in_process(self):
        for profile in ("one_shot", "persistent"):
            for case in DEV_CASES:
                with self.subTest(profile=profile, case=case):
                    options, _gate = case_options(self, profile)
                    harness = support.harness({"launcher": "dev-local", "options": options})
                    self.addCleanup(support.release, harness)
                    chat_id = harness.create_chat("B %s %s" % (profile, case))
                    first = harness.send_turn(chat_id, "%s: first" % case)
                    self.assertEqual(first.agent_message_ids, [])
                    second = harness.send_turn(chat_id, "next")
                    self.assertEqual(len(second.agent_message_ids), 1)
                    records = harness.store.export_records()
                    self.assertEqual(transcript(records), [
                        ("user", "%s: first" % case), NOTICE,
                        ("user", "next"), ("agent", "answer to: next")])
                    self.assert_every_event_reads_as_its_bytes(records, dev_transport.interpret)
                    by_session = {}
                    for e in records:
                        if e["record_type"] == "diagnostic_event" and e["source"] == "agent":
                            by_session.setdefault(e["session_id"], []).append(e)
                    opened = dict((m["message_id"], m["sequence"]) for m in records
                                  if m["record_type"] == "message")
                    sessions = sorted(support.view(harness).sessions_of(chat_id),
                                      key=lambda s: opened[s["transitions"][0]["evidence"]["ref"]])
                    readings = [(e["interpretation"], e["interpreted_type"])
                                for s in sessions
                                for e in sorted(by_session.get(s["session_id"], []),
                                                key=lambda e: e["sequence"])]
                    self.assertEqual(readings, self.expected_dev_readings(case),
                                     "every line preserved, with its reading, in order")
                    self.assertEqual([s["state"] for s in sessions],
                                     ["completed", "completed"] if profile == "one_shot"
                                     else ["running"])
                    self.assert_system_only_fixed_words(records)
                    self.assert_store_valid(harness.store, "b-dev-%s-%s" % (profile, case))
                    support.end_chat(harness, chat_id)

    def test_the_development_transport_over_http_through_run_shell(self):
        for profile in ("one_shot", "persistent"):
            options, _gate = case_options(self, profile)
            root = support.scratch_root()
            shell = ShellProcess(root, launcher="dev-local", launcher_options=options)
            self.addCleanup(shell.kill)
            shell.start()
            for case in DEV_CASES:
                with self.subTest(profile=profile, case=case):
                    _status, chat = shell.post("/api/chats", {})
                    path = "/api/chats/%s" % chat["chat_id"]
                    status, body = post(shell, path + "/messages", {"text": "%s: first" % case})
                    self.assertEqual(status, 201)
                    self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                     [("user", "%s: first" % case), NOTICE])
                    status, body = post(shell, path + "/messages", {"text": "next"})
                    self.assertEqual(status, 201)
                    self.assertEqual([(m["author"], m["text"]) for m in body["messages"]][-2:],
                                     [("user", "next"), ("agent", "answer to: next")])
            shell.kill()
            store = ChatStore(root, read_only=True)
            self.assertEqual(store.verify(), [])
            records = store.export_records()
            self.assert_every_event_reads_as_its_bytes(records, dev_transport.interpret)
            self.assert_system_only_fixed_words(records)
            self.assertEqual(sum(1 for r in records if r["record_type"] == "message"
                                 and r["author"] == "system"), len(DEV_CASES))
            self.keep(store, "b-dev-http-%s" % profile)

    def once(self, behaviour):
        with open(os.path.join(self.codex_home, "behaviour-once"), "w") as handle:
            handle.write(behaviour)

    def test_the_codex_model_in_process(self):
        for case, behaviour in sorted(MODEL_CASES.items()):
            with self.subTest(case=case):
                self.make_dirs()
                harness = support.harness({}, launcher=self.model())
                chat_id = harness.create_chat("B codex %s" % case)
                self.once(behaviour)
                self.assertEqual(harness.send_turn(chat_id, "first").agent_message_ids, [])
                self.assertEqual(len(harness.send_turn(chat_id, "second").agent_message_ids), 1)
                records = harness.store.export_records()
                self.assertEqual(transcript(records), [
                    ("user", "first"), NOTICE, ("user", "second"),
                    ("agent", "answer to: second")])
                self.assert_every_event_reads_as_its_bytes(records, internal_bridge.interpret)
                if case == "malformed":
                    self.assertIn("base64", [e["raw"]["encoding"] for e in records
                                             if e["record_type"] == "diagnostic_event"])
                self.assertEqual([s["state"] for s in support.view(harness).sessions_of(chat_id)],
                                 ["running"])
                self.assert_store_valid(harness.store, "b-codex-%s" % case)
                support.end_chat(harness, chat_id)

    def test_the_codex_model_over_http(self):
        self.make_dirs()
        root = support.scratch_root()
        shell, kill = model_shell(self, root, self.codex_home, self.spool, ())
        for case, behaviour in sorted(MODEL_CASES.items()):
            with self.subTest(case=case):
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                self.once(behaviour)
                status, body = post(shell, path + "/messages", {"text": "first"})
                self.assertEqual(status, 201)
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                 [("user", "first"), NOTICE])
                status, body = post(shell, path + "/messages", {"text": "second"})
                self.assertEqual(status, 201)
                self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                                 [("user", "first"), NOTICE, ("user", "second"),
                                  ("agent", "answer to: second")])
        kill()
        store = ChatStore(root, read_only=True)
        self.assertEqual(store.verify(), [])
        records = store.export_records()
        self.assert_every_event_reads_as_its_bytes(records, internal_bridge.interpret)
        self.assert_system_only_fixed_words(records)
        self.keep(store, "b-codex-http")


# ---------------------------------------------------------------------------
# B. A one-shot launcher that types a payload `stream_end`.
# ---------------------------------------------------------------------------

class SilentStreamEndStub(ScriptedStubLauncher):
    """The scripted stub, keeping its id so phase 3 reads it, whose turn has no
    answer: `turn_complete`, then its own `stream_end` on a one-shot session."""

    def _produce_turn(self, session, instruction_text):
        self._emit_agent_line(session, json.dumps({"type": "agent_thinking"}))
        self._emit_agent_line(session, json.dumps({"type": "turn_complete"}))
        self._emit(session, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                   json.dumps({"type": lb.PAYLOAD_STREAM_END}),
                   interpreted_type=lb.PAYLOAD_STREAM_END)


class AOneShotStreamEndTypedPayload(unittest.TestCase, Checks):
    """The claim is refused as before (502 over HTTP), every byte is kept, an
    answer on the turn still renders, and the chat keeps a way forward: under
    `persistent` the next send is delivered; under `fresh_binding` the launcher
    never reported its agent done, so the one agent per chat is still `running`
    and the next send is refused with the words that name Abandon -- the exit
    decision D2 gives every non-terminal state -- after which a send is served.
    The harness does not conclude the agent ended: that would be lifecycle
    inference (contract 1)."""

    OPTIONS = {"response_shape": "one_shot", "end_of_turn": "stream_end"}

    def test_over_http_through_run_shell(self):
        for continuation in ("persistent", "fresh_binding"):
            with self.subTest(continuation=continuation):
                root = support.scratch_root()
                shell = ShellProcess(root, launcher="scripted-stub",
                                     launcher_options=dict(self.OPTIONS,
                                                           continuation=continuation))
                self.addCleanup(shell.kill)
                shell.start()
                _status, chat = shell.post("/api/chats", {})
                path = "/api/chats/%s" % chat["chat_id"]
                self.assertEqual(post(shell, path + "/messages", {"text": "first"}),
                                 (502, {"error": webapp.INTEGRATION_FAILED}))
                status, served = shell.raw("GET", path)
                self.assertEqual([(m["author"], m["text"])
                                  for m in json.loads(served)["messages"]],
                                 [("user", "first"), ("agent", "answer to: first")],
                                 "the answer renders; no notice for a turn that has one")
                status, body = post(shell, path + "/messages", {"text": "second"})
                if continuation == "persistent":
                    self.assertEqual(status, 502)
                else:
                    self.assertEqual((status, body),
                                     (409, {"error": webapp.REFUSED_BUSY, "refused": True}))
                    self.assertEqual(post(shell, path + "/abandon", {})[0], 200)
                    status, body = post(shell, path + "/messages", {"text": "second"})
                    self.assertEqual(status, 502)
                status, served = shell.raw("GET", path)
                self.assertEqual([(m["author"], m["text"])
                                  for m in json.loads(served)["messages"]][-2:],
                                 [("user", "second"), ("agent", "answer to: second")])
                shell.kill()
                store = ChatStore(root, read_only=True)
                self.assertEqual(store.verify(), [])
                records = store.export_records()
                declined = [e for e in records if e["record_type"] == "diagnostic_event"
                            and raw_bytes(e) == b'{"type": "stream_end"}']
                self.assertEqual(len(declined), 2)
                self.assertEqual(set((e["interpretation"], e["interpreted_type"])
                                     for e in declined), {("unrecognized", None)})
                self.keep(store, "b-stub-stream-end-http-%s" % continuation)

    def test_with_nothing_to_show_the_notice_is_written_and_the_claim_still_refused(self):
        """A declined claim loses no bytes, so a turn reported ended with nothing
        to show still gets its notice -- and the refusal is still raised."""
        for continuation in ("persistent", "fresh_binding"):
            with self.subTest(continuation=continuation):
                harness = support.harness({}, launcher=SilentStreamEndStub(
                    dict(self.OPTIONS, continuation=continuation)))
                chat_id = harness.create_chat("Silent stream_end %s" % continuation)
                with self.assertRaises(lb.LaunchBoundaryError):
                    harness.send_turn(chat_id, "first")
                records = harness.store.export_records()
                self.assertEqual(transcript(records), [("user", "first"), NOTICE])
                self.assertEqual([(e["sequence"], e["interpretation"], e["interpreted_type"])
                                  for e in sorted((r for r in records
                                                   if r["record_type"] == "diagnostic_event"),
                                                  key=lambda e: e["sequence"])],
                                 [(1, "unrecognized", None), (2, "recognized", "turn_complete"),
                                  (3, "unrecognized", None)])
                self.assert_store_valid(harness.store, "c-stub-silent-stream-end-%s"
                                        % continuation)


class SilentStreamEndedPage(ScriptedStubLauncher):
    """A one-shot turn with nothing to show whose page also claims its stream
    ended -- the drain's other refusal branch, which takes the page first."""

    def _produce_turn(self, session, instruction_text):
        self._emit_agent_line(session, json.dumps({"type": "turn_complete"}))

    def events(self, agent_handle, after_sequence):
        page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
        return lb.EventsPage(page.payloads, stream_ended=True)


class Silent(ScriptedStubLauncher):
    def _produce_turn(self, session, instruction_text):
        self._emit_agent_line(session, json.dumps({"type": "turn_complete"}))


class RefusesTheNotice(ChatStore):
    """A store that cannot write the notice, as a damaged or clock-refusing one
    could not."""

    def append_system_message(self, chat_id, text):
        from dory_wrangler.errors import ValidationRefused
        raise ValidationRefused("this store cannot write the notice")


class TheNoticeOnEveryReadingPath(unittest.TestCase, Checks):
    """Sweep rows M07, M08 and M11: the refusal branch that takes a stream-ended
    page applies the rule too; and a notice that cannot be written never replaces
    a refusal the turn already reports, while on a turn with no refusal it is not
    swallowed."""

    def test_the_stream_ended_page_branch_writes_the_notice_before_refusing(self):
        harness = support.harness({}, launcher=SilentStreamEndedPage(
            {"continuation": "persistent", "response_shape": "one_shot"}))
        chat_id = harness.create_chat("Stream-ended, nothing to show")
        with self.assertRaises(lb.LaunchBoundaryError) as raised:
            harness.send_turn(chat_id, "first")
        self.assertIn("signalled that a stream ended", str(raised.exception))
        self.assertEqual(transcript(harness.store.export_records()),
                         [("user", "first"), NOTICE])
        self.assert_store_valid(harness.store, "c-stream-ended-page-silent")

    def test_a_notice_that_cannot_be_written_does_not_replace_the_refusal(self):
        root = support.scratch_root()
        manager = SessionManager(RefusesTheNotice(root), SilentStreamEndStub(
            {"continuation": "persistent", "response_shape": "one_shot",
             "end_of_turn": "stream_end"}))
        chat_id = manager.create_chat("Refusal first")
        with self.assertRaises(lb.LaunchBoundaryError) as raised:
            manager.send_turn(chat_id, "first")
        self.assertIn("no stream to end", str(raised.exception))

    def test_a_notice_that_cannot_be_written_fails_a_turn_with_no_refusal(self):
        from dory_wrangler.errors import ValidationRefused
        root = support.scratch_root()
        manager = SessionManager(RefusesTheNotice(root), Silent(
            {"continuation": "persistent", "response_shape": "one_shot"}))
        chat_id = manager.create_chat("Nothing else to report")
        with self.assertRaises(ValidationRefused):
            manager.send_turn(chat_id, "first")


class Scripted(ScriptedStubLauncher):
    """The scripted stub, keeping its id, whose every turn is `script`: a list of
    `("agent", bytes)` lines read by the development classifier, and
    `("launcher", type, raw)` records the launcher states itself."""

    script = ()

    def _produce_turn(self, session, instruction_text):
        for item in self.script:
            if item[0] == "agent":
                self._emit_agent_line(session, item[1])
            else:
                self._emit(session, lb.SOURCE_LAUNCHER, lb.INTERPRETATION_RECOGNIZED,
                           item[2], interpreted_type=item[1])
                if item[1] != lb.PAYLOAD_STREAM_END or self.capabilities.has_stream:
                    session.stream_ended = True


def scripted(script, continuation, shape):
    launcher = Scripted({"continuation": continuation, "response_shape": shape})
    launcher.script = script
    return launcher


THINKING = ("agent", b'{"type": "agent_thinking"}')


class EachObservedTurnEnd(unittest.TestCase, Checks):
    """Sweep rows M09, M16, M18, M20-M23: every way a turn is observed to end on a
    stream writes the notice, a quiet stream does not, and every way a payload is
    lost or kept is told apart by what the store holds."""

    def turn(self, launcher, store=None, name=None):
        root = support.scratch_root()
        manager = SessionManager(store(root) if store else ChatStore(root), launcher)
        chat_id = manager.create_chat(name or "Turn end")
        try:
            manager.send_turn(chat_id, "first")
            error = None
        except Exception as exc:  # noqa: BLE001 - returned to the test
            error = exc
        return manager, chat_id, error

    def test_a_terminal_lifecycle_event_alone_ends_the_turn(self):
        for kind in (lb.PAYLOAD_SESSION_COMPLETED, lb.PAYLOAD_SESSION_FAILED):
            with self.subTest(kind=kind):
                manager, chat_id, error = self.turn(scripted(
                    [THINKING, ("launcher", kind, json.dumps({"type": kind}).encode())],
                    "fresh_binding", "stream"))
                self.assertIsNone(error)
                self.assertEqual(transcript(manager.store.export_records()),
                                 [("user", "first"), NOTICE])
                self.assert_store_valid(manager.store, "c-terminal-only-%s" % kind)

    def test_an_end_of_stream_alone_ends_the_turn(self):
        manager, chat_id, error = self.turn(scripted(
            [THINKING, ("launcher", lb.PAYLOAD_STREAM_END, b'{"type": "stream_end"}')],
            "persistent", "stream"))
        self.assertIsNone(error)
        self.assertEqual(transcript(manager.store.export_records()), [("user", "first"), NOTICE])
        self.assertEqual([s["state"] for s in support.view(manager).sessions_of(chat_id)],
                         ["unknown"])
        self.assert_store_valid(manager.store, "c-stream-end-only")

    def test_a_quiet_stream_is_not_a_turn_end(self):
        manager, chat_id, error = self.turn(scripted([THINKING], "persistent", "stream"))
        self.assertIsNone(error)
        self.assertEqual(transcript(manager.store.export_records()), [("user", "first")])
        self.assert_store_valid(manager.store, "c-quiet-stream")

    def test_a_contradicting_replay_is_a_loss(self):
        """The launcher replays sequence 1 with other bytes: the store keeps the
        first and refuses the second, which was therefore not preserved."""
        class Replays(Scripted):
            def events(self, agent_handle, after_sequence):
                page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
                if after_sequence == 0:
                    return lb.EventsPage(page.payloads[:1])
                other = lb.EventPayload(1, lb.SOURCE_AGENT, lb.INTERPRETATION_UNRECOGNIZED,
                                        b'{"type": "agent_thinking", "changed": 1}')
                return lb.EventsPage([other] + [p for p in page.payloads if p.sequence > 1])
        launcher = Replays({"continuation": "persistent", "response_shape": "stream"})
        launcher.script = [THINKING, ("agent", b'{"type": "turn_complete"}')]
        manager, chat_id, error = self.turn(launcher)
        self.assertIsInstance(error, StoreError)
        self.assertEqual(transcript(manager.store.export_records()), [("user", "first")])

    def test_a_declined_claim_in_bytes_that_are_not_utf8_loses_nothing(self):
        manager, chat_id, error = self.turn(scripted(
            [("agent", b'{"type": "turn_complete"}'),
             ("launcher", lb.PAYLOAD_STREAM_END, b'{"type": "stream_end", "x": "\xff"}')],
            "persistent", "one_shot"))
        self.assertIsInstance(error, lb.LaunchBoundaryError)
        records = manager.store.export_records()
        self.assertEqual(transcript(records), [("user", "first"), NOTICE])
        self.assertIn("base64", [e["raw"]["encoding"] for e in records
                                 if e["record_type"] == "diagnostic_event"])

    def test_a_refused_terminal_transition_still_reports_the_turn_end(self):
        """The launcher said the agent completed; the store refused to record the
        transition. The bytes are kept, so the turn's notice is written and the
        store's refusal is what the turn reports."""
        class RefusesCompletion(ChatStore):
            def append_transition(self, chat_id, session_id, current, to, *args, **kwargs):
                if to == "completed":
                    raise StoreCorrupt("refused for the test: the clock stepped back")
                return ChatStore.append_transition(self, chat_id, session_id, current, to,
                                                   *args, **kwargs)
        manager, chat_id, error = self.turn(scripted(
            [THINKING, ("launcher", lb.PAYLOAD_SESSION_COMPLETED, b'{"type": "session_completed"}')],
            "fresh_binding", "stream"), store=RefusesCompletion)
        self.assertIsInstance(error, StoreCorrupt)
        self.assertEqual(transcript(manager.store.export_records()), [("user", "first"), NOTICE])


# ---------------------------------------------------------------------------
# C. When the notice is not written.
# ---------------------------------------------------------------------------

class GappedLauncher(ScriptedStubLauncher):
    """A turn whose page reports its end but loses a payload to a gap: `[unknown@1,
    turn_complete@2, unknown@4]` -- sequence 3 never arrives."""

    def _produce_turn(self, session, instruction_text):
        self._emit_agent_line(session, json.dumps({"type": "agent_thinking"}))
        self._emit_agent_line(session, json.dumps({"type": "turn_complete"}))
        session.next_sequence += 1
        self._emit_agent_line(session, json.dumps({"type": "agent_thinking", "n": 4}))


class WhenTheNoticeIsNotWritten(unittest.TestCase, Checks, ModelDirs):

    def test_not_for_a_turn_with_an_answer_and_unrenderable_events(self):
        harness = support.harness({"launcher": "dev-local", "options": {
            "profile": "one_shot",
            "command": [sys.executable, DEV_AGENT, "--profile", "one_shot",
                        "--garbage", "--unknown-type", "--lookalike"]}})
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Answer and noise")
        harness.send_turn(chat_id, "hello")
        self.make_dirs()
        codex = support.harness({}, launcher=self.model(behaviour=(
            "synthetic-unrecognized", "synthetic-malformed", "synthetic-not-utf8",
            "agent-message-without-text")))
        codex_chat = codex.create_chat("Answer and noise, codex")
        codex.send_turn(codex_chat, "hello")
        for name, h, c in (("dev", harness, chat_id), ("codex", codex, codex_chat)):
            with self.subTest(format=name):
                records = h.store.export_records()
                self.assertEqual(transcript(records), [("user", "hello"),
                                                       ("agent", "answer to: hello")])
                self.assertGreaterEqual(sum(1 for e in records
                                            if e["record_type"] == "diagnostic_event"
                                            and e["interpretation"] != "recognized"), 3)
                self.assert_store_valid(h.store, "c-answer-and-noise-%s" % name)
        support.end_chat(codex, codex_chat)

    def test_not_over_a_payload_that_was_lost(self):
        """The notice says whatever the agent sent was preserved; over a gap it
        would not be true, so nothing is written and the refusal stands."""
        for continuation in ("persistent", "fresh_binding"):
            with self.subTest(continuation=continuation):
                harness = support.harness({}, launcher=GappedLauncher(
                    {"continuation": continuation, "response_shape": "stream"}))
                chat_id = harness.create_chat("Gap %s" % continuation)
                with self.assertRaises(lb.LaunchBoundaryError):
                    harness.send_turn(chat_id, "first")
                records = harness.store.export_records()
                self.assertEqual(transcript(records), [("user", "first")])
                self.assertEqual(sorted(e["sequence"] for e in records
                                        if e["record_type"] == "diagnostic_event"), [1, 2])

    def test_not_for_a_turn_that_never_ends_even_across_a_restart(self):
        """A turn in flight has no notice, a concurrent send is refused, and after
        the shell is killed and restarted re-attachment observes nothing -- the
        development launcher cannot re-attach -- so the turn stays without one,
        as it stays without a reply. Abandon is the exit, and the next turn is
        answered."""
        options, gate = case_options(self, "persistent")
        root = support.scratch_root()
        shell = ShellProcess(root, launcher="dev-local", launcher_options=options)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]

        def quiet_post():
            try:
                return post(shell, path + "/messages", {"text": "hang: forever"})
            except Exception as exc:  # noqa: BLE001 - the shell is killed under it
                return exc
        in_thread(quiet_post)
        wait_for(gate + ".parked")
        reader = ChatStore(root, read_only=True)
        self.assertEqual(transcript(reader.export_records()), [("user", "hang: forever")])
        self.assertEqual(post(shell, path + "/messages", {"text": "refused"}),
                         (409, {"error": webapp.REFUSED_IN_FLIGHT, "refused": True}))
        shell.kill()
        shell.start()
        status, served = shell.raw("GET", path)
        self.assertEqual([(m["author"], m["text"]) for m in json.loads(served)["messages"]],
                         [("user", "hang: forever")])
        self.assertEqual(post(shell, path + "/abandon", {})[0], 200)
        status, body = post(shell, path + "/messages", {"text": "next"})
        self.assertEqual(status, 201)
        self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                         [("user", "hang: forever"), ("user", "next"),
                          ("agent", "answer to: next")])
        open(gate, "w").close()
        shell.kill()
        store = ChatStore(root, read_only=True)
        self.assertEqual(store.verify(), [])
        self.keep(store, "c-never-ends-restart")


# ---------------------------------------------------------------------------
# C. Once, across re-attachment and restart.
# ---------------------------------------------------------------------------

class Died(BaseException):
    """The harness process dying, with no unwinding into the loop's handlers."""


class OnceAcrossReattachmentAndRestart(unittest.TestCase, Checks, ModelDirs):

    def notices(self, store, chat_id):
        return [m for m in store.read_messages(chat_id) if m["author"] == "system"]

    def test_a_served_restart_writes_no_second_notice(self):
        self.make_dirs()
        root = support.scratch_root()
        shell, kill = model_shell(self, root, self.codex_home, self.spool, ())
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        with open(os.path.join(self.codex_home, "behaviour-once"), "w") as handle:
            handle.write("synthetic-unrecognized,no-agent-message")
        self.assertEqual(post(shell, path + "/messages", {"text": "first"})[0], 201)
        for _restart in range(2):
            kill()
            os.unlink(os.path.join(root, ".model-port"))  # the old shell's port
            shell, kill = model_shell(self, root, self.codex_home, self.spool, ())
            status, served = shell.raw("GET", path)
            self.assertEqual([(m["author"], m["text"])
                              for m in json.loads(served)["messages"]],
                             [("user", "first"), NOTICE])
        status, body = post(shell, path + "/messages", {"text": "second"})
        self.assertEqual(status, 201)
        self.assertEqual([(m["author"], m["text"]) for m in body["messages"]],
                         [("user", "first"), NOTICE, ("user", "second"),
                          ("agent", "answer to: second")])
        kill()
        store = ChatStore(root, read_only=True)
        self.assertEqual(store.verify(), [])
        self.keep(store, "c-codex-restarts-once")

    def reattached_twice(self, store_root, make_manager, chat_id):
        first = make_manager()
        first.reattach_on_start()
        after_one = [(m["author"], m["content"]["text"])
                     for m in first.store.read_messages(chat_id)]
        first.store.close()
        second = make_manager()
        second.reattach_on_start()
        after_two = [(m["author"], m["content"]["text"])
                     for m in second.store.read_messages(chat_id)]
        return second, after_one, after_two

    def test_re_attachment_observes_a_one_shot_turn_end(self):
        """The harness dies after the Codex call returned and before reading its
        output. At restart re-attachment reads it: a one-shot launcher serves only
        what calls that returned produced, so the turn is observed to end."""
        self.make_dirs()
        root = support.scratch_root()
        launcher = self.model()
        harness = support.harness({}, store_path=root, launcher=launcher)
        chat_id = harness.create_chat("Died before reading")
        harness.send_turn(chat_id, "first")
        with open(os.path.join(self.codex_home, "behaviour-once"), "w") as handle:
            handle.write("synthetic-malformed,no-agent-message")

        def dies(_handle, _after):
            raise Died()
        launcher.events = dies
        with self.assertRaises(Died):
            harness.send_turn(chat_id, "second")
        harness.store.close()
        self.assertEqual(transcript(ChatStore(root, read_only=True).export_records()),
                         [("user", "first"), ("agent", "answer to: first"), ("user", "second")])
        manager, after_one, after_two = self.reattached_twice(
            root, lambda: support.harness({}, store_path=root, launcher=self.model()), chat_id)
        expected = [("user", "first"), ("agent", "answer to: first"), ("user", "second"),
                    NOTICE]
        self.assertEqual(after_one, expected)
        self.assertEqual(after_two, expected, "a second restart writes nothing")
        self.assertEqual(len(manager.send_turn(chat_id, "third").agent_message_ids), 1)
        self.assert_store_valid(manager.store, "c-codex-reattach-observes-end")
        support.end_chat(manager, chat_id)

    def test_re_attachment_observes_a_reported_turn_end_on_a_stream(self):
        """The same with a `stream` launcher that keeps its agents across the
        restart: re-attachment reads the turn's `turn_complete`."""
        root = support.scratch_root()
        stub = ScriptedStubLauncher({"continuation": "persistent", "response_shape": "stream"})
        manager = SessionManager(ChatStore(root), stub)
        chat_id = manager.create_chat("Stream died before reading")
        manager.send_turn(chat_id, "first")
        answer = stub._produce_turn

        def nothing_to_show(session, _text):
            stub._emit_agent_line(session, json.dumps({"type": "agent_thinking"}))
            stub._emit_agent_line(session, b"not json {{{")
            stub._emit_agent_line(session, json.dumps({"type": "turn_complete"}))
        stub._produce_turn = nothing_to_show
        events = stub.events

        def dies(_handle, _after):
            raise Died()
        stub.events = dies
        with self.assertRaises(Died):
            manager.send_turn(chat_id, "second")
        manager.store.close()
        stub.events = events
        stub._produce_turn = answer
        manager, after_one, after_two = self.reattached_twice(
            root, lambda: SessionManager(ChatStore(root), stub), chat_id)
        expected = [("user", "first"), ("agent", "answer to: first"), ("user", "second"),
                    NOTICE]
        self.assertEqual(after_one, expected)
        self.assertEqual(after_two, expected)
        self.assertEqual(len(manager.send_turn(chat_id, "third").agent_message_ids), 1)
        self.assert_store_valid(manager.store, "c-stream-reattach-observes-end")

    def test_a_turn_end_seen_again_writes_no_second_notice(self):
        """Sweep row M25. The turn ends and gets its notice; the agent then says
        more, ending with another `turn_complete`, before any new turn, and a
        restart's re-attachment reads it. That is a turn end observed again for
        the same turn, and the notice already there is what stops a second."""
        root = support.scratch_root()
        stub = scripted([THINKING, ("agent", b'{"type": "turn_complete"}')],
                        "persistent", "stream")
        manager = SessionManager(ChatStore(root), stub)
        chat_id = manager.create_chat("Seen twice")
        manager.send_turn(chat_id, "first")
        self.assertEqual(transcript(manager.store.export_records()),
                         [("user", "first"), NOTICE])
        manager.store.close()
        handle = support.view(ChatStore(root, read_only=True)).sessions_of(chat_id)[0]["agent_handle"]
        agent = stub._session(handle)
        stub._emit_agent_line(agent, b'{"type": "agent_thinking", "late": true}')
        stub._emit_agent_line(agent, b'{"type": "turn_complete"}')
        manager = SessionManager(ChatStore(root), stub)
        manager.reattach_on_start()
        records = manager.store.export_records()
        self.assertEqual(transcript(records), [("user", "first"), NOTICE])
        self.assertEqual(len([e for e in records if e["record_type"] == "diagnostic_event"]), 4,
                         "the late lines were read and preserved")
        self.assert_store_valid(manager.store, "c-turn-end-seen-twice")

    def test_re_attachment_writes_the_notice_before_raising_a_declined_claim(self):
        """Sweep row M35. The harness dies after the call returned; the turn's
        page, read at restart, reports its end and also a claim re-attachment
        declines (a one-shot launcher's end of stream). Nothing was lost, so the
        notice is written, and the refusal leaves the chat as it was."""
        root = support.scratch_root()
        stub = scripted([("agent", b'{"type": "turn_complete"}'),
                         ("launcher", lb.PAYLOAD_STREAM_END, b'{"type": "stream_end"}')],
                        "persistent", "one_shot")
        manager = SessionManager(ChatStore(root), stub)
        chat_id = manager.create_chat("Declined at re-attachment")
        events = stub.events

        def dies(_handle, _after):
            raise Died()
        stub.events = dies
        with self.assertRaises(Died):
            manager.send_turn(chat_id, "first")
        manager.store.close()
        stub.events = events
        manager = SessionManager(ChatStore(root), stub)
        manager.reattach_on_start()
        records = manager.store.export_records()
        self.assertEqual(transcript(records), [("user", "first"), NOTICE])
        self.assertEqual(sorted((e["interpretation"], e["interpreted_type"]) for e in records
                                if e["record_type"] == "diagnostic_event"),
                         [("recognized", "turn_complete"), ("unrecognized", None)])
        self.assert_store_valid(manager.store, "c-reattach-declined-claim")

    def test_re_attachment_that_observes_no_end_writes_nothing(self):
        """Died *before* the call returned: the spool holds nothing of the turn,
        re-attachment reads an empty page, and nothing is concluded."""
        self.make_dirs()
        root = support.scratch_root()
        launcher = self.model()
        harness = support.harness({}, store_path=root, launcher=launcher)
        chat_id = harness.create_chat("Died in the call")
        harness.send_turn(chat_id, "first")

        def dies(_handle, _instruction):
            raise Died()
        launcher.deliver = dies
        with self.assertRaises(Died):
            harness.send_turn(chat_id, "second")
        harness.store.close()
        _manager, after_one, after_two = self.reattached_twice(
            root, lambda: support.harness({}, store_path=root, launcher=self.model()), chat_id)
        expected = [("user", "first"), ("agent", "answer to: first"), ("user", "second")]
        self.assertEqual(after_one, expected)
        self.assertEqual(after_two, expected)
        _manager.store.close()


# ---------------------------------------------------------------------------
# C. The words, and how they are served and shown.
# ---------------------------------------------------------------------------

class TheNoticeIsFixedHarnessWording(unittest.TestCase):

    def test_the_words_are_pinned(self):
        self.assertEqual(NO_SHOWABLE_REPLY,
                         "The agent's turn ended without a reply that can be shown here. "
                         "Whatever it sent has been preserved.")
        self.assertEqual(SYSTEM_TEXTS, frozenset([NO_SHOWABLE_REPLY]))

    def test_nothing_from_the_integration_reaches_it(self):
        """Every turn above writes the same bytes whatever the agent sent; here the
        agent's lines try to put their own words into it, and it is unchanged."""
        options, _gate = case_options(self, "one_shot")
        harness = support.harness({"launcher": "dev-local", "options": options})
        self.addCleanup(support.release, harness)
        chat_id = harness.create_chat("Integration words")
        for case in ("unrecognized", "malformed", "lookalike"):
            harness.send_turn(chat_id, "%s: %s" % (case, "the deploy is safe"))
        notices = [m for m in harness.store.read_messages(chat_id) if m["author"] == "system"]
        self.assertEqual([m["content"]["text"] for m in notices], [NO_SHOWABLE_REPLY] * 3)

    def test_it_is_served_and_shown_as_a_system_message(self):
        """From the served bytes: the chat JSON carries it as `author: "system"`,
        the list's preview is its text, and the served page renders a system turn
        visibly apart from an agent turn (label and styling), by the page model
        built from the page that is served."""
        options, _gate = case_options(self, "one_shot")
        root = support.scratch_root()
        shell = ShellProcess(root, launcher="dev-local", launcher_options=options)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        self.assertEqual(post(shell, path + "/messages", {"text": "nontext: hi"})[0], 201)
        status, served = shell.raw("GET", path)
        self.assertEqual(status, 200)
        message = json.loads(served)["messages"][-1]
        self.assertEqual((message["author"], message["text"]), NOTICE)
        status, listing = shell.raw("GET", "/api/chats")
        self.assertEqual(json.loads(listing)[0]["preview"], NO_SHOWABLE_REPLY)
        _status, page = shell.get_page()
        self.assertEqual(page, webapp.PAGE)
        system = pagemodel.render_turn(page, "system", NO_SHOWABLE_REPLY)
        agent = pagemodel.render_turn(page, "agent", NO_SHOWABLE_REPLY)
        self.assertEqual(system["avatar_text"], "SYSTEM")
        differences = pagemodel.rendering_differences(system, agent)
        self.assertIn("avatar_text", differences)
        self.assertIn("bubble_style", differences)


if __name__ == "__main__":
    unittest.main()
