"""The supported production seam: one process dispatches, and draws what it started.

Every occupancy claim below is made through `manager_dispatch.main` against a real
coordination repository, a real product worktree with a real claim, and a real
binding store on disk. Only the process boundary is faked -- the same three
collaborators `session_lifecycle` already accepts as injectable -- because spawning
a provider is not what any of this is about. Nothing here composes a controller of
its own: a composition that only exists in a test is exactly the defect this rail
was opened to close.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ai_dev_flow import manager_controller as controller_module
from ai_dev_flow import manager_dispatch as dispatch_module
from ai_dev_flow import session_lifecycle, workspaces
from ai_dev_flow.authorization import CONCURRENCY_CEILING_DEFAULT
from ai_dev_flow.decision_manager_launch import QueueSourceContext
from ai_dev_flow.manager_controller import REASON_OWNERSHIP_UNPROVABLE, ManagerController
from ai_dev_flow.manager_dispatch import (
    DispatchError,
    REASON_RUNTIME_UNSTATED,
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
from ai_dev_flow.session_lifecycle import STATE_RUNNING, LifecycleError
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


def payload_in(page: str) -> dict:
    opening = page.index(PAYLOAD_OPEN) + len(PAYLOAD_OPEN)
    closing = page.index(PAYLOAD_CLOSE, opening)
    return json.loads(page[opening:closing])


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

    def serve(self, argv=None):
        """Run the real entry point, capturing the server it would have served."""
        self.publish()
        served = []
        with self.rooted(), unittest.mock.patch.object(
            dispatch_module, "serve_forever", served.append
        ):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(self.argv() if argv is None else argv)
        for server in served:
            self.addCleanup(server.server_close)
        return code, out.getvalue(), err.getvalue(), served

    def agents_on(self, server) -> dict:
        return payload_in(server.RequestHandlerClass.page)["agents"]


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
    def test_a_running_controller_owned_session_is_counted_on_the_served_page(self) -> None:
        """The point of this rail: a number while an agent this process started runs."""
        self.wake()

        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.agents_on(served[0]),
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 1, "reason": None},
        )
        self.assertIn("live occupancy: 1 / 6", out)
        # It was a real dispatch: the session really started and really stopped.
        self.assertEqual(self.shutdown_reported, [4242])

    def test_the_count_drawn_is_the_count_of_the_session_this_process_started(self) -> None:
        """Kill power: take the ownership half away and the number must not survive.

        The store is identical, the durable record is identical, and the dispatch
        is the same dispatch; only the handle this controller held is gone. A
        surface that still drew `1 / 6` here never needed ownership at all.
        """
        self.wake()
        drawn = []
        real_invoke = controller_module.invoke_orchestrator
        real_server = controller_module.make_manager_server

        def capture(*args, **kwargs):
            server = real_server(*args, **kwargs)
            self.addCleanup(server.server_close)
            drawn.append(kwargs["agents"])
            return server

        def forget_the_handle(*args, **kwargs):
            observer = kwargs.pop("while_running")

            def drop_then_observe(launched):
                kwargs["registry"].remove(launched.binding.session_id)
                observer(launched)

            return real_invoke(*args, while_running=drop_then_observe, **kwargs)

        with unittest.mock.patch.object(
            controller_module, "invoke_orchestrator", forget_the_handle
        ), unittest.mock.patch.object(
            controller_module, "make_manager_server", capture
        ):
            # Losing the handle also means the accepted lifecycle can no longer
            # stop what it started, and it says so rather than terminalizing on
            # trust. That refusal is the control working, not a second finding.
            with self.assertRaises(LifecycleError):
                self.serve()

        self.assertEqual(len(drawn), 1)
        self.assertIsNone(drawn[0]["current"])
        self.assertEqual(drawn[0]["reason"], REASON_OWNERSHIP_UNPROVABLE)

    def test_an_empty_scope_still_serves_an_established_zero(self) -> None:
        """No material wake is a fact about this head, not a reason to say nothing."""
        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.agents_on(served[0]),
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 0, "reason": None},
        )
        self.assertIn("live occupancy: 0 / 6", out)
        self.assertIn("no dispatch this run", out)

    def test_a_binding_this_controller_did_not_start_stays_unprovable(self) -> None:
        """Fail closed. Nothing is adopted by pid, by record, or by resemblance."""
        self.foreign_binding()

        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.agents_on(served[0]),
            {
                "permitted": CONCURRENCY_CEILING_DEFAULT,
                "current": None,
                "reason": REASON_OWNERSHIP_UNPROVABLE,
            },
        )
        self.assertIn("not established ({0})".format(REASON_OWNERSHIP_UNPROVABLE), out)

    def test_the_printed_reading_and_the_drawn_reading_are_one_run(self) -> None:
        self.wake()

        _code, out, _err, served = self.serve()

        drawn = self.agents_on(served[0])
        self.assertIn(
            "live occupancy: {0} / {1}".format(drawn["current"], drawn["permitted"]), out
        )


# --------------------------------------------------------------------------
# The rest of the surface is untouched
# --------------------------------------------------------------------------


class QueuePreservationTests(DispatchTestBase):
    def test_the_decision_queue_is_still_drawn_beside_the_count(self) -> None:
        self.wake()

        _code, out, _err, served = self.serve()

        payload = payload_in(served[0].RequestHandlerClass.page)

        # The queue is still the page: every row, every state, and every row's
        # detail, with the count arriving as one more aggregate value beside the
        # allowance windows rather than as a surface of its own.
        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_RUNNING])
        self.assertEqual([row["title"] for row in payload["rows"]], [ORCH_RAIL])
        self.assertEqual(payload["states"], list(QUEUE_STATES))
        self.assertEqual(
            sorted(payload["details"]), sorted(row["itemId"] for row in payload["rows"])
        )
        self.assertTrue(payload["allowance"])
        self.assertIn("agents", payload)
        self.assertNotIn("no dispatch this run", out)

    def test_the_row_and_the_aggregate_rest_on_the_same_registry(self) -> None:
        """One piece of ownership evidence answers the row and the count."""
        self.wake()

        _code, _out, _err, served = self.serve()

        payload = payload_in(served[0].RequestHandlerClass.page)
        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_RUNNING])
        self.assertEqual(payload["agents"]["current"], 1)


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
            self.agents_on(served[0]),
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

    def test_the_page_is_drawn_inside_the_live_window(self) -> None:
        """Structural: the render is reachable only from the observer callback."""
        composition = {
            node.name: node
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.FunctionDef) and node.name in ("dispatch_and_serve", "draw")
        }
        self.assertEqual(sorted(composition), ["dispatch_and_serve", "draw"])
        drawn = ast.dump(composition["draw"])
        for required in ("queue", "serve", "agent_count"):
            with self.subTest(required=required):
                self.assertIn(required, drawn)
        self.assertIn("while_running", ast.dump(composition["dispatch_and_serve"]))

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
