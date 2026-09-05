from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow import claude_runtime
from ai_dev_flow.claude_runtime import (
    ALLOWED_MANIFEST_KEYS,
    ASSET_DIRECTORY,
    ASSET_FILE,
    MINIMUM_SDK_VERSION,
    MODE_LAUNCH,
    MODE_RESUME,
    ClaudeRuntimeError,
    RuntimeRequest,
    build_option_fields,
    interpret_result,
    launch_request,
    parse_version,
    require_supported_sdk,
    resume_request,
    validate_controller_asset,
    validate_plugin_surface,
)
from ai_dev_flow.session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BINDING_STATE_UNBOUND,
    BindingRecord,
    RailIteration,
)
from tests.source_oracles import call_locations, call_sites


RAIL = "issue-55-agent-sdk-isolation-contract"
BLOB = "a" * 40
HEAD = "c" * 40
SESSION = "1a2b3c4d-0001-4000-8000-00000000000a"
OTHER_SESSION = "1a2b3c4d-0002-4000-8000-00000000000b"
SKILL = "executor"
TOOLS = ("Read", "Glob", "Grep", "Bash(python -m unittest:*)")


class RuntimeTestBase(unittest.TestCase):
    """Real directories: provenance and plugin-surface rules are filesystem rules."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name).resolve()

        self.workspace = self.tmp_path / "product" / "ai-dev-issue-55"
        self.workspace.mkdir(parents=True)

        self.controller_root = self.tmp_path / "controller"
        self.prompt_file = self.controller_root / "prompts" / "executor.md"
        self.prompt_file.parent.mkdir(parents=True)
        self.prompt_file.write_text("You are the AI Dev executor.\n", encoding="utf-8")

        self.plugin_root = self.controller_root / "plugins" / "ai-dev-executor"
        self._write_plugin(self.plugin_root, skill=SKILL)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_plugin(self, root: Path, *, skill: str, manifest: dict = None) -> Path:
        (root / "skills" / skill).mkdir(parents=True, exist_ok=True)
        (root / "skills" / skill / "SKILL.md").write_text(
            "---\nname: {0}\n---\n\nBounded executor.\n".format(skill), encoding="utf-8"
        )
        payload = {"name": "ai-dev-executor"} if manifest is None else manifest
        manifest_directory = root / ".claude-plugin"
        manifest_directory.mkdir(parents=True, exist_ok=True)
        (manifest_directory / "plugin.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        return root

    def _record(self, **overrides: object) -> BindingRecord:
        arguments: dict = {
            "project": "ai-dev",
            "ticket": "issue-55",
            "workspace_key": "github:jmrozi1/ai-dev#55",
            "worktree_id": "ai-dev-issue-55",
            "workspace_path": str(self.workspace),
            "rail": RAIL,
            "role": "executor",
            "iteration": RailIteration(rail=RAIL, blob=BLOB),
            "session_id": SESSION,
            "launched_at_head": HEAD,
            "reserved_at": "2026-08-26T12:00:00Z",
            "state": BINDING_STATE_RESERVED,
        }
        arguments.update(overrides)
        return BindingRecord(**arguments)  # type: ignore[arg-type]

    def _bound(self, **overrides: object) -> BindingRecord:
        arguments: dict = {
            "state": BINDING_STATE_BOUND,
            "pid": 4242,
            "pid_domain": "test-host",
            "started_at": "2026-08-26T12:00:02Z",
            "bound_at": "2026-08-26T12:00:03Z",
        }
        arguments.update(overrides)
        return self._record(**arguments)

    def _kwargs(self, **overrides: object) -> dict:
        arguments: dict = {
            "controller_root": self.controller_root,
            "prompt_file": self.prompt_file,
            "plugin_root": self.plugin_root,
            "expected_skill": SKILL,
            "allowed_tools": TOOLS,
            "max_turns": 40,
            "max_budget_usd": 5.0,
        }
        arguments.update(overrides)
        return arguments

    def _launch(self, **overrides: object) -> RuntimeRequest:
        return launch_request(self._record(), **self._kwargs(**overrides))

    def _resume(self, **overrides: object) -> RuntimeRequest:
        return resume_request(self._bound(), **self._kwargs(**overrides))


class OptionContractTests(RuntimeTestBase):
    def test_launch_options_close_every_ambient_source(self) -> None:
        fields = build_option_fields(self._launch())

        self.assertEqual(fields["cwd"], str(self.workspace))
        self.assertEqual(fields["setting_sources"], [])
        self.assertEqual(
            fields["system_prompt"], {"type": "file", "path": str(self.prompt_file)}
        )
        self.assertEqual(
            fields["plugins"], [{"type": "local", "path": str(self.plugin_root)}]
        )
        self.assertEqual(fields["mcp_servers"], {})
        self.assertIs(fields["strict_mcp_config"], True)
        self.assertEqual(fields["permission_mode"], "dontAsk")
        self.assertEqual(fields["allowed_tools"], list(TOOLS))
        self.assertEqual(fields["disallowed_tools"], [])
        self.assertEqual(fields["add_dirs"], [])
        self.assertEqual(fields["env"], {})
        self.assertEqual(fields["extra_args"], {})
        self.assertIsNone(fields["hooks"])
        self.assertIsNone(fields["agents"])
        self.assertIsNone(fields["fallback_model"])
        self.assertEqual(fields["max_turns"], 40)
        self.assertEqual(fields["max_budget_usd"], 5.0)

    def test_launch_supplies_session_id_and_no_resume_or_continue_fallback(self) -> None:
        fields = build_option_fields(self._launch())
        self.assertEqual(fields["session_id"], SESSION)
        self.assertIsNone(fields["resume"])
        self.assertIs(fields["continue_conversation"], False)
        self.assertIs(fields["fork_session"], False)

    def test_resume_supplies_only_the_exact_session_and_never_newest(self) -> None:
        fields = build_option_fields(self._resume())
        self.assertEqual(fields["resume"], SESSION)
        self.assertIsNone(fields["session_id"])
        self.assertIs(fields["continue_conversation"], False)
        self.assertIs(fields["fork_session"], False)

    def test_ambient_setting_sources_can_never_appear(self) -> None:
        for request in (self._launch(), self._resume()):
            with self.subTest(mode=request.mode):
                fields = build_option_fields(request)
                self.assertEqual(fields["setting_sources"], [])
                for source in ("user", "project", "local"):
                    self.assertNotIn(source, fields["setting_sources"])

    def test_no_settings_file_or_permission_array_leaks_through_extra_args(self) -> None:
        fields = build_option_fields(self._launch())
        self.assertEqual(fields["extra_args"], {})
        rendered = json.dumps(fields, sort_keys=True)
        for smuggled in ("--settings", "settings.json", "bypassPermissions", "--bare"):
            self.assertNotIn(smuggled, rendered)

    def test_mcp_configuration_is_strict_and_empty(self) -> None:
        fields = build_option_fields(self._launch())
        self.assertIs(fields["strict_mcp_config"], True)
        self.assertEqual(fields["mcp_servers"], {})

    def test_exactly_one_plugin_is_supplied(self) -> None:
        fields = build_option_fields(self._launch())
        self.assertEqual(len(fields["plugins"]), 1)
        self.assertEqual(fields["plugins"][0]["type"], "local")

    def test_cli_path_is_emitted_only_when_supplied(self) -> None:
        self.assertNotIn("cli_path", build_option_fields(self._launch()))
        with_path = self._launch(cli_path="/opt/claude/bin/claude")
        self.assertEqual(
            build_option_fields(with_path)["cli_path"], "/opt/claude/bin/claude"
        )

    def test_an_unknown_mode_is_refused(self) -> None:
        request = self._launch()
        forged = RuntimeRequest(**{**request.__dict__, "mode": "continue"})
        with self.assertRaises(ClaudeRuntimeError) as caught:
            build_option_fields(forged)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_INVALID_MODE)

    def test_the_request_is_immutable(self) -> None:
        request = self._launch()
        with self.assertRaises(Exception):
            request.mode = MODE_RESUME  # type: ignore[misc]


class IdentityTests(RuntimeTestBase):
    def test_launch_requires_a_reserved_binding(self) -> None:
        for state in (BINDING_STATE_BOUND, BINDING_STATE_UNBOUND):
            with self.subTest(state=state):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    launch_request(self._bound(state=state), **self._kwargs())
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_BINDING_NOT_RESERVED
                )

    def test_resume_requires_a_bound_binding(self) -> None:
        for state in (BINDING_STATE_RESERVED, BINDING_STATE_UNBOUND):
            with self.subTest(state=state):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    resume_request(self._record(state=state), **self._kwargs())
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_BINDING_NOT_BOUND
                )

    def test_launch_and_resume_are_never_both_expressed(self) -> None:
        launch = build_option_fields(self._launch())
        resume = build_option_fields(self._resume())
        self.assertTrue(launch["session_id"] and not launch["resume"])
        self.assertTrue(resume["resume"] and not resume["session_id"])

    def test_a_workspace_other_than_the_bound_one_is_refused(self) -> None:
        elsewhere = self.tmp_path / "product" / "ai-dev-issue-56"
        elsewhere.mkdir(parents=True)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(workspace_path=elsewhere))
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_WORKSPACE_MISMATCH)

    def test_a_malformed_session_id_is_refused_before_any_options_exist(self) -> None:
        with self.assertRaises(Exception) as caught:
            launch_request(self._record(session_id="not-a-uuid"), **self._kwargs())
        self.assertIn("uuid", str(caught.exception).lower())

    def test_the_request_carries_the_binding_identity_verbatim(self) -> None:
        request = self._launch()
        self.assertEqual(request.mode, MODE_LAUNCH)
        self.assertEqual(request.session_id, SESSION)
        self.assertEqual(request.workspace_key, "github:jmrozi1/ai-dev#55")
        self.assertEqual(request.worktree_id, "ai-dev-issue-55")
        self.assertEqual(request.role, "executor")
        self.assertEqual(request.iteration, RailIteration(rail=RAIL, blob=BLOB))


class ProvenanceTests(RuntimeTestBase):
    def test_a_prompt_inside_the_product_workspace_is_refused(self) -> None:
        inside = self.workspace / "prompt.md"
        inside.write_text("editable by the executor\n", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(prompt_file=inside))
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_ASSET_INSIDE_WORKSPACE
        )

    def test_a_plugin_inside_the_product_workspace_is_refused(self) -> None:
        inside = self.workspace / "plugin"
        self._write_plugin(inside, skill=SKILL)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(plugin_root=inside))
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_ASSET_INSIDE_WORKSPACE
        )

    def test_a_prompt_outside_the_controller_root_is_refused(self) -> None:
        stray = self.tmp_path / "elsewhere.md"
        stray.write_text("unowned\n", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(prompt_file=stray))
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_ASSET_OUTSIDE_CONTROLLER_ROOT
        )

    def test_a_symlink_from_the_controller_root_into_the_workspace_is_refused(self) -> None:
        target = self.workspace / "prompt.md"
        target.write_text("editable by the executor\n", encoding="utf-8")
        link = self.controller_root / "prompts" / "linked.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(prompt_file=link))
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_ASSET_INSIDE_WORKSPACE
        )

    def test_a_missing_asset_is_refused_rather_than_silently_skipped(self) -> None:
        # The SDK skips a nonexistent plugin path and continues, so absence must be
        # caught here or the session would run with no skill and no explanation.
        missing = self.controller_root / "plugins" / "absent"
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(plugin_root=missing))
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_MISSING)

    def test_validate_controller_asset_accepts_a_controller_owned_path(self) -> None:
        self.assertEqual(
            validate_controller_asset(
                self.prompt_file, controller_root=self.controller_root,
                workspace_path=self.workspace, label="system prompt file", kind=ASSET_FILE,
            ),
            str(self.prompt_file),
        )

    def test_a_prompt_that_is_a_directory_is_refused(self) -> None:
        directory = self.controller_root / "prompts" / "bundle"
        directory.mkdir()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(prompt_file=directory))
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_NOT_A_FILE)

    def test_a_prompt_that_is_a_special_file_is_refused(self) -> None:
        fifo = self.controller_root / "prompts" / "pipe"
        try:
            os.mkfifo(str(fifo))
        except (AttributeError, OSError, NotImplementedError):
            self.skipTest("named pipes are unavailable on this platform")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(prompt_file=fifo))
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_NOT_A_FILE)

    def test_a_plugin_root_that_is_a_file_is_refused(self) -> None:
        as_file = self.controller_root / "plugins" / "not-a-directory"
        as_file.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(), **self._kwargs(plugin_root=as_file))
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_NOT_A_DIRECTORY)

    def test_an_unreadable_prompt_is_refused(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root bypasses the read permission bit")
        os.chmod(str(self.prompt_file), 0o200)
        try:
            with self.assertRaises(ClaudeRuntimeError) as caught:
                launch_request(self._record(), **self._kwargs())
        finally:
            # Restore here, not via addCleanup: cleanups run after tearDown has
            # already removed the tree.
            os.chmod(str(self.prompt_file), 0o600)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_UNREADABLE)


class PluginSurfaceTests(RuntimeTestBase):
    def _extra(self, relative: str, *, directory: bool = False) -> None:
        target = self.plugin_root / relative
        if directory:
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n", encoding="utf-8")

    def test_unexpected_plugin_capabilities_fail_closed(self) -> None:
        for relative, directory in (
            ("hooks", True),
            ("agents", True),
            ("commands", True),
            ("bin", True),
            (".mcp.json", False),
            (".lsp.json", False),
        ):
            with self.subTest(entry=relative):
                self._extra(relative, directory=directory)
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_SURFACE_UNEXPECTED,
                )
                self.assertIn(relative, caught.exception.detail)
                target = self.plugin_root / relative
                target.rmdir() if directory else target.unlink()

    def test_a_second_skill_fails_closed(self) -> None:
        (self.plugin_root / "skills" / "extra").mkdir()
        (self.plugin_root / "skills" / "extra" / "SKILL.md").write_text("x\n", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_PLUGIN_SKILL_MISSING)

    def test_the_expected_skill_must_actually_be_present(self) -> None:
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill="reviewer", role="reviewer")
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_PLUGIN_SKILL_MISSING)

    def test_a_skill_directory_without_a_skill_file_fails_closed(self) -> None:
        (self.plugin_root / "skills" / SKILL / "SKILL.md").unlink()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_PLUGIN_SKILL_MISSING)

    def test_a_manifest_redirecting_component_discovery_fails_closed(self) -> None:
        # A manifest key such as `hooks` or `mcpServers` points component discovery
        # at an arbitrary path, which would reintroduce exactly what the directory
        # scan rejects.
        for key in ("hooks", "mcpServers", "agents", "commands", "lspServers", "skills"):
            with self.subTest(key=key):
                self._write_plugin(
                    self.plugin_root, skill=SKILL,
                    manifest={"name": "ai-dev-executor", key: "./elsewhere"},
                )
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_MANIFEST_UNEXPECTED,
                )
                self.assertIn(key, caught.exception.detail)

    def test_a_descriptive_manifest_is_accepted(self) -> None:
        self._write_plugin(
            self.plugin_root, skill=SKILL,
            manifest={
                "name": "ai-dev-executor", "displayName": "AI Dev Executor",
                "version": "1.0.0", "description": "Bounded executor role.",
            },
        )
        self.assertEqual(
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL),
            str(self.plugin_root),
        )
        self.assertEqual(
            ALLOWED_MANIFEST_KEYS,
            frozenset({"name", "displayName", "version", "description"}),
        )

    def test_an_extra_file_beside_the_manifest_fails_closed(self) -> None:
        (self.plugin_root / ".claude-plugin" / "marketplace.json").write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_MANIFEST_UNEXPECTED
        )

    def test_a_missing_manifest_fails_closed(self) -> None:
        # Auto-discovery would accept a manifest-less plugin, leaving its identity
        # and declared surface inferred rather than stated.
        manifest = self.plugin_root / ".claude-plugin" / "plugin.json"
        manifest.unlink()
        manifest.parent.rmdir()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_MANIFEST_MISSING
        )

    def test_a_manifest_without_a_usable_name_fails_closed(self) -> None:
        for name in (None, "", "   ", 7, ["ai-dev-executor"]):
            with self.subTest(name=name):
                payload = {} if name is None else {"name": name}
                self._write_plugin(self.plugin_root, skill=SKILL, manifest=payload)
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_MANIFEST_UNEXPECTED,
                )

    def test_non_string_descriptive_manifest_values_fail_closed(self) -> None:
        for key, value in (
            ("displayName", 1), ("version", 1.0), ("description", {"text": "x"}),
        ):
            with self.subTest(key=key):
                self._write_plugin(
                    self.plugin_root, skill=SKILL,
                    manifest={"name": "ai-dev-executor", key: value},
                )
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_MANIFEST_UNEXPECTED,
                )
                self.assertIn(key, caught.exception.detail)

    def test_extra_content_inside_the_expected_skill_fails_closed(self) -> None:
        for relative, directory in (("scripts", True), ("reference.md", False)):
            with self.subTest(entry=relative):
                target = self.plugin_root / "skills" / SKILL / relative
                if directory:
                    target.mkdir()
                else:
                    target.write_text("extra\n", encoding="utf-8")
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_SURFACE_UNEXPECTED,
                )
                self.assertIn(relative, caught.exception.detail)
                target.rmdir() if directory else target.unlink()

    def test_a_skill_file_that_is_a_directory_fails_closed(self) -> None:
        skill_file = self.plugin_root / "skills" / SKILL / "SKILL.md"
        skill_file.unlink()
        skill_file.mkdir()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_ASSET_NOT_A_FILE)

    def test_a_malformed_manifest_fails_closed(self) -> None:
        (self.plugin_root / ".claude-plugin" / "plugin.json").write_text(
            "{ not json", encoding="utf-8"
        )
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_MANIFEST_UNEXPECTED
        )


class RolePackageBindingTests(RuntimeTestBase):
    """A session's role and the package it runs are compared, and must agree.

    Before checkpoint 75 `validate_plugin_surface` was never told which role was
    being launched. It proved the plugin was a well-formed package containing the
    skill its caller named, and said nothing about whether that was the role's
    skill -- so a binding recording `executor` could be launched with the reviewer
    package and nothing anywhere failed closed.
    """

    def _reviewer_plugin(self):
        root = self.controller_root / "plugins" / "ai-dev-reviewer"
        return self._write_plugin(
            root, skill="reviewer", manifest={"name": "ai-dev-reviewer"}
        )

    def test_the_package_of_another_role_is_refused_for_this_binding(self) -> None:
        reviewer = self._reviewer_plugin()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(
                self._record(role="executor"),
                **self._kwargs(plugin_root=reviewer, expected_skill="reviewer")
            )
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_ROLE_MISMATCH
        )
        self.assertIn("reviewer", caught.exception.detail)
        self.assertIn("executor", caught.exception.detail)

    def test_the_same_package_is_admitted_for_the_role_it_belongs_to(self) -> None:
        """The admitting control: only the record's role moves, on one package."""
        reviewer = self._reviewer_plugin()
        request = launch_request(
            self._record(role="reviewer"),
            **self._kwargs(plugin_root=reviewer, expected_skill="reviewer")
        )
        self.assertEqual(request.role, "reviewer")
        self.assertEqual(request.expected_skill, "reviewer")

    def test_the_executor_package_is_refused_for_a_reviewer_binding(self) -> None:
        """The other direction on the shipped fixture, so neither role is special."""
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(self._record(role="reviewer"), **self._kwargs())
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_ROLE_MISMATCH
        )

    def test_the_role_is_required_and_has_no_default(self) -> None:
        """A defaulted role would be skippable by anything that forgot it."""
        import inspect

        parameter = inspect.signature(validate_plugin_surface).parameters["role"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL)

    def test_the_role_comes_off_the_record_and_not_off_an_argument(self) -> None:
        """`_build_request` reads `record.role`; it takes no role of its own."""
        import inspect

        source = inspect.getsource(claude_runtime._build_request)
        # The whole call, not the substring: `role=record.role` also appears in the
        # `RuntimeRequest` construction below, so asserting it alone passed even
        # when the validator was handed the caller's `expected_skill` instead. The
        # M3 mutation found that, and this is the assertion that survives it.
        self.assertIn("expected_skill=expected_skill, role=record.role", source)
        self.assertNotIn(
            "role", inspect.signature(claude_runtime._build_request).parameters
        )

    def test_the_gate_reaches_every_request_this_boundary_builds(self) -> None:
        """Launch, resume and creating-launch all pass through the one comparison."""
        reviewer = self._reviewer_plugin()
        kwargs = self._kwargs(plugin_root=reviewer, expected_skill="reviewer")
        for builder, record in (
            (claude_runtime.launch_request, self._record(role="executor")),
            (
                claude_runtime.create_conversation_request,
                self._bound(role="executor"),
            ),
        ):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    builder(record, **kwargs)
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_PLUGIN_ROLE_MISMATCH,
                )

    def test_the_gate_and_the_request_have_exactly_one_site_and_it_is_the_same_one(
        self,
    ) -> None:
        """The chokepoint, asserted over the call graph rather than over formatting.

        Both halves are needed and neither implies the other. One validator call
        says the gate is not bypassed by a second, laxer call; one `RuntimeRequest`
        construction says no builder can assemble a request *beside* the gate and
        reach the SDK around it. A single site for each, and the same site for both,
        is the property this boundary is built on.

        Until checkpoint 76 this was `module.count("validate_plugin_surface(" +
        newline) == 1` -- a count of calls whose open paren happened to be followed
        by a line break. A real second call written on one line passed it, and no
        shipped test died when one was added. The guard counted a formatting
        artefact; this counts calls, over the whole package rather than one module,
        because a second call anywhere in `ai_dev_flow` would defeat the property.
        """
        for name in ("validate_plugin_surface", "RuntimeRequest"):
            with self.subTest(callee=name):
                self.assertEqual(
                    call_locations(name),
                    [("claude_runtime.py", "_build_request")],
                    "{0} call sites: {1}".format(name, call_sites(name)),
                )

    def test_the_refusal_reason_is_the_wire_value_operators_read(self) -> None:
        """`plugin-role-mismatch` is pinned here, once, as a literal.

        Every other assertion in this suite compares against the *constant*, so the
        string itself could have been renamed in one edit with the whole suite still
        green. It is what an operator greps for and what checkpoint 75's document
        prints, so it is a wire value and it is pinned like one.
        """
        self.assertEqual(
            claude_runtime.REASON_PLUGIN_ROLE_MISMATCH, "plugin-role-mismatch"
        )

    def test_the_role_and_package_comparison_is_case_sensitive(self) -> None:
        """Pinning the decided behaviour, not changing it.

        `skills[0] != role` is an exact string comparison, so an `Executor` package
        is not the `executor` role's package. That is the safe direction -- it fails
        closed, and role names are produced by the control plane rather than typed
        freehand -- but it was undecided in the sense that nothing said so. This
        asserts what the gate does today; a future slice that wants case folding
        must come here and change this on purpose.
        """
        cased = self.controller_root / "plugins" / "ai-dev-Executor"
        self._write_plugin(cased, skill="Executor", manifest={"name": "ai-dev-Executor"})
        if sorted(entry.name for entry in (cased / "skills").iterdir()) != ["Executor"]:
            self.skipTest("this filesystem does not preserve the case of entry names")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            launch_request(
                self._record(role="executor"),
                **self._kwargs(plugin_root=cased, expected_skill="Executor")
            )
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_ROLE_MISMATCH
        )


class NestedProvenanceTests(RuntimeTestBase):
    """Every traversed component must land inside the validated plugin root.

    Checking entry names alone would let a link named `skills` point anywhere and
    the scan would then walk into whatever it found.
    """

    def _relink(self, relative: str, target: Path) -> None:
        source = self.plugin_root / relative
        if source.is_dir() and not source.is_symlink():
            for child in sorted(source.rglob("*"), reverse=True):
                child.rmdir() if child.is_dir() else child.unlink()
            source.rmdir()
        elif source.exists() or source.is_symlink():
            source.unlink()
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")

    def _escape_targets(self) -> dict:
        sibling = self.controller_root / "plugins" / "other-plugin"
        self._write_plugin(sibling, skill=SKILL)
        outside = self.tmp_path / "outside"
        self._write_plugin(outside, skill=SKILL)
        return {
            "the product workspace": self.workspace,
            "a sibling controller directory": sibling,
            "outside the controller root": outside,
        }

    def test_a_skills_directory_symlinked_out_of_the_plugin_fails_closed(self) -> None:
        for where, target in self._escape_targets().items():
            with self.subTest(target=where):
                self._write_plugin(self.plugin_root, skill=SKILL)
                (target / "skills").mkdir(parents=True, exist_ok=True)
                self._relink("skills", target / "skills")
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_PLUGIN_NESTED_ESCAPE
                )

    def test_a_manifest_directory_symlinked_out_of_the_plugin_fails_closed(self) -> None:
        sibling = self.controller_root / "plugins" / "sibling-manifest"
        self._write_plugin(sibling, skill=SKILL)
        self._relink(".claude-plugin", sibling / ".claude-plugin")
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_NESTED_ESCAPE
        )

    def test_a_manifest_file_symlinked_out_of_the_plugin_fails_closed(self) -> None:
        stray = self.controller_root / "stray-plugin.json"
        stray.write_text('{"name": "stray"}\n', encoding="utf-8")
        self._relink(".claude-plugin/plugin.json", stray)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_NESTED_ESCAPE
        )

    def test_an_expected_skill_directory_symlinked_out_of_the_plugin_fails_closed(self) -> None:
        sibling = self.controller_root / "plugins" / "sibling-skill"
        self._write_plugin(sibling, skill=SKILL)
        self._relink("skills/{0}".format(SKILL), sibling / "skills" / SKILL)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_NESTED_ESCAPE
        )

    def test_a_skill_file_symlinked_into_the_workspace_fails_closed(self) -> None:
        target = self.workspace / "SKILL.md"
        target.write_text("executor-authored instructions\n", encoding="utf-8")
        self._relink("skills/{0}/SKILL.md".format(SKILL), target)
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(self.plugin_root, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_NESTED_ESCAPE
        )

    def test_a_plugin_reached_through_a_link_still_validates_its_real_contents(self) -> None:
        # Containment is judged after resolution, so a versioned plugin published
        # behind a `current` link is fine -- and its real contents are still what
        # gets checked.
        link = self.controller_root / "plugins" / "current"
        try:
            link.symlink_to(self.plugin_root)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertEqual(
            validate_plugin_surface(link, expected_skill=SKILL, role=SKILL), str(self.plugin_root)
        )
        (self.plugin_root / "hooks").mkdir()
        with self.assertRaises(ClaudeRuntimeError) as caught:
            validate_plugin_surface(link, expected_skill=SKILL, role=SKILL)
        self.assertEqual(
            caught.exception.reason, claude_runtime.REASON_PLUGIN_SURFACE_UNEXPECTED
        )

    def test_a_plugin_root_reached_through_a_link_is_accepted(self) -> None:
        link = self.controller_root / "plugins" / "current"
        try:
            link.symlink_to(self.plugin_root)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        request = launch_request(self._record(), **self._kwargs(plugin_root=link))
        self.assertEqual(request.plugin_root, str(self.plugin_root))


class EnvironmentScopeTests(RuntimeTestBase):
    def test_the_env_overlay_is_not_environment_isolation(self) -> None:
        # `env={}` is merged with the worker's inherited environment, so it adds
        # nothing and removes nothing. Credential and provider selectors already in
        # that environment survive it, and validating them needs ownership of the
        # child process -- the next rail's work, not this module's.
        fields = build_option_fields(self._launch())
        self.assertEqual(fields["env"], {})
        source = Path(claude_runtime.__file__).read_text(encoding="utf-8")
        self.assertIn("not a scrub of it", source)
        self.assertIn("process-integration rail", source)


class BoundsTests(RuntimeTestBase):
    def test_allowed_tools_must_be_an_exact_non_empty_set(self) -> None:
        for tools in ((), [], ["Read", "Read"], ["Read", ""], "Read", [None]):
            with self.subTest(tools=tools):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    self._launch(allowed_tools=tools)
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_INVALID_ALLOWED_TOOLS
                )

    def test_turn_and_budget_bounds_are_required_and_positive(self) -> None:
        for field, value in (
            ("max_turns", 0),
            ("max_turns", -1),
            ("max_turns", 1.5),
            ("max_turns", True),
            ("max_budget_usd", 0),
            ("max_budget_usd", -0.5),
            ("max_budget_usd", "5"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    self._launch(**{field: value})
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_INVALID_BOUNDS
                )


class SdkBoundaryTests(RuntimeTestBase):
    def test_versions_at_or_below_the_broken_setting_sources_release_are_rejected(self) -> None:
        for version in ("0.1.59", "0.1.0", "0.0.9"):
            with self.subTest(version=version):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    require_supported_sdk(lambda _name, v=version: v)
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_SDK_VERSION_UNSUPPORTED
                )
                self.assertIn("setting_sources", caught.exception.detail)

    def test_supported_versions_are_accepted(self) -> None:
        for version in ("0.1.60", "0.2.140", "1.0.0"):
            with self.subTest(version=version):
                self.assertEqual(require_supported_sdk(lambda _name, v=version: v), version)

    def test_an_unreadable_version_is_rejected_rather_than_guessed(self) -> None:
        for version in ("1.2.3rc1", "", "unknown", None, "1.x"):
            with self.subTest(version=version):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    require_supported_sdk(lambda _name, v=version: v)
                self.assertEqual(
                    caught.exception.reason, claude_runtime.REASON_SDK_VERSION_UNREADABLE
                )

    def test_a_missing_sdk_fails_actionably_before_any_provider_use(self) -> None:
        def absent(name):
            raise LookupError("No package metadata was found for {0}".format(name))

        with self.assertRaises(ClaudeRuntimeError) as caught:
            require_supported_sdk(absent)
        self.assertEqual(caught.exception.reason, claude_runtime.REASON_SDK_MISSING)
        self.assertIn("claude-agent-sdk", caught.exception.detail)

    def test_the_module_never_imports_the_sdk_at_module_scope(self) -> None:
        # Deterministic tests must not require the package to be installed.
        source = Path(claude_runtime.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertNotIn("claude_agent_sdk", line)
        self.assertEqual(parse_version("0.1.60"), MINIMUM_SDK_VERSION)


class ResultTests(RuntimeTestBase):
    def test_a_matching_result_is_reduced_to_binding_relevant_facts(self) -> None:
        request = self._launch()
        result = interpret_result(
            request,
            {
                "session_id": SESSION,
                "subtype": "success",
                "is_error": False,
                "num_turns": 12,
                "total_cost_usd": 0.4213,
                "result": "the assistant's full answer text",
            },
        )
        self.assertEqual(result.session_id, SESSION)
        self.assertEqual(result.mode, MODE_LAUNCH)
        self.assertEqual(result.subtype, "success")
        self.assertFalse(result.is_error)
        self.assertEqual(result.num_turns, 12)
        self.assertAlmostEqual(result.total_cost_usd, 0.4213)

    def test_no_transcript_or_provider_text_survives_into_the_result(self) -> None:
        request = self._launch()
        result = interpret_result(
            request,
            {
                "session_id": SESSION, "subtype": "success",
                "result": "secret assistant text", "transcript_path": "/tmp/t.jsonl",
                "usage": {"input_tokens": 10},
            },
        )
        rendered = json.dumps(result.__dict__, sort_keys=True, default=str)
        for leaked in ("secret assistant text", "transcript", "usage", "input_tokens"):
            self.assertNotIn(leaked, rendered)

    def test_a_different_session_id_in_the_result_is_refused(self) -> None:
        request = self._launch()
        for observed in (OTHER_SESSION, None, ""):
            with self.subTest(observed=observed):
                with self.assertRaises(ClaudeRuntimeError) as caught:
                    interpret_result(request, {"session_id": observed, "subtype": "success"})
                self.assertEqual(
                    caught.exception.reason,
                    claude_runtime.REASON_RESULT_SESSION_MISMATCH,
                )

    def test_a_non_success_subtype_is_an_error_even_without_the_error_flag(self) -> None:
        request = self._resume()
        result = interpret_result(
            request, {"session_id": SESSION, "subtype": "error_max_turns"}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.subtype, "error_max_turns")
        self.assertEqual(result.mode, MODE_RESUME)

    def test_absent_optional_fields_stay_absent_rather_than_becoming_zero(self) -> None:
        request = self._launch()
        result = interpret_result(request, {"session_id": SESSION, "subtype": "success"})
        self.assertIsNone(result.num_turns)
        self.assertIsNone(result.total_cost_usd)


if __name__ == "__main__":
    unittest.main()
