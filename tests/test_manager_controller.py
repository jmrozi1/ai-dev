"""The controller-owned manager surface: one registry, one store, one honest count."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import http.client
import inspect
import io
import json
import unittest
import unittest.mock
from pathlib import Path

from ai_dev_flow import decision_manager_launch as launch
from ai_dev_flow import manager_controller as controller_module
from ai_dev_flow import queue_source as queue_source_module
from ai_dev_flow.attention_projection import (
    DISPOSITION_LIVE,
    ACTIVITY_CONTEXT_ROTATION,
    ACTIVITY_DISCONNECTED_RECOVERY,
    ACTIVITY_EXECUTOR_WORKING,
    ACTIVITY_ORCHESTRATOR_RECONCILING,
    ACTIVITY_REVIEWER_WORKING,
    OWNER_AGENT,
    OWNER_HUMAN,
    project_attention,
    session_evidence,
)
from ai_dev_flow.authorization import CONCURRENCY_CEILING_DEFAULT
from ai_dev_flow.claude_allowance_store import AllowanceStore
from ai_dev_flow.context_lifecycle import (
    EVENT_COMPACTION_OBSERVED,
    REASON_INVALID_THRESHOLD,
    ContextLifecycleError,
)
from ai_dev_flow.progress_store import ProgressStore
from ai_dev_flow.decision_manager import ManagerRun
from ai_dev_flow.decision_manager_launch import QueueSourceContext
from ai_dev_flow.decision_manager_web import LOOPBACK_HOST, PAGE_PATH, start_serving
from ai_dev_flow.manager_controller import (
    REASON_OWNERSHIP_UNPROVABLE,
    ManagerController,
    main,
)
from ai_dev_flow.queue_source import REASON_OWNERSHIP_CONTRADICTORY, QueueSourceError
from ai_dev_flow.session_binding import BINDING_STATE_RESERVED, BindingStore
from ai_dev_flow.session_lifecycle import (
    REASON_ROTATION_REQUIRES_RETIREMENT,
    LifecycleError,
    ownership_evidence,
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    SessionRegistry,
)

from tests.test_decision_manager_launch import (
    BLOCKED_RAIL,
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

# A second durable session on one rail: a replacement in flight, or -- when both
# are provably live -- the contradiction the projection refuses.
ROTATION_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"


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
            progress=ProgressStore(self.tmp_path, "ai-dev/issue-55/progress.json"),
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

    def test_a_genuinely_launched_session_is_the_evidence_executor_working_needs(self) -> None:
        """The other end of the reachability proof: the launch, not the fixture.

        `ProductionCompositionTests` drives the composition from a registry handle
        to a real client's screen. This drives the step before it -- a genuine
        `launch` through the accepted lifecycle, with the real store and the real
        registry -- and shows the evidence it produces is exactly the evidence that
        composition reduces to `executor-working`. The accepted chain is used
        rather than restated: the same `ownership_evidence` the reconciler consumes,
        the same `session_evidence` the queue source builds, and the same
        `project_attention` that decides the activity.

        So neither half rests on the other's fixture. Nothing between the launched
        process handle and the rendered activity is stood in for.
        """
        controller = self._controller()
        outcome = self._launch_through(controller)
        record = self.store.read(outcome.binding.session_id)

        ownership = ownership_evidence(
            controller.registry, [record], alive=ALWAYS_ALIVE
        )
        evidence = session_evidence(record, ownership)
        attention = project_attention(
            record.rail, status="running", has_decision=False, sessions=(evidence,)
        )

        self.assertEqual(record.role, "executor")
        self.assertIs(ownership[record.session_id], True)
        self.assertEqual(evidence.disposition, DISPOSITION_LIVE)
        self.assertEqual(attention.activity, ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(attention.attention_owner, OWNER_AGENT)

    def test_the_controller_launches_into_the_store_it_counts(self) -> None:
        """One store, so the ceiling that admitted the work is the one drawn."""
        controller = self._controller()
        outcome = self._launch_through(controller)

        self.assertEqual(controller.store.root, self.store.root)
        self.assertIsNotNone(self.store.read(outcome.binding.session_id))


class ControllerStopCategoryTests(LifecycleTestBase):
    """`ManagerController.stop` is teardown, and a rotation cannot be asked of it.

    The controller is the public door onto the accepted stop, so it is the door a
    replacement launcher would reach for first. These cases prove it stays open for
    the teardown it was accepted for and closed to anything rotation-shaped --
    decided, as everywhere else, by the session's own mark rather than by anything
    the caller says.
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

    def _mark(self, controller, count=6):
        controller.registry.observe_context_events(
            LIFECYCLE_SESSION,
            [
                {"event": EVENT_COMPACTION_OBSERVED,
                 "session_id": LIFECYCLE_SESSION,
                 "uuid": "{0:08d}-0000-4000-8000-000000000000".format(index)}
                for index in range(count)
            ],
        )

    def _stop_arguments(self):
        probes = []

        def alive(pgid):
            probes.append(pgid)
            return len(probes) == 1

        return {
            "stop": lambda handle: {"process_group_gone": True, "graceful": True,
                                    "exit_code": 0},
            "alive": alive,
        }

    # -- D. the accepted teardown path ----------------------------------------

    def test_case_d_teardown_of_an_unmarked_session_still_works(self) -> None:
        controller = self._controller()
        outcome = self._launch_through(controller)
        stopped = controller.stop(outcome.binding, **self._stop_arguments())
        self.assertTrue(stopped.process_group_gone)
        self.assertTrue(self.store.read(outcome.binding.session_id).is_terminal)
        self.assertEqual(controller.owned_session_ids(), ())

    # -- B. the controller door, closed to rotation ---------------------------

    def test_case_b_the_controller_refuses_to_stop_a_marked_session(self) -> None:
        controller = self._controller()
        outcome = self._launch_through(controller)
        self._mark(controller)
        with self.assertRaises(LifecycleError) as raised:
            controller.stop(outcome.binding, **self._stop_arguments())
        self.assertEqual(
            raised.exception.reason, REASON_ROTATION_REQUIRES_RETIREMENT
        )
        # Nothing was destroyed: the session still holds its slot and its work.
        self.assertFalse(self.store.read(outcome.binding.session_id).is_terminal)
        self.assertEqual(controller.owned_session_ids(), (LIFECYCLE_SESSION,))
        self.assertEqual(controller.agent_count(alive=ALWAYS_ALIVE)["current"], 1)

    def test_case_b_the_controller_will_not_forward_a_retirement_authorization(self) -> None:
        """The one door `**kwargs` would otherwise open, closed explicitly.

        Teardown is never the retirement gate, so this refuses before it reaches the
        lifecycle at all -- and it refuses on an unmarked session too, where the
        lifecycle itself would have let the stop through.
        """
        controller = self._controller()
        outcome = self._launch_through(controller)
        with self.assertRaises(LifecycleError) as raised:
            controller.stop(
                outcome.binding, _retirement=object(), **self._stop_arguments()
            )
        self.assertEqual(
            raised.exception.reason, REASON_ROTATION_REQUIRES_RETIREMENT
        )
        self.assertFalse(self.store.read(outcome.binding.session_id).is_terminal)
        self.assertEqual(controller.owned_session_ids(), (LIFECYCLE_SESSION,))

    def test_case_b_the_controller_adds_no_route_of_its_own_to_a_stop(self) -> None:
        """`stop` is still a pass-through: one call, to the accepted lifecycle."""
        source = inspect.getsource(controller_module.ManagerController.stop)
        self.assertEqual(source.count("stop_session("), 1)
        self.assertNotIn("shutdown_worker", source)
        self.assertNotIn("os.kill", source)


class ControllerContextLifecycleTests(LifecycleTestBase):
    """Compaction state is exposed as system lifecycle state, and only as that."""

    def _source(self) -> QueueSourceContext:
        return QueueSourceContext(
            control_plane=self.tmp_path / "coordination", project="ai-dev",
            ticket="issue-55", binding_root=self.store.root,
        )

    def _controller(self, **kwargs) -> ManagerController:
        kwargs.setdefault("registry", self.registry)
        return ManagerController(self._source(), **kwargs)

    def _run(self) -> ManagerRun:
        return ManagerRun(
            store=AllowanceStore(self.tmp_path / "allowance.json"),
            now=1_800_000_000,
            human_exclusive_since=None,
            progress=ProgressStore(self.tmp_path, "ai-dev/issue-55/progress.json"),
        )

    def _boundaries(self, count):
        return [
            {
                "event": "compaction-observed",
                "session_id": LIFECYCLE_SESSION,
                "uuid": "{0:08d}-0000-4000-8000-000000000000".format(index),
            }
            for index in range(count)
        ]

    def _launch_with(self, controller, boundaries):
        start, _worker = self._starter()

        def send(handle, request, *, prompt, markers=(), timeout=None):
            return {
                "type": "result", "session_id": request.session_id, "mode": request.mode,
                "subtype": "success", "is_error": False, "events": self._boundaries(boundaries),
            }

        return controller.launch(
            self._decision(), self.assignment, reference=self.reference,
            request_kwargs=self._request_kwargs(), prompt="do the work",
            package_root=self.repo_root, now=lambda: self.clock,
            new_session_id=lambda: LIFECYCLE_SESSION, start=start, send=send,
        )

    def test_the_controller_reports_health_count_and_whether_rotation_is_marked(self) -> None:
        controller = self._controller()
        self._launch_with(controller, 2)
        reading = controller.context_lifecycle()[LIFECYCLE_SESSION]
        self.assertEqual(reading["health"], "healthy-complete-from-session-start")
        self.assertEqual(reading["count"], 2)
        self.assertEqual(reading["threshold"], 6)
        self.assertIs(reading["rotationMarked"], False)

    def test_reaching_the_threshold_marks_the_session_and_stops_nothing(self) -> None:
        controller = self._controller()
        outcome = self._launch_with(controller, 6)
        self.assertEqual(controller.rotation_marked_session_ids(), (LIFECYCLE_SESSION,))
        self.assertIs(
            controller.context_lifecycle()[LIFECYCLE_SESSION]["rotationMarked"], True
        )
        # The session is still owned, still bound, and still occupying its slot.
        self.assertEqual(controller.owned_session_ids(), (LIFECYCLE_SESSION,))
        self.assertEqual(
            controller.agent_count(alive=ALWAYS_ALIVE),
            {"permitted": 6, "current": 1, "reason": None},
        )
        self.assertFalse(outcome.binding.is_terminal)

    def test_a_threshold_mark_adds_no_queue_row_and_no_human_attention(self) -> None:
        controller = self._controller()
        self._launch_with(controller, 6)
        view, details = a_queue()
        before = controller.page(self._run(), view, details, alive=ALWAYS_ALIVE)

        payload = payload_in(before)
        self.assertNotIn("rotationMarked", json.dumps(payload))
        self.assertNotIn("compaction", before.lower())

    def test_the_rotation_threshold_and_the_concurrency_ceiling_are_separate(self) -> None:
        """Both default to six. The identical number is a coincidence, not a wire."""
        controller = self._controller()
        self.assertEqual(controller.ceiling, CONCURRENCY_CEILING_DEFAULT)
        self.assertEqual(controller.rotation_threshold, 6)

        narrow_ceiling = ManagerController(self._source(), ceiling=2, registry=SessionRegistry())
        self.assertEqual(narrow_ceiling.ceiling, 2)
        self.assertEqual(narrow_ceiling.rotation_threshold, 6)

        early_rotation = ManagerController(self._source(), rotation_threshold=2)
        self.assertEqual(early_rotation.rotation_threshold, 2)
        self.assertEqual(early_rotation.ceiling, CONCURRENCY_CEILING_DEFAULT)

    def test_an_early_rotation_threshold_does_not_narrow_the_ceiling(self) -> None:
        controller = ManagerController(
            self._source(), rotation_threshold=2,
            registry=SessionRegistry(rotation_threshold=2),
        )
        self._launch_with(controller, 2)
        self.assertEqual(controller.rotation_marked_session_ids(), (LIFECYCLE_SESSION,))
        self.assertEqual(
            controller.agent_count(alive=ALWAYS_ALIVE)["permitted"], CONCURRENCY_CEILING_DEFAULT
        )

    def test_a_registry_and_a_controller_may_not_state_two_rotation_policies(self) -> None:
        with self.assertRaises(ContextLifecycleError) as caught:
            ManagerController(
                self._source(), rotation_threshold=3,
                registry=SessionRegistry(rotation_threshold=4),
            )
        self.assertEqual(caught.exception.reason, REASON_INVALID_THRESHOLD)


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


# --------------------------------------------------------------------------
# One response is one liveness instant, and the live branch is reachable at all
# --------------------------------------------------------------------------


class ProductionCompositionTests(SourcedLaunchTestCase):
    """The supported composition, driven end to end: scope, observe, serve, fetch.

    Two facts converge here. The live-session activity branch was unreachable
    because every production reader passed a registry that had nothing in it, and
    one rendered response could draw its rows from one liveness instant and the
    figure beside them from another. Neither is a model defect -- the accepted
    projection has always answered both correctly when something asked it -- so
    everything below drives `ManagerController`'s own methods and asserts what a
    real client is actually told.

    `own` puts a handle in this controller's real registry, which is exactly what
    `launch` does and is asserted to do in `ControllerLaunchOwnershipTests`; the
    composition under test between that handle and the reader's screen is entirely
    production code.
    """

    class Phase:
        """Liveness that changes at a stated point, not at a hoped-for one.

        A prober scripted to answer `True` then `False` proves nothing on its own:
        it depends on how many times the composition happens to ask, which is the
        property under test. So the answer here is a state, and the state is
        flipped by a real boundary the composition crosses between the two
        readings -- the second read of the durable store, which is the aggregate's
        read. Whatever the composition asks and whenever it asks it, the flip lands
        between the rows and the figure beside them.
        """

        def __init__(self, live: bool = True) -> None:
            self.live = live
            self.probes = []

        def __call__(self, pgid) -> bool:
            self.probes.append(pgid)
            return self.live

    class FlippingStore(BindingStore):
        """This controller's own store, which flips the phase between the halves."""

        def __init__(self, root, phase, *, at: int = 2) -> None:
            super().__init__(root)
            self.phase = phase
            self.at = at
            self.reads = 0

        def records(self):
            self.reads += 1
            if self.reads == self.at:
                self.phase.live = False
            return super().records()

    # -- fixtures ---------------------------------------------------------

    def controller(self, registry=None) -> ManagerController:
        return ManagerController(
            self.context(), registry=self.registry if registry is None else registry
        )

    def a_live_rail(self, rail=LIVE_RAIL, *, role="executor", session_id=SESSION):
        self.authorize(rail, "running")
        record = self.bind(rail, session_id=session_id)
        if role != "executor":
            record = self._as_role(record, role)
        self.own(record)
        return record

    def _as_role(self, record, role):
        """The same durable binding, published in another accepted role.

        Roles are a durable fact about the work, so this rewrites the record the
        store holds rather than teaching the projection a second way to learn one.
        """
        rewritten = dataclasses.replace(record, role=role)
        path = self.store.path_for(record.session_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["role"] = role
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.registry.remove(record.session_id)
        self.own(rewritten)
        return rewritten

    def observed(self, controller=None, *, alive=ALWAYS_ALIVE):
        """One response's worth of observation, through the production methods."""
        owner = self.controller() if controller is None else controller
        run = self.a_run()
        return owner.observe(owner.queue_scope(run), run, alive=alive)

    def payload_over_http(self, controller, *, alive=ALWAYS_ALIVE) -> dict:
        """A real client, a real loopback socket, and the production server."""
        run = self.a_run()
        server = controller.serve_observed(run, controller.queue_scope(run), alive=alive)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        return payload_in(fetched(server.server_address[1]))

    # -- proof 1: the live branch reaches a real client -------------------

    def test_a_live_owned_session_reaches_a_real_client_as_executor_working(self) -> None:
        """The whole point of the rail, asserted over a socket rather than a seam.

        Nothing in this path is a fixture standing in for the composition: the
        scope is resolved from the real coordination repository, the rows are
        projected through the accepted queue source, the aggregate is reduced by
        the accepted reconciler, the page is rendered by the accepted renderer, and
        the bytes are read back by a real HTTP client. Before this rail the same
        client could not have been told this at all.
        """
        self.a_live_rail()

        payload = self.payload_over_http(self.controller())

        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_RUNNING])
        item_id = payload["rows"][0]["itemId"]
        self.assertEqual(payload["details"][item_id]["activity"], ACTIVITY_EXECUTOR_WORKING)
        self.assertEqual(payload["details"][item_id]["attentionOwner"], OWNER_AGENT)
        self.assertEqual(
            payload["agents"], {"permitted": 6, "current": 1, "reason": None}
        )

    def test_the_empty_registry_composition_is_what_used_to_prevent_it(self) -> None:
        """Kill the ownership half and the live branch must go away again.

        The store, the coordination repository and the durable record are identical.
        Only the registry differs, so a row that still read `executor-working` here
        would be a row that never needed ownership evidence at all.
        """
        self.a_live_rail()

        payload = self.payload_over_http(self.controller(registry=SessionRegistry()))

        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_DISCONNECTED])
        item_id = payload["rows"][0]["itemId"]
        self.assertEqual(
            payload["details"][item_id]["activity"], ACTIVITY_DISCONNECTED_RECOVERY
        )
        self.assertIsNone(payload["agents"]["current"])

    # -- proof 2: the other live roles ------------------------------------

    def test_reviewer_working_is_reachable_through_the_same_composition(self) -> None:
        self.a_live_rail(role="reviewer")

        payload = self.payload_over_http(self.controller())

        item_id = payload["rows"][0]["itemId"]
        self.assertEqual(payload["details"][item_id]["activity"], ACTIVITY_REVIEWER_WORKING)
        self.assertEqual(payload["agents"]["current"], 1)

    def test_orchestrator_reconciling_is_reachable_through_the_same_composition(self) -> None:
        self.a_live_rail(role="orchestrator")

        payload = self.payload_over_http(self.controller())

        item_id = payload["rows"][0]["itemId"]
        self.assertEqual(
            payload["details"][item_id]["activity"], ACTIVITY_ORCHESTRATOR_RECONCILING
        )

    # -- proof 3: context rotation ----------------------------------------

    def test_context_rotation_is_reachable_through_the_same_composition(self) -> None:
        """A live session and a replacement reserved on one rail: one row, rotating."""
        self.a_live_rail()
        self.bind(LIVE_RAIL, session_id=ROTATION_SESSION, state=BINDING_STATE_RESERVED)

        payload = self.payload_over_http(self.controller())

        self.assertEqual(len(payload["rows"]), 1)
        item_id = payload["rows"][0]["itemId"]
        self.assertEqual(payload["rows"][0]["state"], STATE_RUNNING)
        self.assertEqual(payload["details"][item_id]["activity"], ACTIVITY_CONTEXT_ROTATION)

    # -- proof 4: contradictory ownership still fails closed --------------

    def test_ownership_contradictory_refuses_the_whole_response(self) -> None:
        """Two provably live sessions on one rail is refused, not rendered.

        The refusal now lands while a response is being produced rather than while
        a server is being constructed, so this asserts it at both boundaries: the
        server cannot even be built, and the observation raises with the accepted
        reason naming both sessions.
        """
        self.a_live_rail()
        self.own(self.bind(LIVE_RAIL, session_id=ROTATION_SESSION))
        controller = self.controller()
        run = self.a_run()
        scope = controller.queue_scope(run)

        with self.assertRaises(QueueSourceError) as refused:
            controller.observe(scope, run, alive=ALWAYS_ALIVE)

        self.assertIn(REASON_OWNERSHIP_CONTRADICTORY, str(refused.exception))
        self.assertIn(SESSION, str(refused.exception))
        self.assertIn(ROTATION_SESSION, str(refused.exception))
        with self.assertRaises(QueueSourceError):
            controller.serve_observed(run, scope, alive=ALWAYS_ALIVE)

    # -- proofs 5 and 6: honest emptiness, fail-closed unprovability -------

    def test_an_empty_scope_still_reaches_a_client_as_an_established_zero(self) -> None:
        payload = self.payload_over_http(self.controller())

        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["agents"], {"permitted": 6, "current": 0, "reason": None})

    def test_ownership_unprovable_fails_closed_at_the_row_and_the_aggregate(self) -> None:
        """A durable binding this controller did not start, both halves at once."""
        self.authorize(LIVE_RAIL, "running")
        self.bind(LIVE_RAIL)

        payload = self.payload_over_http(self.controller())

        self.assertEqual([row["state"] for row in payload["rows"]], [STATE_DISCONNECTED])
        self.assertEqual(
            payload["agents"],
            {"permitted": 6, "current": None, "reason": REASON_OWNERSHIP_UNPROVABLE},
        )

    # -- proofs 7, 8 and 9: the preserved checkpoint-49 architecture -------

    def test_the_row_identity_is_the_durable_rail_and_never_the_transport(self) -> None:
        self.a_live_rail()

        payload = self.payload_over_http(self.controller())

        row = payload["rows"][0]
        self.assertEqual(row["title"], LIVE_RAIL)
        self.assertEqual(row["itemId"], "5:agent|6:ai-dev|8:issue-55|{0}:{1}".format(
            len(LIVE_RAIL), LIVE_RAIL
        ))
        for forbidden in (SESSION, "4242", "test-host"):
            self.assertNotIn(forbidden, row["itemId"])
            self.assertNotIn(forbidden, row["title"])

    def test_waiting_is_still_exactly_the_human_owned_set(self) -> None:
        """Non-vacuous: a live agent-owned row and a waiting human-owned one."""
        self.a_live_rail()
        self.authorize(BLOCKED_RAIL, "blocked")
        self.decide(BLOCKED_RAIL)

        payload = self.payload_over_http(self.controller())

        waiting = {
            row["itemId"] for row in payload["rows"] if row["state"] == STATE_WAITING
        }
        human = {
            item_id for item_id, detail in payload["details"].items()
            if detail["attentionOwner"] == OWNER_HUMAN
        }
        self.assertTrue(waiting)
        self.assertEqual(waiting, human)
        self.assertEqual(len(payload["rows"]), 2)

    def test_the_checkpoint_six_ceiling_is_unchanged_at_six(self) -> None:
        self.a_live_rail()

        payload = self.payload_over_http(self.controller())

        self.assertEqual(payload["agents"]["permitted"], CONCURRENCY_CEILING_DEFAULT)
        self.assertEqual(CONCURRENCY_CEILING_DEFAULT, 6)

    # -- proof 10: one response cannot span two liveness instants ----------

    def test_one_response_cannot_take_its_rows_and_its_figure_from_two_instants(self) -> None:
        """The constructed changing-liveness case, at the exact seam it lands on.

        The phase flips on the second read of the durable store, which is the read
        the aggregate performs. Under the old composition the rows were projected
        while the session was live and the figure was reduced after it was gone, so
        one response said `executor-working` beside `ownership-unprovable`. The
        observation is taken once now, so the flip cannot land inside a response:
        both halves describe the instant the observation was taken.
        """
        self.a_live_rail()
        phase = self.Phase()
        controller = self.controller()
        controller.store = self.FlippingStore(self.binding_root, phase)

        seen = self.observed(controller, alive=phase)

        self.assertGreaterEqual(controller.store.reads, 2)
        self.assertFalse(phase.live, "the flip must really have happened")
        self.assertEqual([row.state for row in seen.view.rows], [STATE_RUNNING])
        self.assertEqual(seen.agents, {"permitted": 6, "current": 1, "reason": None})
        self.assertEqual(len(phase.probes), 1)

    def test_the_split_observation_composition_is_what_used_to_break_it(self) -> None:
        """The same facts through the old two-call shape, kept as the control.

        `queue` and `agent_count` are the accepted checkpoint-46 methods and are
        unchanged; each still takes its own liveness reading, which is correct for
        a caller making one call. Composed into one response they describe two
        instants, and this states that plainly rather than leaving the new
        composition's guarantee resting on an untested claim about the old one.
        """
        self.a_live_rail()
        phase = self.Phase()
        controller = self.controller()
        controller.store = self.FlippingStore(self.binding_root, phase)
        run = self.a_run()

        view, _details = controller.queue(run, alive=phase)
        reading = controller.agent_count(alive=phase)

        self.assertEqual([row.state for row in view.rows], [STATE_RUNNING])
        self.assertIsNone(reading["current"])
        self.assertEqual(reading["reason"], REASON_OWNERSHIP_UNPROVABLE)

    # -- proof 11: a later response re-observes ---------------------------

    def test_a_later_response_observes_again_rather_than_reusing_the_last_one(self) -> None:
        """No durable liveness cache: the second response is allowed to disagree."""
        self.a_live_rail()
        phase = self.Phase()
        controller = self.controller()
        run = self.a_run()
        scope = controller.queue_scope(run)

        first = controller.observe(scope, run, alive=phase)
        phase.live = False
        second = controller.observe(scope, run, alive=phase)

        self.assertEqual([row.state for row in first.view.rows], [STATE_RUNNING])
        self.assertEqual(first.agents["current"], 1)
        self.assertEqual([row.state for row in second.view.rows], [STATE_DISCONNECTED])
        self.assertIsNone(second.agents["current"])
        self.assertEqual(len(phase.probes), 2)

    def test_two_real_requests_to_one_server_re_observe_independently(self) -> None:
        """The same property over the socket, because that is where it must hold."""
        self.a_live_rail()
        phase = self.Phase()
        controller = self.controller()
        run = self.a_run()
        server = controller.serve_observed(run, controller.queue_scope(run), alive=phase)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        port = server.server_address[1]

        live = payload_in(fetched(port))
        phase.live = False
        gone = payload_in(fetched(port))

        self.assertEqual(live["rows"][0]["state"], STATE_RUNNING)
        self.assertEqual(live["agents"]["current"], 1)
        self.assertEqual(gone["rows"][0]["state"], STATE_DISCONNECTED)
        self.assertIsNone(gone["agents"]["current"])

    def test_nothing_about_the_observation_is_retained_on_the_controller(self) -> None:
        """Structural: a response's observation may not outlive the response."""
        self.a_live_rail()
        controller = self.controller()
        before = set(vars(controller))

        self.observed(controller)

        self.assertEqual(set(vars(controller)), before)
        # All four are stated inputs or owned collaborators; `rotation_threshold` is
        # the fifth and is D9's stated policy, not anything a response observed.
        self.assertEqual(
            sorted(before),
            ["ceiling", "registry", "rotation_threshold", "source", "store"],
        )

    # -- the same store and the same registry, not merely equal ones -------

    def test_both_halves_read_the_controllers_own_store_object(self) -> None:
        """Not a second store over the same root: the same object, provably.

        A controller that reserves against one store and draws its rows from
        another agrees only by reading the same files, which is agreement by
        accident. `FlippingStore` counts reads, so this fails outright if either
        half constructs its own.
        """
        self.a_live_rail()
        controller = self.controller()
        controller.store = self.FlippingStore(self.binding_root, self.Phase(), at=0)

        self.observed(controller)

        self.assertEqual(controller.store.reads, 2)

    # -- the durable half is resolved once, and no response refetches ------

    def test_a_response_never_reaches_the_coordination_remote(self) -> None:
        """Re-observing is not polling, stated as a count of resolutions.

        The scope is what costs a fetch, and it is resolved once per run. A
        response re-reads a local store and re-probes liveness; if it resolved the
        scope again, this surface would have become the per-request fetch loop the
        accepted composition forbids.
        """
        self.a_live_rail()
        controller = self.controller()
        run = self.a_run()
        resolutions = []
        real = queue_source_module.resolve_read_source

        with unittest.mock.patch.object(
            queue_source_module, "resolve_read_source",
            lambda root: (resolutions.append(root), real(root))[1],
        ):
            scope = controller.queue_scope(run)
            controller.observe(scope, run, alive=ALWAYS_ALIVE)
            controller.observe(scope, run, alive=ALWAYS_ALIVE)

        self.assertEqual(len(resolutions), 1)


# --------------------------------------------------------------------------
# D8 through the supported production surface, read by a real HTTP client
# --------------------------------------------------------------------------


D8_BLOCKERS = {
    "permission": {
        "kind": "permission",
        "whatFailed": "publishing the executor handoff to the coordination remote",
        "missingCapability": "write access to jmrozi1/ai-dev-control-plane for this host key",
        "humanChange": "add this host's public key as a deploy key with write access",
        "stateChanged": True,
        "nextAction": "re-run the publish step; the checkpoint is already committed",
    },
    "configuration": {
        "kind": "configuration",
        "whatFailed": "resolving the ticket provider for this workspace",
        "missingCapability": "a ticketProvider entry in .ai-dev/config.json",
        "humanChange": "add the github provider block naming jmrozi1/ai-dev",
        "stateChanged": False,
        "nextAction": "re-dispatch this rail; nothing was written and nothing needs undoing",
    },
    "capability": {
        "kind": "capability",
        "whatFailed": "starting a reviewer session through the supported headless path",
        "missingCapability": "the Claude Agent SDK headless entry point on this host",
        "humanChange": "install the supported CLI and confirm it answers --version",
        "stateChanged": False,
        "nextAction": "re-dispatch this rail to a fresh reviewer session",
    },
    "credential": {
        "kind": "credential",
        "whatFailed": "authenticating the coordination remote fetch",
        "missingCapability": "a valid GitHub token for jmrozi1 on this host",
        "humanChange": "run gh auth login and grant repo scope",
        "stateChanged": False,
        "nextAction": "re-run the rail read; the queue projects once the fetch succeeds",
    },
    "environment": {
        "kind": "environment",
        "whatFailed": "starting-identity verification of the canonical worktree",
        "missingCapability": "an exclusively held product worktree",
        "humanChange": "decide the disposition of the two untracked launcher paths",
        "stateChanged": True,
        "nextAction": "re-dispatch the rail to a fresh executor session",
    },
}


class ActionableAttentionOverHttpTests(SourcedLaunchTestCase):
    """The rail's central proof: all nine D8 facts, served to a real client.

    Nothing here stands in for the composition. The scope is resolved from a real
    coordination repository through `ManagerController.queue_scope`, the rows come
    from the accepted queue source, the page is rendered by the accepted renderer
    and served by `serve_observed`, and the bytes are read back over a real
    loopback socket by a real `http.client`. What is asserted is what a person
    fetching that page is actually told.
    """

    def controller(self, registry=None) -> ManagerController:
        return ManagerController(
            self.context(), registry=self.registry if registry is None else registry
        )

    def publish_blocker(self, kind, *, rail=None, role="executor"):
        """One blocked rail carrying one published D8 record."""
        rail = rail or "issue-55-{0}-blocker".format(kind)
        assignment = "" if role is None else "Role: {0}\n".format(role)
        self.write(
            self.scope / "rails" / rail / "rail.md",
            "# Rail: {0}\n\nStatus: blocked\n{1}Depends on: none\n"
            "Shared resource: none\n\n## Goal\n\nbounded work\n".format(rail, assignment),
        )
        published = {
            "schemaVersion": 1,
            "decisionId": "d-{0}".format(kind),
            "project": "ai-dev",
            "ticket": "issue-55",
            "rail": rail,
            "raisedAt": "2026-08-31T11:00:00Z",
            "title": "Clear the {0} blocker on {1}".format(kind, rail),
            "explanation": "This rail cannot continue until a person changes something.",
            "evidence": [{"label": "rail", "locator": "rails/{0}/rail.md".format(rail)}],
            "blocker": dict(D8_BLOCKERS[kind]),
        }
        self.decide(rail_id=rail, payload=published)
        return rail, published

    def payload_over_http(self, controller=None, *, alive=ALWAYS_ALIVE) -> dict:
        owner = self.controller() if controller is None else controller
        run = self.a_run()
        server = owner.serve_observed(run, owner.queue_scope(run), alive=alive)
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)
        return payload_in(fetched(server.server_address[1]))

    def waiting_details(self, payload):
        return {
            payload["details"][row["itemId"]]["rail"]: payload["details"][row["itemId"]]
            for row in payload["rows"]
            if row["state"] == STATE_WAITING
        }

    # -- proofs 1 to 5, 7 and 8, over a socket ---------------------------

    def test_all_five_blocker_kinds_reach_a_real_client_self_contained(self) -> None:
        published_by_rail = {}
        for kind in D8_BLOCKERS:
            rail, published = self.publish_blocker(kind)
            published_by_rail[rail] = published

        payload = self.payload_over_http()

        served = self.waiting_details(payload)
        self.assertEqual(sorted(served), sorted(published_by_rail))
        for rail, published in sorted(published_by_rail.items()):
            block = published["blocker"]
            with self.subTest(kind=block["kind"]):
                detail = served[rail]
                self.assertEqual(detail["attentionOwner"], OWNER_HUMAN)
                # 3, 4, 5 -- project, ticket and the durable rail.
                self.assertEqual(detail["project"], "ai-dev")
                self.assertEqual(detail["ticket"], "issue-55")
                self.assertEqual(detail["rail"], rail)
                blocker = detail["blocker"]
                self.assertIsNone(detail["blockerUnavailable"])
                # 1 -- what failed. 2 -- the affected agent.
                self.assertEqual(blocker["whatFailed"], block["whatFailed"])
                self.assertEqual(blocker["agent"], "executor")
                # 6 -- the missing capability, permission, configuration or credential.
                self.assertEqual(blocker["missingCapability"], block["missingCapability"])
                # 7 -- exactly what the human must change.
                self.assertEqual(blocker["humanChange"], block["humanChange"])
                # 8 -- whether state changed, as a boolean and not a hedge.
                self.assertIs(blocker["stateChanged"], block["stateChanged"])
                # 9 -- the exact next action once it is resolved.
                self.assertEqual(blocker["nextAction"], block["nextAction"])
                self.assertEqual(blocker["kind"], block["kind"])

    def test_the_served_html_carries_the_published_text_and_no_transcript(self) -> None:
        """Proofs 13 and 14 over the bytes rather than over the payload object."""
        rail, published = self.publish_blocker("permission")
        run = self.a_run()
        controller = self.controller()
        server = controller.serve_observed(
            run, controller.queue_scope(run), alive=ALWAYS_ALIVE
        )
        self.addCleanup(server.server_close)
        serving = start_serving(server)
        self.addCleanup(serving.stop)

        body = fetched(server.server_address[1])

        self.assertIn("<!doctype html>", body.lower())
        for value in published["blocker"].values():
            if isinstance(value, str):
                self.assertIn(value, body)
        lowered = body.lower()
        for forbidden in ("transcript", "raw log", "session inspector", "console.",
                          "setinterval", "eventsource", "websocket", "<iframe"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered, forbidden)

    def test_state_changed_is_served_explicitly_in_both_directions(self) -> None:
        """Proof 8. The one answer a person cannot act on is unreachable."""
        changed, _ = self.publish_blocker("permission")
        unchanged, _ = self.publish_blocker("configuration")

        served = self.waiting_details(self.payload_over_http())

        self.assertIs(served[changed]["blocker"]["stateChanged"], True)
        self.assertIs(served[unchanged]["blocker"]["stateChanged"], False)
        for rail in (changed, unchanged):
            self.assertIn(served[rail]["blocker"]["stateChanged"], (True, False))

    # -- proof 9, over the production surface ----------------------------

    def test_the_rail_role_missing_case_arrives_actionable_on_its_own(self) -> None:
        """The concrete reachable record, over a real socket. Proofs 2 to 8.

        This is the case that made the behaviour a defect rather than a caution.
        `authorize()` refuses `rail-role-missing` for a rail with no `Role:`, so an
        orchestrator publishing *about* that refusal produces a valid record whose
        own `humanChange` says to add the missing `Role:`. When the projection
        discarded the blocker for want of an agent, the one published sentence
        telling a person how to make the item actionable was the sentence it threw
        away -- self-perpetuating by construction. Every field below is asserted
        verbatim against what was published, because a paraphrase here is exactly
        the failure D8 names.
        """
        rail = "issue-55-launch-refused-no-role"
        published = {
            "schemaVersion": 1,
            "decisionId": "d-rail-role-missing",
            "project": "ai-dev",
            "ticket": "issue-55",
            "rail": rail,
            "raisedAt": "2026-08-31T11:00:00Z",
            "title": "Launch refused: this rail publishes no Role:",
            "explanation": "This rail cannot start until a person changes something.",
            "evidence": [{"label": "rail", "locator": "rails/{0}/rail.md".format(rail)}],
            "blocker": {
                "kind": "configuration",
                "whatFailed": (
                    "authorize() refused with rail-role-missing: this rail's authorization "
                    "publishes no Role: header, so no managed session can start on it."
                ),
                "missingCapability": "a durable Role: assignment on this rail",
                "humanChange": "add 'Role: executor' to this rail's rail.md and republish it",
                "stateChanged": False,
                "nextAction": "re-run the launch once the rail republishes with a Role: header",
            },
        }
        self.write(
            self.scope / "rails" / rail / "rail.md",
            "# Rail: {0}\n\nStatus: blocked\nDepends on: none\n"
            "Shared resource: none\n\n## Goal\n\nbounded work\n".format(rail),
        )
        self.decide(rail_id=rail, payload=published)

        served = self.waiting_details(self.payload_over_http())[rail]
        blocker = served["blocker"]

        # Proof 2: it reached the served surface at all.
        self.assertIsNotNone(blocker)
        # Proofs 3 to 7, verbatim.
        self.assertEqual(blocker["kind"], "configuration")
        self.assertEqual(blocker["whatFailed"], published["blocker"]["whatFailed"])
        self.assertEqual(
            blocker["missingCapability"], published["blocker"]["missingCapability"]
        )
        self.assertEqual(blocker["humanChange"], published["blocker"]["humanChange"])
        self.assertIs(blocker["stateChanged"], False)
        self.assertEqual(blocker["nextAction"], published["blocker"]["nextAction"])
        # Proof 8: the agent is stated absent, not guessed.
        self.assertIsNone(blocker["agent"])
        self.assertEqual(
            blocker["agentUnavailable"], queue_source_module.BLOCKER_AGENT_UNSOURCED
        )
        # Routing still arrives, and the item is still the person's.
        self.assertEqual(served["project"], "ai-dev")
        self.assertEqual(served["ticket"], "issue-55")
        self.assertEqual(served["rail"], rail)
        self.assertEqual(served["attentionOwner"], OWNER_HUMAN)
        # The self-perpetuating half, closed: the instruction that clears this
        # item is now on the page a person is looking at.
        self.assertIn("Role: executor", blocker["humanChange"])

    def test_an_unsourceable_agent_is_stated_and_the_rest_is_served(self) -> None:
        """The item arrives, the published blocker arrives, and nothing is invented.

        Over the production surface, on a rail the supported publisher genuinely
        accepts: `blocked`, no `Role:` header, one complete six-field record. The
        agent has no durable source and says so; the five facts that do have one
        are on the page, verbatim, where a person can act on them.
        """
        rail, published = self.publish_blocker("credential", role=None)

        payload = self.payload_over_http()

        served = self.waiting_details(payload)
        detail = served[rail]
        self.assertEqual(detail["attentionOwner"], OWNER_HUMAN)
        self.assertEqual(detail["rail"], rail)

        # No item-level withholding: the absence is stated in its own field.
        self.assertIsNone(detail["blockerUnavailable"])
        blocker = detail["blocker"]
        self.assertIsNotNone(blocker)

        self.assertEqual(blocker["kind"], published["blocker"]["kind"])
        self.assertEqual(blocker["whatFailed"], published["blocker"]["whatFailed"])
        self.assertEqual(
            blocker["missingCapability"], published["blocker"]["missingCapability"]
        )
        self.assertEqual(blocker["humanChange"], published["blocker"]["humanChange"])
        self.assertIs(blocker["stateChanged"], published["blocker"]["stateChanged"])
        self.assertEqual(blocker["nextAction"], published["blocker"]["nextAction"])

        self.assertIsNone(blocker["agent"])
        self.assertEqual(
            blocker["agentUnavailable"], queue_source_module.BLOCKER_AGENT_UNSOURCED
        )
        # No managed role was guessed into the gap, and no word of the published
        # blocker was paraphrased into the statement of the gap.
        for invented in ("executor", "reviewer", "orchestrator"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, blocker["agentUnavailable"])
        for value in published["blocker"].values():
            if isinstance(value, str):
                self.assertNotIn(value, blocker["agentUnavailable"])

    # -- proofs 6 and 10 to 12, beside a genuinely live session ----------

    def test_a_live_agent_row_gains_nothing_and_waiting_stays_human_owned(self) -> None:
        """Proofs 10, 11 and 12 in one response, non-vacuously."""
        rail, _ = self.publish_blocker("environment")
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        payload = self.payload_over_http()

        states = {row["itemId"]: row["state"] for row in payload["rows"]}
        self.assertEqual(sorted(states.values()), [STATE_RUNNING, STATE_WAITING])
        owners = {
            payload["details"][item]["attentionOwner"]: item for item in states
        }
        self.assertEqual(len(owners), 2)
        waiting = [item for item, state in states.items() if state == STATE_WAITING]
        self.assertEqual([owners[OWNER_HUMAN]], waiting)

        running_detail = payload["details"][owners[OWNER_AGENT]]
        self.assertEqual(running_detail["activity"], ACTIVITY_EXECUTOR_WORKING)
        self.assertIsNone(running_detail["blocker"])
        self.assertIsNone(running_detail["blockerUnavailable"])
        self.assertEqual(running_detail["rail"], LIVE_RAIL)

        # Rows stayed dense: the row contract is unchanged and carries none of it.
        for row in payload["rows"]:
            with self.subTest(item=row["itemId"]):
                self.assertEqual(
                    set(row),
                    {"itemId", "state", "title", "project", "ticket", "elapsedSeconds"},
                )

        # And the aggregate the checkpoint-6 model owns is untouched by any of it.
        self.assertEqual(
            payload["agents"],
            {"permitted": CONCURRENCY_CEILING_DEFAULT, "current": 1, "reason": None},
        )

    def test_transport_identity_stayed_evidence_on_the_served_page(self) -> None:
        """Proof 6. Session ids and pids are locators, never a work item's name."""
        rail, _ = self.publish_blocker("permission")
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        payload = self.payload_over_http()

        for row in payload["rows"]:
            with self.subTest(item=row["itemId"]):
                self.assertNotIn(SESSION, json.dumps(row))
        for item_id, detail in payload["details"].items():
            with self.subTest(item=item_id):
                self.assertNotIn(SESSION, detail["rail"])
                self.assertNotIn(SESSION, json.dumps(detail["blocker"]))
        evidence = json.dumps(
            [d["evidence"] for d in payload["details"].values()]
        )
        self.assertIn(SESSION, evidence)

    # -- the requirement's own exclusion ---------------------------------

    def test_ordinary_rails_create_no_human_attention_item_at_all(self) -> None:
        """Acceptance, reconciliation and review commissioning are not human work.

        Four rails in four ordinary states and a live executor, and the default
        Waiting view is empty. D8 says this as plainly as it says the nine fields.
        """
        for index, status in enumerate(("ready", "running", "completed", "ready")):
            self.write(
                self.scope / "rails" / "issue-55-ordinary-{0}".format(index) / "rail.md",
                "# Rail: issue-55-ordinary-{0}\n\nStatus: {1}\nRole: executor\n"
                "Depends on: none\nShared resource: none\n\n## Goal\n\nwork\n".format(
                    index, status
                ),
            )
        self.authorize(LIVE_RAIL, "running")
        self.own(self.bind(LIVE_RAIL))

        payload = self.payload_over_http()

        self.assertEqual(payload["defaultFilters"], [STATE_WAITING])
        self.assertEqual(
            [row for row in payload["rows"] if row["state"] == STATE_WAITING], []
        )
        self.assertEqual(
            [
                item for item, detail in payload["details"].items()
                if detail["attentionOwner"] == OWNER_HUMAN
            ],
            [],
        )
