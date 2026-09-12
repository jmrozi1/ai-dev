"""The durable store: create, list, open, send, and every refusal it must make."""

import json
import os
import unittest

from helpers import ONE_SHOT, PERSISTENT_STREAM, StoreCase, answered_turn

from dory_wrangler import atomic
from dory_wrangler.errors import (
    ConcurrencyRefused,
    NotFound,
    StoreError,
    ProvenanceRefused,
    StoreCorrupt,
    TransitionRefused,
    UnsupportedContentType,
    ValidationRefused,
)
from dory_wrangler.service import ChatService
from dory_wrangler.store import ChatStore


class TestChats(StoreCase):
    def test_create_list_and_open(self):
        first = self.store.create_chat("Explain the launch seam")
        second = self.store.create_chat("Something else")
        titles = [c["title"] for c in self.store.list_chats()]
        self.assertEqual(sorted(titles), ["Explain the launch seam", "Something else"])
        self.assertEqual(
            self.store.read_chat(first["chat_id"])["title"], "Explain the launch seam"
        )
        self.assertEqual(self.store.read_messages(second["chat_id"]), [])
        self.assertStoreValid()

    def test_identifiers_are_opaque_and_distinct(self):
        chats = [self.store.create_chat("c%d" % i) for i in range(8)]
        ids_seen = set(c["chat_id"] for c in chats)
        self.assertEqual(len(ids_seen), 8)
        for chat in chats:
            self.assertRegex(chat["chat_id"], r"^cht_[0-9a-z]{8,32}$")
        # Creation order must not be recoverable from the identifiers: if it
        # were, something could order by id instead of by record.
        by_id = sorted(ids_seen)
        by_creation = [c["chat_id"] for c in chats]
        self.assertNotEqual(by_id, by_creation)

    def test_title_bounds_fail_closed(self):
        with self.assertRaises(ValidationRefused):
            self.store.create_chat("")
        with self.assertRaises(ValidationRefused):
            self.store.create_chat("x" * 201)
        self.assertEqual(self.store.list_chats(), [])

    def test_unknown_chat_is_not_found(self):
        with self.assertRaises(NotFound):
            self.store.read_chat("cht_doesnotexist00")
        with self.assertRaises(NotFound):
            self.store.read_chat("not-an-identifier")


class TestMessages(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("A chat")["chat_id"]

    def test_send_and_read_in_order(self):
        for i in range(5):
            self.store.append_user_message(self.chat_id, "turn %d" % i)
        messages = self.store.read_messages(self.chat_id)
        self.assertEqual([m["sequence"] for m in messages], [1, 2, 3, 4, 5])
        self.assertEqual([m["content"]["text"] for m in messages],
                         ["turn %d" % i for i in range(5)])
        self.assertStoreValid()

    def test_user_messages_carry_null_provenance(self):
        message = self.store.append_user_message(self.chat_id, "hello")
        self.assertIsNone(message["session_id"])
        self.assertIsNone(message["source_event_id"])

    def test_richer_content_fails_closed_and_is_counted(self):
        with self.assertRaises(UnsupportedContentType):
            self.store._append_message(
                self.chat_id, "user", "# heading", None, None,
                content_type="text/markdown",
            )
        self.assertEqual(self.store.rejected_content_types, {"text/markdown": 1})
        self.assertEqual(self.store.read_messages(self.chat_id), [])

    def test_empty_text_is_refused(self):
        with self.assertRaises(ValidationRefused):
            self.store.append_user_message(self.chat_id, "")

    def test_a_sequence_gap_is_a_detected_fault(self):
        for i in range(3):
            self.store.append_user_message(self.chat_id, "turn %d" % i)
        directory = os.path.join(self.root, "chats", self.chat_id, "messages")
        os.unlink(os.path.join(directory, "00000002.json"))
        with self.assertRaises(StoreCorrupt) as caught:
            self.store.read_messages(self.chat_id)
        self.assertIn("jumps from 1 to 3", str(caught.exception))

    def test_a_name_that_lies_about_its_content_is_a_detected_fault(self):
        """The ordering comes from the record, so a tidy set of names is not enough.

        This is the shape of defect this ticket family keeps shipping: a checker
        that reads the label -- here, the file name -- instead of the fact. A
        store whose names run 1,2,3 while its records run 1,3,3 must not read as
        a contiguous history.
        """
        for i in range(3):
            self.store.append_user_message(self.chat_id, "turn %d" % i)
        directory = os.path.join(self.root, "chats", self.chat_id, "messages")
        path = os.path.join(directory, "00000002.json")
        with open(path) as handle:
            record = json.load(handle)
        record["sequence"] = 3
        with open(path, "w") as handle:
            json.dump(record, handle)
        with self.assertRaises(StoreCorrupt) as caught:
            self.store.read_messages(self.chat_id)
        self.assertIn("the file name says", str(caught.exception))

    def test_unknown_record_version_fails_closed(self):
        self.store.append_user_message(self.chat_id, "one")
        path = os.path.join(self.root, "chats", self.chat_id, "messages", "00000001.json")
        with open(path) as handle:
            record = json.load(handle)
        record["record_version"] = 2
        with open(path, "w") as handle:
            json.dump(record, handle)
        with self.assertRaises(StoreCorrupt):
            self.store.read_messages(self.chat_id)

    def test_unrecognized_field_fails_closed(self):
        self.store.append_user_message(self.chat_id, "one")
        path = os.path.join(self.root, "chats", self.chat_id, "messages", "00000001.json")
        with open(path) as handle:
            record = json.load(handle)
        record["priority"] = "high"
        with open(path, "w") as handle:
            json.dump(record, handle)
        with self.assertRaises(StoreCorrupt):
            self.store.read_messages(self.chat_id)

    def test_truncated_record_fails_closed(self):
        self.store.append_user_message(self.chat_id, "one")
        path = os.path.join(self.root, "chats", self.chat_id, "messages", "00000001.json")
        with open(path, "r+") as handle:
            data = handle.read()
            handle.seek(0)
            handle.truncate()
            handle.write(data[: len(data) // 2])
        with self.assertRaises(StoreCorrupt):
            self.store.read_messages(self.chat_id)

    def test_a_leftover_temp_file_is_never_history(self):
        """Both filters that keep an interrupted write out of history, separately.

        `_json_names` excludes a name twice over: once because it carries the
        temp prefix and once because it does not end in `.json`. This test used
        a single decoy named `.tmp-abc`, which is excluded by the *suffix*
        rule -- so deleting the temp-prefix filter entirely left it green. Each
        decoy below can only be excluded by one of the two rules, so removing
        either rule is a test failure.
        """
        first = self.store.append_user_message(self.chat_id, "one")
        directory = os.path.join(self.root, "chats", self.chat_id, "messages")

        # Only the temp-prefix filter can reject this one: it is a complete,
        # contract-valid record whose name ends in `.json`.
        temp_named = os.path.join(
            directory, atomic.TEMP_PREFIX + "5f3a9c1d" + ".json"
        )
        with open(temp_named, "w") as handle:
            json.dump({
                "record_type": "message", "record_version": 1,
                "message_id": "msg_" + "b" * 24, "chat_id": self.chat_id,
                "sequence": 2, "author": "user",
                "created_at": "2026-09-12T12:00:00Z",
                "content": {"content_type": "text/plain", "text": "half-written"},
                "session_id": None, "source_event_id": None,
            }, handle)

        # Only the suffix filter can reject this one: no temp prefix.
        with open(os.path.join(directory, "00000002.json.part"), "w") as handle:
            handle.write('{"record_type": "message"')  # deliberately truncated

        # And the original decoy, which either rule would reject.
        with open(os.path.join(directory, atomic.TEMP_PREFIX + "abc"), "w") as handle:
            handle.write('{"record_type": "message"')  # deliberately truncated

        history = self.store.read_messages(self.chat_id)
        self.assertEqual(
            [m["message_id"] for m in history], [first["message_id"]],
            "an interrupted write's artifact reached the user-visible history",
        )
        self.assertStoreValid()


class TestAgentProvenance(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Provenance")["chat_id"]
        self.session, self.event, self.message = answered_turn(
            self.store, self.chat_id, "what is the seam?", "the seam hides launching"
        )
        self.sid = self.session["session_id"]

    def test_an_answered_turn_validates(self):
        self.assertStoreValid()
        messages = self.store.read_messages(self.chat_id)
        self.assertEqual([m["author"] for m in messages], ["user", "agent"])
        self.assertEqual(messages[1]["source_event_id"], self.event["event_id"])

    def test_an_agent_message_citing_nothing_is_refused(self):
        with self.assertRaises(ProvenanceRefused):
            self.store.append_agent_message(self.chat_id, self.sid, "evt_notinthestore", "invented")

    def test_an_agent_message_citing_a_launcher_event_is_refused(self):
        """Only the agent's own output is ever chat (contract 4.2, 7 P2a)."""
        launcher_event, _ = self.store.append_diagnostic_event(
            self.chat_id, self.sid, self.store.next_event_sequence(self.chat_id, self.sid),
            "launcher", "recognized", "launcher_note", '{"note":"started"}',
        )
        with self.assertRaises(ProvenanceRefused) as caught:
            self.store.append_agent_message(
                self.chat_id, self.sid, launcher_event["event_id"], "invented"
            )
        self.assertIn("never chat", str(caught.exception))

    def test_an_agent_message_citing_a_malformed_event_is_refused(self):
        bad, _ = self.store.append_diagnostic_event(
            self.chat_id, self.sid, self.store.next_event_sequence(self.chat_id, self.sid),
            "agent", "malformed", None, "<<not json at all>>",
        )
        with self.assertRaises(ProvenanceRefused):
            self.store.append_agent_message(self.chat_id, self.sid, bad["event_id"], "invented")

    def test_an_agent_message_citing_another_chats_event_is_refused(self):
        other = self.store.create_chat("Elsewhere")["chat_id"]
        _s, other_event, _m = answered_turn(self.store, other, "hi", "hello")
        with self.assertRaises(ProvenanceRefused):
            self.store.append_agent_message(
                self.chat_id, self.sid, other_event["event_id"], "invented"
            )

    def test_agent_output_presupposes_an_agent(self):
        """An event sourced to the agent needs a session that reached running."""
        chat_id = self.store.create_chat("Never launched")["chat_id"]
        user = self.store.append_user_message(chat_id, "go")
        session, _b = self.store.create_session(
            chat_id, user["message_id"], "dev-local", ONE_SHOT
        )
        with self.assertRaises(ProvenanceRefused) as caught:
            self.store.append_diagnostic_event(
                chat_id, session["session_id"], 1, "agent", "recognized",
                "assistant_text", '{"text":"I was never started"}',
            )
        self.assertIn("never entered 'running'", str(caught.exception))


class TestBindings(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Bindings")["chat_id"]

    def test_one_agent_per_chat(self):
        first = self.store.append_user_message(self.chat_id, "first turn")
        self.store.create_session(self.chat_id, first["message_id"], "dev-local", ONE_SHOT)
        second = self.store.append_user_message(self.chat_id, "second turn")
        with self.assertRaises(ConcurrencyRefused) as caught:
            self.store.create_session(self.chat_id, second["message_id"], "dev-local", ONE_SHOT)
        self.assertIn("one agent", str(caught.exception))

    def test_a_session_may_not_be_opened_by_a_turn_that_is_not_there(self):
        with self.assertRaises(ValidationRefused):
            self.store.create_session(self.chat_id, "msg_nothinghere00", "dev-local", ONE_SHOT)

    def test_a_session_may_not_be_opened_by_an_agent_turn(self):
        answered_turn(self.store, self.chat_id, "q", "a")
        agent_message = [m for m in self.store.read_messages(self.chat_id)
                         if m["author"] == "agent"][0]
        with self.assertRaises(ValidationRefused) as caught:
            self.store.create_session(
                self.chat_id, agent_message["message_id"], "dev-local", ONE_SHOT
            )
        self.assertIn("opened by a user turn", str(caught.exception))

    def test_sequential_rebinding_after_release(self):
        answered_turn(self.store, self.chat_id, "turn one", "answer one")
        answered_turn(self.store, self.chat_id, "turn two", "answer two")
        self.assertEqual(
            self.store.chat_agent_status(self.chat_id),
            {"open_bindings": [], "non_terminal_sessions": []},
        )
        self.assertEqual(len(self.store.list_sessions(self.chat_id)), 2)
        self.assertStoreValid()

    def test_a_terminal_transition_releases_the_binding_in_the_same_write(self):
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", ONE_SHOT
        )
        sid = session["session_id"]
        self.assertIsNone(self.store.read_binding(self.chat_id, sid)["released_at"])
        self.store.append_transition(
            self.chat_id, sid, "pending", "launch_failed", "harness",
            {"kind": "harness_action", "ref": None},
        )
        self.assertIsNotNone(self.store.read_binding(self.chat_id, sid)["released_at"])
        self.assertStoreValid()


class TestTransitions(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Transitions")["chat_id"]
        user = self.store.append_user_message(self.chat_id, "go")
        self.session, _b = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM
        )
        self.sid = self.session["session_id"]

    def test_an_unauthorized_pair_is_refused(self):
        with self.assertRaises(TransitionRefused):
            self.store.append_transition(
                self.chat_id, self.sid, "pending", "running", "launcher",
                {"kind": "launch_result", "ref": "req_x0000000"},
            )

    def test_the_wrong_owner_is_refused(self):
        request = self.store.append_launch_request(self.chat_id, self.sid, "go")
        with self.assertRaises(TransitionRefused):
            self.store.append_transition(
                self.chat_id, self.sid, "pending", "launching", "launcher",
                {"kind": "harness_action", "ref": request["request_id"]},
            )

    def test_a_stale_expected_state_is_refused(self):
        request = self.store.append_launch_request(self.chat_id, self.sid, "go")
        self.store.append_transition(
            self.chat_id, self.sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]},
        )
        with self.assertRaises(ConcurrencyRefused):
            self.store.append_transition(
                self.chat_id, self.sid, "pending", "launch_failed", "harness",
                {"kind": "harness_action", "ref": None},
            )

    def test_nothing_leaves_a_terminal_state(self):
        self.store.append_transition(
            self.chat_id, self.sid, "pending", "launch_failed", "harness",
            {"kind": "harness_action", "ref": None},
        )
        with self.assertRaises(TransitionRefused):
            self.store.append_transition(
                self.chat_id, self.sid, "launch_failed", "running", "launcher",
                {"kind": "launch_result", "ref": "req_x0000000"},
            )

    def test_a_handle_nobody_issued_is_refused(self):
        with self.assertRaises(ValidationRefused) as caught:
            self.store.set_agent_handle(self.chat_id, self.sid, "invented-handle")
        self.assertIn("no accepted launch_result", str(caught.exception))


class TestDiagnostics(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Diagnostics")["chat_id"]
        self.session, self.event, _m = answered_turn(
            self.store, self.chat_id, "q", "a", capabilities=PERSISTENT_STREAM
        )
        self.sid = self.session["session_id"]

    def test_diagnostics_are_stored_outside_the_chat_tree(self):
        chat_tree = os.path.join(self.root, "chats", self.chat_id)
        found = []
        for base, _dirs, files in os.walk(chat_tree):
            for name in files:
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(base, name)) as handle:
                    if '"diagnostic_event"' in handle.read():
                        found.append(os.path.join(base, name))
        self.assertEqual(found, [], "a diagnostic event was written inside the chat tree")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "diagnostics", self.chat_id)))

    def test_a_replayed_sequence_is_already_stored(self):
        body = '{"type":"assistant_text","text":"again"}'
        first, created = self.store.append_diagnostic_event(
            self.chat_id, self.sid, 9, "agent", "recognized", "assistant_text", body
        )
        self.assertTrue(created)
        second, created_again = self.store.append_diagnostic_event(
            self.chat_id, self.sid, 9, "agent", "recognized", "assistant_text", body
        )
        self.assertFalse(created_again)
        self.assertEqual(first["event_id"], second["event_id"])

    def test_a_different_payload_at_a_taken_sequence_is_not_a_replay(self):
        """Same sequence is the label; same payload is the fact."""
        self.store.append_diagnostic_event(
            self.chat_id, self.sid, 9, "agent", "recognized", "assistant_text", '{"a":1}'
        )
        with self.assertRaises(StoreCorrupt):
            self.store.append_diagnostic_event(
                self.chat_id, self.sid, 9, "agent", "recognized", "assistant_text", '{"a":2}'
            )

    def test_bounded_out_of_band_retrieval(self):
        for i in range(20):
            self.store.append_diagnostic_event(
                self.chat_id, self.sid, self.store.next_event_sequence(self.chat_id, self.sid),
                "launcher", "unrecognized", None, '{"i":%d}' % i,
            )
        everything = self.store.read_diagnostic_events(self.chat_id, limit=5)
        self.assertEqual(len(everything), 5)
        narrowed = self.store.read_diagnostic_events(
            self.chat_id, session_id=self.sid, sequence_from=3, sequence_to=5
        )
        self.assertEqual([e["sequence"] for e in narrowed], [3, 4, 5])
        # It is a retrieval, not a view: preserved records come back unchanged.
        self.assertTrue(all(e["record_type"] == "diagnostic_event" for e in narrowed))

    def test_the_out_of_band_retrieval_refuses_a_session_id_that_is_not_one(self):
        """Contract section 3: no filename may substitute for an identifier.

        `session_id` becomes a path component in this call, so a value that is
        not an opaque identifier must be refused rather than resolved. Two
        concrete consequences are checked here, not just the refusal: a relative
        `session_id` must not return another chat's preserved events, and it
        must not reach a directory outside the store root.
        """
        other_id = self.store.create_chat("Somebody else")["chat_id"]
        other_session, other_event, _m = answered_turn(
            self.store, other_id, "their question", "their private answer"
        )
        other_sid = other_session["session_id"]
        # The events really are there, so a leak would have something to leak.
        self.assertTrue(
            self.store.read_diagnostic_events(other_id, session_id=other_sid)
        )

        outside = os.path.join(self.root, "not-the-store")
        os.makedirs(outside)
        with open(os.path.join(outside, "00000001.json"), "w") as handle:
            json.dump({
                "record_type": "diagnostic_event", "record_version": 1,
                "event_id": "evt_" + "a" * 24, "chat_id": self.chat_id,
                "session_id": self.sid, "sequence": 1,
                "received_at": "2026-09-12T12:00:00Z", "source": "agent",
                "interpretation": "recognized", "interpreted_type": "assistant_text",
                "raw": {"encoding": "utf-8", "body": "planted outside the store root"},
            }, handle)

        cross_chat = os.path.join("..", other_id, other_sid)
        reaches_out = os.path.relpath(
            outside, os.path.join(self.root, "diagnostics", self.chat_id)
        )
        # Both halves of "opaque identifier": a value that is not an identifier
        # at all, and a value that is a perfectly well-formed identifier of the
        # wrong kind. The second matters because the weakest plausible version
        # of this guard -- reject traversal, or accept any well-formed id --
        # passes the first half and lets a chat or event id address a session.
        bad_session_ids = (
            cross_chat, reaches_out, "../..", "ses_not a real id",
            "not-an-identifier", "", ".", "/etc",
            other_id, other_event["event_id"], self.chat_id,
            "ses_" + "z" * 40, "SES_" + "a" * 12,
        )
        for bad in bad_session_ids:
            with self.assertRaises(NotFound, msg=(
                    "read_diagnostic_events resolved %r as a session; nothing but "
                    "an opaque session identifier may address a session" % (bad,))):
                self.store.read_diagnostic_events(self.chat_id, session_id=bad)

        # The same property on every other diagnostic entry point, all of which
        # address a session through `_events_dir`. V1 was one path out of five
        # that skipped this check, so the check is asserted on all five.
        for bad in bad_session_ids:
            for call in (
                lambda sid: self.store.next_event_sequence(self.chat_id, sid),
                lambda sid: self.store.read_diagnostic_event(
                    self.chat_id, sid, other_event["event_id"]),
                lambda sid: self.store.read_all_events_of_session(self.chat_id, sid),
                lambda sid: self.store.append_diagnostic_event(
                    self.chat_id, sid, 1, "launcher", "unrecognized", None, "{}"),
            ):
                with self.assertRaises(StoreError, msg=(
                        "a diagnostic entry point resolved %r as a session" % (bad,))):
                    call(bad)

        # And the chat half of the address, which is also a path component here.
        for bad_chat in ("../..", os.path.join("..", other_id), "not-a-chat-id",
                         other_sid, ""):
            with self.assertRaises(StoreError, msg=(
                    "read_diagnostic_events resolved %r as a chat" % (bad_chat,))):
                self.store.read_diagnostic_events(bad_chat)

        # The property, not the refusal: nothing this call can be asked for
        # returns a record belonging to another chat or from outside the root.
        mine = self.store.read_diagnostic_events(self.chat_id)
        self.assertTrue(mine)
        for record in mine:
            self.assertEqual(record["chat_id"], self.chat_id)
        self.assertNotIn(
            other_event["event_id"], [r["event_id"] for r in mine],
            "another chat's preserved event came back from this chat's retrieval",
        )
        self.assertNotIn(
            "planted outside the store root",
            json.dumps(mine),
            "the retrieval read a record from outside the store root",
        )

    def test_an_unrecognized_event_is_preserved_verbatim(self):
        body = "¡raw bytes, unparsed!"
        event, _ = self.store.append_diagnostic_event(
            self.chat_id, self.sid, self.store.next_event_sequence(self.chat_id, self.sid),
            "launcher", "unrecognized", None, body,
        )
        reread = self.store.read_diagnostic_event(self.chat_id, self.sid, event["event_id"])
        self.assertEqual(reread["raw"]["body"], body)


class TestExport(StoreCase):
    def test_every_store_this_code_produces_validates(self):
        for title, user_text, agent_text in [
            ("One", "first", "reply one"),
            ("Two", "second", "reply two"),
        ]:
            chat_id = self.store.create_chat(title)["chat_id"]
            answered_turn(self.store, chat_id, user_text, agent_text)
            answered_turn(self.store, chat_id, user_text + " again", agent_text + " again")
        self.assertStoreValid()

    def test_export_raises_rather_than_skipping_what_it_cannot_read(self):
        """An exporter that skips a broken record makes 'it validates' meaningless."""
        chat_id = self.store.create_chat("Export")["chat_id"]
        self.store.append_user_message(chat_id, "one")
        path = os.path.join(self.root, "chats", chat_id, "messages", "00000001.json")
        with open(path, "w") as handle:
            handle.write("{ not json")
        with self.assertRaises(StoreCorrupt):
            self.store.export_records()

    def test_export_reaches_diagnostics_no_chat_points_at(self):
        chat_id = self.store.create_chat("Orphan")["chat_id"]
        session, _e, _m = answered_turn(self.store, chat_id, "q", "a")
        stray = os.path.join(self.root, "diagnostics", chat_id, "ses_strayssssss01")
        os.makedirs(stray)
        with open(os.path.join(stray, "00000001.json"), "w") as handle:
            json.dump(
                {
                    "record_type": "diagnostic_event",
                    "record_version": 1,
                    "event_id": "evt_stray00000001",
                    "chat_id": chat_id,
                    "session_id": "ses_strayssssss01",
                    "sequence": 1,
                    "received_at": "2026-09-12T10:00:00Z",
                    "source": "launcher",
                    "interpretation": "unrecognized",
                    "interpreted_type": None,
                    "raw": {"encoding": "utf-8", "body": "{}"},
                },
                handle,
            )
        records = self.store.export_records()
        self.assertIn(
            "evt_stray00000001",
            [r.get("event_id") for r in records],
            "an event outside every known session escaped the export and so escaped the contract",
        )
        violations = self.store.verify()
        self.assertIn("DANGLING_REFERENCE", set(v[0] for v in violations))


class TestService(StoreCase):
    """The shell's use cases, below HTTP."""

    def setUp(self):
        StoreCase.setUp(self)
        self.seen = []
        self.service = ChatService(
            self.store,
            turn_listener=lambda chat_id, message_id: self.seen.append((chat_id, message_id)),
        )

    def test_the_turn_listener_sees_a_turn_that_is_already_durable(self):
        """#87's seam: it is told after the write, and only because a user sent one."""
        chat_id = self.service.create_chat()["chat_id"]
        self.assertEqual(self.seen, [], "a listener fired without a user turn")
        self.service.send_user_message(chat_id, "a turn")
        self.assertEqual(len(self.seen), 1)
        notified_chat, notified_message = self.seen[0]
        self.assertEqual(notified_chat, chat_id)
        durable = self.store.read_messages(chat_id)
        self.assertEqual(durable[0]["message_id"], notified_message)

    def test_open_chat_reads_from_disk_every_time(self):
        chat_id = self.service.create_chat("Fresh")["chat_id"]
        self.service.send_user_message(chat_id, "first")
        self.store.append_user_message(chat_id, "written by something else")
        self.assertEqual(
            [m["text"] for m in self.service.open_chat(chat_id)["messages"]],
            ["first", "written by something else"],
        )

    def test_the_conversation_list_carries_no_worker_state(self):
        chat_id = self.service.create_chat("Listed")["chat_id"]
        answered_turn(self.store, chat_id, "q", "a")
        entry = self.service.list_chats()[0]
        self.assertEqual(
            sorted(entry), ["chat_id", "created_at", "message_count", "preview",
                            "title", "updated_at"],
        )


class TestReopenWithoutAnAgent(StoreCase):
    def test_a_fresh_process_reads_the_whole_history(self):
        """Contract D1: no live agent, no live launcher, no provider conversation."""
        chat_id = self.store.create_chat("Durable")["chat_id"]
        answered_turn(self.store, chat_id, "what happened?", "here is what happened")
        before = self.store.read_messages(chat_id)

        reopened = ChatStore(self.root)
        after = reopened.read_messages(chat_id)
        self.assertEqual(before, after)
        self.assertEqual([m["author"] for m in after], ["user", "agent"])
        self.assertEqual(
            reopened.chat_agent_status(chat_id),
            {"open_bindings": [], "non_terminal_sessions": []},
        )
        # D2: the evidence the agent message was derived from is still there.
        agent_message = after[1]
        event = reopened.read_diagnostic_event(
            chat_id, agent_message["session_id"], agent_message["source_event_id"]
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["source"], "agent")


if __name__ == "__main__":
    unittest.main()
