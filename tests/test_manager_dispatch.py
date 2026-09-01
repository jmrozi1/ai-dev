"""The supported production seam: one process serves, and dispatches behind it.

Every occupancy claim below is made through `manager_dispatch.main` against a real
coordination repository, a real product worktree with a real claim, and a real
binding store on disk. Only the process boundary is faked -- the same three
collaborators `session_lifecycle` already accepts as injectable -- because spawning
a provider is not what any of this is about. Nothing here composes a controller of
its own: a composition that only exists in a test is exactly the defect this rail
was opened to close.

Every count asserted below was fetched by a real HTTP client over a real loopback
socket from the real server the entry point built, at an instant the fixture can
name. Two instants matter and both are observed. The first is while the dispatched
agent's own request is genuinely in flight -- the process started, the handle is in
the controller's registry, and the binding is bound -- which is a window this
fixture opens by observing from inside the accepted `run_request` boundary rather
than by sleeping in one or by holding a finished worker open. The second is after
that session has been stopped and terminalized, while the page is still reachable,
because a count that was true once and then stopped being true must stop being
served.
"""

from __future__ import annotations

import ast
import contextlib
import http.client
import io
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ai_dev_flow import manager_controller as controller_module
from ai_dev_flow import decision_manager_web as web_module
from ai_dev_flow import manager_dispatch as dispatch_module
from ai_dev_flow import session_lifecycle, workspaces
from ai_dev_flow.authorization import CONCURRENCY_CEILING_DEFAULT
from ai_dev_flow.decision_manager_launch import QueueSourceContext
from ai_dev_flow.decision_manager_web import LOOPBACK_HOST, PAGE_PATH
from ai_dev_flow.manager_controller import REASON_OWNERSHIP_UNPROVABLE, ManagerController
from ai_dev_flow.manager_dispatch import (
    DispatchError,
    REASON_RUNTIME_UNSTATED,
    REASON_SURFACE_UNREACHABLE,
    main,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BindingStore,
    RailIteration,
    attach_process,
    build_record,
)
from ai_dev_flow.decision_queue import QUEUE_STATES
from ai_dev_flow.session_lifecycle import (
    STATE_DISCONNECTED,
    STATE_RUNNING,
    LifecycleError,
)
from ai_dev_flow.tickets import TicketReference

from tests.test_decision_manager_launch import (
    CLAIM_NONE_FLAG,
    PAYLOAD_CLOSE,
    PAYLOAD_OPEN,
    PROJECT,
    TICKET,
    _code_only,
)


ORCH_RAIL = "issue-55-orchestrator"
SOURCE_RAIL = "issue-55-agent-sdk-worker-integration"
SKILL = "orchestrator"
SESSION_ONE = "1a2b3c4d-0001-4000-8000-00000000000a"
FOREIGN_SESSION = "1a2b3c4d-0009-4000-8000-00000000000f"
STARTED_AT = "2026-08-26T12:00:02Z"
RESERVED_AT = "2026-08-26T12:00:01Z"

MODULE_SOURCE = Path(dispatch_module.__file__).read_text(encoding="utf-8")
MODULE_TREE = ast.parse(MODULE_SOURCE)
MODULE_CODE = _code_only(MODULE_SOURCE)
# The same module with its prose removed, so a structural claim anchors on
# what runs rather than on how a docstring happened to word it.
CODE_TREE = ast.parse(MODULE_CODE)


def payload_in(page: str) -> dict:
    opening = page.index(PAYLOAD_OPEN) + len(PAYLOAD_OPEN)
    closing = page.index(PAYLOAD_CLOSE, opening)
    return json.loads(page[opening:closing])


def fetched(server) -> dict:
    """One real HTTP request from a real client, and the payload it was served."""
    connection = http.client.HTTPConnection(
        LOOPBACK_HOST, server.server_address[1], timeout=5
    )
    try:
        connection.request("GET", PAGE_PATH)
        response = connection.getresponse()
        if response.status != 200:
            raise AssertionError("the surface answered {0}".format(response.status))
        return payload_in(response.read().decode("utf-8"))
    finally:
        connection.close()


class Looking(object):
    """Stands in for the person the page is served to: they look, then they close it.

    The real `Serving` runs underneath, so the socket loop, the requests, and the
    shutdown are all the accepted ones. What this replaces is only the part a test
    cannot otherwise reach -- `wait` blocks until a human ends the process -- and it
    ends that wait by fetching the page once, which is exactly what the human it
    stands in for would do.
    """

    def __init__(self, serving, server, look) -> None:
        self._serving = serving
        self._server = server
        self._look = look

    def answering(self) -> bool:
        return self._serving.answering()

    def wait(self) -> None:
        self._look(self._server)

    def stop(self) -> None:
        self._serving.stop()


class FakeHandle(object):
    """A worker handle with no process behind it. Nothing here spawns anything."""

    def __init__(self, pid=4242, pgid=4242):
        self.pid = pid
        self.pgid = pgid
        self.started_at = STARTED_AT


class DispatchTestBase(unittest.TestCase):
    """A published coordination repository, a claimed worktree, an on-disk store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="manager-dispatch-")
        self.addCleanup(self._cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()

        self.product = self._init_repo("product")
        self.reference = TicketReference(
            provider="github", ticket_id="55", repository="jmrozi1/ai-dev"
        )
        self.workspace, self.worktree_id = self._add_workspace("workspace-55")

        self.controller_root = self.tmp_path / "controller"
        self.prompt_file = self.controller_root / "prompts" / "orchestrator.md"
        self.prompt_file.parent.mkdir(parents=True)
        self.prompt_file.write_text("bounded orchestrator\n", encoding="utf-8")
        plugin = self.controller_root / "plugins" / "ai-dev-orchestrator"
        (plugin / "skills" / SKILL).mkdir(parents=True)
        (plugin / "skills" / SKILL / "SKILL.md").write_text(
            "---\nname: orchestrator\n---\n", encoding="utf-8"
        )
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "ai-dev-orchestrator"}) + "\n", encoding="utf-8"
        )
        self.plugin_root = plugin

        self.binding_root = self.tmp_path / "controller-state"
        self.store = BindingStore(self.binding_root)

        self.coordination = self._init_coordination()
        self.scope = self.coordination / PROJECT / TICKET
        self.write(self.scope / "state.md", "# Control Plane State\n")
        self.rail(ORCH_RAIL, role="orchestrator", status="running")
        self.rail(SOURCE_RAIL, role="executor", status="running")

        self.shutdown_reported = []
        self.served = []
        # Observers run inside the accepted `run_request` boundary, which is
        # the window in which the dispatched agent is genuinely working.
        self.while_the_agent_works = []
        self.last_seen = None

    def _cleanup(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.product), "worktree", "prune"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._tmp.cleanup()

    # -- repositories -----------------------------------------------------

    def _git(self, repo_root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args], check=True, text=True,
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Dispatch Tests")
        self._git(repo_root, "config", "user.email", "dispatch@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo_root, "add", "tracked.txt")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _add_workspace(self, name: str):
        path = self.tmp_path / name
        branch = "flow/{0}".format(name)
        self._git(self.product, "worktree", "add", "-q", "-b", branch, str(path), "main")
        worktree_id = workspaces.effective_worktree_id(path)
        workspaces.create_active_claim(
            path, reference=self.reference, worktree_id=worktree_id,
            workspace_path=path, branch=branch,
        )
        return path, worktree_id

    def _init_coordination(self) -> Path:
        """A published coordination repository: a remote, and a tracked branch.

        `build_snapshot` refuses a local worktree read on purpose, so the fixture
        has to be genuinely published for the production reader to resolve it.
        """
        coordination = self._init_repo("coordination")
        bare = self.tmp_path / "coordination.git"
        self._git(coordination, "init", "-q", "--bare", str(bare))
        self._git(coordination, "remote", "add", "origin", str(bare))
        self._git(coordination, "push", "-q", "-u", "origin", "main")
        return coordination

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def rail(self, rail_id: str, *, role: str, status: str, handoff=None) -> Path:
        path = self.write(
            self.scope / "rails" / rail_id / "rail.md",
            "# Rail: {0}\n\nStatus: {1}\nRole: {2}\nDepends on: none\n"
            "Shared resource: none\n\n## Goal\n\nbounded work\n".format(
                rail_id, status, role
            ),
        )
        if handoff is not None:
            self.write(self.scope / "rails" / rail_id / "handoff.md", handoff)
        return path

    def wake(self) -> None:
        """One material wake: a rail whose handoff proposes a different status."""
        self.write(
            self.scope / "rails" / SOURCE_RAIL / "handoff.md",
            "# Handoff\n\nStatus: completed\n\n## Delivered\n\nwork\n",
        )

    def publish(self) -> str:
        self._git(self.coordination, "add", "-A")
        self._git(self.coordination, "commit", "-q", "-m", "publish")
        self._git(self.coordination, "push", "-q", "origin", "main")
        return self._git(self.coordination, "rev-parse", "HEAD")

    # -- durable bindings this process did not start ----------------------

    def foreign_binding(self, *, state=BINDING_STATE_BOUND, session_id=FOREIGN_SESSION,
                        rail=SOURCE_RAIL):
        process: dict = {}
        if state == BINDING_STATE_BOUND:
            process = {
                "pid": 5150, "pid_domain": "another-host",
                "started_at": STARTED_AT, "bound_at": STARTED_AT,
            }
        record = build_record(
            project=PROJECT, ticket=TICKET,
            workspace_key=workspaces.canonical_ticket_key(self.reference),
            worktree_id=self.worktree_id, workspace_path=str(self.workspace),
            rail=rail, role="executor",
            iteration=RailIteration(rail=rail, blob=self.blob(rail)),
            session_id=session_id, launched_at_head="c" * 40,
            reserved_at=RESERVED_AT, state=state, **process
        )
        self.store.write_new(record)
        return record

    def blob(self, rail_id: str) -> str:
        return self._git(
            self.coordination, "hash-object", "--",
            str(self.scope / "rails" / rail_id / "rail.md"),
        )

    # -- the faked process boundary ---------------------------------------

    def _worker(self):
        handle = FakeHandle()

        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            bound = attach_process(
                store, reserved.session_id, pid=handle.pid, pid_domain="test-host",
                started_at=handle.started_at, bound_at="2026-08-26T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            return handle, bound

        def send(worker, request, *, prompt, markers=(), timeout=None):
            # The genuine live window. The process started, the handle is in the
            # controller's registry, and the binding is bound -- this is the agent's
            # own request being carried out, not a pause invented to be observed in.
            for observe in self.while_the_agent_works:
                observe()
            return {
                "type": "result", "session_id": request.session_id,
                "mode": request.mode, "subtype": "success", "is_error": False,
            }

        def shutdown(worker):
            self.shutdown_reported.append(worker.pgid)
            return {"process_group_gone": True, "graceful": True, "exit_code": 0}

        def alive(pgid):
            # Alive until this process proves it stopped it, which is what makes
            # the live window a window rather than an assumption.
            return pgid not in self.shutdown_reported

        return start, send, shutdown, alive

    @contextlib.contextmanager
    def rooted(self):
        """Every repository lookup resolves to this fixture's claimed worktree."""
        start, send, shutdown, alive = self._worker()
        with unittest.mock.patch.object(
            dispatch_module, "resolve_repo_root", lambda cwd=None: self.workspace
        ), unittest.mock.patch(
            "ai_dev_flow.decision_manager_launch.resolve_repo_root",
            lambda cwd=None: self.workspace,
        ), unittest.mock.patch.object(
            session_lifecycle, "start_worker", start
        ), unittest.mock.patch.object(
            session_lifecycle, "run_request", send
        ), unittest.mock.patch.object(
            session_lifecycle, "shutdown_worker", shutdown
        ), unittest.mock.patch.object(
            session_lifecycle, "process_group_alive", alive
        ):
            yield

    # -- invocation -------------------------------------------------------

    def argv(self, **overrides) -> list:
        stated = {
            "--orchestrator-rail": ORCH_RAIL,
            "--ticket-provider": "github",
            "--ticket-id": "55",
            "--ticket-repository": "jmrozi1/ai-dev",
            "--controller-root": str(self.controller_root),
            "--prompt-file": str(self.prompt_file),
            "--plugin-root": str(self.plugin_root),
            "--expected-skill": SKILL,
            "--max-turns": "3",
            "--max-budget-usd": "0.5",
            "--control-plane": str(self.coordination),
            "--project": PROJECT,
            "--ticket": TICKET,
            "--binding-root": str(self.binding_root),
        }
        stated.update(overrides)
        argv = [CLAIM_NONE_FLAG]
        for flag, value in stated.items():
            if value is not None:
                argv.extend([flag, value])
        argv.extend(["--allowed-tool", "Read", "--allowed-tool", "Glob"])
        return argv

    def serve(self, argv=None, *, looking=None):
        """Run the real entry point with its surface genuinely answering throughout.

        Nothing about the server is faked. The accepted socket loop is started by
        the accepted `start_serving`, on the server the entry point itself built,
        and every observation below is a real HTTP request against it. The only
        stand-in is the human: `wait` blocks forever in production, so here it
        fetches the page once -- after the dispatch has finished and been stopped,
        while the surface is still up -- and returns.
        """
        self.publish()
        served = self.served = []

        def last_look(server):
            self.last_seen = fetched(server)
            if looking is not None:
                looking(server)

        def start(server):
            served.append(server)
            return Looking(web_module.start_serving(server), server, last_look)

        with self.rooted(), unittest.mock.patch.object(
            dispatch_module, "start_serving", start
        ):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(self.argv() if argv is None else argv)
        return code, out.getvalue(), err.getvalue(), served

    def served_server(self):
        """The one server this entry point built, while it is still answering."""
        self.assertEqual(len(self.served), 1)
        return self.served[0]

    def agents_after(self) -> dict:
        """What a real client was told once the dispatch had ended."""
        return self.last_seen["agents"]


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


class OneControllerTests(DispatchTestBase):
    def test_the_entry_point_constructs_exactly_one_manager_controller(self) -> None:
        self.wake()
        built = []

        def spy(*args, **kwargs):
            controller = ManagerController(*args, **kwargs)
            built.append(controller)
            return controller

        with unittest.mock.patch.object(dispatch_module, "ManagerController", spy):
            code, _out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(len(built), 1)
        self.assertEqual(len(served), 1)

    def test_the_dispatch_receives_this_controller_s_exact_store_and_registry(self) -> None:
        """Not equivalent ones. The same objects, by identity."""
        self.wake()
        built = []
        seen = []
        real = controller_module.invoke_orchestrator

        def controller_spy(*args, **kwargs):
            controller = ManagerController(*args, **kwargs)
            built.append(controller)
            return controller

        def invocation_spy(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)

        with unittest.mock.patch.object(
            dispatch_module, "ManagerController", controller_spy
        ), unittest.mock.patch.object(
            controller_module, "invoke_orchestrator", invocation_spy
        ):
            code, _out, _err, _served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        controller = built[0]
        self.assertIs(seen[0]["store"], controller.store)
        self.assertIs(seen[0]["registry"], controller.registry)
        # And the ceiling the dispatch was admitted against is the one drawn.
        self.assertEqual(seen[0]["slots"].ceiling, controller.ceiling)

    def test_only_one_store_and_one_registry_exist_in_the_whole_run(self) -> None:
        """A second ownership system is the defect, not an implementation detail."""
        self.wake()
        stores, registries = [], []
        real_store = controller_module.BindingStore
        real_registry = controller_module.SessionRegistry

        def store_spy(*args, **kwargs):
            built = real_store(*args, **kwargs)
            stores.append(built)
            return built

        def registry_spy(*args, **kwargs):
            built = real_registry(*args, **kwargs)
            registries.append(built)
            return built

        with unittest.mock.patch.object(
            controller_module, "BindingStore", store_spy
        ), unittest.mock.patch.object(
            controller_module, "SessionRegistry", registry_spy
        ):
            code, _out, _err, _served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(len(stores), 1)
        self.assertEqual(len(registries), 1)


# --------------------------------------------------------------------------
# The reading, through the production entry point
# --------------------------------------------------------------------------


class LiveOccupancyTests(DispatchTestBase):
    def test_a_real_client_reads_the_live_count_while_that_session_is_running(self) -> None:
        """The point of this rail: a true number, fetched while the agent is working.

        One HTTP request, from a real client, made while the dispatched agent's own
        request is in flight. At that same instant the fixture reads the two things
        the page is claiming about: the controller's own occupancy, and the durable
        binding's state. All three have to agree, and the binding has to still be
        nonterminal -- otherwise the page is describing something that has ended.
        """
        self.wake()
        built = []
        seen = {}

        def controller_spy(*args, **kwargs):
            controller = ManagerController(*args, **kwargs)
            built.append(controller)
            return controller

        def observe() -> None:
            seen["page"] = fetched(self.served_server())["agents"]
            seen["controller"] = built[0].agent_count()
            seen["nonterminal"] = [
                record.session_id
                for record in self.store.records()
                if not record.is_terminal
            ]
            seen["owned"] = built[0].owned_session_ids()

        self.while_the_agent_works.append(observe)

        with unittest.mock.patch.object(
            dispatch_module, "ManagerController", controller_spy
        ):
            code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            seen["page"],
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 1, "reason": None},
        )
        self.assertEqual(seen["controller"]["current"], 1)
        self.assertEqual(len(seen["nonterminal"]), 1)
        self.assertEqual(seen["owned"], tuple(seen["nonterminal"]))
        self.assertIn("live occupancy: 1 / 6", out)
        self.assertEqual(self.shutdown_reported, [4242])
        self.assertEqual(len(served), 1)

    def test_the_count_stops_being_served_once_the_session_it_counted_is_gone(self) -> None:
        """The other half. A number that was true must not keep being served after."""
        self.wake()
        live = {}

        self.while_the_agent_works.append(
            lambda: live.setdefault("agents", fetched(self.served_server())["agents"])
        )

        code, _out, _err, _served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(live["agents"]["current"], 1)
        # Same server, same client, one dispatch later: the slot is free and the
        # page says so rather than repeating what it said while it was full.
        self.assertEqual(
            self.agents_after(),
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 0, "reason": None},
        )
        self.assertEqual(self.shutdown_reported, [4242])

    def test_the_dispatch_is_refused_when_the_surface_cannot_answer(self) -> None:
        """The ordering is a precondition, not a convention a later edit can reverse."""
        self.wake()

        class Unreachable(object):
            def answering(self):
                return False

            def wait(self):
                raise AssertionError("nothing should be waited on")

            def stop(self):
                return None

        self.publish()
        with self.rooted(), unittest.mock.patch.object(
            dispatch_module,
            "start_serving",
            lambda server: Unreachable(),
        ):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(DispatchError) as caught:
                    main(self.argv())

        self.assertEqual(caught.exception.reason, REASON_SURFACE_UNREACHABLE)
        # Refused before anything was enacted: no session was started or stopped.
        self.assertEqual(self.shutdown_reported, [])
        self.assertEqual(self.store.records(), [])

    def test_the_count_drawn_is_the_count_of_the_session_this_process_started(self) -> None:
        """Kill power: take the ownership half away and the number must not survive.

        The store is identical, the durable record is identical, and the dispatch
        is the same dispatch; only the handle this controller held is gone. A
        surface that still drew `1 / 6` here never needed ownership at all.
        """
        self.wake()
        live = {}
        real_invoke = controller_module.invoke_orchestrator

        def forget_the_handle(*args, **kwargs):
            observer = kwargs.pop("while_running")

            def drop_then_observe(launched):
                kwargs["registry"].remove(launched.binding.session_id)
                live["agents"] = fetched(self.served_server())["agents"]
                observer(launched)

            return real_invoke(*args, while_running=drop_then_observe, **kwargs)

        with unittest.mock.patch.object(
            controller_module, "invoke_orchestrator", forget_the_handle
        ):
            # Losing the handle also means the accepted lifecycle can no longer
            # stop what it started, and it says so rather than terminalizing on
            # trust. That refusal is the control working, not a second finding.
            with self.assertRaises(LifecycleError):
                self.serve()

        self.assertIsNone(live["agents"]["current"])
        self.assertEqual(live["agents"]["reason"], REASON_OWNERSHIP_UNPROVABLE)

    def test_an_empty_scope_still_serves_an_established_zero(self) -> None:
        """No material wake is a fact about this head, not a reason to say nothing."""
        code, out, _err, _served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.agents_after(),
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 0, "reason": None},
        )
        self.assertIn("live occupancy: 0 / 6", out)
        self.assertIn("no dispatch this run", out)

    def test_a_binding_this_controller_did_not_start_stays_unprovable(self) -> None:
        """Fail closed. Nothing is adopted by pid, by record, or by resemblance."""
        self.foreign_binding()

        code, out, _err, _served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.agents_after(),
            {
                "permitted": CONCURRENCY_CEILING_DEFAULT,
                "current": None,
                "reason": REASON_OWNERSHIP_UNPROVABLE,
            },
        )
        self.assertIn("not established ({0})".format(REASON_OWNERSHIP_UNPROVABLE), out)

    def test_what_this_process_observed_and_what_a_client_read_are_one_reading(self) -> None:
        """The printed figure is this run's own record; it must not contradict the page."""
        self.wake()
        live = {}

        self.while_the_agent_works.append(
            lambda: live.setdefault("agents", fetched(self.served_server())["agents"])
        )

        _code, out, _err, _served = self.serve()

        self.assertIn(
            "live occupancy: {0} / {1}".format(
                live["agents"]["current"], live["agents"]["permitted"]
            ),
            out,
        )


# --------------------------------------------------------------------------
# The rest of the surface is untouched
# --------------------------------------------------------------------------


class QueuePreservationTests(DispatchTestBase):
    """The queue is still the page, and it is still this one run's snapshot of it.

    Only the agent count follows the clock, and it has to: it is the one figure
    whose subject changes underneath the page. Everything else here describes
    durable state read from the coordination repository, which this run reads once
    -- reading it again per request would be a fetch per request, which is the
    polling loop this surface is not allowed to become.
    """

    def test_the_decision_queue_is_still_drawn_beside_the_count(self) -> None:
        self.foreign_binding()

        _code, out, _err, _served = self.serve()

        payload = self.last_seen

        # The queue is still the page: every row, every state, and every row's
        # detail, with the count arriving as one more aggregate value beside the
        # allowance windows rather than as a surface of its own.
        self.assertEqual([row["title"] for row in payload["rows"]], [SOURCE_RAIL])
        self.assertEqual(payload["states"], list(QUEUE_STATES))
        self.assertEqual(
            sorted(payload["details"]), sorted(row["itemId"] for row in payload["rows"])
        )
        self.assertTrue(payload["allowance"])
        self.assertIn("agents", payload)
        self.assertIn("no dispatch this run", out)

    def test_the_row_and_the_aggregate_rest_on_the_same_registry(self) -> None:
        """One piece of ownership evidence answers the row and the count.

        The binding is real and durable and this controller did not start it. The
        registry cannot prove a handle for it, so the row cannot be drawn Running
        and the count cannot be established -- one absent fact, two answers that
        agree, fetched together by one real client in one request.
        """
        self.foreign_binding()

        _code, _out, _err, _served = self.serve()

        self.assertEqual(
            [row["state"] for row in self.last_seen["rows"]], [STATE_DISCONNECTED]
        )
        self.assertIsNone(self.last_seen["agents"]["current"])
        self.assertEqual(
            self.last_seen["agents"]["reason"], REASON_OWNERSHIP_UNPROVABLE
        )

    def test_only_the_count_moves_between_two_requests(self) -> None:
        """The exact scope of what became live, stated as two real fetches.

        The rows, their details, the filters and the allowance windows are this
        run's projection and are identical in both answers. The agent count is not
        a projection of this run at all, and it is the only thing that differs.
        """
        self.wake()
        during = {}

        self.while_the_agent_works.append(
            lambda: during.setdefault("payload", fetched(self.served_server()))
        )

        _code, _out, _err, _served = self.serve()

        for key in ("rows", "details", "allowance", "states", "defaultFilters"):
            with self.subTest(key=key):
                self.assertEqual(during["payload"][key], self.last_seen[key])
        self.assertEqual(during["payload"]["agents"]["current"], 1)
        self.assertEqual(self.last_seen["agents"]["current"], 0)


class AdmissionPreservationTests(DispatchTestBase):
    def test_a_full_ceiling_refuses_the_dispatch_and_still_serves(self) -> None:
        """Checkpoint 45's admission semantics decide; this composition adds none."""
        self.wake()
        # One reservation per reconciled rail, so the only thing under test is the
        # ceiling: a shared rail would collide on queue identity instead.
        for index in range(CONCURRENCY_CEILING_DEFAULT):
            filler = "issue-55-filler-{0}".format(index)
            self.rail(filler, role="executor", status="running")
            self.foreign_binding(
                state=BINDING_STATE_RESERVED,
                session_id="1a2b3c4d-00{0:02d}-4000-8000-00000000000b".format(index),
                rail=filler,
            )

        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(len(served), 1)
        self.assertIn("concurrency-ceiling-reached", out)
        # Reservations occupy slots on durable evidence alone, so the ceiling is
        # reported as reached rather than as unprovable.
        self.assertEqual(
            self.agents_after(),
            {
                "permitted": CONCURRENCY_CEILING_DEFAULT,
                "current": CONCURRENCY_CEILING_DEFAULT,
                "reason": None,
            },
        )
        self.assertEqual(self.shutdown_reported, [])


# --------------------------------------------------------------------------
# Stated inputs, and nothing inferred
# --------------------------------------------------------------------------


class StatedInputTests(DispatchTestBase):
    def test_unstated_runtime_policy_refuses_before_anything_is_read(self) -> None:
        code, _out, err, served = self.serve(
            argv=self.argv(**{"--prompt-file": None})
        )

        self.assertEqual(code, 1)
        self.assertEqual(served, [])
        self.assertIn(REASON_RUNTIME_UNSTATED, err)

    def test_the_accepted_claim_rule_is_still_the_only_one(self) -> None:
        stated = [item for item in self.argv() if item != CLAIM_NONE_FLAG]

        code, _out, err, served = self.serve(argv=stated)

        self.assertEqual(code, 1)
        self.assertEqual(served, [])
        self.assertIn("exclusivity-claim-unstated", err)

    def test_a_runtime_bound_that_is_not_a_number_is_a_stated_refusal(self) -> None:
        with self.assertRaises(DispatchError):
            dispatch_module.stated_dispatch_inputs(
                [item if item != "3" else "three" for item in self.argv()]
            )


# --------------------------------------------------------------------------
# The composition adds no authority
# --------------------------------------------------------------------------


class NoAddedAuthorityTests(unittest.TestCase):
    def test_the_module_adopts_no_process_and_discovers_no_ownership(self) -> None:
        for forbidden in (
            "getpid", "psutil", "kill(", "process_group_alive", "OwnedSession(",
            "adopt", "pid", "environ", "json.dump",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_adds_no_daemon_service_poller_or_scheduler(self) -> None:
        for forbidden in (
            "threading", "Thread(", "asyncio", "socketserver", "while ",
            "sched", "Timer(", "poll(", "sleep(", "signal", "socket(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_builds_no_second_store_or_registry(self) -> None:
        for forbidden in ("BindingStore", "SessionRegistry", "reconcile_agent_slots"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_reduction_still_has_exactly_one_production_home(self) -> None:
        repo_root = Path(dispatch_module.__file__).parents[1]
        callers = []
        for source in sorted(repo_root.rglob("*.py")):
            relative = source.relative_to(repo_root)
            if relative.parts[0] in ("tests", ".git"):
                continue
            if "reconcile_agent_slots(" in source.read_text(
                encoding="utf-8", errors="replace"
            ):
                callers.append(relative.as_posix())
        self.assertEqual(
            callers, ["ai_dev_flow/authorization.py", "ai_dev_flow/manager_controller.py"]
        )

    def test_the_surface_is_opened_before_the_dispatch_it_counts(self) -> None:
        """Structural: the ordering that was the defect is the ordering of the code.

        `open_surface` is what builds and starts the server, `dispatch_behind`
        cannot run without one that is answering, and `main` calls them in that
        order. An edit that put the dispatch first would have to delete one of
        these three facts to do it.
        """
        composition = {
            node.name: node
            for node in ast.walk(CODE_TREE)
            if isinstance(node, ast.FunctionDef)
            and node.name in ("open_surface", "dispatch_behind", "main")
        }
        self.assertEqual(
            sorted(composition), ["dispatch_behind", "main", "open_surface"]
        )

        opening = ast.dump(composition["open_surface"])
        for required in ("queue", "serve", "start_serving"):
            with self.subTest(required=required):
                self.assertIn(required, opening)
        # Opening a surface may not dispatch anything: that is the whole ordering.
        self.assertNotIn("dispatch", opening)

        behind = ast.dump(composition["dispatch_behind"])
        for required in ("answering", "REASON_SURFACE_UNREACHABLE", "while_running"):
            with self.subTest(required=required):
                self.assertIn(required, behind)
        # And it may not build one, so it can never dispatch behind its own page.
        self.assertNotIn("start_serving", behind)

        called = [
            node.func.id
            for node in ast.walk(composition["main"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertLess(called.index("open_surface"), called.index("dispatch_behind"))

    def test_the_reading_the_page_serves_is_taken_when_a_client_asks(self) -> None:
        """Structural: the controller hands the server a way to read, not a reading."""
        serve = {
            node.name: node
            for node in ast.walk(ast.parse(_code_only(
                Path(controller_module.__file__).read_text(encoding="utf-8")
            )))
            if isinstance(node, ast.FunctionDef) and node.name == "serve"
        }["serve"]
        taken = [
            node
            for node in ast.walk(serve)
            if isinstance(node, ast.Lambda)
            and "agent_count" in ast.dump(node)
        ]
        self.assertEqual(len(taken), 1)
        self.assertIn("make_live_manager_server", ast.dump(serve))

    def test_every_public_name_is_exported(self) -> None:
        public = {
            node.name
            for node in MODULE_TREE.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
        self.assertTrue(public.issubset(set(dispatch_module.__all__)))


if __name__ == "__main__":
    unittest.main()
