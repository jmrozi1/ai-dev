from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from ai_dev_flow import claude_runtime, claude_worker, workspaces
from ai_dev_flow.claude_worker import (
    FORBIDDEN_ENVIRONMENT_NAMES,
    PRESERVED_ENVIRONMENT_NAMES,
    PROTOCOL_VERSION,
    ClaudeWorkerError,
    WorkerHandle,
    build_worker_environment,
    inspect_credential_selectors,
    process_group_alive,
    require_worker_sdk,
    run_request,
    shutdown_worker,
    spawn_worker,
    start_worker,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BindingStore,
    RailIteration,
    SessionBindingError,
    reserve_binding,
    unbind_session,
)
from ai_dev_flow.tickets import TicketReference


RAIL = "issue-55-agent-sdk-worker-integration"
BLOB = "a" * 40
OTHER_BLOB = "b" * 40
HEAD = "c" * 40
SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"
SKILL = "executor"
TOOLS = ("Read", "Glob", "Grep")
REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkerTestBase(unittest.TestCase):
    """Real worktrees, real claims, real child processes. Ownership is not a mock."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()
        self.repo_root = self._init_repo("product")

        self.reference = TicketReference(
            provider="github", ticket_id="55", repository="jmrozi1/ai-dev"
        )
        self.workspace, self.worktree_id = self._add_workspace("workspace-55", self.reference)

        self.controller_root = self.tmp_path / "controller"
        self.prompt_file = self.controller_root / "prompts" / "executor.md"
        self.prompt_file.parent.mkdir(parents=True)
        self.prompt_file.write_text("You are the AI Dev executor.\n", encoding="utf-8")
        self.plugin_root = self.controller_root / "plugins" / "ai-dev-executor"
        (self.plugin_root / "skills" / SKILL).mkdir(parents=True)
        (self.plugin_root / "skills" / SKILL / "SKILL.md").write_text(
            "---\nname: executor\n---\n\nBounded executor.\n", encoding="utf-8"
        )
        (self.plugin_root / ".claude-plugin").mkdir(parents=True)
        (self.plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "ai-dev-executor"}) + "\n", encoding="utf-8"
        )

        self.store = BindingStore(self.tmp_path / "controller-state")
        self.iteration = RailIteration(rail=RAIL, blob=BLOB)
        self.handles = []

    def tearDown(self) -> None:
        for handle in self.handles:
            try:
                shutdown_worker(handle, timeout=3.0)
            except Exception:
                claude_worker._terminate_group(handle.pgid)
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
        self._git(repo_root, "config", "user.name", "Worker Tests")
        self._git(repo_root, "config", "user.email", "worker-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo_root, "add", "tracked.txt")
        self._git(repo_root, "commit", "-q", "-m", "initial commit")
        self._git(repo_root, "branch", "-M", "main")
        return repo_root

    def _add_workspace(self, name: str, reference: TicketReference):
        path = self.tmp_path / name
        branch = "flow/{0}".format(name)
        self._git(self.repo_root, "worktree", "add", "-q", "-b", branch, str(path), "main")
        worktree_id = workspaces.effective_worktree_id(path)
        workspaces.create_active_claim(
            path, reference=reference, worktree_id=worktree_id,
            workspace_path=path, branch=branch,
        )
        return path, worktree_id

    def _reserve(self, **overrides):
        arguments = {
            "project": "ai-dev", "ticket": "issue-55", "reference": self.reference,
            "workspace_path": self.workspace, "worktree_id": self.worktree_id,
            "rail": RAIL, "role": "executor", "iteration": self.iteration,
            "session_id": SESSION, "launched_at_head": HEAD,
            "reserved_at": "2026-08-26T12:00:00Z",
        }
        arguments.update(overrides)
        return reserve_binding(self.store, **arguments)

    def _request_kwargs(self, **overrides):
        arguments = {
            "controller_root": self.controller_root,
            "prompt_file": self.prompt_file,
            "plugin_root": self.plugin_root,
            "expected_skill": SKILL,
            "allowed_tools": TOOLS,
            "max_turns": 2,
            "max_budget_usd": 0.5,
        }
        arguments.update(overrides)
        return arguments

    def _clean_env(self, **extra) -> dict:
        base = {"HOME": str(self.tmp_path / "home"), "PATH": os.environ.get("PATH", "")}
        base.update(extra)
        return base

    def _start(self, record=None, **overrides):
        handle, bound = start_worker(
            self.store,
            record if record is not None else self.store.read(SESSION),
            expected_iteration=overrides.pop("expected_iteration", self.iteration),
            package_root=overrides.pop("package_root", REPO_ROOT),
            environment_source=overrides.pop("environment_source", self._clean_env()),
            **overrides,
        )
        self.handles.append(handle)
        return handle, bound


class ChildEnvironmentTests(WorkerTestBase):
    def test_the_child_environment_is_constructed_from_an_allowlist(self) -> None:
        source = dict(self._clean_env(), LANG="C.UTF-8", SECRET_THING="x", EDITOR="vim")
        environment = build_worker_environment(source, package_root=REPO_ROOT)
        self.assertEqual(environment["HOME"], source["HOME"])
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertEqual(environment["PYTHONPATH"], str(REPO_ROOT))
        self.assertNotIn("SECRET_THING", environment)
        self.assertNotIn("EDITOR", environment)
        self.assertTrue(set(environment) <= set(PRESERVED_ENVIRONMENT_NAMES) | {"PYTHONPATH"})

    def test_every_credential_or_provider_selector_fails_closed(self) -> None:
        for name in FORBIDDEN_ENVIRONMENT_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(ClaudeWorkerError) as caught:
                    build_worker_environment(
                        self._clean_env(**{name: "value-never-read"}), package_root=REPO_ROOT
                    )
                self.assertEqual(
                    caught.exception.reason, claude_worker.REASON_SELECTOR_PRESENT
                )
                self.assertIn(name, caught.exception.detail)
                self.assertNotIn("value-never-read", caught.exception.detail)

    def test_an_empty_selector_still_counts_as_present(self) -> None:
        # Presence is the signal; an empty ANTHROPIC_API_KEY still shadows the
        # subscription profile, so reading the value would be the wrong test.
        with self.assertRaises(ClaudeWorkerError):
            build_worker_environment(
                self._clean_env(ANTHROPIC_API_KEY=""), package_root=REPO_ROOT
            )

    def test_selectors_cannot_be_added_back_through_extras(self) -> None:
        with self.assertRaises(ClaudeWorkerError) as caught:
            build_worker_environment(
                self._clean_env(), package_root=REPO_ROOT,
                extra={"ANTHROPIC_API_KEY": "sneaky"},
            )
        self.assertEqual(caught.exception.reason, claude_worker.REASON_SELECTOR_PRESENT)

    def test_inspect_reports_names_only(self) -> None:
        found = inspect_credential_selectors(
            {"ANTHROPIC_API_KEY": "secret", "CLAUDE_CODE_USE_VERTEX": "1", "PATH": "/bin"}
        )
        self.assertEqual(found, ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_VERTEX"))

    def test_the_home_based_subscription_route_is_preserved(self) -> None:
        self.assertIn("HOME", PRESERVED_ENVIRONMENT_NAMES)

    def test_the_sdk_env_overlay_is_downstream_of_this_environment(self) -> None:
        # The overlay merges with whatever the worker already has, so isolation has
        # to happen here, before the process exists.
        self._reserve()
        request = claude_worker.build_launch_request(
            self.store.read(SESSION), **self._request_kwargs()
        )
        self.assertEqual(claude_runtime.build_option_fields(request)["env"], {})
        environment = build_worker_environment(
            self._clean_env(), package_root=REPO_ROOT
        )
        self.assertEqual(inspect_credential_selectors(environment), ())


class OwnedProcessTests(WorkerTestBase):
    def test_the_worker_starts_without_a_shell_in_its_own_process_group(self) -> None:
        self._reserve()
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as spied:
            handle, bound = self._start()
        arguments, keywords = spied.call_args
        self.assertIs(keywords["shell"], False)
        self.assertIs(keywords["start_new_session"], True)
        self.assertEqual(arguments[0][1:], ["-m", claude_worker.WORKER_MODULE])

        self.assertEqual(handle.pgid, handle.pid)
        self.assertNotEqual(handle.pgid, os.getpgrp())
        self.assertTrue(process_group_alive(handle.pgid))
        self.assertEqual(bound.pid, handle.pid)

    def test_the_worker_inherits_only_the_constructed_environment(self) -> None:
        self._reserve()
        with patch.object(subprocess, "Popen", wraps=subprocess.Popen) as spied:
            self._start(environment_source=self._clean_env(SECRET_THING="x"))
        passed = spied.call_args[1]["env"]
        self.assertNotIn("SECRET_THING", passed)
        self.assertEqual(inspect_credential_selectors(passed), ())

    def test_a_selector_in_the_controller_environment_blocks_the_launch(self) -> None:
        record = self._reserve()
        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(environment_source=self._clean_env(ANTHROPIC_API_KEY="k"))
        self.assertEqual(caught.exception.reason, claude_worker.REASON_SELECTOR_PRESENT)
        self.assertEqual(self.store.read(SESSION), record)

    def test_shutdown_is_bounded_and_the_process_group_is_gone(self) -> None:
        self._reserve()
        handle, _ = self._start()
        report = shutdown_worker(handle, timeout=10.0)
        self.handles.remove(handle)
        self.assertTrue(report["graceful"])
        self.assertTrue(report["process_group_gone"])
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(process_group_alive(handle.pgid))

    def test_shutdown_of_an_unresponsive_worker_still_ends_the_group(self) -> None:
        self._reserve()
        handle, _ = self._start()
        # Simulate a worker that has stopped answering its protocol.
        handle.process.stdout.close()
        report = shutdown_worker(handle, timeout=2.0)
        self.handles.remove(handle)
        self.assertFalse(report["graceful"])
        self.assertTrue(report["process_group_gone"])
        self.assertFalse(process_group_alive(handle.pgid))


    def test_shutdown_refuses_to_report_success_while_the_group_survives(self) -> None:
        # Without this, "shutdown" could mean "asked nicely and stopped looking".
        self._reserve()
        handle, _ = self._start()
        try:
            with patch.object(claude_worker, "_terminate_group", lambda pgid: None), \
                    patch.object(claude_worker, "process_group_alive", lambda pgid: True):
                with self.assertRaises(ClaudeWorkerError) as caught:
                    shutdown_worker(handle, timeout=2.0)
            self.assertEqual(
                caught.exception.reason, claude_worker.REASON_SHUTDOWN_INCOMPLETE
            )
            self.assertIn(str(handle.pgid), caught.exception.detail)
        finally:
            claude_worker._terminate_group(handle.pgid)
        self.handles.remove(handle)
        self.assertFalse(process_group_alive(handle.pgid))


class ReservationAttachmentTests(WorkerTestBase):
    def test_readiness_precedes_attachment_and_records_the_real_identity(self) -> None:
        reserved = self._reserve()
        self.assertEqual(reserved.state, BINDING_STATE_RESERVED)
        self.assertIsNone(reserved.pid)

        handle, bound = self._start()
        self.assertEqual(bound.state, BINDING_STATE_BOUND)
        self.assertEqual(bound.pid, handle.pid)
        self.assertEqual(bound.pid_domain, claude_worker.current_pid_domain())
        self.assertEqual(bound.started_at, handle.started_at)
        self.assertEqual(bound.iteration, self.iteration)
        self.assertEqual(self.store.read(SESSION), bound)

    def test_a_failed_spawn_leaves_the_reservation_exactly_as_it_was(self) -> None:
        reserved = self._reserve()

        def failing(**_kwargs):
            raise ClaudeWorkerError(claude_worker.REASON_SPAWN_FAILED, "no such executable")

        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(spawn=failing)
        self.assertEqual(caught.exception.reason, claude_worker.REASON_SPAWN_FAILED)
        self.assertEqual(self.store.read(SESSION), reserved)
        self.assertIsNone(self.store.read(SESSION).pid)

    def test_a_failed_readiness_handshake_leaves_the_reservation_and_kills_the_group(self) -> None:
        reserved = self._reserve()
        pgids = []

        def silent(**kwargs):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(kwargs["environment"]), cwd=str(kwargs["cwd"]), text=True,
                shell=False, start_new_session=True,
            )
            pgids.append(os.getpgid(process.pid))
            return process

        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(spawn=silent, ready_timeout=1.0)
        self.assertEqual(caught.exception.reason, claude_worker.REASON_COMMAND_TIMEOUT)
        self.assertEqual(self.store.read(SESSION), reserved)
        self.assertFalse(process_group_alive(pgids[0]))

    def test_attachment_happens_exactly_once(self) -> None:
        self._reserve()
        handle, bound = self._start()
        with self.assertRaises(SessionBindingError) as caught:
            claude_worker.attach_process(
                self.store, SESSION, pid=handle.pid,
                pid_domain=claude_worker.current_pid_domain(),
                started_at=handle.started_at, bound_at="2026-08-26T12:00:09Z",
            )
        self.assertEqual(caught.exception.reason, "not-reserved")
        self.assertEqual(self.store.read(SESSION), bound)

    def test_a_second_worker_for_one_binding_is_refused(self) -> None:
        self._reserve()
        first, bound = self._start()
        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(record=bound)
        self.assertEqual(
            caught.exception.reason, claude_worker.REASON_BINDING_NOT_RESERVED
        )
        self.assertEqual(self.store.read(SESSION), bound)

    def test_a_mismatched_iteration_is_refused_before_any_spawn(self) -> None:
        reserved = self._reserve()
        spawned = []
        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(
                expected_iteration=RailIteration(rail=RAIL, blob=OTHER_BLOB),
                spawn=lambda **k: spawned.append(k),
            )
        self.assertEqual(caught.exception.reason, claude_worker.REASON_ITERATION_MISMATCH)
        self.assertEqual(spawned, [])
        self.assertEqual(self.store.read(SESSION), reserved)

    def test_a_terminal_binding_cannot_start_a_worker(self) -> None:
        self._reserve()
        unbind_session(self.store, SESSION)
        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(record=self.store.read(SESSION))
        self.assertEqual(
            caught.exception.reason, claude_worker.REASON_BINDING_NOT_RESERVED
        )

    def test_an_unknown_binding_cannot_start_a_worker(self) -> None:
        reserved = self._reserve()
        stranger = claude_worker.BindingRecord(
            **dict(reserved.__dict__, session_id=OTHER_SESSION)
        )
        with self.assertRaises(ClaudeWorkerError) as caught:
            self._start(record=stranger)
        self.assertEqual(
            caught.exception.reason, claude_worker.REASON_BINDING_NOT_RESERVED
        )


class ProtocolTests(WorkerTestBase):
    def test_the_sdk_verdict_is_surfaced_before_any_command(self) -> None:
        self._reserve()
        handle, _ = self._start()
        if handle.sdk_available:
            self.assertEqual(require_worker_sdk(handle), handle.sdk_version)
        else:
            with self.assertRaises(ClaudeWorkerError) as caught:
                require_worker_sdk(handle)
            self.assertEqual(
                caught.exception.reason, claude_worker.REASON_SDK_UNAVAILABLE
            )

    def test_a_command_without_a_usable_sdk_returns_a_compact_error(self) -> None:
        self._reserve()
        handle, bound = self._start()
        if handle.sdk_available:
            self.skipTest("the SDK is installed; the absent-SDK path cannot be observed")
        request = claude_worker.build_resume_request(bound, **self._request_kwargs())
        with self.assertRaises(ClaudeWorkerError) as caught:
            run_request(handle, request, prompt="ping", timeout=20.0)
        self.assertEqual(caught.exception.reason, claude_worker.REASON_WORKER_FATAL)
        self.assertIn("sdk", caught.exception.detail.lower())

    def test_the_launch_command_carries_the_accepted_option_fields(self) -> None:
        self._reserve()
        record = self.store.read(SESSION)
        request = claude_worker.build_launch_request(record, **self._request_kwargs())
        sent = []
        handle = WorkerHandle(
            process=types.SimpleNamespace(
                stdin=types.SimpleNamespace(
                    write=lambda text: sent.append(json.loads(text)), flush=lambda: None
                ),
                stdout=None, poll=lambda: None,
            ),
            pid=1, pgid=1, started_at="2026-08-26T12:00:01Z",
            sdk_version="0.2.140", sdk_detail=None,
        )
        with patch.object(claude_worker, "_read_message", return_value={
            "type": "result", "session_id": SESSION, "mode": "launch",
            "subtype": "success", "is_error": False, "markers": {},
        }):
            run_request(handle, request, prompt="do the work", markers=["m1"])
        command = sent[0]
        self.assertEqual(command["type"], "launch")
        self.assertEqual(command["session_id"], SESSION)
        self.assertEqual(command["markers"], ["m1"])
        options = command["options"]
        self.assertEqual(options["setting_sources"], [])
        self.assertIs(options["strict_mcp_config"], True)
        self.assertEqual(options["permission_mode"], "dontAsk")
        self.assertEqual(options["session_id"], SESSION)
        self.assertIsNone(options["resume"])
        self.assertIs(options["continue_conversation"], False)

    def test_the_resume_command_uses_exact_resume_and_never_newest(self) -> None:
        self._reserve()
        handle, bound = self._start()
        request = claude_worker.build_resume_request(bound, **self._request_kwargs())
        options = claude_runtime.build_option_fields(request)
        self.assertEqual(options["resume"], SESSION)
        self.assertIsNone(options["session_id"])
        self.assertIs(options["continue_conversation"], False)
        self.assertIs(options["fork_session"], False)

    def test_option_invariants_are_re_asserted_inside_the_worker(self) -> None:
        base = {
            "setting_sources": [], "strict_mcp_config": True, "permission_mode": "dontAsk",
            "continue_conversation": False, "fork_session": False, "mcp_servers": {},
            "session_id": SESSION, "resume": None,
        }
        self.assertEqual(claude_worker._check_option_invariants(dict(base)), dict(base))
        for name, bad in (
            ("setting_sources", ["user"]),
            ("strict_mcp_config", False),
            ("permission_mode", "bypassPermissions"),
            ("continue_conversation", True),
            ("fork_session", True),
            ("mcp_servers", {"x": {}}),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ClaudeWorkerError) as caught:
                    claude_worker._check_option_invariants(dict(base, **{name: bad}))
                self.assertEqual(
                    caught.exception.reason, claude_worker.REASON_PROTOCOL_VIOLATION
                )

    def test_a_command_setting_both_launch_and_resume_is_refused(self) -> None:
        base = {
            "setting_sources": [], "strict_mcp_config": True, "permission_mode": "dontAsk",
            "continue_conversation": False, "fork_session": False, "mcp_servers": {},
        }
        for session_id, resume in ((SESSION, SESSION), (None, None)):
            with self.subTest(session_id=session_id, resume=resume):
                with self.assertRaises(ClaudeWorkerError) as caught:
                    claude_worker._check_option_invariants(
                        dict(base, session_id=session_id, resume=resume)
                    )
                self.assertEqual(
                    caught.exception.reason, claude_worker.REASON_PROTOCOL_VIOLATION
                )

    def test_a_result_for_another_session_is_refused(self) -> None:
        self._reserve()
        record = self.store.read(SESSION)
        request = claude_worker.build_launch_request(record, **self._request_kwargs())
        handle = WorkerHandle(
            process=types.SimpleNamespace(
                stdin=types.SimpleNamespace(write=lambda text: None, flush=lambda: None),
                stdout=None, poll=lambda: None,
            ),
            pid=1, pgid=1, started_at="2026-08-26T12:00:01Z",
            sdk_version="0.2.140", sdk_detail=None,
        )
        with patch.object(claude_worker, "_read_message", return_value={
            "type": "result", "session_id": OTHER_SESSION, "subtype": "success",
        }):
            with self.assertRaises(ClaudeWorkerError) as caught:
                run_request(handle, request, prompt="x")
        self.assertEqual(
            caught.exception.reason, claude_worker.REASON_PROTOCOL_VIOLATION
        )


class ProviderReductionTests(WorkerTestBase):
    """Drive the worker's provider path against an injected fake SDK."""

    def _install_fake_sdk(self, *, result, texts=()):
        module = types.ModuleType("claude_agent_sdk")

        class FakeOptions(object):
            def __init__(self, **fields):
                self.fields = fields

        class Block(object):
            def __init__(self, text):
                self.text = text

        class Assistant(object):
            def __init__(self, texts):
                self.content = [Block(t) for t in texts]

        class ResultMessage(object):
            def __init__(self, payload):
                for key, value in payload.items():
                    setattr(self, key, value)
                self.content = []

        def query(prompt, options):
            async def generator():
                yield Assistant(texts)
                yield ResultMessage(result)

            return generator()

        module.ClaudeAgentOptions = FakeOptions
        module.query = query
        patcher = patch.dict(sys.modules, {"claude_agent_sdk": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return module

    def _command(self, **overrides):
        options = {
            "setting_sources": [], "strict_mcp_config": True, "permission_mode": "dontAsk",
            "continue_conversation": False, "fork_session": False, "mcp_servers": {},
            "session_id": SESSION, "resume": None,
        }
        command = {
            "type": "launch", "mode": "launch", "session_id": SESSION,
            "prompt": "hello", "markers": [], "options": options,
        }
        command.update(overrides)
        return command

    def test_provider_output_is_reduced_to_facts_and_marker_booleans(self) -> None:
        self._install_fake_sdk(
            result={
                "session_id": SESSION, "subtype": "success", "is_error": False,
                "num_turns": 3, "total_cost_usd": 0.01,
                "result": "I read the prompt marker MARKER-P and the skill marker MARKER-S.",
            },
            texts=["thinking out loud about MARKER-P"],
        )
        command = self._command(markers=["MARKER-P", "MARKER-S", "MARKER-ABSENT"])
        payload = claude_worker._result_payload(
            command, claude_worker._run_provider(command)
        )
        self.assertEqual(payload["session_id"], SESSION)
        self.assertEqual(payload["subtype"], "success")
        self.assertFalse(payload["is_error"])
        self.assertEqual(payload["num_turns"], 3)
        self.assertEqual(
            payload["markers"],
            {"MARKER-P": True, "MARKER-S": True, "MARKER-ABSENT": False},
        )

    def test_no_assistant_text_or_transcript_crosses_the_protocol(self) -> None:
        secret = "assistant prose that must not travel"
        self._install_fake_sdk(
            result={
                "session_id": SESSION, "subtype": "success", "is_error": False,
                "result": secret, "transcript_path": "/tmp/session.jsonl",
                "usage": {"input_tokens": 99},
            },
            texts=[secret],
        )
        command = self._command(markers=["MARKER-P"])
        payload = claude_worker._result_payload(
            command, claude_worker._run_provider(command)
        )
        rendered = json.dumps(payload, sort_keys=True)
        for leaked in (secret, "transcript", "usage", "input_tokens", "prose"):
            self.assertNotIn(leaked, rendered)

    def test_a_provider_session_mismatch_is_refused_inside_the_worker(self) -> None:
        self._install_fake_sdk(
            result={"session_id": OTHER_SESSION, "subtype": "success", "is_error": False}
        )
        command = self._command()
        with self.assertRaises(claude_runtime.ClaudeRuntimeError) as caught:
            claude_worker._result_payload(command, claude_worker._run_provider(command))
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_RESULT_SESSION_MISMATCH
        )

    def test_a_provider_run_with_no_result_message_fails_closed(self) -> None:
        module = self._install_fake_sdk(result={})

        def empty(prompt, options):
            async def generator():
                if False:
                    yield None

            return generator()

        module.query = empty
        with self.assertRaises(ClaudeWorkerError) as caught:
            claude_worker._run_provider(self._command())
        self.assertEqual(caught.exception.reason, claude_worker.REASON_WORKER_FATAL)

    def test_the_worker_refuses_an_option_set_that_would_load_ambient_sources(self) -> None:
        self._install_fake_sdk(
            result={"session_id": SESSION, "subtype": "success", "is_error": False}
        )
        command = self._command()
        command["options"] = dict(command["options"], setting_sources=["user", "project"])
        with self.assertRaises(ClaudeWorkerError) as caught:
            claude_worker._run_provider(command)
        self.assertEqual(
            caught.exception.reason, claude_worker.REASON_PROTOCOL_VIOLATION
        )


class WorkerEntryPointTests(WorkerTestBase):
    def test_the_worker_announces_its_own_identity_and_sdk_verdict(self) -> None:
        environment = build_worker_environment(self._clean_env(), package_root=REPO_ROOT)
        process = spawn_worker(environment=environment, cwd=self.workspace)
        try:
            message = claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            self.assertEqual(message["type"], "ready")
            self.assertEqual(message["protocol"], PROTOCOL_VERSION)
            self.assertEqual(message["pid"], process.pid)
            self.assertEqual(message["pgid"], process.pid)
            self.assertTrue(message["started_at"].endswith("Z"))
        finally:
            claude_worker._terminate_group(os.getpgid(process.pid))
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def test_an_unsupported_command_is_reported_without_killing_the_worker(self) -> None:
        environment = build_worker_environment(self._clean_env(), package_root=REPO_ROOT)
        process = spawn_worker(environment=environment, cwd=self.workspace)
        try:
            claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            process.stdin.write(json.dumps({"type": "stop-everything"}) + "\n")
            process.stdin.flush()
            error = claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            self.assertEqual(error["type"], "error")
            self.assertEqual(error["reason"], claude_worker.REASON_PROTOCOL_VIOLATION)

            process.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
            process.stdin.flush()
            stopped = claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            self.assertEqual(stopped["type"], "stopped")
        finally:
            claude_worker._terminate_group(os.getpgid(process.pid))
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

    def test_a_non_json_line_is_reported_rather_than_crashing(self) -> None:
        environment = build_worker_environment(self._clean_env(), package_root=REPO_ROOT)
        process = spawn_worker(environment=environment, cwd=self.workspace)
        try:
            claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            process.stdin.write("not json at all\n")
            process.stdin.flush()
            error = claude_worker._read_message(
                process.stdout, deadline=time.monotonic() + 20, process=process
            )
            self.assertEqual(error["reason"], claude_worker.REASON_PROTOCOL_VIOLATION)
        finally:
            claude_worker._terminate_group(os.getpgid(process.pid))
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
