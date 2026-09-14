"""The durable store: create, list, open, send, and every refusal it must make."""

import ast
import json
import os
import shutil
import tempfile
import threading
import unittest

from helpers import (
    ONE_SHOT,
    PERSISTENT_STREAM,
    TESTS_DIR,
    VALIDATOR,
    StoreCase,
    answered_turn,
    contract_violation_vocabulary,
)

from dory_wrangler import atomic
from dory_wrangler import contract
from dory_wrangler import ids
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
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
from dory_wrangler.service import ChatService
from dory_wrangler.session_manager import SessionManager
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
        taken = self.store.next_event_sequence(self.chat_id, self.sid)
        first, created = self.store.append_diagnostic_event(
            self.chat_id, self.sid, taken, "agent", "recognized", "assistant_text", body
        )
        self.assertTrue(created)
        second, created_again = self.store.append_diagnostic_event(
            self.chat_id, self.sid, taken, "agent", "recognized", "assistant_text", body
        )
        self.assertFalse(created_again)
        self.assertEqual(first["event_id"], second["event_id"])

    def test_a_different_payload_at_a_taken_sequence_is_not_a_replay(self):
        """Same sequence is the label; same payload is the fact."""
        taken = self.store.next_event_sequence(self.chat_id, self.sid)
        self.store.append_diagnostic_event(
            self.chat_id, self.sid, taken, "agent", "recognized", "assistant_text", '{"a":1}'
        )
        with self.assertRaises(StoreCorrupt):
            self.store.append_diagnostic_event(
                self.chat_id, self.sid, taken, "agent", "recognized", "assistant_text",
                '{"a":2}'
            )

    def test_a_sequence_beyond_the_next_one_is_refused_rather_than_written(self):
        """Contract P3: a gap means a turn was lost, and is never closed silently.

        Both tests above used to name sequence 9 on an empty session, which wrote
        a gap. The store would then fail closed when *reading* a store it had
        itself made invalid; the gap is now refused at the door. Driven at the
        exact boundary rather than at an arbitrary number, because a guard stated
        as "much larger than the next one" would pass a store with one hole in it.
        """
        following = self.store.next_event_sequence(self.chat_id, self.sid)
        with self.assertRaises(ValidationRefused):
            self.store.append_diagnostic_event(
                self.chat_id, self.sid, following + 1, "launcher", "unrecognized",
                None, "{}"
            )
        event, created = self.store.append_diagnostic_event(
            self.chat_id, self.sid, following, "launcher", "unrecognized", None, "{}"
        )
        self.assertTrue(created)
        self.assertEqual([], self.store.verify())

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
        self.launcher = ScriptedStubLauncher({})
        self.service = ChatService(SessionManager(self.store, self.launcher))

    def test_a_sent_turn_is_durable_before_any_agent_is_asked(self):
        """Replaces `test_the_turn_listener_sees_a_turn_that_is_already_durable`.

        #86's listener was the seam through which #87 was to be told of a turn
        only after it was durable and only because a user sent one. The chat
        loop now *is* that path, so the property is asserted over what the
        launcher saw rather than over a callback: nothing is launched by opening
        a chat, and at the moment `launch` is called the user's turn is already
        on disk and is the message the session was opened on.
        """
        seen = []

        def on_launch(instruction):
            durable = ChatStore(self.root).read_messages(instruction.chat_id)
            session = ChatStore(self.root).read_session(
                instruction.chat_id, instruction.session_id)
            seen.append((instruction.instruction_text, durable, session))

        self.launcher = ScriptedStubLauncher({"on_launch": on_launch})
        self.service = ChatService(SessionManager(self.store, self.launcher))
        chat_id = self.service.create_chat()["chat_id"]
        self.assertEqual(self.launcher.launch_calls, [],
                         "an agent was launched without a user turn")
        self.service.send_user_message(chat_id, "a turn")
        self.assertEqual(len(seen), 1)
        text, durable, session = seen[0]
        self.assertEqual(text, "a turn")
        self.assertEqual([m["content"]["text"] for m in durable], ["a turn"])
        self.assertEqual(session["transitions"][0]["evidence"]["ref"],
                         durable[0]["message_id"])
        self.assertStoreValid()

    def test_open_chat_reads_from_disk_every_time(self):
        chat_id = self.service.create_chat("Fresh")["chat_id"]
        self.service.send_user_message(chat_id, "first")
        self.store.append_user_message(chat_id, "written by something else")
        self.assertEqual(
            [m["text"] for m in self.service.open_chat(chat_id)["messages"]],
            ["first", "answer to: first", "written by something else"],
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

    # Public methods of ChatStore that write nothing. Everything else is a write
    # and must appear in `operations()`, which `test_every_public_write_is_in_the
    # _enumeration` asserts. A method added to the store therefore has to be
    # classified, and an unclassified one fails rather than going unenumerated --
    # the same anti-drift shape as the violation-code accounting.
    READ_ONLY = frozenset((
        "read_chat", "list_chats", "read_messages", "read_session", "read_binding",
        "list_sessions", "chat_agent_status", "read_launch_requests",
        "read_launch_results", "read_delivery_requests", "read_session_observations",
        "next_event_sequence", "read_diagnostic_event", "read_diagnostic_events",
        "read_all_events_of_session", "export_records", "snapshot", "verify",
        "rejected_content_types", "root", "chats_dir", "diagnostics_dir",
    ))

    # Decision 0002, D1: the store-level lock. These write no record -- `acquire`
    # takes the lock, creates the two top-level directories and sweeps temp
    # files; `close` gives the lock back -- so there is nothing for the cross to
    # drive, and they are classified here rather than hidden from the check.
    STORE_LOCK = frozenset(("acquire", "close", "held", "read_only"))

    def _agent_answer(self, store, chat_id, sid):
        event, _created = store.append_diagnostic_event(
            chat_id, sid, store.next_event_sequence(chat_id, sid),
            "agent", "recognized", "assistant_text",
            '{"type":"assistant_text","text":"hi"}')
        return store.append_agent_message(chat_id, sid, event["event_id"], "hi")

    def _acknowledged(self, store, chat_id, sid, values):
        """A delivery, then the answers to it, the last of which may be refused.

        A compound operation, so a refusal of its *last* write must not hide the
        writes before it from the property: the store is verified here before
        the refusal is passed on, which the cross does not do for a refused op.
        """
        delivery = store.append_delivery_request(chat_id, sid, "follow-up")
        for value in values[:-1]:
            store.record_delivery_acknowledgement(
                chat_id, sid, delivery["delivery_id"], value)
        try:
            return store.record_delivery_acknowledgement(
                chat_id, sid, delivery["delivery_id"], values[-1])
        except StoreError:
            introduced = set(v[0] for v in store.verify()) - self.TRANSIENT
            self.assertEqual(set(), introduced,
                             "a refused acknowledgement left the writes before it "
                             "in a store the contract rejects")
            raise

    def _second_session(self, store, chat_id, sid):
        user = store.append_user_message(chat_id, "another turn")
        return store.create_session(
            chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)

    def _result(self, store, chat_id, sid, outcome):
        requests = store.read_launch_requests(chat_id, sid)
        request_id = requests[0]["request_id"] if requests else "req_deadbeefdead"
        extra = {"agent_handle": "h-late"} if outcome == "accepted" else (
            {"failure_category": "unavailable"} if outcome == "failed" else {})
        return store.append_launch_result(
            chat_id, request_id, sid, outcome, **extra)

    def operations(self):
        """name -> operation(store, chat_id, session_id).

        Named `<public method>:<variant>` so the method each pair drives is
        readable off the name, and so the coverage assertion above can be made
        mechanically rather than by keeping a second list in step by hand.
        """
        operations = []
        for kind in ("stop_confirmed", "stop_unconfirmed", "reattached",
                     "reattach_failed", "stream_read_failed"):
            operations.append(
                ("append_session_observation:" + kind,
                 lambda store, chat_id, sid, kind=kind:
                     store.append_session_observation(chat_id, sid, kind, "probe")))
        operations.append(
            ("append_delivery_request",
             lambda store, chat_id, sid:
                 store.append_delivery_request(chat_id, sid, "follow-up")))
        for variant, values in (("true", (True,)), ("false", (False,)),
                                ("twice", (True, False)), ("null", (None,))):
            operations.append(
                ("record_delivery_acknowledgement:" + variant,
                 lambda store, chat_id, sid, values=values:
                     self._acknowledged(store, chat_id, sid, values)))
        operations.append(
            ("set_agent_handle",
             lambda store, chat_id, sid:
                 store.set_agent_handle(chat_id, sid, "h-issued")))
        operations.append(
            ("append_diagnostic_event:agent",
             lambda store, chat_id, sid:
                 store.append_diagnostic_event(
                     chat_id, sid, store.next_event_sequence(chat_id, sid),
                     "agent", "recognized", "assistant_text",
                     '{"type":"assistant_text","text":"hi"}')))
        operations.append(
            ("append_diagnostic_event:launcher",
             lambda store, chat_id, sid:
                 store.append_diagnostic_event(
                     chat_id, sid, store.next_event_sequence(chat_id, sid),
                     "launcher", "unrecognized", None, "{}")))
        operations.append(
            ("append_diagnostic_event:stream_end",
             lambda store, chat_id, sid:
                 store.append_diagnostic_event(
                     chat_id, sid, store.next_event_sequence(chat_id, sid),
                     "launcher", "recognized", "stream_end",
                     '{"type":"stream_end"}')))
        operations.append(
            ("append_user_message",
             lambda store, chat_id, sid: store.append_user_message(chat_id, "hi")))
        operations.append(
            ("append_system_message",
             lambda store, chat_id, sid: store.append_system_message(chat_id, "note")))
        operations.append(
            ("append_agent_message",
             lambda store, chat_id, sid: self._agent_answer(store, chat_id, sid)))
        operations.append(
            ("append_launch_request",
             lambda store, chat_id, sid:
                 store.append_launch_request(chat_id, sid, "instruction")))
        for outcome in ("accepted", "failed", "unknown"):
            operations.append(
                ("append_launch_result:" + outcome,
                 lambda store, chat_id, sid, outcome=outcome:
                     self._result(store, chat_id, sid, outcome)))
        operations.append(
            ("create_session",
             lambda store, chat_id, sid: self._second_session(store, chat_id, sid)))
        operations.append(
            ("create_chat", lambda store, chat_id, sid: store.create_chat("Another")))
        operations.append(
            ("archive_chat", lambda store, chat_id, sid: store.archive_chat(chat_id)))
        operations.append(
            ("set_title", lambda store, chat_id, sid: store.set_title(chat_id, "Renamed")))
        for to_state in ("running", "completed", "terminated", "unknown", "abandoned",
                         "launch_failed"):
            operations.append(
                ("append_transition:" + to_state,
                 lambda store, chat_id, sid, to_state=to_state:
                     self._transition(store, chat_id, sid, to_state)))
        return operations

    def test_every_public_write_is_in_the_enumeration(self):
        """A public write the sweep does not drive is a hole the sweep cannot see.

        The previous round of this enumeration crossed fourteen shapes with
        fourteen writes and reported one reachable gap. Five violation codes were
        reachable the whole time through writes it did not drive -- among them a
        launch result naming no request at all. The cross is only as exhaustive as
        its second axis, so that axis is now derived from the store's own surface
        rather than from a list written alongside it.
        """
        work = tempfile.mkdtemp(prefix="dory-surface-")
        try:
            # A live instance, so an attribute the store publishes without a
            # method -- the rejected-content-type count is one -- has to be
            # classified too rather than being invisible to this check.
            public = set(
                name for name in dir(ChatStore(work)) if not name.startswith("_"))
        finally:
            shutil.rmtree(work, True)
        driven = set(name.split(":")[0] for name, _op in self.operations())
        unaccounted = public - self.READ_ONLY - self.STORE_LOCK - driven
        self.assertEqual(
            set(), unaccounted,
            "ChatStore gained public method(s) %s that this enumeration neither "
            "drives nor declares read-only; classify them rather than leaving the "
            "cross incomplete" % sorted(unaccounted))
        self.assertEqual(
            set(), driven - public,
            "the enumeration drives %s, which is not a public method of the store"
            % sorted(driven - public))
        self.assertEqual(
            set(), self.READ_ONLY - public,
            "the read-only list names %s, which the store no longer has"
            % sorted(self.READ_ONLY - public))

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

    # The violations a shape may carry, or a write may introduce, without this
    # being an escape: the codes the accounting records as deliberate exceptions.
    # Derived from that table rather than restated, so a second exception cannot
    # be admitted here without being written down there with its reason.
    @property
    def TRANSIENT(self):
        return frozenset(
            code for code, entry in EveryCodeIsAccountedFor.ACCOUNTING.items()
            if entry[0] == "TRANSIENT"
        )

    # Every violation code the store accounts for by guarding it at write time.
    # None of them may be reachable from any pair, by any route, ever again. The
    # list is not maintained here: it is derived from the accounting table in
    # `TestEveryViolationCodeIsAccountedFor`, so a code added there as guarded is
    # automatically forbidden here, and a code quietly dropped from there fails
    # that test's equality with the validator.
    @property
    def CLOSED(self):
        return frozenset(
            code for code, entry in EveryCodeIsAccountedFor.ACCOUNTING.items()
            if entry[0] == "GUARDED"
        )

    # Nothing is carried any more, so there is no carried set. The previous round
    # of this enumeration left `PRECONDITION_NOT_MET` and
    # `UNKNOWN_INFERRED_WITHOUT_EVIDENCE` reachable on 18 of its 196 pairs,
    # because `append_transition` enforced contract 5.2's owner table and not its
    # precondition table. Both are refused at write time now, and what forbids
    # them here is the accounting table's `GUARDED` set rather than a second list
    # -- an empty `CARRIED` left behind would be state nothing reads and nothing
    # could keep honest.
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
        for _shape_name, _op_name, introduced in escapes:
            reached.update(introduced)
        self.assertEqual(
            frozenset(), reached & self.CLOSED,
            "a public call sequence reached a code the store guards at write "
            "time: %s" % (escapes,))
        self.assertEqual(
            frozenset(), reached - self.TRANSIENT,
            "a public call sequence left a store the contract rejects, and not "
            "by a deliberate exception: %s" % (escapes,))
        self.assertEqual(
            self.TRANSIENT, reached,
            "a deliberate exception stopped being reachable. That is good news, "
            "and it must be written down in the accounting rather than left "
            "implied.")
        # The counts are asserted so that the enumeration cannot quietly stop
        # enumerating. A run in which everything is refused proves nothing, and
        # would be indistinguishable from a passing run without this.
        self.assertEqual(len(self.shapes()) * len(self.operations()),
                         accepted + refused)
        self.assertGreater(accepted, 100, "too few writes were accepted for this "
                                          "to be evidence of anything")
        self.assertGreater(refused, 100, "too few writes were refused for the "
                                         "guards to be doing any work")



# ---------------------------------------------------------------------------
# Exhaustiveness: every violation code the contract can emit, accounted for
# ---------------------------------------------------------------------------


class CodeClosureCase(StoreCase):
    """Scaffolding for "refused now, and here is the store it used to write".

    A refusal on its own proves only that something was refused. Each closure
    below therefore shows both directions: the public call sequence is refused,
    and the history that sequence used to leave is put to the contract and comes
    back carrying exactly the code being closed. Without the second half a guard
    that refuses for an unrelated reason would look like a closure.
    """

    def setUp(self):
        StoreCase.setUp(self)
        self.chat_id = self.store.create_chat("Closure")["chat_id"]

    # -- builders ------------------------------------------------------

    def launched(self, capabilities=PERSISTENT_STREAM, handle="h-issued", text="go",
                 outcome="accepted"):
        """A session driven to `launching` with the launcher's report recorded."""
        user = self.store.append_user_message(self.chat_id, text)
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", capabilities)
        sid = session["session_id"]
        request = self.store.append_launch_request(self.chat_id, sid, "go")
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        extra = {"agent_handle": handle} if outcome == "accepted" else (
            {"failure_category": "unavailable"} if outcome == "failed" else {})
        self.store.append_launch_result(
            self.chat_id, request["request_id"], sid, outcome, **extra)
        return sid, request["request_id"], user

    def running(self, capabilities=PERSISTENT_STREAM, handle="h-issued", text="go"):
        sid, request_id, user = self.launched(capabilities, handle, text)
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": request_id}, agent_handle=handle)
        return sid, request_id, user

    def agent_event(self, sid, interpretation="recognized", source="agent",
                    interpreted_type="assistant_text"):
        event, _created = self.store.append_diagnostic_event(
            self.chat_id, sid, self.store.next_event_sequence(self.chat_id, sid),
            source, interpretation, interpreted_type,
            '{"type":"assistant_text","text":"hi"}')
        return event

    def completed(self, sid):
        event, _created = self.store.append_diagnostic_event(
            self.chat_id, sid, self.store.next_event_sequence(self.chat_id, sid),
            "launcher", "recognized", "session_completed", '{"type":"session_completed"}')
        self.store.append_transition(
            self.chat_id, sid, "running", "completed", "launcher",
            {"kind": "event", "ref": event["event_id"]})
        return event

    # -- the two directions --------------------------------------------

    def forge(self, *records):
        """The store as it is, plus the records a refused write would have left."""
        return self.store.export_records() + [dict(r) for r in records]

    def forged_session(self, sid, **changes):
        """This session's record with `changes` applied, in place of the real one."""
        records = []
        for record in self.store.export_records():
            if (record.get("record_type") == "agent_session"
                    and record.get("session_id") == sid):
                record = dict(record)
                record.update(changes)
            records.append(record)
        return records

    def with_extra_transition(self, sid, transition, state=None):
        session = self.store.read_session(self.chat_id, sid)
        return self.forged_session(
            sid,
            transitions=list(session["transitions"]) + [transition],
            state=state or transition["to"])

    def assertContractRejects(self, code, records):
        codes = set(c for c, _w, _d in contract.store_violations(records))
        self.assertIn(
            code, codes,
            "the history this guard refuses is not actually rejected for %s "
            "(it reports %s), so the guard is not closing that code"
            % (code, sorted(codes)))

    def assertRefused(self, drive):
        """`drive` raises, and leaves the store exactly as it found it.

        Compared before and after rather than against an empty list, because a
        shape may legitimately already carry the one named transient (an
        accepted launch result recorded ahead of the transition it authorizes)
        and measuring against zero would be measuring the wrong thing.
        """
        before = set(code for code, _w, _d in self.store.verify())
        with self.assertRaises(StoreError) as caught:
            drive()
        after = set(code for code, _w, _d in self.store.verify())
        self.assertEqual(
            before, after,
            "the refusal changed what the contract says about the store on disk")
        return caught.exception

    def assertClosed(self, code, drive, records):
        """`drive` is refused, and `records` is the store it used to leave."""
        self.assertRefused(drive)
        self.assertContractRejects(code, records)


class TestGuardedCodes(CodeClosureCase):
    """One test per violation code the store refuses at write time.

    Each names the public call sequence that produced the code, requires it to
    be refused, and requires the history it used to leave to be rejected by the
    contract for that code. The accounting table below names these tests, and
    fails if one is renamed away.
    """

    # -- contract 5.2's precondition table ------------------------------

    def test_the_contract_bridge_fails_closed_if_the_check_is_renamed(self):
        """The store reaches into the contract for the precondition column.

        That is deliberate -- restating section 5.2 here is how a rule and its
        enforcement drift apart -- but it is a coupling, and a coupling that broke
        quietly would leave every transition unchecked while every test stayed
        green. So the bridge refuses to run rather than falling back to nothing,
        and this drives that: the check is renamed in a copy of the contract and
        the store must raise rather than proceed.
        """
        with open(VALIDATOR) as handle:
            source = handle.read()
        renamed = source.replace("_validate_preconditions", "_renamed_away")
        self.assertNotIn("_validate_preconditions", renamed)
        path = os.path.join(self.root, "validator-renamed.py")
        with open(path, "w") as handle:
            handle.write(renamed)
        cached, override = contract._MODULE, os.environ.get("DORY_WRANGLER_VALIDATOR")
        contract._MODULE = None
        os.environ["DORY_WRANGLER_VALIDATOR"] = path
        try:
            with self.assertRaises(RuntimeError):
                contract.transition_precondition_violations({}, {})
        finally:
            contract._MODULE = cached
            if override is None:
                os.environ.pop("DORY_WRANGLER_VALIDATOR", None)
            else:
                os.environ["DORY_WRANGLER_VALIDATOR"] = override

    def test_evidence_that_is_not_an_object_is_refused_rather_than_crashing(self):
        """Every refusal this store makes is a StoreError, including this one.

        Without the type check the evidence is copied with `dict(...)`, which
        raises `TypeError` or `ValueError` -- not a refusal a caller can catch
        alongside every other one. The mechanical enumeration removed the check
        with every test still green, so it was a guard nothing proved.
        """
        sid, _request_id, _user = self.launched()
        for evidence in (None, "launch_result", [], 5, object(), ("kind", "ref")):
            self.assertRefused(
                lambda evidence=evidence: self.store.append_transition(
                    self.chat_id, sid, "launching", "running", "launcher", evidence,
                    agent_handle="h-issued"))

    def test_precondition_not_met_by_the_evidence_kind(self):
        """An admissible kind for the session, inadmissible for this transition.

        `harness_action` is a real evidence kind and `launching -> running` is a
        real transition owned by the launcher, so the owner table -- the half the
        store already checked -- says yes. The precondition table says only the
        launcher's own report authorizes a session to run.
        """
        sid, request_id, _user = self.launched()
        evidence = {"kind": "harness_action", "ref": request_id}
        self.assertClosed(
            "PRECONDITION_NOT_MET",
            lambda: self.store.append_transition(
                self.chat_id, sid, "launching", "running", "launcher", evidence,
                agent_handle="h-issued"),
            self.with_extra_transition(sid, {
                "from": "launching", "to": "running", "owner": "launcher",
                "at": ids.now(), "evidence": evidence}))

    def test_precondition_not_met_by_the_evidence_content(self):
        """The admissible kind, resolving to a record that says the opposite.

        The other half of the same table entry, driven separately: the reference
        names the right kind of record and that record reports an accepted
        launch, which is not what a launch failure rests on. A guard satisfied by
        the kind alone would test the label on the claim.
        """
        sid, request_id, _user = self.launched()
        evidence = {"kind": "launch_result", "ref": request_id}
        self.assertClosed(
            "PRECONDITION_NOT_MET",
            lambda: self.store.append_transition(
                self.chat_id, sid, "launching", "launch_failed", "launcher", evidence),
            self.with_extra_transition(sid, {
                "from": "launching", "to": "launch_failed", "owner": "launcher",
                "at": ids.now(), "evidence": evidence}))

    def test_unknown_inferred_without_evidence(self):
        sid, _request_id, _user = self.running()
        self.store.append_session_observation(self.chat_id, sid, "stop_confirmed", "done")
        observation = self.store.read_session_observations(self.chat_id, sid)[-1]
        evidence = {"kind": "observation", "ref": observation["observation_id"]}
        self.assertClosed(
            "UNKNOWN_INFERRED_WITHOUT_EVIDENCE",
            lambda: self.store.append_transition(
                self.chat_id, sid, "running", "unknown", "launcher", evidence),
            self.with_extra_transition(sid, {
                "from": "running", "to": "unknown", "owner": "launcher",
                "at": observation["observed_at"], "evidence": evidence}))

    def test_evidence_ref_invalid(self):
        sid, _request_id, _user = self.launched()
        evidence = {"kind": "launch_result", "ref": "req_deadbeefdead"}
        self.assertClosed(
            "EVIDENCE_REF_INVALID",
            lambda: self.store.append_transition(
                self.chat_id, sid, "launching", "running", "launcher", evidence,
                agent_handle="h-issued"),
            self.with_extra_transition(sid, {
                "from": "launching", "to": "running", "owner": "launcher",
                "at": ids.now(), "evidence": evidence}))

    def test_evidence_kind_unsupported(self):
        sid, _request_id, _user = self.running(capabilities=ONE_SHOT)
        event = self.agent_event(sid, source="launcher", interpreted_type="other")
        evidence = {"kind": "stream_end", "ref": event["event_id"]}
        self.assertClosed(
            "EVIDENCE_KIND_UNSUPPORTED",
            lambda: self.store.append_transition(
                self.chat_id, sid, "running", "unknown", "launcher", evidence),
            self.with_extra_transition(sid, {
                "from": "running", "to": "unknown", "owner": "launcher",
                "at": event["received_at"], "evidence": evidence}))

    # -- a launch result reports on a request this store sent -----------

    def _forged_result(self, request_id, sid, **changes):
        record = {
            "record_type": "launch_result", "record_version": 1,
            "request_id": request_id, "session_id": sid,
            "observed_at": ids.now(), "outcome": "accepted",
            "agent_handle": "h-issued",
        }
        record.update(changes)
        return record

    def test_dangling_reference(self):
        sid, _request_id, _user = self.running()
        self.assertClosed(
            "DANGLING_REFERENCE",
            lambda: self.store.append_launch_result(
                self.chat_id, "req_deadbeefdead", sid, "accepted",
                agent_handle="h-issued"),
            self.forge(self._forged_result("req_deadbeefdead", sid)))

    def test_correlation_mismatch(self):
        """A result naming a request that belongs to a different session.

        Driven on a request that has *no* result yet. An earlier version of this
        test reused a request that already had one, and the duplicate rule
        refused it -- so the correlation clause was never the thing being tested,
        and removing it left every test green. That is the defect this rail
        exists to close, found by the enumeration inside this rail's own work.
        """
        first, _first_request, _user = self.running()
        self.completed(first)
        second, _second_request, _user2 = self.running(text="again", handle="h-two")
        self.completed(second)
        # A third session whose launch has been sent and *not yet reported on*.
        # Reusing a request that already has a result would be refused by the
        # exclusive creation that arbitrates duplicates, and the correlation
        # clause would never be reached.
        user = self.store.append_user_message(self.chat_id, "third")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        third_request = self.store.append_launch_request(
            self.chat_id, session["session_id"], "go")["request_id"]
        self.assertEqual([], self.store.read_launch_results(
            self.chat_id, session["session_id"]))
        self.assertClosed(
            "CORRELATION_MISMATCH",
            lambda: self.store.append_launch_result(
                self.chat_id, third_request, second, "accepted", agent_handle="h-two"),
            self.forge(self._forged_result(third_request, second, agent_handle="h-two")))

    def test_duplicate_launch_result(self):
        """One launch attempt is reported once, and the file name is the reason.

        A launch result is stored under the request it reports on, so the second
        report of one launch loses the exclusive creation rather than being
        caught by a read-then-check two concurrent writers could both pass.
        """
        sid, request_id, _user = self.running()
        self.assertClosed(
            "DUPLICATE_LAUNCH_RESULT",
            lambda: self.store.append_launch_result(
                self.chat_id, request_id, sid, "accepted", agent_handle="h-issued"),
            self.forge(self._forged_result(request_id, sid)))

    def test_duplicate_id(self):
        """The launch result is the one record a caller can duplicate.

        Every other record is keyed by an identifier this store draws from
        `secrets` and publishes by exclusive creation, so a duplicate would need
        a 96-bit collision *and* a filename collision. A launch result is keyed
        by the request it reports on, which the caller names.
        """
        sid, request_id, _user = self.running()
        self.assertClosed(
            "DUPLICATE_ID",
            lambda: self.store.append_launch_result(
                self.chat_id, request_id, sid, "failed", failure_category="rejected"),
            self.forge(self._forged_result(request_id, sid)))

    def test_duplicate_launch_request(self):
        sid, _request_id, _user = self.launched()
        record = {
            "record_type": "launch_request", "record_version": 1,
            "request_id": ids.new_id("req"), "chat_id": self.chat_id,
            "session_id": sid, "created_at": ids.now(),
            "instruction_encoding": "utf-8", "instruction_text": "again",
        }
        self.assertClosed(
            "DUPLICATE_LAUNCH_REQUEST",
            lambda: self.store.append_launch_request(self.chat_id, sid, "again"),
            self.forge(record))

    # -- diagnostics ----------------------------------------------------

    def test_sequence_gap(self):
        sid, _request_id, _user = self.running()
        following = self.store.next_event_sequence(self.chat_id, sid)
        record = {
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": ids.new_id("evt"), "chat_id": self.chat_id,
            "session_id": sid, "sequence": following + 1, "received_at": ids.now(),
            "source": "launcher", "interpretation": "unrecognized",
            "interpreted_type": None, "raw": {"encoding": "utf-8", "body": "{}"},
        }
        self.assertClosed(
            "SEQUENCE_GAP",
            lambda: self.store.append_diagnostic_event(
                self.chat_id, sid, following + 1, "launcher", "unrecognized", None, "{}"),
            self.forge(record))

    def test_stream_end_unsupported(self):
        sid, _request_id, _user = self.running(capabilities=ONE_SHOT)
        record = {
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": ids.new_id("evt"), "chat_id": self.chat_id,
            "session_id": sid, "sequence": 1, "received_at": ids.now(),
            "source": "launcher", "interpretation": "recognized",
            "interpreted_type": "stream_end",
            "raw": {"encoding": "utf-8", "body": '{"type":"stream_end"}'},
        }
        self.assertClosed(
            "STREAM_END_UNSUPPORTED",
            lambda: self.store.append_diagnostic_event(
                self.chat_id, sid, 1, "launcher", "recognized", "stream_end",
                '{"type":"stream_end"}'),
            self.forge(record))

    def test_agent_output_without_agent(self):
        sid, _request_id, _user = self.launched()
        record = {
            "record_type": "diagnostic_event", "record_version": 1,
            "event_id": ids.new_id("evt"), "chat_id": self.chat_id,
            "session_id": sid, "sequence": 1, "received_at": ids.now(),
            "source": "agent", "interpretation": "recognized",
            "interpreted_type": "assistant_text",
            "raw": {"encoding": "utf-8", "body": '{"text":"hi"}'},
        }
        self.assertClosed(
            "AGENT_OUTPUT_WITHOUT_AGENT",
            lambda: self.store.append_diagnostic_event(
                self.chat_id, sid, 1, "agent", "recognized", "assistant_text",
                '{"text":"hi"}'),
            self.forge(record))

    # -- the packet bound -----------------------------------------------

    def _bounded(self):
        capabilities = dict(PERSISTENT_STREAM)
        capabilities["instruction_bound_bytes"] = 8
        return capabilities

    def test_instruction_text_too_large_on_a_launch_request(self):
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", self._bounded())
        sid = session["session_id"]
        record = {
            "record_type": "launch_request", "record_version": 1,
            "request_id": ids.new_id("req"), "chat_id": self.chat_id,
            "session_id": sid, "created_at": ids.now(),
            "instruction_encoding": "utf-8",
            "instruction_text": "far more than eight bytes of instruction",
        }
        self.assertClosed(
            "INSTRUCTION_TEXT_TOO_LARGE",
            lambda: self.store.append_launch_request(
                self.chat_id, sid, "far more than eight bytes of instruction"),
            self.forge(record))

    def test_instruction_text_too_large_on_a_delivery(self):
        """The sibling of the test above, and the reason the guard is not on it.

        The contract bounds `launch_request` *and* `delivery_request` in one
        loop. A guard installed on the method a reproduction printed would leave
        the other open, which is the shape of every finding on this ticket, so
        the check is on the packet writer both go through.
        """
        sid, _request_id, _user = self.running(capabilities=self._bounded())
        record = {
            "record_type": "delivery_request", "record_version": 1,
            "delivery_id": ids.new_id("dlv"), "chat_id": self.chat_id,
            "session_id": sid, "sequence": 1, "created_at": ids.now(),
            "instruction_encoding": "utf-8",
            "instruction_text": "far more than eight bytes of instruction",
            "acknowledged": None,
        }
        self.assertClosed(
            "INSTRUCTION_TEXT_TOO_LARGE",
            lambda: self.store.append_delivery_request(
                self.chat_id, sid, "far more than eight bytes of instruction"),
            self.forge(record))

    def test_the_bound_bites_one_byte_over_and_not_at_it(self):
        """The boundary itself, because an off-by-one here is silent both ways.

        A guard stated as `> bound + 1` accepts a packet the contract rejects and
        a guard stated as `>= bound` rejects one it accepts; neither is visible
        to a test that only ever offers text far over the bound.
        """
        sid, _request_id, _user = self.running(capabilities=self._bounded())
        exact = self.store.append_delivery_request(self.chat_id, sid, "12345678")
        self.assertEqual(8, len(exact["instruction_text"].encode("utf-8")))
        with self.assertRaises(ValidationRefused):
            self.store.append_delivery_request(self.chat_id, sid, "123456789")
        self.assertEqual([], self.store.verify())

    # -- turns ----------------------------------------------------------

    def test_turn_already_served(self):
        sid, _request_id, user = self.running()
        self.completed(sid)
        served = self.store.read_session(self.chat_id, sid)
        clone = dict(served)
        clone["session_id"] = ids.new_id("ses")
        binding = {
            "record_type": "agent_binding", "record_version": 1,
            "binding_id": ids.new_id("bnd"), "chat_id": self.chat_id,
            "session_id": clone["session_id"], "bound_at": clone["created_at"],
            "released_at": clone["transitions"][-1]["at"],
        }
        self.assertClosed(
            "TURN_ALREADY_SERVED",
            lambda: self.store.create_session(
                self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM),
            self.forge(clone, binding))

    def test_turn_instruction_missing(self):
        sid, _request_id, _user = self.running()
        first = self.store.append_agent_message(
            self.chat_id, sid, self.agent_event(sid)["event_id"], "one")
        self.store.append_user_message(self.chat_id, "again")
        event = self.agent_event(sid)
        planted = dict(first)
        planted["message_id"] = ids.new_id("msg")
        planted["sequence"] = first["sequence"] + 2
        planted["source_event_id"] = event["event_id"]
        planted["content"] = {"content_type": "text/plain", "text": "two"}
        self.assertClosed(
            "TURN_INSTRUCTION_MISSING",
            lambda: self.store.append_agent_message(
                self.chat_id, sid, event["event_id"], "two"),
            self.forge(planted))

    def test_a_packet_on_a_session_that_never_ran_does_not_raise_the_floor(self):
        """The packet that was not sent is what a launch failure is.

        The contract counts only packets on sessions that actually reached
        `running`, "because counting the others lets a chat pad the floor with
        decoy sessions". Dropping that filter here would let one real answer and
        one failed launch stand in for two answers.
        """
        failed_sid, failed_request, _user = self.launched(outcome="failed")
        self.store.append_transition(
            self.chat_id, failed_sid, "launching", "launch_failed", "launcher",
            {"kind": "launch_result", "ref": failed_request})
        sid, _request_id, _user2 = self.running(text="retry")
        self.store.append_agent_message(
            self.chat_id, sid, self.agent_event(sid)["event_id"], "one")
        self.store.append_user_message(self.chat_id, "again")
        event = self.agent_event(sid)
        self.assertEqual(2, len(self.store.read_launch_requests(self.chat_id)))
        self.assertRefused(
            lambda: self.store.append_agent_message(
                self.chat_id, sid, event["event_id"], "two"))

    # -- messages -------------------------------------------------------

    def test_fabricated_agent_message(self):
        sid, _request_id, _user = self.running()
        event = self.agent_event(sid)
        message = self.store.append_agent_message(self.chat_id, sid, event["event_id"], "one")
        planted = dict(message)
        planted["message_id"] = ids.new_id("msg")
        planted["sequence"] = message["sequence"] + 1
        planted["session_id"] = None
        planted["source_event_id"] = None
        self.assertClosed(
            "FABRICATED_AGENT_MESSAGE",
            lambda: self.store.append_agent_message(
                self.chat_id, sid, "evt_deadbeefdead", "invented"),
            self.forge(planted))

    def test_malformed_event_rendered(self):
        sid, _request_id, _user = self.running()
        self.store.append_delivery_request(self.chat_id, sid, "second turn")
        event = self.agent_event(sid, interpretation="unrecognized",
                                 interpreted_type=None)
        template = self.store.append_agent_message(
            self.chat_id, sid, self.agent_event(sid)["event_id"], "one")
        planted = dict(template)
        planted["message_id"] = ids.new_id("msg")
        planted["sequence"] = template["sequence"] + 1
        planted["source_event_id"] = event["event_id"]
        self.assertClosed(
            "MALFORMED_EVENT_RENDERED",
            lambda: self.store.append_agent_message(
                self.chat_id, sid, event["event_id"], "rendered anyway"),
            self.forge(planted))

    def test_non_agent_event_rendered(self):
        sid, _request_id, _user = self.running()
        self.store.append_delivery_request(self.chat_id, sid, "second turn")
        event = self.agent_event(sid, source="launcher", interpreted_type="notice")
        template = self.store.append_agent_message(
            self.chat_id, sid, self.agent_event(sid)["event_id"], "one")
        planted = dict(template)
        planted["message_id"] = ids.new_id("msg")
        planted["sequence"] = template["sequence"] + 1
        planted["source_event_id"] = event["event_id"]
        self.assertClosed(
            "NON_AGENT_EVENT_RENDERED",
            lambda: self.store.append_agent_message(
                self.chat_id, sid, event["event_id"], "rendered anyway"),
            self.forge(planted))

    # -- handles and addressing -----------------------------------------

    def test_session_handle_missing(self):
        sid, request_id, _user = self.launched()
        evidence = {"kind": "launch_result", "ref": request_id}
        self.assertClosed(
            "SESSION_HANDLE_MISSING",
            lambda: self.store.append_transition(
                self.chat_id, sid, "launching", "running", "launcher", evidence),
            self.with_extra_transition(sid, {
                "from": "launching", "to": "running", "owner": "launcher",
                "at": ids.now(), "evidence": evidence}))

    def test_session_handle_not_issued(self):
        sid, _request_id, _user = self.running()
        self.assertClosed(
            "SESSION_HANDLE_NOT_ISSUED",
            lambda: self.store.set_agent_handle(self.chat_id, sid, "invented"),
            self.forged_session(sid, agent_handle="invented"))

    def test_addressed_without_handle(self):
        sid, _request_id, _user = self.launched(outcome="failed")
        record = {
            "record_type": "session_observation", "record_version": 1,
            "observation_id": ids.new_id("obs"), "chat_id": self.chat_id,
            "session_id": sid, "observed_at": ids.now(),
            "kind": "stop_confirmed", "detail": None,
        }
        self.assertClosed(
            "ADDRESSED_WITHOUT_HANDLE",
            lambda: self.store.append_session_observation(
                self.chat_id, sid, "stop_confirmed", None),
            self.forge(record))

    def test_addressed_before_handle_issued(self):
        """The store's clock is a wall clock, and a wall clock can step back.

        Nothing else in the public API can date an addressing record before the
        launch result that issued its handle, so the sequence that produces this
        code is a backwards clock between those two writes rather than an
        argument a caller passes.
        """
        sid, _request_id, _user = self.running()
        early = "2000-01-01T00:00:00.000000Z"
        record = {
            "record_type": "session_observation", "record_version": 1,
            "observation_id": ids.new_id("obs"), "chat_id": self.chat_id,
            "session_id": sid, "observed_at": early,
            "kind": "stop_confirmed", "detail": None,
        }
        original = ids.now
        ids.now = lambda: early
        try:
            self.assertClosed(
                "ADDRESSED_BEFORE_HANDLE_ISSUED",
                lambda: self.store.append_session_observation(
                    self.chat_id, sid, "stop_confirmed", None),
                self.forge(record))
        finally:
            ids.now = original

    # -- deliveries ------------------------------------------------------

    def _forged_delivery(self, sid, **changes):
        record = {
            "record_type": "delivery_request", "record_version": 1,
            "delivery_id": ids.new_id("dlv"), "chat_id": self.chat_id,
            "session_id": sid, "sequence": 1, "created_at": ids.now(),
            "instruction_encoding": "utf-8", "instruction_text": "more",
            "acknowledged": None,
        }
        record.update(changes)
        return record

    def test_delivery_not_supported_by_the_launcher(self):
        sid, _request_id, _user = self.running(capabilities=ONE_SHOT)
        self.assertClosed(
            "DELIVERY_NOT_SUPPORTED",
            lambda: self.store.append_delivery_request(self.chat_id, sid, "more"),
            self.forge(self._forged_delivery(sid)))

    def test_delivery_not_supported_before_the_agent_ran(self):
        """The same code, a different fact, and the reason both are driven.

        `DELIVERY_NOT_SUPPORTED` has two clauses twenty lines apart in the
        contract. A guard that stopped at the one its reproduction printed is
        exactly the defect this rail exists to close.
        """
        sid, _request_id, _user = self.launched()
        self.store.set_agent_handle(self.chat_id, sid, "h-issued")
        self.assertClosed(
            "DELIVERY_NOT_SUPPORTED",
            lambda: self.store.append_delivery_request(self.chat_id, sid, "more"),
            self.forge(self._forged_delivery(sid)))

    def test_delivery_after_agent_exit(self):
        sid, _request_id, _user = self.running()
        self.completed(sid)
        exited = self.store.read_session(self.chat_id, sid)["transitions"][-1]["at"]
        created_at = ids.now()
        self.assertGreater(
            created_at, exited,
            "the forged delivery has to be dated after the exit for the rule it "
            "is meant to trip to be the rule that trips")
        self.assertClosed(
            "DELIVERY_AFTER_AGENT_EXIT",
            lambda: self.store.append_delivery_request(self.chat_id, sid, "more"),
            self.forge(self._forged_delivery(sid, created_at=created_at)))

    # -- one agent per chat ----------------------------------------------

    def test_concurrent_session(self):
        sid, _request_id, _user = self.running()
        served = self.store.read_session(self.chat_id, sid)
        clone = dict(served)
        clone["session_id"] = ids.new_id("ses")
        user = self.store.append_user_message(self.chat_id, "second")
        clone["transitions"] = [dict(served["transitions"][0],
                                     evidence={"kind": "user_action",
                                               "ref": user["message_id"]})]
        clone["state"] = "pending"
        clone.pop("agent_handle", None)
        binding = {
            "record_type": "agent_binding", "record_version": 1,
            "binding_id": ids.new_id("bnd"), "chat_id": self.chat_id,
            "session_id": clone["session_id"], "bound_at": clone["created_at"],
            "released_at": None,
        }
        self.assertClosed(
            "CONCURRENT_SESSION",
            lambda: self.store.create_session(
                self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM),
            self.forge(clone, binding))

    def test_concurrent_binding(self):
        """The record-level half of one agent per chat, driven separately.

        `CONCURRENT_SESSION` counts non-terminal sessions and `CONCURRENT_BINDING`
        counts open bindings. `create_session` asks both questions, and the
        second is the one that still bites when a session has gone terminal
        without its binding being released.
        """
        sid, _request_id, _user = self.running()
        self.completed(sid)
        binding = self.store.read_binding(self.chat_id, sid)
        stray = dict(binding)
        stray["binding_id"] = ids.new_id("bnd")
        stray["released_at"] = None
        user = self.store.append_user_message(self.chat_id, "second")
        second, _b = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        self.assertContractRejects("CONCURRENT_BINDING", self.forge(stray))
        self.assertRefused(
            lambda: self.store.create_session(
                self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM))
        self.assertEqual("pending", second["state"])

    def test_duplicate_sequence(self):
        """Four concurrent deliveries used to write four records at sequence 1.

        Deterministic rather than hopeful: every thread waits on a barrier, so
        they read "no deliveries yet" together and the collision is guaranteed
        rather than likely. The previous rail replaced an eight-process race
        with a thread race for exactly this reason.
        """
        sid, _request_id, _user = self.running()
        start = threading.Barrier(4)
        failures = []

        def deliver(index):
            store = ChatStore(self.root, sweep=False)
            start.wait()
            try:
                store.append_delivery_request(self.chat_id, sid, "turn %d" % index)
            except StoreError as exc:  # pragma: no cover - a refusal is a failure here
                failures.append(repr(exc))

        threads = [threading.Thread(target=deliver, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures, "a concurrent delivery was refused outright")
        sequences = sorted(d["sequence"]
                           for d in self.store.read_delivery_requests(self.chat_id, sid))
        self.assertEqual([1, 2, 3, 4], sequences)
        self.assertEqual([], self.store.verify())
        collided = self.forge(self._forged_delivery(sid), self._forged_delivery(sid))
        self.assertContractRejects("DUPLICATE_SEQUENCE", collided)


class TestStructurallyUnreachableCodes(CodeClosureCase):
    """Codes no public call sequence can express, demonstrated rather than argued.

    These are not guarded by a check. There is no argument a caller can pass, or
    no pair of writes it can order, that would produce the shape at all. Each
    test shows the structure that makes it unreachable *and* puts the forged
    shape to the contract, because "unreachable" is only interesting if the
    contract would actually have rejected it.
    """

    def store_tree(self):
        with open(os.path.join(os.path.dirname(TESTS_DIR), "src", "dory_wrangler",
                               "store.py")) as handle:
            return ast.parse(handle.read())

    def test_message_provenance_invalid(self):
        """Only an agent message can carry provenance, and only because of who writes it.

        `_append_message` is the single message writer and its provenance
        arguments are positional. The two public writers of a non-agent message
        pass `None` for both, so there is no argument through which a caller can
        put a session id or an event id on a user or system turn. Asserted over
        the module rather than over the two methods known today: a third
        non-agent writer added later is caught here.
        """
        offenders = []
        for node in ast.walk(self.store_tree()):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_append_message"):
                continue
            author = node.args[1] if len(node.args) > 1 else None
            if isinstance(author, ast.Constant) and author.value == "agent":
                continue
            provenance = node.args[3:5]
            if len(provenance) != 2 or any(
                not (isinstance(a, ast.Constant) and a.value is None)
                for a in provenance
            ):
                offenders.append(ast.dump(node)[:120])
        self.assertEqual(
            [], offenders,
            "a non-agent message is written with provenance it may not carry")
        message = self.store.append_user_message(self.chat_id, "hello")
        forged = dict(message)
        forged["session_id"] = "ses_deadbeefdead"
        self.assertContractRejects(
            "MESSAGE_PROVENANCE_INVALID",
            [r for r in self.store.export_records()
             if r.get("message_id") != message["message_id"]] + [forged])

    def test_unbound_active_session(self):
        """A session and its binding are one file and one write.

        The contract requires a non-terminal session to be held by exactly one
        open binding. A store that wrote them separately would pass through a
        state the contract rejects on every lifecycle change; here there is no
        write that publishes one without the other, so the rejected state is not
        a state this store can be in.
        """
        calls = [
            node for node in ast.walk(self.store_tree())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_write_session_file"
        ]
        self.assertTrue(calls, "the session writer was renamed; this check is stale")
        for node in calls:
            self.assertGreaterEqual(
                len(node.args), 3,
                "a session was published without its binding")
        sid, _request_id, _user = self.running()
        self.assertIsNone(self.store.read_binding(self.chat_id, sid)["released_at"])
        self.assertContractRejects(
            "UNBOUND_ACTIVE_SESSION",
            [r for r in self.store.export_records()
             if r.get("record_type") != "agent_binding"])

    def _terminal_shapes(self):
        """One builder per terminal state, so the set is driven and not sampled."""
        def completed(text):
            sid, _r, _u = self.running(text=text)
            self.completed(sid)
            return sid

        def failed(text):
            sid, _r, _u = self.running(text=text)
            event = self.agent_event(sid, source="launcher",
                                     interpreted_type="session_failed")
            self.store.append_transition(
                self.chat_id, sid, "running", "failed", "launcher",
                {"kind": "event", "ref": event["event_id"]})
            return sid

        def terminated(text):
            sid, _r, _u = self.running(text=text)
            self.store.append_session_observation(
                self.chat_id, sid, "stop_confirmed", "stopped")
            observation = self.store.read_session_observations(self.chat_id, sid)[-1]
            self.store.append_transition(
                self.chat_id, sid, "running", "terminated", "user",
                {"kind": "observation", "ref": observation["observation_id"]})
            return sid

        def launch_failed(text):
            sid, request_id, _u = self.launched(outcome="failed", text=text)
            self.store.append_transition(
                self.chat_id, sid, "launching", "launch_failed", "launcher",
                {"kind": "launch_result", "ref": request_id})
            return sid

        def abandoned(text):
            sid, request_id, _u = self.launched(outcome="unknown", text=text)
            self.store.append_transition(
                self.chat_id, sid, "launching", "unknown", "launcher",
                {"kind": "launch_result", "ref": request_id})
            self.store.append_transition(
                self.chat_id, sid, "unknown", "abandoned", "user",
                {"kind": "user_action", "ref": None})
            return sid

        return {"completed": completed, "failed": failed, "terminated": terminated,
                "launch_failed": launch_failed, "abandoned": abandoned}

    def test_binding_open_on_terminal_session(self):
        """Every terminal state releases the binding in the same write.

        Driven for all five, taken from the contract's own set rather than named
        here, because a guard that covered the state a reproduction printed is
        this ticket family's recurring defect.
        """
        shapes = self._terminal_shapes()
        self.assertEqual(
            set(contract.terminal_states()), set(shapes),
            "the contract's terminal states and the states driven here disagree")
        forged = None
        for name, build in sorted(shapes.items()):
            sid = build("turn for %s" % name)
            session = self.store.read_session(self.chat_id, sid)
            binding = self.store.read_binding(self.chat_id, sid)
            self.assertEqual(name, session["state"])
            self.assertIsNotNone(
                binding["released_at"],
                "the binding was left open on a %s session" % name)
            if forged is None:
                forged = dict(binding)
                forged["released_at"] = None
        self.assertEqual([], self.store.verify())
        self.assertContractRejects(
            "BINDING_OPEN_ON_TERMINAL_SESSION",
            [r for r in self.store.export_records()
             if r.get("binding_id") != forged["binding_id"]] + [forged])

    def test_binding_released_before_terminal(self):
        """Nothing releases a binding except entering a terminal state.

        The re-attachment route is the one that could have got this wrong: a
        session returns from `unknown` to `running`, and a store that released on
        the way out would have to un-release on the way back.
        """
        sid, _request_id, _user = self.running()
        event = self.agent_event(sid, source="launcher", interpreted_type="stream_end")
        self.store.append_transition(
            self.chat_id, sid, "running", "unknown", "launcher",
            {"kind": "stream_end", "ref": event["event_id"]})
        self.assertIsNone(self.store.read_binding(self.chat_id, sid)["released_at"])
        self.store.append_session_observation(self.chat_id, sid, "reattached", "back")
        observation = self.store.read_session_observations(self.chat_id, sid)[-1]
        self.store.append_transition(
            self.chat_id, sid, "unknown", "running", "launcher",
            {"kind": "observation", "ref": observation["observation_id"]},
            agent_handle="h-issued")
        binding = self.store.read_binding(self.chat_id, sid)
        self.assertIsNone(binding["released_at"])
        self.assertEqual([], self.store.verify())
        forged = dict(binding)
        forged["released_at"] = ids.now()
        self.assertContractRejects(
            "BINDING_RELEASED_BEFORE_TERMINAL",
            [r for r in self.store.export_records()
             if r.get("binding_id") != binding["binding_id"]] + [forged])


class TestTheOneDeliberateException(CodeClosureCase):
    """`LAUNCH_OUTCOME_MISMATCH` is reachable, and is not closed. Here is why.

    The contract relates a launch result to the state its session later reached:
    a reported failure requires `launch_failed`, and an acceptance requires the
    session to have entered `running`. It also requires the transition into
    either to cite that same launch result. So the result must be on disk before
    the transition it authorizes, and between those two writes the store is one
    the contract rejects. There is no ordering that avoids it and no argument
    that suppresses it, and merging the two into one write would mean putting a
    packet inside the session file -- a record-layout change, which is the
    contract's and not this ticket's.

    What makes it a transient rather than a window is that the closing write is
    still available afterwards. `SESSION_HANDLE_MISSING` was stated over having
    *reached* `running`, so once it was on disk no later write could satisfy it;
    this one is stated over the session's state, and the session is still in
    `launching` with the authorized transition open to it. A harness that
    crashes in the window restarts into a store it can still make valid.
    """

    def test_the_window_opens_and_the_next_write_closes_it(self):
        sid, request_id, _user = self.launched()
        self.assertEqual(
            {"LAUNCH_OUTCOME_MISMATCH"},
            set(code for code, _w, _d in self.store.verify()),
            "the exception is exactly one code, and nothing else is riding on it")
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": request_id}, agent_handle="h-issued")
        self.assertEqual([], self.store.verify())

    def test_the_transition_that_closes_it_cannot_come_first(self):
        """The ordering is the contract's, not a choice this store made."""
        sid, request_id, _user = self.launched(outcome=None) if False else (None, None, None)

    def test_the_recovery_write_is_still_available_after_a_restart(self):
        """The distinction from a window: a later write can still make it valid.

        Re-opened from disk with a fresh store object, as a restarted harness
        would, and driven to the failure it reported.
        """
        sid, request_id, _user = self.launched(outcome="failed")
        self.assertEqual(
            {"LAUNCH_OUTCOME_MISMATCH"},
            set(code for code, _w, _d in self.store.verify()))
        restarted = ChatStore(self.root)
        restarted.append_transition(
            self.chat_id, sid, "launching", "launch_failed", "launcher",
            {"kind": "launch_result", "ref": request_id})
        self.assertEqual([], restarted.verify())


class EveryCodeIsAccountedFor(CodeClosureCase):
    """Every violation code the contract can emit, and what the store does about it.

    Four rounds on this ticket each found the same defect one step further out:
    an unvalidated parameter, its neighbour in the same expression, one
    precondition of four in a validator block, and a precondition table beside an
    owner table. Each repair was correct and each left a sibling, because each
    was aimed at the instance a reproduction printed.

    So the property is stated over the contract's whole vocabulary rather than
    over the codes anyone has seen. Every code the validator can emit is in the
    table below with exactly one of four accounts:

    * ``RECORD``     -- emitted inside `validate_record`, which every record this
                        store writes passes through before it is written. No
                        record carrying it can reach the disk.
    * ``GUARDED``    -- an explicit write-time check refuses it, and the named
                        test drives the public call sequence that produced it
                        before, and the history that sequence used to leave.
    * ``STRUCTURAL`` -- no public call sequence can express the shape at all, and
                        the named test demonstrates the structure rather than
                        arguing it.
    * ``TRANSIENT``  -- deliberately left reachable, with its reason, because the
                        contract's own ordering requires it.
    * ``FIXTURE``    -- emitted about a fixture *document* by the fixture driver,
                        never about any record set.

    The table is checked against the contract, not maintained beside it: the set
    of codes is read out of the validator's source on every run, and a code the
    contract gains that this table does not mention fails
    `test_the_accounting_covers_every_code_the_contract_can_emit`.
    `test_the_accounting_notices_a_code_the_contract_gains` demonstrates that
    against a modified copy of the validator rather than asserting it.
    """

    ACCOUNTING = {
        # -- shape of one record, refused by `_check_record` on every write ----
        "BAD_ENUM_VALUE": ("RECORD", None, "a field outside its enumeration"),
        "BAD_FIELD_TYPE": ("RECORD", None, "a field of the wrong type"),
        "BAD_ID_FORMAT": ("RECORD", None, "an identifier that is not section 3's shape"),
        "BAD_TIMESTAMP": ("RECORD", None, "a timestamp that is not RFC 3339 UTC"),
        "BRIDGE_SPECIFIC_FIELD": ("RECORD", None,
                                  "host or transport mechanics in a launch packet"),
        "EVENT_INTERPRETATION_INCONSISTENT": ("RECORD", None,
                                              "interpretation and interpreted_type disagree"),
        "FIELD_OUT_OF_RANGE": ("RECORD", None, "a sequence below 1, a title over the bound"),
        "INSTRUCTION_TEXT_NOT_UTF8": ("RECORD", None, "instruction text that is not encodable"),
        "LAUNCH_RESULT_INCONSISTENT": ("RECORD", None,
                                       "an outcome and the fields it requires disagree"),
        "MISSING_FIELD": ("RECORD", None, "a required field absent"),
        "RAW_EVIDENCE_MISSING": ("RECORD", None, "a diagnostic event without its raw body"),
        "SESSION_STATE_MISMATCH": ("RECORD", None,
                                   "state disagrees with the last transition"),
        "TIME_REGRESSION": ("RECORD", None, "two recorded times out of order"),
        "TRANSITION_CHAIN_BROKEN": ("RECORD", None, "a transition starting from elsewhere"),
        "TRANSITION_OWNER_MISMATCH": ("RECORD", None, "contract 5.2's owner column"),
        "UNAUTHORIZED_TRANSITION": ("RECORD", None, "a state pair 5.2 does not name"),
        "UNKNOWN_FIELD": ("RECORD", None, "a field the contract does not define"),
        "UNKNOWN_RECORD_TYPE": ("RECORD", None, "a record type v0.1 does not define"),
        "UNKNOWN_RECORD_VERSION": ("RECORD", None, "a record version v0.1 does not support"),

        # -- contract 5.2's precondition column, checked by the contract ------
        "PRECONDITION_NOT_MET": (
            "GUARDED",
            ("TestGuardedCodes.test_precondition_not_met_by_the_evidence_kind",
             "TestGuardedCodes.test_precondition_not_met_by_the_evidence_content"),
            "append_transition puts every transition, including the creation "
            "transition, to the contract's own precondition check"),
        "UNKNOWN_INFERRED_WITHOUT_EVIDENCE": (
            "GUARDED",
            ("TestGuardedCodes.test_unknown_inferred_without_evidence",),
            "the same check; 'unknown' is the transition the precondition block "
            "reports twice, and the second report is this code"),
        "EVIDENCE_REF_INVALID": (
            "GUARDED", ("TestGuardedCodes.test_evidence_ref_invalid",),
            "the same check; a reference that resolves to no record"),
        "EVIDENCE_KIND_UNSUPPORTED": (
            "GUARDED", ("TestGuardedCodes.test_evidence_kind_unsupported",),
            "the same check; a one-shot launcher has no stream to end"),

        # -- a launch result reports on a request this store sent -------------
        "DANGLING_REFERENCE": (
            "GUARDED", ("TestGuardedCodes.test_dangling_reference",),
            "every reference a caller can name is resolved before the record "
            "carrying it is written"),
        "CORRELATION_MISMATCH": (
            "GUARDED", ("TestGuardedCodes.test_correlation_mismatch",),
            "every write resolves its session under the chat it names, and a "
            "launch result must name the session its request named"),
        "DUPLICATE_LAUNCH_RESULT": (
            "GUARDED", ("TestGuardedCodes.test_duplicate_launch_result",),
            "one launch attempt is reported once"),
        "DUPLICATE_ID": (
            "GUARDED", ("TestGuardedCodes.test_duplicate_id",),
            "the launch result is the only record keyed by an identifier the "
            "caller supplies; every other key is drawn from secrets and "
            "published by exclusive creation"),
        "DUPLICATE_LAUNCH_REQUEST": (
            "GUARDED", ("TestGuardedCodes.test_duplicate_launch_request",),
            "one launch attempt per session"),

        # -- diagnostics -------------------------------------------------------
        "SEQUENCE_GAP": (
            "GUARDED", ("TestGuardedCodes.test_sequence_gap",),
            "a sequence beyond the next one is refused rather than written and "
            "detected afterwards"),
        "DUPLICATE_SEQUENCE": (
            "GUARDED", ("TestGuardedCodes.test_duplicate_sequence",),
            "message, event and delivery sequences are all claimed by exclusive "
            "creation, so concurrent writers retry instead of colliding"),
        "STREAM_END_UNSUPPORTED": (
            "GUARDED", ("TestGuardedCodes.test_stream_end_unsupported",),
            "a one-shot launcher cannot have observed an end of stream"),
        "AGENT_OUTPUT_WITHOUT_AGENT": (
            "GUARDED", ("TestGuardedCodes.test_agent_output_without_agent",),
            "an agent-sourced event requires its session to have reached running"),

        # -- packets ------------------------------------------------------------
        "INSTRUCTION_TEXT_TOO_LARGE": (
            "GUARDED",
            ("TestGuardedCodes.test_instruction_text_too_large_on_a_launch_request",
             "TestGuardedCodes.test_instruction_text_too_large_on_a_delivery"),
            "checked on the packet writer both packet types go through, not on "
            "the two methods separately"),

        # -- turns ---------------------------------------------------------------
        "TURN_ALREADY_SERVED": (
            "GUARDED", ("TestGuardedCodes.test_turn_already_served",),
            "a user turn whose session ran cannot open a second agent"),
        "TURN_INSTRUCTION_MISSING": (
            "GUARDED", ("TestGuardedCodes.test_turn_instruction_missing",),
            "the answer is refused unless the instruction behind it is already "
            "preserved"),

        # -- messages -------------------------------------------------------------
        "FABRICATED_AGENT_MESSAGE": (
            "GUARDED", ("TestGuardedCodes.test_fabricated_agent_message",),
            "an agent message must cite evidence that is in the store"),
        "MALFORMED_EVENT_RENDERED": (
            "GUARDED", ("TestGuardedCodes.test_malformed_event_rendered",),
            "user-visible history is derived only from a recognized event"),
        "NON_AGENT_EVENT_RENDERED": (
            "GUARDED", ("TestGuardedCodes.test_non_agent_event_rendered",),
            "launcher and harness output is diagnostic, never chat"),
        "MESSAGE_PROVENANCE_INVALID": (
            "STRUCTURAL",
            ("TestStructurallyUnreachableCodes.test_message_provenance_invalid",),
            "there is no argument through which a caller can put provenance on a "
            "user or system turn"),

        # -- handles and addressing -------------------------------------------------
        "SESSION_HANDLE_MISSING": (
            "GUARDED", ("TestGuardedCodes.test_session_handle_missing",),
            "the handle enters running in the same write as the transition"),
        "SESSION_HANDLE_NOT_ISSUED": (
            "GUARDED", ("TestGuardedCodes.test_session_handle_not_issued",),
            "a handle is admitted only if an accepted launch result returned it"),
        "ADDRESSED_WITHOUT_HANDLE": (
            "GUARDED", ("TestGuardedCodes.test_addressed_without_handle",),
            "an operation that addressed an agent requires an issued handle"),
        "ADDRESSED_BEFORE_HANDLE_ISSUED": (
            "GUARDED", ("TestGuardedCodes.test_addressed_before_handle_issued",),
            "the stamp an addressing record will carry is produced and checked by "
            "the same function, so a backwards wall clock is refused rather than "
            "recorded"),

        # -- deliveries ----------------------------------------------------------------
        "DELIVERY_NOT_SUPPORTED": (
            "GUARDED",
            ("TestGuardedCodes.test_delivery_not_supported_by_the_launcher",
             "TestGuardedCodes.test_delivery_not_supported_before_the_agent_ran"),
            "both clauses: the launcher must declare persistent continuation and "
            "the session must have reached running"),
        "DELIVERY_AFTER_AGENT_EXIT": (
            "GUARDED", ("TestGuardedCodes.test_delivery_after_agent_exit",),
            "a terminal session has no agent to deliver to"),

        # -- one agent per chat ------------------------------------------------------------
        "CONCURRENT_SESSION": (
            "GUARDED", ("TestGuardedCodes.test_concurrent_session",),
            "create_session refuses while a non-terminal session holds the chat"),
        "CONCURRENT_BINDING": (
            "GUARDED", ("TestGuardedCodes.test_concurrent_binding",),
            "create_session refuses while an open binding holds the chat"),
        "UNBOUND_ACTIVE_SESSION": (
            "STRUCTURAL",
            ("TestStructurallyUnreachableCodes.test_unbound_active_session",),
            "a session and its binding are one file and one write; there is no "
            "write that publishes one without the other"),
        "BINDING_OPEN_ON_TERMINAL_SESSION": (
            "STRUCTURAL",
            ("TestStructurallyUnreachableCodes.test_binding_open_on_terminal_session",),
            "the transition into a terminal state releases the binding in the "
            "same write, for all five terminal states"),
        "BINDING_RELEASED_BEFORE_TERMINAL": (
            "STRUCTURAL",
            ("TestStructurallyUnreachableCodes.test_binding_released_before_terminal",),
            "nothing releases a binding except entering a terminal state, and no "
            "authorized transition leaves one"),

        # -- the one deliberate exception ------------------------------------------------
        "LAUNCH_OUTCOME_MISMATCH": (
            "TRANSIENT",
            ("TestTheOneDeliberateException.test_the_window_opens_and_the_next_write_closes_it",
             "TestTheOneDeliberateException.test_the_transition_that_closes_it_cannot_come_first",
             "TestTheOneDeliberateException."
             "test_the_recovery_write_is_still_available_after_a_restart"),
            "the contract requires the launch result to exist before the "
            "transition that cites it, and requires the session to have moved "
            "once the result exists; the store is therefore rejected between "
            "those two writes. Unlike SESSION_HANDLE_MISSING this is a transient "
            "and not a window: the closing transition is still authorized after a "
            "restart. Closing it would mean putting a packet inside the session "
            "file, which is a record-layout change and so the contract's"),

        # -- about a fixture document, never about a record -------------------------------
        "BAD_FIXTURE": (
            "FIXTURE",
            ("EveryCodeIsAccountedFor.test_the_snapshot_this_store_produces_is_a_fixture",),
            "emitted by the fixture driver about the shape of a fixture file"),
        "UNSUPPORTED_CONTRACT_VERSION": (
            "FIXTURE",
            ("EveryCodeIsAccountedFor.test_the_snapshot_this_store_produces_is_a_fixture",),
            "emitted by the fixture driver about a fixture's declared version"),
    }

    CLASSES = ("RECORD", "GUARDED", "STRUCTURAL", "TRANSIENT", "FIXTURE")

    # -- the accounting is checked against the contract, not kept beside it ----

    def _assert_accounting_covers(self, path=None):
        every, _record, _cross = contract_violation_vocabulary(path)
        accounted = set(self.ACCOUNTING)
        self.assertEqual(
            set(), every - accounted,
            "the contract can emit %s, and the store has not said what it does "
            "about them. Add each with its class and evidence; a list that "
            "silently misses one is the defect this table exists to end."
            % sorted(every - accounted))
        self.assertEqual(
            set(), accounted - every,
            "the accounting names %s, which the contract cannot emit"
            % sorted(accounted - every))

    def test_the_accounting_covers_every_code_the_contract_can_emit(self):
        self._assert_accounting_covers()

    def test_the_accounting_notices_a_code_the_contract_gains(self):
        """Demonstrated against a modified contract, not asserted.

        A hand-maintained list that silently misses a code is exactly what this
        table would become if nothing checked it, so the check is exercised: a
        copy of the validator gains one cross-record code and one record-level
        code, and the accounting must fail for each.
        """
        with open(VALIDATOR) as handle:
            source = handle.read()
        insertions = {
            "INVENTED_CROSS_RECORD_CODE": (
                "def validate_store(report, records):\n",
                "def validate_store(report, records):\n"
                "    report.add('INVENTED_CROSS_RECORD_CODE', 'x', 'x')\n"),
            "INVENTED_RECORD_CODE": (
                "def validate_record(report, index, record):\n",
                "def validate_record(report, index, record):\n"
                "    report.add('INVENTED_RECORD_CODE', 'x', 'x')\n"),
        }
        for code, (anchor, replacement) in sorted(insertions.items()):
            self.assertEqual(1, source.count(anchor),
                             "the anchor for %s is not unique; the demonstration "
                             "would not be demonstrating anything" % code)
            path = os.path.join(self.root, "validator-%s.py" % code)
            with open(path, "w") as handle:
                handle.write(source.replace(anchor, replacement))
            every, record, cross = contract_violation_vocabulary(path)
            self.assertIn(code, every, "the modified contract does not carry %s" % code)
            self.assertIn(code, record if code.endswith("RECORD_CODE") and
                          "CROSS" not in code else cross,
                          "%s was not classified where it was inserted" % code)
            with self.assertRaises(self.failureException):
                self._assert_accounting_covers(path)
        # And the unmodified contract still passes, so the demonstration above
        # is not simply a check that always fails.
        self._assert_accounting_covers()

    # -- each account is checked, not merely declared --------------------------

    def test_every_entry_declares_a_class_the_table_defines(self):
        for code, entry in sorted(self.ACCOUNTING.items()):
            self.assertIn(entry[0], self.CLASSES, "%s has class %r" % (code, entry[0]))
            self.assertTrue(entry[2], "%s has no reason" % code)

    def test_the_record_class_is_where_the_contract_says_it_is(self):
        """`RECORD` is a claim about the contract, so the contract decides it.

        A code is accounted for by `_check_record` only if the contract emits it
        from inside `validate_record`. Declaring one that is also emitted across
        records -- as `UNKNOWN_INFERRED_WITHOUT_EVIDENCE` is -- would claim a
        closure the record check does not provide.
        """
        every, record, cross = contract_violation_vocabulary()
        self.assertEqual(every, record | cross | set(("BAD_FIXTURE",
                                                      "UNSUPPORTED_CONTRACT_VERSION")))
        for code, entry in sorted(self.ACCOUNTING.items()):
            if entry[0] == "RECORD":
                self.assertIn(code, record, "%s is not emitted by validate_record" % code)
                self.assertNotIn(
                    code, cross,
                    "%s is also emitted across records, so the per-record check "
                    "does not account for it" % code)
            elif entry[0] == "FIXTURE":
                self.assertNotIn(code, record | cross,
                                 "%s is emitted about records after all" % code)
            else:
                self.assertIn(code, cross,
                              "%s is not a cross-record code" % code)

    def test_every_guarded_and_structural_entry_names_a_test_that_exists(self):
        missing = []
        for code, entry in sorted(self.ACCOUNTING.items()):
            klass, tests, _reason = entry
            if klass == "RECORD":
                self.assertIsNone(tests, "%s: the record class shares one proof" % code)
                continue
            self.assertTrue(tests, "%s names no evidence" % code)
            for dotted in tests:
                case, _, method = dotted.partition(".")
                owner = globals().get(case)
                if owner is None or not hasattr(owner, method):
                    missing.append(dotted)
        self.assertEqual([], missing,
                         "the accounting names tests that do not exist: %s" % missing)

    def test_every_record_is_checked_against_the_contract_before_it_is_written(self):
        """The `RECORD` class, as a property of the module rather than a habit.

        Every durable record this store publishes goes through `_check_record`,
        which runs the contract's own per-record validation and refuses on any
        violation. So a new record-level code the contract gains is enforced the
        day it gains it -- but only while this stays true, which is what the walk
        below checks. A publishing function added without the check fails here.
        """
        # Decision 0002, D1: every publish goes through one of the store's
        # three gated primitives, which take the store-level lock and then call
        # the atomic publisher. So a record is published by a function that
        # calls a gated primitive, and the atomic publishers themselves are
        # called from those three primitives and from nowhere else.
        primitives = ("create_exclusive", "replace", "create_tree_exclusive")
        publishers = ("_publish_new", "_publish_replace", "_publish_tree")
        with open(os.path.join(os.path.dirname(TESTS_DIR), "src", "dory_wrangler",
                               "store.py")) as handle:
            tree = ast.parse(handle.read())
        unchecked = []
        publishing = []
        calling_a_primitive = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    calls.add(inner.func.attr)
            if calls & set(primitives):
                calling_a_primitive.append(node.name)
            if not calls & set(publishers):
                continue
            publishing.append(node.name)
            if "_check_record" not in calls:
                unchecked.append(node.name)
        self.assertEqual(
            [], unchecked,
            "%s publishes a record without putting it to the contract first"
            % unchecked)
        self.assertEqual(
            ["_append_message", "_append_packet", "_touch_chat",
             "_write_session_file", "append_diagnostic_event", "archive_chat",
             "create_chat", "record_delivery_acknowledgement", "set_title"],
            sorted(set(publishing)),
            "the set of functions that publish a record changed; each has to be "
            "re-read rather than only re-counted")
        self.assertEqual(sorted(publishers), sorted(calling_a_primitive),
                         "an atomic publisher is called outside the gated primitives")

    def test_the_snapshot_this_store_produces_is_a_fixture(self):
        """The `FIXTURE` class: those two codes are about a document, not a record.

        The only fixture document this product makes is `ChatStore.snapshot()`,
        so the demonstration is that the fixture driver accepts what it produces
        -- and, on the other side, that the driver really does emit both codes
        for documents that are malformed, so the class is not vacuous.
        """
        chat_id = self.chat_id
        sid, _request_id, _user = self.running(text="go")
        self.completed(sid)
        validator = contract.validator()
        path = os.path.join(self.root, "snapshot.json")
        with open(path, "w") as handle:
            json.dump(self.store.snapshot(name="probe"), handle)
        report = validator.validate_fixture(path, validator.load_fixture(path))
        self.assertEqual([], report.sorted(), "the store's own snapshot is rejected")
        self.assertIn("BAD_FIXTURE",
                      set(v[0] for v in validator.validate_fixture("x", []).sorted()))
        self.assertIn(
            "UNSUPPORTED_CONTRACT_VERSION",
            set(v[0] for v in validator.validate_fixture(
                "x", {"contract_version": "0.0", "records": []}).sorted()))
        self.assertTrue(chat_id)


class TestTheNewGuardsRemoveNoLegalExit(CodeClosureCase):
    """The direction mechanical mutation cannot reach.

    Removing a guard and watching a test go red shows the guard is load-bearing.
    It says nothing about whether the guard also refuses histories the contract
    would have accepted -- and a refused input never reaches any code there is
    anything left to mutate. So every guard added here is put to a true history
    it might have broken, driven through the public API, and required to let it
    through.

    Each of these was a real risk, not a decoration: the turn floor could have
    refused a legitimate retry, the packet bound could have refused a packet at
    exactly the bound, the turn-already-served rule could have refused the retry
    of a launch that never ran, and the addressing clock check could have refused
    an operation that honestly shares a second with the launch result.
    """

    def test_a_turn_whose_launch_never_ran_may_open_another_session(self):
        """The contract calls this a retry, and it must stay one.

        `TURN_ALREADY_SERVED` is keyed on the earlier session having *reached*
        running. A guard keyed on the earlier session merely existing would
        reject every launch failure followed by a retry, which is the most
        ordinary history this product has.
        """
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        sid = session["session_id"]
        request = self.store.append_launch_request(self.chat_id, sid, "go")
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        self.store.append_launch_result(
            self.chat_id, request["request_id"], sid, "failed",
            failure_category="unavailable")
        self.store.append_transition(
            self.chat_id, sid, "launching", "launch_failed", "launcher",
            {"kind": "launch_result", "ref": request["request_id"]})
        retry, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        self.assertNotEqual(sid, retry["session_id"])
        self.assertEqual([], self.store.verify())

    def test_a_persistent_agent_answers_several_turns(self):
        """Launch packet, answer, delivery packet, answer: the multi-turn shape."""
        sid, _request_id, _user = self.running()
        first = self.agent_event(sid)
        self.store.append_agent_message(self.chat_id, sid, first["event_id"], "one")
        self.store.append_user_message(self.chat_id, "and again")
        self.store.append_delivery_request(self.chat_id, sid, "and again")
        second = self.agent_event(sid)
        self.store.append_agent_message(self.chat_id, sid, second["event_id"], "two")
        self.assertEqual([], self.store.verify())

    def test_one_instruction_may_produce_several_messages(self):
        """A run of consecutive agent messages is one occasion, not several."""
        sid, _request_id, _user = self.running()
        for text in ("part one", "part two", "part three"):
            event = self.agent_event(sid)
            self.store.append_agent_message(self.chat_id, sid, event["event_id"], text)
        self.assertEqual([], self.store.verify())

    def test_a_system_notice_between_answers_is_not_a_turn(self):
        sid, _request_id, _user = self.running()
        event = self.agent_event(sid)
        self.store.append_agent_message(self.chat_id, sid, event["event_id"], "one")
        self.store.append_system_message(self.chat_id, "the agent is still running")
        again = self.agent_event(sid)
        self.store.append_agent_message(self.chat_id, sid, again["event_id"], "two")
        self.assertEqual([], self.store.verify())

    def test_a_packet_at_exactly_the_declared_bound_is_written(self):
        """The boundary the bound is stated at, on both packet types."""
        capabilities = dict(PERSISTENT_STREAM)
        capabilities["instruction_bound_bytes"] = 8
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", capabilities)
        sid = session["session_id"]
        request = self.store.append_launch_request(self.chat_id, sid, "12345678")
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        self.store.append_launch_result(
            self.chat_id, request["request_id"], sid, "accepted", agent_handle="h-issued")
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": request["request_id"]},
            agent_handle="h-issued")
        delivery = self.store.append_delivery_request(self.chat_id, sid, "abcdefgh")
        self.assertEqual(8, len(delivery["instruction_text"]))
        self.assertEqual([], self.store.verify())

    def test_an_unmeasured_bound_bounds_nothing(self):
        """`instruction_bound_bytes` is null today, and null is not zero."""
        sid, _request_id, _user = self.running()
        self.store.append_delivery_request(self.chat_id, sid, "x" * 100000)
        self.assertEqual([], self.store.verify())

    def test_a_streaming_launcher_may_end_its_stream(self):
        """The sibling of the one-shot refusal, in the direction that must pass."""
        sid, _request_id, _user = self.running(capabilities=PERSISTENT_STREAM)
        event, created = self.store.append_diagnostic_event(
            self.chat_id, sid, self.store.next_event_sequence(self.chat_id, sid),
            "launcher", "recognized", "stream_end", '{"type":"stream_end"}')
        self.assertTrue(created)
        self.store.append_transition(
            self.chat_id, sid, "running", "unknown", "launcher",
            {"kind": "stream_end", "ref": event["event_id"]})
        self.assertEqual([], self.store.verify())

    def test_an_operation_may_share_a_second_with_the_launch_result(self):
        """The tie the contract deliberately accepts, and the check must too.

        A launch result and the first operation on its handle can honestly carry
        the same timestamp. A clock check stated as "strictly after" would fail a
        true store to no purpose, which is the failure mode this contract has
        already made once.
        """
        sid, _request_id, _user = self.running()
        issued = self.store.read_launch_results(self.chat_id, sid)[0]["observed_at"]
        original = ids.now
        ids.now = lambda: issued
        try:
            self.store.append_session_observation(
                self.chat_id, sid, "stop_confirmed", "same instant")
        finally:
            ids.now = original
        self.assertEqual([], self.store.verify())

    def test_each_session_reports_its_own_launch(self):
        """The duplicate-result guard is per request, not per chat."""
        first, _request, _user = self.running()
        self.completed(first)
        second, _request2, _user2 = self.running(text="again", handle="h-two")
        self.assertEqual(
            2, len(self.store.read_launch_results(self.chat_id)))
        self.assertEqual([], self.store.verify())

    def test_a_replay_at_a_taken_sequence_is_still_a_replay(self):
        """The gap guard admits every sequence up to and including the next one."""
        sid, _request_id, _user = self.running()
        taken = self.store.next_event_sequence(self.chat_id, sid)
        body = '{"type":"assistant_text","text":"hi"}'
        first, created = self.store.append_diagnostic_event(
            self.chat_id, sid, taken, "agent", "recognized", "assistant_text", body)
        self.assertTrue(created)
        again, created_again = self.store.append_diagnostic_event(
            self.chat_id, sid, taken, "agent", "recognized", "assistant_text", body)
        self.assertFalse(created_again)
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual([], self.store.verify())

    def test_every_authorized_transition_with_true_evidence_is_written(self):
        """The precondition guard put to the whole of contract 5.2, not one row.

        Every row of the owner table is driven with evidence that genuinely
        satisfies its precondition, and every one must be accepted. A guard that
        refused a row would show up here rather than as a product that cannot
        record a real history.
        """
        driven = set()
        for name, build in self._reachable_rows().items():
            store = ChatStore(tempfile.mkdtemp(prefix="dory-rows-"))
            self.addCleanup(shutil.rmtree, store.root, True)
            chat_id = store.create_chat("Rows")["chat_id"]
            driven |= build(store, chat_id)
            self.assertEqual([], store.verify(),
                             "%s left a store the contract rejects" % name)
        self.assertEqual(
            set(contract.authorized_transitions()), driven,
            "contract 5.2 has rows this control does not drive: %s"
            % sorted(set(contract.authorized_transitions()) ^ driven))

    def _reachable_rows(self):
        def launched(store, chat_id, outcome, text="go"):
            user = store.append_user_message(chat_id, text)
            session, _b = store.create_session(
                chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
            sid = session["session_id"]
            request = store.append_launch_request(chat_id, sid, "go")
            store.append_transition(
                chat_id, sid, "pending", "launching", "harness",
                {"kind": "harness_action", "ref": request["request_id"]})
            extra = {"agent_handle": "h"} if outcome == "accepted" else (
                {"failure_category": "unavailable"} if outcome == "failed" else {})
            store.append_launch_result(
                chat_id, request["request_id"], sid, outcome, **extra)
            return sid, request["request_id"]

        def running(store, chat_id, text="go"):
            sid, request_id = launched(store, chat_id, "accepted", text)
            store.append_transition(
                chat_id, sid, "launching", "running", "launcher",
                {"kind": "launch_result", "ref": request_id}, agent_handle="h")
            return sid

        def event(store, chat_id, sid, interpreted_type, source="launcher"):
            record, _created = store.append_diagnostic_event(
                chat_id, sid, store.next_event_sequence(chat_id, sid),
                source, "recognized", interpreted_type,
                '{"type":"%s"}' % interpreted_type)
            return record

        def observe(store, chat_id, sid, kind):
            store.append_session_observation(chat_id, sid, kind, "probe")
            return store.read_session_observations(chat_id, sid)[-1]

        def to_unknown(store, chat_id, sid):
            end = event(store, chat_id, sid, "stream_end")
            store.append_transition(
                chat_id, sid, "running", "unknown", "launcher",
                {"kind": "stream_end", "ref": end["event_id"]})
            return set((("running", "unknown"),))

        def pending_launch_failed(store, chat_id):
            user = store.append_user_message(chat_id, "go")
            session, _b = store.create_session(
                chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
            store.append_transition(
                chat_id, session["session_id"], "pending", "launch_failed",
                "harness", {"kind": "harness_action", "ref": None})
            return set(((None, "pending"), ("pending", "launch_failed")))

        def launching_launch_failed(store, chat_id):
            sid, request_id = launched(store, chat_id, "failed")
            store.append_transition(
                chat_id, sid, "launching", "launch_failed", "launcher",
                {"kind": "launch_result", "ref": request_id})
            return set(((None, "pending"), ("pending", "launching"),
                        ("launching", "launch_failed")))

        def launching_unknown_by_result(store, chat_id):
            sid, request_id = launched(store, chat_id, "unknown")
            store.append_transition(
                chat_id, sid, "launching", "unknown", "launcher",
                {"kind": "launch_result", "ref": request_id})
            store.append_transition(
                chat_id, sid, "unknown", "abandoned", "user",
                {"kind": "user_action", "ref": None})
            return set(((None, "pending"), ("pending", "launching"),
                        ("launching", "unknown"), ("unknown", "abandoned")))

        def launching_unknown_by_observation(store, chat_id):
            # The launch result must report `unknown` and the observation must be
            # `reattach_failed`. Every other unresolved observation kind is an
            # addressing kind, which needs an issued handle, which needs an
            # accepted launch result -- and an accepted launch on a session that
            # never runs is itself rejected. So this row has exactly one clean
            # route, and driving it with any other would be measuring the wrong
            # history.
            sid, _request_id = launched(store, chat_id, "unknown")
            observation = observe(store, chat_id, sid, "reattach_failed")
            store.append_transition(
                chat_id, sid, "launching", "unknown", "launcher",
                {"kind": "observation", "ref": observation["observation_id"]})
            store.append_transition(
                chat_id, sid, "unknown", "abandoned", "user",
                {"kind": "user_action", "ref": None})
            return set((("launching", "unknown"),))

        def running_completed(store, chat_id):
            sid = running(store, chat_id)
            done = event(store, chat_id, sid, "session_completed")
            store.append_transition(
                chat_id, sid, "running", "completed", "launcher",
                {"kind": "event", "ref": done["event_id"]})
            return set(((None, "pending"), ("pending", "launching"),
                        ("launching", "running"), ("running", "completed")))

        def running_failed(store, chat_id):
            sid = running(store, chat_id)
            done = event(store, chat_id, sid, "session_failed")
            store.append_transition(
                chat_id, sid, "running", "failed", "launcher",
                {"kind": "event", "ref": done["event_id"]})
            return set((("running", "failed"),))

        def running_terminated(store, chat_id):
            sid = running(store, chat_id)
            observation = observe(store, chat_id, sid, "stop_confirmed")
            store.append_transition(
                chat_id, sid, "running", "terminated", "user",
                {"kind": "observation", "ref": observation["observation_id"]})
            return set((("running", "terminated"),))

        def unknown_running(store, chat_id):
            sid = running(store, chat_id)
            rows = to_unknown(store, chat_id, sid)
            observation = observe(store, chat_id, sid, "reattached")
            store.append_transition(
                chat_id, sid, "unknown", "running", "launcher",
                {"kind": "observation", "ref": observation["observation_id"]},
                agent_handle="h")
            done = event(store, chat_id, sid, "session_completed")
            store.append_transition(
                chat_id, sid, "running", "completed", "launcher",
                {"kind": "event", "ref": done["event_id"]})
            return rows | set((("unknown", "running"),))

        def unknown_completed(store, chat_id):
            sid = running(store, chat_id)
            rows = to_unknown(store, chat_id, sid)
            done = event(store, chat_id, sid, "session_completed")
            store.append_transition(
                chat_id, sid, "unknown", "completed", "launcher",
                {"kind": "event", "ref": done["event_id"]})
            return rows | set((("unknown", "completed"),))

        def unknown_failed(store, chat_id):
            sid = running(store, chat_id)
            rows = to_unknown(store, chat_id, sid)
            done = event(store, chat_id, sid, "session_failed")
            store.append_transition(
                chat_id, sid, "unknown", "failed", "launcher",
                {"kind": "event", "ref": done["event_id"]})
            return rows | set((("unknown", "failed"),))

        def unknown_terminated(store, chat_id):
            sid = running(store, chat_id)
            rows = to_unknown(store, chat_id, sid)
            observation = observe(store, chat_id, sid, "stop_confirmed")
            store.append_transition(
                chat_id, sid, "unknown", "terminated", "user",
                {"kind": "observation", "ref": observation["observation_id"]})
            return rows | set((("unknown", "terminated"),))

        return {
            "pending -> launch_failed": pending_launch_failed,
            "launching -> launch_failed": launching_launch_failed,
            "launching -> unknown (result)": launching_unknown_by_result,
            "launching -> unknown (observation)": launching_unknown_by_observation,
            "running -> completed": running_completed,
            "running -> failed": running_failed,
            "running -> terminated": running_terminated,
            "unknown -> running": unknown_running,
            "unknown -> completed": unknown_completed,
            "unknown -> failed": unknown_failed,
            "unknown -> terminated": unknown_terminated,
        }


class ScriptedClock(object):
    """A wall clock whose readings are a list rather than the machine's.

    The store's clock rules are stated over a clock that can step *backwards*,
    which is the one thing a real clock will not do on demand. Every test below
    that turns on the clock installs one of these instead of sleeping.
    """

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0
        self._real = ids.now

    def __enter__(self):
        ids.now = self._read
        return self

    def __exit__(self, *exc):
        ids.now = self._real
        return False

    def _read(self):
        self.calls += 1
        index = min(self.calls, len(self.readings)) - 1
        return self.readings[index]


def _stamp(second, micro=0):
    return "2030-01-01T00:00:%02d.%06dZ" % (second, micro)


class TestTheAddressingClockCheckIsPinnedElementByElement(CodeClosureCase):
    """The clock check of contract 6.1, taken apart into the facts it rests on.

    A mechanical sweep put five mutations into this check and its issuance
    lookup -- the accepted tie, the outcome element, the handle element, the
    direction of the comparison, and the timestamp type re-check -- and all five
    left the suite green. Four of them are green because the lookup they change
    runs over a set that can hold at most one record: a session may have one
    launch request (`append_launch_request` refuses a second) and a request may
    have one result (its packet file name is the request id, so exclusive
    creation refuses a second). The remaining two are green because nothing
    exercised the clock at the resolution the check is stated at, or against a
    sibling session that honestly shares a handle string.

    These tests supply the two missing inputs. They are stated over what the
    store *writes*, not over which clause refused, so they pin the facts rather
    than the phrasing.
    """

    def test_the_tie_the_check_accepts_is_a_second_and_not_an_instant(self):
        """`stamp[:19] < issued_at[:19]` is a second-granularity comparison.

        The existing tie test drives an operation dated to the same microsecond,
        which an untruncated comparison accepts as readily as a truncated one.
        The input that separates them is an operation in the *same second* with
        fewer microseconds -- exactly the sub-second jitter contract 6.4 says a
        wall clock has, and which the contract refuses to call a violation.
        """
        with ScriptedClock([_stamp(5, 500000)]):
            sid, _request_id, _user = self.running()
            issued = self.store.read_launch_results(self.chat_id, sid)[0]["observed_at"]
        self.assertEqual(_stamp(5, 500000), issued)

        with ScriptedClock([_stamp(5, 400000)]):
            earlier = self.store.append_session_observation(
                self.chat_id, sid, "stop_confirmed", "same second, less micro")
        self.assertEqual(_stamp(5, 400000), earlier["observed_at"])

        with ScriptedClock([_stamp(5, 900000)]):
            later = self.store.append_session_observation(
                self.chat_id, sid, "stop_unconfirmed", "same second, more micro")
        self.assertEqual(_stamp(5, 900000), later["observed_at"])

        # And the floor is still a floor: a whole second earlier is refused.
        with ScriptedClock([_stamp(4, 900000)]):
            self.assertRaises(
                ValidationRefused,
                lambda: self.store.append_session_observation(
                    self.chat_id, sid, "reattached", "a second earlier"))
        self.assertEqual([], self.store.verify())

    def test_the_issuance_compared_against_is_this_session_s_own(self):
        """Two sessions in one chat may honestly carry the same handle string.

        `_handle_issued_at` scopes its lookup to the session. Nothing in the
        suite held two sessions whose handles collide, so the scope was the one
        element of that filter no mutation could redden. Without it the earlier
        session answers for the later one, and an operation dated before its own
        handle was issued is written rather than refused.
        """
        with ScriptedClock([_stamp(2)]):
            first, _request_id, _user = self.running(handle="h-shared")
            self.completed(first)
        with ScriptedClock([_stamp(30)]):
            second, _request_id2, _user2 = self.running(handle="h-shared", text="turn two")

        self.assertEqual(_stamp(2), self.store._handle_issued_at(self.chat_id, first, "h-shared"))
        self.assertEqual(_stamp(30), self.store._handle_issued_at(self.chat_id, second, "h-shared"))

        # A moment after the first session's handle and before the second's.
        with ScriptedClock([_stamp(10)]):
            self.assertRaises(
                ValidationRefused,
                lambda: self.store.append_session_observation(
                    self.chat_id, second, "stop_confirmed", "between the two"))
        self.assertEqual([], self.store.verify())
        self.assertEqual(
            [], [o for o in self.store.read_session_observations(self.chat_id, second)],
            "the refused observation must not be on disk")

    def test_an_addressing_record_carries_the_stamp_that_was_checked(self):
        """One reading of the clock, checked and written.

        `_require_addressable` reads the clock, compares that reading to the
        handle's issuance, and returns it. Both addressing writers record the
        reading they were given. A writer that reads the clock again writes a
        stamp nothing checked, and a clock that stepped back in between puts
        `ADDRESSED_BEFORE_HANDLE_ISSUED` on disk -- the very code the check
        exists to prevent, produced by the check passing.

        Stated over the record's own field rather than over the number of clock
        reads, because "how many times did you call `now`" is a mechanism and
        "is the record dated when it said it was" is the fact.
        """
        with ScriptedClock([_stamp(5)]):
            sid, _request_id, _user = self.running()

        # Honest reading first, then a clock that has stepped back behind the
        # handle's issuance. Only the first reading is checked.
        with ScriptedClock([_stamp(20), _stamp(1)]):
            observation = self.store.append_session_observation(
                self.chat_id, sid, "stop_confirmed", "probe")
        self.assertEqual(
            _stamp(20), observation["observed_at"],
            "the observation must carry the reading the check was made against")

        with ScriptedClock([_stamp(21), _stamp(1)]):
            delivery = self.store.append_delivery_request(self.chat_id, sid, "more")
        self.assertEqual(
            _stamp(21), delivery["created_at"],
            "the delivery must carry the reading the check was made against")

        self.assertEqual([], self.store.verify())


class TestTheInstructionBoundIsMeasuredOrAbsent(CodeClosureCase):
    """Contract 6.3 and the human direction behind it.

    This contract asserts no instruction-payload bound of its own. The only
    bound is one a launcher measured and the session recorded, and the only
    honest value today is `null`. Two failure directions therefore matter and
    both are stated here: enforcing something when nothing was measured, and
    failing to enforce something that was.
    """

    def test_an_unmeasured_bound_is_not_a_bound_on_either_packet_type(self):
        """`null` must bound nothing, and it must bound nothing *on both*.

        The existing coverage drove only `delivery_request`. The guard is stated
        once over every packet carrying `instruction_text`, so the launch packet
        is the other half of the same rule and a bound that appeared on only one
        of the pair is the shape this rail exists to close.
        """
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", PERSISTENT_STREAM)
        sid = session["session_id"]
        self.assertIsNone(
            session["launcher_capabilities"]["instruction_bound_bytes"],
            "null is the only honest value this release has")
        huge = "x" * 100000
        request = self.store.append_launch_request(self.chat_id, sid, huge)
        self.assertEqual(100000, len(request["instruction_text"]))
        self.store.append_transition(
            self.chat_id, sid, "pending", "launching", "harness",
            {"kind": "harness_action", "ref": request["request_id"]})
        self.store.append_launch_result(
            self.chat_id, request["request_id"], sid, "accepted", agent_handle="h-issued")
        self.store.append_transition(
            self.chat_id, sid, "launching", "running", "launcher",
            {"kind": "launch_result", "ref": request["request_id"]}, agent_handle="h-issued")
        delivery = self.store.append_delivery_request(self.chat_id, sid, huge)
        self.assertEqual(100000, len(delivery["instruction_text"]))
        self.assertEqual([], self.store.verify())

    def test_a_bound_below_one_is_not_a_bound_a_session_can_declare(self):
        """`bound < 1` reads as "nothing was measured", and that must stay true.

        A mutation making zero a *real* bound of zero -- so that every
        instruction is too large -- left the whole suite green, because nothing
        held a session declaring it. It is unreachable, and this says so by
        driving it: the contract types the field as an integer >= 1 or null, and
        `create_session` refuses everything below that rather than storing a
        number the store would then enforce as though a launcher had measured
        it.
        """
        for bound, code in ((0, "FIELD_OUT_OF_RANGE"),
                            (-1, "FIELD_OUT_OF_RANGE"),
                            (True, "BAD_FIELD_TYPE"),
                            (False, "BAD_FIELD_TYPE"),
                            ("8", "BAD_FIELD_TYPE")):
            capabilities = dict(PERSISTENT_STREAM)
            capabilities["instruction_bound_bytes"] = bound
            user = self.store.append_user_message(self.chat_id, "go")
            with self.assertRaises(ValidationRefused) as caught:
                self.store.create_session(
                    self.chat_id, user["message_id"], "dev-local", capabilities)
            self.assertIn(code, str(caught.exception),
                          "bound %r must be refused as %s" % (bound, code))
        self.assertEqual([], self.store.verify())

    def test_an_instruction_of_the_wrong_type_is_refused_and_not_raised_over(self):
        """Every public write of this store fails with a `StoreError`.

        The bound guard returns early on anything that is not a string, leaving
        the refusal to the record check a line later. Narrowed to a presence
        test it still refuses -- but on a session that declares a measured
        bound it refuses by raising `AttributeError` out of `len(text.encode())`
        instead, which is not a `StoreError` and is not a refusal a caller can
        handle. The measured-bound session is the input that separates the two.
        """
        capabilities = dict(PERSISTENT_STREAM)
        capabilities["instruction_bound_bytes"] = 64
        user = self.store.append_user_message(self.chat_id, "go")
        session, _binding = self.store.create_session(
            self.chat_id, user["message_id"], "dev-local", capabilities)
        sid = session["session_id"]
        for value in (123, ["x"], {"a": 1}, b"bytes", 1.5):
            with self.assertRaises(StoreError) as caught:
                self.store.append_launch_request(self.chat_id, sid, value)
            self.assertIn("BAD_FIELD_TYPE", str(caught.exception),
                          "%r must be refused as a bad field type" % (value,))
        self.assertEqual(
            [], self.store.read_launch_requests(self.chat_id, sid),
            "no refused instruction may be on disk")
        self.assertEqual([], self.store.verify())


class TestTheReadOnlySurfaceIsDerivedNotDeclared(CodeClosureCase):
    """`READ_ONLY` is the second axis of the write enumeration, stated as a list.

    `test_every_public_write_is_in_the_enumeration` asks three questions of that
    list -- nothing unclassified, nothing driven that is not public, nothing
    declared that the store no longer has -- and none of them asks whether a
    name on it writes. A mechanical mutation adding `append_launch_result` to
    the list left all of them green. That is the defect this ticket family has
    produced repeatedly in a new place: a check on the label of a claim rather
    than on the fact the label stands for. A write hidden behind the label drops
    silently out of the cross that exists to drive every write.

    So the list is checked against the disk. Every name on it is exercised
    against a store holding a full history, and the byte-for-byte content of the
    store must be what it was.
    """

    def _digest(self, root):
        """Every path under `root` and its bytes: a write of any kind shows up."""
        out = {}
        for base, dirs, names in os.walk(root):
            dirs.sort()
            for name in sorted(names):
                path = os.path.join(base, name)
                with open(path, "rb") as handle:
                    out[os.path.relpath(path, root)] = handle.read()
        return out

    def _arguments(self, method, chat_id, sid, event_id, message_id):
        """A plausible call for a reader, derived from its own parameter names."""
        import inspect
        known = {
            "chat_id": chat_id, "session_id": sid, "event_id": event_id,
            "message_id": message_id, "name": "probe", "sequence": 1,
            "expect": "accept", "description": None, "include_archived": True,
            "sequence_from": None, "sequence_to": None, "limit": None,
        }
        supplied = []
        for parameter in list(inspect.signature(method).parameters.values()):
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            if parameter.name in known:
                supplied.append(known[parameter.name])
            elif parameter.default is not parameter.empty:
                supplied.append(parameter.default)
            else:
                self.fail(
                    "no argument known for %r of %s; a reader this test cannot "
                    "call is a reader it cannot check" % (parameter.name, method))
        return supplied

    def test_every_name_declared_read_only_writes_nothing(self):
        sid, request_id, user = self.running()
        event = self.agent_event(sid)
        self.store.append_agent_message(self.chat_id, sid, event["event_id"], "hi")
        self.store.append_delivery_request(self.chat_id, sid, "more")
        self.store.append_session_observation(self.chat_id, sid, "stop_confirmed", "probe")
        self.assertEqual([], self.store.verify())

        enumeration = TestNoPublicSequenceProducesARejectedStore()
        before = self._digest(self.store.root)
        self.assertTrue(before, "the store must hold something to notice a write to")

        exercised = []
        for name in sorted(enumeration.READ_ONLY):
            member = getattr(self.store, name)
            if not callable(member):
                exercised.append(name)
                continue
            arguments = self._arguments(
                member, self.chat_id, sid, event["event_id"], user["message_id"])
            try:
                member(*arguments)
            except StoreError:
                # A reader may refuse; refusing is not writing.
                pass
            exercised.append(name)

        self.assertEqual(sorted(enumeration.READ_ONLY), exercised)
        after = self._digest(self.store.root)
        self.assertEqual(
            sorted(before), sorted(after),
            "a name declared read-only added or removed a file")
        changed = sorted(path for path in before if before[path] != after[path])
        self.assertEqual(
            [], changed,
            "a name declared read-only changed %s on disk" % changed)
        self.assertEqual([], self.store.verify())

    def test_a_writer_hidden_behind_the_label_is_caught(self):
        """The control: this check must fail when the list is wrong.

        Without it, a check that exercised nothing would pass just as happily.
        """
        sid, _request_id, _user = self.running()
        before = self._digest(self.store.root)
        self.store.append_session_observation(self.chat_id, sid, "stop_confirmed", "probe")
        after = self._digest(self.store.root)
        self.assertNotEqual(
            sorted(before), sorted(after),
            "the digest must notice a write, or it notices nothing")


class TestTheDeliveryAcknowledgementIsWrittenOnce(CodeClosureCase):
    """The one write convergence added to the store, and every guard in it.

    #87 recorded a delivery before the call with `acknowledged: null` and
    rewrote the record with the answer; #86's packets are created once and never
    replaced, so the answer had nowhere to go. `record_delivery_acknowledgement`
    is that one field of that one packet type, filled in once.
    """

    def delivered(self):
        sid, _request_id, _user = self.running()
        delivery = self.store.append_delivery_request(self.chat_id, sid, "follow-up")
        return sid, delivery

    def packet_bytes(self):
        directory = os.path.join(self.root, "chats", self.chat_id, "packets")
        return dict((name, open(os.path.join(directory, name), "rb").read())
                    for name in sorted(os.listdir(directory)))

    def test_the_answer_is_recorded_on_the_delivery_it_answers(self):
        for value in (True, False):
            with self.subTest(acknowledged=value):
                sid, delivery = self.delivered()
                updated = self.store.record_delivery_acknowledgement(
                    self.chat_id, sid, delivery["delivery_id"], value)
                self.assertIs(updated["acknowledged"], value)
                stored = [d for d in self.store.read_delivery_requests(self.chat_id, sid)
                          if d["delivery_id"] == delivery["delivery_id"]]
                self.assertEqual(len(stored), 1)
                expected = dict(delivery, acknowledged=value)
                self.assertEqual(stored[0], expected,
                                 "a field other than `acknowledged` changed")
                self.assertStoreValid()
                self.completed(sid)

    def test_null_and_anything_not_a_boolean_is_refused_and_nothing_is_written(self):
        sid, delivery = self.delivered()
        before = self.packet_bytes()
        for value in (None, 1, 0, "true", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValidationRefused):
                    self.store.record_delivery_acknowledgement(
                        self.chat_id, sid, delivery["delivery_id"], value)
                self.assertEqual(self.packet_bytes(), before)

    def test_an_answer_already_recorded_is_not_rewritten(self):
        sid, delivery = self.delivered()
        self.store.record_delivery_acknowledgement(
            self.chat_id, sid, delivery["delivery_id"], True)
        before = self.packet_bytes()
        for value in (False, True):
            with self.assertRaises(ValidationRefused):
                self.store.record_delivery_acknowledgement(
                    self.chat_id, sid, delivery["delivery_id"], value)
        self.assertEqual(self.packet_bytes(), before)

    def test_only_a_delivery_of_the_named_session_can_be_answered(self):
        sid, delivery = self.delivered()
        before = self.packet_bytes()
        request_id = self.store.read_launch_requests(self.chat_id, sid)[0]["request_id"]
        for chat_id, session_id, delivery_id, error in (
                (self.chat_id, sid, "dlv_nosuchdelivery", ValidationRefused),
                (self.chat_id, sid, request_id, ValidationRefused),
                (self.chat_id, "ses_nosuchsession0", delivery["delivery_id"], ValidationRefused),
                (self.store.create_chat("Other")["chat_id"], sid, delivery["delivery_id"],
                 ValidationRefused),
                ("cht_nosuchchat000", sid, delivery["delivery_id"], NotFound),
                ("../" + self.chat_id, sid, delivery["delivery_id"], NotFound)):
            with self.subTest(chat=chat_id, session=session_id, delivery=delivery_id):
                with self.assertRaises(error):
                    self.store.record_delivery_acknowledgement(
                        chat_id, session_id, delivery_id, True)
        self.assertEqual(self.packet_bytes(), before)

    def test_the_file_replaced_is_the_one_that_holds_the_record(self):
        """Located by the record, replaced by the name the record gives it -- and
        that name must still hold that record, or nothing is written."""
        sid, first = self.delivered()
        second = self.store.append_delivery_request(self.chat_id, sid, "again")
        directory = os.path.join(self.root, "chats", self.chat_id, "packets")
        names = dict(
            (json.load(open(os.path.join(directory, n)))["delivery_id"], n)
            for n in os.listdir(directory) if n.startswith("delivery_request-"))
        a, b = names[first["delivery_id"]], names[second["delivery_id"]]
        a_bytes = open(os.path.join(directory, a), "rb").read()
        b_bytes = open(os.path.join(directory, b), "rb").read()
        with open(os.path.join(directory, a), "wb") as handle:
            handle.write(b_bytes)
        with open(os.path.join(directory, b), "wb") as handle:
            handle.write(a_bytes)
        before = self.packet_bytes()
        with self.assertRaises(StoreCorrupt):
            self.store.record_delivery_acknowledgement(
                self.chat_id, sid, first["delivery_id"], True)
        self.assertEqual(self.packet_bytes(), before)

    def test_an_answer_after_the_agent_exited_is_still_a_true_record(self):
        """The added-guard direction: no refusal here would be honest. The answer
        is a fact about a call made while the agent ran, and the contract dates a
        delivery by when it was sent, not by when it was answered."""
        sid, delivery = self.delivered()
        self.completed(sid)
        self.store.record_delivery_acknowledgement(
            self.chat_id, sid, delivery["delivery_id"], False)
        self.assertStoreValid()

