"""The shell: conversation list, active conversation view, new-chat flow, send.

Driven over HTTP against the real server process, because the claim is about the
product a user touches rather than about a Python object.
"""

import json
import os
import shutil
import tempfile
import unittest
from urllib.error import HTTPError

from helpers import answered_turn
from shellproc import ShellProcess

from dory_wrangler.store import ChatStore


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dory-shell-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shell = ShellProcess(self.root).start()
        self.addCleanup(self.shell.kill)


class TestShellFlows(ShellCase):
    def test_the_page_serves_with_no_external_asset(self):
        status, page = self.shell.get_page()
        self.assertEqual(status, 200)
        for scheme in ("http://", "https://", "//cdn", "cdnjs", "unpkg", "jsdelivr"):
            self.assertNotIn(
                scheme, page,
                "the shell page reaches outside itself for %r; it has to render on a "
                "network with no internet" % scheme,
            )
        self.assertIn("New chat", page)

    def test_new_chat_flow_then_list_then_open(self):
        status, first = self.shell.post("/api/chats", {})
        self.assertEqual(status, 201)
        _status, second = self.shell.post("/api/chats", {"title": "Second"})

        _status, listed = self.shell.get("/api/chats")
        self.assertEqual(len(listed), 2)
        self.assertIn("Second", [c["title"] for c in listed])

        _status, opened = self.shell.get("/api/chats/" + first["chat_id"])
        self.assertEqual(opened["chat_id"], first["chat_id"])
        self.assertEqual(opened["messages"], [])
        self.assertEqual(second["messages"], [])

    def test_send_renders_the_durable_ordered_history(self):
        _status, chat = self.shell.post("/api/chats", {})
        chat_id = chat["chat_id"]
        for i in range(4):
            _status, body = self.shell.post(
                "/api/chats/%s/messages" % chat_id, {"text": "turn %d" % i}
            )
        self.assertEqual([m["sequence"] for m in body["messages"]], [1, 2, 3, 4])
        self.assertEqual(
            [m["text"] for m in body["messages"]], ["turn %d" % i for i in range(4)]
        )
        # What the shell returned is what is on disk, not what it remembered.
        on_disk = ChatStore(self.root).read_messages(chat_id)
        self.assertEqual(
            [m["content"]["text"] for m in on_disk],
            [m["text"] for m in body["messages"]],
        )

    def test_a_new_chat_takes_its_name_from_the_first_turn(self):
        _status, chat = self.shell.post("/api/chats", {})
        _status, body = self.shell.post(
            "/api/chats/%s/messages" % chat["chat_id"],
            {"text": "Explain the launch seam to me"},
        )
        self.assertEqual(body["title"], "Explain the launch seam to me")

    def test_an_empty_turn_is_refused(self):
        _status, chat = self.shell.post("/api/chats", {})
        self.assertEqual(
            self.shell.status_of("POST", "/api/chats/%s/messages" % chat["chat_id"],
                                 {"text": "   "}),
            400,
        )

    def test_an_unknown_chat_is_not_found(self):
        self.assertEqual(self.shell.status_of("GET", "/api/chats/cht_nothinghere0"), 404)


class TestShellBoundaries(ShellCase):
    """The shell presents a single-assistant conversation and nothing else."""

    def test_there_is_no_diagnostic_lifecycle_or_worker_surface(self):
        store = ChatStore(self.root)
        chat_id = store.create_chat("Bounded")["chat_id"]
        session, event, _message = answered_turn(store, chat_id, "q", "the answer")

        for path in (
            "/api/diagnostics",
            "/api/chats/%s/diagnostics" % chat_id,
            "/api/chats/%s/events" % chat_id,
            "/api/chats/%s/sessions" % chat_id,
            "/api/sessions",
            "/api/sessions/%s" % session["session_id"],
            "/api/chats/%s/bindings" % chat_id,
        ):
            self.assertEqual(
                self.shell.status_of("GET", path), 404,
                "%s is reachable; contract P4 keeps diagnostics off the chat surface "
                "and #82 owns any view of worker internals" % path,
            )

    def test_the_transcript_payload_carries_no_worker_internals(self):
        store = ChatStore(self.root)
        chat_id = store.create_chat("Bounded")["chat_id"]
        session, event, _message = answered_turn(store, chat_id, "q", "the answer")

        _status, opened = self.shell.get("/api/chats/" + chat_id)
        blob = json.dumps(opened)
        for leak in (session["session_id"], event["event_id"], "diagnostic",
                     "transition", "binding", "launcher", "running", "completed"):
            self.assertNotIn(
                leak, blob,
                "the active conversation payload exposes %r; the shell shows one "
                "assistant conversation" % leak,
            )
        self.assertEqual([m["author"] for m in opened["messages"]], ["user", "agent"])
        self.assertEqual(opened["messages"][1]["text"], "the answer")

        _status, listed = self.shell.get("/api/chats")
        list_blob = json.dumps(listed)
        self.assertNotIn(session["session_id"], list_blob)
        self.assertNotIn(event["event_id"], list_blob)

    def test_the_shell_starts_no_process(self):
        """#86 launches nothing. #87 owns the launch boundary."""
        from shellproc import children_of

        _status, chat = self.shell.post("/api/chats", {})
        self.shell.post("/api/chats/%s/messages" % chat["chat_id"], {"text": "hello"})
        self.assertEqual(
            children_of(self.shell.process.pid), [],
            "the shell started a child process; agent launching is #87's",
        )


class TestShellRejectsBadAddressing(ShellCase):
    def test_identifiers_from_the_url_cannot_escape_the_store(self):
        """Chat ids are opaque and checked against the contract's shape first."""
        for hostile in (
            "../../etc",
            "..%2f..%2fetc",
            "cht_ok/../../../etc",
            ".",
            "",
            "cht_UPPERCASE1234",
            "cht_short",
        ):
            status = self.shell.status_of("GET", "/api/chats/" + hostile)
            self.assertIn(
                status, (404,),
                "GET /api/chats/%r answered %s" % (hostile, status),
            )
        self.assertTrue(os.path.isdir(os.path.join(self.root, "chats")))


class TestShellFailsClosed(ShellCase):
    def test_a_corrupt_history_is_refused_rather_than_partially_rendered(self):
        store = ChatStore(self.root)
        chat_id = store.create_chat("Broken")["chat_id"]
        for i in range(3):
            store.append_user_message(chat_id, "turn %d" % i)
        os.unlink(os.path.join(self.root, "chats", chat_id, "messages", "00000002.json"))

        try:
            status, body = self.shell.get("/api/chats/" + chat_id)
        except HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode("utf-8"))
        self.assertEqual(status, 409)
        self.assertIn("sequence jumps", body["error"])
        # It refused; it did not return the two messages it could still read.
        self.assertNotIn("turn 0", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
