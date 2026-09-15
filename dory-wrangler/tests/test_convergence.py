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
PERSISTENT = {"continuation": "persistent", "response_shape": "stream",
              "end_of_turn": "turn_complete"}
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


def dotted(node):
    """`os.replace`, `self._store.acquire`, `open` -- or '' for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


MODULES_THAT_WRITE = ("os", "shutil", "atomic", "io", "pathlib", "tempfile")
OS_WRITERS = ("open", "fdopen", "write", "replace", "rename", "renames", "link", "symlink",
              "unlink", "remove", "rmdir", "removedirs", "makedirs", "mkdir", "mkfifo",
              "mknod", "truncate", "ftruncate", "chmod", "chown", "utime")
ATOMIC_WRITERS = ("create_exclusive", "replace", "create_tree_exclusive", "sweep_temp_files",
                  "ChatLock", "own_store", "_write_temp", "_rmtree")


def _writer(call, aliases, module_name):
    """The name a call that can write a file is recorded under, or None."""
    callee = dotted(call.func)
    head, _, tail = callee.partition(".")
    if head in aliases:
        callee = aliases[head] + ("." + tail if tail else "")
        head, _, tail = callee.partition(".")
    if module_name == "atomic" and not tail and head in ATOMIC_WRITERS:
        callee, head, tail = "atomic." + head, "atomic", head
    if callee in ("open", "io.open"):
        mode = call.args[1] if len(call.args) > 1 else None
        for keyword in call.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if mode is None:
            return None  # the default mode reads
        if isinstance(mode, ast.Constant) and not (set(str(mode.value)) & set("wax+")):
            return None
        return "open"
    if head == "os" and tail in OS_WRITERS:
        if tail == "open" and len(call.args) > 1 and dotted(call.args[1]) == "os.O_RDONLY":
            return None
        return callee
    if head == "atomic" and tail in ATOMIC_WRITERS:
        return callee
    if head in ("shutil", "tempfile", "pathlib"):
        return callee
    if callee in ("exec", "eval", "__import__"):
        return callee
    if callee == "getattr" and call.args and dotted(call.args[0]) in MODULES_THAT_WRITE:
        return "getattr(%s)" % dotted(call.args[0])
    return None


def write_sites():
    """Every call in the product that can create, change or remove a file, as
    (file, qualified function, callee); module-level code is `<module>`. An
    import that brings a writer in under another name is recorded too."""
    found = set()
    for path in product_sources():
        name = relative(path)
        module_name = os.path.splitext(os.path.basename(path))[0]
        tree = parse(path)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[-1]
                for alias in node.names:
                    if module in MODULES_THAT_WRITE:
                        aliases[alias.asname or alias.name] = "%s.%s" % (module, alias.name)
                        found.add((name, "<module>", "import %s.%s" % (module, alias.name)))
                    elif alias.name in MODULES_THAT_WRITE and alias.asname:
                        aliases[alias.asname] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in MODULES_THAT_WRITE and alias.asname:
                        aliases[alias.asname] = alias.name

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    visit(child, scope + [child.name])
                    continue
                if isinstance(child, ast.Call):
                    callee = _writer(child, aliases, module_name)
                    if callee:
                        found.add((name, ".".join(scope) or "<module>", callee))
                visit(child, scope)

        visit(tree, [])
    return found


AUDITED_RUN = textwrap.dedent('''
    import json, os, sys, threading
    src, product, root = sys.argv[1:4]
    sys.path.insert(0, src)
    root = os.path.realpath(root)
    product = os.path.realpath(product)
    product_src = os.path.realpath(src)
    product_programs = [os.path.join(product, "run_shell.py"),
                        os.path.join(product, "validate_store.py")]
    WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    MUTATING = ("os.rename", "os.remove", "os.link", "os.mkdir", "os.rmdir", "os.symlink",
                "os.truncate", "os.chmod", "os.chown", "os.utime", "shutil.rmtree",
                "shutil.move", "shutil.copyfile", "shutil.copytree")
    from dory_wrangler import atomic
    seen = {"product": [], "controls": []}
    sink = ["product"]
    busy = threading.local()

    def hook(event, args):
        if getattr(busy, "on", False):
            return
        if event == "open":
            path, mode, flags = args
            if not isinstance(path, str):
                return
            if mode is not None:
                if not set(mode) & set("wax+"):
                    return
            elif not (flags or 0) & WRITE_FLAGS:
                return
        elif event in MUTATING:
            path = args[0]
            if not isinstance(path, str):
                return
        else:
            return
        busy.on = True
        try:
            if not os.path.realpath(path).startswith(root):
                return
            frame = sys._getframe(1)
            while frame is not None and not (
                    os.path.realpath(frame.f_code.co_filename).startswith(product_src + os.sep)
                    or os.path.realpath(frame.f_code.co_filename) in product_programs):
                frame = frame.f_back
            if frame is None:
                where = ("<outside the product>", "")
            else:
                where = (os.path.relpath(os.path.realpath(frame.f_code.co_filename), product),
                         frame.f_code.co_name)
            held = root in atomic._OWNERS
            seen[sink[0]].append([where[0], where[1], event.split(".")[-1], held])
        finally:
            busy.on = False

    sys.addaudithook(hook)

    from dory_wrangler import ids
    from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
    from dory_wrangler.webapp import build_server
    persistent = {"continuation": "persistent", "response_shape": "stream",
                  "end_of_turn": "turn_complete"}

    server = build_server(root, port=0, quiet=True, launcher=ScriptedStubLauncher(persistent))
    service = server.service
    chat = service.create_chat()["chat_id"]
    service.send_user_message(chat, "a launched turn")
    service.send_user_message(chat, "a delivered turn")
    service.abandon(chat)
    handle_chat = service.create_chat()["chat_id"]
    service.send_user_message(handle_chat, "left running for the restart")
    handle = service.store.list_sessions(handle_chat)[0][0]["agent_handle"]
    server.server_close()

    server = build_server(root, port=0, quiet=True,
                          launcher=ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
    service = server.service
    unknown = service.create_chat()["chat_id"]
    service.send_user_message(unknown, "hello")
    service.abandon(unknown)
    pending = service.create_chat()["chat_id"]
    real = service.store.append_transition

    def stepped(*a, **k):
        real_now, ids.now = ids.now, lambda: "2000-01-01T00:00:00.000000Z"
        try:
            return real(*a, **k)
        finally:
            ids.now = real_now

    service.store.append_transition = stepped
    try:
        service.send_user_message(pending, "stranded")
    except Exception:
        pass
    del service.store.append_transition
    service.abandon(pending)
    server.server_close()
    sink[0] = "setup"
    seen["setup"] = []
    os.makedirs(os.path.join(root, "chats", ".tmp-a-staging-tree", "messages"))
    with open(os.path.join(root, "chats", chat, ".tmp-a-temp-file"), "w") as h:
        h.write("left by an interrupted write")
    sink[0] = "product"

    server = build_server(root, port=0, quiet=True,
                          launcher=ScriptedStubLauncher(dict(persistent, resume_handles=[handle])))
    server.service.abandon(handle_chat)
    server.server_close()

    sink[0] = "controls"
    with open(os.path.join(root, "a-second-writer.json"), "w") as h:
        h.write("{}")
    atomic.create_exclusive(os.path.join(root, "chats", chat, "unlocked.json"), b"{}")
    del seen["setup"]
    print(json.dumps(seen))
''')


class ExactlyOneStoreAndOneServedApplication(unittest.TestCase):
    """The first acceptance criterion of #88, read off the tree."""

    def test_the_harness_tree_and_its_store_are_gone(self):
        self.assertFalse(os.path.exists(os.path.join(PRODUCT, "harness")))
        for gone in ("harness_store.py", "identity.py", "app.py"):
            self.assertFalse(
                os.path.exists(os.path.join(PRODUCT, "src", "dory_wrangler", gone)), gone)

    def test_exactly_one_class_is_a_store(self):
        """A store is anything that publishes a record: code that calls the atomic
        publishers. Walked over *every* function, method and module body -- not
        only classes -- and over imports, so a module-level publisher or an
        imported alias is seen too. Exactly one class publishes, through its
        three gated primitives."""
        found = sorted(set(
            (file, where) for file, where, callee in write_sites()
            if callee in ("atomic.create_exclusive", "atomic.replace",
                          "atomic.create_tree_exclusive")
            or callee.startswith("import atomic.")))
        self.assertEqual(found, [
            ("src/dory_wrangler/store.py", "ChatStore._publish_new"),
            ("src/dory_wrangler/store.py", "ChatStore._publish_replace"),
            ("src/dory_wrangler/store.py", "ChatStore._publish_tree"),
        ])

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

    # Review finding R7. The check this replaces compared a set of (file,
    # call-name) pairs, so a second writer added to a file that already had one
    # call of that name -- `os.open` in session_manager.py, `atomic.replace`
    # through a module function in wiring.py -- was invisible; the review planted
    # exactly those two and every check stayed green. This one is exact at the
    # granularity of the function: every call in the product that can create,
    # change or remove a file, with the function it is made from. Equality, so a
    # new writer anywhere -- a new function, a new call in an old function, an
    # import that renames a publisher, an indirection through getattr/exec --
    # fails here and has to be added deliberately.
    WRITE_SITES = {
        # atomic.py: the primitives themselves
        ("src/dory_wrangler/atomic.py", "_write_temp", "os.open"),
        ("src/dory_wrangler/atomic.py", "_write_temp", "os.write"),
        ("src/dory_wrangler/atomic.py", "create_exclusive", "atomic._write_temp"),
        ("src/dory_wrangler/atomic.py", "replace", "atomic._write_temp"),
        ("src/dory_wrangler/atomic.py", "create_exclusive", "os.link"),
        ("src/dory_wrangler/atomic.py", "create_exclusive", "os.unlink"),
        ("src/dory_wrangler/atomic.py", "replace", "os.replace"),
        ("src/dory_wrangler/atomic.py", "create_tree_exclusive", "os.mkdir"),
        ("src/dory_wrangler/atomic.py", "create_tree_exclusive", "os.rename"),
        ("src/dory_wrangler/atomic.py", "sweep_temp_files", "os.unlink"),
        ("src/dory_wrangler/atomic.py", "_rmtree", "os.unlink"),
        ("src/dory_wrangler/atomic.py", "_rmtree", "os.rmdir"),
        ("src/dory_wrangler/atomic.py", "sweep_temp_files", "atomic._rmtree"),
        ("src/dory_wrangler/atomic.py", "ChatLock.__enter__", "os.open"),
        ("src/dory_wrangler/atomic.py", "own_store", "os.makedirs"),
        ("src/dory_wrangler/atomic.py", "own_store", "os.open"),
        # store.py: every write behind the store-level lock (decision D1)
        ("src/dory_wrangler/store.py", "ChatStore.acquire", "atomic.own_store"),
        ("src/dory_wrangler/store.py", "ChatStore.acquire", "os.makedirs"),
        ("src/dory_wrangler/store.py", "ChatStore.acquire", "atomic.sweep_temp_files"),
        ("src/dory_wrangler/store.py", "ChatStore._publish_new", "atomic.create_exclusive"),
        ("src/dory_wrangler/store.py", "ChatStore._publish_replace", "atomic.replace"),
        ("src/dory_wrangler/store.py", "ChatStore._publish_tree",
         "atomic.create_tree_exclusive"),
        ("src/dory_wrangler/store.py", "ChatStore._make_dirs", "os.makedirs"),
        ("src/dory_wrangler/store.py", "ChatStore._lock", "atomic.ChatLock"),
        ("src/dory_wrangler/store.py", "ChatStore.create_chat.build", "os.mkdir"),
        ("src/dory_wrangler/store.py", "ChatStore.create_chat.build", "open"),
        # outside the store: the chat's turn-lock file, and two command-line outputs
        ("src/dory_wrangler/session_manager.py", "_Held.__enter__", "os.open"),
        ("src/dory_wrangler/serve.py", "main", "open"),
        ("validate_store.py", "main", "open"),
    }

    def test_no_record_has_a_second_writer(self):
        self.assertEqual(sorted(write_sites()), sorted(self.WRITE_SITES))

    def test_every_store_write_takes_the_store_lock_first(self):
        """Decision D1, read off store.py: each gated primitive calls
        `self.acquire()` before the call that writes, and the chat-creation
        builder is only ever handed to `_publish_tree`."""
        tree = parse(os.path.join(PRODUCT, "src", "dory_wrangler", "store.py"))
        store = [n for n in tree.body
                 if isinstance(n, ast.ClassDef) and n.name == "ChatStore"][0]
        methods = dict((n.name, n) for n in store.body if isinstance(n, ast.FunctionDef))
        for name in ("_publish_new", "_publish_replace", "_publish_tree", "_make_dirs",
                     "_lock"):
            calls = sorted((c.lineno, c.col_offset, dotted(c.func))
                           for c in ast.walk(methods[name]) if isinstance(c, ast.Call))
            order = [callee for _line, _col, callee in calls]
            self.assertIn("self.acquire", order, name)
            writer = [callee for callee in order
                      if callee.startswith("atomic.") or callee == "os.makedirs"][0]
            self.assertLess(order.index("self.acquire"), order.index(writer), name)
        builder_uses = [dotted(c.func) for c in ast.walk(methods["create_chat"])
                        if isinstance(c, ast.Call)
                        and any(isinstance(a, ast.Name) and a.id == "build" for a in c.args)]
        self.assertEqual(builder_uses, ["self._publish_tree"])

    def test_every_write_the_running_product_makes_is_a_listed_site_under_the_lock(self):
        """The same claim made of the running product rather than its source.

        A child process installs an audit hook that sees every file the
        interpreter opens for writing, renames, links, removes or makes a
        directory for, then drives the product end to end: the served
        application's start-up, a launched and a delivered turn with its
        acknowledgement, the one action from `running`, `unknown` and `pending`,
        a restart that sweeps and re-attaches, and the first-turn title. Every
        write under the store root must come from a function listed above and,
        except the lock's own file, be made while the store-level lock is held.
        Two controls in the same child must be caught: a write from outside the
        product, and a primitive called without the lock."""
        root = support.scratch_root()
        completed = subprocess.run(
            [sys.executable, "-c", AUDITED_RUN, support.SRC, PRODUCT, root],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=300)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        allowed = set((file, where.split(".")[-1]) for file, where, _ in self.WRITE_SITES)
        self.assertGreater(len(report["product"]), 50)
        for file, function, event, held in report["product"]:
            self.assertIn((file, function), allowed, (file, function, event))
            if (file, function) != ("src/dory_wrangler/atomic.py", "own_store"):
                self.assertTrue(held, "%s:%s wrote (%s) without the store lock"
                                % (file, function, event))
        # Every site that makes a filesystem call itself -- rather than handing
        # the write to an atomic primitive, where the hook sees it -- was reached.
        exercised = set((file, function) for file, function, _e, _h in report["product"])
        direct = set((file, where.split(".")[-1]) for file, where, callee in self.WRITE_SITES
                     if not callee.startswith("atomic."))
        self.assertEqual(
            direct - exercised - {("src/dory_wrangler/serve.py", "main"),
                                  ("validate_store.py", "main")},
            set(), "a listed write site was never exercised, so this run cannot vouch for it")
        controls = [tuple(c) for c in report["controls"]]
        self.assertIn("<outside the product>", [c[0] for c in controls],
                      "a write from outside the product was not seen")
        self.assertTrue(
            [c for c in controls if c[0] == "src/dory_wrangler/atomic.py" and c[3] is False],
            "a primitive called without the store lock was not seen as unlocked")

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


def completes_in_another_thread(test, action, seconds=20):
    """Run `action` in a fresh thread -- each HTTP request is one -- and require
    it to finish. A lock left held by a failed thread shows up here as a hang."""
    import threading
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as exc:  # noqa: BLE001 - reported below
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    test.assertFalse(thread.is_alive(), "a later action on the chat is blocked: the "
                                        "turn lock was left held")
    if "error" in result:
        raise result["error"]
    return result.get("value")


class TheTurnLockGivesBackWhatItTook(unittest.TestCase, StoreCheck):
    """Review finding R6, and the lock's own paths the review's mutations left
    unpinned (M01, M02, M06)."""

    def open_descriptors_on(self, name):
        return sum(1 for fd in os.listdir("/proc/self/fd")
                   if os.path.realpath("/proc/self/fd/" + fd).endswith(name))

    def test_a_failed_open_of_the_lock_file_leaves_nothing_held(self):
        import errno
        from dory_wrangler import session_manager
        harness = support.harness({"launcher": "scripted-stub"})
        chat_id = harness.create_chat("Open fails")
        real_open = session_manager.os.open
        armed = [True]

        def fails_once(path, *args, **kwargs):
            if armed[0] and str(path).endswith(session_manager.TURN_LOCK_NAME):
                armed[0] = False
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_open(path, *args, **kwargs)

        session_manager.os.open = fails_once
        try:
            with self.assertRaises(PermissionError):
                harness.send_turn(chat_id, "one")
        finally:
            session_manager.os.open = real_open
        outcome = completes_in_another_thread(self, lambda: harness.send_turn(chat_id, "two"))
        self.assertEqual(outcome.session_state, "completed")
        self.assertEqual([t for _, a, t in harness.transcript(chat_id) if a == "user"], ["two"])
        self.assert_store_valid(harness.store, "turn-lock-open-failed-once")

    def test_a_failed_flock_leaves_nothing_held_and_leaks_no_descriptor(self):
        import errno
        from dory_wrangler import session_manager
        harness = support.harness({"launcher": "scripted-stub"})
        chat_id = harness.create_chat("flock fails")
        real_flock = session_manager.fcntl.flock
        armed = [True]

        def fails_once(fd, operation):
            if armed[0] and operation & session_manager.fcntl.LOCK_EX:
                armed[0] = False
                raise OSError(errno.ENOLCK, "No locks available")
            return real_flock(fd, operation)

        session_manager.fcntl.flock = fails_once
        try:
            with self.assertRaises(OSError):
                harness.send_turn(chat_id, "one")
        finally:
            session_manager.fcntl.flock = real_flock
        self.assertEqual(self.open_descriptors_on(session_manager.TURN_LOCK_NAME), 0)
        outcome = completes_in_another_thread(self, lambda: harness.send_turn(chat_id, "two"))
        self.assertEqual(outcome.session_state, "completed")
        self.assertEqual(self.open_descriptors_on(session_manager.TURN_LOCK_NAME), 0)

    def test_a_hold_refused_because_another_descriptor_holds_it_takes_nothing(self):
        """M01. Two chat loops over one store in one process: the second's
        re-attachment finds the chat held by the first and skips it -- and must
        not keep the in-process lock it took on the way, or every later action of
        the second loop on that chat, from any other thread, blocks forever."""
        import threading
        parked, release = threading.Event(), threading.Event()

        class Parks(ScriptedStubLauncher):
            def launch(self, instruction):
                parked.set()
                release.wait(30)
                return ScriptedStubLauncher.launch(self, instruction)

        root = support.scratch_root()
        first = SessionManager(ChatStore(root), Parks({}))
        chat_id = first.create_chat("Held by the other loop")
        turn = threading.Thread(target=lambda: first.send_turn(chat_id, "first"))
        turn.start()
        self.assertTrue(parked.wait(30))
        second = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        self.assertEqual(second.reattach_on_start(), [])
        release.set()
        turn.join(30)
        outcome = completes_in_another_thread(
            self, lambda: second.send_turn(chat_id, "second"))
        self.assertEqual(outcome.session_state, "completed")

    def test_a_re_entrant_exit_keeps_the_file_lock_for_the_outer_hold(self):
        """M02. A launcher that calls back into the loop during `launch` is
        refused, and returning from that inner call must not release the file
        lock the outer turn still holds: another descriptor still cannot take
        it while the launch continues."""
        seen = {}

        class ReEnters(ScriptedStubLauncher):
            def launch(self, instruction):
                try:
                    harness.send_turn(chat_id, "from inside the launch")
                except ConcurrentLaunchRefused:
                    seen["refused"] = True
                other = SessionManager(ChatStore(harness.store.root), ScriptedStubLauncher({}))
                with other._turns.hold(chat_id, blocking=False) as held:
                    seen["other_acquired"] = held.acquired
                return ScriptedStubLauncher.launch(self, instruction)

        harness = support.harness({}, launcher=ReEnters({}))
        chat_id = harness.create_chat("Re-entrant")
        harness.send_turn(chat_id, "outer")
        self.assertEqual(seen, {"refused": True, "other_acquired": False})

    def test_the_lock_path_is_built_only_from_a_chat_that_exists(self):
        """M06. The lock file's path is built from the store's own validated chat
        directory, so a value that is not a chat identifier is refused as one and
        creates no file anywhere -- in particular not in the store root."""
        from dory_wrangler.errors import NotFound
        from dory_wrangler.session_manager import TURN_LOCK_NAME
        root = support.scratch_root()
        harness = support.harness({"launcher": "scripted-stub"}, store_path=root)
        for hostile in ("..", ".", "cht_doesnotexist0000", "../chats"):
            with self.assertRaises(NotFound):
                harness.send_turn(hostile, "hello")
        for where in (root, os.path.join(root, "chats"), os.path.dirname(root)):
            self.assertFalse(os.path.exists(os.path.join(where, TURN_LOCK_NAME)), where)
        chat_id = harness.create_chat("Still works")
        self.assertEqual(completes_in_another_thread(
            self, lambda: harness.send_turn(chat_id, "hello")).session_state, "completed")


class StartUpGoesOnPastOneChatItCannotReAttach(unittest.TestCase, StoreCheck):
    """Review finding R8, and mutations M03 and M22: start-up re-attaches every
    chat it can, and one it cannot costs only that chat."""

    def unknown_chat(self, harness, title):
        harness._boundary._launch_outcomes = ["unknown"]
        chat_id = harness.create_chat(title)
        harness.send_turn(chat_id, "hello")
        return chat_id

    def running_chat(self, root, title):
        harness = SessionManager(ChatStore(root), ScriptedStubLauncher(PERSISTENT))
        chat_id = harness.create_chat(title)
        harness.send_turn(chat_id, "hello")
        return chat_id

    def test_one_damaged_chat_record_fails_closed_for_that_chat_only(self):
        """R8. A damaged `chat.json` used to stop the application from starting.
        Now the shell starts, re-attaches every other chat, lists the damaged
        one without showing anything from it, refuses to open or act on it, and
        every other chat works."""
        import threading
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from dory_wrangler import service as service_module
        from dory_wrangler import webapp
        root = support.scratch_root()
        good = self.running_chat(root, "Good")
        damaged = self.running_chat(root, "Damaged")
        with open(os.path.join(root, "chats", damaged, "chat.json"), "w") as handle:
            handle.write("{ not a record")
        server = webapp.build_server(root, port=0, quiet=True,
                                     launcher_config={"launcher": "scripted-stub"})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def call(method, path, payload=None):
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                              method=method, headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=30) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        store = ChatStore(root, read_only=True)
        self.assertEqual(store.list_sessions(good)[0][0]["state"], "unknown",
                         "the readable chat was not re-attached")
        self.assertEqual(store.list_sessions(damaged)[0][0]["state"], "running",
                         "the damaged chat was acted on")
        status, listed = call("GET", "/api/chats")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(c["chat_id"] for c in listed), sorted([good, damaged]))
        entry = [c for c in listed if c["chat_id"] == damaged][0]
        self.assertEqual((entry["title"], entry["preview"], entry.get("unreadable")),
                         (service_module.UNREADABLE_TITLE, "", True))
        self.assertEqual(call("GET", "/api/chats/" + damaged),
                         (409, {"error": webapp.UNREADABLE}))
        self.assertEqual(call("POST", "/api/chats/%s/messages" % damaged, {"text": "x"})[0], 409)
        self.assertEqual(call("POST", "/api/chats/%s/abandon" % damaged, {})[0], 409)
        self.assertEqual(call("POST", "/api/chats/%s/abandon" % good, {})[0], 200)
        status, body = call("POST", "/api/chats/%s/messages" % good, {"text": "again"})
        self.assertEqual((status, body["messages"][-1]["text"]), (201, "answer to: again"))

    def test_an_archived_chat_is_re_attached_too(self):
        """M03. An archived chat's live agent is still a live agent."""
        root = support.scratch_root()
        chat_id = self.running_chat(root, "Archived with a live agent")
        ChatStore(root).archive_chat(chat_id)
        reopened = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        session_id = reopened.store.list_sessions(chat_id)[0][0]["session_id"]
        self.assertEqual(reopened.reattach_on_start(), [(session_id, "unknown")])
        self.assertEqual(reopened.abandon(chat_id), "abandoned")
        self.assert_store_valid(reopened.store, "archived-chat-reattached")

    def test_a_re_attachment_the_store_refuses_costs_only_that_chat(self):
        """M22. A store *refusal* while re-attaching one chat -- here the wall
        clock stepped back past that session's last transition -- leaves that chat
        as it was, and every other chat is still re-attached."""
        root = support.scratch_root()
        refused = self.running_chat(root, "Refused")
        other = self.running_chat(root, "Other")
        reopened = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        real = reopened.store.append_transition

        def clock_behind_for(chat_id, *args, **kwargs):
            if chat_id == refused:
                real_now, ids.now = ids.now, lambda: "2000-01-01T00:00:00.000000Z"
                try:
                    return real(chat_id, *args, **kwargs)
                finally:
                    ids.now = real_now
            return real(chat_id, *args, **kwargs)

        reopened.store.append_transition = clock_behind_for
        outcomes = reopened.reattach_on_start()
        del reopened.store.append_transition
        other_session = reopened.store.list_sessions(other)[0][0]
        self.assertEqual(outcomes, [(other_session["session_id"], "unknown")])
        self.assertEqual(reopened.store.list_sessions(refused)[0][0]["state"], "running")
        # And the chat it could not re-attach still has the one action as its exit.
        self.assertEqual(reopened.abandon(refused), "abandoned")
        self.assert_store_valid(reopened.store, "reattachment-refused-for-one-chat")


HOLDS_THE_STORE = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from dory_wrangler.store import ChatStore
    root, holding, release = sys.argv[2:5]
    store = ChatStore(root)
    store.acquire()
    open(holding, "w").close()
    while not os.path.exists(release):
        time.sleep(0.01)
''')


class TheStoreLockHasNoGaps(unittest.TestCase, StoreCheck):
    """Pins for the guards decision D1 added, each against the input that
    distinguishes it."""

    def other_process_holds(self, root):
        tag = os.path.join(root, "..", os.path.basename(root) + "-%d" % time.time_ns())
        script, holding, release = tag + "-holder.py", tag + "-holding", tag + "-release"
        with open(script, "w") as handle:
            handle.write(HOLDS_THE_STORE)
        child = subprocess.Popen([sys.executable, script, support.SRC, root, holding, release])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        while not os.path.exists(holding):
            self.assertIsNone(child.poll())
            time.sleep(0.01)

        def let_go():
            open(release, "w").close()
            self.assertEqual(child.wait(), 0)
        return let_go

    def can_another_process_take(self, root):
        program = ("import sys; sys.path.insert(0, %r)\n"
                   "from dory_wrangler.store import ChatStore\n"
                   "from dory_wrangler.errors import StoreInUse\n"
                   "try:\n    ChatStore(%r).acquire(); print('took')\n"
                   "except StoreInUse:\n    print('refused')\n" % (support.SRC, root))
        return subprocess.check_output([sys.executable, "-c", program],
                                       universal_newlines=True).strip()

    def test_a_read_only_store_writes_nothing_and_takes_no_lock(self):
        from dory_wrangler.errors import ReadOnlyStore
        root = support.scratch_root()
        writer = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        chat_id = writer.create_chat("Read only")
        writer.send_turn(chat_id, "hello")
        writer.store.close()
        let_go = self.other_process_holds(root)
        temp = os.path.join(root, "chats", chat_id, ".tmp-live-write")
        with open(temp, "wb") as handle:
            handle.write(b"x")
        reader = ChatStore(root, read_only=True)
        before = reader.export_records()
        for write in (lambda: reader.acquire(),
                      lambda: reader.create_chat("no"),
                      lambda: reader.append_user_message(chat_id, "no"),
                      lambda: reader.set_title(chat_id, "no"),
                      lambda: reader.archive_chat(chat_id)):
            with self.assertRaises(ReadOnlyStore):
                write()
        self.assertFalse(reader.held)
        self.assertEqual(reader.export_records(), before)
        self.assertTrue(os.path.exists(temp))
        empty = ChatStore(support.scratch_root(), read_only=True)
        self.assertEqual((empty.list_chats(), empty.export_records(), empty.chat_ids()),
                         ([], [], []))
        let_go()

    def test_a_refused_process_creates_no_turn_lock_file(self):
        root = support.scratch_root()
        store = ChatStore(root)
        chat_id = store.create_chat("Never acted on")["chat_id"]
        store.close()
        let_go = self.other_process_holds(root)
        from dory_wrangler.errors import StoreInUse
        from dory_wrangler.session_manager import TURN_LOCK_NAME
        here = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        for action in (lambda: here.send_turn(chat_id, "hello"), lambda: here.abandon(chat_id),
                       lambda: here.stop_agent(chat_id, "stop")):
            with self.assertRaises(StoreInUse):
                action()
        self.assertFalse(os.path.exists(os.path.join(root, "chats", chat_id, TURN_LOCK_NAME)))
        let_go()

    def test_a_second_holder_in_the_same_process_does_not_sweep(self):
        root = support.scratch_root()
        first = ChatStore(root)
        chat_id = first.create_chat("Held")["chat_id"]
        temp = os.path.join(root, "chats", chat_id, ".tmp-in-flight-in-this-process")
        with open(temp, "wb") as handle:
            handle.write(b"a write another thread has not published yet")
        second = ChatStore(root)
        second.acquire()
        self.assertTrue(os.path.exists(temp), "a second holder swept a live write")
        first.close()
        second.close()
        third = ChatStore(root)
        third.acquire()
        self.assertFalse(os.path.exists(temp), "taking the lock did not sweep")

    COLLECTED_WHILE_GUARDED = textwrap.dedent("""
        import fcntl, gc, os, sys
        sys.path.insert(0, sys.argv[1])
        from dory_wrangler import atomic
        from dory_wrangler.store import ChatStore
        base = sys.argv[2]

        def collected_holder(name):
            store = ChatStore(os.path.join(base, name))
            store.acquire()
            store.cycle = store  # freed only by the cyclic collector

        real_makedirs, real_close = os.makedirs, os.close

        def collecting(real):
            def call(*args, **kwargs):
                gc.collect()
                return real(*args, **kwargs)
            return call

        # 1. A holder collected while `own_store` holds the guard.
        collected_holder("a")
        os.makedirs = collecting(real_makedirs)
        ChatStore(os.path.join(base, "b")).acquire()
        os.makedirs = real_makedirs
        # 2. A holder collected while an explicit close holds the guard.
        closing = ChatStore(os.path.join(base, "c"))
        closing.acquire()
        collected_holder("d")
        os.close = collecting(real_close)
        closing.close()
        os.close = real_close

        free = []
        for name in "abcd":
            fd = os.open(os.path.join(base, name, atomic.STORE_LOCK_NAME), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(name)
            except BlockingIOError:
                pass
            finally:
                os.close(fd)
        print(" ".join(free), len(atomic._OWNERS))
    """)

    def test_a_store_collected_while_the_guard_is_held_does_not_deadlock(self):
        """Found when the remediation sweep was re-run: the collector finalized a
        held `ChatStore` inside `own_store`, whose finalizer then waited for the
        guard its own thread held, and the suite hung. Deterministic here by
        collecting at exactly those points, in a child with a deadline, so a
        regression fails this test rather than hanging the suite."""
        base = support.scratch_root()
        try:
            done = subprocess.run(
                [sys.executable, "-c", self.COLLECTED_WHILE_GUARDED, support.SRC, base],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("a store collected while the store-lock guard was held deadlocked")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "a b c d 0"),
                         "every collected or closed hold is given back: %s" % done.stdout)

    def test_the_lock_is_released_only_by_the_last_holder_in_the_process(self):
        root = support.scratch_root()
        first, second = ChatStore(root), ChatStore(root)
        first.acquire()
        second.acquire()
        first.close()
        self.assertEqual(self.can_another_process_take(root), "refused")
        second.close()
        self.assertEqual(self.can_another_process_take(root), "took")

    def test_closing_the_served_application_gives_the_store_back(self):
        from dory_wrangler.webapp import build_server
        root = support.scratch_root()
        server = build_server(root, port=0, quiet=True,
                              launcher_config={"launcher": "scripted-stub"})
        self.assertEqual(self.can_another_process_take(root), "refused")
        server.server_close()
        self.assertEqual(self.can_another_process_take(root), "took")

    def test_a_shell_refused_its_port_re_attaches_nothing_and_holds_nothing(self):
        import socket
        from dory_wrangler.webapp import build_server
        root = support.scratch_root()
        harness = SessionManager(ChatStore(root), ScriptedStubLauncher(PERSISTENT))
        chat_id = harness.create_chat("Live when the port was taken")
        harness.send_turn(chat_id, "hello")
        harness.store.close()
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        before = ChatStore(root, read_only=True).export_records()
        with self.assertRaises(OSError):
            build_server(root, port=taken.getsockname()[1], quiet=True,
                         launcher_config={"launcher": "scripted-stub"})
        self.assertEqual(ChatStore(root, read_only=True).export_records(), before)
        self.assertEqual(self.can_another_process_take(root), "took")

    def test_a_launcher_that_misuses_the_seam_during_re_attachment_costs_only_that_chat(self):
        root = support.scratch_root()
        broken = SessionManager(ChatStore(root), ScriptedStubLauncher(PERSISTENT))
        misused = broken.create_chat("Misused at restart")
        broken.send_turn(misused, "hello")
        fine = broken.create_chat("Fine")
        broken._boundary._launch_outcomes = ["unknown"]
        broken.send_turn(fine, "hello")
        handle = broken.store.list_sessions(misused)[0][0]["agent_handle"]
        stored = len(broken.store.read_all_events_of_session(
            misused, broken.store.list_sessions(misused)[0][0]["session_id"]))
        broken.store.close()

        class Gaps(ScriptedStubLauncher):
            def events(self, agent_handle, after_sequence):
                return lb.EventsPage([lb.EventPayload(after_sequence + 5, "agent",
                                                      "unrecognized", b"past a gap")])

        reopened = SessionManager(ChatStore(root),
                                  Gaps(dict(PERSISTENT, resume_handles=[handle])))
        outcomes = dict(reopened.reattach_on_start())
        fine_session = reopened.store.list_sessions(fine)[0][0]
        self.assertEqual(outcomes, {fine_session["session_id"]: "unknown"})
        self.assertEqual(reopened.abandon(misused), "terminated")
        self.assertEqual(reopened.abandon(fine), "abandoned")
        self.assertEqual(stored, len(reopened.store.read_all_events_of_session(
            misused, reopened.store.list_sessions(misused)[0][0]["session_id"])))

    def test_a_stop_answer_that_is_not_a_usable_confirmation_is_unconfirmed(self):
        cases = {
            "not a StopAck": lambda ack: "stopped, honestly",
            "confirmed mutated to a truthy non-boolean": lambda ack: setattr(
                ack, "confirmed", "yes") or ack,
            "detail mutated to a non-string": lambda ack: setattr(ack, "detail", 7) or ack,
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                class Spoils(ScriptedStubLauncher):
                    def stop(self, agent_handle, reason):
                        return spoil(ScriptedStubLauncher.stop(self, agent_handle, reason))

                harness = SessionManager(ChatStore(support.scratch_root()), Spoils(PERSISTENT))
                chat_id = harness.create_chat(name)
                harness.send_turn(chat_id, "hello")
                final = harness.abandon(chat_id)
                kinds = [o["kind"] for o in harness.store.read_session_observations(chat_id)]
                if name.startswith("detail"):
                    self.assertEqual((final, kinds), ("terminated", ["stop_confirmed"]))
                else:
                    self.assertEqual((final, kinds), ("abandoned", ["stop_unconfirmed"]))
                self.assert_store_valid(harness.store, "spoiled-stop-" + name.replace(" ", "-"))

    def test_the_launcher_id_and_capabilities_written_are_the_ones_checked(self):
        class Changes(ScriptedStubLauncher):
            reads = 0

            @property
            def launcher_id(self):
                Changes.reads += 1
                return "scripted-stub" if Changes.reads == 1 else "Not A Launcher Id!"

        harness = SessionManager(ChatStore(support.scratch_root()), Changes({}))
        chat_id = harness.create_chat("Changes between reads")
        self.assertEqual(harness.send_turn(chat_id, "hello").session_state, "completed")
        self.assertEqual(Changes.reads, 1)

    def test_a_re_attachment_page_that_ends_the_stream_carries_the_session_to_unknown(self):
        root = support.scratch_root()
        first = support.harness({"launcher": "scripted-stub", "options": PERSISTENT},
                                store_path=root)
        chat_id = first.create_chat("Stream ended while the harness was down")
        first.send_turn(chat_id, "one")
        session = first.store.list_sessions(chat_id)[0][0]
        stored = len(first.store.read_all_events_of_session(chat_id, session["session_id"]))
        first.store.close()
        resuming = ScriptedStubLauncher(dict(PERSISTENT, resume_handles=[session["agent_handle"]]))
        agent = resuming._sessions[session["agent_handle"]]
        agent.next_sequence = stored + 1
        resuming._emit(agent, "launcher", "recognized", json.dumps({"type": "stream_end"}),
                       interpreted_type="stream_end")
        reopened = support.harness({}, store_path=root, launcher=resuming)
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "unknown")])
        last = reopened.store.read_session(chat_id, session["session_id"])["transitions"][-1]
        self.assertEqual((last["to"], last["evidence"]["kind"]), ("unknown", "stream_end"))
        self.assertEqual(reopened.abandon(chat_id), "abandoned")
        self.assert_store_valid(reopened.store, "reattachment-page-stream-end")


class TheResumePointIsTheLastStoredSequence(unittest.TestCase, StoreCheck):
    """`events` is resumable by sequence, and the loop now reads its resume point
    from `ChatStore.next_event_sequence` rather than from #87's store. The drain's
    value is pinned by every multi-page turn; re-attachment's is pinned here, by
    the argument the launcher was actually handed."""

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


class PagedLauncher(lb.LaunchBoundary):
    """A fresh-binding stream launcher that serves fixed pages, one per `events`."""

    launcher_id = "scripted-stub"

    def __init__(self, pages, handle="paged-agent-0001"):
        self._pages = [list(page) for page in pages]
        self._handle = handle
        self._capabilities = lb.LauncherCapabilities("fresh_binding", "stream", None)
        self.stops = []

    @property
    def capabilities(self):
        return self._capabilities

    def launch(self, instruction):
        return lb.LaunchResult("accepted", agent_handle=self._handle)

    def events(self, agent_handle, after_sequence):
        return lb.EventsPage(self._pages.pop(0) if self._pages else [])

    def stop(self, agent_handle, reason):
        self.stops.append(agent_handle)
        return lb.StopAck(True, detail="stopped")


def agent_text(sequence, text):
    return lb.EventPayload(sequence, "agent", "recognized",
                           json.dumps({"type": "assistant_text", "text": text}).encode(),
                           interpreted_type="assistant_text", text=text)


def launcher_report(sequence, kind):
    return lb.EventPayload(sequence, "launcher", "recognized",
                           json.dumps({"type": kind}).encode(), interpreted_type=kind)


class APageIsPreservedPastARefusal(unittest.TestCase, StoreCheck):
    """Review finding R5: after a refusal in the middle of a page, the honest
    payloads later on the same page are still preserved (contract 7, P1). #87
    preserved `[1, 2, 3]`; convergence preserved `[1]`. Both causes."""

    def preserved(self, harness, chat_id):
        session = harness.store.list_sessions(chat_id)[0][0]
        return [e["sequence"] for e in
                harness.store.read_all_events_of_session(chat_id, session["session_id"])]

    def test_after_a_contradicting_replay(self):
        from dory_wrangler.errors import StoreCorrupt
        contradiction = lb.EventPayload(1, "agent", "unrecognized",
                                        b"different bytes at sequence one")
        harness = SessionManager(ChatStore(support.scratch_root()), PagedLauncher([
            [agent_text(1, "first")],
            [contradiction, agent_text(2, "second"), launcher_report(3, "session_completed")],
        ]))
        chat_id = harness.create_chat("Contradicting replay")
        with self.assertRaises(StoreCorrupt):
            harness.send_turn(chat_id, "hello")
        self.assertEqual(self.preserved(harness, chat_id), [1, 2, 3])
        self.assertEqual(harness.store.list_sessions(chat_id)[0][0]["state"], "completed")
        self.assertEqual([t for _, a, t in harness.transcript(chat_id) if a == "agent"],
                         ["first", "second"])
        self.assert_store_valid(harness.store, "page-preserved-past-a-contradiction")

    def test_after_a_clock_refusal_of_a_mid_page_completion(self):
        harness = SessionManager(ChatStore(support.scratch_root()), PagedLauncher([
            [agent_text(1, "first"), launcher_report(2, "session_completed"),
             lb.EventPayload(3, "launcher", "unrecognized", b'{"type": "exit_report"}')],
        ]))
        chat_id = harness.create_chat("Clock refusal mid-page")
        real = harness.store.append_transition

        def clock_steps_back_for_completion(*args, **kwargs):
            if args[3] == "completed":
                real_now, ids.now = ids.now, lambda: "2000-01-01T00:00:00.000000Z"
                try:
                    return real(*args, **kwargs)
                finally:
                    ids.now = real_now
            return real(*args, **kwargs)

        harness.store.append_transition = clock_steps_back_for_completion
        with self.assertRaises(ValidationRefused):
            harness.send_turn(chat_id, "hello")
        del harness.store.append_transition
        self.assertEqual(self.preserved(harness, chat_id), [1, 2, 3])
        # The completion was refused, so the session is where the store left it,
        # and the one lifecycle action is its exit (decision D2).
        self.assertEqual(harness.store.list_sessions(chat_id)[0][0]["state"], "running")
        self.assertEqual(harness.abandon(chat_id), "terminated")
        self.assert_store_valid(harness.store, "page-preserved-past-a-clock-refusal")


class ReAttachmentPreservesWhatItReads(unittest.TestCase, StoreCheck):
    """The third edge of the stop-then-abandon composition. On a launcher that
    can resume, the page re-attachment reads can carry the agent's answer and
    its completion; it is preserved and acted on, so the chat is not later
    recorded as a user terminating an agent that had completed."""

    def test_a_completion_reported_to_re_attachment_is_kept(self):
        root = support.scratch_root()
        first = support.harness({"launcher": "scripted-stub",
                                 "options": PERSISTENT},
                                store_path=root)
        chat_id = first.create_chat("Completed while the harness was down")
        first.send_turn(chat_id, "one")
        session = support.view(first).sessions_of(chat_id)[0]
        stored = len(support.view(first).events_of(session["session_id"]))
        first.store.close()

        resuming = ScriptedStubLauncher(dict(PERSISTENT,
                                             resume_handles=[session["agent_handle"]]))
        agent = resuming._sessions[session["agent_handle"]]
        agent.next_sequence = stored + 1
        resuming._emit(agent, "agent", "recognized",
                       json.dumps({"type": "assistant_text", "text": "the late answer"}),
                       interpreted_type="assistant_text", text="the late answer")
        resuming._emit(agent, "agent", "recognized", json.dumps({"type": "turn_complete"}),
                       interpreted_type="turn_complete")
        resuming._emit(agent, "launcher", "recognized",
                       json.dumps({"type": "session_completed"}),
                       interpreted_type="session_completed")
        reopened = support.harness({}, store_path=root, launcher=resuming)
        self.assertEqual(reopened.reattach_on_start(),
                         [(session["session_id"], "completed")])
        events = support.view(reopened).events_of(session["session_id"])
        self.assertEqual([e["sequence"] for e in events], list(range(1, stored + 4)))
        self.assertEqual([t for _, a, t in reopened.transcript(chat_id) if a == "agent"],
                         ["answer to: one", "the late answer"])
        # Nothing left to abandon, and nothing was stopped.
        with self.assertRaises(NotPermitted):
            reopened.abandon(chat_id)
        self.assertEqual(resuming.stop_calls, [])
        self.assertEqual(reopened.send_turn(chat_id, "two").session_state, "running")
        support.end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "reattachment-page-preserved")


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
                    # Only the loop's pre-flight is skipped: the store's own
                    # checks inside `append_delivery_request` still run. (Since
                    # review finding R4 the loop's pre-flight is the store's
                    # `preflight_delivery`, which runs `_require_deliverable`.)
                    harness._store.preflight_delivery = lambda *args: None
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


class ATurnIsRecordedOnlyIfWhatItOpensIsAcceptable(unittest.TestCase, StoreCheck):
    """Review finding R4 (#87 F3, widened): decision 0003's invariant, as ordering.

    A user turn is written only after the session and packet it would open have
    been composed by the store's own composers and put to the store's own
    checks (`ChatStore.preflight_launch`, `preflight_delivery`). The cases below
    are not special cases in the product -- there is no clause for any of them
    -- they are instances of one rule: each would have been refused by a write
    *after* the turn was recorded, and each is now refused with nothing written,
    after which the same chat takes a well-formed turn.
    """

    PERSISTENT = {"continuation": "persistent", "response_shape": "stream",
                  "end_of_turn": "turn_complete"}

    def refused_with_nothing_written(self, harness, chat_id, text, name):
        before = harness.store.export_records()
        with self.assertRaises(ValidationRefused) as caught:
            harness.send_turn(chat_id, text)
        self.assertEqual(harness.store.export_records(), before,
                         "%s: a turn was recorded though what it opens is refused" % name)
        return caught.exception

    def exit_through_a_good_turn(self, root, chat_id, name, options=None):
        good = SessionManager(ChatStore(root), ScriptedStubLauncher(options or {}))
        outcome = good.send_turn(chat_id, "a well-formed turn")
        self.assertIn(outcome.session_state, ("completed", "running"))
        support.end_chat(good, chat_id)
        self.assert_store_valid(good.store, name)

    def test_a_malformed_launcher_id(self):
        for name, value in (("pattern", "Not A Launcher Id!"), ("type", 42)):
            class BadId(ScriptedStubLauncher):
                launcher_id = value
            root = support.scratch_root()
            harness = SessionManager(ChatStore(root), BadId({}))
            chat_id = harness.create_chat("Malformed launcher_id")
            self.refused_with_nothing_written(harness, chat_id, "hello", name)
            harness.store.close()
            self.exit_through_a_good_turn(root, chat_id, "preflight-launcher-id-" + name)

    def test_capabilities_changed_after_construction(self):
        root = support.scratch_root()
        launcher = ScriptedStubLauncher({})
        launcher._capabilities.continuation = "bogus"
        harness = SessionManager(ChatStore(root), launcher)
        chat_id = harness.create_chat("Mutated capabilities")
        self.refused_with_nothing_written(harness, chat_id, "hello", "capabilities")
        harness.store.close()
        self.exit_through_a_good_turn(root, chat_id, "preflight-mutated-capabilities")

    def test_a_lone_surrogate_on_the_launch_path_and_on_the_delivery_path(self):
        root = support.scratch_root()
        harness = SessionManager(ChatStore(root), ScriptedStubLauncher({}))
        chat_id = harness.create_chat("Surrogate, launch")
        self.refused_with_nothing_written(harness, chat_id, "two \ud800", "launch")
        self.assertEqual(harness.send_turn(chat_id, "two").session_state, "completed")

        for bound in (None, 64):
            harness = SessionManager(ChatStore(support.scratch_root()), ScriptedStubLauncher(
                dict(self.PERSISTENT, instruction_bound_bytes=bound)))
            chat_id = harness.create_chat("Surrogate, delivery")
            harness.send_turn(chat_id, "one")
            self.refused_with_nothing_written(harness, chat_id, "two \ud800", "delivery")
            self.assertTrue(harness.send_turn(chat_id, "two").delivered)
            support.end_chat(harness, chat_id)
            self.assert_store_valid(harness.store, "preflight-surrogate-delivery-%s" % bound)

    def test_a_lone_surrogate_over_http(self):
        import threading
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from dory_wrangler.webapp import build_server, STORE_REFUSED
        for options, first in (({}, None), (self.PERSISTENT, "one")):
            root = support.scratch_root()
            server = build_server(root, port=0, quiet=True,
                                  launcher=ScriptedStubLauncher(options))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            port = server.server_address[1]
            chat_id = server.service.create_chat()["chat_id"]
            if first:
                server.service.send_user_message(chat_id, first)
            before = ChatStore(root, read_only=True).export_records()
            request = Request("http://127.0.0.1:%d/api/chats/%s/messages" % (port, chat_id),
                              data=b'{"text": "two \\ud800"}', method="POST",
                              headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=30)
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(json.loads(caught.exception.read().decode("utf-8")),
                             {"error": STORE_REFUSED})
            self.assertEqual(ChatStore(root, read_only=True).export_records(), before)

    def test_an_instruction_bound_the_session_recorded_differs_from_the_launchers(self):
        """The session declared 10 bytes; after a restart the resuming launcher
        declares none. The loop's check reads the launcher and passes; the
        store's reads the session, and now runs before the turn is written."""
        root = support.scratch_root()
        first = support.harness({"launcher": "scripted-stub",
                                 "options": dict(self.PERSISTENT, instruction_bound_bytes=10)},
                                store_path=root)
        chat_id = first.create_chat("Bound recorded on the session")
        first.send_turn(chat_id, "short")
        handle = support.view(first).sessions_of(chat_id)[0]["agent_handle"]
        first.store.close()
        reopened = support.harness(
            {"launcher": "scripted-stub",
             "options": dict(self.PERSISTENT, resume_handles=[handle])}, store_path=root)
        reopened.reattach_on_start()
        self.refused_with_nothing_written(reopened, chat_id, "x" * 40, "bound")
        self.assertTrue(reopened.send_turn(chat_id, "x" * 10).delivered)
        support.end_chat(reopened, chat_id)
        self.assert_store_valid(reopened.store, "preflight-session-bound")

    def test_an_unformable_packet(self):
        root = support.scratch_root()
        harness = SessionManager(ChatStore(root), ScriptedStubLauncher({}),
                                 compose=lambda chat_id, text, store: "")
        chat_id = harness.create_chat("Empty packet")
        self.refused_with_nothing_written(harness, chat_id, "hello", "empty packet")


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
    """Decision 0003, section 3, resolved by decision D2.

    A launcher that misuses the seam mid-turn leaves the session `running` with
    nothing reading it. These tests used to pin that the shell had **no exit**
    until a restart, and none at all on a launcher that can resume. Their
    expectation changed deliberately: the shell's one lifecycle action is the
    exit, through the user's `stop`, before and after a restart.
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

    def test_without_a_restart_the_one_action_is_the_exit(self):
        _root, harness, chat_id, _session = self.stranded()
        with self.assertRaises(ConcurrentLaunchRefused):
            harness.send_turn(chat_id, "again")
        self.assertEqual(harness.abandon(chat_id), "terminated")
        harness._boundary._end_of_turn = "session_completed"
        self.assertEqual(harness.send_turn(chat_id, "again").session_state, "completed")
        self.assert_store_valid(harness.store, "stranded-running-abandoned-without-restart")

    def test_a_restart_on_a_launcher_that_cannot_resume_gives_abandon_back(self):
        root, harness, chat_id, session = self.stranded()
        harness.store.close()
        reopened = support.harness({"launcher": "scripted-stub"}, store_path=root)
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "unknown")])
        self.assertEqual(reopened.abandon(chat_id), "abandoned")
        self.assertEqual(reopened.send_turn(chat_id, "again").session_state, "completed")
        self.assert_store_valid(reopened.store, "stranded-running-restart-abandon")

    def test_a_restart_on_a_launcher_that_can_resume_has_the_same_exit(self):
        root, harness, chat_id, session = self.stranded()
        harness.store.close()
        reopened = support.harness(
            {"launcher": "scripted-stub",
             "options": {"resume_handles": [session["agent_handle"]]}},
            store_path=root)
        self.assertEqual(reopened.reattach_on_start(), [(session["session_id"], "running")])
        with self.assertRaises(ConcurrentLaunchRefused):
            reopened.send_turn(chat_id, "again")
        self.assertEqual(reopened.abandon(chat_id), "terminated")
        self.assertEqual(reopened.send_turn(chat_id, "again").session_state, "completed")
        self.assert_store_valid(reopened.store, "stranded-running-resumed-then-abandoned")

    def test_through_the_shell_the_misuse_is_a_502_then_a_refusal_then_the_exit(self):
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
        self.assertEqual(shell.raw("POST", path + "/abandon", {})[0], 200)
        shell.kill()
        shell = ShellProcess(root)
        self.addCleanup(shell.kill)
        shell.start()
        self.assertEqual(shell.raw("POST", path + "/messages", {"text": "again"})[0], 201)
        self.assertEqual(ChatStore(root, read_only=True).verify(), [])


class EveryNonTerminalStateHasTheOneActionAsItsExit(unittest.TestCase, StoreCheck):
    """Decision D2 and review finding R2: the exit table, state by state, driven
    through `ChatService.abandon` -- the call the shell's one action makes.

    Each test strands a session the way the review did, shows the chat refusing
    turns, takes the one action, checks the exact transitions it wrote against
    the route decision D2 names, and shows the chat taking a new turn."""

    def service(self, launcher, root=None):
        from dory_wrangler.service import ChatService
        return ChatService(SessionManager(ChatStore(root or support.scratch_root()), launcher))

    def route(self, service, chat_id, session_id):
        session = service.store.read_session(chat_id, session_id)
        return [(t["from"], t["to"], t["owner"], t["evidence"]["kind"])
                for t in session["transitions"]]

    def exits(self, service, chat_id, expected_tail, name, stranded=True):
        session = service.store.list_sessions(chat_id)[0][0]
        if stranded:
            with self.assertRaises(ConcurrentLaunchRefused):
                service.send_user_message(chat_id, "refused while stranded")
        service.abandon(chat_id)
        route = self.route(service, chat_id, session["session_id"])
        self.assertEqual(route[-len(expected_tail):], expected_tail, route)
        for transition in route:
            self.assertIn((transition[0], transition[1]),
                          __import__("dory_wrangler.contract", fromlist=["x"])
                          .authorized_transitions())
        service.sessions._boundary._launch_outcomes = []
        after = service.send_user_message(chat_id, "a new turn after the exit")
        self.assertEqual(after["messages"][-1]["author"], "agent")
        self.assert_store_valid(service.store, name)

    def clock_step_on(self, store, to_state):
        real = store.append_transition

        def stepped(*args, **kwargs):
            if args[3] == to_state:
                real_now, ids.now = ids.now, lambda: "2000-01-01T00:00:00.000000Z"
                try:
                    return real(*args, **kwargs)
                finally:
                    ids.now = real_now
            return real(*args, **kwargs)

        store.append_transition = stepped
        return lambda: delattr(store, "append_transition")

    def test_pending_stranded_by_a_clock_step_before_launching(self):
        service = self.service(ScriptedStubLauncher({}))
        chat_id = service.create_chat()["chat_id"]
        undo = self.clock_step_on(service.store, "launching")
        with self.assertRaises(ValidationRefused):
            service.send_user_message(chat_id, "hello")
        undo()
        self.assertEqual(service.store.list_sessions(chat_id)[0][0]["state"], "pending")
        self.exits(service, chat_id,
                   [("pending", "launch_failed", "harness", "harness_action")],
                   "d2-exit-pending")

    def test_launching_stranded_by_a_clock_step_during_launch(self):
        service = self.service(ScriptedStubLauncher({}))
        chat_id = service.create_chat()["chat_id"]
        undo = self.clock_step_on(service.store, "running")
        with self.assertRaises(ValidationRefused):
            service.send_user_message(chat_id, "hello")
        undo()
        self.assertEqual(service.store.list_sessions(chat_id)[0][0]["state"], "launching")
        self.assertEqual([r["outcome"] for r in service.store.read_launch_results(chat_id)],
                         ["accepted"])
        self.exits(service, chat_id,
                   [("launching", "running", "launcher", "launch_result"),
                    ("running", "terminated", "user", "observation")],
                   "d2-exit-launching-accepted")

    def test_launching_whose_failure_was_recorded_but_not_its_state(self):
        service = self.service(ScriptedStubLauncher({"launch_outcomes": ["rejected"]}))
        chat_id = service.create_chat()["chat_id"]
        undo = self.clock_step_on(service.store, "launch_failed")
        with self.assertRaises(ValidationRefused):
            service.send_user_message(chat_id, "hello")
        undo()
        self.exits(service, chat_id,
                   [("launching", "launch_failed", "launcher", "launch_result")],
                   "d2-exit-launching-failed")

    def test_launching_whose_unknown_outcome_was_recorded_but_not_its_state(self):
        service = self.service(ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
        chat_id = service.create_chat()["chat_id"]
        undo = self.clock_step_on(service.store, "unknown")
        with self.assertRaises(ValidationRefused):
            service.send_user_message(chat_id, "hello")
        undo()
        self.exits(service, chat_id,
                   [("launching", "unknown", "launcher", "launch_result"),
                    ("unknown", "abandoned", "user", "user_action")],
                   "d2-exit-launching-unknown")

    def test_launching_with_no_launch_result_at_all(self):
        service = self.service(ScriptedStubLauncher({}))
        chat_id = service.create_chat()["chat_id"]
        real = service.store.append_launch_result

        def io_error(*args, **kwargs):
            raise ValidationRefused("the result could not be written")

        service.store.append_launch_result = io_error
        with self.assertRaises(ValidationRefused):
            service.send_user_message(chat_id, "hello")
        del service.store.append_launch_result
        self.assertEqual(service.store.read_launch_results(chat_id), [])
        self.exits(service, chat_id,
                   [("launching", "unknown", "launcher", "observation"),
                    ("unknown", "abandoned", "user", "user_action")],
                   "d2-exit-launching-no-result")

    def test_running_whose_stop_is_confirmed(self):
        service = self.service(ScriptedStubLauncher(PERSISTENT))
        chat_id = service.create_chat()["chat_id"]
        service.send_user_message(chat_id, "hello")
        session = service.store.list_sessions(chat_id)[0][0]
        self.assertEqual(service.sessions.abandon(chat_id), "terminated")
        self.assertEqual(self.route(service, chat_id, session["session_id"])[-1],
                         ("running", "terminated", "user", "observation"))
        # A confirmed stop leaves nothing to abandon, and that is the success.
        with self.assertRaises(NotPermitted):
            service.abandon(chat_id)
        self.assert_store_valid(service.store, "d2-exit-running-confirmed")

    def test_running_whose_stop_is_not_confirmed(self):
        service = self.service(ScriptedStubLauncher(dict(PERSISTENT, stop_confirms=False)))
        chat_id = service.create_chat()["chat_id"]
        service.send_user_message(chat_id, "hello")
        self.exits(service, chat_id,
                   [("running", "unknown", "launcher", "observation"),
                    ("unknown", "abandoned", "user", "user_action")],
                   "d2-exit-running-unconfirmed", stranded=False)

    def test_running_whose_stop_raises_something_other_than_a_launcher_error(self):
        class StopCrashes(ScriptedStubLauncher):
            def stop(self, agent_handle, reason):
                raise RuntimeError("the stop path fell over")

        service = self.service(StopCrashes(PERSISTENT))
        chat_id = service.create_chat()["chat_id"]
        service.send_user_message(chat_id, "hello")
        self.exits(service, chat_id,
                   [("running", "unknown", "launcher", "observation"),
                    ("unknown", "abandoned", "user", "user_action")],
                   "d2-exit-running-stop-crashes", stranded=False)
        observations = service.store.read_session_observations(chat_id)
        self.assertEqual(observations[0]["kind"], "stop_unconfirmed")
        self.assertIn("RuntimeError", observations[0]["detail"])

    def test_unknown(self):
        service = self.service(ScriptedStubLauncher({"launch_outcomes": ["unknown"]}))
        chat_id = service.create_chat()["chat_id"]
        service.send_user_message(chat_id, "hello")
        self.exits(service, chat_id, [("unknown", "abandoned", "user", "user_action")],
                   "d2-exit-unknown")

    def test_a_launcher_calling_back_during_an_action_in_flight_is_not_given_an_exit(self):
        """What must not change: a turn in flight. Inside `launch` the session is
        `launching` and the turn holds the chat; the one action, called back by
        the launcher on the same thread, is refused and writes nothing."""
        seen = {}

        class CallsBack(ScriptedStubLauncher):
            def launch(self, instruction):
                before = service.store.list_sessions(chat_id)[0][0]
                try:
                    service.sessions.abandon(chat_id)
                except NotPermitted:
                    seen["refused"] = True
                seen["unchanged"] = service.store.list_sessions(chat_id)[0][0] == before
                return ScriptedStubLauncher.launch(self, instruction)

        service = self.service(CallsBack({}))
        chat_id = service.create_chat()["chat_id"]
        service.send_user_message(chat_id, "hello")
        self.assertEqual(seen, {"refused": True, "unchanged": True})
        self.assertEqual(service.store.list_sessions(chat_id)[0][0]["state"], "completed")

    def test_a_holder_killed_after_start_up_is_no_longer_a_stranding(self):
        """Review finding R2, second half. Server B used to start while process A
        held a chat mid-launch, skip that chat, and -- after A was killed -- keep
        it stranded for B's whole life. Under decision D1 B cannot start while A
        serves the store; once A is gone B starts, re-attaches, and the one
        action is the exit."""
        import signal
        from dory_wrangler.errors import StoreInUse
        from dory_wrangler.webapp import build_server
        root = support.scratch_root()
        store = ChatStore(root)
        chat_id = store.create_chat("Held, then killed")["chat_id"]
        store.close()
        holder = textwrap.dedent('''
            import os, sys, time
            sys.path.insert(0, sys.argv[1])
            from dory_wrangler.launchers.scripted_stub import ScriptedStubLauncher
            from dory_wrangler.store import ChatStore
            from dory_wrangler.session_manager import SessionManager
            root, chat_id, marker = sys.argv[2:5]
            class Parks(ScriptedStubLauncher):
                def launch(self, instruction):
                    open(marker, "w").close()
                    time.sleep(600)
            SessionManager(ChatStore(root), Parks({})).send_turn(chat_id, "from the holder")
        ''')
        script = os.path.join(root, "..", os.path.basename(root) + "-killed-holder.py")
        marker = os.path.join(root, "..", os.path.basename(root) + "-killed-entered")
        with open(script, "w") as handle:
            handle.write(holder)
        child = subprocess.Popen([sys.executable, script, support.SRC, root, chat_id, marker])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        while not os.path.exists(marker):
            self.assertIsNone(child.poll())
            time.sleep(0.02)
        with self.assertRaises(StoreInUse):
            build_server(root, port=0, quiet=True, launcher_config={"launcher": "scripted-stub"})
        child.send_signal(signal.SIGKILL)
        child.wait()
        server = build_server(root, port=0, quiet=True,
                              launcher_config={"launcher": "scripted-stub"})
        try:
            service = server.service
            self.assertEqual(service.store.list_sessions(chat_id)[0][0]["state"], "unknown")
            with self.assertRaises(ConcurrentLaunchRefused):
                service.send_user_message(chat_id, "refused")
            service.abandon(chat_id)
            self.assertEqual(service.send_user_message(chat_id, "after")["messages"][-1]["text"],
                             "answer to: after")
            self.assert_store_valid(service.store, "d2-holder-killed-then-restart")
        finally:
            server.server_close()


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
