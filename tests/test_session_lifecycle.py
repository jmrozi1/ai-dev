from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow import claude_worker, session_lifecycle, workspaces
from ai_dev_flow.authorization import (
    ACTION_CONTINUE,
    ACTION_LAUNCH,
    REASON_CONCURRENCY_CEILING,
    AuthorizationDecision,
    ControlPlaneObservation,
    RailObservation,
    WorkspaceObservation,
    authorize,
    reconcile_agent_slots,
)
from ai_dev_flow.claude_runtime import ClaudeRuntimeError
from ai_dev_flow.context_lifecycle import (
    CONTEXT_POLICY_FRESH,
    CONTEXT_POLICY_PERSISTENT,
    EVENT_COMPACTION_OBSERVED,
    OBSERVATION_HEALTHY,
    OBSERVATION_UNAVAILABLE,
    OBSERVATION_UNHEALTHY,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BindingStore,
    RailIteration,
    SessionBindingError,
    attach_process,
    reserve_binding,
    unbind_session,
)
from ai_dev_flow.session_lifecycle import (
    RETIREMENT_REFUSED,
    RETIREMENT_RETIRED,
    ROTATION_NOT_READY,
    ROTATION_READY,
    STATE_DISCONNECTED,
    STATE_RUNNING,
    STATE_WAITING,
    Assignment,
    LifecycleError,
    OwnedSession,
    RailFacts,
    RotationHandoffFacts,
    SessionRegistry,
    WorktreeFacts,
    continue_session,
    elapsed_seconds,
    evaluate_rotation_readiness,
    launch_session,
    observe_session,
    recover_session,
    require_owned,
    retire_old_context,
    single_liveness_snapshot,
    stop_session,
)
from ai_dev_flow.tickets import TicketReference


RAIL = "issue-55-session-lifecycle-reconciliation"
OTHER_RAIL = "issue-55-agent-sdk-worker-integration"
BLOB = "a" * 40
OTHER_BLOB = "b" * 40
HEAD = "c" * 40
SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"
# The successor a rotation binds. A distinct identity, because a replacement
# that reused its predecessor's would read as a continuation of work it never did.
SUCCESSOR = "1a2b3c4d-0003-4000-8000-00000000000c"
SKILL = "executor"
TOOLS = ("Read", "Glob")
# Git object names of published handoff bytes. Two of them, because currency is a
# question about *which* publication is there, and one name can never answer it.
PUBLICATION = "d" * 40
NEXT_PUBLICATION = "e" * 40
# Where the product repository stands, before and after one commit lands. Two of
# them for the same reason: whether a publication is current is a question about
# *which* state it was written against, and one name can never answer it.
PRODUCT_HEAD = "1" * 40
NEXT_PRODUCT_HEAD = "2" * 40


_UNSET = object()


class _PublishedHandoff(object):
    """The caller's control-plane read of which handoff publication currently stands.

    Stands in for `rail_handoff_publication`'s object name, which is what a real
    caller passes. It is a value the agent moves by publishing, never something the
    lifecycle writes: nothing in the module under test can change what this returns.

    `work_state` models the one thing the real `publish` does that a bare value
    cannot: it captures the product state *at the instant of publication*, which is
    the fact this module has no way to take for itself. That the real helper
    actually captures it there, against a real repository, is proved in
    `tests/test_control_plane.py`, not assumed here.
    """

    def __init__(self, value=None):
        self.value = value
        self.work_state = None

    def publish(self, value, work_state):
        self.value = value
        self.work_state = work_state

    def __call__(self):
        return self.value


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


class LifecycleTestBase(unittest.TestCase):
    """Real worktrees and claims, injected workers. No process, no provider, no SDK."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()
        self.repo_root = self._init_repo("product")
        self.reference = TicketReference(
            provider="github", ticket_id="55", repository="jmrozi1/ai-dev"
        )
        self.workspace, self.worktree_id = self._add_workspace("workspace-55")

        self.controller_root = self.tmp_path / "controller"
        self.prompt_file = self.controller_root / "prompts" / "executor.md"
        self.prompt_file.parent.mkdir(parents=True)
        self.prompt_file.write_text("bounded executor\n", encoding="utf-8")
        plugin = self.controller_root / "plugins" / "ai-dev-executor"
        (plugin / "skills" / SKILL).mkdir(parents=True)
        (plugin / "skills" / SKILL / "SKILL.md").write_text("---\nname: executor\n---\n", encoding="utf-8")
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "ai-dev-executor"}) + "\n", encoding="utf-8"
        )
        self.plugin_root = plugin

        self.store = BindingStore(self.tmp_path / "controller-state")
        self.registry = SessionRegistry()
        self.iteration = RailIteration(rail=RAIL, blob=BLOB)
        self.assignment = Assignment(
            project="ai-dev", ticket="issue-55", rail=RAIL, role="executor", head=HEAD,
            iteration=self.iteration, workspace_key="github:jmrozi1/ai-dev#55",
            worktree_id=self.worktree_id, workspace_path=str(self.workspace),
        )
        self.clock = "2026-08-26T12:00:00Z"

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_root), "worktree", "prune"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._tmpdir.cleanup()

    def _git(self, repo_root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args], check=True, text=True,
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)
        self._git(repo_root, "init", "-q")
        self._git(repo_root, "config", "user.name", "Lifecycle Tests")
        self._git(repo_root, "config", "user.email", "lifecycle@example.com")
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
            path, reference=self.reference, worktree_id=worktree_id,
            workspace_path=path, branch=branch,
        )
        return path, worktree_id

    # -- injected collaborators -------------------------------------------------

    def _request_kwargs(self, **overrides):
        arguments = {
            "controller_root": self.controller_root,
            "prompt_file": self.prompt_file,
            "plugin_root": self.plugin_root,
            "expected_skill": SKILL,
            "allowed_tools": TOOLS,
            "max_turns": 3,
            "max_budget_usd": 0.5,
        }
        arguments.update(overrides)
        return arguments

    def _decision(self, action=ACTION_LAUNCH, **overrides):
        arguments = {
            "authorized": True, "reason": "ok", "detail": "",
            "project": "ai-dev", "ticket": "issue-55", "rail": RAIL, "role": "executor",
            "action": action, "iteration": self.iteration, "head": HEAD,
            # Every pre-existing case authorizes one agent with room to spare.
            "ceiling": 6,
        }
        arguments.update(overrides)
        return AuthorizationDecision(**arguments)

    def _starter(self, handle=None, record_calls=None, fail=None):
        """Stand in for claude_worker.start_worker: attach exactly as it would."""
        worker = handle if handle is not None else FakeHandle()

        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            if record_calls is not None:
                record_calls.append({"session_id": reserved.session_id, "state": reserved.state})
            if fail is not None:
                raise fail
            bound = attach_process(
                store, reserved.session_id, pid=worker.pid, pid_domain="test-host",
                started_at=worker.started_at, bound_at="2026-08-26T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            return worker, bound

        return start, worker

    def _sender(self, results=None, fail=None, record_calls=None):
        sent = record_calls if record_calls is not None else []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            sent.append({"mode": request.mode, "session_id": request.session_id, "prompt": prompt})
            if fail is not None:
                raise fail
            payload = {"type": "result", "session_id": request.session_id,
                       "mode": request.mode, "subtype": "success", "is_error": False}
            if results:
                payload.update(results)
            return payload

        return send, sent

    def _launch(self, **overrides):
        start, worker = overrides.pop("starter", (None, None))
        if start is None:
            start, worker = self._starter()
        send, sent = overrides.pop("sender", (None, None))
        if send is None:
            send, sent = self._sender()
        arguments = {
            "store": self.store, "registry": self.registry, "reference": self.reference,
            "request_kwargs": self._request_kwargs(), "prompt": "do the work",
            "package_root": self.repo_root, "now": lambda: self.clock,
            "new_session_id": lambda: SESSION, "start": start, "send": send,
        }
        arguments.update(overrides)
        outcome = launch_session(self._decision(), self.assignment, **arguments)
        return outcome, worker, sent


class LaunchOrderingTests(LifecycleTestBase):
    def test_the_launch_request_is_built_while_the_binding_is_still_reserved(self) -> None:
        seen = []
        start, _ = self._starter(record_calls=seen)
        outcome, _worker, sent = self._launch(starter=(start, None))

        # start_worker saw a reserved record, which is only possible if the request
        # was constructed before attachment.
        self.assertEqual(seen, [{"session_id": SESSION, "state": BINDING_STATE_RESERVED}])
        self.assertEqual(outcome.request.mode, "launch")
        self.assertEqual(outcome.request.session_id, SESSION)
        self.assertEqual(outcome.binding.state, BINDING_STATE_BOUND)
        self.assertEqual(sent, [{"mode": "launch", "session_id": SESSION, "prompt": "do the work"}])

    def test_the_run_2_binding_not_reserved_failure_is_now_structurally_impossible(self) -> None:
        # Building the request from the post-attachment record is what failed
        # before; the coordinator never has the chance to do it that way.
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            claude_worker.build_launch_request(outcome.binding, **self._request_kwargs())
        self.assertEqual(caught.exception.reason, "binding-not-reserved")

    def test_the_full_order_is_reserve_build_start_attach_send(self) -> None:
        order = []
        worker = FakeHandle()

        def start(store, reserved, *, expected_iteration, package_root, now, **kwargs):
            order.append("start:{0}".format(reserved.state))
            bound = attach_process(
                store, reserved.session_id, pid=worker.pid, pid_domain="test-host",
                started_at=worker.started_at, bound_at="2026-08-26T12:00:03Z",
                expected_iteration=expected_iteration,
            )
            order.append("attach:{0}".format(bound.state))
            return worker, bound

        def send(handle, request, *, prompt, markers=(), timeout=None):
            order.append("send:{0}".format(request.mode))
            return {"session_id": request.session_id, "subtype": "success", "is_error": False}

        self._launch(starter=(start, worker), sender=(send, []))
        self.assertEqual(order, ["start:reserved", "attach:bound", "send:launch"])

    def test_the_session_is_registered_with_the_observed_process_identity(self) -> None:
        outcome, worker, _sent = self._launch()
        owned = self.registry.get(SESSION)
        self.assertIsNotNone(owned)
        self.assertEqual(owned.pid, worker.pid)
        self.assertEqual(owned.pgid, worker.pgid)
        self.assertEqual(owned.started_at, worker.started_at)
        self.assertEqual(owned.iteration, self.iteration)
        self.assertEqual(owned.role, "executor")
        self.assertEqual(owned.mismatches(outcome.binding), ())


class LaunchAuthorizationTests(LifecycleTestBase):
    def test_an_unauthorized_or_refusing_decision_launches_nothing(self) -> None:
        for decision in (
            self._decision(authorized=False, action=None, reason="rail-not-dispatched"),
            None,
        ):
            with self.subTest(decision=decision):
                start, _ = self._starter()
                with self.assertRaises(LifecycleError) as caught:
                    launch_session(
                        decision, self.assignment, store=self.store, registry=self.registry,
                        reference=self.reference, request_kwargs=self._request_kwargs(),
                        prompt="x", package_root=self.repo_root, start=start,
                        send=self._sender()[0], new_session_id=lambda: SESSION,
                        now=lambda: self.clock,
                    )
                self.assertEqual(caught.exception.reason, session_lifecycle.REASON_NOT_AUTHORIZED)
                self.assertEqual(self.store.record_files(), [])

    def test_a_continue_decision_cannot_launch(self) -> None:
        with self.assertRaises(LifecycleError) as caught:
            launch_session(
                self._decision(action=ACTION_CONTINUE), self.assignment,
                store=self.store, registry=self.registry, reference=self.reference,
                request_kwargs=self._request_kwargs(), prompt="x",
                package_root=self.repo_root, start=self._starter()[0],
                send=self._sender()[0], new_session_id=lambda: SESSION, now=lambda: self.clock,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_WRONG_ACTION)
        self.assertEqual(self.store.record_files(), [])

    def test_a_decision_for_another_scope_or_iteration_launches_nothing(self) -> None:
        for overrides, reason in (
            ({"ticket": "issue-56"}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"rail": OTHER_RAIL}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"role": "reviewer"}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"head": "d" * 40}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"iteration": RailIteration(rail=RAIL, blob=OTHER_BLOB)},
             session_lifecycle.REASON_ITERATION_DRIFT),
        ):
            with self.subTest(**overrides):
                with self.assertRaises(LifecycleError) as caught:
                    launch_session(
                        self._decision(**overrides), self.assignment, store=self.store,
                        registry=self.registry, reference=self.reference,
                        request_kwargs=self._request_kwargs(), prompt="x",
                        package_root=self.repo_root, start=self._starter()[0],
                        send=self._sender()[0], new_session_id=lambda: SESSION,
                        now=lambda: self.clock,
                    )
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(self.store.record_files(), [])


class LaunchFailureTests(LifecycleTestBase):
    def test_request_construction_failure_leaves_a_reservation_and_no_process(self) -> None:
        started = []
        start, _ = self._starter(record_calls=started)
        with self.assertRaises(ClaudeRuntimeError):
            self._launch(
                starter=(start, None),
                request_kwargs=self._request_kwargs(prompt_file=self.workspace / "inside.md"),
            )
        self.assertEqual(started, [])
        record = self.store.read(SESSION)
        self.assertEqual(record.state, BINDING_STATE_RESERVED)
        self.assertFalse(record.has_process_identity)
        self.assertIsNone(self.registry.get(SESSION))

    def test_spawn_or_readiness_failure_leaves_the_reservation_truthful(self) -> None:
        boom = claude_worker.ClaudeWorkerError("readiness-failed", "no readiness line")
        start, _ = self._starter(fail=boom)
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._launch(starter=(start, None))
        record = self.store.read(SESSION)
        self.assertEqual(record.state, BINDING_STATE_RESERVED)
        self.assertFalse(record.has_process_identity)
        self.assertIsNone(self.registry.get(SESSION))

    def test_provider_failure_stops_the_owned_process_and_keeps_the_truth(self) -> None:
        stopped = []
        send, _ = self._sender(fail=RuntimeError("provider refused"))
        with self.assertRaises(LifecycleError) as caught:
            self._launch(
                sender=(send, []),
                stop=lambda handle: stopped.append(handle.pgid) or {"process_group_gone": True},
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_LAUNCH_FAILED)
        self.assertEqual(stopped, [4242])
        # The process really did start and bind. Saying otherwise would be a lie.
        record = self.store.read(SESSION)
        self.assertEqual(record.state, BINDING_STATE_BOUND)
        self.assertEqual(record.pid, 4242)
        self.assertIsNone(self.registry.get(SESSION))

    def test_no_failure_path_reuses_the_session_id(self) -> None:
        send, _ = self._sender(fail=RuntimeError("provider refused"))
        with self.assertRaises(LifecycleError):
            self._launch(sender=(send, []), stop=lambda handle: {"process_group_gone": True})
        with self.assertRaises(SessionBindingError) as caught:
            self._launch()
        self.assertEqual(caught.exception.reason, "duplicate-session-id")


class ContinueTests(LifecycleTestBase):
    def _bound(self):
        outcome, worker, _sent = self._launch()
        return outcome, worker

    def test_continuation_sends_an_exact_resume_through_the_owned_worker(self) -> None:
        outcome, worker = self._bound()
        send, sent = self._sender()
        result = continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on", send=send,
            alive=lambda pgid: True,
        )
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(sent, [{"mode": "resume", "session_id": SESSION, "prompt": "carry on"}])

    def test_a_second_invocation_while_one_is_in_flight_is_refused(self) -> None:
        self._bound()
        seen = {}

        def reentrant(handle, request, *, prompt, markers=(), timeout=None):
            with self.assertRaises(LifecycleError) as caught:
                continue_session(
                    self._decision(action=ACTION_CONTINUE), self.assignment,
                    store=self.store, registry=self.registry, session_id=SESSION,
                    request_kwargs=self._request_kwargs(), prompt="second",
                    send=lambda *a, **k: None, alive=lambda pgid: True,
                )
            seen["reason"] = caught.exception.reason
            return {"session_id": request.session_id, "subtype": "success"}

        continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="first", send=reentrant,
            alive=lambda pgid: True,
        )
        self.assertEqual(seen["reason"], session_lifecycle.REASON_INVOCATION_IN_FLIGHT)

    def test_the_in_flight_marker_clears_even_when_the_invocation_fails(self) -> None:
        self._bound()
        send, _ = self._sender(fail=RuntimeError("provider error"))
        with self.assertRaises(RuntimeError):
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x", send=send,
                alive=lambda pgid: True,
            )
        self.assertEqual(self.registry.in_flight(), ())
        good, sent = self._sender()
        continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="again", send=good,
            alive=lambda pgid: True,
        )
        self.assertEqual(len(sent), 1)

    def test_continuation_requires_a_bound_binding(self) -> None:
        start, _ = self._starter()
        launch_session(
            self._decision(), self.assignment, store=self.store, registry=self.registry,
            reference=self.reference, request_kwargs=self._request_kwargs(), prompt="x",
            package_root=self.repo_root, now=lambda: self.clock,
            new_session_id=lambda: SESSION, start=start, send=self._sender()[0],
        )
        unbind_session(self.store, SESSION)
        with self.assertRaises(LifecycleError) as caught:
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x",
                send=self._sender()[0], alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)

    def test_continuation_refuses_without_an_exact_owned_handle(self) -> None:
        self._bound()
        self.registry.remove(SESSION)
        with self.assertRaises(LifecycleError) as caught:
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x",
                send=self._sender()[0], alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_HANDLE_MISSING)

    def test_continuation_refuses_a_stale_pid_that_now_belongs_to_someone_else(self) -> None:
        # A recycled pid is why identity is compared field by field rather than
        # by asking whether *a* process with that number is alive.
        outcome, worker = self._bound()
        owned = self.registry.get(SESSION)
        self.registry.add(session_lifecycle.OwnedSession(
            **dict(owned.__dict__, started_at="2026-08-26T13:59:59Z")
        ))
        with self.assertRaises(LifecycleError) as caught:
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x",
                send=self._sender()[0], alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_HANDLE_MISMATCH)
        self.assertIn("startedAt", caught.exception.detail)

    def test_continuation_refuses_iteration_drift(self) -> None:
        self._bound()
        drifted = session_lifecycle.Assignment(
            **dict(self.assignment.__dict__, iteration=RailIteration(rail=RAIL, blob=OTHER_BLOB))
        )
        with self.assertRaises(LifecycleError) as caught:
            continue_session(
                self._decision(action=ACTION_CONTINUE, iteration=drifted.iteration), drifted,
                store=self.store, registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x",
                send=self._sender()[0], alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_ITERATION_DRIFT)


class ObservationTests(LifecycleTestBase):
    def _facts(self, **overrides):
        arguments = {"identifier": RAIL, "status": "running", "rail_blob": BLOB,
                     "pending_human_decision": None}
        arguments.update(overrides)
        return RailFacts(**arguments)

    def test_running_requires_a_running_rail_and_a_live_owned_process(self) -> None:
        outcome, _worker, _sent = self._launch()
        projection = observe_session(
            self._facts(), outcome.binding, self.registry,
            now="2026-08-26T12:05:03Z", alive=lambda pgid: True,
        )
        self.assertEqual(projection.state, STATE_RUNNING)
        self.assertEqual(projection.session_id, SESSION)
        self.assertEqual(projection.elapsed_seconds, 301)

    def test_waiting_requires_a_blocked_rail_and_a_durable_decision(self) -> None:
        outcome, _worker, _sent = self._launch()
        projection = observe_session(
            self._facts(status="blocked", pending_human_decision="pick a runtime boundary"),
            outcome.binding, self.registry, now=self.clock, alive=lambda pgid: True,
        )
        self.assertEqual(projection.state, STATE_WAITING)
        self.assertIn("runtime boundary", projection.detail)

    def test_a_blocked_rail_without_a_decision_is_not_waiting(self) -> None:
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(LifecycleError) as caught:
            observe_session(
                self._facts(status="blocked"), outcome.binding, self.registry,
                now=self.clock, alive=lambda pgid: True,
            )
        self.assertEqual(
            caught.exception.reason, session_lifecycle.REASON_BLOCKED_WITHOUT_DECISION
        )

    def test_disconnected_when_the_owned_handle_is_gone(self) -> None:
        outcome, _worker, _sent = self._launch()
        self.registry.remove(SESSION)
        projection = observe_session(
            self._facts(), outcome.binding, self.registry,
            now=self.clock, alive=lambda pgid: True,
        )
        self.assertEqual(projection.state, STATE_DISCONNECTED)
        self.assertEqual(projection.reason, session_lifecycle.REASON_DISCONNECTED_NO_HANDLE)

    def test_disconnected_when_the_process_group_is_gone(self) -> None:
        outcome, _worker, _sent = self._launch()
        projection = observe_session(
            self._facts(), outcome.binding, self.registry,
            now=self.clock, alive=lambda pgid: False,
        )
        self.assertEqual(projection.state, STATE_DISCONNECTED)
        self.assertEqual(projection.reason, session_lifecycle.REASON_DISCONNECTED_NOT_LIVE)

    def test_disconnected_when_the_owned_identity_no_longer_matches(self) -> None:
        outcome, _worker, _sent = self._launch()
        owned = self.registry.get(SESSION)
        self.registry.add(OwnedSession(**dict(owned.__dict__, pid=99999)))
        projection = observe_session(
            self._facts(), outcome.binding, self.registry,
            now=self.clock, alive=lambda pgid: True,
        )
        self.assertEqual(projection.state, STATE_DISCONNECTED)
        self.assertEqual(projection.reason, session_lifecycle.REASON_DISCONNECTED_MISMATCH)

    def test_incomplete_observation_projects_nothing(self) -> None:
        outcome, _worker, _sent = self._launch()
        for rail, record in ((None, outcome.binding), (self._facts(), None), (None, None)):
            with self.subTest(rail=rail is not None, record=record is not None):
                with self.assertRaises(LifecycleError) as caught:
                    observe_session(rail, record, self.registry, now=self.clock)
                self.assertEqual(
                    caught.exception.reason, session_lifecycle.REASON_OBSERVATION_INCOMPLETE
                )

    def test_a_terminal_binding_projects_nothing(self) -> None:
        outcome, _worker, _sent = self._launch()
        unbind_session(self.store, SESSION)
        with self.assertRaises(LifecycleError) as caught:
            observe_session(
                self._facts(), self.store.read(SESSION), self.registry, now=self.clock
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)

    def test_rail_and_binding_iteration_drift_projects_nothing(self) -> None:
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(LifecycleError) as caught:
            observe_session(
                self._facts(rail_blob=OTHER_BLOB), outcome.binding, self.registry,
                now=self.clock, alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_ITERATION_DRIFT)

    def test_a_live_session_on_a_rail_that_is_not_running_fails_closed(self) -> None:
        outcome, _worker, _sent = self._launch()
        for status in ("ready", "completed"):
            with self.subTest(status=status):
                with self.assertRaises(LifecycleError) as caught:
                    observe_session(
                        self._facts(status=status), outcome.binding, self.registry,
                        now=self.clock, alive=lambda pgid: True,
                    )
                self.assertEqual(
                    caught.exception.reason, session_lifecycle.REASON_RAIL_NOT_RUNNING
                )

    def test_a_binding_for_another_rail_projects_nothing(self) -> None:
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(LifecycleError) as caught:
            observe_session(
                self._facts(identifier=OTHER_RAIL), outcome.binding, self.registry,
                now=self.clock, alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SCOPE_MISMATCH)


class SingleLivenessSnapshotTests(LifecycleTestBase):
    """One observation per process group, for the life of one caller's read.

    The primitive on its own. What consumes it, and why one read needs it, is
    `queue_source`'s business; this only proves the snapshot is a snapshot.
    """

    class Counting:
        def __init__(self, answers):
            self.answers = list(answers)
            self.calls = []

        def __call__(self, pgid):
            self.calls.append(pgid)
            return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]

    def test_one_process_group_is_observed_once_however_often_it_is_asked(self) -> None:
        underlying = self.Counting([True, False, False])
        observe = single_liveness_snapshot(underlying)

        readings = [observe(4242) for _ in range(3)]

        self.assertEqual(readings, [True, True, True])
        self.assertEqual(underlying.calls, [4242])

    def test_a_false_reading_is_held_just_as_firmly_as_a_true_one(self) -> None:
        underlying = self.Counting([False, True, True])
        observe = single_liveness_snapshot(underlying)

        readings = [observe(4242) for _ in range(3)]

        self.assertEqual(readings, [False, False, False])
        self.assertEqual(underlying.calls, [4242])

    def test_distinct_process_groups_each_get_their_own_observation(self) -> None:
        underlying = self.Counting([True, False])
        observe = single_liveness_snapshot(underlying)

        first, second = observe(4242), observe(4343)

        self.assertEqual((first, second), (True, False))
        self.assertEqual(underlying.calls, [4242, 4343])
        # And each is then held.
        self.assertEqual((observe(4242), observe(4343)), (True, False))
        self.assertEqual(underlying.calls, [4242, 4343])

    def test_a_fresh_snapshot_re_observes_rather_than_reusing_the_last_one(self) -> None:
        """This is what keeps it a snapshot rather than a durable cache."""
        underlying = self.Counting([True, False])

        first = single_liveness_snapshot(underlying)(4242)
        second = single_liveness_snapshot(underlying)(4242)

        self.assertEqual((first, second), (True, False))
        self.assertEqual(underlying.calls, [4242, 4242])

    def test_it_defaults_to_the_accepted_prober_rather_than_a_rule_of_its_own(self) -> None:
        with patch.object(session_lifecycle, "process_group_alive") as probe:
            probe.return_value = True
            observe = single_liveness_snapshot()

            self.assertTrue(observe(4242))
            self.assertTrue(observe(4242))

        probe.assert_called_once_with(4242)

    def test_require_owned_reads_the_snapshot_and_probes_nothing_further(self) -> None:
        """The consumer seam: proving ownership twice observes liveness once."""
        outcome, _worker, _sent = self._launch()
        underlying = self.Counting([True, False])
        observe = single_liveness_snapshot(underlying)

        require_owned(self.registry, outcome.binding, alive=observe)
        require_owned(self.registry, outcome.binding, alive=observe)

        self.assertEqual(len(underlying.calls), 1)


class ElapsedTimeTests(LifecycleTestBase):
    def test_elapsed_is_derived_from_the_injected_clock(self) -> None:
        self.assertEqual(
            elapsed_seconds("2026-08-26T12:00:00Z", "2026-08-26T12:00:42Z"), 42
        )
        self.assertEqual(
            elapsed_seconds("2026-08-26T12:00:00Z", "2026-08-27T12:00:00Z"), 86400
        )

    def test_backwards_clock_movement_clamps_to_zero(self) -> None:
        self.assertEqual(
            elapsed_seconds("2026-08-26T12:00:30Z", "2026-08-26T12:00:00Z"), 0
        )

    def test_a_malformed_timestamp_is_refused(self) -> None:
        for started, now in (("2026-08-26 12:00:00", "2026-08-26T12:00:00Z"),
                             ("2026-08-26T12:00:00Z", "yesterday"),
                             (None, "2026-08-26T12:00:00Z")):
            with self.subTest(started=started, now=now):
                with self.assertRaises(LifecycleError) as caught:
                    elapsed_seconds(started, now)
                self.assertEqual(
                    caught.exception.reason, session_lifecycle.REASON_INVALID_TIMESTAMP
                )

    def test_elapsed_never_influences_the_projected_state(self) -> None:
        outcome, _worker, _sent = self._launch()
        facts = RailFacts(identifier=RAIL, status="running", rail_blob=BLOB)
        fresh = observe_session(
            facts, outcome.binding, self.registry, now="2026-08-26T12:00:03Z",
            alive=lambda pgid: True,
        )
        ancient = observe_session(
            facts, outcome.binding, self.registry, now="2027-08-26T12:00:03Z",
            alive=lambda pgid: True,
        )
        self.assertEqual(fresh.state, ancient.state)
        self.assertEqual(fresh.state, STATE_RUNNING)
        self.assertGreater(ancient.elapsed_seconds, fresh.elapsed_seconds)


class StopTests(LifecycleTestBase):
    def test_stop_proves_the_group_is_gone_before_terminalizing(self) -> None:
        outcome, worker, _sent = self._launch()
        order = []
        gone = {"value": False}

        def stopper(handle):
            order.append("shutdown")
            gone["value"] = True
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}

        original = session_lifecycle.unbind_session

        def watched(store, session_id):
            order.append("unbind")
            return original(store, session_id)

        with patch.object(session_lifecycle, "unbind_session", watched):
            result = stop_session(
                self.store, self.registry, outcome.binding, stop=stopper,
                alive=lambda pgid: not gone["value"],
            )
        self.assertEqual(order, ["shutdown", "unbind"])
        self.assertEqual(result.binding.state, BINDING_STATE_UNBOUND)
        self.assertTrue(result.process_group_gone)
        self.assertEqual(result.pid, worker.pid)
        self.assertIsNone(self.registry.get(SESSION))

    def test_an_unproven_shutdown_leaves_the_binding_nonterminal(self) -> None:
        outcome, _worker, _sent = self._launch()
        for report in ({"graceful": False, "process_group_gone": False}, {}, None):
            with self.subTest(report=report):
                with self.assertRaises(LifecycleError) as caught:
                    stop_session(
                        self.store, self.registry, outcome.binding,
                        stop=lambda handle: report, alive=lambda pgid: True,
                    )
                self.assertEqual(
                    caught.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE
                )
                self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_a_group_still_alive_after_a_clean_looking_report_is_refused(self) -> None:
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(LifecycleError) as caught:
            stop_session(
                self.store, self.registry, outcome.binding,
                stop=lambda handle: {"process_group_gone": True},
                alive=lambda pgid: True,
            )
        self.assertEqual(
            caught.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE
        )
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_stop_refuses_without_an_exact_owned_handle(self) -> None:
        outcome, _worker, _sent = self._launch()
        self.registry.remove(SESSION)
        stopped = []
        with self.assertRaises(LifecycleError) as caught:
            stop_session(
                self.store, self.registry, outcome.binding,
                stop=lambda handle: stopped.append(1), alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_HANDLE_MISSING)
        self.assertEqual(stopped, [])
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_stop_refuses_a_handle_whose_group_is_already_gone(self) -> None:
        # Deliberate: if the group is already gone the handle proves nothing, so the
        # binding stays nonterminal and the session becomes a human decision rather
        # than being quietly terminalized on an unprovable assumption.
        outcome, _worker, _sent = self._launch()
        stopped = []
        with self.assertRaises(LifecycleError) as caught:
            stop_session(
                self.store, self.registry, outcome.binding,
                stop=lambda handle: stopped.append(1), alive=lambda pgid: False,
            )
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_HANDLE_MISMATCH)
        self.assertEqual(stopped, [])
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_stopping_a_terminal_binding_is_refused(self) -> None:
        outcome, _worker, _sent = self._launch()
        unbind_session(self.store, SESSION)
        with self.assertRaises(LifecycleError) as caught:
            stop_session(self.store, self.registry, self.store.read(SESSION),
                         stop=lambda handle: {"process_group_gone": True})
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)


class RecoveryTests(LifecycleTestBase):
    def test_a_restarted_controller_projects_disconnected_and_refuses_everything(self) -> None:
        outcome, _worker, _sent = self._launch()
        restarted = SessionRegistry()  # a fresh controller owns nothing

        report = recover_session(outcome.binding, restarted, now="2026-08-26T12:10:03Z")
        self.assertEqual(report.state, STATE_DISCONNECTED)
        self.assertEqual(report.reason, session_lifecycle.REASON_DISCONNECTED_NO_HANDLE)
        self.assertEqual(report.session_id, SESSION)
        self.assertEqual(report.elapsed_seconds, 601)
        self.assertIn("human decides", report.human_decision)

        with self.assertRaises(LifecycleError) as continued:
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=restarted, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="x",
                send=self._sender()[0], alive=lambda pgid: True,
            )
        self.assertEqual(continued.exception.reason, session_lifecycle.REASON_HANDLE_MISSING)

        with self.assertRaises(LifecycleError) as stopped:
            stop_session(self.store, restarted, outcome.binding,
                         stop=lambda handle: {"process_group_gone": True})
        self.assertEqual(stopped.exception.reason, session_lifecycle.REASON_HANDLE_MISSING)
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_recovery_performs_no_process_or_provider_action(self) -> None:
        outcome, _worker, _sent = self._launch()
        restarted = SessionRegistry()
        with patch.object(subprocess, "Popen") as popen, \
                patch.object(subprocess, "run") as run, \
                patch.object(claude_worker, "process_group_alive") as probe, \
                patch.object(claude_worker, "shutdown_worker") as shutdown, \
                patch("os.kill") as kill, patch("os.killpg") as killpg:
            recover_session(outcome.binding, restarted, now=self.clock)
        popen.assert_not_called()
        run.assert_not_called()
        probe.assert_not_called()
        shutdown.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_recovery_never_unbinds_rebinds_or_clears_identity(self) -> None:
        outcome, _worker, _sent = self._launch()
        before = self.store.read(SESSION)
        recover_session(outcome.binding, SessionRegistry(), now=self.clock)
        after = self.store.read(SESSION)
        self.assertEqual(after, before)
        self.assertEqual(after.state, BINDING_STATE_BOUND)
        self.assertEqual(after.pid, before.pid)
        self.assertEqual(len(self.store.record_files()), 1)

    def test_recovery_refuses_a_session_that_is_not_disconnected(self) -> None:
        outcome, _worker, _sent = self._launch()
        with self.assertRaises(LifecycleError) as caught:
            recover_session(outcome.binding, self.registry, now=self.clock,
                            alive=lambda pgid: True)
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_HANDLE_MISMATCH)

    def test_recovery_refuses_a_terminal_binding(self) -> None:
        outcome, _worker, _sent = self._launch()
        unbind_session(self.store, SESSION)
        with self.assertRaises(LifecycleError) as caught:
            recover_session(self.store.read(SESSION), SessionRegistry(), now=self.clock)
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)


class ContextLifecycleTests(LifecycleTestBase):
    """Compaction observation through the real launch/continue composition."""

    BOUNDARY = "83e1ea6e-7979-4dca-9cd8-85d26656f905"
    OTHER_BOUNDARY = "0f0f0f0f-1111-4222-8333-444444444444"

    def _observed(self, uuid=None, session_id=SESSION):
        return {
            "event": EVENT_COMPACTION_OBSERVED,
            "session_id": session_id,
            "uuid": uuid or self.BOUNDARY,
        }

    def _boundaries(self, count):
        return [
            self._observed(uuid="{0:08d}-0000-4000-8000-000000000000".format(index))
            for index in range(count)
        ]

    def _event_sender(self, turns):
        """A sender whose successive invocations report successive event lists."""
        remaining = list(turns)
        sent = []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            sent.append(request.mode)
            events = remaining.pop(0) if remaining else []
            return {
                "type": "result", "session_id": request.session_id, "mode": request.mode,
                "subtype": "success", "is_error": False, "events": events,
            }

        return send, sent

    def _continue(self, send):
        return continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on", send=send,
            alive=lambda pgid: True,
        )

    def _reading(self, session_id=SESSION):
        return self.registry.context(session_id).reading()

    # -- observation from the session's own start ------------------------------

    def test_a_launched_session_is_observed_from_its_own_start(self) -> None:
        self._launch()
        reading = self._reading()
        self.assertEqual(reading.health, OBSERVATION_HEALTHY)
        self.assertEqual(reading.count, 0)
        self.assertIs(reading.rotation_marked, False)

    def test_a_session_this_controller_did_not_start_reports_history_unavailable(self) -> None:
        # Taking ownership is not the same as having watched a session from its
        # beginning, and exact resume never replays what was missed.
        outcome, _worker, _sent = self._launch()
        self.registry.remove(SESSION)
        self.registry.add(outcome.owned)
        reading = self._reading()
        self.assertEqual(reading.health, OBSERVATION_UNAVAILABLE)
        self.assertIsNone(reading.count)
        self.assertNotEqual(reading.count, 0)

    # -- counting --------------------------------------------------------------

    def test_a_valid_boundary_increments_manager_state_exactly_once(self) -> None:
        send, _sent = self._event_sender([[self._observed()]])
        self._launch(sender=(send, []))
        self.assertEqual(self._reading().count, 1)

    def test_the_same_boundary_observed_twice_does_not_count_twice(self) -> None:
        send, _sent = self._event_sender([[self._observed(), self._observed()]])
        self._launch(sender=(send, []))
        self.assertEqual(self._reading().count, 1)

    def test_a_boundary_replayed_on_a_later_turn_does_not_count_twice(self) -> None:
        launch_send, _ = self._event_sender([[self._observed()]])
        self._launch(sender=(launch_send, []))
        resume_send, _ = self._event_sender([[self._observed()]])
        self._continue(resume_send)
        self.assertEqual(self._reading().count, 1)

    def test_exact_resume_of_a_monitored_session_preserves_a_truthful_count(self) -> None:
        launch_send, _ = self._event_sender([[self._observed()]])
        self._launch(sender=(launch_send, []))
        resume_send, sent = self._event_sender([[self._observed(uuid=self.OTHER_BOUNDARY)]])
        self._continue(resume_send)
        self.assertEqual(sent, ["resume"])
        reading = self._reading()
        self.assertEqual(reading.count, 2)
        self.assertEqual(reading.health, OBSERVATION_HEALTHY)

    def test_an_event_naming_another_session_contaminates_nothing(self) -> None:
        send, _ = self._event_sender([[self._observed(session_id=OTHER_SESSION)]])
        self._launch(sender=(send, []))
        reading = self._reading()
        self.assertEqual(reading.observed, 0)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)
        self.assertIsNone(reading.count)

    def test_a_turn_reporting_no_events_is_not_a_session_with_no_history(self) -> None:
        # The accepted senders report no `events` key at all; that is one quiet turn,
        # not a claim about the session, and the health says what the count is worth.
        self._launch()
        self._continue(self._sender()[0])
        self.assertEqual(self._reading().health, OBSERVATION_HEALTHY)

    # -- threshold -------------------------------------------------------------

    def test_five_observed_compactions_do_not_mark_and_six_do(self) -> None:
        send, _ = self._event_sender([self._boundaries(5)])
        self._launch(sender=(send, []))
        self.assertIs(self._reading().rotation_marked, False)
        self.assertEqual(self.registry.rotation_marked_session_ids(), ())

        self._continue(self._event_sender([self._boundaries(6)])[0])
        self.assertIs(self._reading().rotation_marked, True)
        self.assertEqual(self.registry.rotation_marked_session_ids(), (SESSION,))

    def test_the_threshold_is_configurable_on_the_registry(self) -> None:
        self.registry = SessionRegistry(rotation_threshold=3)
        send, _ = self._event_sender([self._boundaries(3)])
        self._launch(sender=(send, []))
        self.assertEqual(self._reading().threshold, 3)
        self.assertIs(self._reading().rotation_marked, True)

    def test_reaching_the_threshold_neither_stops_nor_replaces_the_worker(self) -> None:
        stopped = []
        send, _ = self._event_sender([self._boundaries(6)])
        outcome, worker, _sent = self._launch(
            sender=(send, []), stop=lambda handle, **kwargs: stopped.append(handle)
        )
        self.assertIs(self._reading().rotation_marked, True)
        # The mark is the whole action. The process, the handle, the registry entry
        # and the binding are all exactly what they were.
        self.assertEqual(stopped, [])
        self.assertIs(self.registry.get(SESSION).handle, worker)
        self.assertEqual(self.registry.get(SESSION), outcome.owned)
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_a_marked_session_still_continues_normally(self) -> None:
        send, _ = self._event_sender([self._boundaries(6)])
        self._launch(sender=(send, []))
        resume_send, sent = self._event_sender([[]])
        self._continue(resume_send)
        self.assertEqual(sent, ["resume"])
        self.assertIs(self._reading().rotation_marked, True)

    # -- role policy -----------------------------------------------------------

    def _launch_as(self, role):
        """One launch under a role other than executor. Nothing is made persistent."""
        assignment = session_lifecycle.Assignment(
            **dict(self.assignment.__dict__, role=role)
        )
        start, _worker = self._starter()
        return launch_session(
            self._decision(role=role), assignment, store=self.store,
            registry=self.registry, reference=self.reference,
            request_kwargs=self._request_kwargs(), prompt="x",
            package_root=self.repo_root, now=lambda: self.clock,
            new_session_id=lambda: SESSION, start=start, send=self._sender()[0],
        )

    def test_a_reviewer_keeps_the_fresh_session_policy(self) -> None:
        self._launch_as("reviewer")
        reading = self._reading()
        self.assertEqual(reading.role, "reviewer")
        self.assertEqual(reading.context_policy, CONTEXT_POLICY_FRESH)
        self.assertEqual(reading.count, 0)

    def test_an_orchestrator_keeps_the_fresh_session_policy(self) -> None:
        # D10 makes an orchestrator a fresh event-driven invocation, so its policy
        # is recorded as fresh. It is still observed rather than exempted, because
        # D10 also says the rotation invariant applies if one ever does reach the
        # threshold -- and a session that is not observed reports zero, which is the
        # one thing this checkpoint must never do.
        self._launch_as("orchestrator")
        reading = self._reading()
        self.assertEqual(reading.role, "orchestrator")
        self.assertEqual(reading.context_policy, CONTEXT_POLICY_FRESH)
        self.assertEqual(reading.count, 0)

    def test_nothing_here_makes_a_fresh_role_session_persistent(self) -> None:
        # The fresh policy is preserved by not resuming these sessions, which is
        # accepted behaviour this checkpoint leaves exactly as it found it.
        outcome = self._launch_as("reviewer")
        self.assertEqual(outcome.request.mode, "launch")
        self.assertEqual(self.registry.context(SESSION).context_policy, CONTEXT_POLICY_FRESH)

    def test_an_executor_carries_the_persistent_context_policy(self) -> None:
        self._launch()
        self.assertEqual(self._reading().context_policy, CONTEXT_POLICY_PERSISTENT)

    # -- bounded memory --------------------------------------------------------

    def test_stopping_a_session_drops_its_whole_observation_memory(self) -> None:
        send, _ = self._event_sender([[self._observed()]])
        outcome, worker, _sent = self._launch(sender=(send, []))
        self.assertEqual(self._reading().count, 1)
        stop_session(
            self.store, self.registry, outcome.binding,
            stop=lambda handle: {"graceful": True, "exit_code": 0, "process_group_gone": True},
            alive=self._dying(worker.pgid),
        )
        self.assertIsNone(self.registry.context(SESSION))
        self.assertEqual(self.registry.context_readings(), {})

    def _dying(self, pgid):
        seen = {"count": 0}

        def alive(observed_pgid):
            seen["count"] += 1
            return seen["count"] == 1

        return alive


class NoProviderContactTests(LifecycleTestBase):
    def test_the_module_never_imports_the_sdk_or_spawns_a_process(self) -> None:
        source = Path(session_lifecycle.__file__).read_text(encoding="utf-8")
        for forbidden in ("claude_agent_sdk", "subprocess", "Popen", "os.kill", "killpg"):
            self.assertNotIn(forbidden, source)

    def test_a_whole_launch_and_stop_touches_no_real_process(self) -> None:
        # Every process-owning collaborator is injected, so the real ones are never
        # reached. (subprocess itself is not patched here: proving workspace
        # identity legitimately shells out to git.)
        with patch.object(claude_worker, "spawn_worker") as spawn, \
                patch.object(claude_worker, "start_worker") as start, \
                patch.object(claude_worker, "run_request") as send, \
                patch.object(claude_worker, "shutdown_worker") as shutdown, \
                patch.object(claude_worker, "process_group_alive") as probe:
            outcome, _worker, _sent = self._launch()
            gone = {"value": False}

            def stopper(handle):
                gone["value"] = True
                return {"graceful": True, "exit_code": 0, "process_group_gone": True}

            stop_session(
                self.store, self.registry, outcome.binding,
                stop=stopper, alive=lambda pgid: not gone["value"],
            )
        for mock in (spawn, start, send, shutdown, probe):
            mock.assert_not_called()


class FailedInvocationObservationTests(LifecycleTestBase):
    """A failed continue keeps what it observed and stops claiming a complete history.

    The reviewer's exact failure: a compaction the product had already decoded was
    discarded because the invocation carrying it failed, while the surviving session
    went on reporting `healthy-complete-from-session-start`, `count 0`,
    `rotationMarked False`. Every case below is that shape.
    """

    BOUNDARY = "83e1ea6e-7979-4dca-9cd8-85d26656f905"

    def _observed(self, uuid=None, session_id=SESSION):
        return {
            "event": EVENT_COMPACTION_OBSERVED,
            "session_id": session_id,
            "uuid": uuid or self.BOUNDARY,
        }

    def _failing_sender(self, events=(), error=None):
        """A sender that reports events exactly as `run_request` does, then fails."""
        failure = error if error is not None else claude_worker.ClaudeWorkerError(
            claude_worker.REASON_WORKER_FATAL, "provider-error: the provider blew up",
            events,
        )

        def send(handle, request, *, prompt, markers=(), timeout=None):
            raise failure

        return send

    def _succeeding_sender(self, events=()):
        def send(handle, request, *, prompt, markers=(), timeout=None):
            return {
                "type": "result", "session_id": request.session_id, "mode": request.mode,
                "subtype": "success", "is_error": False, "events": list(events),
            }

        return send

    def _continue(self, send):
        return continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on", send=send,
            alive=lambda pgid: True,
        )

    def _reading(self):
        return self.registry.context(SESSION).reading()

    # -- 1. the positive control: nothing about the healthy path changed ---------

    def test_a_valid_event_on_a_successful_invocation_counts_and_stays_healthy(self) -> None:
        self._launch()
        self._continue(self._succeeding_sender([self._observed()]))
        reading = self._reading()
        self.assertEqual(reading.observed, 1)
        self.assertEqual(reading.count, 1)
        self.assertEqual(reading.health, OBSERVATION_HEALTHY)
        self.assertIs(reading.rotation_marked, False)

    # -- 2/3. the two failures the reviewer reproduced, and they behave alike ----

    def test_a_worker_fatal_after_an_event_keeps_the_event_and_degrades_the_claim(self) -> None:
        self._launch()
        with self.assertRaises(claude_worker.ClaudeWorkerError) as caught:
            self._continue(self._failing_sender([self._observed()]))
        # The failure is still a failure and still carries its own reason.
        self.assertEqual(caught.exception.reason, claude_worker.REASON_WORKER_FATAL)
        reading = self._reading()
        self.assertEqual(reading.observed, 1)          # kept, not discarded
        self.assertIsNone(reading.count)               # no longer a complete history
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)
        self.assertIsNot(reading.rotation_marked, False)

    def test_a_command_timeout_after_an_event_behaves_exactly_as_a_fatal_does(self) -> None:
        self._launch()
        timeout = claude_worker.ClaudeWorkerError(
            claude_worker.REASON_COMMAND_TIMEOUT,
            "the worker did not answer within its bound.",
            [self._observed()],
        )
        with self.assertRaises(claude_worker.ClaudeWorkerError) as caught:
            self._continue(self._failing_sender(error=timeout))
        self.assertEqual(caught.exception.reason, claude_worker.REASON_COMMAND_TIMEOUT)
        reading = self._reading()
        self.assertEqual(reading.observed, 1)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    def test_the_session_survives_the_failed_invocation_and_stays_continuable(self) -> None:
        # The reason this matters at all: the failure does not remove the session.
        self._launch()
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender([self._observed()]))
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertEqual(self.registry.in_flight(), ())
        self._continue(self._succeeding_sender())
        # And a later good turn does not heal what could not be watched.
        self.assertEqual(self._reading().health, OBSERVATION_UNHEALTHY)

    # -- 4. a duplicate cannot count twice, on the failing path either -----------

    def test_a_duplicate_event_on_a_failed_invocation_does_not_double_count(self) -> None:
        self._launch()
        self._continue(self._succeeding_sender([self._observed()]))
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender([self._observed(), self._observed()]))
        reading = self._reading()
        self.assertEqual(reading.observed, 1)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    # -- 5. association still guards the failing path ----------------------------

    def test_a_mismatched_session_event_on_a_failed_invocation_counts_nothing(self) -> None:
        self._launch()
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender([self._observed(session_id=OTHER_SESSION)]))
        reading = self._reading()
        self.assertEqual(reading.observed, 0)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)
        self.assertIsNone(reading.rotation_marked)

    def test_a_malformed_event_on_a_failed_invocation_counts_nothing(self) -> None:
        self._launch()
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender([{"event": "not-a-lifecycle-event"}, "junk"]))
        reading = self._reading()
        self.assertEqual(reading.observed, 0)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    def test_only_the_worker_channels_own_refusal_may_carry_events(self) -> None:
        # An arbitrary exception degrades the claim, but cannot introduce events.
        self._launch()
        rogue = RuntimeError("provider error")
        rogue.events = (self._observed(),)
        with self.assertRaises(RuntimeError):
            self._continue(self._failing_sender(error=rogue))
        reading = self._reading()
        self.assertEqual(reading.observed, 0)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)

    # -- 6. a preserved floor may still prove the threshold was reached ----------

    def test_a_preserved_floor_reaching_the_threshold_still_marks_while_partial(self) -> None:
        self._launch()
        boundaries = [
            self._observed(uuid="{0:08d}-0000-4000-8000-000000000000".format(index))
            for index in range(6)
        ]
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender(boundaries))
        reading = self._reading()
        self.assertEqual(reading.observed, 6)
        self.assertIs(reading.rotation_marked, True)   # the floor proves it
        self.assertIsNone(reading.count)               # the total is still unknown
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)
        self.assertEqual(self.registry.rotation_marked_session_ids(), (SESSION,))

    def test_five_preserved_boundaries_leave_the_threshold_undetermined(self) -> None:
        self._launch()
        boundaries = [
            self._observed(uuid="{0:08d}-0000-4000-8000-000000000000".format(index))
            for index in range(5)
        ]
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._continue(self._failing_sender(boundaries))
        reading = self._reading()
        self.assertEqual(reading.observed, 5)
        self.assertIsNone(reading.rotation_marked)     # undetermined, never a false mark
        self.assertEqual(self.registry.rotation_marked_session_ids(), ())

    # -- 7. launch failure is still different, and deliberately so ---------------

    def test_a_failed_launch_still_removes_the_session_and_leaves_no_claim(self) -> None:
        send = self._failing_sender([self._observed()])
        with self.assertRaises(LifecycleError) as caught:
            self._launch(sender=(send, []))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_LAUNCH_FAILED)
        self.assertIsNone(self.registry.get(SESSION))
        self.assertIsNone(self.registry.context(SESSION))
        self.assertEqual(self.registry.context_readings(), {})


class RotationHarness(LifecycleTestBase):
    """The fixtures a rotation boundary is projected from: rail, handoff, worktree, work.

    Moved verbatim out of the accepted rotation cases so the retirement cases are
    composed from exactly the fixtures readiness is, rather than from a second set
    that could drift from them. No accepted case is altered by the move.
    """

    HANDOFF = "ai-dev/issue-55/rails/{0}/handoff.md".format(RAIL)

    def setUp(self) -> None:
        super().setUp()
        # The caller's own fresh control-plane read, standing in for
        # `rail_handoff_publication`. `None` is the honest starting state: this rail
        # has no published handoff until an agent writes one.
        self.published = _PublishedHandoff()
        # Where the product repository stands. A commit name and nothing else: it is
        # only ever compared for equality, never ordered or dated.
        self.product_head = PRODUCT_HEAD
        # Evidence-producing work that moves no repository at all: a test run, a
        # read-only review, an orchestrator reading durable state. This is the class
        # a product-head comparison is blind to, and the reason checkpoint 61's
        # discriminator was constant end to end on a read-only rail.
        self.evidence = []

    def _envelope(self, payload):
        """One final assistant message carrying a delimited handoff, as a turn ends.

        Prose on both sides of the delimiters, because a real one has it: the
        controller publishes what lies between them and discards the rest without
        reading either.
        """
        return "Done. Handoff below.\n{0}\n{1}\n{2}\nStopping here.\n".format(
            session_lifecycle.HANDOFF_ENVELOPE_BEGIN,
            payload,
            session_lifecycle.HANDOFF_ENVELOPE_END,
        )

    def _publish(self, handoff_bytes):
        """The durable publishing act, standing in for `control_plane.publish`.

        Returns the identity of what it published, which is what makes a
        finalization creditable at all. The real act captures the product state at
        the same instant, which is proved against real repositories in
        `tests/test_control_plane.py`.
        """
        self.published.publish(handoff_bytes, self.product_head)
        return handoff_bytes

    def _finalizer(self, publish=None, bookkeeping=None):
        """The deterministic post-turn finalization the manager composes."""
        return session_lifecycle.terminal_finalizer(
            publish=publish if publish is not None else self._publish,
            bookkeeping=bookkeeping,
        )

    def _work(self, terminal=None, raw_terminal=_UNSET, mid_turn=None, fail=None,
              finalizer=_UNSET, commits_before=(), commits_after=(),
              evidence_after=(), is_error=False):
        """One authorized invocation of real work, bracketed exactly as production is.

        `terminal` is the handoff the agent's *final* assistant message carries --
        the last thing it produces before the turn ends. `mid_turn` is a publication
        the agent makes for itself partway through, which is the shape checkpoints
        59 to 61 could not tell apart from a publication at the end. Nothing here
        reaches around `continue_session`: the credit, if any, is established by the
        same call that performs the work, after that call's turn has ended.

        `commits_before`/`commits_after` are product commits landing inside the same
        turn; `evidence_after` is work that produces evidence and moves no
        repository at all.
        """
        def send(handle, request, *, prompt, markers=(), timeout=None):
            for head in commits_before:
                self.product_head = head
            if mid_turn is not None:
                self.published.publish(mid_turn, self.product_head)
            for head in commits_after:
                self.product_head = head
            self.evidence.extend(evidence_after)
            if fail is not None:
                raise fail
            if raw_terminal is not _UNSET:
                payload = raw_terminal
            else:
                payload = self._envelope(terminal) if terminal is not None else None
            return {"type": "result", "session_id": request.session_id,
                    "mode": request.mode,
                    "subtype": "error_during_execution" if is_error else "success",
                    "is_error": is_error,
                    "terminal_payload": payload}

        return continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on",
            send=send, alive=lambda pgid: True,
            finalize_handoff=(
                self._finalizer() if finalizer is _UNSET else finalizer
            ),
        )

    def _mark(self, count=6, session_id=SESSION):
        """Drive the accepted counter to a real threshold mark. No shortcut flag."""
        events = [
            {"event": EVENT_COMPACTION_OBSERVED, "session_id": session_id,
             "uuid": "{0:08d}-0000-4000-8000-000000000000".format(index)}
            for index in range(count)
        ]
        self.registry.observe_context_events(session_id, events)

    def _rail(self, **overrides):
        arguments = {"identifier": RAIL, "status": "running", "rail_blob": BLOB}
        arguments.update(overrides)
        return RailFacts(**arguments)

    def _handoff(self, **overrides):
        """A fresh control-plane read of the rail's handoff, as it actually stands."""
        arguments = {
            "rail": RAIL,
            "published": self.published.value is not None,
            "location": self.HANDOFF,
            "publication": self.published.value,
            "work_state": self.published.work_state,
        }
        arguments.update(overrides)
        return RotationHandoffFacts(**arguments)

    def _worktree(self, **overrides):
        arguments = {
            "worktree_id": self.worktree_id, "path": str(self.workspace),
            "clean": True, "active_operation": None, "head": self.product_head,
        }
        arguments.update(overrides)
        return WorktreeFacts(**arguments)

    def _evaluate(self, **overrides):
        arguments = {
            "rail": self._rail(), "record": self.store.read(SESSION),
            "registry": self.registry, "handoff": self._handoff(),
            "worktree": self._worktree(),
        }
        arguments.update(overrides)
        rail = arguments.pop("rail")
        record = arguments.pop("record")
        registry = arguments.pop("registry")
        return evaluate_rotation_readiness(rail, record, registry, **arguments)


class RotationBoundaryTests(RotationHarness):
    """marked -> safe boundary -> durable handoff -> rotation-ready, and nothing past it.

    Every case here projects. Nothing in this class terminates a context, stops a
    worker, launches or binds a replacement, or writes a durable record, and the
    last two cases prove that rather than assert it.
    """

    # -- the transition itself --------------------------------------------------

    def test_marked_at_a_safe_boundary_with_a_current_handoff_is_rotation_ready(self) -> None:
        self._launch()
        self._mark()
        # The agent publishes its handoff during an authorized invocation, which is
        # the only place a persistent agent can publish from.
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_ROTATION_HANDOFF_ESTABLISHED)
        self.assertTrue(readiness.ready)
        self.assertEqual((readiness.observed, readiness.threshold), (6, 6))

    def test_work_after_the_agents_own_publication_is_not_rotation_ready(self) -> None:
        # THE load-bearing case, in the shape the mechanism now refuses structurally.
        # The agent works, publishes its handoff for itself partway through the turn,
        # then goes on working -- committing, so the tree it leaves is clean and every
        # other condition is satisfied -- and its turn ends carrying no handoff. The
        # artifact on the rail is real, published and byte-identical to one that
        # would have been credited; it is simply not one this controller finalized
        # after the turn, so nothing proves it followed the agent's last act.
        self._launch()
        self._mark()
        self._work(
            commits_before=(PRODUCT_HEAD,),
            mid_turn=PUBLICATION,
            commits_after=(NEXT_PRODUCT_HEAD,),
        )
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertFalse(readiness.ready)
        self.assertIsNone(readiness.handoff)
        self.assertTrue(self._handoff().published)
        self.assertIn("never finalized", readiness.detail)

    def test_the_two_orderings_are_separated_by_who_published_and_when(self) -> None:
        # The control the mechanism is measured against, and the reason it is
        # structural rather than conventional. Same session, same mark, same single
        # invocation, same published bytes, same successful return, and -- this is
        # the point -- the same product commit in both arms, so the checkpoint-61
        # discriminator is identical across them and answers nothing.
        #
        # GOOD: the bytes arrive as the turn's terminal result and the controller
        # publishes them once the turn has ended.
        # BAD: the agent publishes exactly those bytes itself, mid-turn, and keeps
        # working.
        self._launch()
        self._mark()
        self._work(commits_before=(NEXT_PRODUCT_HEAD,), terminal=PUBLICATION)
        good = self._evaluate()

        boundary = self.registry.work_boundary(SESSION)
        good_publication = self.published.value
        good_work_state = self._handoff().work_state
        good_head = self._worktree().head

        self.tearDown()
        self.setUp()
        self._launch()
        self._mark()
        self._work(commits_before=(NEXT_PRODUCT_HEAD,), mid_turn=PUBLICATION)
        stale = self._evaluate()

        # Every fact checkpoint 61 had to work with is identical across the two.
        self.assertEqual(self.registry.work_boundary(SESSION), boundary)
        self.assertEqual(self.published.value, good_publication)
        self.assertEqual(self._handoff().work_state, good_work_state)
        self.assertEqual(self._worktree().head, good_head)
        self.assertEqual(self._handoff().work_state, self._worktree().head)
        # The one fact that differs is the one this checkpoint adds: whether this
        # controller performed the publication itself, after the turn ended.
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        self.assertEqual(good.state, ROTATION_READY)
        self.assertEqual(stale.state, ROTATION_NOT_READY)
        self.assertEqual(stale.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_several_units_of_work_then_a_publication_is_current(self) -> None:
        # Work accumulating over more than one invocation is the ordinary shape of a
        # persistent rail. Nothing about that makes the handoff stale: what matters
        # is only that the publication was written against the state the last of it
        # left behind.
        self._launch()
        self._mark()
        self._work(commits_before=("3" * 40,))
        self._work(commits_before=("4" * 40,))
        self._work(commits_before=(NEXT_PRODUCT_HEAD,), terminal=PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(self.registry.work_boundary(SESSION), 3)
        self.assertEqual(readiness.handoff.handoff_publication, PUBLICATION)

    def test_a_publication_recording_no_product_state_is_not_current(self) -> None:
        # Fails closed rather than assuming nothing moved: a handoff published
        # before this fact existed, or against a checkout too incoherent to identify,
        # records no state, and an unproven ordering is not a proven one.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate(handoff=self._handoff(work_state=None))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_a_workspace_read_that_cannot_name_its_head_is_not_current(self) -> None:
        # The same refusal from the other side. One half of a comparison is not a
        # comparison.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate(worktree=self._worktree(head=None))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_coordination_work_after_the_publication_leaves_it_current(self) -> None:
        # The supported executor path publishes and pushes first, *then* allocates a
        # receipt -- so a rule reading "nothing at all after publication" would break
        # the documented normal path. Allocating that receipt moves the coordination
        # repository, not the product one; it changes no outcome, evidence,
        # unresolved work or next action a replacement resumes, and it must not
        # invalidate a handoff that is otherwise current.
        self._launch()
        self._mark()
        self._work(commits_before=(NEXT_PRODUCT_HEAD,), terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        # Whatever the coordination repository does next, the product state the
        # handoff was written against is still where the workspace stands.
        self.assertEqual(self._handoff().work_state, self._worktree().head)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_a_publication_with_no_work_after_it_needs_no_republication(self) -> None:
        # Nothing happened, so nothing has to be said again: the standing
        # publication is still the current one, evaluated twice with no second
        # publication and no second invocation in between.
        self._launch()
        self._mark()
        self._work(commits_before=(NEXT_PRODUCT_HEAD,), terminal=PUBLICATION)
        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(first.state, ROTATION_READY)
        self.assertEqual(second.state, ROTATION_READY)
        self.assertEqual(self.published.value, PUBLICATION)

    def test_the_handoff_carries_the_exact_durable_identity_and_nothing_else(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        handoff = self._evaluate().handoff
        record = self.store.read(SESSION)
        self.assertEqual(handoff.to_dict(), {
            "project": "ai-dev", "ticket": "issue-55", "rail": RAIL, "iteration": BLOB,
            "role": "executor", "workspaceKey": record.workspace_key,
            "worktreeId": self.worktree_id, "workspacePath": str(self.workspace),
            "launchedAtHead": HEAD, "handoff": self.HANDOFF,
            "handoffPublication": PUBLICATION, "sessionId": SESSION,
        })
        # Every value is the binding's own, so a replacement resolves the same rail
        # and the same workspace a fresh agent would.
        self.assertEqual(
            (handoff.project, handoff.ticket, handoff.rail, handoff.iteration_blob),
            (record.project, record.ticket, record.rail, record.iteration.blob),
        )

    def test_no_field_of_the_handoff_comes_from_a_transcript_or_the_provider(self) -> None:
        # The whole payload is reproducible from durable state alone: this rebuilds
        # it from the store and the control-plane read, with no session held.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        expected = self._evaluate().handoff.to_dict()
        record = self.store.read(SESSION)
        self.assertEqual(expected, {
            "project": record.project, "ticket": record.ticket, "rail": record.rail,
            "iteration": record.iteration.blob, "role": record.role,
            "workspaceKey": record.workspace_key, "worktreeId": record.worktree_id,
            "workspacePath": record.workspace_path,
            "launchedAtHead": record.launched_at_head,
            "handoff": self.HANDOFF, "handoffPublication": self.published.value,
            "sessionId": record.session_id,
        })

    # -- the safe boundary ------------------------------------------------------

    def test_marked_but_in_flight_is_not_rotation_ready(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        self.registry.begin_invocation(SESSION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT)
        self.assertIsNone(readiness.handoff)
        # The *temporal* boundary opens again the moment that invocation ends, with
        # no timer and nothing else to wait for -- in-flight is no longer the
        # reason. What the ended invocation leaves behind is a unit of work the
        # published handoff does not describe, which is a separate condition and
        # says so.
        self.registry.end_invocation(SESSION)
        after = self._evaluate()
        self.assertEqual(after.state, ROTATION_NOT_READY)
        self.assertEqual(after.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        # And a handoff published across the next invocation restores it.
        self._work(terminal=NEXT_PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_a_failed_invocation_still_closes_the_boundary_it_opened(self) -> None:
        # The failure path clears in-flight in its `finally`, so a marked session
        # that suffered a failed turn is still reachable as a *temporal* boundary.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        failure = claude_worker.ClaudeWorkerError(claude_worker.REASON_WORKER_FATAL, "boom")
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._work(fail=failure)
        self.assertEqual(self.registry.in_flight(), ())
        readiness = self._evaluate()
        # Still marked -- the floor proved the threshold -- and the temporal
        # boundary is open. But the failed invocation is a unit of work whose
        # outcome nobody can state, so the handoff published before it can no longer
        # be called current. That is checkpoint 58's work-boundary uncertainty, and
        # it fails closed rather than inferring that nothing changed.
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertEqual(
            self.registry.context(SESSION).reading().health, OBSERVATION_UNHEALTHY
        )

    # -- not marked -------------------------------------------------------------

    def test_an_unmarked_session_never_transitions(self) -> None:
        self._launch()
        self._mark(count=5)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION)
        self.assertEqual(readiness.observed, 5)

    def test_an_undetermined_mark_on_a_partial_history_is_not_permission(self) -> None:
        self._launch()
        self._mark(count=5)
        self.registry.observe_failed_invocation(SESSION, "a turn failed", ())
        reading = self.registry.context(SESSION).reading()
        self.assertIsNone(reading.rotation_marked)
        self.assertEqual(self._evaluate().reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION)

    def test_a_session_this_controller_does_not_observe_is_not_ready(self) -> None:
        self._launch()
        self.registry.remove(SESSION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION)

    # -- ambiguous product state ------------------------------------------------

    def test_a_dirty_worktree_is_not_a_rotation_boundary(self) -> None:
        self._launch()
        self._mark()
        readiness = self._evaluate(worktree=self._worktree(clean=False))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_WORKTREE_INCOHERENT)
        self.assertIsNone(readiness.handoff)

    def test_a_repository_mid_operation_is_not_a_rotation_boundary(self) -> None:
        self._launch()
        self._mark()
        readiness = self._evaluate(worktree=self._worktree(active_operation="rebase"))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_WORKTREE_INCOHERENT)
        self.assertIn("mid-rebase", readiness.detail)

    # -- durable handoff evidence -----------------------------------------------

    def test_an_unpublished_handoff_leaves_the_session_marked_and_not_ready(self) -> None:
        self._launch()
        self._mark()
        readiness = self._evaluate(handoff=self._handoff(published=False))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_PUBLISHED)
        self.assertIsNone(readiness.handoff)
        # Still marked. Not ready is not un-marked.
        self.assertIs(self.registry.context(SESSION).reading().rotation_marked, True)

    # -- fail closed on missing or contradictory durable identity ---------------

    def test_a_missing_durable_observation_is_refused_rather_than_answered(self) -> None:
        self._launch()
        self._mark()
        for missing in ("record", "handoff", "worktree", "rail"):
            with self.subTest(missing=missing):
                with self.assertRaises(LifecycleError) as caught:
                    self._evaluate(**{missing: None})
                self.assertEqual(
                    caught.exception.reason, session_lifecycle.REASON_OBSERVATION_INCOMPLETE
                )

    def test_a_rail_that_disagrees_with_the_binding_fails_closed(self) -> None:
        self._launch()
        self._mark()
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(rail=self._rail(identifier=OTHER_RAIL))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SCOPE_MISMATCH)

    def test_an_iteration_that_has_moved_on_fails_closed(self) -> None:
        self._launch()
        self._mark()
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(rail=self._rail(rail_blob=OTHER_BLOB))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_ITERATION_DRIFT)

    def test_a_handoff_observation_of_another_rail_fails_closed(self) -> None:
        self._launch()
        self._mark()
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(handoff=self._handoff(rail=OTHER_RAIL))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SCOPE_MISMATCH)

    def test_a_worktree_observation_of_another_workspace_fails_closed(self) -> None:
        self._launch()
        self._mark()
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(worktree=self._worktree(path="/somewhere/else"))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SCOPE_MISMATCH)

    def test_a_terminal_or_unbound_binding_has_no_context_to_rotate(self) -> None:
        self._launch()
        self._mark()
        unbind_session(self.store, SESSION)
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate()
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)

    def test_a_reservation_that_never_bound_a_process_has_no_context_to_rotate(self) -> None:
        reserve_binding(
            self.store, project="ai-dev", ticket="issue-55", reference=self.reference,
            workspace_path=str(self.workspace), worktree_id=self.worktree_id, rail=RAIL,
            role="executor", iteration=self.iteration, session_id=OTHER_SESSION,
            launched_at_head=HEAD, reserved_at=self.clock, ceiling=6,
        )
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(record=self.store.read(OTHER_SESSION))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_NOT_BOUND)

    # -- no side effect, which is the whole stop boundary -----------------------

    def test_reaching_rotation_ready_changes_nothing_at_all(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        before = (
            self.store.read(SESSION).to_dict(),
            self.registry.context_readings(),
            self.registry.in_flight(),
            sorted(owned.session_id for owned in self.registry.sessions()),
            self.registry.work_boundary(SESSION),
            self.registry.terminal_finalization(SESSION),
        )
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        after = (
            self.store.read(SESSION).to_dict(),
            self.registry.context_readings(),
            self.registry.in_flight(),
            sorted(owned.session_id for owned in self.registry.sessions()),
            self.registry.work_boundary(SESSION),
            self.registry.terminal_finalization(SESSION),
        )
        self.assertEqual(before, after)
        # The worker was never asked to stop, and the session is still continuable.
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_rotation_readiness_creates_no_human_attention_and_no_second_session(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        with patch.object(session_lifecycle, "start_worker") as starter, \
                patch.object(session_lifecycle, "shutdown_worker") as stopper:
            readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        starter.assert_not_called()
        stopper.assert_not_called()
        # Rotation readiness is system-owned: the projection carries a machine
        # reason and a locator, and nothing shaped like a human decision.
        self.assertFalse(hasattr(readiness, "human_decision"))

    # -- handoff currency: which publication, and whether work outran it --------
    #
    # `published` says a handoff exists. These say the one that exists was written
    # for the boundary this session is standing at. Both facts are mechanical --
    # the object name of the published bytes, and this controller's own count of
    # the invocations it began -- and no case here reads a word of what the handoff
    # says.

    def test_a_handoff_published_across_a_unit_of_work_is_current(self) -> None:
        # Case A: marked, safe boundary, coherent worktree, and a handoff this
        # controller watched appear across the invocation that produced it.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.handoff.handoff_publication, PUBLICATION)
        self.assertEqual(readiness.handoff.handoff_location, self.HANDOFF)

    def test_authorized_work_after_publication_leaves_the_handoff_insufficient(self) -> None:
        # THE regression, exactly as the checkpoint-59 review drove it: same
        # assignment, same rail, same iteration, three further authorized
        # invocations that all succeed, and no new publication after them.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        ready = self._evaluate()
        self.assertEqual(ready.state, ROTATION_READY)
        locator = ready.handoff.handoff_location

        for _ in range(3):
            self._work()

        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        # No locator is handed out at all now, where checkpoint 59 handed out this
        # byte-identical one after the same three invocations.
        self.assertIsNone(readiness.handoff)
        self.assertEqual(locator, self.HANDOFF)
        # The artifact is still there and still published. Existence was never the
        # thing in doubt.
        self.assertTrue(self._handoff().published)
        self.assertEqual(self.published.value, PUBLICATION)
        # Three later turns each ended carrying no handoff, and each cleared the
        # standing credit as it went, so what is refused now is the absence of any
        # finalization rather than a stale one.
        self.assertIn("never finalized", readiness.detail)

    def test_republishing_after_that_work_restores_readiness(self) -> None:
        # Case C, continuing from the regression above.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        for _ in range(3):
            self._work()
        self.assertEqual(self._evaluate().reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

        self._work(terminal=NEXT_PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.handoff.handoff_publication, NEXT_PUBLICATION)

    def test_the_rail_iteration_is_identical_across_ready_not_ready_and_ready(self) -> None:
        # The point of the whole checkpoint: iteration freshness could not have
        # answered this. The blob the authorization was read from is byte-identical
        # in all three states, and the iteration guard never fires.
        self._launch()
        self._mark()
        seen = []

        self._work(terminal=PUBLICATION)
        seen.append(self._evaluate().state)
        blobs = [self.store.read(SESSION).iteration.blob]

        for _ in range(3):
            self._work()
        seen.append(self._evaluate().state)
        blobs.append(self.store.read(SESSION).iteration.blob)

        self._work(terminal=NEXT_PUBLICATION)
        seen.append(self._evaluate().state)
        blobs.append(self.store.read(SESSION).iteration.blob)

        self.assertEqual(seen, [ROTATION_READY, ROTATION_NOT_READY, ROTATION_READY])
        self.assertEqual(blobs, [BLOB, BLOB, BLOB])
        self.assertEqual(self._rail().rail_blob, BLOB)
        # And iteration drift is still its own separate refusal, undisturbed.
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate(rail=self._rail(rail_blob=OTHER_BLOB))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_ITERATION_DRIFT)

    def test_no_work_after_publication_requires_no_republication(self) -> None:
        # Case D: readiness is not a demand for ceremony. With nothing done since
        # the handoff was published, repeated projections stay ready and ask for
        # nothing.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        for _ in range(4):
            self.assertEqual(self._evaluate().state, ROTATION_READY)
        self.assertEqual(self.published.value, PUBLICATION)
        self.assertEqual(
            self.registry.terminal_finalization(SESSION).publication, PUBLICATION
        )

    def test_identical_bytes_finalized_again_after_work_are_current(self) -> None:
        # Deliberately the opposite of what checkpoint 60 asserted here, and the
        # supersession is the point. Checkpoint 60 refused a republication of
        # identical bytes because, working only from two reads either side of a
        # turn, unchanged bytes were unchanged *evidence* -- it could not tell a
        # stale artifact from a freshly restated one, so it failed closed on byte
        # novelty as a proxy for recency.
        #
        # Recency is no longer a proxy. The second turn ended, this controller
        # published what that turn's terminal result carried, and the finalization
        # is recorded at the second turn's own work boundary. That the agent's
        # second handoff says what its first one said is a statement about the work,
        # which this layer does not judge and never reads. Byte novelty was never
        # the invariant; the ordering was, and the ordering holds.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        first = self.registry.terminal_finalization(SESSION)
        self._work(terminal=PUBLICATION)
        second = self.registry.terminal_finalization(SESSION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(first.publication, second.publication)
        self.assertEqual((first.work_boundary, second.work_boundary), (1, 2))
        self.assertEqual(self.registry.work_boundary(SESSION), 2)

    def test_a_handoff_this_controller_never_saw_published_is_not_current(self) -> None:
        # The ticket's own documented shape: an earlier handoff lying at the rail's
        # canonical path from work that is not this session's. It is present, it is
        # published, and this controller never watched it appear across any unit of
        # this session's work -- so it proves nothing about this session.
        self._launch()
        self._mark()
        self.published.value = PUBLICATION
        for _ in range(2):
            self._work()
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertIsNone(self.registry.terminal_finalization(SESSION))

    def test_an_observation_that_cannot_name_the_publication_fails_closed(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate(handoff=self._handoff(publication=None))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertIsNone(readiness.handoff)

    def test_a_publication_other_than_the_one_proven_current_is_refused(self) -> None:
        # Something republished the handoff outside this session's work. The
        # controller cannot say the bytes now on offer are the ones it proved.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        readiness = self._evaluate(handoff=self._handoff(publication=NEXT_PUBLICATION))
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_a_durable_publication_that_fails_leaves_nothing_standing(self) -> None:
        # An unwritable control plane is silence, never evidence -- and it does not
        # turn a completed invocation into a raised one.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        def unwritable(handoff_bytes):
            raise RuntimeError("the coordination repository could not be written")

        result = self._work(
            terminal=NEXT_PUBLICATION, finalizer=self._finalizer(publish=unwritable)
        )
        self.assertEqual(result["subtype"], "success")
        self.assertEqual(
            result["finalization"]["state"],
            session_lifecycle.FINALIZATION_PUBLICATION_FAILED,
        )
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_a_caller_that_finalizes_nothing_at_all_is_never_ready(self) -> None:
        # Currency is not opt-out. A caller that supplies no finalizer establishes
        # nothing, and readiness says so rather than falling back to existence.
        self._launch()
        self._mark()
        self.published.value = PUBLICATION
        result = self._work(terminal=PUBLICATION, finalizer=None)
        self.assertEqual(
            result["finalization"]["state"],
            session_lifecycle.FINALIZATION_NOT_ATTEMPTED,
        )
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_a_current_handoff_does_not_excuse_the_other_conditions(self) -> None:
        # Dirty, mid-operation and in-flight each still prevent readiness on their
        # own, with their own reasons, and are not reachable through currency.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        self.assertEqual(
            self._evaluate(worktree=self._worktree(clean=False)).reason,
            session_lifecycle.REASON_WORKTREE_INCOHERENT,
        )
        self.assertEqual(
            self._evaluate(worktree=self._worktree(active_operation="merge")).reason,
            session_lifecycle.REASON_WORKTREE_INCOHERENT,
        )
        self.registry.begin_invocation(SESSION)
        self.assertEqual(
            self._evaluate().reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT
        )
        self.registry.end_invocation(SESSION)

    def test_contradictory_identity_still_fails_closed_with_a_current_handoff(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        for overrides, reason in (
            ({"rail": self._rail(identifier=OTHER_RAIL)}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"rail": self._rail(rail_blob=OTHER_BLOB)}, session_lifecycle.REASON_ITERATION_DRIFT),
            ({"handoff": self._handoff(rail=OTHER_RAIL)}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"worktree": self._worktree(path="/somewhere/else")}, session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"handoff": None}, session_lifecycle.REASON_OBSERVATION_INCOMPLETE),
        ):
            with self.subTest(overrides=sorted(overrides)):
                with self.assertRaises(LifecycleError) as caught:
                    self._evaluate(**overrides)
                self.assertEqual(caught.exception.reason, reason)

    def test_the_currency_facts_carry_no_content_and_no_second_handoff(self) -> None:
        # The lifecycle never holds a representation of what a handoff says: the
        # observation is a rail, a presence, a location and an object name, and the
        # carried payload names the same one artifact.
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(RotationHandoffFacts)),
            ("rail", "published", "location", "publication", "work_state"),
        )
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        carried = self._evaluate().handoff.to_dict()
        self.assertEqual(carried["handoff"], self.HANDOFF)
        self.assertEqual(carried["handoffPublication"], PUBLICATION)
        self.assertNotIn("content", carried)
        self.assertNotIn("status", carried)

    def test_the_whole_currency_cycle_terminates_replaces_and_asks_nobody(self) -> None:
        # Items 7 and 8, across the ready -> not ready -> ready cycle rather than a
        # single projection: no worker is started or stopped, no second session
        # appears, the binding stays bound and continuable, and nothing shaped like
        # a human decision is produced at any point.
        self._launch()
        self._mark()
        with patch.object(session_lifecycle, "start_worker") as starter, \
                patch.object(session_lifecycle, "shutdown_worker") as stopper:
            self._work(terminal=PUBLICATION)
            first = self._evaluate()
            for _ in range(3):
                self._work()
            stale = self._evaluate()
            self._work(terminal=NEXT_PUBLICATION)
            again = self._evaluate()
        self.assertEqual(
            [first.state, stale.state, again.state],
            [ROTATION_READY, ROTATION_NOT_READY, ROTATION_READY],
        )
        starter.assert_not_called()
        stopper.assert_not_called()
        for readiness in (first, stale, again):
            self.assertFalse(hasattr(readiness, "human_decision"))
            self.assertIn(readiness.reason, (
                session_lifecycle.REASON_ROTATION_HANDOFF_ESTABLISHED,
                session_lifecycle.REASON_HANDOFF_NOT_CURRENT,
            ))
        self.assertEqual(
            sorted(owned.session_id for owned in self.registry.sessions()), [SESSION]
        )
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertIsNotNone(self.registry.get(SESSION))

    # -- terminal handoff finalization: the eight load-bearing shapes -----------
    #
    # Each of these is a shape the mechanism must distinguish *structurally* -- by
    # which act happened and where in the invocation lifecycle, never by reading a
    # word of a handoff and never by a rule an agent is asked to follow.

    def test_case_a_work_then_evidence_then_a_terminal_handoff_may_become_ready(self) -> None:
        self._launch()
        self._mark()
        result = self._work(
            commits_before=(NEXT_PRODUCT_HEAD,),
            evidence_after=("test suite green",),
            terminal=PUBLICATION,
        )
        self.assertEqual(
            result["finalization"]["state"], session_lifecycle.FINALIZATION_ESTABLISHED
        )
        self.assertEqual(result["finalization"]["publication"], PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.handoff.handoff_publication, PUBLICATION)
        self.assertEqual(self.evidence, ["test suite green"])

    def test_case_b_readiness_uses_only_the_final_payload(self) -> None:
        # Handoff-shaped text produced mid-turn, then more product work, then more
        # evidence work, then the real final handoff. The controller never saw the
        # mid-turn text at all -- only the result message carries a payload -- so
        # what becomes durable is the last thing the agent wrote and nothing else.
        self._launch()
        self._mark()
        result = self._work(
            commits_before=(NEXT_PRODUCT_HEAD,),
            mid_turn=None,
            evidence_after=("a second test run",),
            terminal=NEXT_PUBLICATION,
        )
        self.assertEqual(result["finalization"]["publication"], NEXT_PUBLICATION)
        self.assertEqual(self.published.value, NEXT_PUBLICATION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.handoff.handoff_publication, NEXT_PUBLICATION)

    def test_case_b_a_mid_turn_publication_is_superseded_by_the_final_payload(self) -> None:
        # The same case with the mid-turn text actually published by the agent. The
        # bytes that end up credited are still the final ones, and the durable
        # artifact is the finalized publication rather than the agent's own.
        self._launch()
        self._mark()
        self._work(
            mid_turn=PUBLICATION,
            commits_after=(NEXT_PRODUCT_HEAD,),
            evidence_after=("more evidence",),
            terminal=NEXT_PUBLICATION,
        )
        self.assertEqual(self.published.value, NEXT_PUBLICATION)
        self.assertEqual(
            self.registry.terminal_finalization(SESSION).publication, NEXT_PUBLICATION
        )
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_case_c_a_mid_turn_publication_is_not_the_crediting_publication(self) -> None:
        # Publish-like activity by the agent partway through, then further work,
        # then a turn that ends with no handoff at all. The artifact is published,
        # the tree is clean, the mark is real -- and none of that is a finalization.
        self._launch()
        self._mark()
        result = self._work(
            mid_turn=PUBLICATION,
            commits_after=(NEXT_PRODUCT_HEAD,),
            evidence_after=("work after the agent published",),
        )
        self.assertEqual(
            result["finalization"]["state"], session_lifecycle.FINALIZATION_NO_PAYLOAD
        )
        self.assertTrue(self._handoff().published)
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)

    def test_case_d_read_only_work_can_become_ready_with_no_product_movement(self) -> None:
        # The class checkpoint 61 could not reach, and the whole reason for this
        # checkpoint. A reviewer or orchestrator invocation: evidence produced, no
        # commit, no product movement whatsoever. Checkpoint 61's discriminator is
        # the same commit name at every instant -- it is asserted constant here, so
        # the pass cannot be coming from it -- and readiness is nonetheless
        # established, because the credited publication was made after the turn.
        self._launch()
        self._mark()
        head_before = self.product_head
        self._work(
            evidence_after=("read the diff", "ran two focused test classes"),
            terminal=PUBLICATION,
        )
        self.assertEqual(self.product_head, head_before)
        self.assertEqual(self._handoff().work_state, head_before)
        self.assertEqual(self._worktree().head, head_before)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.handoff.handoff_publication, PUBLICATION)

        # And the same rail, with the agent publishing for itself mid-turn and then
        # doing more read-only work, is refused -- which is precisely the pair
        # checkpoint 61 answered identically.
        self.tearDown()
        self.setUp()
        self._launch()
        self._mark()
        self._work(mid_turn=PUBLICATION, evidence_after=("more review after it",))
        self.assertEqual(self._handoff().work_state, self._worktree().head)
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)

    def test_case_e_a_failed_durable_finalization_is_not_ready(self) -> None:
        self._launch()
        self._mark()

        def refuses(handoff_bytes):
            raise RuntimeError("publish refused: coordination upstream is ahead")

        result = self._work(
            terminal=PUBLICATION, finalizer=self._finalizer(publish=refuses)
        )
        self.assertEqual(result["subtype"], "success")
        self.assertEqual(
            result["finalization"]["state"],
            session_lifecycle.FINALIZATION_PUBLICATION_FAILED,
        )
        self.assertIsNone(self.published.value)
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)

    def test_case_e_an_ambiguous_terminal_payload_finalizes_nothing(self) -> None:
        # Two envelopes in one final message. Choosing between them would be a guess
        # about which bytes the agent meant to be durable, so nothing is published.
        self._launch()
        self._mark()
        result = self._work(raw_terminal=(
            self._envelope(PUBLICATION) + self._envelope(NEXT_PUBLICATION)
        ))
        self.assertEqual(
            result["finalization"]["state"],
            session_lifecycle.FINALIZATION_AMBIGUOUS_PAYLOAD,
        )
        self.assertIsNone(self.published.value)
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)

    def test_case_f_bookkeeping_failure_fails_closed_and_the_old_context_survives(self) -> None:
        # Publication succeeds; the push or the receipt then fails, which changes the
        # unresolved work and the exact next action while the standing handoff still
        # claims otherwise. Not ready -- and the old context is untouched: still
        # owned, still bound, still continuable, never asked to stop.
        self._launch()
        self._mark()
        allocated = []

        def receipt():
            allocated.append("attempted")
            raise RuntimeError("proceed refused: the control plane was not pushed")

        with patch.object(session_lifecycle, "shutdown_worker") as stopper:
            result = self._work(
                terminal=PUBLICATION, finalizer=self._finalizer(bookkeeping=receipt)
            )
        self.assertEqual(
            result["finalization"]["state"],
            session_lifecycle.FINALIZATION_BOOKKEEPING_FAILED,
        )
        self.assertEqual(allocated, ["attempted"])
        # The bytes really did become durable -- that is what makes this the hard
        # case -- and they are still not credited.
        self.assertEqual(self.published.value, PUBLICATION)
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        stopper.assert_not_called()
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        # And another bounded invocation, whose terminal handoff says what actually
        # happened, is the recovery -- not termination first.
        self._work(terminal=NEXT_PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_case_g_a_failed_provider_invocation_keeps_the_fail_closed_semantics(self) -> None:
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        failure = claude_worker.ClaudeWorkerError(claude_worker.REASON_WORKER_FATAL, "boom")
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._work(terminal=NEXT_PUBLICATION, fail=failure)
        # The finalization never ran, so the earlier credit survives -- and is
        # refused anyway, because a further invocation has begun since it.
        self.assertEqual(self.registry.in_flight(), ())
        self.assertEqual(self.registry.terminal_finalization(SESSION).work_boundary, 1)
        self.assertEqual(self.registry.work_boundary(SESSION), 2)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertIn("work boundary", readiness.detail)
        self.assertEqual(
            self.registry.context(SESSION).reading().health, OBSERVATION_UNHEALTHY
        )

    def test_case_g_a_turn_reporting_an_error_finalizes_nothing(self) -> None:
        # The other half: an invocation that returns rather than raises, carrying a
        # provider error. The worker already drops the payload on that path; this
        # pins the lifecycle behaviour if a payload ever arrived anyway.
        self._launch()
        self._mark()
        result = self._work(terminal=PUBLICATION, is_error=True)
        self.assertTrue(result["is_error"])
        self.assertEqual(
            result["finalization"]["state"], session_lifecycle.FINALIZATION_ERRORED_TURN
        )
        self.assertIsNone(self.published.value)
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)

    def test_case_h_a_finalization_is_never_credited_across_the_next_invocation(self) -> None:
        # Checkpoint 60's protection, preserved on the new record. A finalization
        # belongs to the boundary it was made at; a later invocation moves the
        # boundary past it, whether or not that invocation touched the repository.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        credited = self.registry.terminal_finalization(SESSION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        self._work(evidence_after=("further review work, no commit",))
        self.assertEqual(self.registry.work_boundary(SESSION), 2)
        self.assertEqual(credited.work_boundary, 1)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        # The artifact never moved; only the boundary did.
        self.assertEqual(self.published.value, PUBLICATION)

    def test_the_finalization_is_the_controllers_own_act_and_follows_the_turn(self) -> None:
        # The ordering property, asserted rather than described. The publishing act
        # is recorded against the invocation bracket: it cannot run while the
        # invocation is in flight, and the agent's `send` has already returned when
        # it does. There is no arrangement of agent behaviour that reorders this,
        # because the agent is not the one publishing.
        self._launch()
        self._mark()
        seen = []

        def publish(handoff_bytes):
            seen.append(("published", self.registry.in_flight(), tuple(self.evidence)))
            self.published.publish(handoff_bytes, self.product_head)
            return handoff_bytes

        def send(handle, request, *, prompt, markers=(), timeout=None):
            self.evidence.append("last act of the turn")
            seen.append(("agent acted", self.registry.in_flight(), tuple(self.evidence)))
            return {"type": "result", "session_id": request.session_id,
                    "mode": request.mode, "subtype": "success", "is_error": False,
                    "terminal_payload": self._envelope(PUBLICATION)}

        continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on",
            send=send, alive=lambda pgid: True,
            finalize_handoff=self._finalizer(publish=publish),
        )
        self.assertEqual(
            [step for step, _, _ in seen], ["agent acted", "published"]
        )
        # In flight during the agent's act, and provably not during the publication.
        self.assertEqual(seen[0][1], (SESSION,))
        self.assertEqual(seen[1][1], ())
        # Everything the agent did is already behind the publication.
        self.assertEqual(seen[1][2], ("last act of the turn",))
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_the_finalizer_publishes_exact_bytes_without_reading_them(self) -> None:
        # No second representation of the handoff exists, and no part of this layer
        # parses one. What is published is exactly what lay between the delimiters.
        body = "# Handoff\n\n```\nproduct: action-required\n```\n\nUnresolved: none.\n"
        self._launch()
        self._mark()
        captured = []

        def publish(handoff_bytes):
            captured.append(handoff_bytes)
            self.published.publish("blob-of-" + str(len(handoff_bytes)), self.product_head)
            return "blob-of-" + str(len(handoff_bytes))

        self._work(terminal=body, finalizer=self._finalizer(publish=publish))
        self.assertEqual(captured, [body.strip("\n")])
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_nothing_of_the_terminal_payload_is_retained_by_the_lifecycle(self) -> None:
        # The permission is transport, not memory. After finalization the registry
        # holds a session id, an object name and a boundary count -- and no prose.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        finalization = self.registry.terminal_finalization(SESSION)
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(finalization)),
            ("session_id", "publication", "work_boundary"),
        )
        self.assertEqual(
            dataclasses.asdict(finalization),
            {"session_id": SESSION, "publication": PUBLICATION, "work_boundary": 1},
        )


class RealWorkerFailedContinueJoinTests(LifecycleTestBase):
    """One pipe-backed case protecting the real join the rest of the suite stubs.

    Everywhere else `send` is injected, so `run_request`'s failure-side event
    accumulation and the lifecycle fold that consumes it are proven separately and
    their join is asserted. This drives the real `run_request` over a real pipe so
    a change to either side has to keep them agreeing. One case, deliberately: the
    branch coverage lives in the two suites that already have it.
    """

    def test_a_real_worker_failure_carries_its_event_into_lifecycle_state(self) -> None:
        import os

        self._launch()
        read_fd, write_fd = os.pipe()
        writer = os.fdopen(write_fd, "w", encoding="utf-8")
        writer.write(json.dumps({
            "type": claude_worker.MESSAGE_EVENT, "protocol": 1,
            "event": {"event": EVENT_COMPACTION_OBSERVED, "session_id": SESSION,
                      "uuid": "9f1d0c3a-0000-4000-8000-00000000000f"},
        }) + "\n")
        writer.write(json.dumps({
            "type": "error", "reason": "worker-fatal", "detail": "the provider failed",
        }) + "\n")
        writer.close()
        stdout = os.fdopen(read_fd, "r", encoding="utf-8")
        self.addCleanup(stdout.close)
        piped = claude_worker.WorkerHandle(
            process=types.SimpleNamespace(
                stdin=types.SimpleNamespace(write=lambda text: None, flush=lambda: None),
                stdout=stdout, poll=lambda: None, returncode=None,
            ),
            pid=1, pgid=1, started_at="2026-08-26T12:00:01Z",
            sdk_version="0.2.152", sdk_detail=None,
        )

        def send(_handle, request, *, prompt, markers=(), **kwargs):
            return claude_worker.run_request(
                piped, request, prompt=prompt, markers=markers, **kwargs
            )

        with self.assertRaises(claude_worker.ClaudeWorkerError) as caught:
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="carry on",
                send=send, alive=lambda pgid: True,
            )
        self.assertEqual(caught.exception.reason, claude_worker.REASON_WORKER_FATAL)
        reading = self.registry.context(SESSION).reading()
        self.assertEqual(reading.observed, 1)
        self.assertIsNone(reading.count)
        self.assertEqual(reading.health, OBSERVATION_UNHEALTHY)


class OldContextRetirementTests(RotationHarness):
    """rotation-ready -> OLD CONTEXT RETIRED, and deliberately nothing past it.

    The first destructive cases in this suite. Every one of them proves what was
    terminated and what was not, and the class as a whole proves that nothing here
    launches, binds, or continues a replacement -- the half of D9's sentence this
    slice does not implement.

    No case starts a process. The worker handle is injected exactly as it is in the
    accepted stop cases, and the process group is a value these tests move, so a
    proof that the group is gone is a proof about an observation the code actually
    took rather than about a report it was handed.
    """

    RETIREMENT_CLOCK = "2026-08-26T12:10:03Z"

    def setUp(self) -> None:
        super().setUp()
        # The process group, as something observable that a stop actually changes.
        self.group_alive = {4242: True}
        # Every liveness observation and every stop, in the order they happened.
        # Ordering is the whole subject here: an ownership proof that reused an
        # older instant, or a gone-ness claim taken before the stop, would both be
        # invisible in a boolean and are plain in this list.
        self.observations = []

    def _alive(self, pgid):
        answer = bool(self.group_alive.get(pgid, False))
        self.observations.append(("alive", pgid, answer))
        return answer

    def _stopper(self, report=None, kills=True):
        """The accepted shutdown mechanism's contract, and nothing more.

        `kills` is separable from `report` on purpose: a stop that reports success
        without ending the group is exactly the claim retirement must refuse to
        take on trust.
        """
        def stop(handle):
            self.observations.append(("stop", handle.pgid))
            if kills:
                self.group_alive[handle.pgid] = False
            if report is not None:
                return report
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}
        return stop

    def _ready(self):
        """A session that is genuinely rotation-ready, by the accepted route only."""
        outcome, worker, _sent = self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        return outcome, worker

    def _retire(self, **overrides):
        arguments = {
            "store": self.store, "registry": self.registry, "rail": self._rail(),
            "record": self.store.read(SESSION), "handoff": self._handoff(),
            "worktree": self._worktree(), "now": self.RETIREMENT_CLOCK,
            "stop": self._stopper(), "alive": self._alive,
        }
        arguments.update(overrides)
        store = arguments.pop("store")
        registry = arguments.pop("registry")
        rail = arguments.pop("rail")
        record = arguments.pop("record")
        return retire_old_context(store, registry, rail, record, **arguments)

    def _assert_untouched(self, *, still_owned=True):
        """The session survived this refusal exactly as it was."""
        self.assertEqual([entry for entry in self.observations if entry[0] == "stop"], [])
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        if still_owned:
            self.assertIsNotNone(self.registry.get(SESSION))
            self.assertIsNotNone(self.registry.context(SESSION))
        # Nothing was reserved, bound, or replaced by the refusal.
        self.assertEqual([record.session_id for record in self.store.records()], [SESSION])

    # -- A. rotation-ready and owned ------------------------------------------

    def test_case_a_a_ready_owned_session_is_retired_and_proven_gone(self) -> None:
        _outcome, worker = self._ready()
        retirement = self._retire()

        self.assertTrue(retirement.retired)
        self.assertEqual(retirement.state, RETIREMENT_RETIRED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_OLD_CONTEXT_RETIRED)
        self.assertEqual(retirement.session_id, SESSION)
        self.assertIsNone(retirement.recovery)
        # The readiness that authorized it is the one this call took itself.
        self.assertTrue(retirement.readiness.ready)
        self.assertEqual(retirement.readiness.handoff.handoff_publication, PUBLICATION)
        # And the process group is gone as a matter of observation.
        self.assertTrue(retirement.stopped.process_group_gone)
        self.assertTrue(retirement.stopped.graceful)
        self.assertEqual(retirement.stopped.exit_code, 0)
        self.assertEqual(retirement.stopped.pgid, worker.pgid)
        self.assertFalse(self.group_alive[worker.pgid])

    def test_case_a_the_group_is_proven_gone_by_a_probe_taken_after_the_stop(self) -> None:
        # The distinction this whole slice turns on: `process_group_gone: True` in
        # the report is a claim, and the refusal below proves the code does not stop
        # there. Three observations, in one order: ownership proved live, the stop
        # ran, and the group was observed again afterwards and found gone.
        _outcome, worker = self._ready()
        self._retire()

        self.assertEqual(
            self.observations,
            [("alive", worker.pgid, True), ("alive", worker.pgid, True),
             ("stop", worker.pgid), ("alive", worker.pgid, False)],
        )
        # The last observation is a new one. A pre-flight snapshot reused across the
        # destructive act would have answered `True` here and the retirement would
        # have been refused, so this passing is the proof it was not reused.
        self.assertEqual(self.observations[-1], ("alive", worker.pgid, False))

    def test_case_a_lifecycle_state_is_truthful_after_a_retirement(self) -> None:
        self._ready()
        self.assertIsNotNone(self.registry.terminal_finalization(SESSION))
        self.assertEqual(self.registry.rotation_marked_session_ids(), (SESSION,))

        retirement = self._retire()

        # Not live-owned.
        self.assertIsNone(self.registry.get(SESSION))
        self.assertEqual(self.registry.sessions(), [])
        # Not rotation-ready, and not rotation-marked.
        self.assertEqual(self.registry.rotation_marked_session_ids(), ())
        # The context-lifecycle observation is released exactly as `remove` does.
        self.assertIsNone(self.registry.context(SESSION))
        self.assertEqual(self.registry.context_readings(), {})
        # And so is every other fact that only meant something while the handle did.
        self.assertIsNone(self.registry.terminal_finalization(SESSION))
        self.assertEqual(self.registry.work_boundary(SESSION), 0)
        self.assertEqual(self.registry.in_flight(), ())
        # The durable record says terminated rather than running.
        self.assertEqual(retirement.stopped.binding.state, BINDING_STATE_UNBOUND)
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_UNBOUND)
        # A retired session no longer projects a rotation boundary at all.
        with self.assertRaises(LifecycleError) as caught:
            self._evaluate()
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)

    def test_case_a_a_retirement_touches_no_real_process_and_no_provider(self) -> None:
        self._ready()
        with patch.object(subprocess, "Popen") as popen, \
                patch.object(claude_worker, "shutdown_worker") as shutdown, \
                patch.object(claude_worker, "process_group_alive") as probe, \
                patch("os.kill") as kill, patch("os.killpg") as killpg:
            retirement = self._retire()
        self.assertTrue(retirement.retired)
        popen.assert_not_called()
        shutdown.assert_not_called()
        probe.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_the_ownership_proof_and_a_fresh_readiness_both_precede_the_stop(self) -> None:
        # Order is the mechanism: prove the exact process is ours, project readiness
        # from the caller's fresh observations, and only then destroy anything.
        self._ready()
        order = []
        original_owned = session_lifecycle.require_owned
        original_ready = session_lifecycle.evaluate_rotation_readiness
        original_stop = session_lifecycle.stop_session

        def owned(*args, **kwargs):
            order.append("require_owned")
            return original_owned(*args, **kwargs)

        def ready(*args, **kwargs):
            order.append("evaluate_rotation_readiness")
            return original_ready(*args, **kwargs)

        def stop(*args, **kwargs):
            order.append("stop_session")
            return original_stop(*args, **kwargs)

        with patch.object(session_lifecycle, "require_owned", owned), \
                patch.object(session_lifecycle, "evaluate_rotation_readiness", ready), \
                patch.object(session_lifecycle, "stop_session", stop):
            self.assertTrue(self._retire().retired)
        self.assertEqual(
            order,
            ["require_owned", "evaluate_rotation_readiness", "stop_session",
             "require_owned"],
        )

    # -- B. not rotation-ready --------------------------------------------------

    def test_case_b_an_unmarked_session_is_not_retired(self) -> None:
        self._launch()
        self._work(terminal=PUBLICATION)
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION
        )
        self.assertIsNone(retirement.stopped)
        self.assertFalse(retirement.readiness.ready)
        self._assert_untouched()

    def test_case_b_a_marked_session_with_no_published_handoff_is_not_retired(self) -> None:
        self._launch()
        self._mark()
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_PUBLISHED
        )
        self.assertIsNone(retirement.stopped)
        self._assert_untouched()

    def test_case_b_an_incoherent_workspace_is_not_retired(self) -> None:
        self._ready()
        retirement = self._retire(worktree=self._worktree(clean=False))
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_WORKTREE_INCOHERENT)
        self._assert_untouched()

    def test_case_b_a_refused_session_is_still_continuable_afterwards(self) -> None:
        self._launch()
        self._mark()
        self._retire()
        # Untouched means untouched: the session goes on working normally.
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    # -- C. stale readiness -- the load-bearing case ----------------------------

    def test_case_c_a_readiness_taken_before_further_work_never_authorizes_a_stop(self) -> None:
        # The reason checkpoints 59 to 62 exist. A caller observes rotation-ready,
        # the session then does more authorized work, and the caller acts on the
        # answer it is still holding. Retirement re-projects at the moment it acts,
        # so what the caller holds is irrelevant.
        self._ready()
        stale = self._evaluate()
        self.assertTrue(stale.ready)

        self._work(terminal=None, commits_after=(NEXT_PRODUCT_HEAD,))

        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self.assertIsNone(retirement.stopped)
        self._assert_untouched()
        # The value the caller held still says ready. It was true when it was taken
        # and it is worthless now, which is exactly why it is not what authorizes.
        self.assertTrue(stale.ready)

    def test_case_c_there_is_no_parameter_a_stale_readiness_could_arrive_through(self) -> None:
        # Structural rather than conventional: retirement takes observations, and a
        # readiness verdict is not among them, so no caller can supply one.
        parameters = inspect.signature(retire_old_context).parameters
        self.assertEqual(
            tuple(parameters),
            ("store", "registry", "rail", "record", "handoff", "worktree", "now",
             "stop", "alive"),
        )
        self.assertNotIn("readiness", parameters)
        self.assertNotIn("ready", parameters)

    def test_case_c_the_projection_is_taken_from_the_callers_observations_at_call_time(self) -> None:
        # The same session, the same registry, two different fresh reads: one
        # retires and the other refuses. Nothing about the session is stored that
        # could have decided this.
        self._ready()
        moved = self._retire(worktree=self._worktree(head=NEXT_PRODUCT_HEAD))
        self.assertEqual(moved.state, RETIREMENT_REFUSED)
        self.assertEqual(moved.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self._assert_untouched()
        self.assertTrue(self._retire().retired)

    # -- D. disconnected or unprovable ownership --------------------------------

    def test_case_d_a_ready_session_whose_group_is_gone_routes_to_recovery(self) -> None:
        # Readiness deliberately says nothing about liveness, so this session is
        # rotation-ready by every durable fact while the process it names is gone.
        # Retiring it would terminalize a binding on a durable claim alone.
        _outcome, worker = self._ready()
        self.group_alive[worker.pgid] = False
        self.assertEqual(self._evaluate().state, ROTATION_READY)

        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_DISCONNECTED_NOT_LIVE)
        self.assertIsNone(retirement.stopped)
        # Ownership was proved independently of readiness, and failed before any
        # readiness value existed to be consulted.
        self.assertIsNone(retirement.readiness)
        # It is a human decision, described rather than acted on.
        self.assertEqual(retirement.recovery.state, STATE_DISCONNECTED)
        self.assertEqual(retirement.recovery.session_id, SESSION)
        self.assertEqual(retirement.recovery.elapsed_seconds, 601)
        self.assertIn("human decides", retirement.recovery.human_decision)
        self.assertEqual(retirement.detail, retirement.recovery.human_decision)
        self._assert_untouched()

    def test_case_d_a_handle_that_disagrees_with_its_binding_routes_to_recovery(self) -> None:
        self._ready()
        owned = self.registry.get(SESSION)
        # Re-taking ownership never resets an observation, so the session stays
        # marked and every readiness fact stays exactly where it was.
        self.registry.add(dataclasses.replace(owned, pid=owned.pid + 1))

        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_DISCONNECTED_MISMATCH)
        self.assertIsNone(retirement.stopped)
        self.assertIsNone(retirement.readiness)
        self.assertIn("human decides", retirement.recovery.human_decision)
        self._assert_untouched()

    def test_case_d_a_restarted_controller_retires_nothing_on_the_durable_record(self) -> None:
        self._ready()
        restarted = SessionRegistry()  # a fresh controller owns nothing

        retirement = self._retire(registry=restarted)
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_DISCONNECTED_NO_HANDLE)
        self.assertIsNone(retirement.stopped)
        self.assertIsNone(retirement.readiness)
        self.assertIn("human decides", retirement.recovery.human_decision)
        self._assert_untouched()

    def test_case_d_the_recovery_route_performs_no_process_action_at_all(self) -> None:
        _outcome, worker = self._ready()
        self.group_alive[worker.pgid] = False
        with patch.object(subprocess, "Popen") as popen, \
                patch.object(claude_worker, "shutdown_worker") as shutdown, \
                patch("os.kill") as kill, patch("os.killpg") as killpg:
            retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        popen.assert_not_called()
        shutdown.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    def test_case_d_retirement_never_reaches_the_stop_without_an_owned_handle(self) -> None:
        # `stop_session` demands `require_owned` and retirement does not get to
        # decide otherwise: the same proof is taken before it, and the stop is not
        # reached at all when it fails.
        self._ready()
        self.registry.remove(SESSION)
        reached = []
        original_stop = session_lifecycle.stop_session

        def stop(*args, **kwargs):
            reached.append(True)
            return original_stop(*args, **kwargs)

        with patch.object(session_lifecycle, "stop_session", stop):
            retirement = self._retire()
        self.assertEqual(reached, [])
        self.assertEqual(retirement.reason, session_lifecycle.REASON_DISCONNECTED_NO_HANDLE)

    # -- E. an invocation in flight ---------------------------------------------

    def test_case_e_an_invocation_in_flight_is_never_interrupted(self) -> None:
        self._ready()
        self.registry.begin_invocation(SESSION)
        try:
            retirement = self._retire()
        finally:
            self.registry.end_invocation(SESSION)
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT)
        self.assertIsNone(retirement.stopped)
        self._assert_untouched()

    def test_case_e_a_retirement_asked_during_a_turn_stops_nothing(self) -> None:
        # The refusal is taken from inside a real invocation bracket, which is the
        # only instant at which the question is dangerous.
        self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        attempts = []

        def send(handle, request, *, prompt, markers=(), timeout=None):
            attempts.append(self._retire())
            return {"type": "result", "session_id": request.session_id,
                    "mode": request.mode, "subtype": "success", "is_error": False,
                    "terminal_payload": self._envelope(NEXT_PUBLICATION)}

        continue_session(
            self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
            registry=self.registry, session_id=SESSION,
            request_kwargs=self._request_kwargs(), prompt="carry on",
            send=send, alive=lambda pgid: True, finalize_handoff=self._finalizer(),
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT)
        self.assertEqual([entry for entry in self.observations if entry[0] == "stop"], [])
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertIsNotNone(self.registry.get(SESSION))

    # -- F. retirement failure --------------------------------------------------

    def test_case_f_a_shutdown_that_reports_failure_leaves_the_session_whole(self) -> None:
        self._ready()
        with self.assertRaises(LifecycleError) as caught:
            self._retire(stop=self._stopper(
                report={"graceful": False, "process_group_gone": False}, kills=False
            ))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE)
        self._assert_still_owned_bound_and_continuable()

    def test_case_f_a_shutdown_that_claims_success_it_cannot_prove_is_refused(self) -> None:
        # The report says the group is gone and the group is still there. This is the
        # half-retired state in its most tempting form, and it is refused.
        self._ready()
        with self.assertRaises(LifecycleError) as caught:
            self._retire(stop=self._stopper(
                report={"graceful": True, "exit_code": 0, "process_group_gone": True},
                kills=False,
            ))
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE)
        self._assert_still_owned_bound_and_continuable()

    def test_case_f_a_shutdown_that_raises_leaves_the_session_whole(self) -> None:
        self._ready()

        def stop(handle):
            self.observations.append(("stop", handle.pgid))
            raise claude_worker.ClaudeWorkerError("shutdown-incomplete", "boom")

        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._retire(stop=stop)
        self._assert_still_owned_bound_and_continuable(stopped=True)

    def _assert_still_owned_bound_and_continuable(self, *, stopped=False) -> None:
        """A failure to retire leaves a session, not a half-retired remnant."""
        record = self.store.read(SESSION)
        self.assertEqual(record.state, BINDING_STATE_BOUND)
        self.assertFalse(record.is_terminal)
        # Still owned, with every fact the handle carried still standing.
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertIsNotNone(self.registry.context(SESSION))
        self.assertIsNotNone(self.registry.terminal_finalization(SESSION))
        self.assertEqual(self.registry.work_boundary(SESSION), 1)
        self.assertEqual(self.registry.rotation_marked_session_ids(), (SESSION,))
        # Nothing was launched or reserved in its place.
        self.assertEqual([entry.session_id for entry in self.store.records()], [SESSION])
        self.assertEqual([owned.session_id for owned in self.registry.sessions()], [SESSION])
        if stopped:
            self.assertIn(("stop", 4242), self.observations)
        # And it is still continuable: a failure to retire is not a dead session.
        self._work(terminal=NEXT_PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    # -- G. no replacement, on any path ----------------------------------------

    def test_case_g_a_successful_retirement_launches_and_binds_nothing(self) -> None:
        self._ready()
        # Every route to a new context, held shut across the destructive act.
        with patch.object(session_lifecycle, "start_worker") as start, \
                patch.object(session_lifecycle, "reserve_binding") as reserve, \
                patch.object(session_lifecycle, "launch_request") as launch, \
                patch.object(session_lifecycle, "resume_request") as resume, \
                patch.object(session_lifecycle, "run_request") as run:
            retirement = self._retire()
        self.assertTrue(retirement.retired)
        start.assert_not_called()
        reserve.assert_not_called()
        launch.assert_not_called()
        resume.assert_not_called()
        run.assert_not_called()
        # Exactly one durable record exists, and it is the one that was retired.
        records = self.store.records()
        self.assertEqual([entry.session_id for entry in records], [SESSION])
        self.assertEqual(records[0].state, BINDING_STATE_UNBOUND)
        # Nothing is owned, nothing is reserved, nothing is bound.
        self.assertEqual(self.registry.sessions(), [])
        self.assertEqual(self.registry.in_flight(), ())

    def test_case_g_the_retirement_outcome_carries_no_replacement(self) -> None:
        self._ready()
        retirement = self._retire()
        fields = {field.name for field in dataclasses.fields(retirement)}
        self.assertEqual(
            fields,
            {"session_id", "rail", "state", "reason", "detail", "readiness",
             "stopped", "recovery"},
        )
        # The durable identity a later slice would resume from is reported, and
        # resuming from it is not something this slice can do.
        self.assertEqual(retirement.readiness.handoff.session_id, SESSION)
        self.assertNotIn("replacement", retirement.detail)

    def test_case_g_retiring_an_already_retired_session_is_refused(self) -> None:
        self._ready()
        self.assertTrue(self._retire().retired)
        with self.assertRaises(LifecycleError) as caught:
            self._retire()
        self.assertEqual(caught.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL)
        self.assertEqual([entry.session_id for entry in self.store.records()], [SESSION])

    # -- H. the accepted properties are still exactly what they were ------------

    def test_case_h_58_a_failed_invocation_invalidates_the_currency_retirement_needs(self) -> None:
        self._ready()
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            self._work(fail=claude_worker.ClaudeWorkerError("worker-fatal", "boom"))
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self._assert_untouched()

    def test_case_h_60_a_finalization_is_never_credited_across_the_next_invocation(self) -> None:
        self._ready()
        boundary = self.registry.work_boundary(SESSION)
        self._work(terminal=None)
        self.assertEqual(self.registry.work_boundary(SESSION), boundary + 1)
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self._assert_untouched()

    def test_case_h_61_product_state_moving_outside_any_invocation_still_refuses(self) -> None:
        # The residual guard: nothing inside a turn can produce this any more, but a
        # human or another session can, and a handoff written against a state the
        # workspace has left is not what a replacement would resume from.
        self._ready()
        self.product_head = NEXT_PRODUCT_HEAD
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self._assert_untouched()

    def test_case_h_62_only_a_post_turn_finalization_can_authorize_a_retirement(self) -> None:
        # The agent publishes a real handoff for itself midway through its turn and
        # keeps working. The bytes on the rail are genuine and every other condition
        # holds; nothing proves they followed the agent's last act, so the old
        # context is not retired.
        self._launch()
        self._mark()
        self._work(mid_turn=PUBLICATION, terminal=None,
                   commits_after=(NEXT_PRODUCT_HEAD,))
        self.assertTrue(self._handoff().published)
        retirement = self._retire()
        self.assertEqual(retirement.state, RETIREMENT_REFUSED)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT)
        self._assert_untouched()

    def test_case_h_an_identity_contradiction_still_fails_closed_before_any_stop(self) -> None:
        self._ready()
        for override, reason in (
            ({"rail": self._rail(identifier=OTHER_RAIL)},
             session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"rail": self._rail(rail_blob=OTHER_BLOB)},
             session_lifecycle.REASON_ITERATION_DRIFT),
            ({"handoff": self._handoff(rail=OTHER_RAIL)},
             session_lifecycle.REASON_SCOPE_MISMATCH),
            ({"worktree": self._worktree(path="/somewhere/else")},
             session_lifecycle.REASON_SCOPE_MISMATCH),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(LifecycleError) as caught:
                    self._retire(**override)
                self.assertEqual(caught.exception.reason, reason)
                self._assert_untouched()


class StopCategoryReconciliationTests(RotationHarness):
    """No rotation-shaped composition may stop a managed context outside the gate.

    The subject is the *category* of a stop, and where it comes from. It comes from
    the session's own rotation mark, read out of the registry at the instant of the
    stop -- never from a parameter, a function name, or which caller is asking. So
    these cases never establish a category by arranging a caller; they arrange the
    *session*, and then try every public door.

    The fixtures are the accepted retirement ones, deliberately: the same launch,
    the same real threshold mark, the same injected worker handle, and the same
    observation list, so that a stop refused here is refused on exactly the facts a
    stop permitted there was permitted on.
    """

    def setUp(self) -> None:
        super().setUp()
        self.group_alive = {4242: True}
        self.observations = []

    def _alive(self, pgid):
        answer = bool(self.group_alive.get(pgid, False))
        self.observations.append(("alive", pgid, answer))
        return answer

    def _stopper(self, report=None, kills=True):
        def stop(handle):
            self.observations.append(("stop", handle.pgid))
            if kills:
                self.group_alive[handle.pgid] = False
            if report is not None:
                return report
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}
        return stop

    def _unmarked(self):
        """A launched session that is provably below the rotation threshold."""
        outcome, worker, _sent = self._launch()
        self._work(terminal=PUBLICATION)
        return outcome, worker

    def _ready(self):
        """A genuinely rotation-ready session, by the accepted route only."""
        outcome, worker, _sent = self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        return outcome, worker

    def _retire(self, **overrides):
        arguments = {
            "store": self.store, "registry": self.registry, "rail": self._rail(),
            "record": self.store.read(SESSION), "handoff": self._handoff(),
            "worktree": self._worktree(), "now": "2026-08-26T12:10:03Z",
            "stop": self._stopper(), "alive": self._alive,
        }
        arguments.update(overrides)
        store = arguments.pop("store")
        registry = arguments.pop("registry")
        rail = arguments.pop("rail")
        record = arguments.pop("record")
        return retire_old_context(store, registry, rail, record, **arguments)

    def _stop_directly(self, **overrides):
        """The raw stop, exactly as any caller reaching past the gate would make it."""
        arguments = {"stop": self._stopper(), "alive": self._alive}
        arguments.update(overrides)
        return stop_session(
            self.store, self.registry, self.store.read(SESSION), **arguments
        )

    def _assert_untouched(self):
        """The session survived this refusal exactly as it was: nothing destroyed."""
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertIsNotNone(self.registry.context(SESSION))
        self.assertEqual(
            [record.session_id for record in self.store.records()], [SESSION]
        )
        self.assertTrue(self.group_alive[4242])

    # -- the category itself ---------------------------------------------------

    def test_the_category_comes_from_the_session_not_from_the_caller(self) -> None:
        """One session, two categories, and nothing about the caller changed.

        This is the whole structural claim in one case. The same registry, the same
        record, the same call -- and the answer moves only because the session's own
        observed compaction count crossed D9's threshold.
        """
        self._unmarked()
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_NON_ROTATION,
        )
        self._mark()
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_ROTATION,
        )

    def test_the_category_is_total_and_a_stop_is_permitted_by_exactly_one_of_it(self) -> None:
        """Three answers, and only one of them is a stop this module will perform."""
        self.assertEqual(len(set(session_lifecycle.STOP_CATEGORIES)), 3)
        self._unmarked()
        for arrange in (
            lambda: None,
            lambda: self._mark(),
            lambda: self.registry.observe_failed_invocation(SESSION, "not provable"),
        ):
            arrange()
            self.assertIn(
                session_lifecycle.stop_category(self.registry, SESSION),
                session_lifecycle.STOP_CATEGORIES,
            )

    def test_the_category_is_read_in_call_and_not_carried(self) -> None:
        """A category read before the mark does not authorize the stop after it."""
        self._unmarked()
        held = session_lifecycle.stop_category(self.registry, SESSION)
        self.assertEqual(held, session_lifecycle.STOP_CATEGORY_NON_ROTATION)
        self._mark()
        # The held value still reads as teardown, and is powerless.
        self.assertEqual(held, session_lifecycle.STOP_CATEGORY_NON_ROTATION)
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    # -- A. through the gate ---------------------------------------------------

    def test_case_a_a_rotation_through_the_gate_is_permitted(self) -> None:
        self._ready()
        retirement = self._retire()
        self.assertTrue(retirement.retired)
        self.assertEqual(retirement.reason, session_lifecycle.REASON_OLD_CONTEXT_RETIRED)
        self.assertIsNotNone(retirement.stopped)
        self.assertTrue(retirement.stopped.process_group_gone)
        self.assertFalse(self.group_alive[4242])
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(self.registry.sessions(), [])

    def test_case_a_the_gate_still_refuses_a_stale_handoff(self) -> None:
        self._ready()
        self._work()
        retirement = self._retire()
        self.assertFalse(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT
        )
        self._assert_untouched()

    def test_case_a_the_gate_still_routes_a_disconnected_session_to_recovery(self) -> None:
        self._ready()
        self.group_alive[4242] = False
        retirement = self._retire()
        self.assertFalse(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_DISCONNECTED_NOT_LIVE
        )
        self.assertIsNotNone(retirement.recovery)
        self.assertIsNone(retirement.readiness)
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )

    def test_case_a_the_gate_still_refuses_an_invocation_in_flight(self) -> None:
        self._ready()
        self.registry.begin_invocation(SESSION)
        try:
            retirement = self._retire()
        finally:
            self.registry.end_invocation(SESSION)
        self.assertFalse(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT
        )
        self._assert_untouched()

    def test_case_a_the_gate_still_refuses_a_session_that_is_not_ready(self) -> None:
        self._unmarked()
        retirement = self._retire()
        self.assertFalse(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION
        )
        self._assert_untouched()

    # -- B. bypassing the gate: the load-bearing case ---------------------------

    def test_case_b_a_raw_stop_of_a_marked_session_is_refused(self) -> None:
        """The composition this checkpoint exists to make unavailable.

        A session that is marked, ready, owned and alive -- every fact a rotation
        needs -- and the raw stop still refuses, because the raw stop is not the
        gate. Nothing is destroyed, so the work this session is holding survives to
        be retired properly.
        """
        self._ready()
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    def test_case_b_the_refusal_precedes_the_stopper_entirely(self) -> None:
        """Refused before anything is asked to die, not after it failed to."""
        self._ready()
        with self.assertRaises(session_lifecycle.LifecycleError):
            self._stop_directly(stop=self._stopper(kills=True))
        self.assertNotIn(("stop", 4242), self.observations)
        self.assertTrue(self.group_alive[4242])

    def test_case_b_an_arbitrary_object_is_not_an_authorization(self) -> None:
        self._ready()
        for forgery in (True, "authorized", object(), {"ready": True}):
            with self.assertRaises(session_lifecycle.LifecycleError) as raised:
                self._stop_directly(_retirement=forgery)
            self.assertEqual(
                raised.exception.reason,
                session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
            )
        self._assert_untouched()

    def test_case_b_an_authorization_for_another_session_is_refused(self) -> None:
        """It names the session it was minted for, so it cannot be pointed elsewhere."""
        self._ready()
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        elsewhere = session_lifecycle._RetirementAuthorization(
            session_id="some-other-session", readiness=readiness
        )
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly(_retirement=elsewhere)
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    def test_case_b_an_authorization_carrying_a_refusal_is_refused(self) -> None:
        """A verdict that never said yes cannot be dressed up as one that did."""
        self._unmarked()
        not_ready = self._evaluate()
        self.assertFalse(not_ready.ready)
        self._mark()
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly(
                _retirement=session_lifecycle._RetirementAuthorization(
                    session_id=SESSION, readiness=not_ready
                )
            )
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    def test_case_b_a_real_authorization_cannot_be_replayed(self) -> None:
        """Kept from a genuine retirement, it stops nothing a second time."""
        self._ready()
        captured = []
        original = session_lifecycle.stop_session

        def capturing(*args, **kwargs):
            captured.append(kwargs.get("_retirement"))
            return original(*args, **kwargs)

        with patch.object(session_lifecycle, "stop_session", capturing):
            self.assertTrue(self._retire().retired)
        authorization = captured[0]
        self.assertIsInstance(
            authorization, session_lifecycle._RetirementAuthorization
        )
        # The very same authorization, against the very same session.
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            stop_session(
                self.store, self.registry, self.store.read(SESSION),
                stop=self._stopper(), alive=self._alive,
                _retirement=authorization,
            )
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL
        )

    def test_case_b_no_public_name_yields_an_authorization(self) -> None:
        """Refused, and the refusal has no public escape hatch to argue with.

        The parameter is private, the type is private, and nothing exported from
        this module constructs or returns one. `retire_old_context` is the only
        producer, and it produces one only after its own refusals have all declined
        to fire.
        """
        public = [name for name in vars(session_lifecycle) if not name.startswith("_")]
        self.assertNotIn("RetirementAuthorization", public)
        for name in public:
            self.assertNotIsInstance(
                getattr(session_lifecycle, name),
                session_lifecycle._RetirementAuthorization,
            )
        self.assertIn("_retirement", session_lifecycle.stop_session.__code__.co_varnames)
        signature = inspect.signature(session_lifecycle.stop_session)
        self.assertEqual(
            [name for name in signature.parameters if name.startswith("_")],
            ["_retirement"],
        )
        source = inspect.getsource(session_lifecycle)
        self.assertEqual(source.count("_RetirementAuthorization("), 1)

    # -- C / D. teardown is untouched ------------------------------------------

    def test_case_c_teardown_of_an_unmarked_session_still_works(self) -> None:
        """The accepted stop, on the accepted facts, with the accepted result."""
        self._unmarked()
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_NON_ROTATION,
        )
        outcome = self._stop_directly()
        self.assertTrue(outcome.process_group_gone)
        self.assertTrue(outcome.binding.is_terminal)
        self.assertFalse(self.group_alive[4242])
        self.assertEqual(self.registry.sessions(), [])
        self.assertIn(("stop", 4242), self.observations)

    def test_case_c_a_freshly_launched_session_is_provably_teardown(self) -> None:
        """The category a one-shot dispatch's teardown sees, before any work at all.

        `launch_session` is the only production caller that takes ownership, and it
        observes from the session's start, so a session it launched is provably
        below the threshold rather than merely not known to be above it.
        """
        self._launch()
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_NON_ROTATION,
        )
        self.assertIs(self.registry.context(SESSION).reading().rotation_marked, False)

    # -- E. a category that cannot be established ------------------------------

    def test_case_e_an_unprovable_category_fails_closed(self) -> None:
        """Under the threshold but not provably so, and therefore not teardown.

        The observation floor is below six, but the history is not complete, so
        whether the threshold was reached is undetermined. The accepted reading
        already refuses to call that unmarked, and the stop now refuses to act as
        though it were.
        """
        self._unmarked()
        self.registry.observe_failed_invocation(
            SESSION, "the invocation watching this session did not finish"
        )
        self.assertIsNone(self.registry.context(SESSION).reading().rotation_marked)
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_UNPROVEN,
        )
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_STOP_CATEGORY_UNPROVEN,
        )
        self._assert_untouched()

    def test_case_e_an_unprovable_category_does_not_default_to_unconditional(self) -> None:
        """It refuses rather than falling through to the accepted teardown."""
        self._unmarked()
        self.registry.observe_failed_invocation(SESSION, "history is not provable")
        with self.assertRaises(session_lifecycle.LifecycleError):
            self._stop_directly()
        # And no authorization rescues it either: retirement is not available to a
        # session that cannot even be shown to be marked.
        retirement = self._retire()
        self.assertFalse(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION
        )
        self._assert_untouched()

    def test_case_e_an_unwatched_session_is_unprovable_rather_than_unmarked(self) -> None:
        """Not having watched a session is not evidence about it."""
        self._unmarked()
        self.registry._context.forget(SESSION)
        self.assertIsNone(self.registry.context(SESSION))
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_UNPROVEN,
        )
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_STOP_CATEGORY_UNPROVEN,
        )

    # -- F. the checkpoint-63 properties, through the new precondition ---------

    def test_case_f_the_termination_proof_is_still_taken_after_the_stop(self) -> None:
        """The observation sequence is exactly the accepted one; nothing perturbed it."""
        self._ready()
        self.assertTrue(self._retire().retired)
        self.assertEqual(
            self.observations,
            [("alive", 4242, True), ("alive", 4242, True), ("stop", 4242),
             ("alive", 4242, False)],
        )

    def test_case_f_ownership_is_still_proven_before_the_category(self) -> None:
        """A session nobody can prove they hold is still answered as disconnected."""
        self._ready()
        self.registry.remove(SESSION)
        with self.assertRaises(session_lifecycle.LifecycleError) as raised:
            self._stop_directly()
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_HANDLE_MISSING
        )

    def test_case_f_the_gate_still_launches_nothing_on_the_permitted_path(self) -> None:
        self._ready()
        with patch.object(session_lifecycle, "start_worker") as worker, \
                patch.object(session_lifecycle, "reserve_binding") as reserve:
            self.assertTrue(self._retire().retired)
        worker.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual([r.session_id for r in self.store.records()], [SESSION])
        self.assertEqual(self.registry.sessions(), [])
        self.assertEqual(self.registry.in_flight(), ())


class SupervisedTeardownTests(RotationHarness):
    """A session whose category cannot be established is stopped, not stranded.

    Checkpoint 64 made a rotation-shaped stop impossible outside the retirement
    gate, and in doing so left `category-unprovable` refused by every route: the
    stop refuses it, the gate refuses it as `not-marked-for-rotation`, and recovery
    refuses it as not disconnected. A refusal that can never succeed is not neutral
    -- the reconciler counts every nonterminal binding against D6's ceiling, so it
    holds an agent slot for the life of the controller.

    These cases are about the third route and, just as much, about what it must not
    become. The fixtures are the accepted retirement ones, deliberately: the same
    launch, the same real threshold mark, the same injected handle, so a stop
    refused here is refused on exactly the facts a stop permitted there was
    permitted on.
    """

    def setUp(self) -> None:
        super().setUp()
        self.group_alive = {4242: True}
        self.observations = []

    def _alive(self, pgid):
        answer = bool(self.group_alive.get(pgid, False))
        self.observations.append(("alive", pgid, answer))
        return answer

    def _stopper(self, report=None, kills=True):
        def stop(handle):
            self.observations.append(("stop", handle.pgid))
            if kills:
                self.group_alive[handle.pgid] = False
            if report is not None:
                return report
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}
        return stop

    def _unprovable(self):
        """A launched, owned, live session whose compaction history is not complete.

        Exactly the state checkpoint 64's review traced: the observation floor is
        below the threshold, but a failed invocation means the history cannot be
        called complete, so the reading refuses to say whether the threshold was
        reached. Reached through the accepted observation path, not by setting a
        flag.
        """
        outcome, worker, _sent = self._launch()
        self.registry.observe_failed_invocation(
            SESSION, "the invocation watching this session did not finish"
        )
        self.assertIsNone(self.registry.context(SESSION).reading().rotation_marked)
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_UNPROVEN,
        )
        return outcome, worker

    def _marked(self):
        """A genuinely rotation-ready session, by the accepted route only."""
        outcome, worker, _sent = self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        return outcome, worker

    def _supervise(self, **overrides):
        arguments = {
            "now": "2026-08-26T12:10:03Z", "stop": self._stopper(), "alive": self._alive,
        }
        arguments.update(overrides)
        return session_lifecycle.supervised_teardown(
            self.store, self.registry, self.store.read(SESSION), **arguments
        )

    def _retire(self, **overrides):
        arguments = {
            "handoff": self._handoff(), "worktree": self._worktree(),
            "now": "2026-08-26T12:10:03Z", "stop": self._stopper(), "alive": self._alive,
        }
        arguments.update(overrides)
        return retire_old_context(
            self.store, self.registry, self._rail(), self.store.read(SESSION),
            **arguments
        )

    def _slots(self):
        """What D6's reconciler makes of this controller, right now.

        The production reduction, not a restatement of it: the same durable records
        the manager admits against, and the same ownership evidence its registry can
        prove. A separate prober, so asking the question leaves no trace in the
        observation sequence the stop cases assert on.
        """
        records = list(self.store.records())
        return reconcile_agent_slots(
            records,
            ownership=session_lifecycle.ownership_evidence(
                self.registry, records,
                alive=lambda pgid: bool(self.group_alive.get(pgid, False)),
            ),
        )

    def _assert_untouched(self):
        """The session survived this refusal exactly as it was: nothing destroyed."""
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertTrue(self.group_alive[4242])

    # -- A. the state that had no route at all ---------------------------------

    def test_case_a_an_unprovable_category_is_stopped_rather_than_refused_forever(self) -> None:
        """The whole point: it stops, and the binding is truthfully terminal.

        Every other door refuses this session, and each of those refusals is right
        for the route it belongs to. This one acts, and says why it could not tell
        what kind of stop it was performing.
        """
        self._unprovable()
        result = self._supervise()
        self.assertTrue(result.torn_down)
        self.assertEqual(result.state, session_lifecycle.SUPERVISED_STOPPED)
        self.assertEqual(result.reason, session_lifecycle.REASON_SUPERVISED_TEARDOWN)
        self.assertIsNotNone(result.stopped)
        self.assertTrue(result.stopped.process_group_gone)
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(self.registry.sessions(), [])
        self.assertIsNone(result.recovery)

    def test_case_a_the_process_group_is_proven_gone_and_not_asserted(self) -> None:
        """The proof is a fresh observation taken after the stopper, as everywhere."""
        self._unprovable()
        self.assertTrue(self._supervise().torn_down)
        self.assertFalse(self.group_alive[4242])
        self.assertEqual(
            self.observations,
            [("alive", 4242, True), ("stop", 4242), ("alive", 4242, False)],
        )

    def test_case_a_the_ambiguity_is_reported_rather_than_swallowed(self) -> None:
        """The report names the unresolved question and what a human should check."""
        self._unprovable()
        result = self._supervise()
        reading = "whether the rotation threshold of 6 was reached is undetermined"
        self.assertIn(reading, result.ambiguity)
        self.assertIn(result.ambiguity, result.human_action)
        self.assertIn(RAIL, result.human_action)
        self.assertIn("handoff", result.human_action)
        self.assertIn("nothing was launched", result.detail)

    def test_case_a_the_supervised_route_launches_and_binds_nothing(self) -> None:
        """It stops. A rotation is a stop plus a replacement, and there is none."""
        self._unprovable()
        with patch.object(session_lifecycle, "start_worker") as worker, \
                patch.object(session_lifecycle, "reserve_binding") as reserve:
            self.assertTrue(self._supervise().torn_down)
        worker.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual([r.session_id for r in self.store.records()], [SESSION])
        self.assertEqual(self.registry.in_flight(), ())

    def test_case_a_a_session_this_controller_never_watched_is_also_stoppable(self) -> None:
        """The other producer of an unprovable category, through the same route."""
        self._launch()
        self.registry._context.forget(SESSION)
        self.assertIsNone(self.registry.context(SESSION))
        result = self._supervise()
        self.assertTrue(result.torn_down)
        self.assertIn("no context observation", result.ambiguity)
        self.assertTrue(self.store.read(SESSION).is_terminal)

    # -- B. the load-bearing case: this must not become a bypass ---------------

    def test_case_b_a_rotation_marked_session_is_refused_by_the_supervised_route(self) -> None:
        """Marked, ready, owned and live -- and still not this route's business.

        Supervised teardown acts on a category that cannot be established. This one
        is established, so the refusal names the route that owns it.
        """
        self._marked()
        with self.assertRaises(LifecycleError) as raised:
            self._supervise()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        # Two independent refusals stand between this route and a marked session,
        # and this is the outer one: the route's own precondition, which says why
        # this is the wrong route rather than only that the stop is refused.
        self.assertIn("its category is established", raised.exception.detail)
        self.assertIn("retire_old_context", raised.exception.detail)
        self._assert_untouched()

    def test_case_b_the_rotation_refusal_lives_below_every_route(self) -> None:
        """Structural, not procedural: the primitive refuses for itself.

        This reaches past both public routes to the one place that destroys
        anything, holding a proven owned handle for a marked session, and it is
        still refused. So a route that forgot to check the category -- this one, or
        one written later -- could not become a way to stop a marked session.
        """
        self._marked()
        record = self.store.read(SESSION)
        owned = require_owned(self.registry, record, alive=self._alive)
        with self.assertRaises(LifecycleError) as raised:
            session_lifecycle._stop_owned_process(
                self.store, self.registry, record, owned,
                stop=self._stopper(), alive=self._alive,
            )
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    def test_case_b_the_supervised_route_mints_no_authorization_and_carries_none(self) -> None:
        """It has nowhere to put one, and nothing in it makes one."""
        source = inspect.getsource(session_lifecycle.supervised_teardown)
        self.assertNotIn("_RetirementAuthorization", source)
        self.assertNotIn("_retirement=", source)
        self.assertEqual(
            [name for name in
             inspect.signature(session_lifecycle.supervised_teardown).parameters
             if name.startswith("_")],
            [],
        )
        self._unprovable()
        with self.assertRaises(TypeError):
            self._supervise(_retirement=object())
        self._assert_untouched()

    def test_case_b_the_two_routes_cannot_be_confused_at_the_seam(self) -> None:
        """One route per established category, and neither answers for the other."""
        self._marked()
        with self.assertRaises(LifecycleError) as supervised:
            self._supervise()
        self.assertEqual(
            supervised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()
        # And the gate is still the only thing that can stop it -- unchanged by the
        # refusal it just made.
        retirement = self._retire()
        self.assertTrue(retirement.retired)
        self.assertEqual(
            retirement.reason, session_lifecycle.REASON_OLD_CONTEXT_RETIRED
        )
        self.assertFalse(self.group_alive[4242])

    def test_case_b_a_raw_stop_of_a_marked_session_is_still_refused(self) -> None:
        """Checkpoint 64's own case, re-asserted through the refactored primitive."""
        self._marked()
        with self.assertRaises(LifecycleError) as raised:
            stop_session(
                self.store, self.registry, self.store.read(SESSION),
                stop=self._stopper(), alive=self._alive,
            )
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()

    # -- C. the accepted teardown, unchanged -----------------------------------

    def test_case_c_ordinary_teardown_of_a_provably_unmarked_session_still_works(self) -> None:
        self._launch()
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_NON_ROTATION,
        )
        outcome = stop_session(
            self.store, self.registry, self.store.read(SESSION),
            stop=self._stopper(), alive=self._alive,
        )
        self.assertTrue(outcome.process_group_gone)
        self.assertTrue(outcome.binding.is_terminal)
        self.assertFalse(self.group_alive[4242])
        self.assertEqual(self.registry.sessions(), [])

    def test_case_c_the_supervised_route_refuses_an_established_category(self) -> None:
        """A provable category needs no supervision, and does not get any here."""
        self._launch()
        with self.assertRaises(LifecycleError) as raised:
            self._supervise()
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_CATEGORY_IS_PROVABLE
        )
        self.assertIn("stop_session", raised.exception.detail)
        self._assert_untouched()
        # And the route that does own it still works, after the refusal.
        self.assertTrue(
            stop_session(
                self.store, self.registry, self.store.read(SESSION),
                stop=self._stopper(), alive=self._alive,
            ).binding.is_terminal
        )

    # -- D. disconnection still goes to a human --------------------------------

    def test_case_d_a_disconnected_session_still_routes_to_recovery(self) -> None:
        """Supervised teardown is not a new way to skip the disconnected reading."""
        self._unprovable()
        self.group_alive[4242] = False
        result = self._supervise()
        self.assertFalse(result.torn_down)
        self.assertEqual(result.state, session_lifecycle.SUPERVISED_REFUSED)
        self.assertEqual(
            result.reason, session_lifecycle.REASON_DISCONNECTED_NOT_LIVE
        )
        self.assertIsNotNone(result.recovery)
        self.assertIsNone(result.stopped)
        self.assertEqual(result.recovery.state, STATE_DISCONNECTED)
        self.assertIn("A human decides", result.human_action)
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )
        self.assertFalse(self.store.read(SESSION).is_terminal)

    def test_case_d_a_session_with_no_owned_handle_routes_to_recovery(self) -> None:
        """Ownership is proven before the category is even asked for."""
        self._unprovable()
        self.registry.remove(SESSION)
        result = self._supervise()
        self.assertFalse(result.torn_down)
        self.assertEqual(
            result.reason, session_lifecycle.REASON_DISCONNECTED_NO_HANDLE
        )
        self.assertIsNotNone(result.recovery)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )

    # -- E. failure fails closed -----------------------------------------------

    def test_case_e_a_shutdown_that_proves_nothing_fails_closed(self) -> None:
        """The session survives truthfully, and the report says what a human must do."""
        self._unprovable()
        result = self._supervise(
            stop=self._stopper(
                report={"graceful": False, "exit_code": None,
                        "process_group_gone": False},
                kills=False,
            )
        )
        self.assertFalse(result.torn_down)
        self.assertEqual(result.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE)
        self.assertIsNone(result.stopped)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertTrue(self.group_alive[4242])
        self.assertIn("4242", result.human_action)
        self.assertIn("still occupies one of this manager's agent slots",
                      result.human_action)
        self.assertIn("Nothing was launched", result.human_action)

    def test_case_e_a_stopper_that_claims_more_than_it_proves_fails_closed(self) -> None:
        """It said the group was gone; the fresh observation disagreed."""
        self._unprovable()
        result = self._supervise(stop=self._stopper(kills=False))
        self.assertFalse(result.torn_down)
        self.assertEqual(result.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE)
        self.assertIn("still alive after shutdown", result.detail)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertTrue(self.group_alive[4242])

    def test_case_e_nothing_is_launched_on_the_failed_path(self) -> None:
        self._unprovable()
        with patch.object(session_lifecycle, "start_worker") as worker, \
                patch.object(session_lifecycle, "reserve_binding") as reserve:
            result = self._supervise(
                stop=self._stopper(
                    report={"graceful": False, "exit_code": None,
                            "process_group_gone": False},
                    kills=False,
                )
            )
        self.assertFalse(result.torn_down)
        worker.assert_not_called()
        reserve.assert_not_called()
        self.assertEqual([r.session_id for r in self.store.records()], [SESSION])

    def test_case_e_the_ambiguity_is_still_reported_on_the_failed_path(self) -> None:
        self._unprovable()
        result = self._supervise(stop=self._stopper(kills=False))
        self.assertIn("undetermined", result.ambiguity)
        self.assertIn(result.ambiguity, result.human_action)

    # -- F. the D6 slot, proven behaviourally ----------------------------------

    def test_case_f_the_supervised_teardown_releases_the_d6_slot(self) -> None:
        """The accepted-requirement half, through the production reconciler.

        Before: one nonterminal binding this controller can prove it owns, and the
        reconciler counts it against the ceiling of six. After: the binding is
        terminal, so the reconciler no longer counts it -- and does not count it as
        unprovable either, which would be the same slot held under another name.
        """
        self._unprovable()
        before = self._slots()
        self.assertEqual(before.occupants, (SESSION,))
        self.assertEqual(before.occupied, 1)
        self.assertTrue(before.provable)

        self.assertTrue(self._supervise().torn_down)

        after = self._slots()
        self.assertEqual(after.occupants, ())
        self.assertEqual(after.occupied, 0)
        self.assertEqual(after.unprovable, ())
        self.assertTrue(after.provable)
        self.assertEqual(after.ceiling, before.ceiling)

    def test_case_f_a_refused_stop_keeps_holding_the_slot_it_holds(self) -> None:
        """The hazard, stated as a fact rather than as a fear -- and its boundary.

        A stop that cannot be proven leaves the binding nonterminal, so the slot
        stays occupied. That is correct: the process may still be running. What was
        wrong was a refusal no route could ever get past, and that is what the
        supervised route removes -- not the fail-closed behaviour of a stop that
        genuinely failed.
        """
        self._unprovable()
        refused = self._supervise(stop=self._stopper(kills=False))
        self.assertFalse(refused.torn_down)
        self.assertEqual(self._slots().occupied, 1)
        # The same session, stopped successfully on a later attempt, releases it.
        self.assertTrue(self._supervise().torn_down)
        self.assertEqual(self._slots().occupied, 0)

    def test_case_f_the_release_is_the_accepted_terminalization_and_not_a_new_one(self) -> None:
        """No second way to make a binding terminal was invented for this route."""
        self.assertEqual(
            inspect.getsource(session_lifecycle.supervised_teardown).count(
                "unbind_session("
            ),
            0,
        )
        self._unprovable()
        result = self._supervise()
        self.assertEqual(result.stopped.binding.state, BINDING_STATE_UNBOUND)
        self.assertTrue(result.stopped.binding.is_terminal)

    # -- G. checkpoint 63 and 64 properties ------------------------------------

    def test_case_g_the_retirement_gate_is_unchanged_end_to_end(self) -> None:
        self._marked()
        retirement = self._retire()
        self.assertTrue(retirement.retired)
        self.assertEqual(
            self.observations,
            [("alive", 4242, True), ("alive", 4242, True), ("stop", 4242),
             ("alive", 4242, False)],
        )
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(self.registry.sessions(), [])

    def test_case_g_readiness_is_still_projected_in_call(self) -> None:
        """A verdict taken before further work does not survive that work."""
        self._marked()
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        self._work()
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)
        self.assertEqual(
            self._retire().reason, session_lifecycle.REASON_HANDOFF_NOT_CURRENT
        )
        self._assert_untouched()

    def test_case_g_the_single_producer_and_the_private_surface_are_unchanged(self) -> None:
        """Checkpoint 64's self-maintaining assertions, re-asserted beside the new route."""
        source = inspect.getsource(session_lifecycle)
        self.assertEqual(source.count("_RetirementAuthorization("), 1)
        self.assertEqual(
            [name for name in
             inspect.signature(session_lifecycle.stop_session).parameters
             if name.startswith("_")],
            ["_retirement"],
        )
        public = [name for name in vars(session_lifecycle) if not name.startswith("_")]
        self.assertNotIn("RetirementAuthorization", public)
        for name in public:
            self.assertNotIsInstance(
                getattr(session_lifecycle, name),
                session_lifecycle._RetirementAuthorization,
            )

    def test_case_g_exactly_two_routes_reach_the_one_destructive_primitive(self) -> None:
        """One definition, two call sites, and both of them are the guarded routes."""
        self.assertEqual(
            inspect.getsource(session_lifecycle).count("_stop_owned_process("), 3
        )
        for route in (session_lifecycle.stop_session,
                      session_lifecycle.supervised_teardown):
            with self.subTest(route=route.__name__):
                self.assertIn(
                    "_stop_owned_process(", inspect.getsource(route)
                )
        self.assertNotIn(
            "shutdown_worker",
            inspect.getsource(session_lifecycle.supervised_teardown),
        )

    def test_case_g_ownership_is_still_proven_independently_of_the_category(self) -> None:
        self._unprovable()
        self.registry.remove(SESSION)
        with self.assertRaises(LifecycleError) as raised:
            stop_session(
                self.store, self.registry, self.store.read(SESSION),
                stop=self._stopper(), alive=self._alive,
            )
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_HANDLE_MISSING
        )

    def test_case_g_a_terminal_binding_is_refused_by_the_new_route_too(self) -> None:
        self._unprovable()
        self.assertTrue(self._supervise().torn_down)
        with self.assertRaises(LifecycleError) as raised:
            self._supervise()
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_BINDING_TERMINAL
        )

    # -- H. the counting the category is read from -----------------------------

    def test_case_h_marking_counting_and_degradation_are_unchanged(self) -> None:
        """The three readings this route depends on, none of them touched here."""
        self._launch()
        self.assertIs(self.registry.context(SESSION).reading().rotation_marked, False)
        self.registry.observe_failed_invocation(SESSION, "history is not provable")
        self.assertIsNone(self.registry.context(SESSION).reading().rotation_marked)
        self._mark()
        self.assertIs(self.registry.context(SESSION).reading().rotation_marked, True)
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_ROTATION,
        )

    def test_case_h_a_marked_session_is_never_supervised_however_it_got_marked(self) -> None:
        """Degraded first, then marked: the mark wins, and the gate owns it."""
        self._launch()
        self.registry.observe_failed_invocation(SESSION, "history is not provable")
        self._mark()
        with self.assertRaises(LifecycleError) as raised:
            self._supervise()
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_untouched()


if __name__ == "__main__":
    unittest.main()


class ReplacementLaunchTests(RotationHarness):
    """retired old context -> A REPLACEMENT LAUNCHED AND BOUND, and nothing past it.

    Checkpoint 63 implemented the first half of D9's terminate-and-replace sentence
    and stopped, launching nothing. This is the second half. It is deliberately not
    the third: a successor is brought into existence and bound to the rail, and no
    work is invoked through it, because resuming from the durable handoff is a
    separate act that these cases prove is not reachable from here.

    The fixtures are the accepted retirement ones, on purpose: the same launch, the
    same real threshold mark, the same published handoff, the same injected handle
    and the same process group modelled as a value these tests move. So a swap
    permitted here is permitted on exactly the facts a retirement was permitted on
    there, and a process proven gone is proven by an observation the code took
    rather than by a report it was handed.

    No case starts a real process.
    """

    RETIREMENT_CLOCK = "2026-08-26T12:10:03Z"
    PREDECESSOR_PGID = 4242
    SUCCESSOR_PGID = 5353

    def setUp(self) -> None:
        super().setUp()
        self.group_alive = {self.PREDECESSOR_PGID: True}
        # Every liveness observation, stop, durable read and process start, in the
        # order they happened. Ordering is the entire subject of this class: a
        # reservation taken before a stop, or a fact read before the retirement it
        # authorizes, would both be invisible in a boolean and are plain here.
        self.observations = []
        self._stopped = False
        self._probes_since_stop = 0
        # When set, the predecessor's process group comes back to life this many
        # post-stop probes later -- which is how a retirement that honestly proved
        # it gone can still be wrong by the time a launch would happen.
        self.resurrect_after = None
        self.successor = None

    # -- the world these cases observe ----------------------------------------

    def _alive(self, pgid):
        if pgid == self.PREDECESSOR_PGID and self._stopped:
            self._probes_since_stop += 1
            if (
                self.resurrect_after is not None
                and self._probes_since_stop > self.resurrect_after
            ):
                self.group_alive[pgid] = True
        answer = bool(self.group_alive.get(pgid, False))
        self.observations.append(("alive", pgid, answer))
        return answer

    def _stopper(self, report=None, kills=True):
        def stop(handle):
            self.observations.append(("stop", handle.pgid))
            self._stopped = True
            if kills:
                self.group_alive[handle.pgid] = False
            if report is not None:
                return report
            return {"graceful": True, "exit_code": 0, "process_group_gone": True}
        return stop

    def _successor_starter(self, fail=None):
        """Attach the successor exactly as the accepted starter attaches any launch."""
        worker = FakeHandle(pid=self.SUCCESSOR_PGID, pgid=self.SUCCESSOR_PGID)
        start, worker = self._starter(handle=worker, fail=fail)

        def wrapped(store, reserved, **kwargs):
            # Recorded before the attach, with the state the record is actually in:
            # a successor started against anything but a reservation would show here.
            self.observations.append(("start", reserved.session_id, reserved.state))
            outcome = start(store, reserved, **kwargs)
            self.group_alive[worker.pgid] = True
            return outcome

        self.successor = worker
        return wrapped

    # -- the facts the route reads for itself ---------------------------------

    def _read_rail(self):
        self.observations.append(("read", "rail"))
        return self._rail()

    def _read_handoff(self):
        self.observations.append(("read", "handoff"))
        return self._handoff()

    def _read_worktree(self):
        self.observations.append(("read", "worktree"))
        return self._worktree()

    def _observation(self, **overrides):
        arguments = {
            "project": "ai-dev",
            "ticket": "issue-55",
            "head": HEAD,
            "rails": (
                RailObservation(
                    identifier=RAIL, status="running", rail_blob=BLOB, role="executor"
                ),
            ),
            "workspace": WorkspaceObservation(
                workspace_key="github:jmrozi1/ai-dev#55",
                worktree_id=self.worktree_id,
                workspace_path=str(self.workspace),
            ),
        }
        arguments.update(overrides)
        return ControlPlaneObservation(**arguments)

    def _read_observation(self):
        self.observations.append(("read", "observation"))
        return self._observation()

    def _read_slots(self, records):
        """The occupancy reduction, as the controller hands it over: over exactly
        the records the route read for itself, at the moment it asks."""
        self.observations.append(
            ("read", "slots", tuple(
                sorted(r.session_id for r in records if not r.is_terminal)
            ))
        )
        return self._reduce(records)

    def _reduce(self, records, ceiling=6):
        """What D6's reconciler makes of these records, by the production reduction.

        A separate prober from `_alive`, so asking the question leaves no trace in
        the observation sequence the ordering cases assert on.
        """
        return reconcile_agent_slots(
            list(records),
            ownership=session_lifecycle.ownership_evidence(
                self.registry, list(records),
                alive=lambda pgid: bool(self.group_alive.get(pgid, False)),
            ),
            ceiling=ceiling,
        )

    def _slots(self, ceiling=6):
        """What this controller's occupancy is right now, over the durable store."""
        return self._reduce(self.store.records(), ceiling=ceiling)

    # -- the states these cases start from ------------------------------------

    def _ready(self):
        """A genuinely rotation-ready session, by the accepted route only."""
        outcome, worker, _sent = self._launch()
        self._mark()
        self._work(terminal=PUBLICATION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        return outcome, worker

    def _unprovable(self):
        """A live session whose compaction history cannot be called complete."""
        outcome, worker, _sent = self._launch()
        self.registry.observe_failed_invocation(
            SESSION, "the invocation watching this session did not finish"
        )
        self.assertIsNone(self.registry.context(SESSION).reading().rotation_marked)
        self.assertEqual(
            session_lifecycle.stop_category(self.registry, SESSION),
            session_lifecycle.STOP_CATEGORY_UNPROVEN,
        )
        return outcome, worker

    def _fill(self, count, ceiling=6):
        """Other managed agents this controller holds, so the ceiling is real."""
        filled = []
        for index in range(count):
            rail = "issue-55-filler-rail-{0}".format(index)
            blob = "{0}{1}".format(index, "f" * 39)
            iteration = RailIteration(rail=rail, blob=blob)
            session = "1a2b3c4d-01{0:02d}-4000-8000-00000000000f".format(index)
            pid = 7000 + index
            reserve_binding(
                self.store, project="ai-dev", ticket="issue-55",
                reference=self.reference, workspace_path=str(self.workspace),
                worktree_id=self.worktree_id, rail=rail, role="executor",
                iteration=iteration, session_id=session, launched_at_head=HEAD,
                reserved_at=self.clock, ceiling=ceiling,
            )
            bound = attach_process(
                self.store, session, pid=pid, pid_domain="test-host",
                started_at="2026-08-26T12:00:02Z", bound_at="2026-08-26T12:00:03Z",
                expected_iteration=iteration,
            )
            self.group_alive[pid] = True
            self.registry.add(
                OwnedSession(
                    session_id=session, handle=FakeHandle(pid=pid, pgid=pid), pid=pid,
                    pid_domain="test-host", pgid=pid, started_at=bound.started_at,
                    iteration=iteration, workspace_path=bound.workspace_path,
                    role=bound.role,
                ),
                observed_from_start=True,
            )
            filled.append(session)
        return filled

    def _replace(self, **overrides):
        arguments = {
            "session_id": SESSION,
            "assignment": self.assignment,
            "read_rail": self._read_rail,
            "read_handoff": self._read_handoff,
            "read_worktree": self._read_worktree,
            "read_slots": self._read_slots,
            "read_observation": self._read_observation,
            "reference": self.reference,
            "request_kwargs": self._request_kwargs(),
            "package_root": self.repo_root,
            "now": self.RETIREMENT_CLOCK,
            "clock": lambda: self.clock,
            "new_session_id": lambda: SUCCESSOR,
            "start": self._successor_starter(),
            "stop": self._stopper(),
            "alive": self._alive,
        }
        arguments.update(overrides)
        return session_lifecycle.replace_old_context(
            self.store, self.registry, **arguments
        )

    # -- shared assertions -----------------------------------------------------

    def _assert_nothing_launched(self):
        """No successor exists: no reservation, no process, no second binding."""
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "start"], []
        )
        self.assertIsNone(self.store.read(SUCCESSOR))
        self.assertIsNone(self.registry.get(SUCCESSOR))
        self.assertEqual(
            sorted(record.session_id for record in self.store.records()), [SESSION]
        )

    def _assert_predecessor_survives(self):
        """The old session is exactly as it was: owned, bound, nonterminal, live."""
        self.assertEqual(
            [entry for entry in self.observations if entry[0] == "stop"], []
        )
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertTrue(self.group_alive[self.PREDECESSOR_PGID])
        self.assertEqual(self._slots().occupants, (SESSION,))

    # -- A. retired -> replacement launched and bound --------------------------

    def test_case_a_a_retired_context_is_replaced_by_a_distinct_bound_successor(self) -> None:
        _outcome, worker = self._ready()
        before = self._slots()
        self.assertEqual(before.occupants, (SESSION,))

        replacement = self._replace()

        self.assertTrue(replacement.launched)
        self.assertEqual(replacement.state, session_lifecycle.REPLACEMENT_LAUNCHED)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_REPLACEMENT_LAUNCHED
        )
        self.assertEqual(replacement.predecessor_session_id, SESSION)

        # The predecessor was retired by this call, on the readiness this call
        # projected, and its group is gone as a matter of observation.
        self.assertTrue(replacement.retirement.retired)
        self.assertTrue(replacement.retirement.readiness.ready)
        self.assertEqual(
            replacement.retirement.readiness.handoff.handoff_publication, PUBLICATION
        )
        self.assertTrue(replacement.retirement.stopped.process_group_gone)
        self.assertEqual(replacement.retirement.stopped.pgid, worker.pgid)
        self.assertFalse(self.group_alive[worker.pgid])

        # The successor is a different session, and says so in every record of it.
        self.assertEqual(replacement.replacement.session_id, SUCCESSOR)
        self.assertNotEqual(replacement.replacement.session_id, SESSION)
        self.assertEqual(replacement.replacement.binding.state, BINDING_STATE_BOUND)
        self.assertEqual(replacement.replacement.binding.rail, RAIL)
        self.assertEqual(replacement.replacement.owned.pgid, self.SUCCESSOR_PGID)
        self.assertIsNotNone(self.registry.get(SUCCESSOR))

        # The predecessor's binding is terminal, and this controller holds nothing
        # for it: no handle, no context, no work boundary.
        predecessor = self.store.read(SESSION)
        self.assertTrue(predecessor.is_terminal)
        self.assertEqual(predecessor.state, BINDING_STATE_UNBOUND)
        self.assertIsNone(self.registry.get(SESSION))
        self.assertIsNone(self.registry.context(SESSION))

        # D6 across the swap: one occupant before, one after, and it is the
        # successor rather than the predecessor. Nothing became unprovable.
        after = self._slots()
        self.assertEqual(after.occupants, (SUCCESSOR,))
        self.assertEqual(after.occupied, 1)
        self.assertEqual(after.unprovable, ())
        self.assertTrue(after.provable)
        self.assertEqual(after.ceiling, before.ceiling)

    def test_case_a_the_successor_is_reserved_only_after_the_predecessor_is_gone(self) -> None:
        self._ready()
        self._replace()

        kinds = [entry[0] for entry in self.observations]
        stopped_at = kinds.index("stop")
        started_at = kinds.index("start")
        self.assertLess(stopped_at, started_at)
        # And the successor's process was started against a *reservation*, which is
        # the accepted launch ordering: the durable record existed before anything
        # was spawned.
        self.assertEqual(
            self.observations[started_at], ("start", SUCCESSOR, BINDING_STATE_RESERVED)
        )
        # The proof that the group is gone is an observation taken after the stop,
        # and this route takes one of its own after the retirement returned.
        post_stop_probes = [
            entry for entry in self.observations[stopped_at:started_at]
            if entry[0] == "alive" and entry[1] == self.PREDECESSOR_PGID
        ]
        self.assertGreaterEqual(len(post_stop_probes), 2)
        self.assertTrue(all(entry[2] is False for entry in post_stop_probes))
        # The occupancy this launch was admitted against was reduced from a store
        # read taken after the terminalization, so the slot being counted is the
        # released one -- not the predecessor's, carried from before the stop.
        reductions = [
            entry for entry in self.observations
            if entry[0] == "read" and entry[1] == "slots"
        ]
        self.assertEqual(len(reductions), 1)
        self.assertGreater(self.observations.index(reductions[0]), stopped_at)
        self.assertEqual(reductions[0][2], ())
        # Every fact the retirement decided on was read before the stop, and the
        # facts the launch decided on only after it.
        rail_read = self.observations.index(("read", "rail"))
        observation_read = self.observations.index(("read", "observation"))
        self.assertLess(rail_read, stopped_at)
        self.assertLess(self.observations.index(("read", "handoff")), stopped_at)
        self.assertLess(self.observations.index(("read", "worktree")), stopped_at)
        self.assertGreater(observation_read, stopped_at)
        self.assertLess(observation_read, started_at)

    def test_case_a_a_successor_may_not_be_minted_with_its_predecessors_identity(self) -> None:
        self._ready()
        with self.assertRaises(LifecycleError) as raised:
            self._replace(new_session_id=lambda: SESSION)
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_SUCCESSOR_IDENTITY_REUSED
        )
        # The retirement really did happen -- this refusal is about the successor --
        # and no second record was written for the reused id.
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(
            sorted(record.session_id for record in self.store.records()), [SESSION]
        )
        self.assertEqual([entry for entry in self.observations if entry[0] == "start"], [])

    def test_case_a_the_store_refuses_a_reused_session_id_beneath_the_route(self) -> None:
        """The identity guard is not the only thing holding this."""
        self._ready()
        self._replace()
        with self.assertRaises(SessionBindingError) as raised:
            reserve_binding(
                self.store, project="ai-dev", ticket="issue-55",
                reference=self.reference, workspace_path=str(self.workspace),
                worktree_id=self.worktree_id, rail=RAIL, role="executor",
                iteration=RailIteration(rail=RAIL, blob=OTHER_BLOB),
                session_id=SESSION, launched_at_head=HEAD,
                reserved_at=self.clock, ceiling=6,
            )
        self.assertIn("already", str(raised.exception))

    # -- B. a live predecessor is never replaced -------------------------------

    def test_case_b_a_predecessor_that_was_not_retired_gets_no_replacement(self) -> None:
        """LOAD-BEARING: no replacement is bound beside a live predecessor."""
        outcome, _worker, _sent = self._launch()
        self._mark()
        # No handoff was ever published, so this session is live and not ready.
        self.assertEqual(self._evaluate().state, ROTATION_NOT_READY)

        replacement = self._replace()

        self.assertFalse(replacement.launched)
        self.assertEqual(replacement.state, session_lifecycle.REPLACEMENT_REFUSED)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_PREDECESSOR_NOT_RETIRED
        )
        self.assertIsNone(replacement.replacement)
        self.assertFalse(replacement.retirement.retired)
        self.assertIn("nothing was launched", replacement.detail)
        self._assert_nothing_launched()
        self._assert_predecessor_survives()

    def test_case_b_the_authorizer_cannot_produce_a_launch_beside_a_live_binding(self) -> None:
        """The refusal above is not the only thing standing between the two.

        Even if a route reached the launch with a live predecessor, the accepted
        authorizer answers a rail that already holds a live binding with a
        *continuation* -- never a launch -- and the launch gate demands a launch.
        """
        self._ready()
        records = list(self.store.records())
        decision = authorize(
            self._observation(),
            project="ai-dev", ticket="issue-55", rail=RAIL, role="executor",
            expected_head=HEAD, rail_blob=BLOB, slots=self._slots(),
            bindings=records, in_flight_session_ids=(),
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.action, ACTION_CONTINUE)
        self.assertNotEqual(decision.action, ACTION_LAUNCH)

    def test_case_b_the_store_refuses_a_second_binding_for_a_live_rail_iteration(self) -> None:
        """And beneath the authorizer, the durable record refuses it too."""
        self._ready()
        with self.assertRaises(SessionBindingError) as raised:
            reserve_binding(
                self.store, project="ai-dev", ticket="issue-55",
                reference=self.reference, workspace_path=str(self.workspace),
                worktree_id=self.worktree_id, rail=RAIL, role="executor",
                iteration=self.iteration, session_id=SUCCESSOR,
                launched_at_head=HEAD, reserved_at=self.clock, ceiling=6,
            )
        self.assertIn("already held by session {0}".format(SESSION), str(raised.exception))

    def test_case_b_a_predecessor_alive_again_at_the_launch_instant_gets_no_replacement(self) -> None:
        """The retirement proved it gone; this route proves it again, and disagrees.

        The strongest form of "actually retired": the retirement's own post-shutdown
        probe honestly returned false, and by the instant a reservation would be
        written the group answers again. A launch here would bind a successor beside
        a live predecessor on the strength of a claim that was true a moment ago.
        """
        self._ready()
        self.resurrect_after = 1
        with self.assertRaises(LifecycleError) as raised:
            self._replace()
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_RETIREMENT_UNPROVEN
        )
        self.assertIn("fresh liveness probe", str(raised.exception))
        self.assertIn("Nothing was launched", str(raised.exception))
        self._assert_nothing_launched()

    # -- C. retirement failed -> no launch, and the truth survives -------------

    def test_case_c_a_shutdown_that_cannot_be_proven_launches_nothing(self) -> None:
        self._ready()
        with self.assertRaises(LifecycleError) as raised:
            self._replace(stop=self._stopper(kills=False))
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE
        )
        # The old session survives truthfully: still bound, still owned, still
        # counted, and its process group is still there.
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)
        self.assertFalse(self.store.read(SESSION).is_terminal)
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertTrue(self.group_alive[self.PREDECESSOR_PGID])
        self.assertEqual(self._slots().occupants, (SESSION,))
        self._assert_nothing_launched()

    def test_case_c_a_stopper_that_reports_no_shutdown_launches_nothing(self) -> None:
        self._ready()
        with self.assertRaises(LifecycleError) as raised:
            self._replace(
                stop=self._stopper(
                    report={"graceful": False, "exit_code": None,
                            "process_group_gone": False},
                    kills=False,
                )
            )
        self.assertEqual(
            raised.exception.reason, session_lifecycle.REASON_SHUTDOWN_INCOMPLETE
        )
        self._assert_nothing_launched()
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_case_c_a_disconnected_predecessor_is_recovered_not_replaced(self) -> None:
        """Retirement routes it to a human; a successor would claim it was rotated."""
        self._ready()
        self.registry.remove(SESSION)
        replacement = self._replace()
        self.assertFalse(replacement.launched)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_PREDECESSOR_NOT_RETIRED
        )
        self.assertIsNotNone(replacement.retirement.recovery)
        self.assertIsNone(replacement.replacement)
        self._assert_nothing_launched()

    def test_case_c_a_missing_binding_is_refused_rather_than_replaced(self) -> None:
        replacement = self._replace()
        self.assertFalse(replacement.launched)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_PREDECESSOR_MISSING
        )
        self.assertIsNone(replacement.retirement)
        self.assertIsNone(replacement.replacement)
        self.assertEqual(list(self.store.records()), [])

    # -- D. supervised teardown is not rotation and produces no successor ------

    def test_case_d_a_supervised_teardown_launches_no_replacement(self) -> None:
        """It stops a session whose category nobody established. There is no
        continuity for a successor to claim, so none is created."""
        _outcome, worker = self._unprovable()
        teardown = session_lifecycle.supervised_teardown(
            self.store, self.registry, self.store.read(SESSION),
            now=self.RETIREMENT_CLOCK, stop=self._stopper(), alive=self._alive,
        )
        self.assertTrue(teardown.torn_down)
        self.assertFalse(self.group_alive[worker.pgid])
        # Stopped, and nothing put in its place.
        self.assertFalse(hasattr(teardown, "replacement"))
        self._assert_nothing_launched()
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(self._slots().occupants, ())
        self.assertEqual(self._slots().unprovable, ())

    def test_case_d_the_supervised_route_cannot_reach_a_launch_at_all(self) -> None:
        source = inspect.getsource(session_lifecycle.supervised_teardown)
        for token in (
            "replace_old_context", "_reserve_and_bind", "reserve_binding",
            "launch_session", "start_worker", "_NewBinding", "BoundReplacement",
        ):
            self.assertEqual(source.count(token), 0, token)
        self.assertEqual(
            list(inspect.signature(session_lifecycle.supervised_teardown).parameters),
            ["store", "registry", "record", "now", "stop", "alive"],
        )

    def test_case_d_an_unprovable_category_gets_no_replacement_by_the_rotation_route(self) -> None:
        """And the rotation route will not quietly do the supervised route's job."""
        self._unprovable()
        replacement = self._replace()
        self.assertFalse(replacement.launched)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_PREDECESSOR_NOT_RETIRED
        )
        self.assertEqual(
            replacement.retirement.reason,
            session_lifecycle.REASON_NOT_MARKED_FOR_ROTATION,
        )
        self._assert_nothing_launched()
        self._assert_predecessor_survives()

    # -- E. the ceiling holds across the swap ---------------------------------

    def test_case_e_a_swap_at_the_ceiling_neither_exceeds_it_nor_consumes_two_slots(self) -> None:
        self._ready()
        fillers = self._fill(5)
        before = self._slots()
        self.assertEqual(before.occupied, 6)
        self.assertEqual(before.ceiling, 6)
        self.assertTrue(before.provable)
        self.assertIn(SESSION, before.occupants)

        replacement = self._replace()
        self.assertTrue(replacement.launched)

        after = self._slots()
        self.assertEqual(after.occupied, 6)
        self.assertEqual(after.ceiling, 6)
        self.assertTrue(after.provable)
        self.assertEqual(after.unprovable, ())
        # The successor took the slot the predecessor released, and nothing else
        # moved: exactly one identity changed.
        self.assertEqual(
            sorted(after.occupants), sorted(list(fillers) + [SUCCESSOR])
        )
        self.assertNotIn(SESSION, after.occupants)
        # Two slots were never consumed: the predecessor's binding is terminal, and
        # the nonterminal set is the same size it was.
        self.assertTrue(self.store.read(SESSION).is_terminal)
        self.assertEqual(
            len([r for r in self.store.records() if not r.is_terminal]), 6
        )

    def test_case_e_the_ceiling_is_real_at_the_limit_this_swap_passed(self) -> None:
        """Non-vacuity: the swap fitted because a slot was released, not because
        the ceiling was not being enforced."""
        self._ready()
        self._fill(5)
        self.assertEqual(self._slots().occupied, 6)
        with self.assertRaises(SessionBindingError) as raised:
            reserve_binding(
                self.store, project="ai-dev", ticket="issue-55",
                reference=self.reference, workspace_path=str(self.workspace),
                worktree_id=self.worktree_id, rail="issue-55-one-too-many",
                role="executor",
                iteration=RailIteration(rail="issue-55-one-too-many", blob="9" * 40),
                session_id=OTHER_SESSION, launched_at_head=HEAD,
                reserved_at=self.clock, ceiling=6,
            )
        self.assertIn("past the ceiling", str(raised.exception))

    def test_case_e_a_launch_before_the_retirement_would_have_been_refused(self) -> None:
        """Which is why the ordering is the mechanism and not a preference."""
        self._ready()
        self._fill(5)
        records = list(self.store.records())
        decision = authorize(
            self._observation(
                rails=(
                    RailObservation(
                        identifier="issue-55-fresh-rail", status="running",
                        rail_blob=BLOB, role="executor",
                    ),
                ),
            ),
            project="ai-dev", ticket="issue-55", rail="issue-55-fresh-rail",
            role="executor", expected_head=HEAD, rail_blob=BLOB,
            slots=self._slots(), bindings=records, in_flight_session_ids=(),
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.reason, REASON_CONCURRENCY_CEILING)

    def test_case_e_an_unauthorized_replacement_leaves_the_rail_one_agent_lighter(self) -> None:
        """A refusal after the retirement is truthful, not a second launch attempt."""
        self._ready()
        replacement = self._replace(
            read_observation=lambda: self._observation(head="0" * 40)
        )
        self.assertFalse(replacement.launched)
        self.assertEqual(
            replacement.reason, session_lifecycle.REASON_REPLACEMENT_NOT_AUTHORIZED
        )
        self.assertTrue(replacement.retirement.retired)
        self.assertIsNone(replacement.replacement)
        self.assertIn("one fewer agent", replacement.detail)
        # The slot was released and nothing took it. Never two, never seven.
        self.assertEqual(self._slots().occupants, ())
        self.assertIsNone(self.store.read(SUCCESSOR))

    # -- F. nothing is continued through the replacement ----------------------

    def test_case_f_no_work_invocation_reaches_the_replacement(self) -> None:
        self._ready()

        def forbidden(*args, **kwargs):
            raise AssertionError("work was invoked on the replacement")

        with patch.object(session_lifecycle, "run_request", forbidden):
            replacement = self._replace()

        self.assertTrue(replacement.launched)
        # Nothing was sent, so nothing is in flight and no boundary was opened.
        self.assertEqual(self.registry.in_flight(), ())
        self.assertEqual(self.registry.work_boundary(SUCCESSOR), 0)
        self.assertIsNone(self.registry.terminal_finalization(SUCCESSOR))
        # And no result exists to be mistaken for one: the type has no such field.
        self.assertNotIn(
            "result",
            [field.name for field in dataclasses.fields(session_lifecycle.BoundReplacement)],
        )
        self.assertNotIn(
            "result",
            [field.name for field in dataclasses.fields(session_lifecycle.ContextReplacement)],
        )

    def test_case_f_the_route_has_nothing_to_invoke_work_with(self) -> None:
        parameters = list(
            inspect.signature(session_lifecycle.replace_old_context).parameters
        )
        for absent in ("send", "prompt", "markers", "command_timeout", "finalize_handoff"):
            self.assertNotIn(absent, parameters)
        source = inspect.getsource(session_lifecycle.replace_old_context)
        for token in ("run_request", "resume_request", "continue_session", "_observe_context"):
            self.assertEqual(source.count(token), 0, token)

    def test_case_f_a_caller_cannot_smuggle_a_sender_in(self) -> None:
        self._ready()
        with self.assertRaises(TypeError):
            self._replace(send=lambda *a, **k: None)
        with self.assertRaises(TypeError):
            self._replace(prompt="carry on")

    # -- G. the continue_session wiring obligation ----------------------------

    def _production_callers(self, name):
        """Every call to `name` inside the shipped package, by parsing it.

        Counted from the syntax tree rather than from a grep, so prose that names a
        function in a docstring or a comment is not mistaken for a call to it.
        """
        callers = {}
        for path in sorted(Path(session_lifecycle.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = 0
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                identifier = getattr(target, "id", None) or getattr(target, "attr", None)
                if identifier == name:
                    found += 1
            if found:
                callers[path.name] = found
        return callers

    def test_case_g_no_production_continue_session_caller_is_created_here(self) -> None:
        """Checkpoint 66 does not create the first one, so the D8 delivery
        obligation is not triggered and carries to the slice that does.

        `category-unprovable` is reachable only through degraded observation, and
        the sole degrader is `continue_session`'s failure path. While no production
        code calls it, no production supervised teardown can occur.
        """
        self.assertEqual(self._production_callers("continue_session"), {})

    def test_case_g_wiring_continue_session_obliges_wiring_the_supervised_route(self) -> None:
        """The obligation, stated as something the code must keep true.

        This passes today because neither caller exists. It will still pass when a
        later slice wires both. It fails only if a slice makes a real supervised
        teardown possible -- by giving `continue_session` a production caller --
        without giving the route that reports it one, which is the shape the
        checkpoint-65 review named as a genuine D8 failure.

        It constrains the *caller*, not the delivery: that `human_action` reaches a
        durable human-attention record is a property of the surface that slice
        builds, and this cannot see it.
        """
        continues = self._production_callers("continue_session")
        supervised = self._production_callers("supervised_teardown")
        if continues:
            self.assertTrue(
                supervised,
                "a production `continue_session` caller exists in {0}, so a real "
                "supervised teardown is now possible and `supervised_teardown` must "
                "have a production caller whose `human_action` reaches a durable D8 "
                "human-attention record.".format(sorted(continues)),
            )

    # -- H. checkpoints 59 to 65 intact ---------------------------------------

    def test_case_h_readiness_is_projected_in_call_and_never_handed_in(self) -> None:
        self._ready()
        for carried in ("readiness", "decision", "slots", "rail", "handoff",
                        "worktree", "record", "retirement", "ceiling", "observation"):
            with self.assertRaises(TypeError):
                self._replace(**{carried: object()})

    def test_case_h_the_retirement_authorization_still_has_one_producer(self) -> None:
        module = Path(session_lifecycle.__file__).read_text(encoding="utf-8")
        self.assertEqual(module.count("_RetirementAuthorization("), 1)
        source = inspect.getsource(session_lifecycle.replace_old_context)
        self.assertEqual(source.count("_RetirementAuthorization"), 0)
        self.assertEqual(source.count("_retirement"), 0)
        self.assertEqual(
            [
                name
                for name in inspect.signature(session_lifecycle.stop_session).parameters
                if name.startswith("_")
            ],
            ["_retirement"],
        )
        self.assertEqual(
            [
                name
                for name in inspect.signature(
                    session_lifecycle.replace_old_context
                ).parameters
                if name.startswith("_")
            ],
            [],
        )

    def test_case_h_the_chokepoint_refusal_still_stands_beneath_this_route(self) -> None:
        """A marked session still cannot be stopped except through the gate."""
        self._ready()
        with self.assertRaises(LifecycleError) as raised:
            stop_session(
                self.store, self.registry, self.store.read(SESSION),
                stop=self._stopper(), alive=self._alive,
            )
        self.assertEqual(
            raised.exception.reason,
            session_lifecycle.REASON_ROTATION_REQUIRES_RETIREMENT,
        )
        self._assert_predecessor_survives()

    def test_case_h_new_bindings_still_come_into_existence_in_exactly_one_place(self) -> None:
        """The extraction moved the launch ordering; it did not duplicate it."""
        tree = ast.parse(
            Path(session_lifecycle.__file__).read_text(encoding="utf-8")
        )
        reservers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "reserve_binding"
        ]
        self.assertEqual(len(reservers), 1)
        starters = inspect.getsource(session_lifecycle._reserve_and_bind)
        self.assertIn("observed_from_start=True", starters)
        self.assertEqual(
            inspect.getsource(session_lifecycle.launch_session).count(
                "observed_from_start"
            ),
            0,
        )

    def test_case_h_the_post_turn_terminal_finalization_still_credits_the_handoff(self) -> None:
        self._ready()
        self.assertEqual(self.published.value, PUBLICATION)
        finalization = self.registry.terminal_finalization(SESSION)
        self.assertIsNotNone(finalization)
        replacement = self._replace()
        self.assertEqual(
            replacement.retirement.readiness.handoff.handoff_publication, PUBLICATION
        )

    def test_case_h_an_ordinary_launch_is_unchanged_by_the_extraction(self) -> None:
        outcome, worker, sent = self._launch()
        self.assertEqual(outcome.binding.state, BINDING_STATE_BOUND)
        self.assertEqual(outcome.owned.session_id, SESSION)
        self.assertEqual(outcome.owned.pgid, worker.pgid)
        self.assertEqual([entry["session_id"] for entry in sent], [SESSION])
        self.assertEqual(outcome.result["session_id"], SESSION)
