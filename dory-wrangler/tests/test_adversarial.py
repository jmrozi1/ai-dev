"""Adversarial probes: attempts to break every guarantee this ticket claims.

Four independent authors on this ticket family have shipped the same defect --
a checker that tests the *label* on a claim rather than the *fact* it stands
for. So each guarantee here is attacked rather than demonstrated, and the
attacks that found nothing are recorded alongside the ones that found something.

Three kinds of probe live here:

* **Escape attempts.** Construct a store or a call that violates a guarantee and
  see whether the code accepts it. Most are expected to be refused; the ones
  that are accepted are recorded as findings with their reproduction.
* **Mutation probes.** Deliberately weaken the implementation, re-run the test
  that is supposed to be guarding it, and require that test to *fail*. A
  guarantee proven only by the test written alongside it is not proven; this is
  how that is checked rather than asserted.
* **Negative controls.** Break the world the test depends on and require the
  test to notice -- for instance, deleting the store between a kill and a
  restart and requiring the history not to come back.

Run `python3 dory-wrangler/tests/test_adversarial.py --report` for the table
that goes in the handoff.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import pagemodel
from helpers import ONE_SHOT, TESTS_DIR, answer_turn, answered_turn

from dory_wrangler import atomic
from dory_wrangler.webapp import PAGE
from dory_wrangler.errors import (
    ConcurrencyRefused,
    ProvenanceRefused,
    StoreCorrupt,
    StoreError,
)
from dory_wrangler.store import ChatStore

# Probes whose outcome is a finding rather than a refusal. Naming them here
# means a probe that starts passing (or stops finding) is a test failure rather
# than a silent change of story.
EXPECTED_FINDINGS = {
    "A6-transcription-is-not-checked",
    "A7-system-authored-assistant-text",
    "B7-a-truncated-tail-is-undetectable",
    "H1-turn-floor-rejects-an-honest-chat",
}

_RESULTS = []


class ProbeCase(unittest.TestCase):
    """Every test here is a probe; `record` captures what it found."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dory-probe-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = ChatStore(self.root)

    def record(self, name, attack, held, detail):
        _RESULTS.append(
            {"name": name, "attack": attack, "held": held, "detail": detail}
        )
        if name in EXPECTED_FINDINGS:
            self.assertFalse(
                held,
                "%s was expected to find an escape and did not; the finding it "
                "documents may have been closed, which is good news that must be "
                "written down rather than left implied" % name,
            )
        else:
            self.assertTrue(held, "%s found an escape: %s" % (name, detail))

    # -- helpers -------------------------------------------------------

    def chat_with_answer(self, title="Probe"):
        chat_id = self.store.create_chat(title)["chat_id"]
        session, event, message = answered_turn(self.store, chat_id, "q", "the answer")
        return chat_id, session, event, message

    def write_raw_message(self, chat_id, sequence, record):
        path = os.path.join(self.root, "chats", chat_id, "messages", "%08d.json" % sequence)
        with open(path, "w") as handle:
            json.dump(record, handle)
        return path


# ---------------------------------------------------------------------------
# A. No fabricated history
# ---------------------------------------------------------------------------


class TestNoFabricatedHistory(ProbeCase):
    def test_a1_citing_an_event_that_is_not_there(self):
        chat_id, session, _e, _m = self.chat_with_answer()
        held, detail = True, "refused: no such preserved event"
        try:
            self.store.append_agent_message(
                chat_id, session["session_id"], "evt_neverexisted0", "invented"
            )
            held, detail = False, "the store accepted a citation of a record that is not there"
        except ProvenanceRefused as exc:
            detail = "refused: %s" % exc
        self.record("A1-citation-does-not-resolve",
                    "agent message citing an event id no record carries", held, detail)

    def test_a2_citing_a_launcher_event(self):
        chat_id, session, _e, _m = self.chat_with_answer()
        sid = session["session_id"]
        launcher_event, _ = self.store.append_diagnostic_event(
            chat_id, sid, self.store.next_event_sequence(chat_id, sid),
            "launcher", "recognized", "launcher_note", '{"note":"anything"}',
        )
        held, detail = True, ""
        try:
            self.store.append_agent_message(
                chat_id, sid, launcher_event["event_id"], "text the agent never produced"
            )
            held, detail = False, "launcher output was rendered as chat"
        except ProvenanceRefused as exc:
            detail = "refused: %s" % exc
        self.record("A2-launcher-output-as-chat",
                    "agent message sourced to a launcher-produced event", held, detail)

    def test_a3_citing_a_malformed_event(self):
        chat_id, session, _e, _m = self.chat_with_answer()
        sid = session["session_id"]
        bad, _ = self.store.append_diagnostic_event(
            chat_id, sid, self.store.next_event_sequence(chat_id, sid),
            "agent", "malformed", None, "<<<not parseable>>>",
        )
        held, detail = True, ""
        try:
            self.store.append_agent_message(chat_id, sid, bad["event_id"], "cleaned up text")
            held, detail = False, "a malformed event reached the user as chat"
        except ProvenanceRefused as exc:
            detail = "refused: %s" % exc
        self.record("A3-malformed-event-as-chat",
                    "agent message derived from a malformed event", held, detail)

    def test_a4_citing_another_chats_event(self):
        chat_id, session, _e, _m = self.chat_with_answer("Here")
        other_id, _s, other_event, _m2 = self.chat_with_answer("Elsewhere")
        held, detail = True, ""
        try:
            self.store.append_agent_message(
                chat_id, session["session_id"], other_event["event_id"], "borrowed"
            )
            held, detail = False, "evidence from another chat was accepted"
        except (ProvenanceRefused, StoreError) as exc:
            detail = "refused: %s" % exc
        self.record("A4-cross-chat-evidence",
                    "agent message citing evidence preserved on a different chat",
                    held, detail)

    def test_a5_writing_the_record_directly_around_the_store(self):
        """The store's write-time refusal is one gate. The read must be another."""
        chat_id, session, _e, _m = self.chat_with_answer()
        sid = session["session_id"]
        launcher_event, _ = self.store.append_diagnostic_event(
            chat_id, sid, self.store.next_event_sequence(chat_id, sid),
            "launcher", "recognized", "launcher_note", '{"note":"anything"}',
        )
        self.write_raw_message(chat_id, 3, {
            "record_type": "message",
            "record_version": 1,
            "message_id": "msg_smuggled000001",
            "chat_id": chat_id,
            "sequence": 3,
            "author": "agent",
            "created_at": "2026-09-12T12:00:00Z",
            "content": {"content_type": "text/plain", "text": "I am not from the agent"},
            "session_id": sid,
            "source_event_id": launcher_event["event_id"],
        })
        codes = set(code for code, _w, _d in self.store.verify())
        held = "NON_AGENT_EVENT_RENDERED" in codes
        self.record("A5-bypass-the-store-api",
                    "write the fabricated message file directly and validate the store",
                    held, "violations: %s" % (sorted(codes) or "none"))

    def test_a6_the_text_is_never_compared_to_the_evidence(self):
        """FINDING. A citation proves a citation, not a transcription.

        The contract calls an agent message "transcribed evidence rather than
        narration" and enforces that by requiring a citation. Nothing -- not the
        contract, not the validator, not this store -- compares the message text
        with the cited event's preserved `raw.body`. A store may therefore cite
        a real, recognized, agent-sourced event and display text that has no
        relation to it, and everything accepts it.

        This is not closable here: deriving display text from a raw payload is
        #88's, so any rule about the relationship between them has to be stated
        where that derivation lives.
        """
        chat_id = self.store.create_chat("Narration")["chat_id"]
        user = self.store.append_user_message(chat_id, "what did you find?")
        session, _b = self.store.create_session(chat_id, user["message_id"], "dev-local", ONE_SHOT)
        sid = session["session_id"]
        request = self.store.append_launch_request(chat_id, sid, "what did you find?")
        self.store.append_transition(chat_id, sid, "pending", "launching", "harness",
                                     {"kind": "harness_action", "ref": request["request_id"]})
        self.store.append_launch_result(chat_id, request["request_id"], sid, "accepted",
                                        agent_handle="h1")
        self.store.append_transition(chat_id, sid, "launching", "running", "launcher",
                                     {"kind": "launch_result", "ref": request["request_id"]})
        self.store.set_agent_handle(chat_id, sid, "h1")
        event, _ = self.store.append_diagnostic_event(
            chat_id, sid, 1, "agent", "recognized", "assistant_text",
            '{"type":"assistant_text","text":"the build failed on line 12"}',
        )
        self.store.append_agent_message(
            chat_id, sid, event["event_id"], "Everything passed, no problems found."
        )
        violations = self.store.verify()
        held = bool(violations)
        self.record(
            "A6-transcription-is-not-checked",
            "agent message that cites real agent evidence but says something else",
            held,
            "accepted with %d violations; the transcript reads 'Everything passed, no "
            "problems found.' while the cited evidence says 'the build failed on line "
            "12'" % len(violations),
        )

    def test_a7_fabricated_assistant_text_as_a_system_message(self):
        """FINDING. `author: "system"` carries no provenance requirement at all.

        Contract 4.2 requires an `author: "agent"` message to cite preserved
        agent evidence, and requires `system` to cite nothing. Nothing bounds
        what a system message may say, so arbitrary assistant-sounding text is
        representable and validates. The guarantee therefore rests on the
        renderer visually separating system from agent, which is a property of
        the UI rather than of the record model.

        The shell here writes no system message and renders one distinctly
        (dashed avatar, muted italics, `SYSTEM` label), so the escape is closed
        in this implementation and open in the contract.
        """
        chat_id = self.store.create_chat("System voice")["chat_id"]
        self.store.append_user_message(chat_id, "is the deploy safe?")
        self.store.append_system_message(
            chat_id, "Yes, I checked the deploy and it is completely safe."
        )
        violations = self.store.verify()
        held = bool(violations)

        # What keeps this escape closed here is a *fact about the rendering*, so
        # that fact is derived rather than grepped for. The original assertion
        # was `".turn.system .bubble" in page`, a substring search; review
        # emptied those rules and relabelled `system` to `AGENT` while leaving
        # both greped substrings in the file, and every test stayed green. The
        # page model below computes what a reader would actually see for each
        # author -- the avatar label and the declarations that cascade onto the
        # turn, the avatar and the bubble -- from the page that is served.
        text = "Yes, I checked the deploy and it is completely safe."
        system_turn = pagemodel.render_turn(PAGE, "system", text)
        agent_turn = pagemodel.render_turn(PAGE, "agent", text)
        differences = pagemodel.rendering_differences(system_turn, agent_turn)
        renders_distinctly = bool(differences)
        self.record(
            "A7-system-authored-assistant-text",
            "unattributed assistant-sounding text carried as author: system",
            held,
            "accepted with %d violations; the same text renders differently as system "
            "than as agent in %s, so this is a contract residual rather than a defect "
            "in this shell" % (len(violations), ", ".join(differences) or "nothing"),
        )
        self.assertTrue(
            renders_distinctly,
            "the one thing keeping A7 closed here is that a system turn does not "
            "look like an agent turn, and it now does: identical avatar label %r "
            "and identical styling. Nothing in the record model bounds system "
            "text, so this rendering is the whole guard."
            % (system_turn["avatar_text"],),
        )
        # Naming the facets keeps the probe honest about *why* it is closed: an
        # author class with no CSS behind it is invisible, so a difference in
        # class names alone would not count and is excluded from the model.
        self.assertIn("avatar_text", differences,
                      "a system turn no longer carries its own label")
        self.assertIn("bubble_style", differences,
                      "a system turn's text is no longer styled apart from an "
                      "agent's")

    def test_a8_agent_output_without_an_agent(self):
        chat_id = self.store.create_chat("Never ran")["chat_id"]
        user = self.store.append_user_message(chat_id, "go")
        session, _b = self.store.create_session(chat_id, user["message_id"], "dev-local", ONE_SHOT)
        held, detail = True, ""
        try:
            self.store.append_diagnostic_event(
                chat_id, session["session_id"], 1, "agent", "recognized",
                "assistant_text", '{"text":"from an agent that never started"}',
            )
            held, detail = False, "an unlaunched session produced agent output"
        except ProvenanceRefused as exc:
            detail = "refused: %s" % exc
        self.record("A8-agent-output-without-an-agent",
                    "agent-sourced event on a session that never reached running",
                    held, detail)


# ---------------------------------------------------------------------------
# B. Contiguity
# ---------------------------------------------------------------------------


class TestContiguity(ProbeCase):
    def setUp(self):
        ProbeCase.setUp(self)
        self.chat_id = self.store.create_chat("Contiguity")["chat_id"]
        for i in range(4):
            self.store.append_user_message(self.chat_id, "turn %d" % i)
        self.messages_dir = os.path.join(self.root, "chats", self.chat_id, "messages")

    def test_b1_a_missing_middle_turn(self):
        os.unlink(os.path.join(self.messages_dir, "00000002.json"))
        held, detail = False, "a lost turn was closed silently"
        try:
            self.store.read_messages(self.chat_id)
        except StoreCorrupt as exc:
            held, detail = True, "detected: %s" % exc
        self.record("B1-missing-middle-turn", "delete a message from the middle of a chat",
                    held, detail)

    def test_b2_names_that_look_contiguous_over_records_that_are_not(self):
        """The exact defect class this ticket family keeps shipping."""
        path = os.path.join(self.messages_dir, "00000002.json")
        with open(path) as handle:
            record = json.load(handle)
        record["sequence"] = 4
        with open(path, "w") as handle:
            json.dump(record, handle)
        held, detail = False, "the file names were trusted in place of the records"
        try:
            self.store.read_messages(self.chat_id)
        except StoreCorrupt as exc:
            held, detail = True, "detected: %s" % exc
        self.record("B2-contiguous-names-over-a-gap",
                    "file names 1..4 while the records inside run 1,4,3,4", held, detail)

    def test_b3_renaming_a_file_to_close_a_gap(self):
        os.unlink(os.path.join(self.messages_dir, "00000002.json"))
        os.rename(os.path.join(self.messages_dir, "00000004.json"),
                  os.path.join(self.messages_dir, "00000002.json"))
        held, detail = False, "a renamed file closed a real gap"
        try:
            self.store.read_messages(self.chat_id)
        except StoreCorrupt as exc:
            held, detail = True, "detected: %s" % exc
        self.record("B3-rename-to-close-a-gap",
                    "move the last message into the missing sequence's file name",
                    held, detail)

    def test_b4_a_duplicate_sequence_in_two_files(self):
        path = os.path.join(self.messages_dir, "00000003.json")
        with open(path) as handle:
            record = json.load(handle)
        record["sequence"] = 2
        record["message_id"] = "msg_duplicated0001"
        with open(path, "w") as handle:
            json.dump(record, handle)
        held, detail = False, "two turns shared a sequence"
        try:
            self.store.read_messages(self.chat_id)
        except StoreCorrupt as exc:
            held, detail = True, "detected: %s" % exc
        self.record("B4-duplicate-sequence", "two message records carrying the same sequence",
                    held, detail)

    def test_b5_no_public_call_can_leave_a_gap(self):
        """Sequences are claimed by exclusive create, so skipping one is not reachable."""
        directory = self.messages_dir
        before = sorted(os.listdir(directory))
        # Take the next sequence out from under the writer, repeatedly, and check
        # it retries into the next free slot rather than skipping.
        for _ in range(3):
            nxt = self.store._next_sequence(directory)
            atomic.create_exclusive(
                os.path.join(directory, "%08d.json" % nxt),
                json.dumps({
                    "record_type": "message", "record_version": 1,
                    "message_id": "msg_stolen%06d" % nxt, "chat_id": self.chat_id,
                    "sequence": nxt, "author": "system",
                    "created_at": "2026-09-12T12:00:00Z",
                    "content": {"content_type": "text/plain", "text": "stolen slot"},
                    "session_id": None, "source_event_id": None,
                }).encode("utf-8"),
            )
            self.store.append_user_message(self.chat_id, "after the theft")
        messages = self.store.read_messages(self.chat_id)
        held = [m["sequence"] for m in messages] == list(range(1, len(messages) + 1))
        self.record("B5-sequence-theft", "steal each sequence slot just before the writer takes it",
                    held, "history is %d turns, contiguous: %s (was %d files)"
                    % (len(messages), held, len(before)))

    def test_b6_no_agent_message_survives_losing_its_evidence(self):
        chat_id, session, event, _m = self.chat_with_answer("Evidence")
        path = os.path.join(self.root, "diagnostics", chat_id, session["session_id"],
                            "00000001.json")
        os.unlink(path)
        held, detail = False, "the message kept pointing at evidence that is gone"
        try:
            codes = set(code for code, _w, _d in self.store.verify())
            held = bool(codes)
            detail = "violations: %s" % sorted(codes)
        except StoreCorrupt as exc:
            held, detail = True, "detected on read: %s" % exc
        self.record("B6-evidence-deleted-under-a-message",
                    "delete the preserved event an agent message cites", held, detail)

    def test_b7_a_truncated_tail(self):
        """FINDING, and an honest one: losing the newest turn is undetectable.

        Contiguity is from 1, so removing the last message leaves 1..n-1, which
        is a perfectly contiguous history of a chat that is one turn shorter.
        Nothing in the records distinguishes it from a chat where that turn was
        never sent. Closing it needs a per-chat count or a chained record, which
        the contract does not define and which is not #86's to invent.
        """
        os.unlink(os.path.join(self.messages_dir, "00000004.json"))
        detected = True
        try:
            messages = self.store.read_messages(self.chat_id)
            detected = False
        except StoreCorrupt:
            messages = None
        held = detected
        self.record(
            "B7-a-truncated-tail-is-undetectable",
            "delete the newest turn instead of a middle one",
            held,
            "read back %d turns with no fault reported; the loss of the most recent "
            "turn is not representable in the record model"
            % (len(messages) if messages is not None else -1),
        )


# ---------------------------------------------------------------------------
# C. Atomicity
# ---------------------------------------------------------------------------


class TestAtomicityProbes(ProbeCase):
    def test_c1_a_complete_looking_temp_file_is_still_not_history(self):
        """The decoy must be a name the reader would otherwise read.

        This probe wrote its decoy as `.tmp-sneaky`, with no `.json` suffix, so
        what skipped it was the suffix filter and not the temp-prefix filter the
        probe claims to be attacking: review deleted the temp-prefix filter and
        every test stayed green. The decoy now ends in `.json`, so the only
        thing that can keep it out of history is the guard under attack.

        A second decoy carries the suffix rule in the same way, so neither
        filter can be removed without this probe reporting an escape.
        """
        chat_id = self.store.create_chat("Temp")["chat_id"]
        first = self.store.append_user_message(chat_id, "one")
        directory = os.path.join(self.root, "chats", chat_id, "messages")
        # A fully valid record, written under a temp name that would be read if
        # the temp prefix were not filtered. If the reader accepted it, an
        # interrupted write would become history.
        decoy = {
            "record_type": "message", "record_version": 1,
            "message_id": "msg_fromatempfile1", "chat_id": chat_id,
            "sequence": 2, "author": "user",
            "created_at": "2026-09-12T12:00:00Z",
            "content": {"content_type": "text/plain", "text": "half-written"},
            "session_id": None, "source_event_id": None,
        }
        temp_named = atomic.TEMP_PREFIX + "5neaky" + ".json"
        with open(os.path.join(directory, temp_named), "w") as handle:
            json.dump(decoy, handle)
        # And one that only the suffix rule excludes: no temp prefix at all.
        suffix_only = dict(decoy, message_id="msg_fromabackupfile")
        with open(os.path.join(directory, "00000002.json.part"), "w") as handle:
            json.dump(suffix_only, handle)

        try:
            messages = self.store.read_messages(chat_id)
            detail = "history holds %r" % [m["message_id"] for m in messages]
        except StoreError as exc:
            messages = None
            detail = "read_messages failed closed on the decoy: %s" % exc
        held = messages is not None and \
            [m["message_id"] for m in messages] == [first["message_id"]]
        self.record("C1-valid-record-under-a-temp-name",
                    "a complete, contract-valid record left under an interrupted "
                    "write's name, and under a name that is not a record name",
                    held, detail)

    def test_c2_fault_injection_is_inert_without_the_environment(self):
        """The crash hook must not be reachable by anything but the test harness."""
        env = dict(os.environ)
        env.pop(atomic.FAULT_ENV, None)
        completed = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "_crashwriter.py"),
             self.root, "chat", "No fault set"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        held = completed.returncode == 0
        self.record("C2-fault-hook-inert-by-default",
                    "run the write path with no fault environment variable", held,
                    "exit %d" % completed.returncode)

    def test_c3_the_fault_hook_is_not_reachable_over_http(self):
        source_dir = os.path.join(os.path.dirname(TESTS_DIR), "src", "dory_wrangler")
        with open(os.path.join(source_dir, "webapp.py")) as handle:
            page = handle.read()
        # `preventDefault(` contains the substring `fault(`, so the search is for
        # the hook by name rather than for a fragment that reads like it.
        hits = [token for token in (atomic.FAULT_ENV, "atomic.fault", "FAULT_POINTS",
                                    "DORY_WRANGLER_FAULT")
                if token in page]
        held = not hits
        self.record("C3-fault-hook-not-on-the-http-surface",
                    "look for any route or handler that can set or trigger a fault point",
                    held, "webapp.py references: %s" % (hits or "nothing"))

    def test_c4_a_crash_during_a_lifecycle_write_cannot_split_the_pair(self):
        chat_id = self.store.create_chat("Pair")["chat_id"]
        user = self.store.append_user_message(chat_id, "go")
        session, _b = self.store.create_session(chat_id, user["message_id"], "dev-local", ONE_SHOT)
        sid = session["session_id"]
        env = dict(os.environ)
        env[atomic.FAULT_ENV] = "pre_publish"
        subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, "_crashwriter.py"),
             self.root, "transition", chat_id, sid],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        fresh = ChatStore(self.root, sweep=False)
        state = fresh.read_session(chat_id, sid)["state"]
        released = fresh.read_binding(chat_id, sid)["released_at"]
        held = (state == "pending" and released is None) and not fresh.verify()
        self.record("C4-session-and-binding-cannot-split",
                    "kill the process between a terminal transition and its binding release",
                    held, "state=%s released_at=%s" % (state, released))


# ---------------------------------------------------------------------------
# D. Reopen fidelity, and the negative controls that make it mean something
# ---------------------------------------------------------------------------


class TestReopenFidelity(ProbeCase):
    def test_d1_negative_control_the_history_does_not_come_back_from_nowhere(self):
        """If the restart test passes with the disk emptied, it is measuring nothing."""
        from shellproc import ShellProcess

        shell = ShellProcess(self.root)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        shell.post("/api/chats/%s/messages" % chat_id, {"text": "this should not survive"})
        shell.kill()

        shutil.rmtree(os.path.join(self.root, "chats"))
        shell.start()
        status = shell.status_of("GET", "/api/chats/" + chat_id)
        _s, listed = shell.get("/api/chats")
        held = status == 404 and listed == []
        self.record("D1-negative-control-emptied-store",
                    "delete the store between the kill and the restart", held,
                    "reopen status %s, list %r" % (status, listed))

    def test_d2_the_transcript_does_not_read_the_diagnostics_tree(self):
        """Contract P4, checked as a fact rather than by reading the code.

        The diagnostics tree is made unreadable. If rendering a transcript
        touched it, rendering would fail.
        """
        chat_id, session, _e, _m = self.chat_with_answer("P4")
        diagnostics = os.path.join(self.root, "diagnostics")
        mode = os.stat(diagnostics).st_mode
        os.chmod(diagnostics, 0)
        self.addCleanup(os.chmod, diagnostics, stat.S_IMODE(mode))
        try:
            from dory_wrangler.service import ChatService

            rendered = ChatService(ChatStore(self.root, sweep=False)).open_chat(chat_id)
            held = len(rendered["messages"]) == 2
            detail = "transcript rendered %d turns with the diagnostics tree unreadable" % len(
                rendered["messages"]
            )
        except (StoreError, OSError) as exc:
            held, detail = False, "rendering failed: %s" % exc
        self.record("D2-transcript-independent-of-diagnostics",
                    "make the diagnostics tree unreadable and render the transcript",
                    held, detail)

    def test_d3_reopening_needs_no_process_that_wrote_anything(self):
        chat_id, session, event, _m = self.chat_with_answer("Offline")
        # A brand new interpreter, which has never held any of this in memory.
        script = (
            "import sys;sys.path.insert(0,%r);"
            "from dory_wrangler.store import ChatStore;"
            "s=ChatStore(%r);"
            "print([m['content']['text'] for m in s.read_messages(%r)]);"
            "print(s.verify())"
            % (os.path.join(os.path.dirname(TESTS_DIR), "src"), self.root, chat_id)
        )
        completed = subprocess.run([sys.executable, "-c", script],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = completed.stdout.decode("utf-8")
        held = completed.returncode == 0 and "the answer" in out and out.strip().endswith("[]")
        self.record("D3-read-from-a-process-that-never-wrote",
                    "read the full history from a separate interpreter", held,
                    out.strip().replace("\n", " | ") or completed.stderr.decode("utf-8"))


# ---------------------------------------------------------------------------
# E. One agent per chat
# ---------------------------------------------------------------------------


class TestOneAgentPerChat(ProbeCase):
    def test_e1_a_second_live_session(self):
        chat_id = self.store.create_chat("Two agents")["chat_id"]
        first = self.store.append_user_message(chat_id, "one")
        self.store.create_session(chat_id, first["message_id"], "dev-local", ONE_SHOT)
        second = self.store.append_user_message(chat_id, "two")
        held, detail = False, "a second live agent was opened on the same chat"
        try:
            self.store.create_session(chat_id, second["message_id"], "dev-local", ONE_SHOT)
        except ConcurrencyRefused as exc:
            held, detail = True, "refused: %s" % exc
        self.record("E1-second-live-session", "open a second agent while one is non-terminal",
                    held, detail)

    def test_e2_a_second_session_written_around_the_store(self):
        chat_id = self.store.create_chat("Smuggled agent")["chat_id"]
        user = self.store.append_user_message(chat_id, "one")
        session, binding = self.store.create_session(chat_id, user["message_id"],
                                                     "dev-local", ONE_SHOT)
        clone_id = "ses_smuggled00001"
        clone = dict(session)
        clone["session_id"] = clone_id
        clone_binding = dict(binding)
        clone_binding["binding_id"] = "bnd_smuggled00001"
        clone_binding["session_id"] = clone_id
        path = os.path.join(self.root, "chats", chat_id, "sessions", clone_id + ".json")
        with open(path, "w") as handle:
            json.dump({"session": clone, "binding": clone_binding}, handle)
        codes = set(code for code, _w, _d in self.store.verify())
        held = "CONCURRENT_SESSION" in codes and "CONCURRENT_BINDING" in codes
        self.record("E2-second-session-written-directly",
                    "write a second non-terminal session and binding straight to disk",
                    held, "violations: %s" % sorted(codes))

    def test_e3_two_processes_opening_a_session_at_once(self):
        chat_id = self.store.create_chat("Race for the agent")["chat_id"]
        user = self.store.append_user_message(chat_id, "one")
        script = (
            "import sys;sys.path.insert(0,%r);"
            "from dory_wrangler.store import ChatStore;"
            "from dory_wrangler.errors import StoreError;"
            "s=ChatStore(%r, sweep=False)\n"
            "try:\n"
            "    s.create_session(%r, %r, 'dev-local', "
            "{'continuation':'fresh_binding','response_shape':'one_shot',"
            "'instruction_bound_bytes':None});print('opened')\n"
            "except StoreError as e:\n"
            "    print('refused')\n"
            % (os.path.join(os.path.dirname(TESTS_DIR), "src"), self.root,
               chat_id, user["message_id"])
        )
        children = [subprocess.Popen([sys.executable, "-c", script],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(6)]
        outcomes = []
        for child in children:
            out, err = child.communicate()
            outcomes.append(out.decode("utf-8").strip() or err.decode("utf-8").strip())
        opened = outcomes.count("opened")
        codes = set(code for code, _w, _d in self.store.verify())
        held = opened == 1 and not codes
        self.record("E3-race-to-open-an-agent",
                    "six processes call create_session on one chat at the same moment",
                    held, "%d opened, %d refused, violations: %s"
                    % (opened, len(outcomes) - opened, sorted(codes) or "none"))


# ---------------------------------------------------------------------------
# F. Export and verification integrity
# ---------------------------------------------------------------------------


class TestVerificationIntegrity(ProbeCase):
    def test_f1_the_exporter_cannot_hide_what_it_cannot_read(self):
        chat_id = self.store.create_chat("Export")["chat_id"]
        self.store.append_user_message(chat_id, "one")
        path = os.path.join(self.root, "chats", chat_id, "messages", "00000001.json")
        with open(path, "w") as handle:
            handle.write("{ truncated")
        held, detail = False, "the exporter skipped a record it could not read"
        try:
            self.store.export_records()
        except StoreCorrupt as exc:
            held, detail = True, "raised: %s" % exc
        self.record("F1-exporter-cannot-skip", "break a record and export the store",
                    held, detail)

    def test_f2_an_unknown_record_type_is_not_quietly_dropped(self):
        chat_id = self.store.create_chat("Unknown")["chat_id"]
        path = os.path.join(self.root, "chats", chat_id, "messages", "00000001.json")
        with open(path, "w") as handle:
            json.dump({"record_type": "reviewer_assignment", "record_version": 1}, handle)
        held, detail = False, "an undefined record type was ignored"
        try:
            self.store.read_messages(chat_id)
        except StoreCorrupt as exc:
            held, detail = True, "refused: %s" % exc
        self.record("F2-unknown-record-type", "add a record type the contract does not define",
                    held, detail)

    def test_f3_verify_on_an_empty_store_is_vacuous_and_nothing_leans_on_it(self):
        """An empty store validates. Recorded so no evidence rests on that."""
        empty = ChatStore(tempfile.mkdtemp(prefix="dory-empty-"))
        self.addCleanup(shutil.rmtree, empty.root, True)
        vacuous = empty.verify() == [] and empty.export_records() == []
        self.record("F3-empty-store-validates-vacuously",
                    "check whether 'the store validates' can be true of nothing at all",
                    vacuous,
                    "an empty store exports 0 records and reports no violations, so a "
                    "validation claim is only evidence alongside a record count")
        self.store.create_chat("A real record")
        self.assertEqual(len(self.store.export_records()), 1)


# ---------------------------------------------------------------------------
# G. Contract findings reachable from the integrated product
# ---------------------------------------------------------------------------


class TestContractFindings(ProbeCase):
    def test_h1_the_turn_floor_rejects_an_honest_chat(self):
        """FINDING. A user who sends twice before an answer makes the chat invalid.

        Contract 6.4's floor counts every user message before the last agent
        message as "answered", and counts only packets on sessions that reached
        `running`. Two honest and reachable v0.1 sequences therefore produce a
        store the validator rejects:

          * the user sends a second turn while the first agent is still running
            (one agent per chat means no second session, so no second packet);
          * the first launch fails and a later turn succeeds (the failed
            session's packet does not count).

        Neither involves a fabricated record. The chat is exactly what happened.
        This is not #86's to fix: the rule lives in the contract and the packets
        live in #87, so it is reported rather than worked around, and no
        #86-produced store can trigger it because #86 writes no packets and no
        agent messages.
        """
        chat_id = self.store.create_chat("Two turns, one answer")["chat_id"]
        first = self.store.append_user_message(chat_id, "please look at the build")
        # A launch that fails: no agent, so its packet does not count.
        session, _b = self.store.create_session(chat_id, first["message_id"],
                                                "dev-local", ONE_SHOT)
        self.store.append_launch_request(chat_id, session["session_id"], "please look")
        self.store.append_transition(chat_id, session["session_id"], "pending",
                                     "launch_failed", "harness",
                                     {"kind": "harness_action", "ref": None})
        # The user tries again, and this time an agent answers.
        second = self.store.append_user_message(chat_id, "any luck?")
        answer_turn(self.store, chat_id, second, "the build failed on line 12")

        codes = set(code for code, _w, _d in self.store.verify())
        held = "TURN_INSTRUCTION_MISSING" not in codes
        self.record(
            "H1-turn-floor-rejects-an-honest-chat",
            "a failed launch followed by a later successful turn, with no fabricated record",
            held,
            "the validator emits %s for a chat that records exactly what happened; "
            "reachable by #87 the moment a launch fails or a user sends twice"
            % sorted(codes),
        )


# ---------------------------------------------------------------------------
# Mutation probes: break the implementation, require the test to notice
# ---------------------------------------------------------------------------


def _run(test_id):
    suite = unittest.TestLoader().loadTestsFromName(test_id)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return result.wasSuccessful()


class TestMutations(unittest.TestCase):
    """Each mutation weakens one guard and requires its guard test to fail.

    A guarantee proven only by the test written alongside it is not proven. These
    check the other direction: with the guard gone, does anything actually go
    red?
    """

    def _mutate(self, name, attack, target, attribute, replacement, guard_test):
        original = getattr(target, attribute)
        setattr(target, attribute, replacement)
        try:
            still_passes = _run(guard_test)
        finally:
            setattr(target, attribute, original)
        held = not still_passes
        _RESULTS.append({
            "name": name, "attack": attack, "held": held,
            "detail": "with the guard removed, %s %s"
                      % (guard_test, "failed as it must" if held else "STILL PASSED"),
        })
        self.assertTrue(
            held,
            "%s: %s passed with its guard removed, so it was never testing the guard"
            % (name, guard_test),
        )

    def test_m1_without_the_provenance_check(self):
        def unguarded(self, chat_id, session_id, source_event_id, text):
            return self._append_message(chat_id, "agent", text, session_id, source_event_id)

        self._mutate(
            "M1-provenance-guard-removed",
            "let append_agent_message write without checking the cited evidence",
            ChatStore, "append_agent_message", unguarded,
            "test_store.TestAgentProvenance.test_an_agent_message_citing_a_launcher_event_is_refused",
        )

    def test_m2_without_the_contiguity_check(self):
        original = ChatStore.read_messages

        def unguarded(self, chat_id):
            try:
                return original(self, chat_id)
            except StoreCorrupt:
                return []

        self._mutate(
            "M2-contiguity-guard-removed",
            "swallow the gap detection and return what could be read",
            ChatStore, "read_messages", unguarded,
            "test_store.TestMessages.test_a_sequence_gap_is_a_detected_fault",
        )

    def test_m3_without_exclusive_sequence_allocation(self):
        original = atomic.create_exclusive

        def unguarded(path, data):
            directory = os.path.dirname(path)
            temp = os.path.join(directory, atomic.TEMP_PREFIX + os.urandom(6).hex())
            with open(temp, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)  # last writer wins instead of first writer wins

        self._mutate(
            "M3-exclusive-creation-removed",
            "replace the exclusive link with a plain rename, so a sequence can be overwritten",
            atomic, "create_exclusive", staticmethod(unguarded).__func__,
            "test_atomicity.TestConcurrentSenders.test_threads_racing_for_a_sequence_lose_nothing",
        )

    def test_m4_without_the_exporter_raising(self):
        original = ChatStore.export_records

        def unguarded(self):
            try:
                return original(self)
            except StoreCorrupt:
                return []

        self._mutate(
            "M4-exporter-swallows-errors",
            "let the exporter drop what it cannot read, so every store 'validates'",
            ChatStore, "export_records", unguarded,
            "test_store.TestExport.test_export_raises_rather_than_skipping_what_it_cannot_read",
        )

    def test_m5_without_the_binding_cardinality_check(self):
        original = ChatStore.chat_agent_status

        def unguarded(self, chat_id):
            return {"open_bindings": [], "non_terminal_sessions": []}

        self._mutate(
            "M5-one-agent-per-chat-guard-removed",
            "make the cardinality query always answer 'nothing is bound'",
            ChatStore, "chat_agent_status", unguarded,
            "test_store.TestBindings.test_one_agent_per_chat",
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report():
    # Running this file as a script makes it `__main__`, while the loader below
    # imports it again under its own name. The probe results live in that second
    # module object, so they are read from there rather than from this one.
    import test_adversarial as loaded

    suite = unittest.TestLoader().loadTestsFromNames([
        "test_adversarial.TestNoFabricatedHistory",
        "test_adversarial.TestContiguity",
        "test_adversarial.TestAtomicityProbes",
        "test_adversarial.TestReopenFidelity",
        "test_adversarial.TestOneAgentPerChat",
        "test_adversarial.TestVerificationIntegrity",
        "test_adversarial.TestContractFindings",
        "test_adversarial.TestMutations",
    ])
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    print("probe                                          outcome   what it found")
    print("-" * 100)
    results = loaded._RESULTS
    for entry in sorted(results, key=lambda e: e["name"]):
        outcome = "held" if entry["held"] else "FINDING"
        print("%-46s %-9s %s" % (entry["name"], outcome, entry["detail"]))
    print("-" * 100)
    print("%d probes; %d found an escape or a residual; probe suite %s"
          % (len(results),
             len([e for e in results if not e["held"]]),
             "passed" if result.wasSuccessful() else "FAILED"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--report" in sys.argv:
        sys.exit(report())
    unittest.main()
