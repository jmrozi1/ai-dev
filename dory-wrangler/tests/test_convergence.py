"""#88's convergence, asserted against the tree and against the running product.

Four claims, each stated over a fact rather than a narrative:

1. exactly one store implementation and one served application remain, and no
   record has two writers -- read off the product's own sources;
2. the concurrent-turn policy is one point, its default refuses before
   recording, and the alternative is contract-valid;
3. one serving process per store (decision 0002, D1): a second process is
   refused before it sweeps, re-attaches or writes, and changes nothing;
4. every guard convergence *added* to the chat loop has an input it refuses and
   a legal exit for that input -- and the one exit convergence did not provide
   (a `running` session the shell cannot drain) is pinned as measured, so it
   cannot change silently in either direction (decision 0003, section 3).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest

import support
from support import StoreCheck

from dory_wrangler import ids
from dory_wrangler import launch_boundary as lb
from dory_wrangler.errors import (
    ConcurrentLaunchRefused,
    NotPermitted,
    ValidationRefused,
)
from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
from dory_wrangler.session_manager import SessionManager
from dory_wrangler.store import ChatStore

PRODUCT = support.DORY
SOURCES_ROOTS = (os.path.join(PRODUCT, "src"),)
TOP_LEVEL_PROGRAMS = ("run_shell.py", "validate_store.py")


def product_sources():
    """Every Python source of the product: the package and its two programs.

    The contract, its fixtures and its validator are #85's and read-only here;
    the tests are not the product.
    """
    paths = []
    for root in SOURCES_ROOTS:
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    paths.append(os.path.join(base, name))
    for name in TOP_LEVEL_PROGRAMS:
        paths.append(os.path.join(PRODUCT, name))
    return paths


def relative(path):
    return os.path.relpath(path, PRODUCT)


def parse(path):
    with open(path) as handle:
        return ast.parse(handle.read(), filename=path)


class ExactlyOneStoreAndOneServedApplication(unittest.TestCase):
    """The first acceptance criterion of #88, read off the tree."""

    def test_the_harness_tree_and_its_store_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(PRODUCT, "harness")))
        for gone in ("harness_store.py", "identity.py", "app.py"):
            self.assertFalse(
                os.path.exists(os.path.join(PRODUCT, "src", "dory_wrangler", gone)), gone)

    def test_exactly_one_class_is_a_store(self):
        """A store is anything that publishes a record: a class whose methods
        call the atomic publishers. Exactly one such class exists."""
        publishers = {"create_exclusive", "replace", "create_tree_exclusive"}
        found = []
        for path in product_sources():
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.ClassDef):
                    continue
                calls = set(
                    inner.func.attr for inner in ast.walk(node)
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute))
                if calls & publishers:
                    found.append((relative(path), node.name))
        self.assertEqual(found, [("src/dory_wrangler/store.py", "ChatStore")])

    def test_only_the_store_composes_a_record(self):
        """Every durable record carries `record_type`. A dict literal with that
        key is a record being made; outside the store there is none, so nothing
        else can write one however it reaches the disk."""
        found = []
        for path in product_sources():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Dict) and any(
                        isinstance(k, ast.Constant) and k.value == "record_type"
                        for k in node.keys):
                    found.append(relative(path))
        self.assertEqual(sorted(set(found)), ["src/dory_wrangler/store.py"])

    # Every filesystem write in the product outside the store and its atomic
    # primitives, with what it writes. Equality, so a new one fails here.
    WRITES_OUTSIDE_THE_STORE = {
        ("src/dory_wrangler/serve.py", "open"): "the --port-file the tests read",
        ("src/dory_wrangler/session_manager.py", "os.open"): "the chat's turn-lock file",
        ("validate_store.py", "open"): "the --snapshot export, a fixture document",
    }

    def test_no_record_has_a_second_writer(self):
        writers = {"open", "os.open", "os.replace", "os.rename", "os.link", "os.unlink",
                   "os.remove", "os.makedirs", "os.mkdir", "shutil.copy", "shutil.move",
                   "shutil.rmtree", "shutil.copytree"}
        found = set()
        for path in product_sources():
            name = relative(path)
            if name in ("src/dory_wrangler/store.py", "src/dory_wrangler/atomic.py"):
                continue
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    called = func.id
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    called = "%s.%s" % (func.value.id, func.attr)
                else:
                    continue
                if called not in writers:
                    continue
                if called == "open":
                    mode = node.args[1] if len(node.args) > 1 else None
                    for keyword in node.keywords:
                        if keyword.arg == "mode":
                            mode = keyword.value
                    if not (isinstance(mode, ast.Constant) and set(mode.value) & set("wax+")):
                        continue
                found.add((name, called))
        self.assertEqual(found, set(self.WRITES_OUTSIDE_THE_STORE))

    def test_exactly_one_served_application(self):
        """One HTTP server class, one handler class, one place that serves."""
        servers, handlers, serving = [], [], []
        for path in product_sources():
            tree = parse(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [getattr(b, "id", getattr(b, "attr", None)) for b in node.bases]
                    if any(b and b.endswith("HTTPServer") for b in bases):
                        servers.append((relative(path), node.name))
                    if any(b and b.endswith("RequestHandler") for b in bases):
                        handlers.append((relative(path), node.name))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "serve_forever"):
                    serving.append(relative(path))
        self.assertEqual(servers, [("src/dory_wrangler/webapp.py", "ShellServer")])
        self.assertEqual(handlers, [("src/dory_wrangler/webapp.py", "ShellHandler")])
        self.assertEqual(serving, ["src/dory_wrangler/serve.py"])

    def test_the_served_application_is_the_chat_loop_over_the_store(self):
        """Not merely one server: the one server's turns go through the one loop
        over the one store."""
        root = support.scratch_root()
        from dory_wrangler.webapp import build_server
        server = build_server(root, port=0, quiet=True,
                              launcher_config={"launcher": "scripted-stub"})
        try:
            self.assertIsInstance(server.service.sessions, SessionManager)
            self.assertIsInstance(server.service.sessions.store, ChatStore)
            self.assertIs(server.service.store, server.service.sessions.store)
        finally:
            server.server_close()


class TheConcurrentTurnPolicyIsOnePoint(unittest.TestCase, StoreCheck):
    """Decision 0003: the default refuses before recording, and the flip is one
    method whose result the contract accepts."""

    def unknown_chat(self, manager_class=SessionManager):
        root = support.scratch_root()
        harness = manager_class(ChatStore(root),
                                ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
        chat_id = harness.create_chat("A chat already held")
        harness.send_turn(chat_id, "first")
        return harness, chat_id

    def test_the_default_records_nothing_for_a_refused_turn(self):
        harness, chat_id = self.unknown_chat()
        before = harness.store.export_records()
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "second")
        self.assertEqual(harness.store.export_records(), before)

    def test_the_policy_is_decided_in_one_method(self):
        """Every refusal for concurrency is raised from `_refuse_concurrent_turn`:
        no other method of the loop raises `ConcurrentLaunchRefused`."""
        import inspect
        from dory_wrangler import session_manager
        tree = ast.parse(inspect.getsource(session_manager))
        raisers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Raise) and isinstance(inner.exc, ast.Call)
                            and getattr(inner.exc.func, "id", None)
                            == "ConcurrentLaunchRefused"):
                        raisers.add(node.name)
        self.assertEqual(raisers, {"_refuse_concurrent_turn"})

    def test_the_flipped_policy_is_contract_valid(self):
        class RecordsThenRefuses(SessionManager):
            def _refuse_concurrent_turn(self, chat_id, session, text):
                self._store.append_user_message(chat_id, text)
                raise ConcurrentLaunchRefused(self._refusal_reason(session))

        harness, chat_id = self.unknown_chat(RecordsThenRefuses)
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "second")
        self.assertEqual([a for _, a, _ in harness.transcript(chat_id)], ["user", "user"])
        harness.abandon(chat_id)
        harness.send_turn(chat_id, "third")
        self.assertEqual([a for _, a, _ in harness.transcript(chat_id)],
                         ["user", "user", "user", "agent"])
        self.assert_store_valid(harness.store, "concurrent-turn-policy-flipped",
                                "#86's record-then-refuse policy on the converged loop.")


LOCK_HOLDER = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from dory_wrangler import launch_boundary as lb
    from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
    from dory_wrangler.store import ChatStore
    from dory_wrangler.session_manager import SessionManager
    root, chat_id, marker, release = sys.argv[2:6]

    class WaitsForTheTest(ScriptedStubLauncher):
        def launch(self, instruction):
            open(marker, "w").close()
            while not os.path.exists(release):
                time.sleep(0.02)
            return ScriptedStubLauncher.launch(self, instruction)

    SessionManager(ChatStore(root), WaitsForTheTest({})).send_turn(chat_id, "from the other process")
''')


class OneServingProcessPerStore(unittest.TestCase, StoreCheck):
    """Decision 0002, D1: exactly one serving process per store, enforced.

    These two tests replace convergence's `TheTurnLockHoldsAcrossProcesses`,
    which asserted that two processes could serve one store and that the turn
    lock serialised them. The review showed a store shared that way is not safe
    (a second opener swept the first's in-flight writes and re-attached its live
    agents), and v0.1 never required it. Their expectation changed deliberately:
    a second process is now refused, before it has swept, re-attached or written
    anything, and the process serving the store is unaffected.
    """

    def test_a_second_process_is_refused_the_store_another_is_serving(self):
        root = support.scratch_root()
        store = ChatStore(root)
        chat_id = store.create_chat("Shared")["chat_id"]
        store.close()  # this process gives the store to the one that will serve it
        script = os.path.join(root, "..", os.path.basename(root) + "-holder.py")
        with open(script, "w") as handle:
            handle.write(LOCK_HOLDER)
        marker = os.path.join(root, "..", os.path.basename(root) + "-entered")
        release = os.path.join(root, "..", os.path.basename(root) + "-release")
        child = subprocess.Popen([sys.executable, script, support.SRC, root, chat_id,
                                  marker, release])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        while not os.path.exists(marker):
            self.assertIsNone(child.poll(), "the other process died before launching")
            time.sleep(0.02)

        reader = ChatStore(root, read_only=True)
        before = reader.export_records()
        temp = os.path.join(root, "chats", chat_id, ".tmp-an-unpublished-write")
        with open(temp, "wb") as handle:
            handle.write(b"another process's write in flight")

        here = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        from dory_wrangler.errors import StoreInUse
        from dory_wrangler.webapp import build_server
        with self.assertRaises(StoreInUse):
            here.send_turn(chat_id, "from this process")
        with self.assertRaises(StoreInUse):
            here.reattach_on_start()
        with self.assertRaises(StoreInUse):
            build_server(root, port=0, quiet=True,
                         launcher_config={"launcher": "scripted-stub"})
        # Changed nothing: no record, no transition, no sweep.
        self.assertEqual(reader.export_records(), before)
        self.assertTrue(os.path.exists(temp), "a refused process swept a live write")
        self.assertEqual(reader.list_sessions(chat_id)[0][0]["state"], "launching")
        # Read-only tooling still works while the store is served.
        tool = subprocess.run([sys.executable, os.path.join(PRODUCT, "validate_store.py"), root],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(tool.returncode, (0, 1), tool.stderr)
        self.assertEqual(reader.export_records(), before)
        os.unlink(temp)

        open(release, "w").close()
        self.assertEqual(child.wait(), 0)
        outcome = here.send_turn(chat_id, "from this process")
        self.assertEqual(outcome.session_state, "completed")
        self.assertEqual([t for _, _, t in here.transcript(chat_id)], [
            "from the other process", "answer to: from the other process",
            "from this process", "answer to: from this process"])
        self.assert_store_valid(here.store, "second-process-refused-then-serves")

    def test_a_turn_sent_by_a_second_process_is_refused_and_leaves_nothing(self):
        root = support.scratch_root()
        first = SessionManager(ChatStore(root),
                               ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
        chat_id = first.create_chat("Held elsewhere")
        first.send_turn(chat_id, "first")
        program = (
            "import sys; sys.path.insert(0, %r);"
            "from dory_wrangler.store import ChatStore;"
            "from dory_wrangler.session_manager import SessionManager;"
            "from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher;"
            "from dory_wrangler.errors import StoreInUse;"
            "m = SessionManager(ChatStore(%r), ScriptedStubLauncher({}));"
            "\ntry:\n    m.send_turn(%r, 'second')\n"
            "except StoreInUse:\n    print('refused')\n"
            % (support.SRC, root, chat_id))
        before = first.store.export_records()
        out = subprocess.check_output([sys.executable, "-c", program],
                                      universal_newlines=True)
        self.assertEqual(out.strip(), "refused")
        self.assertEqual(first.store.export_records(), before)


SERVES_FORTY_TURNS = textwrap.dedent('''
    import os, sys, collections, time
    sys.path.insert(0, sys.argv[1])
    from dory_wrangler.store import ChatStore
    from dory_wrangler.session_manager import SessionManager
    from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
    root, started, finished, release = sys.argv[2:6]
    manager = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
    manager.store.acquire()
    open(started, "w").close()
    failures = collections.Counter()
    for i in range(40):
        try:
            chat_id = manager.create_chat("turn %d" % i)
            manager.send_turn(chat_id, "hello %d" % i)
        except BaseException as exc:
            failures[type(exc).__name__] += 1
    print(sum(failures.values()), dict(failures))
    sys.stdout.flush()
    open(finished, "w").close()
    while not os.path.exists(release):
        time.sleep(0.01)
''')


class ASecondShellAgainstALiveOneChangesNothing(unittest.TestCase, StoreCheck):
    """Review finding R1, as the review reproduced it, closed (decision 0002, D1)."""

    def test_a_real_second_shell_against_a_live_dev_local_one(self):
        """A live shell with the real `dev-local` launcher holds a running, idle,
        real agent process. A second `run_shell.py` on the same store -- on the
        same port, as the review ran it, and on a free one -- is refused and
        exits having changed nothing: no transition, no observation, no swept
        temp file, and no second agent."""
        from shellproc import ShellProcess, children_of, is_alive
        root = support.scratch_root()
        first = ShellProcess(root, launcher="dev-local",
                             launcher_options={"profile": "persistent"})
        self.addCleanup(first.kill)
        first.start()
        agents = lambda: [c for c in children_of(first.process.pid) if is_alive(c)]
        self.addCleanup(lambda: [os.kill(c, 9) for c in agents()] if first.process else None)
        _status, chat = first.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        self.assertEqual(first.raw("POST", path + "/messages", {"text": "hello"})[0], 201)
        self.assertEqual(len(agents()), 1)
        reader = ChatStore(root, read_only=True)
        temp = os.path.join(root, "chats", chat["chat_id"], ".tmp-an-unpublished-write")
        for port in (str(first.port), "0"):
            with open(temp, "wb") as handle:
                handle.write(b"a write the live shell has not published yet")
            before = reader.export_records()
            second = subprocess.run(
                [sys.executable, os.path.join(PRODUCT, "run_shell.py"), "--root", root,
                 "--port", port, "--quiet", "--launcher", "dev-local",
                 "--launcher-options", json.dumps({"profile": "persistent"})],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(second.returncode, 3, second.stderr)
            self.assertIn(b"another shell is already serving", second.stderr)
            self.assertEqual(reader.export_records(), before, "the second shell wrote")
            self.assertTrue(os.path.exists(temp), "the second shell swept a live write")
            self.assertEqual(reader.list_sessions(chat["chat_id"])[0][0]["state"], "running")
            self.assertEqual(reader.read_session_observations(chat["chat_id"]), [])
        os.unlink(temp)
        # The first shell's agent is untouched: its next turn is delivered to it.
        self.assertEqual(first.raw("POST", path + "/messages", {"text": "still there?"})[0], 201)
        self.assertEqual(len(agents()), 1, "a second agent was started for the chat")
        self.assertEqual(len(reader.list_sessions(chat["chat_id"])), 1)
        self.assertEqual(reader.verify(), [])

    def test_forty_turns_served_while_another_process_keeps_trying_to_serve(self):
        """The review's 40-turn run: one process serves 40 turns while another
        opens the store the way a serving process does, as fast as it can. Every
        attempt is refused, and every turn is served whole."""
        from dory_wrangler.errors import StoreInUse
        from dory_wrangler.webapp import build_server
        root = support.scratch_root()
        script = os.path.join(root, "..", os.path.basename(root) + "-forty.py")
        started, finished, release = (
            os.path.join(root, "..", os.path.basename(root) + suffix)
            for suffix in ("-started", "-finished", "-release"))
        with open(script, "w") as handle:
            handle.write(SERVES_FORTY_TURNS)
        child = subprocess.Popen([sys.executable, script, support.SRC, root, started,
                                  finished, release],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        while not os.path.exists(started):
            self.assertIsNone(child.poll(), child.stderr.read() if child.poll() else "")
            time.sleep(0.01)
        refused, opened = 0, 0
        # Until the serving process says it has finished -- it then waits, still
        # holding the store, so no attempt below can land after it exits.
        while not os.path.exists(finished):
            self.assertIsNone(child.poll())
            try:
                build_server(root, port=0, quiet=True,
                             launcher_config={"launcher": "scripted-stub"}).server_close()
                opened += 1
            except StoreInUse:
                refused += 1
        open(release, "w").close()
        out, err = child.communicate()
        self.assertEqual(out.strip(), "0 {}", err)
        self.assertEqual(opened, 0)
        self.assertGreater(refused, 0)
        store = ChatStore(root, read_only=True)
        chats = store.list_chats()
        self.assertEqual(len(chats), 40)
        for chat in chats:
            self.assertEqual([s["state"] for s, _ in store.list_sessions(chat["chat_id"])],
                             ["completed"])
            self.assertEqual([m["author"] for m in store.read_messages(chat["chat_id"])],
                             ["user", "agent"])
        self.assertEqual(store.verify(), [])
        # And once the serving process is gone, the application starts.
        build_server(root, port=0, quiet=True,
                     launcher_config={"launcher": "scripted-stub"}).server_close()


class TheTurnLockHoldsWithinOneProcess(unittest.TestCase, StoreCheck):
    """The same lock, between threads, at the two points a race would bite.

    Each test lets a second action start while the first is parked at the exact
    point the lock exists to protect, then releases the first. With the lock the
    second always waits, so the outcome below is certain; without it the second
    runs into the window and the outcome differs. Nothing here depends on how
    long anything takes when the lock is present.
    """

    def test_a_second_send_cannot_interleave_between_the_check_and_the_record(self):
        import threading
        parked, release, entered_twice = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def compose(chat_id, text, store):
            calls.append(text)
            if len(calls) == 1:
                parked.set()
                release.wait(10)
            else:
                entered_twice.set()
            return text

        harness = support.harness({"launcher": "scripted-stub"}, compose=compose)
        chat_id = harness.create_chat("Two sends at once")
        errors = []

        def send(text):
            try:
                harness.send_turn(chat_id, text)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted on
                errors.append(exc)

        first = threading.Thread(target=send, args=("first",))
        first.start()
        self.assertTrue(parked.wait(10))
        second = threading.Thread(target=send, args=("second",))
        second.start()
        entered_twice.wait(1.0)  # with the lock this never happens before release
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(errors, [])
        self.assertEqual([t for _, _, t in harness.transcript(chat_id)],
                         ["first", "answer to: first", "second", "answer to: second"])
        self.assert_store_valid(harness.store, "two-threads-send-serialised")

    def test_abandon_waits_for_the_turn_in_flight(self):
        import threading
        parked, release = threading.Event(), threading.Event()

        class Parks(ScriptedStubLauncher):
            def launch(self, instruction):
                parked.set()
                release.wait(10)
                return ScriptedStubLauncher.launch(self, instruction)

        harness = support.harness({}, launcher=Parks({"launch_outcomes": ["unknown"]}))
        chat_id = harness.create_chat("Abandon during a launch")
        outcome = {}
        turn = threading.Thread(target=lambda: harness.send_turn(chat_id, "hello"))
        turn.start()
        self.assertTrue(parked.wait(10))

        def abandon():
            try:
                outcome["state"] = harness.abandon(chat_id)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        user = threading.Thread(target=abandon)
        user.start()
        user.join(1.0)  # with the lock it is still waiting here
        release.set()
        turn.join(10)
        user.join(10)
        self.assertEqual(outcome, {"state": "abandoned"},
                         "abandon acted on a launch still in flight instead of waiting "
                         "for the outcome it abandons")
        self.assert_store_valid(harness.store, "abandon-waits-for-the-turn")


class TheResumePointIsTheLastStoredSequence(unittest.TestCase, StoreCheck):
    """`events` is resumable by sequence, and the loop now reads its resume point
    from `ChatStore.next_event_sequence` rather than from #87's store. The drain's
    value is pinned by every multi-page turn; re-attachment discards its page, so
    its value is pinned here, by the argument the launcher was actually handed."""

    def test_re_attachment_resumes_after_the_last_preserved_event(self):
        seen = []

        class Records(ScriptedStubLauncher):
            def events(self, agent_handle, after_sequence):
                seen.append(after_sequence)
                return ScriptedStubLauncher.events(self, agent_handle, after_sequence)

        options = {"continuation": "persistent", "response_shape": "stream",
                   "end_of_turn": "turn_complete"}
        root = support.scratch_root()
        first = support.harness({}, store_path=root, launcher=ScriptedStubLauncher(options))
        chat_id = first.create_chat("Resume point")
        first.send_turn(chat_id, "hello")
        session = support.view(first).sessions_of(chat_id)[0]
        stored = len(support.view(first).events_of(session["session_id"]))
        self.assertEqual(stored, 2)
        reopened = support.harness(
            {}, store_path=root,
            launcher=Records(dict(options, resume_handles=[session["agent_handle"]])))
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "running")])
        self.assertEqual(seen, [stored])
        support.end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "reattach-resume-point")


class TheGuardsConvergenceAddedHaveExits(unittest.TestCase, StoreCheck):
    """Each added guard: the input it refuses, and where that input goes."""

    def test_a_launch_result_mutated_after_construction_is_a_classified_failure(self):
        class Mutates(ScriptedStubLauncher):
            def launch(self, instruction):
                result = ScriptedStubLauncher.launch(self, instruction)
                result.outcome = "bogus"
                return result

        harness = support.harness({}, launcher=Mutates({}))
        chat_id = harness.create_chat("Mutated result")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual((outcome.session_state, outcome.failure_category),
                         ("launch_failed", "internal_error"))
        self.assertEqual(support.view(harness).open_bindings(chat_id), [])
        self.assertEqual(harness.send_turn(chat_id, "again").session_state, "launch_failed")
        self.assert_store_valid(harness.store, "mutated-launch-result-classified")

    def test_a_launch_result_mutated_into_an_inconsistent_one_is_refused_at_the_seam(self):
        class Inconsistent(ScriptedStubLauncher):
            def launch(self, instruction):
                result = ScriptedStubLauncher.launch(self, instruction)
                result.agent_handle = None
                return result

        harness = support.harness({}, launcher=Inconsistent({}))
        chat_id = harness.create_chat("Handle removed")
        outcome = harness.send_turn(chat_id, "hello")
        self.assertEqual(outcome.session_state, "launch_failed")
        self.assert_store_valid(harness.store, "handle-removed-after-construction")

    def test_a_mutated_acknowledgement_records_nothing(self):
        class Garbles(ScriptedStubLauncher):
            def deliver(self, agent_handle, instruction):
                ack = ScriptedStubLauncher.deliver(self, agent_handle, instruction)
                ack.acknowledged = "yes"
                return ack

        harness = support.harness({}, launcher=Garbles(
            {"continuation": "persistent", "response_shape": "stream",
             "end_of_turn": "turn_complete"}))
        chat_id = harness.create_chat("Garbled ack")
        harness.send_turn(chat_id, "one")
        harness.send_turn(chat_id, "two")
        deliveries = harness.store.read_delivery_requests(chat_id)
        self.assertEqual([d["acknowledged"] for d in deliveries], [None])
        support.end_chat(harness, chat_id)
        self.assert_store_valid(harness.store, "garbled-ack-recorded-as-unknown")

    def test_a_delivery_the_store_would_refuse_is_refused_before_the_turn_is_recorded(self):
        """The delivery pre-flight. Its input: a wall clock that stepped back past
        the second in which the handle was issued, so the store refuses to date an
        addressing record before its handle. Without the pre-flight the user's
        turn is written and then the delivery is refused: a turn never offered."""
        config = {"continuation": "persistent", "response_shape": "stream",
                  "end_of_turn": "turn_complete"}
        for preflight in (True, False):
            with self.subTest(preflight=preflight):
                harness = support.harness({}, launcher=ScriptedStubLauncher(config))
                chat_id = harness.create_chat("Clock stepped back")
                harness.send_turn(chat_id, "one")
                if not preflight:
                    # Only the loop's pre-flight is skipped: the store's own call
                    # inside `append_delivery_request` still runs.
                    real = harness._store._require_deliverable
                    calls = []

                    def skip_the_preflight(chat, sid):
                        calls.append(sid)
                        if len(calls) == 1:
                            return None
                        return real(chat, sid)

                    harness._store._require_deliverable = skip_the_preflight
                real_now = ids.now
                ids.now = lambda: "2000-01-01T00:00:00.000000Z"
                try:
                    with self.assertRaises(ValidationRefused):
                        harness.send_turn(chat_id, "two")
                finally:
                    ids.now = real_now
                users = [t for _, a, t in harness.transcript(chat_id) if a == "user"]
                if preflight:
                    self.assertEqual(users, ["one"], "a refused delivery left its turn")
                    # The exit: the clock recovers and the same action succeeds.
                    harness.send_turn(chat_id, "two")
                    support.end_chat(harness, chat_id)
                    self.assert_store_valid(harness.store, "delivery-preflight-clock")
                else:
                    self.assertEqual(users, ["one", "two"],
                                     "the control must show the pre-flight is what "
                                     "keeps the unoffered turn out")


class TheServiceLayerAroundTheLoop(unittest.TestCase, StoreCheck):
    """`ChatService` now calls the loop instead of writing a turn itself; the two
    things it still does around that call are pinned here."""

    def service(self, options):
        from dory_wrangler.service import ChatService
        return ChatService(support.harness({"launcher": "scripted-stub", "options": options}))

    def test_a_first_turn_names_the_chat_even_when_what_follows_it_fails(self):
        service = self.service({"continuation": "fresh_binding", "response_shape": "one_shot",
                                "end_of_turn": "stream_end"})
        chat_id = service.create_chat()["chat_id"]
        with self.assertRaises(lb.LaunchBoundaryError):
            service.send_user_message(chat_id, "Name me anyway")
        self.assertEqual(service.store.read_chat(chat_id)["title"], "Name me anyway",
                         "the turn is in the chat, so the chat takes its name from it")

    def test_abandon_fails_closed_on_a_chat_whose_record_cannot_be_read(self):
        service = self.service({"launch_outcomes": ["unknown"]})
        chat_id = service.create_chat("Damaged")["chat_id"]
        service.send_user_message(chat_id, "hello")
        path = os.path.join(service.store.chats_dir, chat_id, "chat.json")
        with open(path, "w") as handle:
            handle.write("{ not a record")
        before = [s["state"] for s, _ in service.store.list_sessions(chat_id)]
        from dory_wrangler.errors import StoreCorrupt
        with self.assertRaises(StoreCorrupt):
            service.abandon(chat_id)
        self.assertEqual([s["state"] for s, _ in service.store.list_sessions(chat_id)], before)


class ARunningSessionTheShellCannotDrain(unittest.TestCase, StoreCheck):
    """Decision 0003, section 3: measured, pinned, and left for a decision.

    A launcher that misuses the seam mid-turn leaves the session `running` with
    nothing reading it. The chat loop's exit is the user's Stop, which #87
    proved and which still works; the shell exposes only Abandon, which the
    contract admits from `unknown` alone.
    """

    ONE_SHOT_THAT_ENDS_A_STREAM = {"continuation": "fresh_binding",
                                   "response_shape": "one_shot",
                                   "end_of_turn": "stream_end"}

    def stranded(self):
        root = support.scratch_root()
        harness = support.harness({"launcher": "scripted-stub",
                                   "options": self.ONE_SHOT_THAT_ENDS_A_STREAM},
                                  store_path=root)
        chat_id = harness.create_chat("Stranded")
        with self.assertRaises(lb.LaunchBoundaryError):
            harness.send_turn(chat_id, "hello")
        session = support.view(harness).sessions_of(chat_id)[0]
        self.assertEqual(session["state"], "running")
        return root, harness, chat_id, session

    def test_until_a_restart_the_shell_has_no_exit(self):
        _root, harness, chat_id, _session = self.stranded()
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "again")
        with self.assertRaises(NotPermitted):
            harness.abandon(chat_id)
        # The loop's own exit is still there; the shell does not expose it.
        harness.stop_agent(chat_id, "the user pressed Stop")
        self.assertEqual(support.view(harness).sessions_of(chat_id)[0]["state"], "terminated")
        self.assert_store_valid(harness.store, "stranded-running-stopped-through-the-loop")

    def test_a_restart_on_a_launcher_that_cannot_resume_gives_abandon_back(self):
        root, _harness, chat_id, session = self.stranded()
        reopened = support.harness({"launcher": "scripted-stub"}, store_path=root)
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "unknown")])
        reopened.abandon(chat_id)
        self.assertEqual(reopened.send_turn(chat_id, "again").session_state, "completed")
        self.assert_store_valid(reopened.store, "stranded-running-restart-abandon")

    def test_a_restart_on_a_launcher_that_can_resume_does_not(self):
        root, _harness, chat_id, session = self.stranded()
        reopened = support.harness(
            {"launcher": "scripted-stub",
             "options": {"resume_handles": [session["agent_handle"]]}},
            store_path=root)
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "running")])
        with self.assertRaises(ConcurrentLaunchRefused):
            reopened.send_turn(chat_id, "again")
        with self.assertRaises(NotPermitted):
            reopened.abandon(chat_id)

    def test_through_the_shell_the_misuse_is_a_502_and_then_refusals(self):
        from shellproc import ShellProcess
        root = support.scratch_root()
        shell = ShellProcess(root, launcher_options=self.ONE_SHOT_THAT_ENDS_A_STREAM)
        self.addCleanup(shell.kill)
        shell.start()
        _status, chat = shell.post("/api/chats", {})
        path = "/api/chats/%s" % chat["chat_id"]
        self.assertEqual(shell.raw("POST", path + "/messages", {"text": "hello"})[0], 502)
        status, body = shell.raw("POST", path + "/messages", {"text": "again"})
        self.assertEqual((status, json.loads(body)["refused"]), (409, True))
        self.assertEqual(shell.raw("POST", path + "/abandon", {})[0], 409)
        shell.kill()
        # The restart is on a launcher that cannot resume and does not misuse the
        # seam, so re-attachment leaves `unknown` and the one action works.
        shell = ShellProcess(root)
        self.addCleanup(shell.kill)
        shell.start()
        self.assertEqual(shell.raw("POST", path + "/abandon", {})[0], 200)
        self.assertEqual(shell.raw("POST", path + "/messages", {"text": "again"})[0], 201)
        self.assertEqual(ChatStore(root).verify(), [])


class AKillAtAnyWriteOfATurnLeavesAWholeStoreWithAnExit(unittest.TestCase, StoreCheck):
    """#86's atomicity and restart recovery, re-proved with #87's loop in the path.

    #86 proved every fault point of every *primitive* write leaves a whole
    store. Convergence put a chat loop above those writes that makes eleven or
    more of them per turn, so the property that has to hold now is about the
    turn: a real process serving a real turn through `SessionManager` over
    `ChatStore` is killed at every declared fault point of every write that turn
    makes, and after each kill

    * the store reads back whole -- nothing partial is visible, the history is
      contiguous, and the turns durable before the crash are untouched;
    * the contract accepts it, except for the one transient the code accounting
      names (`LAUNCH_OUTCOME_MISMATCH`, an accepted launch not yet `running`);
    * a restart leaves no session in a state without a legal exit: re-attachment
      once, the user's abandon if it says `unknown`, and then a new turn is
      answered and the store is valid with no transient left.
    """

    CRASH = os.path.join(support.HERE, "_crashturn.py")
    TRANSIENT = {"LAUNCH_OUTCOME_MISMATCH"}

    def child(self, root, chat_id, options, k, point, warmup):
        completed = subprocess.run(
            [sys.executable, self.CRASH, root, chat_id, json.dumps(options), str(k), point,
             json.dumps(warmup)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return completed.returncode, completed.stdout, completed.stderr

    def template(self, options, turns_before):
        import shutil
        root = support.scratch_root("crash-template-")
        chat_id = ChatStore(root).create_chat("Killed mid-turn")["chat_id"]
        code, out, err = self.child(root, chat_id, options, 0, "pre_publish", turns_before)
        self.assertEqual(code, 0, err)
        writes = int(out.strip())
        shutil.rmtree(root)
        return writes

    def enumerate_kills(self, options, turns_before, points, least_writes):
        import shutil
        from dory_wrangler import atomic
        writes = self.template(options, turns_before)
        self.assertGreaterEqual(writes, least_writes,
                                "the turn made fewer writes than it should, so the "
                                "enumeration would be walking the wrong thing")
        exercised_points, killed_writes = set(), set()
        for point in points:
            for k in range(1, writes + 1):
                root = support.scratch_root("crash-")
                chat_id = ChatStore(root).create_chat("Killed mid-turn")["chat_id"]
                durable = []
                for text in turns_before:
                    durable += [text, "answer to: %s" % text]
                code, _out, err = self.child(root, chat_id, options, k, point, turns_before)
                if code == 0:
                    shutil.rmtree(root)
                    continue  # this write never reaches this point
                self.assertEqual(code, atomic.FAULT_EXIT_CODE, err)
                exercised_points.add(point)
                killed_writes.add(k)
                where = "killed at %s of write %d" % (point, k)

                fresh = ChatStore(root, sweep=False)
                records = fresh.export_records()  # raises if anything partial shows
                messages = fresh.read_messages(chat_id)
                self.assertEqual([m["content"]["text"] for m in messages[:len(durable)]],
                                 durable, where)
                self.assertEqual([m["sequence"] for m in messages],
                                 list(range(1, len(messages) + 1)), where)
                codes = set(v[0] for v in fresh.verify())
                self.assertLessEqual(codes, self.TRANSIENT, "%s: %s" % (where, codes))
                self.assertTrue(records)

                reopened = SessionManager(ChatStore(root), ScriptedStubLauncher(options))
                reopened.reattach_on_start()
                for session, _binding in reopened.store.list_sessions(chat_id):
                    self.assertNotIn(session["state"], ("pending", "launching"), where)
                    if session["state"] == "unknown":
                        reopened.abandon(chat_id)
                    elif session["state"] == "running":
                        support.end_chat(reopened, chat_id)
                outcome = reopened.send_turn(chat_id, "after the crash")
                self.assertIn(outcome.session_state, ("completed", "running"), where)
                support.end_chat(reopened, chat_id)
                self.assertEqual(reopened.store.verify(), [], where)
                shutil.rmtree(root)
        return exercised_points, killed_writes, writes

    def test_a_launched_turn_killed_at_every_fault_point_of_every_write(self):
        from dory_wrangler import atomic
        options = {"continuation": "fresh_binding", "response_shape": "one_shot"}
        # user message, session, launch request, launching, launch result,
        # running, three events, agent message, completed
        points, killed, writes = self.enumerate_kills(
            options, ["before"], atomic.FAULT_POINTS, least_writes=11)
        self.assertEqual(points, set(atomic.FAULT_POINTS),
                         "a declared fault point was never reached by a turn")
        self.assertEqual(killed, set(range(1, writes + 1)),
                         "a write of the turn was never killed")

    def test_a_delivered_turn_killed_at_every_write(self):
        options = {"continuation": "persistent", "response_shape": "stream",
                   "end_of_turn": "turn_complete"}
        # user message, delivery, acknowledgement, two events, agent message
        points, killed, writes = self.enumerate_kills(
            options, ["before"], ("pre_publish", "post_publish"), least_writes=6)
        self.assertEqual(killed, set(range(1, writes + 1)))


if __name__ == "__main__":
    unittest.main()
