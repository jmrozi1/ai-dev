"""The shell: conversation list, active conversation view, new-chat flow, send.

Driven over HTTP against the real server process, because the claim is about the
product a user touches rather than about a Python object.
"""

import ast
import inspect
import json
import os
import shutil
import tempfile
import unittest
from urllib.error import HTTPError

from helpers import answered_turn
from shellproc import ShellProcess

from dory_wrangler import webapp
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

    # The six routes this release is allowed to answer, as the URL fragments the
    # handler dispatches on. This is a closed world: the assertion below is
    # equality, so a seventh route cannot be added without changing this list,
    # and changing this list is the visible act of widening the release boundary.
    INTENDED_ROUTE_FRAGMENTS = {
        "/", "/index.html", "/healthz", "/api/chats", "/api/chats/", "/messages",
    }
    INTENDED_METHODS = {"do_GET", "do_POST"}

    def test_there_is_no_diagnostic_lifecycle_or_worker_surface(self):
        """The boundary is asserted over the dispatch, not over a list of bad paths.

        This test used to check that seven named paths answered 404. A denylist
        cannot hold this line and did not: review added an eighth route,
        `/api/worker/<chat_id>`, returning raw preserved agent output straight to
        the browser, and all 106 tests stayed green. Seven doors were shut; the
        wall had a new one.

        So enumerate the handler instead. `BaseHTTPRequestHandler` dispatches a
        request to `do_<METHOD>` on the handler class and nowhere else, so the
        string fragments those methods compare against *are* the surface.
        """
        source = inspect.getsource(webapp)
        tree = ast.parse(source)
        handlers = [node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "ShellHandler"]
        self.assertEqual(
            len(handlers), 1,
            "the request handler this test enumerates is no longer a single class "
            "named ShellHandler; the boundary must be enumerated over whatever "
            "replaced it rather than silently over nothing",
        )
        handler = handlers[0]

        methods = set()
        fragments = set()
        for node in ast.walk(handler):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("do_"):
                methods.add(node.name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith("/"):
                fragments.add(node.value)

        self.assertEqual(
            methods, self.INTENDED_METHODS,
            "the shell answers HTTP methods this release did not intend: %s"
            % sorted(methods ^ self.INTENDED_METHODS),
        )
        self.assertEqual(
            fragments, self.INTENDED_ROUTE_FRAGMENTS,
            "the shell's URL surface changed: %s. Contract P4 keeps diagnostics "
            "off the chat surface and #82 owns any view of worker internals, so a "
            "new route is a release-boundary change and has to be made here "
            "deliberately." % sorted(fragments ^ self.INTENDED_ROUTE_FRAGMENTS),
        )

        # The inventory is of the code; this is the same claim made of the
        # running process. Every intended route answers, and a sample of the
        # worker-internal shapes somebody would reach for does not.
        store = ChatStore(self.root)
        chat_id = store.create_chat("Bounded")["chat_id"]
        session, event, _message = answered_turn(store, chat_id, "q", "the answer")

        for method, path in (
            ("GET", "/"), ("GET", "/index.html"), ("GET", "/healthz"),
            ("GET", "/api/chats"), ("GET", "/api/chats/%s" % chat_id),
        ):
            status, _body = self.shell.raw(method, path)
            self.assertNotEqual(
                status, 404,
                "%s %s is in the inventory but the shell does not serve it"
                % (method, path),
            )
        for path in (
            "/api/diagnostics",
            "/api/chats/%s/diagnostics" % chat_id,
            "/api/chats/%s/events" % chat_id,
            "/api/chats/%s/sessions" % chat_id,
            "/api/sessions",
            "/api/sessions/%s" % session["session_id"],
            "/api/chats/%s/bindings" % chat_id,
            "/api/worker/%s" % chat_id,
            "/api/chats/%s/events/%s" % (chat_id, event["event_id"]),
        ):
            status, _body = self.shell.raw("GET", path)
            self.assertEqual(
                status, 404,
                "%s is reachable; contract P4 keeps diagnostics off the chat surface "
                "and #82 owns any view of worker internals" % path,
            )

    def test_nothing_any_route_serves_carries_worker_internals(self):
        """What is served, route by route, rather than which routes are shut.

        A route is only half the boundary. This drives every route in the
        inventory against a store that really holds a session, a binding, a
        launch packet and preserved agent output, and requires that none of
        those reach the browser through any of them.
        """
        store = ChatStore(self.root)
        chat_id = store.create_chat("Bounded")["chat_id"]
        session, event, _message = answered_turn(store, chat_id, "q", "the answer")
        preserved = store.read_diagnostic_events(
            chat_id, session_id=session["session_id"]
        )
        self.assertTrue(preserved, "the fixture has no preserved output to leak")

        served = []
        served.append(("GET /", self.shell.get_page()[1]))
        served.append(("GET /index.html", self.shell.raw("GET", "/index.html")[1]))
        served.append(("GET /healthz", json.dumps(self.shell.get("/healthz")[1])))
        served.append(("GET /api/chats", json.dumps(self.shell.get("/api/chats")[1])))
        served.append(("GET /api/chats/<id>",
                       json.dumps(self.shell.get("/api/chats/" + chat_id)[1])))
        _status, created = self.shell.post("/api/chats", {"title": "Another"})
        served.append(("POST /api/chats", json.dumps(created)))
        served.append(("POST /api/chats/<id>/messages", json.dumps(
            self.shell.post("/api/chats/%s/messages" % chat_id, {"text": "hello"})[1])))

        # Concrete worker-internal values, not words that might mean something
        # else: these exist only inside the store and must not appear anywhere.
        secrets = [session["session_id"], event["event_id"], session["agent_handle"],
                   session["launcher_id"], preserved[0]["raw"]["body"]]
        for where, body in served:
            for secret in secrets:
                self.assertNotIn(
                    secret, body,
                    "%s served the worker-internal value %r" % (where, secret),
                )

        # And the vocabulary, over the JSON surface only: the HTML page is a
        # document with its own words (`addEventListener`), while every JSON
        # response is data this shell composed and can be held to the boundary.
        for where, body in served:
            if where == "GET /":
                continue
            for leak in ("session", "binding", "transition", "launch", "launcher",
                         "diagnostic", "observation", "agent_handle", "worker",
                         "record_type", "packet"):
                self.assertNotIn(
                    leak, body,
                    "%s carries %r; the chat surface exposes chat metadata and "
                    "message text and nothing else" % (where, leak),
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
