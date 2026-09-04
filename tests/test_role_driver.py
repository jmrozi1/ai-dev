"""`role_driver` holds more than one managed role session live at once, or refuses."""

from __future__ import annotations

from pathlib import Path
import subprocess
import types
import unittest

from ai_dev_flow import role_driver, role_driver_dispatch
from ai_dev_flow.manager_controller import ManagerController
from ai_dev_flow.manager_dispatch import DispatchError
from ai_dev_flow.orchestrator_invocation import InvocationRefused
from ai_dev_flow.role_driver import (
    REASON_NO_LAUNCH_STATED,
    RoleLaunch,
    drive_roles,
)
from ai_dev_flow.role_driver_dispatch import (
    REASON_LAUNCH_SCOPE_DISAGREEMENT,
    stated_launch_groups,
)
from ai_dev_flow.role_invocation import REASON_RAIL_ROLE, REASON_ROLE_NOT_LAUNCHABLE
from ai_dev_flow.session_binding import (
    BindingStore,
    RailIteration,
    attach_process,
    reserve_binding,
)
from ai_dev_flow.session_lifecycle import SessionRegistry

from tests.test_role_invocation import (
    EXECUTOR,
    EXECUTOR_RAIL,
    HEAD,
    ORCHESTRATOR,
    PROJECT,
    REVIEWER,
    REVIEWER_RAIL,
    TICKET,
    FakeHandle,
    RoleInvocationTestBase,
)


class DriverTestBase(RoleInvocationTestBase):
    """Real store, real registry, real controller, real predicate. Injected worker.

    The process boundary is injected through exactly the accepted `launch_kwargs` /
    `stop_kwargs` the lifecycle already defines -- there is no driver-level hook and
    no way to hand the driver a decision. Liveness is one prober shared by the
    occupancy reduction and by teardown, so a session that has been stopped stops
    counting for the same reason a real one would: its process group is gone.
    """

    def setUp(self) -> None:
        super().setUp()
        self.live = {}
        self.next_pid = 7100
        self.started = []
        self.torn_down = []

    # -- injected process boundary, one distinct worker per launch ---------------

    def _driver_starter(self):
        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            self.next_pid += 1
            handle = FakeHandle(pid=self.next_pid, pgid=self.next_pid)
            self.live[handle.pgid] = True
            bound = attach_process(
                store,
                reserved.session_id,
                pid=handle.pid,
                pid_domain="test-host",
                started_at=handle.started_at,
                bound_at="2026-09-04T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            self.started.append(reserved.session_id)
            return handle, bound

        return start

    def _driver_stopper(self):
        def stop(handle, **kwargs):
            self.torn_down.append(handle.pgid)
            self.live[handle.pgid] = False
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}

        return stop

    def _alive(self, pgid):
        return bool(self.live.get(pgid))

    # -- fixtures ---------------------------------------------------------------

    def _controller(self, *, ceiling=6, registry=None):
        return ManagerController(
            types.SimpleNamespace(
                control_plane=self.tmp_path,
                project=PROJECT,
                ticket=TICKET,
                binding_root=self.tmp_path / "controller-state",
            ),
            ceiling=ceiling,
            registry=registry,
        )

    def _occupy(self, count):
        """`count` reserved records: the shape that actually reaches the ceiling.

        Reserved rather than bound, because `authorize` never subtracts
        `slots.unprovable`, so a bound record this controller did not start answers
        `concurrency-count-unprovable` instead of `concurrency-ceiling-reached`.
        """
        for index in range(count):
            reserve_binding(
                self.store,
                project=PROJECT,
                ticket=TICKET,
                reference=self.reference,
                workspace_path=self.workspace,
                worktree_id=self.worktree_id,
                rail="occupant-rail-{0}".format(index),
                role=EXECUTOR,
                iteration=RailIteration(
                    rail="occupant-rail-{0}".format(index),
                    blob="{0}{1}".format(index, "f" * 39),
                ),
                session_id="00000000-0000-4000-8000-00000000000{0}".format(index),
                launched_at_head=HEAD,
                reserved_at="2026-09-04T11:00:0{0}Z".format(index),
                ceiling=6,
            )

    def _launches(self, *pairs):
        return [
            RoleLaunch(rail=rail, role=role, request_kwargs=self._request_kwargs(role))
            for rail, role in pairs
        ]

    def _drive(self, launches, *, controller=None, snapshot=None, observation=None,
               send=None, **overrides):
        controller = controller if controller is not None else self._controller()
        snapshot = snapshot if snapshot is not None else self._snapshot()
        observation = observation if observation is not None else self._observation()
        sender, recorded = self._sender()
        arguments = dict(
            controller=controller,
            reference=self.reference,
            package_root=self.repo_root,
            launch_kwargs={
                "start": self._driver_starter(),
                "send": send if send is not None else sender,
            },
            stop_kwargs={"stop": self._driver_stopper(), "alive": self._alive},
            alive=self._alive,
        )
        arguments.update(overrides)
        outcome = drive_roles(snapshot, launches, observation, **arguments)
        return outcome, controller, recorded


class ConcurrentHoldingTests(DriverTestBase):
    """The capability: more than one managed session alive, owned and counted at once."""

    def test_two_sessions_are_held_live_at_the_same_instant(self):
        """The whole point of the slice, asserted at the instant it is true.

        `while_held` runs while both sessions are still bound and both handles are
        still in the controller's registry, and what it reads is the controller's own
        reconciled occupancy -- not a number this test kept.
        """
        witnessed = {}

        def observe(held):
            witnessed["count"] = len(held)
            witnessed["reading"] = observed_controller[0].agent_count(alive=self._alive)
            witnessed["registry"] = len(observed_controller[0].registry.sessions())
            witnessed["states"] = sorted(
                observed_controller[0].store.read(session.session_id).state
                for session in held
            )

        observed_controller = [self._controller()]
        outcome, controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER)),
            controller=observed_controller[0],
            while_held=observe,
        )

        self.assertEqual(witnessed["count"], 2)
        self.assertEqual(witnessed["registry"], 2)
        self.assertEqual(witnessed["states"], ["bound", "bound"])
        self.assertEqual(witnessed["reading"], {"permitted": 6, "current": 2, "reason": None})

        self.assertEqual(len(outcome.held), 2)
        self.assertEqual(outcome.refusals, ())
        self.assertEqual(outcome.peak_live, 2)
        self.assertEqual(outcome.peak_occupancy["permitted"], 6)
        self.assertEqual(
            [session.role for session in outcome.held], [EXECUTOR, REVIEWER]
        )
        self.assertEqual(
            [session.rail for session in outcome.held], [EXECUTOR_RAIL, REVIEWER_RAIL]
        )
        # Two distinct sessions, two distinct worker process groups.
        self.assertEqual(len({session.session_id for session in outcome.held}), 2)
        self.assertEqual(len({session.pgid for session in outcome.held}), 2)
        self.assertEqual(controller.registry.sessions(), [])

    def test_occupancy_grows_with_each_admission(self):
        """Admission N+1 is decided against an occupancy that already includes N.

        This is how D6 is enforced without a second count: the first launch consumes
        a slot by reserving a binding, and the second admission reconciles that same
        store before the predicate sees it.
        """
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual(outcome.entry_occupancy["current"], 0)
        self.assertEqual([session.occupancy["current"] for session in outcome.held], [1, 2])
        self.assertEqual(outcome.exit_occupancy["current"], 0)

    def test_every_held_session_is_released_in_reverse_order_and_proven_gone(self):
        outcome, controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual(
            self.torn_down, [outcome.held[1].pgid, outcome.held[0].pgid]
        )
        self.assertEqual(len(outcome.released), 2)
        for released in outcome.released:
            self.assertTrue(released.process_group_gone)
            self.assertTrue(released.graceful)
            self.assertEqual(released.binding_state, "unbound")
        self.assertEqual(controller.registry.sessions(), [])
        self.assertEqual(
            sorted(record.state for record in self.store.records()),
            ["unbound", "unbound"],
        )

    def test_each_session_runs_its_own_role_directive_and_role_package(self):
        """Role fidelity did not move: two sessions, two roles, two role packages."""
        _outcome, _controller, sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual([entry["role"] for entry in sent], [EXECUTOR, REVIEWER])
        self.assertEqual([entry["expected_skill"] for entry in sent], [EXECUTOR, REVIEWER])
        self.assertNotEqual(sent[0]["prompt"], sent[1]["prompt"])

    def test_three_sessions_are_held_at_once_on_three_rails(self):
        """Two is the property; three is the evidence that two was not a special case."""
        third = self._third_rail_scope()
        outcome, _controller, _sent = self._drive(
            self._launches(
                (EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER), (third[0], EXECUTOR)
            ),
            snapshot=third[1],
            observation=third[2],
        )
        self.assertEqual(len(outcome.held), 3)
        self.assertEqual(outcome.peak_live, 3)
        self.assertEqual([session.occupancy["current"] for session in outcome.held], [1, 2, 3])

    def _third_rail_scope(self):
        """A third running rail, so a three-way hold has three standing authorizations."""
        from ai_dev_flow.authorization import RailObservation
        from ai_dev_flow.orchestrator_trigger import RailSnapshot, ScopeSnapshot

        rail = "role-launch-executor-rail-two"
        blob = "d" * 40
        snapshot = self._snapshot()
        extended = ScopeSnapshot(
            project=snapshot.project,
            ticket=snapshot.ticket,
            head=snapshot.head,
            state_blob=snapshot.state_blob,
            rails=tuple(
                sorted(
                    snapshot.rails
                    + (
                        RailSnapshot(
                            identifier=rail,
                            authorization_blob=blob,
                            status="running",
                            role=EXECUTOR,
                        ),
                    ),
                    key=lambda entry: entry.identifier,
                )
            ),
        )
        observation = self._observation()
        observed = type(observation)(
            project=observation.project,
            ticket=observation.ticket,
            head=observation.head,
            rails=observation.rails
            + (
                RailObservation(
                    identifier=rail, status="running", rail_blob=blob, role=EXECUTOR
                ),
            ),
            workspace=observation.workspace,
        )
        return rail, extended, observed


class CeilingTests(DriverTestBase):
    """D6 is a limit this driver cannot cross, and it is not a target it aims at."""

    def test_the_ceiling_refuses_the_launch_that_would_exceed_it(self):
        """Five occupied, one admitted to six, and the seventh is refused while six run."""
        self._occupy(5)
        outcome, controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )

        self.assertEqual(len(outcome.held), 1)
        self.assertEqual(outcome.held[0].occupancy["current"], 6)
        self.assertEqual(outcome.peak_live, 6)
        self.assertEqual(len(outcome.refusals), 1)
        refusal = outcome.refusals[0]
        self.assertEqual(refusal.rail, REVIEWER_RAIL)
        self.assertEqual(refusal.reason, "not-authorized")
        self.assertIn("concurrency-ceiling-reached", refusal.detail)
        # Nothing was reserved, spawned or sent for the refused rail.
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.store.records()), 6)
        self.assertEqual(controller.registry.sessions(), [])

    def test_one_slot_lower_the_same_two_launches_are_both_admitted(self):
        """The discriminating half: the fixture can say yes as well as no."""
        self._occupy(4)
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual(len(outcome.held), 2)
        self.assertEqual(outcome.refusals, ())
        self.assertEqual(outcome.peak_live, 6)

    def test_a_total_that_cannot_be_established_fails_closed(self):
        """A bound record this controller cannot prove it owns refuses, never admits.

        `authorize` does not subtract `slots.unprovable`, so an unprovable total is a
        refusal rather than a smaller number, and nothing is launched at all.
        """
        reserve_binding(
            self.store,
            project=PROJECT,
            ticket=TICKET,
            reference=self.reference,
            workspace_path=self.workspace,
            worktree_id=self.worktree_id,
            rail="foreign-rail",
            role=EXECUTOR,
            iteration=RailIteration(rail="foreign-rail", blob="e" * 40),
            session_id="99999999-0000-4000-8000-999999999999",
            launched_at_head=HEAD,
            reserved_at="2026-09-04T11:00:09Z",
            ceiling=6,
        )
        attach_process(
            self.store,
            "99999999-0000-4000-8000-999999999999",
            pid=4242,
            pid_domain="another-host",
            started_at="2026-09-04T11:00:10Z",
            bound_at="2026-09-04T11:00:11Z",
            expected_iteration=RailIteration(rail="foreign-rail", blob="e" * 40),
        )

        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual(outcome.held, ())
        self.assertEqual(len(outcome.refusals), 2)
        for refusal in outcome.refusals:
            self.assertIn("concurrency-count-unprovable", refusal.detail)
        self.assertEqual(self.started, [])

    def test_a_free_slot_is_never_a_reason_to_launch_anything(self):
        """The ceiling is a limit, not a target: one stated launch starts one session."""
        outcome, _controller, sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR))
        )
        self.assertEqual(len(outcome.held), 1)
        self.assertEqual(outcome.peak_live, 1)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(outcome.entry_occupancy["current"], 0)


class DriverRefusalTests(DriverTestBase):
    """Every gate that refused a single launch still refuses one launch of many."""

    def test_a_rail_assigned_another_role_is_refused_and_the_rest_still_run(self):
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, REVIEWER), (REVIEWER_RAIL, REVIEWER))
        )
        self.assertEqual(len(outcome.refusals), 1)
        self.assertEqual(outcome.refusals[0].reason, REASON_RAIL_ROLE)
        self.assertEqual(outcome.refusals[0].rail, EXECUTOR_RAIL)
        self.assertEqual(len(outcome.held), 1)
        self.assertEqual(outcome.held[0].rail, REVIEWER_RAIL)

    def test_a_rail_that_is_not_running_is_refused(self):
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER)),
            snapshot=self._snapshot(executor_status="paused"),
        )
        self.assertEqual(len(outcome.refusals), 1)
        self.assertEqual(outcome.refusals[0].reason, "role-rail-not-running")
        self.assertEqual(len(outcome.held), 1)

    def test_the_orchestrator_role_refuses_the_whole_run_before_anything_is_spent(self):
        """Pre-flight, because a later refusal would already have spent real sessions."""
        with self.assertRaises(InvocationRefused) as caught:
            self._drive(
                self._launches((EXECUTOR_RAIL, EXECUTOR))
                + [
                    RoleLaunch(
                        rail=REVIEWER_RAIL,
                        role=ORCHESTRATOR,
                        request_kwargs=self._request_kwargs(REVIEWER),
                    )
                ]
            )
        self.assertEqual(caught.exception.reason, REASON_ROLE_NOT_LAUNCHABLE)
        self.assertIn("material-wake gate", str(caught.exception))
        self.assertEqual(self.started, [])
        self.assertEqual(self.store.records(), [])

    def test_a_run_that_states_no_launch_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._drive([])
        self.assertEqual(caught.exception.reason, REASON_NO_LAUNCH_STATED)

    def test_the_same_rail_twice_is_refused_by_the_accepted_predicate(self):
        """No new rule: a nonterminal binding on a rail makes the second a continuation.

        This door may only start a fresh session, so the accepted predicate's own
        answer refuses it, and the driver adds nothing of its own.
        """
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR), (EXECUTOR_RAIL, EXECUTOR))
        )
        self.assertEqual(len(outcome.held), 1)
        self.assertEqual(len(outcome.refusals), 1)
        self.assertEqual(outcome.refusals[0].reason, "continuation-refused")


class TeardownTests(DriverTestBase):
    """Nothing this driver opens outlives it, on any path out."""

    def test_a_while_held_that_raises_releases_every_held_session(self):
        def explode(held):
            raise RuntimeError("the caller's window failed")

        controller = self._controller()
        with self.assertRaises(RuntimeError):
            self._drive(
                self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER)),
                controller=controller,
                while_held=explode,
            )
        self.assertEqual(len(self.torn_down), 2)
        self.assertEqual(controller.registry.sessions(), [])
        self.assertFalse(any(self.live.values()))

    def test_a_launch_failure_releases_what_was_already_held(self):
        """A second launch that fails does not strand the first one's process group."""
        calls = []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            calls.append(request.session_id)
            if len(calls) == 2:
                raise RuntimeError("provider refused the second invocation")
            return {
                "session_id": request.session_id,
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "events": [],
            }

        controller = self._controller()
        with self.assertRaises(Exception) as caught:
            self._drive(
                self._launches((EXECUTOR_RAIL, EXECUTOR), (REVIEWER_RAIL, REVIEWER)),
                controller=controller,
                send=send,
            )
        self.assertIn("launch", str(caught.exception).lower())
        # The first session's process group was torn down by the driver, not left.
        self.assertIn(self.next_pid - 1, self.torn_down)
        self.assertEqual(controller.registry.sessions(), [])


class StructuralTests(DriverTestBase):
    """Properties a reviewer should not have to take on trust."""

    def _source(self, name):
        import ai_dev_flow

        return (Path(ai_dev_flow.__file__).parent / name).read_text(encoding="utf-8")

    def _names(self, name):
        """Every identifier the module's code actually uses, comments and prose excluded.

        Deliberately an AST walk and not a substring search: this file argues at
        length, in comments, about the schedulers and second counts it does not have,
        and a test that could be failed by explaining itself would push the reasoning
        out of the code.
        """
        import ast

        tree = ast.parse(self._source(name))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    used.update(alias.name.split("."))
                    used.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                used.update((node.module or "").split("."))
                for alias in node.names:
                    used.add(alias.name)
                    used.add(alias.asname or alias.name)
        return used

    def test_the_driver_has_no_scheduler_pool_thread_or_priority_model(self):
        forbidden = (
            "threading",
            "multiprocessing",
            "futures",
            "asyncio",
            "Thread",
            "Pool",
            "heapq",
            "sched",
            "PriorityQueue",
            "Queue",
        )
        for name in ("role_driver.py", "role_driver_dispatch.py"):
            used = self._names(name)
            for token in forbidden:
                self.assertNotIn(token, used, "{0} uses {1}".format(name, token))

    def test_there_is_no_injection_point_for_a_decision_or_a_launcher(self):
        """The gate always calls the accepted predicate; nothing can answer for it."""
        import inspect

        signature = inspect.signature(drive_roles)
        for forbidden in ("decision", "authorized", "authorize", "launcher", "gate"):
            self.assertNotIn(forbidden, signature.parameters)
        used = self._names("role_driver.py")
        for forbidden in ("authorize", "AuthorizationDecision", "launch_session"):
            self.assertNotIn(forbidden, used)

    def test_the_driver_adds_no_second_count_of_agents(self):
        """Occupancy comes from the controller's reduction and is never recomputed."""
        used = self._names("role_driver.py")
        for forbidden in ("reconcile_agent_slots", "AgentSlots", "CONCURRENCY_CEILING_DEFAULT",
                          "occupied", "ceiling"):
            self.assertNotIn(forbidden, used)
            self.assertFalse(hasattr(role_driver, forbidden))
        # The only occupancy readings it holds are ones the controller produced.
        self.assertEqual(self._source("role_driver.py").count("controller.agent_count("), 3)

    def test_the_orchestrator_entry_points_are_byte_unchanged(self):
        """`manager_dispatch` and `orchestrator_invocation` were not touched at all."""
        import ai_dev_flow

        package = Path(ai_dev_flow.__file__).parent
        for name in ("manager_dispatch.py", "orchestrator_invocation.py"):
            try:
                committed = subprocess.run(
                    ["git", "-C", str(package.parent), "show",
                     "HEAD:ai_dev_flow/{0}".format(name)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("no git checkout to compare against")
            self.assertEqual(committed, (package / name).read_bytes(), name)

    def test_the_module_has_a_main_and_it_is_the_driver_path(self):
        self.assertTrue(callable(role_driver_dispatch.main))
        text = self._source("role_driver_dispatch.py")
        self.assertIn('if __name__ == "__main__":', text)
        self.assertEqual(text.count("drive_roles("), 1)


class EntryPointTests(DriverTestBase):
    """The command line states every launch in full, and refuses what it cannot."""

    def _group(self, rail, role):
        return [
            "--rail", rail,
            "--role", role,
            "--ticket-provider", "github",
            "--ticket-id", "55",
            "--ticket-repository", "jmrozi1/ai-dev",
            "--controller-root", str(self.controller_root),
            # A role the packet cannot carry still states a full runtime policy, so
            # the refusal under test is the role and not a missing flag.
            "--prompt-file", str(self.prompts.get(role, self.prompts[EXECUTOR])),
            "--plugin-root", str(self.plugins.get(role, self.plugins[EXECUTOR])),
            "--expected-skill", role,
            "--allowed-tool", "Read",
            "--max-turns", "2",
            "--max-budget-usd", "0.5",
        ]

    def test_two_launch_groups_are_parsed_and_the_scope_argv_falls_through(self):
        argv = (
            ["--control-plane", str(self.tmp_path)]
            + self._group(EXECUTOR_RAIL, EXECUTOR)
            + self._group(REVIEWER_RAIL, REVIEWER)
            + ["--project", PROJECT, "--ticket", TICKET]
        )
        launches, remaining = stated_launch_groups(argv)

        self.assertEqual([inputs.rail for inputs in launches], [EXECUTOR_RAIL, REVIEWER_RAIL])
        self.assertEqual([inputs.role for inputs in launches], [EXECUTOR, REVIEWER])
        self.assertEqual(
            launches[0].request_kwargs["expected_skill"], EXECUTOR
        )
        self.assertEqual(
            launches[1].request_kwargs["prompt_file"], self.prompts[REVIEWER]
        )
        for flag in ("--control-plane", "--project", "--ticket"):
            self.assertIn(flag, remaining)
        self.assertNotIn("--rail", remaining)

    def test_a_run_that_states_no_rail_is_refused_at_the_command_line(self):
        with self.assertRaises(DispatchError) as caught:
            stated_launch_groups(["--project", PROJECT])
        self.assertEqual(caught.exception.reason, REASON_NO_LAUNCH_STATED)

    def test_the_orchestrator_role_is_refused_at_the_command_line(self):
        with self.assertRaises(DispatchError) as caught:
            stated_launch_groups(self._group(EXECUTOR_RAIL, ORCHESTRATOR))
        self.assertEqual(caught.exception.reason, REASON_ROLE_NOT_LAUNCHABLE)

    def test_a_group_missing_its_runtime_policy_is_refused_in_the_accepted_words(self):
        group = self._group(EXECUTOR_RAIL, EXECUTOR)
        del group[group.index("--prompt-file"): group.index("--prompt-file") + 2]
        with self.assertRaises(DispatchError) as caught:
            stated_launch_groups(group)
        self.assertEqual(caught.exception.reason, "runtime-policy-unstated")
        self.assertIn("--prompt-file", str(caught.exception))

    def test_two_launches_naming_different_tickets_are_refused(self):
        second = self._group(REVIEWER_RAIL, REVIEWER)
        second[second.index("--ticket-id") + 1] = "56"
        with self.assertRaises(DispatchError) as caught:
            stated_launch_groups(self._group(EXECUTOR_RAIL, EXECUTOR) + second)
        self.assertEqual(caught.exception.reason, REASON_LAUNCH_SCOPE_DISAGREEMENT)


class ControllerPassThroughTests(DriverTestBase):
    """`open_role` is a pass-through onto the accepted door and adds nothing."""

    def test_it_supplies_its_own_store_registry_and_reconciled_occupancy(self):
        registry = SessionRegistry()
        controller = self._controller(registry=registry)
        seen = {}

        def fake_open(snapshot, packet, observation, **kwargs):
            seen.update(kwargs)
            raise InvocationRefused("stopped-here", "the pass-through was observed")

        import ai_dev_flow.manager_controller as controller_module

        real = controller_module.open_role_session
        controller_module.open_role_session = fake_open
        try:
            with self.assertRaises(InvocationRefused):
                controller.open_role(
                    self._snapshot(),
                    None,
                    self._observation(),
                    reference=self.reference,
                    request_kwargs=self._request_kwargs(EXECUTOR),
                    package_root=self.repo_root,
                )
        finally:
            controller_module.open_role_session = real

        self.assertIs(seen["store"], controller.store)
        self.assertIs(seen["registry"], registry)
        self.assertEqual(seen["slots"].ceiling, 6)
        self.assertEqual(seen["bindings"], controller.store.records())

    def test_a_store_this_controller_does_not_own_is_never_read(self):
        """One controller, one store, one registry: the driver cannot substitute either."""
        controller = self._controller()
        other = BindingStore(self.tmp_path / "somebody-elses-store")
        outcome, _controller, _sent = self._drive(
            self._launches((EXECUTOR_RAIL, EXECUTOR)), controller=controller
        )
        self.assertEqual(other.records(), [])
        self.assertEqual(len(controller.store.records()), 1)
        self.assertEqual(outcome.held[0].session_id, controller.store.records()[0].session_id)


if __name__ == "__main__":
    unittest.main()
