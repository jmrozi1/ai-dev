"""The durable store: create, list, open, send, and every refusal it must make."""

import json
import os
import shutil
import tempfile
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

    def test_the_diagnostics_address_refuses_a_chat_id_that_is_not_one(self):
        """The other half of the same `os.path.join`.

        The sibling test above drives `session_id`, which is the argument the
        first finding named. This drives `chat_id`, which is the argument that
        sits beside it in the same expression and was validated on two of the
        five diagnostic entry points and not on the other three -- so another
        chat's preserved agent output, and records outside the store root, came
        back through `next_event_sequence`, `read_diagnostic_event` and
        `read_all_events_of_session`.

        It is written as the mirror image of the `session_id` test on purpose.
        The defect was never that one check was missing; it was that the two
        arguments of one join were held to different standards, and a test that
        covered one argument thoroughly and the other incidentally is what let
        that survive a repair aimed straight at it.
        """
        other_id = self.store.create_chat("Somebody else")["chat_id"]
        other_session, other_event, _m = answered_turn(
            self.store, other_id, "their question", "their private answer"
        )
        other_sid = other_session["session_id"]
        self.assertTrue(self.store.read_all_events_of_session(other_id, other_sid))

        outside = os.path.join(self.root, "not-the-store", "ses_" + "b" * 24)
        os.makedirs(outside)
        with open(os.path.join(outside, "00000001.json"), "w") as handle:
            json.dump({
                "record_type": "diagnostic_event", "record_version": 1,
                "event_id": "evt_" + "c" * 24, "chat_id": other_id,
                "session_id": "ses_" + "b" * 24, "sequence": 1,
                "received_at": "2026-09-12T12:00:00Z", "source": "agent",
                "interpretation": "recognized", "interpreted_type": "assistant_text",
                "raw": {"encoding": "utf-8", "body": "planted outside the store root"},
            }, handle)

        diagnostics = os.path.join(self.root, "diagnostics")
        # Traversal, wrong-kind and malformed, exactly as for `session_id`. The
        # wrong-kind rows matter for the same reason: a guard that only filters
        # `..` lets a session or event identifier address a chat.
        bad_chats = (
            os.path.join("..", "diagnostics", other_id),
            os.path.relpath(os.path.dirname(outside), diagnostics),
            "..", ".", "", "/etc", os.path.join("..", "..", "..", "etc"),
            self.chat_id + os.sep + ".." + os.sep + ".." + os.sep + "x",
            self.chat_id + os.sep, "." + os.sep + self.chat_id,
            self.sid, other_event["event_id"], "not-a-chat-id",
            "cht_" + "z" * 40, "CHT_" + "a" * 12, "cht_aaaaaaa", 5, None,
        )
        entry_points = (
            ("next_event_sequence",
             lambda cid, sid: self.store.next_event_sequence(cid, sid)),
            ("append_diagnostic_event",
             lambda cid, sid: self.store.append_diagnostic_event(
                 cid, sid, 1, "launcher", "unrecognized", None, "{}")),
            ("read_diagnostic_event",
             lambda cid, sid: self.store.read_diagnostic_event(
                 cid, sid, other_event["event_id"])),
            ("read_diagnostic_events",
             lambda cid, sid: self.store.read_diagnostic_events(cid, session_id=sid)),
            ("read_all_events_of_session",
             lambda cid, sid: self.store.read_all_events_of_session(cid, sid)),
        )
        for name, call in entry_points:
            for bad in bad_chats:
                with self.assertRaises(StoreError, msg=(
                        "%s resolved %r as a chat; nothing but an opaque chat "
                        "identifier may address a chat" % (name, bad))):
                    call(bad, other_sid)

        # The property, not the refusal. Every entry point, asked for the chat
        # it was given, returns that chat's records and nothing else.
        mine = self.store.read_all_events_of_session(self.chat_id, self.sid)
        self.assertTrue(mine)
        for record in mine:
            self.assertEqual(record["chat_id"], self.chat_id)
        self.assertNotIn("their private answer", json.dumps(mine))
        self.assertNotIn("planted outside the store root", json.dumps(mine))
        self.assertIsNone(
            self.store.read_diagnostic_event(
                self.chat_id, self.sid, other_event["event_id"]),
            "another chat's preserved event was located from this chat",
        )

    def test_the_retrieval_still_requires_the_chat_to_exist(self):
        """A guard this rail's own repair stopped holding, put back under test.

        `read_diagnostic_events` called `read_chat(chat_id)`, and before this
        rail that call was the only thing validating `chat_id` on this path --
        so removing it went red. Now `_chat_events_dir` checks the identifier's
        *shape*, and removing `read_chat` leaves only the *existence* check
        unheld: a well-formed identifier for a chat that does not exist would
        return `[]` instead of refusing, and the caller would read "this chat
        has no diagnostics" out of "there is no such chat".

        The mechanical enumeration on this rail found that (N27) in the repair
        rather than in the code it repaired, which is the second time on this
        ticket that a fix has quietly demoted a guard it was standing on.
        """
        absent = "cht_" + "f" * 24
        self.assertNotEqual(absent, self.chat_id)
        with self.assertRaises(NotFound):
            self.store.read_diagnostic_events(absent)
        with self.assertRaises(NotFound):
            self.store.read_diagnostic_events(absent, session_id=self.sid)

    def test_an_unrecognized_event_is_preserved_verbatim(self):
        body = "¡raw bytes, unparsed!"
        event, _ = self.store.append_diagnostic_event(
            self.chat_id, self.sid, self.store.next_event_sequence(self.chat_id, self.sid),
            "launcher", "unrecognized", None, body,
        )
        reread = self.store.read_diagnostic_event(self.chat_id, self.sid, event["event_id"])
        self.assertEqual(reread["raw"]["body"], body)


class TestPathComponents(unittest.TestCase):
    """The class the diagnostics address defect belongs to, as one property.

    Two rails have now repaired one instance of "a caller-supplied value becomes
    a path component without being checked" -- V1 on `session_id`, V2 on the
    `chat_id` sitting beside it in the same `os.path.join`. Repairing instances
    is what let the second one exist, so the property is stated over the module
    rather than over the two call sites the findings happened to name.

    The property: **in the store, no parameter of a function is ever handed
    straight to `os.path.join` as a path component.** A parameter is exactly the shape a caller's
    value has, so every such value must pass through `_under`, which checks each
    component. Anything else joined -- a string literal, a name read out of
    `os.listdir`, a filename derived from an integer -- is a value the store
    produced itself.

    Checked against the source rather than against a list of known call sites,
    so a *new* join added later is caught by the same test. Run against the
    store as it stood before this rail, it reports `_events_dir` on both of its
    arguments, which is V1 and V2 together.
    """

    ALLOWED = {
        # `_under` is the door itself: it joins a store-derived base with
        # components it has just checked one by one. Exempting it is what makes
        # the rule "go through the door", not "never join".
        "_under",
    }

    def _offenders(self, source, filename):
        import ast

        tree = ast.parse(source, filename)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in self.ALLOWED:
                continue
            args = node.args
            names = set()
            for group in (args.posonlyargs, args.args, args.kwonlyargs):
                names.update(a.arg for a in group)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    names.add(extra.arg)
            names.discard("self")
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                if not (isinstance(func, ast.Attribute) and func.attr == "join"):
                    continue
                # Position 0 is the base every join needs, and a base is a
                # path the store already derived (a chat directory, a staging
                # directory). A *component* is what a caller's value would
                # become, so the rule is stated over every position after the
                # first.
                for arg in inner.args[1:]:
                    # The whole expression, not the bare name. The first version
                    # of this check looked only at `ast.Name` arguments and
                    # stayed green when `_session_path` and `_append_packet`
                    # were reverted, because both join `parameter + ".json"` --
                    # a parameter one node deeper. Looking at the argument and
                    # not at the expression around it is the same mistake as
                    # checking one argument of a join and not its neighbour.
                    for sub_node in ast.walk(arg):
                        if isinstance(sub_node, ast.Name) and sub_node.id in names:
                            offenders.append((node.name, sub_node.id, inner.lineno))
        return offenders

    def _store_source(self):
        import dory_wrangler.store as store_module

        path = store_module.__file__
        with open(path, encoding="utf-8") as handle:
            return handle.read(), path

    def test_no_parameter_is_joined_into_a_path_without_being_checked(self):
        source, path = self._store_source()
        offenders = self._offenders(source, path)
        self.assertEqual(
            [], offenders,
            "a caller-supplied value reaches os.path.join directly: %s. Every "
            "such value must go through `_under`, which checks each component; "
            "this is the defect that produced V1 and then V2." % (offenders,),
        )

    def test_the_check_above_is_the_one_that_would_have_caught_it(self):
        """The detector, held up against the defect it claims to detect.

        A structural test that passes is worth nothing until it has been shown
        to fail on the thing it exists to find. This reconstructs the pre-repair
        `_events_dir` -- the exact expression both findings came out of -- and
        requires the check to name *both* of its arguments, not just the one V1
        named.
        """
        before = (
            "import os\n"
            "class S(object):\n"
            "    def _events_dir(self, chat_id, session_id):\n"
            "        if not ids.is_id(session_id, 'ses'):\n"
            "            raise NotFound(session_id)\n"
            "        return os.path.join(self.diagnostics_dir, chat_id, session_id)\n"
        )
        found = self._offenders(before, "<before>")
        self.assertEqual(
            sorted(name for _fn, name, _line in found),
            ["chat_id", "session_id"],
            "the check must report both arguments of the join, or it repeats the "
            "mistake of covering the argument a finding named and not its neighbour",
        )

    def test_every_escaping_component_is_refused(self):
        """`_under` itself, at element granularity.

        Each row is one shape a single path component must not have. They are
        listed separately rather than as one regular expression because a guard
        stated as one pattern is a guard tested as one pattern, and the shapes
        that survived the last two repairs were the ones nobody enumerated.
        """
        from dory_wrangler.store import _under

        base = os.path.join(os.sep, "store", "diagnostics")
        escapes = [
            "..", ".", "",
            ".." + os.sep + "..",
            "a" + os.sep + "b",
            os.sep + "etc",
            os.sep,
            "cht_" + "a" * 24 + os.sep + "..",
            "." + os.sep + "cht_" + "a" * 24,
            "cht_" + "a" * 24 + os.sep,
            "with\0null",
            5, None, b"bytes", ["a"],
        ]
        safe = "cht_" + "a" * 24
        for escape in escapes:
            # Every component position, not just the first. Checking only
            # `components[0]` passes the whole list above, and the neighbour of
            # the checked value is precisely where both of this store's path
            # defects have lived.
            for position, args in enumerate(((escape,), (safe, escape),
                                             (safe, safe, escape))):
                with self.assertRaises(NotFound, msg=(
                        "%r was accepted as path component %d of %d"
                        % (escape, position + 1, len(args)))):
                    _under(base, *args)
        # And the positive half: an ordinary component still joins, and it joins
        # beneath the base rather than anywhere else.
        joined = _under(base, "cht_" + "a" * 24, "ses_" + "b" * 24)
        self.assertEqual(
            joined, os.path.join(base, "cht_" + "a" * 24, "ses_" + "b" * 24)
        )
        self.assertTrue(os.path.normpath(joined).startswith(base + os.sep))


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



class LaunchCase(StoreCase):
    """A chat plus the launch sequences the two classes below both need."""

    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Launch")["chat_id"]

    def launching(self, capabilities=None, outcome="accepted", handle="h-issued"):
        """A session in `launching`, with whatever the launcher reported."""
        user = self.store.append_user_message(self.chat_id, "go")
        session, _b = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local",
            capabilities or PERSISTENT_STREAM,
        )
        sid = session["session_id"]
        request = self.store.append_launch_request(self.chat_id, sid, "go")
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]},
        )
        if outcome is not None:
            extra = {"agent_handle": handle} if outcome == "accepted" else (
                {"failure_category": "unavailable"} if outcome == "failed" else {})
            self.store.append_launch_result(
                self.chat_id, request["request_id"], sid, outcome, **extra)
        return sid, request["request_id"]

    def running(self, capabilities=None, handle="h-issued"):
        sid, rid = self.launching(capabilities=capabilities, handle=handle)
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": rid}, agent_handle=handle,
        )
        return sid, rid


class TestTheHandleEntersRunning(LaunchCase):
    """Contract 4.3 as a window that is never open, not a rule remembered later.

    `SESSION_HANDLE_MISSING` is stated over a session having *reached*
    `running`, so no later write can satisfy it: once the transition is on disk
    without the handle, the store is rejected and stays rejected. Nothing in the
    seam obliges a harness to call `set_agent_handle` at all, and a harness that
    crashes in between leaves a permanently invalid store with no fault recorded
    anywhere. So the handle goes in with the transition, the same way a session
    and its binding go in together and for the same reason.
    """

    def test_launching_to_running_without_a_handle_is_refused(self):
        sid, rid = self.launching()
        with self.assertRaises(ValidationRefused):
            self.store.append_transition(
                self.chat_id, sid, "launching", "running", "launcher",
                {"kind": "launch_result", "ref": rid},
            )
        # Refused means nothing was written: the session is still in launching
        # and the store still validates. A guard that raised after the write
        # would leave exactly the state it exists to prevent.
        self.assertEqual("launching", self.store.read_session(self.chat_id, sid)["state"])
        # The store carries only the transient the accepted result opens and the
        # next write closes -- not the permanent violation this guard exists to
        # prevent.
        self.assertEqual(
            ["LAUNCH_OUTCOME_MISMATCH"],
            sorted(set(v[0] for v in self.store.verify())),
        )

    def test_unknown_to_running_without_a_handle_is_refused(self):
        """The second authorized transition into `running`, driven separately.

        One element of the pair is not the pair. `launching -> running` is the
        one a reproduction prints; `unknown -> running` is the route the
        contract calls out by name as the one a handle rule must also close,
        because a launcher that never accepted anything never returned a handle.
        """
        sid, rid = self.launching(outcome="unknown")
        self.store.append_transition(
            self.chat_id, sid, "launching", "unknown", "launcher",
            {"kind": "launch_result", "ref": rid},
        )
        event, _c = self.store.append_diagnostic_event(
            self.chat_id, sid, 1, "launcher", "recognized", "session_started",
            '{"type":"session_started"}',
        )
        with self.assertRaises(ValidationRefused):
            self.store.append_transition(
                self.chat_id, sid, "unknown", "running", "launcher",
                {"kind": "event", "ref": event["event_id"]},
            )
        self.assertEqual("unknown", self.store.read_session(self.chat_id, sid)["state"])
        self.assertStoreValid()

    def test_a_reattached_session_returns_to_running_with_the_handle_it_had(self):
        """The true history the guard must not make unrepresentable."""
        sid, _rid = self.running()
        event, _c = self.store.append_diagnostic_event(
            self.chat_id, sid, 1, "launcher", "recognized", "stream_end",
            '{"type":"stream_end"}',
        )
        self.store.append_transition(
            self.chat_id, sid, "running", "unknown", "launcher",
            {"kind": "stream_end", "ref": event["event_id"]},
        )
        self.store.append_session_observation(self.chat_id, sid, "reattached", "back")
        observation = self.store.read_session_observations(self.chat_id, sid)[-1]
        self.store.append_transition(
            self.chat_id, sid, "unknown", "running", "launcher",
            {"kind": "observation", "ref": observation["observation_id"]},
        )
        self.assertEqual("running", self.store.read_session(self.chat_id, sid)["state"])
        self.assertStoreValid()

    def test_a_handle_nobody_issued_cannot_enter_through_the_transition(self):
        """The new door is held to the same standard as `set_agent_handle`.

        A second way to put the handle on the record is a second place the
        issuance rule could be missing from, which is how this defect class
        travels. It re-derives issuance from the launch results rather than
        trusting what it was passed.
        """
        sid, rid = self.launching()
        with self.assertRaises(ValidationRefused):
            self.store.append_transition(
                self.chat_id, sid, "launching", "running", "launcher",
                {"kind": "launch_result", "ref": rid},
                agent_handle="a-handle-nobody-issued",
            )
        self.assertEqual("launching", self.store.read_session(self.chat_id, sid)["state"])

    def test_an_offered_handle_of_the_wrong_type_is_refused_not_crashed_on(self):
        """Why this guard needs the type test `_require_addressable` does not.

        `_require_addressable` reads its handle off the session, so a non-string
        one is already `BAD_FIELD_TYPE` on the fail-closed read before the
        membership test runs -- which is why that guard is a single `in`. Here
        the handle arrives straight from the caller and has been read by
        nothing, so an unhashable value reaches `in issued` and raises
        `TypeError` rather than refusing. The mechanical enumeration on this
        rail (N31) found the clause untested; this is the test.
        """
        sid, rid = self.launching()
        for offered in ([], ["a"], {"h": 1}, 5, object()):
            try:
                self.store.append_transition(
                    self.chat_id, sid, "launching", "running", "launcher",
                    {"kind": "launch_result", "ref": rid}, agent_handle=offered)
            except ValidationRefused:
                pass
            except TypeError as exc:  # pragma: no cover - the failure this pins
                self.fail("agent_handle=%r raised TypeError rather than being "
                          "refused: %s" % (offered, exc))
            else:
                self.fail("agent_handle=%r was accepted" % (offered,))
        self.assertEqual("launching", self.store.read_session(self.chat_id, sid)["state"])

    def test_a_handle_belongs_only_to_the_write_that_enters_running(self):
        sid, rid = self.launching()
        with self.assertRaises(ValidationRefused):
            self.store.append_transition(
                self.chat_id, sid, "launching", "launch_failed", "launcher",
                {"kind": "launch_result", "ref": rid},
                agent_handle="h-issued",
            )

    def test_a_session_never_carries_a_second_agent(self):
        """Two *issued* handles on one session, which is the only way to reach
        this clause.

        Offering an unissued handle is refused by the issuance test one line
        above, so a test that offered one would pass with this clause deleted --
        which is exactly what the mechanical enumeration reported (N32). One
        launch request per session and one result per request means a second
        issued handle cannot be reached by an honest sequence, so the second
        `launch_result` is planted directly and the guard is asked about a
        handle that genuinely *was* issued and is not this session's agent.
        """
        sid, rid = self.running()
        second = "h-issued-too"
        packet = os.path.join(self.root, "chats", self.chat_id, "packets",
                              "launch_result-req_" + "e" * 24 + ".json")
        with open(packet, "w") as handle:
            json.dump({
                "record_type": "launch_result", "record_version": 1,
                "request_id": "req_" + "e" * 24, "session_id": sid,
                "observed_at": "2026-09-12T12:00:00.000000Z",
                "outcome": "accepted", "agent_handle": second,
            }, handle)
        self.assertEqual({"h-issued", second},
                         self.store._issued_handles(self.chat_id, sid))

        event, _c = self.store.append_diagnostic_event(
            self.chat_id, sid, 1, "launcher", "recognized", "stream_end",
            '{"type":"stream_end"}',
        )
        self.store.append_transition(
            self.chat_id, sid, "running", "unknown", "launcher",
            {"kind": "stream_end", "ref": event["event_id"]},
        )
        self.store.append_session_observation(self.chat_id, sid, "reattached", "back")
        observation = self.store.read_session_observations(self.chat_id, sid)[-1]
        with self.assertRaises(ValidationRefused):
            self.store.append_transition(
                self.chat_id, sid, "unknown", "running", "launcher",
                {"kind": "observation", "ref": observation["observation_id"]},
                agent_handle=second,
            )
        # And the one it does carry still gets it back.
        self.store.append_transition(
            self.chat_id, sid, "unknown", "running", "launcher",
            {"kind": "observation", "ref": observation["observation_id"]},
            agent_handle="h-issued",
        )
        self.assertEqual("running", self.store.read_session(self.chat_id, sid)["state"])

    def test_the_window_is_never_open_at_any_point_of_an_honest_launch(self):
        """Validated after every single write, not only at the end.

        This is the shape of evidence the finding asked for. A guard proved by
        the end state proves nothing about a window: the store was rejected in
        the middle and a crash there would have left it that way forever.
        """
        user = self.store.append_user_message(self.chat_id, "go")
        session, _b = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        sid = session["session_id"]
        seen = []

        def after(label):
            codes = sorted(set(v[0] for v in self.store.verify()))
            seen.append((label, codes))

        after("session created")
        request = self.store.append_launch_request(self.chat_id, sid, "go")
        after("launch requested")
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        after("launching")
        self.store.append_launch_result(
            self.chat_id, request["request_id"], sid, "accepted", agent_handle="h-issued")
        after("launch accepted")
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": request["request_id"]},
            agent_handle="h-issued")
        after("running")

        offenders = [(label, codes) for label, codes in seen
                     if "SESSION_HANDLE_MISSING" in codes]
        self.assertEqual(
            [], offenders,
            "the store was SESSION_HANDLE_MISSING at %s; that is a window a "
            "crash can be caught in, and no later write is required to close it"
            % (offenders,),
        )
        self.assertEqual([], seen[-1][1])
        # The one transient that does occur is named rather than hidden: an
        # accepted launch_result is ahead of the transition it authorizes, and
        # the very next write closes it.
        self.assertEqual(["LAUNCH_OUTCOME_MISMATCH"], seen[-2][1])


class TestDeliveryPreconditions(LaunchCase):
    """Contract 6.5's other three preconditions, one test each.

    They sit within twenty lines of one another in the validator and share two
    codes between four facts, which is exactly the arrangement in which guarding
    "the one the reproduction printed" looks complete.
    """

    def test_a_delivery_needs_a_launcher_that_declared_persistent(self):
        sid, _rid = self.running(capabilities=ONE_SHOT)
        with self.assertRaises(ValidationRefused):
            self.store.append_delivery_request(self.chat_id, sid, "follow-up")
        self.assertEqual([], self.store.read_delivery_requests(self.chat_id, sid))
        self.assertStoreValid()

    def test_a_delivery_needs_a_session_that_actually_ran(self):
        """The second clause of the same code as the first test.

        `DELIVERY_NOT_SUPPORTED` covers two different facts. A guard that closed
        the capability half and left this one would pass any probe keyed on the
        code and would still let the store write a rejected history.
        """
        sid, _rid = self.launching()
        self.store.set_agent_handle(self.chat_id, sid, "h-issued")
        with self.assertRaises(ValidationRefused):
            self.store.append_delivery_request(self.chat_id, sid, "follow-up")
        self.assertEqual([], self.store.read_delivery_requests(self.chat_id, sid))

    def test_a_delivery_after_the_agent_exited_is_refused(self):
        sid, _rid = self.running()
        event, _c = self.store.append_diagnostic_event(
            self.chat_id, sid, 1, "launcher", "recognized", "session_completed",
            '{"type":"session_completed"}',
        )
        self.store.append_transition(
            self.chat_id, sid, "running", "completed", "launcher",
            {"kind": "event", "ref": event["event_id"]},
        )
        with self.assertRaises(ValidationRefused):
            self.store.append_delivery_request(self.chat_id, sid, "follow-up")
        self.assertEqual([], self.store.read_delivery_requests(self.chat_id, sid))
        self.assertStoreValid()

    def test_the_exit_check_is_the_session_state_and_not_a_clock_comparison(self):
        """Every terminal state, not the one a reproduction happened to reach.

        The validator states this one over timestamps because a fixture is a
        history it did not watch being made. The store is writing now, so it can
        read the fact instead -- which also means a delivery that shares a
        whole-second timestamp with the exit, the tie the validator deliberately
        accepts, is still refused here rather than slipping through.
        """
        for to_state, evidence in (("completed", "session_completed"),
                                   ("failed", "session_failed")):
            sid, _rid = self.running()
            event, _c = self.store.append_diagnostic_event(
                self.chat_id, sid, 1, "launcher", "recognized", evidence,
                '{"type":"%s"}' % evidence,
            )
            self.store.append_transition(
                self.chat_id, sid, "running", to_state, "launcher",
                {"kind": "event", "ref": event["event_id"]},
            )
            with self.assertRaises(ValidationRefused, msg=(
                    "a delivery was accepted on a session in %r" % to_state)):
                self.store.append_delivery_request(self.chat_id, sid, "follow-up")

    def test_a_delivery_to_a_session_whose_liveness_is_unknown_is_recorded(self):
        """The precondition is about the history, not about the current state.

        `unknown` means the harness cannot tell whether the agent is alive; it
        is explicitly not a terminal state, and contract 5.3 keeps it separate
        from one for exactly that reason. A guard that asked "is this session in
        `running` now?" instead of "did it ever reach `running`?" would refuse a
        delivery the contract accepts, and every other test here would still
        pass -- which the mechanical enumeration reported as N50.
        """
        sid, _rid = self.running()
        event, _c = self.store.append_diagnostic_event(
            self.chat_id, sid, 1, "launcher", "recognized", "stream_end",
            '{"type":"stream_end"}',
        )
        self.store.append_transition(
            self.chat_id, sid, "running", "unknown", "launcher",
            {"kind": "stream_end", "ref": event["event_id"]},
        )
        self.assertEqual("unknown", self.store.read_session(self.chat_id, sid)["state"])
        self.store.append_delivery_request(self.chat_id, sid, "still there?")
        self.assertEqual(1, len(self.store.read_delivery_requests(self.chat_id, sid)))
        self.assertStoreValid()

    def test_a_delivery_to_a_running_persistent_agent_is_still_recorded(self):
        """The legal exit the three refusals must not have removed."""
        sid, _rid = self.running()
        first = self.store.append_delivery_request(self.chat_id, sid, "one more thing")
        second = self.store.append_delivery_request(self.chat_id, sid, "and another")
        self.assertEqual([1, 2], [first["sequence"], second["sequence"]])
        self.assertStoreValid()



class TestNoPublicSequenceProducesARejectedStore(unittest.TestCase):
    """The class V3 belongs to, enumerated rather than argued.

    The claim "every store this code produces validates" had been established
    for a correct caller and for the shapes a reproduction happened to print.
    Three violations survived that, because the guard written for each finding
    closed the fact the finding named and not the other facts in the same
    validator block.

    So the property is stated over the product of what a caller can reach: every
    session shape the public API can build, crossed with every public write, and
    for each pair the store must either refuse or leave a store the contract
    accepts. Nothing is written out of band. A new operation, a new observation
    kind or a new reachable shape has to be added here, and an unguarded one
    fails rather than going unnoticed.

    The enumeration is at element granularity deliberately: each observation
    kind separately rather than "an observation", each terminal state separately
    rather than "terminal", because the last two repairs were both complete at
    the granularity they were checked at.
    """

    # -- the shapes a caller can build ---------------------------------

    def _launch_to(self, store, chat_id, capabilities, outcome, handle="h-issued"):
        user = store.append_user_message(chat_id, "go")
        session, _b = store.create_session(
            chat_id, user["message_id"], "dev-local", capabilities)
        sid = session["session_id"]
        request = store.append_launch_request(chat_id, sid, "go")
        store.append_transition(
            chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        if outcome is not None:
            extra = {"agent_handle": handle} if outcome == "accepted" else (
                {"failure_category": "unavailable"} if outcome == "failed" else {})
            store.append_launch_result(chat_id, request["request_id"], sid, outcome, **extra)
        return sid, request["request_id"]

    def _running(self, store, chat_id, capabilities, handle="h-issued"):
        sid, rid = self._launch_to(store, chat_id, capabilities, "accepted", handle)
        store.append_transition(
            chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": rid}, agent_handle=handle)
        return sid, rid

    def _terminal_from_running(self, store, chat_id, capabilities, to_state, kind):
        sid, rid = self._running(store, chat_id, capabilities)
        event, _c = store.append_diagnostic_event(
            chat_id, sid, 1, "launcher", "recognized", kind, '{"type":"%s"}' % kind)
        store.append_transition(
            chat_id, sid, "running", to_state, "launcher",
            {"kind": "event", "ref": event["event_id"]})
        return sid, rid

    def shapes(self):
        """name -> builder(store, chat_id) -> session_id."""
        def pending_no_request(store, chat_id):
            user = store.append_user_message(chat_id, "go")
            session, _b = store.create_session(
                chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
            return session["session_id"]

        def pending_with_request(store, chat_id):
            sid = pending_no_request(store, chat_id)
            store.append_launch_request(chat_id, sid, "go")
            return sid

        def launching_no_result(store, chat_id):
            return self._launch_to(store, chat_id, PERSISTENT_STREAM, None)[0]

        def launching_accepted(store, chat_id):
            return self._launch_to(store, chat_id, PERSISTENT_STREAM, "accepted")[0]

        def launching_failed(store, chat_id):
            return self._launch_to(store, chat_id, PERSISTENT_STREAM, "failed")[0]

        def launching_unknown(store, chat_id):
            return self._launch_to(store, chat_id, PERSISTENT_STREAM, "unknown")[0]

        def running_persistent(store, chat_id):
            return self._running(store, chat_id, PERSISTENT_STREAM)[0]

        def running_one_shot(store, chat_id):
            return self._running(store, chat_id, ONE_SHOT)[0]

        def unknown_after_running(store, chat_id):
            sid, _rid = self._running(store, chat_id, PERSISTENT_STREAM)
            event, _c = store.append_diagnostic_event(
                chat_id, sid, 1, "launcher", "recognized", "stream_end",
                '{"type":"stream_end"}')
            store.append_transition(
                chat_id, sid, "running", "unknown", "launcher",
                {"kind": "stream_end", "ref": event["event_id"]})
            return sid

        def completed(store, chat_id):
            return self._terminal_from_running(
                store, chat_id, PERSISTENT_STREAM, "completed", "session_completed")[0]

        def failed(store, chat_id):
            return self._terminal_from_running(
                store, chat_id, PERSISTENT_STREAM, "failed", "session_failed")[0]

        def terminated(store, chat_id):
            sid, _rid = self._running(store, chat_id, PERSISTENT_STREAM)
            store.append_session_observation(chat_id, sid, "stop_confirmed", "stopped")
            observation = store.read_session_observations(chat_id, sid)[-1]
            store.append_transition(
                chat_id, sid, "running", "terminated", "user",
                {"kind": "observation", "ref": observation["observation_id"]})
            return sid

        def launch_failed(store, chat_id):
            sid, rid = self._launch_to(store, chat_id, PERSISTENT_STREAM, "failed")
            store.append_transition(
                chat_id, sid, "launching", "launch_failed", "launcher",
                {"kind": "launch_result", "ref": rid})
            return sid

        def abandoned(store, chat_id):
            sid, rid = self._launch_to(store, chat_id, PERSISTENT_STREAM, "unknown")
            store.append_transition(
                chat_id, sid, "launching", "unknown", "launcher",
                {"kind": "launch_result", "ref": rid})
            store.append_transition(
                chat_id, sid, "unknown", "abandoned", "user",
                {"kind": "user_action", "ref": None})
            return sid

        return [
            ("pending-no-request", pending_no_request),
            ("pending-with-request", pending_with_request),
            ("launching-no-result", launching_no_result),
            ("launching-accepted", launching_accepted),
            ("launching-failed", launching_failed),
            ("launching-unknown", launching_unknown),
            ("running-persistent", running_persistent),
            ("running-one-shot", running_one_shot),
            ("unknown-after-running", unknown_after_running),
            ("completed", completed),
            ("failed", failed),
            ("terminated", terminated),
            ("launch-failed", launch_failed),
            ("abandoned", abandoned),
        ]

    # -- the writes a caller can make ----------------------------------

    def operations(self):
        """name -> operation(store, chat_id, session_id)."""
        operations = []
        for kind in ("stop_confirmed", "stop_unconfirmed", "reattached",
                     "reattach_failed", "stream_read_failed"):
            operations.append(
                ("observation:" + kind,
                 lambda store, chat_id, sid, kind=kind:
                     store.append_session_observation(chat_id, sid, kind, "probe")))
        operations.append(
            ("delivery",
             lambda store, chat_id, sid:
                 store.append_delivery_request(chat_id, sid, "follow-up")))
        operations.append(
            ("set_agent_handle",
             lambda store, chat_id, sid:
                 store.set_agent_handle(chat_id, sid, "h-issued")))
        operations.append(
            ("event:agent",
             lambda store, chat_id, sid:
                 store.append_diagnostic_event(
                     chat_id, sid, store.next_event_sequence(chat_id, sid),
                     "agent", "recognized", "assistant_text",
                     '{"type":"assistant_text","text":"hi"}')))
        operations.append(
            ("event:launcher",
             lambda store, chat_id, sid:
                 store.append_diagnostic_event(
                     chat_id, sid, store.next_event_sequence(chat_id, sid),
                     "launcher", "unrecognized", None, "{}")))
        for to_state in ("running", "completed", "terminated", "unknown", "abandoned",
                         "launch_failed"):
            operations.append(
                ("transition:" + to_state,
                 lambda store, chat_id, sid, to_state=to_state:
                     self._transition(store, chat_id, sid, to_state)))
        return operations

    def _transition(self, store, chat_id, sid, to_state):
        """Attempt a transition into `to_state` with whatever evidence exists.

        Deliberately does *not* offer a handle: the point is what a caller who
        simply drives the lifecycle can leave on disk.
        """
        session = store.read_session(chat_id, sid)
        requests = store.read_launch_requests(chat_id, sid)
        events = store.read_diagnostic_events(chat_id, session_id=sid)
        observations = store.read_session_observations(chat_id, sid)
        for evidence in (
            {"kind": "launch_result", "ref": requests[0]["request_id"]} if requests else None,
            {"kind": "harness_action", "ref": requests[0]["request_id"]} if requests else None,
            {"kind": "event", "ref": events[0]["event_id"]} if events else None,
            {"kind": "stream_end", "ref": events[0]["event_id"]} if events else None,
            {"kind": "observation", "ref": observations[0]["observation_id"]}
            if observations else None,
            {"kind": "user_action", "ref": None},
            {"kind": "harness_action", "ref": None},
        ):
            if evidence is None:
                continue
            try:
                return store.append_transition(
                    chat_id, sid, session["state"], to_state, "launcher", evidence)
            except StoreError:
                pass
            try:
                return store.append_transition(
                    chat_id, sid, session["state"], to_state, "harness", evidence)
            except StoreError:
                pass
            try:
                return store.append_transition(
                    chat_id, sid, session["state"], to_state, "user", evidence)
            except StoreError as exc:
                last = exc
        raise last

    # -- the property --------------------------------------------------

    # The one violation a shape may already carry before any write is attempted.
    # An accepted `launch_result` is ahead of the transition it authorizes, and
    # the very next lifecycle write closes it -- unlike SESSION_HANDLE_MISSING,
    # which nothing was required to close. Naming it here means a *second*
    # transient cannot appear without this test saying so.
    TRANSIENT = frozenset(("LAUNCH_OUTCOME_MISMATCH",))

    # The codes this rail closed. None of them may be reachable from any pair,
    # by any route, ever again.
    CLOSED = frozenset((
        "SESSION_HANDLE_MISSING",
        "DELIVERY_NOT_SUPPORTED",
        "DELIVERY_AFTER_AGENT_EXIT",
        "ADDRESSED_WITHOUT_HANDLE",
    ))

    # Found by this enumeration and deliberately *not* fixed here, because the
    # rail that authorized this work names the three codes above and forbids
    # broadening. `append_transition` checks contract 5.2's owner table
    # (`AUTHORIZED_TRANSITIONS`) and does not check its precondition table
    # (`TRANSITION_PRECONDITIONS`), so a caller that offers evidence of an
    # admissible *kind* but the wrong *content* -- a `harness_action` where a
    # `launch_result` is required, an inferred `unknown` with no observation
    # behind it -- writes a transition the contract rejects. It is the same
    # defect class as V3 one table over, and it is reported rather than quietly
    # patched or quietly tolerated. Named here so it cannot grow, cannot shrink
    # unnoticed, and cannot be mistaken for something this test does not see.
    CARRIED = frozenset(("PRECONDITION_NOT_MET", "UNKNOWN_INFERRED_WITHOUT_EVIDENCE"))
    SHAPES_WITH_A_TRANSIENT = frozenset(("launching-accepted", "launching-failed"))

    def test_every_shape_crossed_with_every_write(self):
        accepted, refused, escapes = 0, 0, []
        carried = set()
        operations = self.operations()
        for shape_name, build in self.shapes():
            # The shape is built once and copied for each operation. Every
            # operation must start from the *same* store, and building it 14
            # times over would be 14 times the fsyncs for an identical result.
            template = tempfile.mkdtemp(prefix="dory-enum-tpl-")
            try:
                store = ChatStore(template)
                chat_id = store.create_chat("Enumeration")["chat_id"]
                sid = build(store, chat_id)
                before = set(v[0] for v in store.verify())
                self.assertLessEqual(
                    before, self.TRANSIENT,
                    "the shape %r is invalid before any write, and not by a "
                    "named transient; the enumeration would be measuring the "
                    "wrong thing" % shape_name)
                if before:
                    carried.add(shape_name)
                for op_name, operate in operations:
                    work = tempfile.mkdtemp(prefix="dory-enum-")
                    root = os.path.join(work, "store")
                    try:
                        shutil.copytree(template, root)
                        copy = ChatStore(root)
                        try:
                            operate(copy, chat_id, sid)
                        except StoreError:
                            refused += 1
                            continue
                        accepted += 1
                        after = set(v[0] for v in copy.verify())
                        introduced = sorted(after - before)
                        if introduced:
                            escapes.append((shape_name, op_name, introduced))
                    finally:
                        shutil.rmtree(work, True)
            finally:
                shutil.rmtree(template, True)
        self.assertEqual(
            self.SHAPES_WITH_A_TRANSIENT, carried,
            "the set of shapes that are invalid before any write changed; a new "
            "one is a window like the one this rail closed, not a detail")
        reached = set()
        for shape_name, op_name, introduced in escapes:
            reached.update(introduced)
            self.assertTrue(
                op_name.startswith("transition:"),
                "%s/%s introduced %s; the carried evidence-precondition gap is "
                "specific to append_transition, so this is something else"
                % (shape_name, op_name, introduced))
        self.assertEqual(
            frozenset(), reached & self.CLOSED,
            "a public call sequence reached a code this rail closed: %s"
            % (escapes,))
        self.assertLessEqual(
            reached, self.CARRIED,
            "a public call sequence left a store the contract rejects, and not "
            "by the carried evidence-precondition gap: %s" % (escapes,))
        self.assertEqual(
            self.CARRIED, reached,
            "the carried evidence-precondition gap stopped being reachable. "
            "That is good news, and it must be written down rather than left "
            "implied -- update CARRIED and say so in the handoff.")
        # The counts are asserted so that the enumeration cannot quietly stop
        # enumerating. A run in which everything is refused proves nothing, and
        # would be indistinguishable from a passing run without this.
        self.assertEqual(len(self.shapes()) * len(self.operations()),
                         accepted + refused)
        self.assertGreater(accepted, 40, "too few writes were accepted for this "
                                         "to be evidence of anything")
        self.assertGreater(refused, 40, "too few writes were refused for the "
                                        "guards to be doing any work")


if __name__ == "__main__":
    unittest.main()
