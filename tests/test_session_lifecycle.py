from __future__ import annotations

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
    AuthorizationDecision,
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
SKILL = "executor"
TOOLS = ("Read", "Glob")


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


class RotationBoundaryTests(LifecycleTestBase):
    """marked -> safe boundary -> durable handoff -> rotation-ready, and nothing past it.

    Every case here projects. Nothing in this class terminates a context, stops a
    worker, launches or binds a replacement, or writes a durable record, and the
    last two cases prove that rather than assert it.
    """

    HANDOFF = "ai-dev/issue-55/rails/{0}/handoff.md".format(RAIL)

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
        arguments = {"rail": RAIL, "published": True, "location": self.HANDOFF}
        arguments.update(overrides)
        return RotationHandoffFacts(**arguments)

    def _worktree(self, **overrides):
        arguments = {
            "worktree_id": self.worktree_id, "path": str(self.workspace),
            "clean": True, "active_operation": None,
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

    # -- the transition itself --------------------------------------------------

    def test_marked_at_a_safe_boundary_with_a_durable_handoff_is_rotation_ready(self) -> None:
        self._launch()
        self._mark()
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_ROTATION_HANDOFF_ESTABLISHED)
        self.assertTrue(readiness.ready)
        self.assertEqual((readiness.observed, readiness.threshold), (6, 6))

    def test_the_handoff_carries_the_exact_durable_identity_and_nothing_else(self) -> None:
        self._launch()
        self._mark()
        handoff = self._evaluate().handoff
        record = self.store.read(SESSION)
        self.assertEqual(handoff.to_dict(), {
            "project": "ai-dev", "ticket": "issue-55", "rail": RAIL, "iteration": BLOB,
            "role": "executor", "workspaceKey": record.workspace_key,
            "worktreeId": self.worktree_id, "workspacePath": str(self.workspace),
            "launchedAtHead": HEAD, "handoff": self.HANDOFF, "sessionId": SESSION,
        })
        # Every value is the binding's own, so a replacement resolves the same rail
        # and the same workspace a fresh agent would.
        self.assertEqual(
            (handoff.project, handoff.ticket, handoff.rail, handoff.iteration_blob),
            (record.project, record.ticket, record.rail, record.iteration.blob),
        )

    def test_no_field_of_the_handoff_comes_from_a_transcript_or_the_provider(self) -> None:
        # The whole payload is reproducible from durable state alone: this rebuilds
        # it from the store and the control-plane locator, with no session held.
        self._launch()
        self._mark()
        expected = self._evaluate().handoff.to_dict()
        record = self.store.read(SESSION)
        self.assertEqual(expected, {
            "project": record.project, "ticket": record.ticket, "rail": record.rail,
            "iteration": record.iteration.blob, "role": record.role,
            "workspaceKey": record.workspace_key, "worktreeId": record.worktree_id,
            "workspacePath": record.workspace_path,
            "launchedAtHead": record.launched_at_head,
            "handoff": self.HANDOFF, "sessionId": record.session_id,
        })

    # -- the safe boundary ------------------------------------------------------

    def test_marked_but_in_flight_is_not_rotation_ready(self) -> None:
        self._launch()
        self._mark()
        self.registry.begin_invocation(SESSION)
        readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_NOT_READY)
        self.assertEqual(readiness.reason, session_lifecycle.REASON_INVOCATION_IN_FLIGHT)
        self.assertIsNone(readiness.handoff)
        # And the boundary opens again the moment that invocation ends, with no
        # timer and nothing else to wait for.
        self.registry.end_invocation(SESSION)
        self.assertEqual(self._evaluate().state, ROTATION_READY)

    def test_a_failed_invocation_still_closes_the_boundary_it_opened(self) -> None:
        # The failure path clears in-flight in its `finally`, so a marked session
        # that suffered a failed turn is still reachable as a boundary.
        self._launch()
        self._mark()
        failure = claude_worker.ClaudeWorkerError(claude_worker.REASON_WORKER_FATAL, "boom")
        with self.assertRaises(claude_worker.ClaudeWorkerError):
            continue_session(
                self._decision(action=ACTION_CONTINUE), self.assignment, store=self.store,
                registry=self.registry, session_id=SESSION,
                request_kwargs=self._request_kwargs(), prompt="carry on",
                send=self._sender(fail=failure)[0], alive=lambda pgid: True,
            )
        self.assertEqual(self.registry.in_flight(), ())
        readiness = self._evaluate()
        # Still marked -- the floor proved the threshold -- and still ready, even
        # though the history is now partial.
        self.assertEqual(readiness.state, ROTATION_READY)
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
        before = (
            self.store.read(SESSION).to_dict(),
            self.registry.context_readings(),
            self.registry.in_flight(),
            sorted(owned.session_id for owned in self.registry.sessions()),
        )
        self.assertEqual(self._evaluate().state, ROTATION_READY)
        after = (
            self.store.read(SESSION).to_dict(),
            self.registry.context_readings(),
            self.registry.in_flight(),
            sorted(owned.session_id for owned in self.registry.sessions()),
        )
        self.assertEqual(before, after)
        # The worker was never asked to stop, and the session is still continuable.
        self.assertIsNotNone(self.registry.get(SESSION))
        self.assertEqual(self.store.read(SESSION).state, BINDING_STATE_BOUND)

    def test_rotation_readiness_creates_no_human_attention_and_no_second_session(self) -> None:
        self._launch()
        self._mark()
        with patch.object(session_lifecycle, "start_worker") as starter, \
                patch.object(session_lifecycle, "shutdown_worker") as stopper:
            readiness = self._evaluate()
        self.assertEqual(readiness.state, ROTATION_READY)
        starter.assert_not_called()
        stopper.assert_not_called()
        # Rotation readiness is system-owned: the projection carries a machine
        # reason and a locator, and nothing shaped like a human decision.
        self.assertFalse(hasattr(readiness, "human_decision"))


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


if __name__ == "__main__":
    unittest.main()
