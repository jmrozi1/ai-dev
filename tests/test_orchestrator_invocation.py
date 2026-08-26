"""`orchestrator_invocation` requires two independent gates before one fresh orchestrator."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import json
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ai_dev_flow import orchestrator_invocation as invocation
from ai_dev_flow import orchestrator_trigger as trigger
from ai_dev_flow import session_lifecycle, workspaces
from ai_dev_flow.authorization import (
    OBSERVATION_PARTIAL,
    SOURCE_HEALTH_UNPUSHED,
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
    authorize,
)
from ai_dev_flow.claude_runtime import ClaudeRuntimeError
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BindingStore,
    RailIteration,
    attach_process,
    reserve_binding,
)
from ai_dev_flow.session_lifecycle import (
    STATE_DISCONNECTED,
    LifecycleError,
    RailFacts,
    SessionRegistry,
    observe_session,
)
from ai_dev_flow.tickets import TicketReference

PROJECT = "ai-dev"
TICKET = "issue-55"
HEAD = "c" * 40
OTHER_HEAD = "e" * 40
STATE_BLOB = "5" * 40

ORCH_RAIL = "issue-55-standing-orchestration"
SOURCE_RAIL = "issue-55-source-work"
ORCH_BLOB = "a" * 40
SOURCE_BLOB = "b" * 40
SOURCE_HANDOFF_BLOB = "d" * 40

SESSION_ONE = "1a2b3c4d-0001-4000-8000-00000000000a"
SESSION_TWO = "1a2b3c4d-0002-4000-8000-00000000000b"
SKILL = "orchestrator"


class FakeHandle(object):
    """A worker handle with no process behind it. Nothing here spawns anything."""

    def __init__(self, pid=4242, pgid=4242, started_at="2026-08-26T12:00:02Z"):
        self.pid = pid
        self.pgid = pgid
        self.started_at = started_at
        self.sdk_version = "0.2.139"
        self.sdk_detail = None
        self.process = types.SimpleNamespace(returncode=0)

    @property
    def sdk_available(self):
        return True


class InvocationTestBase(unittest.TestCase):
    """Real worktrees and claims, injected workers. No process, no provider, no SDK."""

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

        self.store = BindingStore(self.tmp_path / "controller-state")
        self.registry = SessionRegistry()
        self.clock = "2026-08-26T12:00:00Z"

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
        self._git(repo_root, "config", "user.name", "Invocation Tests")
        self._git(repo_root, "config", "user.email", "invocation@example.com")
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

    # -- snapshot, wake, packet, observation ------------------------------------

    def _snapshot(self, *, head=HEAD, orch_status="running", orch_unreconciled=False,
                  orch_present=True, source_handoff=SOURCE_HANDOFF_BLOB, orch_blob=ORCH_BLOB):
        rails = []
        if orch_present:
            rails.append(
                trigger.RailSnapshot(
                    identifier=ORCH_RAIL,
                    authorization_blob=orch_blob,
                    status=orch_status,
                    proposed_status="completed" if orch_unreconciled else None,
                )
            )
        rails.append(
            trigger.RailSnapshot(
                identifier=SOURCE_RAIL,
                authorization_blob=SOURCE_BLOB,
                status="running",
                proposed_status="completed",
                handoff_blob=source_handoff,
            )
        )
        return trigger.ScopeSnapshot(
            project=PROJECT,
            ticket=TICKET,
            head=head,
            state_blob=STATE_BLOB,
            rails=tuple(sorted(rails, key=lambda entry: entry.identifier)),
        )

    def _proposal(self, snapshot=None, rails=(SOURCE_RAIL,), head=None, project=PROJECT,
                  ticket=TICKET, reasons=None):
        snapshot = snapshot if snapshot is not None else self._snapshot()
        if reasons is None:
            reasons = []
            for name in rails:
                entry = snapshot.rail(name)
                reasons.append(
                    trigger.WakeReason(
                        kind=trigger.WAKE_UNRECONCILED_HANDOFF,
                        rail=name,
                        fingerprint=(trigger.WAKE_UNRECONCILED_HANDOFF,)
                        + entry.material_fingerprint,
                    )
                )
        return trigger.WakeProposal(
            project=project,
            ticket=ticket,
            head=head if head is not None else snapshot.head,
            reasons=tuple(reasons),
        )

    def _packet(self, snapshot=None):
        return trigger.build_packet(snapshot if snapshot is not None else self._snapshot())

    def _observation(self, *, head=HEAD, orch_status="running", orch_unreconciled=False,
                     orch_blob=ORCH_BLOB, workspace="ok", completeness=None,
                     source_health=None, holders=None):
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

        arguments = dict(
            project=PROJECT,
            ticket=TICKET,
            head=head,
            rails=(
                RailObservation(
                    identifier=ORCH_RAIL,
                    status=orch_status,
                    rail_blob=orch_blob,
                    unreconciled=orch_unreconciled,
                    shared_resource="orchestration-slot" if holders else None,
                ),
                RailObservation(
                    identifier=SOURCE_RAIL, status="running", rail_blob=SOURCE_BLOB,
                    unreconciled=True,
                ),
            ),
            workspace=observed,
        )
        if completeness is not None:
            arguments["completeness"] = completeness
        if source_health is not None:
            arguments["source_health"] = source_health
        if holders is not None:
            arguments["foreign_resource_holders"] = holders
        return ControlPlaneObservation(**arguments)

    # -- injected process boundary ----------------------------------------------

    def _request_kwargs(self, **overrides):
        arguments = {
            "controller_root": self.controller_root,
            "prompt_file": self.prompt_file,
            "plugin_root": self.plugin_root,
            "expected_skill": SKILL,
            "allowed_tools": ("Read", "Glob"),
            "max_turns": 3,
            "max_budget_usd": 0.5,
        }
        arguments.update(overrides)
        return arguments

    def _starter(self, handle=None, record_calls=None, fail=None):
        worker = handle if handle is not None else FakeHandle()

        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            if record_calls is not None:
                record_calls.append({"session_id": reserved.session_id, "state": reserved.state})
            if fail is not None:
                raise fail
            bound = attach_process(
                store,
                reserved.session_id,
                pid=worker.pid,
                pid_domain="test-host",
                started_at=worker.started_at,
                bound_at="2026-08-26T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            return worker, bound

        return start, worker

    def _sender(self, fail=None, record_calls=None):
        sent = record_calls if record_calls is not None else []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            sent.append(
                {"mode": request.mode, "session_id": request.session_id, "prompt": prompt}
            )
            if fail is not None:
                raise fail
            return {
                "type": "result",
                "session_id": request.session_id,
                "mode": request.mode,
                "subtype": "success",
                "is_error": False,
                "assistant_text": "SECRET-ASSISTANT-TEXT",
            }

        return send, sent

    def _stopper(self, gone=True, record_calls=None):
        """stop_session probes `alive` before shutdown and again after.

        The handle must be live going in, so a constant False would be refused as a
        stale handle before shutdown ever ran. The flag flips only when the fake
        shutdown actually reports the group gone.
        """
        calls = record_calls if record_calls is not None else []
        state = {"alive": True}

        def stop(handle, **kwargs):
            calls.append(handle.pgid)
            if gone:
                state["alive"] = False
            return {"process_group_gone": gone, "graceful": True, "exit_code": 0}

        def alive(pgid):
            return state["alive"]

        return stop, calls, alive

    def _invoke(self, **overrides):
        snapshot = overrides.pop("snapshot", None) or self._snapshot()
        proposal = overrides.pop("proposal", "default")
        if proposal == "default":
            proposal = self._proposal(snapshot)
        packet = overrides.pop("packet", None) or self._packet(snapshot)
        observation = overrides.pop("observation", None) or self._observation()

        start = overrides.pop("start", None)
        send = overrides.pop("send", None)
        stop = overrides.pop("stop", None)
        alive = overrides.pop("alive", None)
        session_ids = overrides.pop("session_ids", [SESSION_ONE])
        if start is None:
            start, _ = self._starter()
        if send is None:
            send, _ = self._sender()
        if stop is None:
            stop, _, alive = self._stopper()
        if alive is None:
            alive = lambda pgid: True

        minted = list(session_ids)

        arguments = {
            "orchestrator_rail": ORCH_RAIL,
            "store": self.store,
            "registry": self.registry,
            "reference": self.reference,
            "request_kwargs": self._request_kwargs(),
            "package_root": self.repo_root,
            "launch_kwargs": {
                "now": lambda: self.clock,
                "new_session_id": lambda: minted.pop(0),
                "start": start,
                "send": send,
                "stop": stop,
            },
            "stop_kwargs": {"stop": stop, "alive": alive},
        }
        arguments.update(overrides)
        return invocation.invoke_orchestrator(snapshot, proposal, packet, observation, **arguments)


# --------------------------------------------------------------------------
# The happy path and the composed seam
# --------------------------------------------------------------------------


class SuccessfulInvocationTests(InvocationTestBase):
    def test_both_gates_satisfied_runs_one_fresh_orchestrator_and_terminalizes_it(self) -> None:
        outcome = self._invoke()

        self.assertEqual(outcome.project, PROJECT)
        self.assertEqual(outcome.ticket, TICKET)
        self.assertEqual(outcome.head, HEAD)
        self.assertEqual(outcome.rail, ORCH_RAIL)
        self.assertEqual(outcome.role, invocation.ORCHESTRATOR_ROLE)
        self.assertEqual(outcome.session_id, SESSION_ONE)
        self.assertEqual(outcome.iteration_blob, ORCH_BLOB)
        self.assertEqual(outcome.wake_rails, (SOURCE_RAIL,))
        self.assertEqual(outcome.binding_state, BINDING_STATE_UNBOUND)
        self.assertTrue(outcome.process_group_gone)

        record = self.store.read(SESSION_ONE)
        self.assertEqual(record.state, BINDING_STATE_UNBOUND)
        self.assertEqual(record.role, invocation.ORCHESTRATOR_ROLE)
        self.assertEqual(record.rail, ORCH_RAIL)
        self.assertIsNone(self.registry.get(SESSION_ONE))

    def test_the_packet_directive_is_the_prompt_and_the_request_is_launch_only(self) -> None:
        send, sent = self._sender()
        self._invoke(send=send)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["prompt"], trigger.DIRECTIVE)
        self.assertEqual(sent[0]["mode"], "launch")
        self.assertEqual(sent[0]["session_id"], SESSION_ONE)

    def test_the_request_is_built_while_the_binding_is_still_reserved(self) -> None:
        seen = []
        start, _ = self._starter(record_calls=seen)
        self._invoke(start=start)
        self.assertEqual(seen, [{"session_id": SESSION_ONE, "state": BINDING_STATE_RESERVED}])

    def test_the_outcome_carries_no_provider_content_or_live_handle(self) -> None:
        outcome = self._invoke()
        names = {entry.name for entry in fields(invocation.InvocationOutcome)}
        for forbidden in ("handle", "result", "output", "transcript", "response", "text", "prompt"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(any(forbidden in name for name in names))
        self.assertNotIn("SECRET-ASSISTANT-TEXT", repr(outcome))

    def test_the_accepted_seams_are_actually_called_in_order(self) -> None:
        order = []
        real_authorize = invocation.authorize
        real_launch = invocation.launch_session
        real_stop = invocation.stop_session

        def spy(name, target):
            def wrapper(*args, **kwargs):
                order.append(name)
                return target(*args, **kwargs)

            return wrapper

        with mock.patch.object(invocation, "authorize", spy("authorize", real_authorize)), \
             mock.patch.object(invocation, "launch_session", spy("launch", real_launch)), \
             mock.patch.object(invocation, "stop_session", spy("stop", real_stop)):
            self._invoke()
        self.assertEqual(order, ["authorize", "launch", "stop"])

    def test_no_caller_can_supply_a_fabricated_authorization_decision(self) -> None:
        import inspect

        parameters = set(inspect.signature(invocation.invoke_orchestrator).parameters)
        for forbidden in ("decision", "authorized", "authorize", "authorize_with"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_it_composes_with_a_real_control_plane_read(self) -> None:
        plane = self.tmp_path / "plane"
        (plane / PROJECT / TICKET / "rails" / ORCH_RAIL).mkdir(parents=True)
        (plane / PROJECT / TICKET / "rails" / SOURCE_RAIL).mkdir(parents=True)
        self._git_init_plane(plane)
        scope = plane / PROJECT / TICKET
        (scope / "state.md").write_text("# Control Plane State\n", encoding="utf-8")
        for rail, status in ((ORCH_RAIL, "running"), (SOURCE_RAIL, "running")):
            (scope / "rails" / rail / "rail.md").write_text(
                "# Rail: {0}\n\nStatus: {1}\nOwner: orchestrator\n\n## Goal\n\nwork\n".format(
                    rail, status
                ),
                encoding="utf-8",
            )
        (scope / "rails" / SOURCE_RAIL / "handoff.md").write_text(
            "# Handoff\n\nStatus: completed\n\n## Delivered\n\nwork\n", encoding="utf-8"
        )
        self._git(plane, "add", "-A")
        self._git(plane, "commit", "-q", "-m", "publish")
        head = self._git(plane, "rev-parse", "HEAD")
        before = self._git(plane, "status", "--porcelain")

        from ai_dev_flow.control_plane import ReadSource

        snapshot = trigger.build_snapshot(
            ReadSource(plane, head, head), project=PROJECT, ticket=TICKET
        )
        proposal = trigger.propose_wake(snapshot)
        packet = trigger.build_packet(snapshot)
        self.assertIsNotNone(proposal)

        observation = ControlPlaneObservation(
            project=PROJECT,
            ticket=TICKET,
            head=head,
            rails=(
                RailObservation(
                    identifier=ORCH_RAIL,
                    status="running",
                    rail_blob=snapshot.rail(ORCH_RAIL).authorization_blob,
                ),
                RailObservation(
                    identifier=SOURCE_RAIL,
                    status="running",
                    rail_blob=snapshot.rail(SOURCE_RAIL).authorization_blob,
                    unreconciled=True,
                ),
            ),
            workspace=WorkspaceObservation(
                workspace_key=self.workspace_key,
                worktree_id=self.worktree_id,
                workspace_path=str(self.workspace),
            ),
        )

        outcome = self._invoke(
            snapshot=snapshot, proposal=proposal, packet=packet, observation=observation
        )
        self.assertEqual(outcome.head, head)
        self.assertEqual(outcome.wake_rails, (SOURCE_RAIL,))
        self.assertEqual(outcome.binding_state, BINDING_STATE_UNBOUND)
        self.assertEqual(self._git(plane, "rev-parse", "HEAD"), head)
        self.assertEqual(self._git(plane, "status", "--porcelain"), before)

    def _git_init_plane(self, plane: Path) -> None:
        self._git(plane, "init", "-q")
        self._git(plane, "config", "user.name", "Plane")
        self._git(plane, "config", "user.email", "plane@example.com")


# --------------------------------------------------------------------------
# Gate one: the material wake
# --------------------------------------------------------------------------


class WakeGateTests(InvocationTestBase):
    def _refused(self, **overrides):
        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(**overrides)
        self.assertEqual(self.store.records(), [])
        return caught.exception.reason

    def test_a_missing_wake_refuses(self) -> None:
        self.assertEqual(self._refused(proposal=None), invocation.REASON_NO_MATERIAL_WAKE)

    def test_an_empty_wake_refuses(self) -> None:
        empty = trigger.WakeProposal(project=PROJECT, ticket=TICKET, head=HEAD, reasons=())
        self.assertEqual(self._refused(proposal=empty), invocation.REASON_NO_MATERIAL_WAKE)

    def test_a_wake_from_another_head_refuses(self) -> None:
        snapshot = self._snapshot()
        stale = self._proposal(snapshot, head=OTHER_HEAD)
        self.assertEqual(
            self._refused(snapshot=snapshot, proposal=stale),
            invocation.REASON_WAKE_SCOPE_MISMATCH,
        )

    def test_a_wake_from_another_scope_refuses(self) -> None:
        snapshot = self._snapshot()
        foreign = self._proposal(snapshot, project="other-proj")
        self.assertEqual(
            self._refused(snapshot=snapshot, proposal=foreign),
            invocation.REASON_WAKE_SCOPE_MISMATCH,
        )

    def test_a_wake_naming_a_rail_absent_from_the_snapshot_refuses(self) -> None:
        snapshot = self._snapshot()
        ghost = trigger.WakeReason(
            kind=trigger.WAKE_UNRECONCILED_HANDOFF,
            rail="issue-55-ghost-rail",
            fingerprint=(trigger.WAKE_UNRECONCILED_HANDOFF, "issue-55-ghost-rail", "x", "", ""),
        )
        self.assertEqual(
            self._refused(snapshot=snapshot, proposal=self._proposal(snapshot, reasons=[ghost])),
            invocation.REASON_WAKE_STALE,
        )

    def test_a_wake_whose_rail_moved_on_refuses(self) -> None:
        older = self._snapshot()
        proposal = self._proposal(older)
        newer = self._snapshot(source_handoff="9" * 40)
        self.assertEqual(
            self._refused(snapshot=newer, proposal=proposal, packet=self._packet(newer)),
            invocation.REASON_WAKE_STALE,
        )

    def test_a_wake_naming_the_dedicated_orchestrator_rail_refuses(self) -> None:
        snapshot = self._snapshot(orch_unreconciled=True)
        self.assertEqual(
            self._refused(
                snapshot=snapshot,
                proposal=self._proposal(snapshot, rails=(ORCH_RAIL,)),
                packet=self._packet(snapshot),
            ),
            invocation.REASON_SELF_WAKE,
        )

    def test_self_wake_is_refused_even_alongside_a_legitimate_reason(self) -> None:
        snapshot = self._snapshot(orch_unreconciled=True)
        self.assertEqual(
            self._refused(
                snapshot=snapshot,
                proposal=self._proposal(snapshot, rails=(SOURCE_RAIL, ORCH_RAIL)),
                packet=self._packet(snapshot),
            ),
            invocation.REASON_SELF_WAKE,
        )


# --------------------------------------------------------------------------
# Gate two: the standing orchestrator authorization
# --------------------------------------------------------------------------


class StandingAuthorizationGateTests(InvocationTestBase):
    def _refused(self, **overrides):
        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(**overrides)
        self.assertEqual(self.store.records(), [])
        return caught.exception

    def test_a_missing_dedicated_rail_refuses(self) -> None:
        snapshot = self._snapshot(orch_present=False)
        self.assertEqual(
            self._refused(snapshot=snapshot, packet=self._packet(snapshot)).reason,
            invocation.REASON_RAIL_MISSING,
        )

    def test_a_dedicated_rail_that_is_not_running_refuses(self) -> None:
        for status in ("ready", "blocked", "completed"):
            with self.subTest(status=status):
                self.store = BindingStore(self.tmp_path / ("state-" + status))
                snapshot = self._snapshot(orch_status=status)
                self.assertEqual(
                    self._refused(snapshot=snapshot, packet=self._packet(snapshot)).reason,
                    invocation.REASON_RAIL_NOT_RUNNING,
                )

    def test_an_unreconciled_dedicated_rail_refuses(self) -> None:
        snapshot = self._snapshot(orch_unreconciled=True)
        self.assertEqual(
            self._refused(snapshot=snapshot, packet=self._packet(snapshot)).reason,
            invocation.REASON_RAIL_UNRECONCILED,
        )

    def test_the_generic_predicate_refuses_an_observation_that_disagrees(self) -> None:
        cases = {
            "iteration-mismatch": dict(observation=self._observation(orch_blob="9" * 40)),
            "head-mismatch": dict(observation=self._observation(head=OTHER_HEAD)),
            "observation-incomplete": dict(
                observation=self._observation(completeness=OBSERVATION_PARTIAL)
            ),
            "source-unhealthy": dict(
                observation=self._observation(source_health=SOURCE_HEALTH_UNPUSHED)
            ),
            "rail-not-dispatched": dict(observation=self._observation(orch_status="ready")),
            "rail-unreconciled": dict(observation=self._observation(orch_unreconciled=True)),
            "resource-contended": dict(
                observation=self._observation(holders={"orchestration-slot": ("other-scope",)})
            ),
            "workspace-identity-ambiguous": dict(
                observation=self._observation(workspace="unproven")
            ),
        }
        for expected, overrides in cases.items():
            with self.subTest(expected=expected):
                self.store = BindingStore(self.tmp_path / ("state-" + expected))
                refusal = self._refused(**overrides)
                self.assertEqual(refusal.reason, invocation.REASON_NOT_AUTHORIZED)
                self.assertIn(expected, refusal.detail)

    def test_a_workspace_absent_from_the_observation_refuses(self) -> None:
        refusal = self._refused(observation=self._observation(workspace=None))
        self.assertEqual(refusal.reason, invocation.REASON_NOT_AUTHORIZED)
        self.assertIn("workspace-identity-ambiguous", refusal.detail)


# --------------------------------------------------------------------------
# Continuation is refused, never taken
# --------------------------------------------------------------------------


class ContinuationRefusalTests(InvocationTestBase):
    def _bind_existing(self, session_id=SESSION_TWO):
        iteration = RailIteration(rail=ORCH_RAIL, blob=ORCH_BLOB)
        reserved = reserve_binding(
            self.store,
            project=PROJECT,
            ticket=TICKET,
            reference=self.reference,
            workspace_path=self.workspace,
            worktree_id=self.worktree_id,
            rail=ORCH_RAIL,
            role=invocation.ORCHESTRATOR_ROLE,
            iteration=iteration,
            session_id=session_id,
            launched_at_head=HEAD,
            reserved_at=self.clock,
        )
        return reserved

    def test_an_existing_bound_orchestrator_refuses_instead_of_continuing(self) -> None:
        reserved = self._bind_existing()
        bound = attach_process(
            self.store,
            reserved.session_id,
            pid=999,
            pid_domain="test-host",
            started_at="2026-08-26T11:00:00Z",
            bound_at="2026-08-26T11:00:01Z",
        )
        self.assertEqual(bound.state, BINDING_STATE_BOUND)

        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(bindings=(bound,))
        self.assertEqual(caught.exception.reason, invocation.REASON_CONTINUATION_REFUSED)
        self.assertEqual(self.store.read(SESSION_TWO).state, BINDING_STATE_BOUND)
        self.assertIsNone(self.store.read(SESSION_ONE))

    def test_an_existing_reserved_orchestrator_refuses_and_does_not_relaunch(self) -> None:
        reserved = self._bind_existing()
        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(bindings=(reserved,))
        self.assertEqual(caught.exception.reason, invocation.REASON_NOT_AUTHORIZED)
        self.assertIn("binding-not-ready", caught.exception.detail)
        self.assertIsNone(self.store.read(SESSION_ONE))

    def test_the_module_cannot_continue_a_session_at_all(self) -> None:
        source = Path(invocation.__file__).with_suffix(".py").read_text(encoding="utf-8")
        for forbidden in ("continue_session", "resume_request", "ACTION_CONTINUE"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


# --------------------------------------------------------------------------
# The packet must still be current
# --------------------------------------------------------------------------


class PacketCurrencyTests(InvocationTestBase):
    def test_a_packet_bound_to_an_older_head_stops_the_invocation(self) -> None:
        older = self._snapshot()
        packet = self._packet(older)
        newer = self._snapshot(head=OTHER_HEAD)
        proposal = self._proposal(newer)

        # The observation still agrees with the packet, so the accepted predicate
        # authorizes; the only thing left to notice the drift is require_current.
        with self.assertRaises(trigger.TriggerError) as caught:
            self._invoke(
                snapshot=newer,
                proposal=proposal,
                packet=packet,
                observation=self._observation(head=HEAD),
            )
        self.assertEqual(caught.exception.reason, trigger.REASON_PACKET_STALE)
        self.assertEqual(self.store.records(), [])

    def test_a_packet_and_observation_from_an_older_head_are_refused_by_authorization(self) -> None:
        older = self._snapshot()
        newer = self._snapshot(head=OTHER_HEAD)
        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(
                snapshot=newer,
                proposal=self._proposal(newer),
                packet=self._packet(older),
                observation=self._observation(head=OTHER_HEAD),
            )
        self.assertEqual(caught.exception.reason, invocation.REASON_NOT_AUTHORIZED)
        self.assertIn("head-mismatch", caught.exception.detail)
        self.assertEqual(self.store.records(), [])

    def test_a_packet_addressing_another_role_is_refused(self) -> None:
        packet = self._packet()
        object.__setattr__(packet, "role", "executor")
        with self.assertRaises(invocation.InvocationRefused) as caught:
            self._invoke(packet=packet)
        self.assertEqual(caught.exception.reason, invocation.REASON_PACKET_ROLE)


# --------------------------------------------------------------------------
# One shot at a time, then genuinely new
# --------------------------------------------------------------------------


class SequentialInvocationTests(InvocationTestBase):
    def test_two_distinct_wakes_produce_two_distinct_terminal_sessions(self) -> None:
        send, sent = self._sender()
        first = self._invoke(send=send, session_ids=[SESSION_ONE])

        second_snapshot = self._snapshot(source_handoff="9" * 40)
        second = self._invoke(
            snapshot=second_snapshot,
            proposal=self._proposal(second_snapshot),
            packet=self._packet(second_snapshot),
            send=send,
            session_ids=[SESSION_TWO],
        )

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual({first.session_id, second.session_id}, {SESSION_ONE, SESSION_TWO})
        for session_id in (SESSION_ONE, SESSION_TWO):
            self.assertEqual(self.store.read(session_id).state, BINDING_STATE_UNBOUND)
        self.assertEqual([entry["mode"] for entry in sent], ["launch", "launch"])


# --------------------------------------------------------------------------
# Failure is truthful and never retried
# --------------------------------------------------------------------------


class FailureTruthTests(InvocationTestBase):
    def test_a_provider_failure_after_binding_stays_nonterminal_and_projects_disconnected(self):
        seen = []
        start, worker = self._starter(record_calls=seen)
        send, sent = self._sender(fail=RuntimeError("provider refused"))

        with self.assertRaises(LifecycleError) as caught:
            self._invoke(start=start, send=send)
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_LAUNCH_FAILED)

        record = self.store.read(SESSION_ONE)
        self.assertEqual(record.state, BINDING_STATE_BOUND)
        self.assertFalse(record.is_terminal)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(sent), 1)

        projection = observe_session(
            RailFacts(identifier=ORCH_RAIL, status="running", rail_blob=ORCH_BLOB),
            record,
            self.registry,
            now="2026-08-26T12:05:00Z",
        )
        self.assertEqual(projection.state, STATE_DISCONNECTED)

    def test_a_spawn_failure_preserves_the_reservation_and_does_not_retry(self) -> None:
        seen = []
        start, _ = self._starter(record_calls=seen, fail=RuntimeError("spawn refused"))
        with self.assertRaises(RuntimeError):
            self._invoke(start=start)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_RESERVED)

    def test_a_request_construction_failure_starts_no_process(self) -> None:
        seen = []
        start, _ = self._starter(record_calls=seen)
        with self.assertRaises(ClaudeRuntimeError):
            self._invoke(
                start=start,
                request_kwargs=self._request_kwargs(prompt_file=self.controller_root / "gone.md"),
            )
        self.assertEqual(seen, [])
        self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_RESERVED)

    def test_an_unproven_shutdown_leaves_the_binding_nonterminal(self) -> None:
        stop, calls, alive = self._stopper(gone=False)
        with self.assertRaises(LifecycleError) as caught:
            self._invoke(stop=stop, alive=alive)
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE)
        self.assertEqual(self.store.read(SESSION_ONE).state, BINDING_STATE_BOUND)
        self.assertEqual(len(calls), 1)


# --------------------------------------------------------------------------
# The module decides and enacts; it does not write, judge, or wait
# --------------------------------------------------------------------------


class ModulePurityTests(InvocationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.source_text = Path(invocation.__file__).with_suffix(".py").read_text(encoding="utf-8")

    def _absent(self, tokens, *, lowered=False) -> None:
        haystack = self.source_text.lower() if lowered else self.source_text
        self.assertEqual(
            [token for token in tokens if token in haystack],
            [],
            msg="orchestrator_invocation.py must not mention these",
        )

    def test_it_never_writes_the_control_plane_or_parses_its_prose(self) -> None:
        self._absent(
            (
                "publish(",
                "write_text",
                "write_text_atomic",
                "allocate_proceed_number",
                "collect_rail_states",
                "splitlines",
                "state.md",
                "mkdir",
            )
        )

    def test_it_starts_no_process_and_keeps_no_timing_machinery(self) -> None:
        self._absent(
            (
                "subprocess",
                "Popen",
                "os.kill",
                "killpg",
                "claude_agent_sdk",
                "threading",
                "Timer",
                "sleep",
                "datetime",
                "import time",
                "time.time",
                "perf_counter",
            )
        )

    def test_it_has_no_counter_retry_or_scoring_machinery(self) -> None:
        self._absent(("retry", "attempts", "score", "backoff", "threshold", "counter"), lowered=True)

    def test_it_implements_no_review_or_human_queue_behavior(self) -> None:
        self._absent(("review", "queue", "escalat", "pending_human_decision"), lowered=True)

    def test_it_reuses_the_accepted_predicates_rather_than_copying_them(self) -> None:
        self.assertEqual(
            [
                name
                for name in ("authorize", "launch_session", "stop_session", "require_current")
                if name not in self.source_text
            ],
            [],
            msg="orchestrator_invocation.py must call the accepted seams",
        )
        self._absent(("_require_decision", "reserve_binding", "attach_process", "unbind_session"))


if __name__ == "__main__":
    unittest.main()
