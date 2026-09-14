"""The shell: conversation list, active conversation view, new-chat flow, send.

Driven over HTTP against the real server process, because the claim is about the
product a user touches rather than about a Python object.
"""

import ast
import contextlib
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

    @contextlib.contextmanager
    def stopped_shell_store(self):
        """Write into the store while no shell serves it, then serve it again.

        Decision 0002, D1: one serving process per store. These tests used to
        write records behind a live shell's back from the test process, which is
        a second writer on a served store; they now stop the shell, write, give
        the store back, and restart it, so the shell serves what is on disk.
        """
        self.shell.kill()
        store = ChatStore(self.root)
        try:
            yield store
        finally:
            store.close()
            self.shell.start()


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
        # Each turn is answered by the agent through the chat loop before the
        # send returns, so the history the send renders holds both sides.
        self.assertEqual([m["sequence"] for m in body["messages"]], list(range(1, 9)))
        expected = []
        for i in range(4):
            expected += ["turn %d" % i, "answer to: turn %d" % i]
        self.assertEqual([m["text"] for m in body["messages"]], expected)
        self.assertEqual([m["author"] for m in body["messages"]], ["user", "agent"] * 4)
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

    # The routes this release is allowed to answer, as the URL fragments the
    # handler dispatches on. This is a closed world: the assertion below is
    # equality, so a route cannot be added without changing this list, and
    # changing this list is the visible act of widening the release boundary.
    # `/abandon` is that act, made by #88's convergence (decision 0003): the one
    # lifecycle action a user needs when a restart leaves an agent nobody can
    # reach, and nothing else.
    INTENDED_ROUTE_FRAGMENTS = {
        "/", "/index.html", "/healthz", "/api/chats", "/api/chats/", "/messages",
        "/abandon",
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
        with self.stopped_shell_store() as store:
            chat_id = store.create_chat("Bounded")["chat_id"]
            session, event, _message = answered_turn(store, chat_id, "q", "the answer")

        for method, path in (
            ("GET", "/"), ("GET", "/index.html"), ("GET", "/healthz"),
            ("GET", "/api/chats"), ("GET", "/api/chats/%s" % chat_id),
            ("POST", "/api/chats/%s/abandon" % chat_id),
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
        with self.stopped_shell_store() as store:
            chat_id = store.create_chat("Bounded")["chat_id"]
            session, event, _message = answered_turn(store, chat_id, "q", "the answer")
        store = ChatStore(self.root, read_only=True)
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
        # The abandon route's own answers: a refusal (this chat's agent finished)
        # and a success, each composed by the shell and each held to the boundary.
        status, refused = self.shell.raw("POST", "/api/chats/%s/abandon" % chat_id, {})
        self.assertEqual(status, 409)
        served.append(("POST /api/chats/<id>/abandon (refused)", refused))
        live = ShellProcess(tempfile.mkdtemp(prefix="dory-shell-unknown-"),
                            launcher_options={"launch_outcomes": ["unknown"]})
        self.addCleanup(shutil.rmtree, live.root, True)
        self.addCleanup(live.kill)
        live.start()
        _status, stuck = live.post("/api/chats", {})
        status, busy = live.raw("POST", "/api/chats/%s/messages" % stuck["chat_id"],
                                {"text": "hello"})
        self.assertEqual(status, 201)
        status, busy = live.raw("POST", "/api/chats/%s/messages" % stuck["chat_id"],
                                {"text": "again"})
        self.assertEqual(status, 409)
        served.append(("POST /api/chats/<id>/messages (refused)", busy))
        status, abandoned = live.raw("POST", "/api/chats/%s/abandon" % stuck["chat_id"], {})
        self.assertEqual(status, 200)
        served.append(("POST /api/chats/<id>/abandon", abandoned))
        unknown_store = ChatStore(live.root, read_only=True)
        unknown_session = unknown_store.list_sessions(stuck["chat_id"])[0][0]
        secrets_elsewhere = [unknown_session["session_id"], "abandoned", "unknown"]
        for where, body in served[-3:]:
            for secret in secrets_elsewhere:
                self.assertNotIn(secret, body,
                                 "%s served the worker-internal value %r" % (where, secret))

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
        with self.stopped_shell_store() as store:
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
        """The shell launches nothing of its own: a turn reaches an agent only
        through the configured launcher, and the one these tests configure is
        in-process. The development launcher's real processes are #87's tests'."""
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
        with self.stopped_shell_store() as store:
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
        # Review finding R3: the body is the shell's fixed words, not the
        # store's refusal text (which named the gap, "sequence jumps").
        self.assertEqual(body, {"error": webapp.UNREADABLE})
        # It refused; it did not return the two messages it could still read.
        self.assertNotIn("turn 0", json.dumps(body))



class TestEveryErrorBodyIsFixedWords(unittest.TestCase):
    """Review finding R3, over the bodies actually served, on every error path.

    Since convergence every session, packet, event and transition write runs
    inside a request, so every refusal those writes make can reach a handler.
    This drives one instance of each error a route can answer -- including a
    store refusal through each POST route, a launcher misuse, and an exception no
    route anticipated -- against a served application whose store really holds
    sessions, handles, launcher ids and preserved raw output, and requires of
    every error body that

    * its `error` is one of the shell's own fixed sentences (`webapp.ERROR_WORDS`);
    * it carries no value the stores hold: every id, handle, launcher id and raw
      body is read back from the stores after the run, not listed in advance.
    """

    PERSISTENT = {"continuation": "persistent", "response_shape": "stream",
                  "end_of_turn": "turn_complete"}

    def serve(self, launcher):
        import threading
        root = tempfile.mkdtemp(prefix="dory-errors-")
        self.addCleanup(shutil.rmtree, root, True)
        server = webapp.build_server(root, port=0, quiet=True, launcher=launcher)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.daemon = True
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.roots.append(root)
        return server.server_address[1], server

    def call(self, port, method, path, payload=None, raw=None, headers=None):
        from urllib.request import Request, urlopen
        data = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None)
        request = Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                          headers=headers or ({"Content-Type": "application/json"}
                                              if data else {}))
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def error(self, where, expected_status, port, method, path, **kwargs):
        status, body = self.call(port, method, path, **kwargs)
        self.assertEqual(status, expected_status, "%s answered %s: %s" % (where, status, body))
        self.served.append((where, body))
        return body

    def chat(self, port, first_turn=None):
        _status, body = self.call(port, "POST", "/api/chats", {})
        chat_id = json.loads(body)["chat_id"]
        if first_turn is not None:
            status, body = self.call(port, "POST", "/api/chats/%s/messages" % chat_id,
                                     {"text": first_turn})
            self.assertEqual(status, 201, body)
        return chat_id

    def test_every_error_body_is_fixed_words_carrying_nothing_the_store_holds(self):
        from dory_wrangler import ids, launch_boundary as lb
        from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
        self.roots, self.served = [], []

        # -- one store holding a live persistent agent, preserved output, a
        #    finished session: the values that must not leak exist ------------
        port, server = self.serve(ScriptedStubLauncher(dict(self.PERSISTENT,
                                                            instruction_bound_bytes=64)))
        live = self.chat(port, "one")
        messages = "/api/chats/%s/messages" % live

        self.error("GET unknown route", 404, port, "GET", "/api/sessions")
        self.error("GET nested route", 404, port, "GET", "/api/chats/%s/x" % live)
        self.error("GET bad chat id", 404, port, "GET", "/api/chats/not-an-id")
        self.error("GET unknown chat", 404, port, "GET", "/api/chats/cht_nothinghere000")
        self.error("POST unknown route", 404, port, "POST", "/api/sessions", payload={})
        self.error("POST not JSON", 400, port, "POST", messages, raw=b"{nope",
                   headers={"Content-Type": "application/json"})
        self.error("POST not an object", 400, port, "POST", messages, raw=b"[1]",
                   headers={"Content-Type": "application/json"})
        self.error("POST bad length", 400, port, "POST", messages, raw=b"{}",
                   headers={"Content-Type": "application/json", "Content-Length": "x"})
        self.error("POST too large", 400, port, "POST", messages, raw=b"{}",
                   headers={"Content-Type": "application/json",
                            "Content-Length": str(webapp.MAX_BODY_BYTES + 1)})
        self.error("POST no text", 400, port, "POST", messages, payload={"text": "  "})
        self.error("POST messages, unknown chat", 404, port, "POST",
                   "/api/chats/cht_nothinghere000/messages", payload={"text": "hi"})
        self.error("POST abandon, unknown chat", 404, port, "POST",
                   "/api/chats/cht_nothinghere000/abandon", payload={})
        self.error("POST chats, title the store refuses", 409, port, "POST", "/api/chats",
                   payload={"title": "t" * 500})
        self.error("POST messages, over the declared bound", 409, port, "POST", messages,
                   payload={"text": "x" * 200})
        # A store refusal through the send route: the wall clock stepped back
        # past the second the handle was issued in.
        real_now = ids.now
        ids.now = lambda: "2000-01-01T00:00:00.000000Z"
        try:
            self.error("POST messages, store refuses (clock)", 409, port, "POST", messages,
                       payload={"text": "two"})
        finally:
            ids.now = real_now
        self.error("POST messages, lone surrogate", 409, port, "POST", messages,
                   raw=b'{"text": "two \\ud800"}', headers={"Content-Type": "application/json"})
        finished = self.chat(port)
        self.error("POST abandon, nothing to abandon", 409, port, "POST",
                   "/api/chats/%s/abandon" % finished, payload={})
        # An exception no route anticipated, carrying worker internals in its text.
        session = server.service.store.list_sessions(live)[0][0]
        real_open = server.service.open_chat

        def explodes(chat_id):
            raise RuntimeError("%s %s" % (session["session_id"], session["agent_handle"]))

        server.service.open_chat = explodes
        try:
            self.error("GET unanticipated exception", 500, port, "GET", "/api/chats/" + live)
            self.error("POST unanticipated exception", 500, port, "POST", "/api/chats",
                       payload={})
        finally:
            server.service.open_chat = real_open
        # A damaged chat record, through GET and through both POST routes.
        damaged = self.chat(port, "doomed")
        with open(os.path.join(self.roots[-1], "chats", damaged, "chat.json"), "w") as handle:
            handle.write('{"session": "%s"' % session["session_id"])
        self.error("GET damaged chat", 409, port, "GET", "/api/chats/" + damaged)
        self.error("POST messages, damaged chat", 409, port, "POST",
                   "/api/chats/%s/messages" % damaged, payload={"text": "hi"})
        self.error("POST abandon, damaged chat", 409, port, "POST",
                   "/api/chats/%s/abandon" % damaged, payload={})

        # -- a launcher that contradicts what is stored: StoreCorrupt mid-turn --
        class Contradicts(ScriptedStubLauncher):
            def events(self, agent_handle, after_sequence):
                page = ScriptedStubLauncher.events(self, agent_handle, after_sequence)
                if after_sequence == 0:
                    return page
                forged = lb.EventPayload(1, "agent", "unrecognized",
                                         b"raw bytes that contradict sequence one")
                return lb.EventsPage([forged] + page.payloads)

        port, _server = self.serve(Contradicts(self.PERSISTENT))
        chat_id = self.chat(port, "one")
        self.error("POST messages, store refuses (contradicting replay)", 409, port, "POST",
                   "/api/chats/%s/messages" % chat_id, payload={"text": "two"})

        # -- a launcher that misuses the seam: 502; then the busy refusal ------
        port, _server = self.serve(ScriptedStubLauncher(
            {"continuation": "fresh_binding", "response_shape": "one_shot",
             "end_of_turn": "stream_end"}))
        chat_id = self.chat(port)
        self.error("POST messages, launcher misuse", 502, port, "POST",
                   "/api/chats/%s/messages" % chat_id, payload={"text": "hello"})
        port, _server = self.serve(ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
        chat_id = self.chat(port, "hello")
        self.error("POST messages, busy", 409, port, "POST",
                   "/api/chats/%s/messages" % chat_id, payload={"text": "again"})

        # -- what the stores hold, read back after the run ---------------------
        secrets = set()
        for root in self.roots:
            store = ChatStore(root, read_only=True)
            for chat_dir in sorted(os.listdir(store.chats_dir)):
                try:
                    store.read_chat(chat_dir)
                except Exception:  # noqa: BLE001 - the damaged chat; its sessions still count
                    pass
                for pair in store.list_sessions(chat_dir):
                    for record in pair:
                        secrets.update(v for k, v in record.items()
                                       if isinstance(v, str) and (k.endswith("_id") or k in (
                                           "agent_handle", "launcher_id")))
                    for event in store.read_all_events_of_session(
                            chat_dir, pair[0]["session_id"]):
                        secrets.update((event["event_id"], event["raw"]["body"]))
                for kind in ("launch_request", "launch_result", "delivery_request",
                             "session_observation"):
                    for record in store._read_packets(chat_dir, kind):
                        secrets.update(v for k, v in record.items()
                                       if isinstance(v, str) and k.endswith("_id"))
                for message in store.read_messages(chat_dir):
                    if message["session_id"]:
                        secrets.update((message["message_id"], message["session_id"],
                                        message["source_event_id"]))
        chat_ids = set(n for root in self.roots for n in os.listdir(os.path.join(root, "chats")))
        self.assertTrue(any(s.startswith("ses_") for s in secrets))
        self.assertTrue(any(s.startswith("evt_") for s in secrets))
        self.assertIn("scripted-stub", secrets)
        self.assertTrue(any(s.startswith("stub-agent-") for s in secrets))
        self.assertEqual(len(set(where for where, _ in self.served)), 25)
        for where, body in self.served:
            decoded = json.loads(body)
            self.assertIn(decoded.get("error"), webapp.ERROR_WORDS, where)
            self.assertLessEqual(set(decoded), {"error", "refused"}, where)
            for secret in secrets - chat_ids:
                self.assertNotIn(secret, body, "%s served %r" % (where, secret))
            for word in ("session", "handle", "sequence", "launch", "transition",
                         "unknown", "running", "evidence", "StoreCorrupt", "Traceback"):
                self.assertNotIn(word, body, "%s carries %r" % (where, word))


if __name__ == "__main__":
    unittest.main()
