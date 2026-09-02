"""The controller-owned manager surface: one registry, one store, one honest count."""

from __future__ import annotations

import ast
import contextlib
import http.client
import io
import json
import unittest
import unittest.mock
from pathlib import Path

from ai_dev_flow import decision_manager_launch as launch
from ai_dev_flow import manager_controller as controller_module
from ai_dev_flow.claude_allowance_store import AllowanceStore
from ai_dev_flow.decision_manager import ManagerRun
from ai_dev_flow.decision_manager_launch import QueueSourceContext
from ai_dev_flow.decision_manager_web import LOOPBACK_HOST, PAGE_PATH, start_serving
from ai_dev_flow.manager_controller import (
    REASON_OWNERSHIP_UNPROVABLE,
    ManagerController,
    main,
)
from ai_dev_flow.session_binding import BINDING_STATE_RESERVED
from ai_dev_flow.session_lifecycle import STATE_DISCONNECTED, STATE_RUNNING, SessionRegistry

from tests.test_decision_manager_launch import (
    CLAIM_NONE_FLAG,
    _code_only,
    LIVE_RAIL,
    PAYLOAD_CLOSE,
    PAYLOAD_OPEN,
    SESSION,
    SourcedLaunchTestCase,
    a_queue,
)
from tests.test_session_lifecycle import LifecycleTestBase
from tests.test_session_lifecycle import SESSION as LIFECYCLE_SESSION


MODULE_SOURCE = Path(controller_module.__file__).read_text(encoding="utf-8")
MODULE_TREE = ast.parse(MODULE_SOURCE)
MODULE_CODE = _code_only(MODULE_SOURCE)
ALWAYS_ALIVE = lambda pgid: True  # noqa: E731 - a prober, not a policy


def payload_in(page: str) -> dict:
    opening = page.index(PAYLOAD_OPEN) + len(PAYLOAD_OPEN)
    closing = page.index(PAYLOAD_CLOSE, opening)
    return json.loads(page[opening:closing])


def fetched(port: int) -> str:
    """One real HTTP request, from a real client, over a real loopback socket."""
    connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=5)
    try:
        connection.request("GET", PAGE_PATH)
        return connection.getresponse().read().decode("utf-8")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# A real launch, counted by the controller that made it
# --------------------------------------------------------------------------


class ControllerLaunchOwnershipTests(LifecycleTestBase):
    """The seam checkpoint 44 was missing: the launcher and the renderer are one.

    Nothing is stubbed between the launch and the count. The session is started
    through the accepted lifecycle into this controller's own registry, and the
    same controller then reduces and draws it.
    """

    def _controller(self, registry=None) -> ManagerController:
        source = QueueSourceContext(
            control_plane=self.tmp_path / "coordination",
            project="ai-dev",
            ticket="issue-55",
            binding_root=self.store.root,
        )
        return ManagerController(
            source, registry=self.registry if registry is None else registry
        )

    def _launch_through(self, controller: ManagerController):
        start, worker = self._starter()
        send, _sent = self._sender()
        return controller.launch(
            self._decision(),
            self.assignment,
            reference=self.reference,
            request_kwargs=self._request_kwargs(),
            prompt="do the work",
            package_root=self.repo_root,
            now=lambda: self.clock,
            new_session_id=lambda: LIFECYCLE_SESSION,
            start=start,
            send=send,
        )

    def _run(self) -> ManagerRun:
        return ManagerRun(
            store=AllowanceStore(self.tmp_path / "allowance.json"),
            now=1_800_000_000,
            human_exclusive_since=None,
        )

    def test_a_session_launched_through_the_controller_is_counted_by_it(self) -> None:
        controller = self._controller()

        outcome = self._launch_through(controller)

        self.assertEqual(controller.owned_session_ids(), (outcome.binding.session_id,))
        self.assertEqual(
            controller.agent_count(alive=ALWAYS_ALIVE),
            {"permitted": 6, "current": 1, "reason": None},
        )

    def test_the_page_draws_the_count_of_the_launch_the_controller_owns(self) -> None:
        """End to end on the supported path: launch, then render, one controller."""
        controller = self._controller()
        self._launch_through(controller)
        view, details = a_queue()

        page = controller.page(self._run(), view, details, alive=ALWAYS_ALIVE)

        self.assertEqual(
            payload_in(page)["agents"],
            {"permitted": 6, "current": 1, "reason": None},
        )

    def test_a_controller_that_did_not_start_the_session_reports_the_reason(self) -> None:
        """Kill power: remove the ownership half and the count must not survive.

        The store is identical and the record is identical; only the registry that
        proved the handle is gone. A count that still read `1 / 6` here would be a
        count that never needed ownership at all.
        """
        self._launch_through(self._controller())

        stranger = self._controller(registry=SessionRegistry())
        reading = stranger.agent_count(alive=ALWAYS_ALIVE)

        self.assertIsNone(reading["current"])
        self.assertEqual(reading["reason"], REASON_OWNERSHIP_UNPROVABLE)

    def test_a_stopped_session_frees_the_slot_it_held(self) -> None:
        controller = self._controller()
        outcome = self._launch_through(controller)
        # Alive while the handle is being proved, gone once shutdown reports it, so
        # the accepted lifecycle's before-and-after checks both see the truth.
        probes = []

        def alive(pgid):
            probes.append(pgid)
            return len(probes) == 1

        controller.stop(
            outcome.binding,
            stop=lambda handle: {"process_group_gone": True, "graceful": True,
                                "exit_code": 0},
            alive=alive,
        )

        self.assertEqual(controller.owned_session_ids(), ())
        self.assertEqual(
            controller.agent_count(alive=ALWAYS_ALIVE),
            {"permitted": 6, "current": 0, "reason": None},
        )

    def test_a_real_client_reads_a_live_count_and_then_reads_the_truth_after_it(self) -> None:
        """The serve-time property, both halves, through one server and one client.

        While the session this controller started is genuinely running, a real
        HTTP client is told `1 / 6` -- and the durable binding really is nonterminal
        at that instant. After the accepted lifecycle stops it, the very same server
        tells the next client the slot is free. Nothing is slept on and nothing is
        held open: the occupancy simply changed, and the page followed it.
        """
        controller = self._controller()
        outcome = self._launch_through(controller)
        view, details = a_queue()

        server = controller.serve(self._run(), view, details, alive=ALWAYS_ALIVE)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        port = server.server_address[1]

        live = payload_in(fetched(port))["agents"]
        self.assertEqual(live, {"permitted": 6, "current": 1, "reason": None})
        self.assertFalse(self.store.read(outcome.binding.session_id).is_terminal)
        self.assertEqual(controller.agent_count(alive=ALWAYS_ALIVE)["current"], 1)

        probes = []

        def alive(pgid):
            probes.append(pgid)
            return len(probes) == 1

        controller.stop(
            outcome.binding,
            stop=lambda handle: {"process_group_gone": True, "graceful": True,
                                 "exit_code": 0},
            alive=alive,
        )

        self.assertTrue(self.store.read(outcome.binding.session_id).is_terminal)
        self.assertEqual(
            payload_in(fetched(port))["agents"],
            {"permitted": 6, "current": 0, "reason": None},
        )

    def test_the_controller_launches_into_the_store_it_counts(self) -> None:
        """One store, so the ceiling that admitted the work is the one drawn."""
        controller = self._controller()
        outcome = self._launch_through(controller)

        self.assertEqual(controller.store.root, self.store.root)
        self.assertIsNotNone(self.store.read(outcome.binding.session_id))


class ControllerDispatchTests(LifecycleTestBase):
    """`dispatch` hands the accepted invocation this controller's own two halves.

    The gates themselves are `orchestrator_invocation`'s and are tested there. What
    is proved here is the only thing this method adds: the objects it passes are
    this controller's, by identity, and the occupancy it states is reconciled from
    those same objects.
    """

    def _controller(self) -> ManagerController:
        source = QueueSourceContext(
            control_plane=self.tmp_path / "coordination",
            project="ai-dev",
            ticket="issue-55",
            binding_root=self.store.root,
        )
        return ManagerController(source, registry=self.registry)

    def _dispatch(self, controller, **overrides):
        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            seen["positional"] = args
            return "an outcome"

        with unittest.mock.patch.object(controller_module, "invoke_orchestrator", spy):
            controller.dispatch(
                "snapshot", "proposal", "packet", "observation",
                orchestrator_rail="issue-55-orchestrator", **overrides
            )
        return seen

    def test_the_dispatch_is_given_this_controller_s_exact_store_and_registry(self) -> None:
        controller = self._controller()

        seen = self._dispatch(controller)

        self.assertIs(seen["store"], controller.store)
        self.assertIs(seen["registry"], controller.registry)

    def test_the_occupancy_stated_is_reconciled_from_those_same_two_halves(self) -> None:
        """The ceiling a dispatch is admitted against is the one the page draws."""
        controller = self._controller()

        seen = self._dispatch(controller)

        self.assertEqual(seen["slots"].ceiling, controller.ceiling)
        self.assertEqual(seen["slots"], controller.occupancy(controller.store.records()))
        self.assertEqual(seen["in_flight_session_ids"], controller.registry.in_flight())

    def test_the_bindings_stated_are_the_records_the_count_was_reduced_from(self) -> None:
        """One read of the store, so admission and the ceiling cannot disagree."""
        controller = self._controller()

        seen = self._dispatch(controller)

        self.assertEqual(
            [record.session_id for record in seen["bindings"]],
            [record.session_id for record in controller.store.records()],
        )

    def test_the_observation_seam_reaches_the_accepted_invocation_unchanged(self) -> None:
        drawn = []

        def draw(launched):
            drawn.append(launched)

        controller = self._controller()

        seen = self._dispatch(controller, while_running=draw)

        self.assertIs(seen["while_running"], draw)

    def test_it_supplies_no_authorization_decision_of_its_own(self) -> None:
        """Adding a gate here would be a second place a launch could be permitted."""
        controller = self._controller()

        seen = self._dispatch(controller)

        for forbidden in ("decision", "authorized", "authorize"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, seen)


# --------------------------------------------------------------------------
# The entry point reads one real durable scope
# --------------------------------------------------------------------------


class ControllerEntryPointTests(SourcedLaunchTestCase):
    """The controller-owned surface, driven through its own production entry."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def serve(self, argv=None, *, claim=(CLAIM_NONE_FLAG,)):
        served = []
        stated = list(claim) + (self.source_argv() if argv is None else list(argv))
        with self.rooted(), unittest.mock.patch.object(
            controller_module, "serve_forever", served.append
        ):
            code, out, err = self.run_main(stated)
        for server in served:
            self.addCleanup(server.server_close)
        return code, out, err, served

    def controller(self, registry=None) -> ManagerController:
        return ManagerController(
            self.context(), registry=self.registry if registry is None else registry
        )

    def test_an_empty_scope_serves_an_established_zero(self) -> None:
        """Zero is a count, and the surface says so rather than saying nothing."""
        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            payload_in(served[0].RequestHandlerClass.document())["agents"],
            {"permitted": 6, "current": 0, "reason": None},
        )
        self.assertIn("live occupancy: 0 / 6", out)

    def test_a_binding_this_controller_did_not_start_is_not_reported_as_zero(self) -> None:
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        code, out, _err, served = self.serve()

        self.assertEqual(code, 0)
        self.assertEqual(
            payload_in(served[0].RequestHandlerClass.document())["agents"],
            {"permitted": 6, "current": None, "reason": REASON_OWNERSHIP_UNPROVABLE},
        )
        self.assertIn("not established ({0})".format(REASON_OWNERSHIP_UNPROVABLE), out)

    def test_the_entry_point_states_how_many_handles_it_holds(self) -> None:
        _code, out, _err, _served = self.serve()

        self.assertIn("owned session handles: 0", out)

    def test_a_reservation_occupies_a_slot_with_no_handle_at_all(self) -> None:
        """A reservation is a launch already committed to, and it is provable."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, state=BINDING_STATE_RESERVED)

        reading = self.controller(registry=SessionRegistry()).agent_count()

        self.assertEqual(reading, {"permitted": 6, "current": 1, "reason": None})

    def test_the_rows_and_the_count_rest_on_the_same_registry(self) -> None:
        """One piece of ownership evidence answers the row and the aggregate."""
        self.authorize(LIVE_RAIL, "running")
        record = self.bind(LIVE_RAIL)
        self.own(record)
        owning = self.controller()

        view, _details = owning.queue(owning_run := self.a_run(), alive=ALWAYS_ALIVE)
        reading = owning.agent_count(alive=ALWAYS_ALIVE)

        self.assertEqual([row.state for row in view.rows], [STATE_RUNNING])
        self.assertEqual(reading, {"permitted": 6, "current": 1, "reason": None})

        stranger = self.controller(registry=SessionRegistry())
        stranger_view, _ = stranger.queue(owning_run, alive=ALWAYS_ALIVE)
        stranger_reading = stranger.agent_count(alive=ALWAYS_ALIVE)

        self.assertEqual([row.state for row in stranger_view.rows], [STATE_DISCONNECTED])
        self.assertIsNone(stranger_reading["current"])

    def test_a_source_refusal_reaches_no_server(self) -> None:
        """A refusal drawn as an empty page is the one outcome this must not produce."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL, blob="f" * 40)

        code, _out, err, served = self.serve()

        self.assertEqual(code, 2)
        self.assertEqual(served, [])
        self.assertNotIn("Traceback", err)

    def test_a_stated_claim_is_required_exactly_as_the_accepted_rule_says(self) -> None:
        code, _out, err, served = self.serve(claim=())

        self.assertEqual(code, 1)
        self.assertEqual(served, [])
        self.assertIn(launch.REASON_CLAIM_UNSTATED, err)

    def test_a_stated_scope_is_required_exactly_as_the_accepted_rule_says(self) -> None:
        code, _out, err, served = self.serve(argv=[])

        self.assertEqual(code, 1)
        self.assertEqual(served, [])
        self.assertIn(launch.REASON_SOURCE_UNSTATED, err)


# --------------------------------------------------------------------------
# One queue read is one liveness instant
# --------------------------------------------------------------------------


class ControllerQueueCoherenceTests(SourcedLaunchTestCase):
    """The supported boundary, on the facts that used to refuse the whole queue.

    Both halves of this controller consume the same liveness question. The
    aggregate asks it once and has always degraded gracefully; the queue asked it
    twice, with a control-plane subprocess between the two, and refused every row
    when the two answers disagreed. An ordinary worker exit is exactly that
    disagreement, so the failing case was the end of a normal run.

    These drive `ManagerController.queue`, not the pure model, because that is
    where the two observations lived.
    """

    class Flipping:
        """Would answer differently on each successive observation, if asked twice."""

        def __init__(self, answers):
            self.answers = list(answers)
            self.probes = []

        def __call__(self, pgid):
            self.probes.append(pgid)
            return self.answers[min(len(self.probes) - 1, len(self.answers) - 1)]

    def controller(self, registry=None) -> ManagerController:
        return ManagerController(
            self.context(), registry=self.registry if registry is None else registry
        )

    def a_live_rail(self):
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

    def test_a_stably_live_session_is_still_drawn_running(self) -> None:
        self.a_live_rail()
        prober = self.Flipping([True])

        view, _details = self.controller().queue(self.a_run(), alive=prober)

        self.assertEqual([row.state for row in view.rows], [STATE_RUNNING])

    def test_a_stably_dead_session_is_still_drawn_disconnected(self) -> None:
        self.a_live_rail()
        prober = self.Flipping([False])

        view, _details = self.controller().queue(self.a_run(), alive=prober)

        self.assertEqual([row.state for row in view.rows], [STATE_DISCONNECTED])

    def test_a_worker_exiting_mid_read_serves_a_row_instead_of_refusing(self) -> None:
        """`True` then `False` used to raise `lifecycle-refused` for the whole queue."""
        self.a_live_rail()
        prober = self.Flipping([True, False])
        controller = self.controller()

        view, _details = controller.queue(self.a_run(), alive=prober)

        self.assertEqual([row.state for row in view.rows], [STATE_RUNNING])
        self.assertEqual(len(prober.probes), 1)

    def test_a_worker_appearing_mid_read_serves_a_row_instead_of_refusing(self) -> None:
        """`False` then `True`, the direction that refused just as completely."""
        self.a_live_rail()
        prober = self.Flipping([False, True])

        view, _details = self.controller().queue(self.a_run(), alive=prober)

        self.assertEqual([row.state for row in view.rows], [STATE_DISCONNECTED])
        self.assertEqual(len(prober.probes), 1)

    def _both_halves_survive(self, answers):
        """`agent_count` never refused on these facts. Now neither does the queue."""
        self.a_live_rail()
        controller = self.controller()

        view, _details = controller.queue(self.a_run(), alive=self.Flipping(answers))
        reading = controller.agent_count(alive=self.Flipping(answers))

        self.assertEqual(len(view.rows), 1)
        self.assertEqual(reading["permitted"], 6)

    def test_neither_half_collapses_when_a_worker_exits_mid_read(self) -> None:
        self._both_halves_survive([True, False])

    def test_neither_half_collapses_when_a_worker_appears_mid_read(self) -> None:
        self._both_halves_survive([False, True])

    def test_a_controller_that_started_nothing_still_reports_unprovable(self) -> None:
        """Coherence did not turn a fail-closed aggregate into a confident count."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)
        stranger = self.controller(registry=SessionRegistry())

        view, _details = stranger.queue(self.a_run(), alive=self.Flipping([True, False]))
        reading = stranger.agent_count(alive=ALWAYS_ALIVE)

        self.assertEqual([row.state for row in view.rows], [STATE_DISCONNECTED])
        self.assertIsNone(reading["current"])
        self.assertEqual(reading["reason"], REASON_OWNERSHIP_UNPROVABLE)


# --------------------------------------------------------------------------
# The composition adds no authority
# --------------------------------------------------------------------------


class NoAddedAuthorityTests(unittest.TestCase):
    def test_the_module_adopts_no_process_and_makes_no_ownership_durable(self) -> None:
        for forbidden in (
            "getpid", "psutil", "kill(", "process_group_alive", "OwnedSession(",
            "adopt", "write_text", "json.dump", "open(", "environ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_adds_no_daemon_service_or_polling_loop(self) -> None:
        for forbidden in (
            "threading", "Thread(", "asyncio", "socketserver", "while ",
            "sched", "Timer(", "poll(", "sleep(", "signal",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_module_states_no_count_or_ceiling_rule_of_its_own(self) -> None:
        """The reconciler decides occupancy; nothing here recounts or repairs it."""
        for forbidden in ("is_terminal", "is_reserved", "+ 1", "- 1", "sorted("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, MODULE_CODE)

    def test_the_renderer_never_reaches_the_evidence_it_draws(self) -> None:
        """`decision_manager_web` receives reduced values and can check none of them."""
        web = Path(controller_module.__file__).with_name("decision_manager_web.py")
        text = _code_only(web.read_text(encoding="utf-8"))
        for forbidden in (
            "session_lifecycle", "session_binding", "authorization",
            "reconcile_agent_slots", "SessionRegistry", "BindingStore",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_reduction_has_exactly_one_production_home(self) -> None:
        """C1's defect was a second, unreachable one. There must not be two."""
        repo_root = Path(controller_module.__file__).parents[1]
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

    def test_the_aggregate_reaches_the_page_through_production_code_only(self) -> None:
        """Every route to the drawn reading runs inside the module, not in a test."""
        rendering = {
            node.name: ast.dump(node)
            for node in ast.walk(MODULE_TREE)
            if isinstance(node, ast.FunctionDef) and node.name in ("page", "serve", "main")
        }
        self.assertEqual(sorted(rendering), ["main", "page", "serve"])
        for name in ("page", "serve"):
            with self.subTest(name=name):
                self.assertIn("agent_count", rendering[name])
        self.assertIn("serve", rendering["main"])

    def test_every_public_name_is_exported(self) -> None:
        public = {
            node.name
            for node in MODULE_TREE.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        }
        self.assertTrue(public.issubset(set(controller_module.__all__)))


if __name__ == "__main__":
    unittest.main()
