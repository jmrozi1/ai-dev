"""`role_invocation` starts executor- and reviewer-role sessions, one at a time, or refuses."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import types
import unittest

from ai_dev_flow import role_dispatch, role_invocation, workspaces
from ai_dev_flow.authorization import (
    AgentSlots,
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
)
from ai_dev_flow.manager_controller import ManagerController
from ai_dev_flow.orchestrator_invocation import InvocationRefused
from ai_dev_flow.orchestrator_trigger import RailSnapshot, ScopeSnapshot
from ai_dev_flow.role_invocation import (
    LAUNCHABLE_ROLES,
    REASON_RAIL_NOT_RUNNING,
    REASON_RAIL_ROLE,
    REASON_RAIL_UNRECONCILED,
    REASON_ROLE_NOT_LAUNCHABLE,
    REASON_SESSION_ALREADY_LIVE,
    RolePacket,
    build_role_packet,
    invoke_role,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_RESERVED,
    BindingStore,
    RailIteration,
    attach_process,
    reserve_binding,
)
from ai_dev_flow.session_lifecycle import SessionRegistry
from ai_dev_flow.tickets import TicketReference

PROJECT = "ai-dev"
TICKET = "issue-55"
HEAD = "c" * 40
STATE_BLOB = "5" * 40

EXECUTOR_RAIL = "role-launch-executor-rail"
REVIEWER_RAIL = "role-launch-reviewer-rail"
EXECUTOR_BLOB = "a" * 40
REVIEWER_BLOB = "b" * 40

EXECUTOR = "executor"
REVIEWER = "reviewer"
ORCHESTRATOR = "orchestrator"


class FakeHandle(object):
    """A worker handle with no process behind it. Nothing here spawns anything."""

    def __init__(self, pid=5151, pgid=5151, started_at="2026-09-04T12:00:02Z"):
        self.pid = pid
        self.pgid = pgid
        self.started_at = started_at
        self.sdk_version = "0.2.152"
        self.sdk_detail = None
        self.process = types.SimpleNamespace(returncode=0)

    @property
    def sdk_available(self):
        return True


class RoleInvocationTestBase(unittest.TestCase):
    """Real worktrees, claims, stores and predicate. Injected worker; no provider."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()
        self.repo_root = self._init_repo("product")
        self.reference = TicketReference(
            provider="github", ticket_id="55", repository="jmrozi1/ai-dev"
        )
        self.workspace, self.worktree_id = self._add_workspace("workspace-55")
        self.workspace_key = "github:jmrozi1/ai-dev#55"

        self.controller_root = self.tmp_path / "controller"
        self.prompts = {}
        self.plugins = {}
        for role in LAUNCHABLE_ROLES:
            prompt = self.controller_root / "prompts" / "{0}.md".format(role)
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text("bounded {0}\n".format(role), encoding="utf-8")
            self.prompts[role] = prompt
            plugin = self.controller_root / "plugins" / "ai-dev-{0}".format(role)
            (plugin / "skills" / role).mkdir(parents=True)
            (plugin / "skills" / role / "SKILL.md").write_text(
                "---\nname: {0}\n---\n".format(role), encoding="utf-8"
            )
            (plugin / ".claude-plugin").mkdir(parents=True)
            (plugin / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "ai-dev-{0}".format(role)}) + "\n", encoding="utf-8"
            )
            self.plugins[role] = plugin

        self.store = BindingStore(self.tmp_path / "controller-state")
        self.registry = SessionRegistry()

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "prune"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._tmpdir.cleanup()

    # -- fixtures ---------------------------------------------------------------

    def _git(self, repo_root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root)] + list(args),
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Role Launch Tests")
        self._git(repo_root, "config", "user.email", "role-launch@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo_root, "add", "tracked.txt")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _add_workspace(self, name: str):
        path = self.tmp_path / name
        branch = "flow/{0}".format(name)
        self._git(self.repo_root, "worktree", "add", "-q", "-b", branch, str(path), "main")
        worktree_id = workspaces.effective_worktree_id(path)
        workspaces.create_active_claim(
            path,
            reference=self.reference,
            worktree_id=worktree_id,
            workspace_path=path,
            branch=branch,
        )
        return path, worktree_id

    # -- scope ------------------------------------------------------------------

    def _snapshot(
        self,
        *,
        head=HEAD,
        executor_role=EXECUTOR,
        reviewer_role=REVIEWER,
        executor_status="running",
        executor_proposed=None,
        executor_blob=EXECUTOR_BLOB,
    ):
        rails = (
            RailSnapshot(
                identifier=EXECUTOR_RAIL,
                authorization_blob=executor_blob,
                status=executor_status,
                proposed_status=executor_proposed,
                role=executor_role,
            ),
            RailSnapshot(
                identifier=REVIEWER_RAIL,
                authorization_blob=REVIEWER_BLOB,
                status="running",
                role=reviewer_role,
            ),
        )
        return ScopeSnapshot(
            project=PROJECT,
            ticket=TICKET,
            head=head,
            state_blob=STATE_BLOB,
            rails=tuple(sorted(rails, key=lambda entry: entry.identifier)),
        )

    def _observation(
        self,
        *,
        head=HEAD,
        executor_role=EXECUTOR,
        reviewer_role=REVIEWER,
        executor_status="running",
        executor_unreconciled=False,
        executor_blob=EXECUTOR_BLOB,
        workspace="ok",
    ):
        if workspace == "ok":
            observed = WorkspaceObservation(
                workspace_key=self.workspace_key,
                worktree_id=self.worktree_id,
                workspace_path=str(self.workspace),
            )
        elif workspace == "unproven":
            observed = WorkspaceObservation(
                workspace_key=self.workspace_key,
                worktree_id=self.worktree_id,
                workspace_path=str(self.workspace),
                identity_problem="claim names another worktree",
            )
        else:
            observed = None
        return ControlPlaneObservation(
            project=PROJECT,
            ticket=TICKET,
            head=head,
            rails=(
                RailObservation(
                    identifier=EXECUTOR_RAIL,
                    status=executor_status,
                    rail_blob=executor_blob,
                    role=executor_role,
                    unreconciled=executor_unreconciled,
                ),
                RailObservation(
                    identifier=REVIEWER_RAIL,
                    status="running",
                    rail_blob=REVIEWER_BLOB,
                    role=reviewer_role,
                ),
            ),
            workspace=observed,
        )

    # -- injected process boundary ----------------------------------------------

    def _request_kwargs(self, role, **overrides):
        arguments = {
            "controller_root": self.controller_root,
            "prompt_file": self.prompts[role],
            "plugin_root": self.plugins[role],
            "expected_skill": role,
            "allowed_tools": ("Read", "Glob"),
            "max_turns": 2,
            "max_budget_usd": 0.25,
        }
        arguments.update(overrides)
        return arguments

    def _starter(self, handle=None, seen=None):
        worker = handle if handle is not None else FakeHandle()

        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            if seen is not None:
                seen.append({"session_id": reserved.session_id, "state": reserved.state})
            bound = attach_process(
                store,
                reserved.session_id,
                pid=worker.pid,
                pid_domain="test-host",
                started_at=worker.started_at,
                bound_at="2026-09-04T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            return worker, bound

        return start, worker

    def _sender(self, sent=None):
        recorded = sent if sent is not None else []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            recorded.append(
                {
                    "mode": request.mode,
                    "session_id": request.session_id,
                    "role": request.role,
                    "prompt": prompt,
                    "expected_skill": request.expected_skill,
                }
            )
            return {
                "session_id": request.session_id,
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "total_cost_usd": 0.01,
                "events": [],
            }

        return send, recorded

    def _stopper(self, seen=None, gone=True):
        """`stop_session` probes `alive` before shutdown and again after.

        The handle must be live going in, or it is refused as stale before shutdown
        ever runs; the flag flips only when the fake shutdown reports the group gone.
        """
        state = {"alive": True}

        def stop(handle, **kwargs):
            if seen is not None:
                seen.append(handle.pgid)
            if gone:
                state["alive"] = False
            return {"graceful": True, "exit_code": 0, "process_group_gone": gone}

        def alive(pgid):
            return state["alive"]

        return stop, alive

    def _slots(self, *, ceiling=6, occupants=(), unprovable=()):
        return AgentSlots(
            ceiling=ceiling, occupants=tuple(occupants), unprovable=tuple(unprovable)
        )

    def _invoke(self, *, role=EXECUTOR, rail=None, snapshot=None, observation=None,
                slots=None, registry=None, store=None, sent=None, seen=None,
                stopped=None, **overrides):
        rail = rail if rail is not None else (
            EXECUTOR_RAIL if role == EXECUTOR else REVIEWER_RAIL
        )
        snapshot = snapshot if snapshot is not None else self._snapshot()
        observation = observation if observation is not None else self._observation()
        start, _worker = self._starter(seen=seen)
        send, recorded = self._sender(sent=sent)
        registry = registry if registry is not None else self.registry
        store = store if store is not None else self.store
        arguments = dict(
            store=store,
            registry=registry,
            reference=self.reference,
            request_kwargs=self._request_kwargs(role),
            package_root=self.repo_root,
            slots=slots if slots is not None else self._slots(),
            bindings=store.records(),
            launch_kwargs={"start": start, "send": send},
            stop_kwargs=dict(zip(("stop", "alive"), self._stopper(seen=stopped))),
        )
        arguments.update(overrides)
        packet = build_role_packet(snapshot, rail=rail, role=role)
        outcome = invoke_role(snapshot, packet, observation, **arguments)
        return outcome, recorded


class RoleLaunchTests(RoleInvocationTestBase):
    """The capability: a session really is started, in the role it was authorized for."""

    def test_an_executor_role_session_is_launched_bound_and_stopped(self):
        """The whole capability in one assertion set, for `executor`."""
        stopped = []
        outcome, sent = self._invoke(role=EXECUTOR, stopped=stopped)

        self.assertEqual(outcome.role, EXECUTOR)
        self.assertEqual(outcome.rail, EXECUTOR_RAIL)
        self.assertEqual(outcome.iteration_blob, EXECUTOR_BLOB)
        self.assertEqual(outcome.binding_state, "unbound")
        self.assertTrue(outcome.process_group_gone)
        # No wake produced this, and the outcome says so rather than inventing one.
        self.assertEqual(outcome.wake_rails, ())
        # The session was really sent the role's own directive, once, as a launch.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["mode"], "launch")
        self.assertEqual(sent[0]["prompt"], role_invocation.DIRECTIVES[EXECUTOR])
        self.assertEqual(sent[0]["session_id"], outcome.session_id)
        self.assertEqual(stopped, [FakeHandle().pgid])

    def test_a_reviewer_role_session_is_launched_bound_and_stopped(self):
        """The same, for `reviewer`, on the rail durably assigned to it."""
        outcome, sent = self._invoke(role=REVIEWER)

        self.assertEqual(outcome.role, REVIEWER)
        self.assertEqual(outcome.rail, REVIEWER_RAIL)
        self.assertEqual(outcome.iteration_blob, REVIEWER_BLOB)
        self.assertEqual(sent[0]["prompt"], role_invocation.DIRECTIVES[REVIEWER])
        self.assertNotEqual(
            role_invocation.DIRECTIVES[EXECUTOR], role_invocation.DIRECTIVES[REVIEWER]
        )

    def test_the_role_reaches_the_durable_binding_and_the_runtime_request(self):
        """Role fidelity is a property of the record and the request, not of a label.

        The role the caller asked for is the role the authorization was granted for,
        the role written into the durable binding, and the role carried on the
        runtime request the provider is invoked with. If any of those could differ,
        a session could run as one role under another role's authority.
        """
        for role, rail in ((EXECUTOR, EXECUTOR_RAIL), (REVIEWER, REVIEWER_RAIL)):
            with self.subTest(role=role):
                store = BindingStore(self.tmp_path / "store-{0}".format(role))
                outcome, sent = self._invoke(
                    role=role, store=store, registry=SessionRegistry()
                )
                record = store.read(outcome.session_id)
                self.assertEqual(record.role, role)
                self.assertEqual(record.rail, rail)
                self.assertEqual(sent[0]["role"], role)
                self.assertEqual(sent[0]["expected_skill"], role)

    def test_the_live_window_is_offered_once_with_the_session_really_running(self):
        """`while_running` is the one instant the launch is live, and it is real."""
        observed = {}

        def watch(launched):
            record = self.store.read(launched.owned.session_id)
            observed["binding_state"] = record.state
            observed["binding_role"] = record.role
            observed["owned"] = [
                owned.session_id for owned in self.registry.sessions()
            ]

        outcome, _sent = self._invoke(role=EXECUTOR, while_running=watch)
        self.assertEqual(observed["binding_state"], "bound")
        self.assertEqual(observed["binding_role"], EXECUTOR)
        self.assertEqual(observed["owned"], [outcome.session_id])
        # And afterwards the handle is gone, which is what makes the next launch
        # possible at all.
        self.assertEqual(self.registry.sessions(), [])


class RoleFidelityRefusalTests(RoleInvocationTestBase):
    """A role a rail did not grant cannot be launched, from either source of truth."""

    def test_a_rail_assigned_another_role_is_refused_from_the_snapshot(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, rail=REVIEWER_RAIL)
        self.assertEqual(caught.exception.reason, REASON_RAIL_ROLE)
        self.assertEqual(self.store.records(), [])

    def test_the_predicate_refuses_independently_when_the_observation_disagrees(self):
        """Two sources must agree; neither may stand in for the other.

        The snapshot says the rail is assigned to `executor` and the observation
        says `reviewer`. The snapshot check therefore passes and the accepted
        predicate is the one that refuses -- which is the point: the second source
        is load-bearing on its own.
        """
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(
                role=EXECUTOR,
                snapshot=self._snapshot(executor_role=EXECUTOR),
                observation=self._observation(executor_role=REVIEWER),
            )
        self.assertEqual(caught.exception.reason, "not-authorized")
        self.assertIn("rail-role-mismatch", str(caught.exception))
        self.assertEqual(self.store.records(), [])

    def test_a_rail_naming_no_role_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, snapshot=self._snapshot(executor_role=None))
        self.assertEqual(caught.exception.reason, REASON_RAIL_ROLE)
        self.assertIn("no role", str(caught.exception))


class OrchestratorIsRefusedTests(RoleInvocationTestBase):
    """This door cannot start an orchestrator, so the wake gate stays whole."""

    def test_a_packet_cannot_be_built_for_the_orchestrator_role(self):
        with self.assertRaises(InvocationRefused) as caught:
            build_role_packet(self._snapshot(), rail=EXECUTOR_RAIL, role=ORCHESTRATOR)
        self.assertEqual(caught.exception.reason, REASON_ROLE_NOT_LAUNCHABLE)

    def test_the_door_refuses_the_orchestrator_role_even_if_a_packet_carried_it(self):
        """Defence in depth: the door checks the value it would actually bind.

        A frozen dataclass can be mutated past `__post_init__` by
        `object.__setattr__`, so a packet is not a place a role can be trusted to
        stay. The enactment boundary checks it again, from the field that would be
        carried into the binding.
        """
        packet = build_role_packet(self._snapshot(), rail=EXECUTOR_RAIL, role=EXECUTOR)
        object.__setattr__(packet, "role", ORCHESTRATOR)
        with self.assertRaises(InvocationRefused) as caught:
            invoke_role(
                self._snapshot(),
                packet,
                self._observation(),
                store=self.store,
                registry=self.registry,
                reference=self.reference,
                request_kwargs=self._request_kwargs(EXECUTOR),
                package_root=self.repo_root,
                slots=self._slots(),
            )
        self.assertEqual(caught.exception.reason, REASON_ROLE_NOT_LAUNCHABLE)
        self.assertEqual(self.store.records(), [])

    def test_the_launchable_set_is_exactly_the_two_authorized_roles(self):
        self.assertEqual(set(LAUNCHABLE_ROLES), {EXECUTOR, REVIEWER})
        self.assertNotIn(ORCHESTRATOR, LAUNCHABLE_ROLES)
        self.assertEqual(set(role_invocation.DIRECTIVES), set(LAUNCHABLE_ROLES))


class SequentialOnlyTests(RoleInvocationTestBase):
    """One managed session at a time, refused by the door rather than trusted."""

    def _owned_stub(self, session_id):
        from ai_dev_flow.session_lifecycle import OwnedSession

        return OwnedSession(
            session_id=session_id,
            handle=FakeHandle(),
            pid=5151,
            pid_domain="test-host",
            pgid=5151,
            started_at="2026-09-04T12:00:02Z",
            iteration=RailIteration(rail=EXECUTOR_RAIL, blob=EXECUTOR_BLOB),
            workspace_path=str(self.workspace),
            role=EXECUTOR,
        )

    def test_a_controller_already_holding_a_session_is_refused(self):
        """The second launch never happens, and nothing of it is written down.

        The registry is put into the state a concurrent driver would put it in --
        one live handle still held -- and the door refuses before the predicate,
        before any reservation, and before any process could exist.
        """
        registry = SessionRegistry()
        registry.add(self._owned_stub("11111111-0000-4000-8000-00000000aaaa"))
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=REVIEWER, registry=registry)
        self.assertEqual(caught.exception.reason, REASON_SESSION_ALREADY_LIVE)
        self.assertEqual(self.store.records(), [])

    def test_the_same_call_with_an_empty_registry_is_admitted(self):
        """The discriminating half: the refusal is the registry's doing, nothing else.

        Every other argument is identical to the refused call above. Only the
        registry differs, so the fixture cannot pass by refusing everything.
        """
        outcome, _sent = self._invoke(role=REVIEWER, registry=SessionRegistry())
        self.assertEqual(outcome.role, REVIEWER)

    def test_the_refusal_reads_the_registry_it_would_launch_into(self):
        """Not a parameter: a caller cannot answer this question for the door.

        `in_flight_session_ids` and `bindings` are caller-stated and say nothing
        here; the held handle is read from the registry `launch_session` would
        actually add to.
        """
        registry = SessionRegistry()
        registry.add(self._owned_stub("22222222-0000-4000-8000-00000000bbbb"))
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(
                role=EXECUTOR,
                registry=registry,
                bindings=(),
                in_flight_session_ids=(),
            )
        self.assertEqual(caught.exception.reason, REASON_SESSION_ALREADY_LIVE)
        self.assertIn("22222222", str(caught.exception))

    def test_a_launch_leaves_the_registry_empty_so_the_next_one_is_possible(self):
        """Sequential means the door closes behind itself, not that it opens once."""
        registry = SessionRegistry()
        first, _a = self._invoke(role=EXECUTOR, registry=registry)
        self.assertEqual(registry.sessions(), [])
        second, _b = self._invoke(role=REVIEWER, registry=registry)
        self.assertEqual(registry.sessions(), [])
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual((first.role, second.role), (EXECUTOR, REVIEWER))

    def test_nothing_in_the_new_modules_can_hold_two_sessions_at_once(self):
        """Structural: no thread, no pool, no scheduler, no loop over launches."""
        import ai_dev_flow

        package = Path(ai_dev_flow.__file__).parent
        forbidden = (
            "threading",
            "multiprocessing",
            "concurrent.futures",
            "asyncio",
            "Thread(",
            "Pool(",
        )
        for name in ("role_invocation.py", "role_dispatch.py"):
            text = (package / name).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, "{0} mentions {1}".format(name, token))
        # Exactly one enactment per process, and exactly one door behind it.
        entry = (package / "role_dispatch.py").read_text(encoding="utf-8")
        self.assertEqual(entry.count("dispatch_role("), 1)
        body = entry.split("def main(", 1)[1]
        code = [
            line for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for keyword in ("for ", "while ", "await ", "async "):
            self.assertFalse(
                [line for line in code if line.strip().startswith(keyword)],
                "main() contains a {0}loop".format(keyword.strip()),
            )


class AdmissionControlTests(RoleInvocationTestBase):
    """D6 is evaluated at this door exactly as it is at the accepted one."""

    def _occupy(self, count, *, state=BINDING_STATE_RESERVED):
        """`count` reserved records, which is the shape that reaches the ceiling.

        Deliberately `reserved` rather than `bound`: `authorize` never subtracts
        `slots.unprovable`, so bound records this controller did not start answer
        `concurrency-count-unprovable` instead. A reservation occupies a slot on the
        durable record alone, which is what makes the ceiling reason reachable.
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
        self.assertEqual(len(self.store.records()), count)

    def test_the_ceiling_refuses_a_launch_and_nothing_is_reserved(self):
        self._occupy(6)
        controller = ManagerController(
            types.SimpleNamespace(
                control_plane=self.tmp_path,
                project=PROJECT,
                ticket=TICKET,
                binding_root=self.tmp_path / "controller-state",
            )
        )
        records = controller.store.records()
        slots = controller.occupancy(records)
        self.assertEqual(slots.occupied, 6)
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, slots=slots, bindings=records)
        self.assertEqual(caught.exception.reason, "not-authorized")
        self.assertIn("concurrency-ceiling-reached", str(caught.exception))
        self.assertEqual(len(self.store.records()), 6)

    def test_one_slot_below_the_ceiling_the_same_launch_is_admitted(self):
        """The discriminating half: the fixture must be able to say yes as well as no."""
        self._occupy(5)
        controller = ManagerController(
            types.SimpleNamespace(
                control_plane=self.tmp_path,
                project=PROJECT,
                ticket=TICKET,
                binding_root=self.tmp_path / "controller-state",
            )
        )
        records = controller.store.records()
        outcome, _sent = self._invoke(
            role=EXECUTOR, slots=controller.occupancy(records), bindings=records
        )
        self.assertEqual(outcome.role, EXECUTOR)

    def test_an_unprovable_total_refuses_rather_than_admitting(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, slots=self._slots(unprovable=("session-x",)))
        self.assertEqual(caught.exception.reason, "not-authorized")
        self.assertIn("concurrency-count-unprovable", str(caught.exception))
        self.assertEqual(self.store.records(), [])


class StandingAuthorizationTests(RoleInvocationTestBase):
    """Every accepted precondition still refuses on this door."""

    def test_a_rail_that_is_not_running_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(
                role=EXECUTOR,
                snapshot=self._snapshot(executor_status="blocked"),
                observation=self._observation(executor_status="blocked"),
            )
        self.assertEqual(caught.exception.reason, REASON_RAIL_NOT_RUNNING)

    def test_an_unreconciled_rail_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(
                role=EXECUTOR,
                snapshot=self._snapshot(executor_proposed="completed"),
                observation=self._observation(executor_unreconciled=True),
            )
        self.assertEqual(caught.exception.reason, REASON_RAIL_UNRECONCILED)

    def test_an_iteration_that_moved_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(
                role=EXECUTOR,
                snapshot=self._snapshot(executor_blob="9" * 40),
                observation=self._observation(),
            )
        self.assertEqual(caught.exception.reason, "not-authorized")
        self.assertIn("iteration-mismatch", str(caught.exception))

    def test_an_unproven_workspace_is_refused_before_anything_is_reserved(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, observation=self._observation(workspace="unproven"))
        # The accepted predicate reaches the workspace before this module's own
        # workspace rule does, and refuses first. Both are real; the earlier one wins.
        self.assertEqual(caught.exception.reason, "not-authorized")
        self.assertIn("workspace-identity-ambiguous", str(caught.exception))
        self.assertEqual(self.store.records(), [])

    def test_a_missing_rail_is_refused(self):
        with self.assertRaises(InvocationRefused) as caught:
            self._invoke(role=EXECUTOR, rail="no-such-rail")
        self.assertEqual(caught.exception.reason, "role-rail-missing")

    def test_there_is_no_parameter_for_an_authorization_decision(self):
        """The predicate is always called here; no caller can supply a verdict."""
        import inspect

        parameters = set(inspect.signature(invoke_role).parameters)
        self.assertNotIn("decision", parameters)
        self.assertNotIn("authorized", parameters)
        self.assertIn("slots", parameters)


class EntryPointTests(RoleInvocationTestBase):
    """The path is reachable from a `main()`, and states everything it spends."""

    def test_the_module_has_a_main_and_it_is_the_launch_path(self):
        self.assertTrue(callable(role_dispatch.main))
        source = Path(role_dispatch.__file__).read_text(encoding="utf-8")
        self.assertIn("dispatch_role(", source)
        self.assertIn('if __name__ == "__main__":', source)

    def test_the_role_must_be_stated(self):
        code = role_dispatch.main(
            [
                "--rail", EXECUTOR_RAIL,
                "--controller-root", str(self.controller_root),
                "--prompt-file", str(self.prompts[EXECUTOR]),
                "--plugin-root", str(self.plugins[EXECUTOR]),
                "--expected-skill", EXECUTOR,
                "--allowed-tool", "Read",
                "--max-turns", "2",
                "--max-budget-usd", "0.25",
                "--ticket-provider", "github",
                "--ticket-id", "55",
                "--control-plane", str(self.repo_root),
                "--project", PROJECT,
                "--ticket", TICKET,
                "--binding-root", str(self.tmp_path / "bindings"),
            ]
        )
        self.assertEqual(code, 1)

    def test_the_orchestrator_role_is_refused_at_the_command_line(self):
        code = role_dispatch.main(
            [
                "--rail", EXECUTOR_RAIL,
                "--role", ORCHESTRATOR,
                "--controller-root", str(self.controller_root),
                "--prompt-file", str(self.prompts[EXECUTOR]),
                "--plugin-root", str(self.plugins[EXECUTOR]),
                "--expected-skill", EXECUTOR,
                "--allowed-tool", "Read",
                "--max-turns", "2",
                "--max-budget-usd", "0.25",
                "--ticket-provider", "github",
                "--ticket-id", "55",
                "--control-plane", str(self.repo_root),
                "--project", PROJECT,
                "--ticket", TICKET,
                "--binding-root", str(self.tmp_path / "bindings"),
            ]
        )
        self.assertEqual(code, 1)

    def test_the_controller_pass_through_supplies_its_own_store_and_registry(self):
        source = Path(ManagerController.dispatch_role.__code__.co_filename).read_text(
            encoding="utf-8"
        )
        body = source.split("def dispatch_role(", 1)[1].split("def owned_session_ids", 1)[0]
        for expected in ("store=self.store", "registry=self.registry", "self.occupancy("):
            self.assertIn(expected, body)
        # No gate of its own: the pass-through adds no refusal and no policy.
        self.assertNotIn("raise", body)


if __name__ == "__main__":
    unittest.main()
