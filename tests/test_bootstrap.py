from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.cli import FIXED_FLOW_EXECUTABLE_COMMANDS
from ai_dev_flow.bootstrap import (
    BootstrapError,
    LEGACY_RETIRED_FLOW_LAUNCHER_NAMES,
    OWNERSHIP_MARKER,
    _DIRECT_FLOW_ROUTE_TOKEN,
    _render_path_guidance,
    _is_path_on_path,
    _paths_equal,
    resolve_legacy_installation_manifest_path,
    resolve_prefix_launcher_ownership_path,
    render_cmd_launcher,
    render_posix_launcher,
    render_powershell_launcher,
    run_bootstrap,
)


class BootstrapCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.config_path = self.tmp_path / "cfg" / "config.yaml"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_prefix_ownership_record(
        self,
        *,
        home: Path,
        selected_prefix: str,
        platform: str,
        install_directory: Path | str,
        owned_launchers: dict[str, str],
    ) -> Path:
        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=home)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "selected_prefix": selected_prefix,
            "platform": platform,
            "install_directory": str(install_directory),
            "owned_launchers": dict(sorted(owned_launchers.items())),
        }
        record_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return record_path

    def _write_legacy_installation_manifest(
        self,
        *,
        home: Path,
        managed_launchers: dict[str, str],
    ) -> Path:
        manifest_path = resolve_legacy_installation_manifest_path(os_name="posix", home=home)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "managed_launchers": dict(sorted(managed_launchers.items())),
        }
        manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return manifest_path

    def _legacy_retired_launcher_text(self, launcher_name: str) -> str:
        exec_line_by_name = {
            "flow": "exec 'ai-dev' 'flow' \"$@\"",
            "flow-help": "exec 'ai-dev' 'flow' '--help' \"$@\"",
            "flow-review": "exec 'ai-dev' 'flow' 'review' \"$@\"",
            "flow-task-prepare": "exec 'ai-dev' 'flow' 'task-prepare' \"$@\"",
        }
        exec_line = exec_line_by_name[launcher_name]
        return (
            "#!/usr/bin/env sh\n"
            "# AI_DEV_MANAGED_LAUNCHER_V1\n"
            "set -eu\n"
            f"{exec_line}\n"
        )

    def test_fixed_flow_executable_registry_matches_checkpoint_two_plus_ticket_surface(self) -> None:
        self.assertEqual(
            FIXED_FLOW_EXECUTABLE_COMMANDS,
            (
                "start",
                "patch",
                "status",
                "diff",
                "commit",
                "reset",
                "promote",
                "complete",
                "block",
                "resume",
                "ticket-create",
                "ticket-show",
                "ticket-query",
            ),
        )

    def test_render_posix_launcher_forwards_args(self) -> None:
        text = render_posix_launcher(
            repo_root=Path("/tmp/a b/o'brien/repo"),
            python_executable=Path("/usr/bin/python3"),
            command_name="ai-dev",
        )
        self.assertIn(OWNERSHIP_MARKER, text)
        self.assertIn('exec "${PYTHON_EXECUTABLE}" -m ai_dev_flow.cli "$@"', text)

    def test_render_powershell_launcher_forwards_args_and_exit_code(self) -> None:
        text = render_powershell_launcher(
            repo_root=Path("C:/Users/O'Brien/Repo"),
            python_executable=Path("C:/Python311/python.exe"),
            command_name="ai-dev",
        )
        self.assertIn(OWNERSHIP_MARKER, text)
        self.assertIn("& $pythonExecutable -m ai_dev_flow.cli @args", text)
        self.assertIn("$global:LASTEXITCODE = $downstreamExitCode", text)
        self.assertNotRegex(text, r"(?im)^\s*exit\b")
        self.assertNotIn("Invoke-Expression", text)
        self.assertIn("$hadFlowCommandName = Test-Path Env:FLOW_COMMAND_NAME", text)
        self.assertIn("$env:FLOW_COMMAND_NAME = $previousFlowCommandName", text)
        self.assertIn("Remove-Item Env:FLOW_COMMAND_NAME", text)
        self.assertIn("$env:PYTHONPATH = $previousPythonPath", text)

    def test_render_cmd_launcher_forwards_args_and_exit_code(self) -> None:
        text = render_cmd_launcher(
            repo_root=Path("C:/Users/Example/Repo"),
            python_executable=Path("C:/Python311/python.exe"),
            command_name="ai-dev",
        )
        self.assertIn(OWNERSHIP_MARKER, text)
        self.assertIn("-m ai_dev_flow.cli %*", text)
        self.assertIn("exit /b %AI_DEV_EXIT%", text)

    def test_path_detection_posix_and_windows(self) -> None:
        self.assertTrue(
            _is_path_on_path(Path("/tmp/bin"), path_value="/usr/bin:/tmp/bin:/bin", windows=False)
        )
        self.assertFalse(
            _is_path_on_path(Path("/tmp/bin"), path_value="/usr/bin:/bin", windows=False)
        )
        self.assertTrue(
            _is_path_on_path(
                Path("C:/Users/Example/.local/bin"),
                path_value='C:/Windows/System32;"c:/users/example/.LOCAL/bin"',
                windows=True,
            )
        )

    def test_windows_path_comparison_semantics(self) -> None:
        self.assertTrue(
            _paths_equal(
                Path("C:/Users/Example/Repo"),
                Path("c:/users/example/repo"),
                windows=True,
            )
        )
        self.assertFalse(
            _paths_equal(
                Path("/tmp/A"),
                Path("/tmp/a"),
                windows=False,
            )
        )

    def test_posix_install_is_idempotent_and_preserves_existing_config(self) -> None:
        home = self.tmp_path / "home"
        install_dir = home / ".local" / "bin"
        home.mkdir(parents=True)

        first = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value="/usr/bin:/bin",
        )
        self.assertEqual(first.command_name, "flow-*")
        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            launcher_path = install_dir / f"flow-{command}"
            self.assertTrue(launcher_path.exists())
            self.assertTrue(launcher_path.stat().st_mode & 0o111)

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value=f"{install_dir}:/usr/bin:/bin",
        )
        states = [item.state for item in second.launcher_statuses]
        self.assertEqual(states, ["up-to-date"] * len(FIXED_FLOW_EXECUTABLE_COMMANDS))
        self.assertTrue(second.install_dir_on_path)

    def test_non_owned_collision_is_rejected(self) -> None:
        home = self.tmp_path / "home-collision"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                interactive=False,
            )

        self.assertEqual(collision_path.read_text(encoding="utf-8"), "#!/usr/bin/env sh\necho custom\n")

    def test_interactive_y_replaces_non_owned_conflicting_launcher(self) -> None:
        home = self.tmp_path / "home-collision-yes"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            input_stream=io.StringIO("y\n"),
            output_stream=io.StringIO(),
            interactive=True,
        )

        self.assertIn(OWNERSHIP_MARKER, collision_path.read_text(encoding="utf-8"))
        states = {item.path.name: item.state for item in result.launcher_statuses}
        self.assertEqual(states["flow-start"], "updated")

    def test_interactive_yes_replaces_non_owned_conflicting_launcher(self) -> None:
        home = self.tmp_path / "home-collision-yes-long"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            input_stream=io.StringIO("yes\n"),
            output_stream=io.StringIO(),
            interactive=True,
        )

        self.assertIn(OWNERSHIP_MARKER, collision_path.read_text(encoding="utf-8"))

    def test_interactive_declines_preserve_conflicting_launcher_bytes(self) -> None:
        responses = ("\n", "n\n", "No\n", "maybe\n")
        for index, response in enumerate(responses):
            with self.subTest(response=response.strip() or "empty"):
                home = self.tmp_path / f"home-collision-decline-{index}"
                install_dir = home / ".local" / "bin"
                install_dir.mkdir(parents=True, exist_ok=True)
                collision_path = install_dir / "flow-start"
                original = "#!/usr/bin/env sh\necho custom\n"
                collision_path.write_text(original, encoding="utf-8")
                output_capture = io.StringIO()

                with self.assertRaises(BootstrapError) as context:
                    run_bootstrap(
                        platform="posix",
                        repo_root=self.repo_root,
                        prefix="flow",
                        install_directory=install_dir,
                        home=home,
                        shell_program="/bin/bash",
                        config_path=self.config_path,
                        input_stream=io.StringIO(response),
                        output_stream=output_capture,
                        interactive=True,
                    )

                self.assertIn("Preserved conflicting launcher", str(context.exception))
                self.assertIn("Replace it? [y/N]", output_capture.getvalue())
                self.assertEqual(collision_path.read_text(encoding="utf-8"), original)

    def test_noninteractive_without_force_refuses_and_preserves_conflicting_launcher(self) -> None:
        home = self.tmp_path / "home-collision-noninteractive"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        original = "#!/usr/bin/env sh\necho custom\n"
        collision_path.write_text(original, encoding="utf-8")

        with self.assertRaises(BootstrapError) as context:
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                interactive=False,
            )

        self.assertIn("Cannot prompt to replace conflicting launcher", str(context.exception))
        self.assertEqual(collision_path.read_text(encoding="utf-8"), original)

    def test_force_replaces_conflicting_launcher_without_stdin_read(self) -> None:
        class _ExplodingInput:
            def readline(self) -> str:
                raise AssertionError("stdin should not be read in force mode")

            def isatty(self) -> bool:
                return False

        home = self.tmp_path / "home-collision-force"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")
        stderr_capture = io.StringIO()

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            force=True,
            input_stream=_ExplodingInput(),
            output_stream=stderr_capture,
            interactive=False,
        )

        self.assertIn(OWNERSHIP_MARKER, collision_path.read_text(encoding="utf-8"))
        self.assertIn("Force-replacing conflicting launcher", stderr_capture.getvalue())

    def test_force_replacement_updates_ownership_and_second_install_requires_no_force(self) -> None:
        home = self.tmp_path / "home-collision-record"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            force=True,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            interactive=False,
        )

        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=home)
        record_data = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertIn(str(collision_path), record_data["owned_launchers"])
        self.assertEqual(
            record_data["owned_launchers"][str(collision_path)],
            f"symlink:{(self.repo_root / 'skills' / 'copilot' / 'flow' / 'scripts' / 'flow-start').resolve()}",
        )

        class _NoReadInput:
            def readline(self) -> str:
                raise AssertionError("prompt should not be reached")

            def isatty(self) -> bool:
                return True

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            input_stream=_NoReadInput(),
            output_stream=io.StringIO(),
            interactive=True,
        )
        self.assertTrue(all(item.state == "up-to-date" for item in second.launcher_statuses if item.path == collision_path))

    def test_modified_owned_launcher_follows_prompt_force_contract(self) -> None:
        home = self.tmp_path / "home-collision-divergent-owned"
        install_dir = home / ".local" / "bin"
        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        launcher_path = install_dir / "flow-start"
        launcher_path.unlink()
        launcher_path.write_text("#!/usr/bin/env sh\necho user change\n", encoding="utf-8")

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                interactive=False,
            )

        self.assertTrue(launcher_path.is_file())
        self.assertIn("user change", launcher_path.read_text(encoding="utf-8"))

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            force=True,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            interactive=False,
        )
        self.assertTrue(launcher_path.is_symlink())

    def test_force_replacement_does_not_overwrite_symlink_target(self) -> None:
        home = self.tmp_path / "home-collision-symlink"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        target_path = home / "outside-target.sh"
        target_path.write_text("#!/usr/bin/env sh\necho outside\n", encoding="utf-8")
        launcher_path = install_dir / "flow-start"
        launcher_path.symlink_to(target_path)

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            force=True,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            interactive=False,
        )

        self.assertEqual(target_path.read_text(encoding="utf-8"), "#!/usr/bin/env sh\necho outside\n")
        self.assertTrue(launcher_path.is_symlink())
        self.assertEqual(
            launcher_path.resolve(),
            (self.repo_root / "skills" / "copilot" / "flow" / "scripts" / "flow-start").resolve(),
        )

    def test_unrelated_similarly_named_file_remains_untouched_when_forcing_conflict_replacement(self) -> None:
        home = self.tmp_path / "home-collision-unrelated"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        unrelated_path = install_dir / "flow-start-custom"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")
        unrelated_path.write_text("custom tool\n", encoding="utf-8")

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            force=True,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            interactive=False,
        )

        self.assertEqual(unrelated_path.read_text(encoding="utf-8"), "custom tool\n")

    def test_non_owned_collision_with_marker_mention_is_rejected(self) -> None:
        home = self.tmp_path / "home-collision-marker"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "flow-start"
        collision_path.write_text(
            "#!/usr/bin/env sh\n"
            "echo start\n"
            f"echo {OWNERSHIP_MARKER}\n",
            encoding="utf-8",
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                interactive=False,
            )

        self.assertIn(OWNERSHIP_MARKER, collision_path.read_text(encoding="utf-8"))

    def test_invalid_repo_root_fails(self) -> None:
        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.tmp_path / "missing-repo",
                config_path=self.config_path,
            )

    def test_invalid_python_fails(self) -> None:
        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                explicit_python=str(self.tmp_path / "no-python"),
                config_path=self.config_path,
            )

    def test_invalid_prefix_rejected_before_filesystem_work(self) -> None:
        invalid_prefixes = ["", " flow", "flow ", "flow/status", "flow*", "flow?", "flow.", "flow_"]
        for invalid_prefix in invalid_prefixes:
            with self.subTest(prefix=invalid_prefix):
                with self.assertRaises(BootstrapError) as ctx:
                    run_bootstrap(
                        platform="posix",
                        repo_root=self.tmp_path / "missing-repo",
                        prefix=invalid_prefix,
                        config_path=self.config_path,
                    )

                self.assertIn("Invalid prefix", str(ctx.exception))

    def test_windows_install_renders_cmd_and_ps1_without_eval(self) -> None:
        home = self.tmp_path / "home-win"
        install_dir = home / ".local" / "bin"
        result = run_bootstrap(
            platform="windows",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            user_profile=str(home),
            config_path=self.config_path,
            path_value="C:/Windows/System32",
        )
        self.assertFalse(result.install_dir_on_path)

        ps1_path = install_dir / "flow-status.ps1"
        cmd_path = install_dir / "flow-status.cmd"
        ps1_text = ps1_path.read_text(encoding="utf-8")
        cmd_text = cmd_path.read_text(encoding="utf-8")
        self.assertIn("@args", ps1_text)
        self.assertIn("$global:LASTEXITCODE = $downstreamExitCode", ps1_text)
        self.assertNotRegex(ps1_text, r"(?im)^\s*exit\b")
        self.assertNotIn("Invoke-Expression", ps1_text)
        self.assertIn("%*", cmd_text)

    def test_launcher_write_failure_restores_previous_owned_file(self) -> None:
        home = self.tmp_path / "home-rollback"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = install_dir / "flow-start"
        original = "#!/usr/bin/env sh\n# AI_DEV_LAUNCHER_V1\necho old\n"
        launcher_path.write_text(original, encoding="utf-8")
        launcher_path.chmod(0o755)

        with patch("pathlib.Path.symlink_to", side_effect=OSError("symlink fail")):
            with self.assertRaises(BootstrapError):
                run_bootstrap(
                    platform="posix",
                    repo_root=self.repo_root,
                    prefix="flow",
                    install_directory=install_dir,
                    home=home,
                    shell_program="/bin/bash",
                    config_path=self.config_path,
                    input_stream=io.StringIO("y\n"),
                    output_stream=io.StringIO(),
                    interactive=True,
                )

        self.assertEqual(launcher_path.read_text(encoding="utf-8"), original)

    def test_posix_launcher_end_to_end_executes_and_forwards_exit_codes(self) -> None:
        home = self.tmp_path / "home-e2e"
        install_dir = home / "bin"
        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value="/usr/bin:/bin",
        )
        self.assertEqual(result.command_name, "flow-*")

        launcher = install_dir / "flow-status"
        self.assertTrue(launcher.exists())

        help_run = subprocess.run(
            [str(launcher), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.tmp_path),
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("usage:", help_run.stdout.lower())
        self.assertNotIn("config", help_run.stdout)

        invalid_token = "not-a-real-flow-status-token"
        invalid_run = subprocess.run(
            [str(launcher), invalid_token],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.tmp_path),
        )
        self.assertNotEqual(invalid_run.returncode, 0)
        self.assertIn("Usage: flow-status", invalid_run.stderr)

    def test_windows_path_guidance_single_quotes_literal_path(self) -> None:
        special_path = Path("C:/Users/O'Brien/Dir With $dollars/with`tick;semi(paren)")
        guidance = _render_path_guidance(special_path, windows=True)
        self.assertIn("Environment Variables", guidance)
        self.assertIn("$env:Path = 'C:/Users/O''Brien/Dir With $dollars/with`tick;semi(paren)' + ';' + $env:Path", guidance)
        self.assertNotIn("setx", guidance.lower())

    def test_bootstrap_powershell_wrapper_contains_no_exit(self) -> None:
        wrapper_path = self.repo_root / "tools" / "compatibility" / "bootstrap-ai-dev.ps1"
        text = wrapper_path.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?im)^\s*exit\b")
        self.assertNotIn("Invoke-Expression", text)
        self.assertIn("$global:LASTEXITCODE", text)

    def test_powershell_launcher_runtime_restores_environment(self) -> None:
        pwsh_path = shutil.which("pwsh")
        if pwsh_path is None:
            self.skipTest("pwsh is not available")

        home = self.tmp_path / "home-ps-runtime"
        install_dir = home / "bin"
        result = run_bootstrap(
            platform="windows",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            user_profile=str(home),
            config_path=self.config_path,
            path_value="C:/Windows/System32",
        )
        launcher = install_dir / "ai-dev.ps1"
        self.assertTrue(launcher.exists())
        self.assertEqual(result.command_name, "ai-dev")

        invalid_token = "not-a-real-ai-dev-command"
        script_with_existing = (
            f"$env:FLOW_COMMAND_NAME='already-there';"
            f"$env:PYTHONPATH='existing-pythonpath';"
            f". '{launcher}' '{invalid_token}';"
            "Write-Output 'AFTER-LAUNCH';"
            "Write-Output \"FLOW_AFTER=$env:FLOW_COMMAND_NAME\";"
            "if (Test-Path Env:FLOW_COMMAND_NAME) { Write-Output 'FLOW_EXISTS=1' } else { Write-Output 'FLOW_EXISTS=0' };"
            "Write-Output \"PYTHONPATH_AFTER=$env:PYTHONPATH\";"
            "Write-Output \"LAST_AFTER=$global:LASTEXITCODE\";"
        )
        completed_existing = subprocess.run(
            [pwsh_path, "-NoProfile", "-Command", script_with_existing],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.tmp_path),
            env={**dict(os.environ), "HOME": str(home), "USERPROFILE": str(home), "AI_DEV_PYTHON": sys.executable},
        )
        self.assertIn("AFTER-LAUNCH", completed_existing.stdout)
        self.assertIn("FLOW_AFTER=already-there", completed_existing.stdout)
        self.assertIn("FLOW_EXISTS=1", completed_existing.stdout)
        self.assertIn("PYTHONPATH_AFTER=existing-pythonpath", completed_existing.stdout)
        match_existing = re.search(r"LAST_AFTER=(-?\d+)", completed_existing.stdout)
        self.assertIsNotNone(match_existing)
        self.assertNotEqual(int(match_existing.group(1)), 0)

        script_without_existing = (
            "Remove-Item Env:FLOW_COMMAND_NAME -ErrorAction SilentlyContinue;"
            "$env:PYTHONPATH='existing-pythonpath';"
            f". '{launcher}' '{invalid_token}';"
            "Write-Output 'AFTER-LAUNCH';"
            "if (Test-Path Env:FLOW_COMMAND_NAME) { Write-Output 'FLOW_EXISTS=1' } else { Write-Output 'FLOW_EXISTS=0' };"
            "Write-Output \"PYTHONPATH_AFTER=$env:PYTHONPATH\";"
            "Write-Output \"LAST_AFTER=$global:LASTEXITCODE\";"
        )
        completed_absent = subprocess.run(
            [pwsh_path, "-NoProfile", "-Command", script_without_existing],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.tmp_path),
            env={**dict(os.environ), "HOME": str(home), "USERPROFILE": str(home), "AI_DEV_PYTHON": sys.executable},
        )
        self.assertIn("AFTER-LAUNCH", completed_absent.stdout)
        self.assertIn("FLOW_EXISTS=0", completed_absent.stdout)
        self.assertIn("PYTHONPATH_AFTER=existing-pythonpath", completed_absent.stdout)
        match_absent = re.search(r"LAST_AFTER=(-?\d+)", completed_absent.stdout)
        self.assertIsNotNone(match_absent)
        self.assertNotEqual(int(match_absent.group(1)), 0)

    def test_bootstrap_powershell_wrapper_runtime_returns_to_same_shell(self) -> None:
        pwsh_path = shutil.which("pwsh")
        if pwsh_path is None:
            self.skipTest("pwsh is not available")

        wrapper = self.repo_root / "tools" / "compatibility" / "bootstrap-ai-dev.ps1"
        temp_home = self.tmp_path / "home-wrapper-ps"
        temp_home.mkdir(parents=True, exist_ok=True)
        script = (
            f"& '{wrapper}';"
            "Write-Output 'AFTER-BOOTSTRAP';"
            "Write-Output \"LAST_AFTER=$global:LASTEXITCODE\";"
        )
        completed = subprocess.run(
            [pwsh_path, "-NoProfile", "-Command", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), "HOME": str(temp_home), "USERPROFILE": str(temp_home), "AI_DEV_PYTHON": sys.executable},
        )

        self.assertIn("AFTER-BOOTSTRAP", completed.stdout)
        match = re.search(r"LAST_AFTER=(-?\d+)", completed.stdout)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), 0)

    def test_prefix_install_creates_fixed_flow_launchers(self) -> None:
        home = self.tmp_path / "home-prefix"
        install_dir = home / ".local" / "bin"

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertEqual(result.command_name, "flow-*")
        installed_names = {status.path.name for status in result.launcher_statuses}
        expected_names = {f"flow-{command}" for command in FIXED_FLOW_EXECUTABLE_COMMANDS}
        self.assertEqual(installed_names, expected_names)

        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            launcher_path = install_dir / f"flow-{command}"
            self.assertTrue(launcher_path.is_symlink())
            self.assertEqual(
                launcher_path.resolve(),
                (self.repo_root / "skills" / "copilot" / "flow" / "scripts" / f"flow-{command}").resolve(),
            )
            launcher_text = launcher_path.read_text(encoding="utf-8")
            self.assertIn(_DIRECT_FLOW_ROUTE_TOKEN, launcher_text)
            self.assertIn(f'"{command}"', launcher_text)

    def test_recorded_owned_launchers_are_removed_on_prefix_change(self) -> None:
        home = self.tmp_path / "home-prefix-stale"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        removed = {item.path.name for item in result.launcher_statuses if item.state == "removed"}
        self.assertIn("flow-status", removed)

    def test_legacy_retired_launchers_are_removed_only_when_digest_and_shape_match(self) -> None:
        home = self.tmp_path / "home-legacy-retired-cleanup"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        managed_launchers: dict[str, str] = {}
        for launcher_name in LEGACY_RETIRED_FLOW_LAUNCHER_NAMES:
            path = install_dir / launcher_name
            content = self._legacy_retired_launcher_text(launcher_name)
            path.write_text(content, encoding="utf-8")
            managed_launchers[str(path)] = hashlib.sha256(content.encode("utf-8")).hexdigest()

        self._write_legacy_installation_manifest(
            home=home,
            managed_launchers=managed_launchers,
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        removed = {
            item.path.name: item.state
            for item in result.launcher_statuses
            if item.path.name in LEGACY_RETIRED_FLOW_LAUNCHER_NAMES
        }
        self.assertEqual(
            removed,
            {name: "removed" for name in LEGACY_RETIRED_FLOW_LAUNCHER_NAMES},
        )
        for launcher_name in LEGACY_RETIRED_FLOW_LAUNCHER_NAMES:
            self.assertFalse((install_dir / launcher_name).exists())

    def test_legacy_retired_launcher_is_preserved_when_digest_mismatch(self) -> None:
        home = self.tmp_path / "home-legacy-retired-digest-mismatch"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        launcher_name = "flow-review"
        launcher_path = install_dir / launcher_name
        launcher_content = self._legacy_retired_launcher_text(launcher_name)
        launcher_path.write_text(launcher_content, encoding="utf-8")

        self._write_legacy_installation_manifest(
            home=home,
            managed_launchers={
                str(launcher_path): hashlib.sha256(b"wrong-digest-source").hexdigest(),
            },
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        status = [
            item.state
            for item in result.launcher_statuses
            if item.path == launcher_path
        ]
        self.assertEqual(status, ["preserved-divergent"])
        self.assertTrue(launcher_path.exists())

    def test_legacy_retired_launcher_is_preserved_when_shape_mismatch(self) -> None:
        home = self.tmp_path / "home-legacy-retired-shape-mismatch"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        launcher_name = "flow-task-prepare"
        launcher_path = install_dir / launcher_name
        launcher_content = (
            "#!/usr/bin/env sh\n"
            "# AI_DEV_MANAGED_LAUNCHER_V1\n"
            "set -eu\n"
            "exec 'ai-dev' 'flow' 'status' \"$@\"\n"
        )
        launcher_path.write_text(launcher_content, encoding="utf-8")

        self._write_legacy_installation_manifest(
            home=home,
            managed_launchers={
                str(launcher_path): hashlib.sha256(launcher_content.encode("utf-8")).hexdigest(),
            },
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        status = [
            item.state
            for item in result.launcher_statuses
            if item.path == launcher_path
        ]
        self.assertEqual(status, ["preserved-divergent"])
        self.assertTrue(launcher_path.exists())

    def test_legacy_cleanup_is_narrow_to_retired_flow_launcher_names(self) -> None:
        home = self.tmp_path / "home-legacy-retired-narrow-scope"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        launcher_path = install_dir / "flow-legacy-custom"
        launcher_content = (
            "#!/usr/bin/env sh\n"
            "# AI_DEV_MANAGED_LAUNCHER_V1\n"
            "set -eu\n"
            "exec 'ai-dev' 'flow' 'status' \"$@\"\n"
        )
        launcher_path.write_text(launcher_content, encoding="utf-8")

        self._write_legacy_installation_manifest(
            home=home,
            managed_launchers={
                str(launcher_path): hashlib.sha256(launcher_content.encode("utf-8")).hexdigest(),
            },
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertTrue(launcher_path.exists())
        self.assertFalse(any(item.path == launcher_path for item in result.launcher_statuses))

    def test_invalid_prefix_rejected_before_repo_validation(self) -> None:
        invalid_prefixes = ["", " flow", "flow ", "flow/status", "flow*", "flow?", "flow.", "flow_"]
        for invalid_prefix in invalid_prefixes:
            with self.subTest(prefix=invalid_prefix):
                with self.assertRaises(BootstrapError) as ctx:
                    run_bootstrap(
                        platform="posix",
                        repo_root=self.tmp_path / "missing-repo",
                        prefix=invalid_prefix,
                        config_path=self.config_path,
                    )
                self.assertIn("Invalid prefix", str(ctx.exception))

    def test_prefix_transition_flow_to_ai_flow_removes_old_owned_launchers(self) -> None:
        home = self.tmp_path / "home-prefix-transition-1"
        install_dir = home / ".local" / "bin"

        first = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        self.assertTrue(any(item.path.name == "flow-status" for item in first.launcher_statuses))

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            self.assertFalse((install_dir / f"flow-{command}").exists())
            self.assertTrue((install_dir / f"ai-flow-{command}").exists())

        removed = {item.path.name for item in second.launcher_statuses if item.state == "removed"}
        self.assertIn("flow-status", removed)

    def test_custom_prefix_installs_ticket_command_launchers(self) -> None:
        home = self.tmp_path / "home-prefix-ticket-launchers"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertTrue((install_dir / "ai-flow-ticket-create").exists())
        self.assertTrue((install_dir / "ai-flow-ticket-show").exists())
        self.assertTrue((install_dir / "ai-flow-ticket-query").exists())

    def test_prefix_transition_ai_flow_to_flow_removes_old_owned_launchers(self) -> None:
        home = self.tmp_path / "home-prefix-transition-2"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            self.assertFalse((install_dir / f"ai-flow-{command}").exists())
            self.assertTrue((install_dir / f"flow-{command}").exists())

        removed = {item.path.name for item in second.launcher_statuses if item.state == "removed"}
        self.assertIn("ai-flow-status", removed)

    def test_same_prefix_moved_to_different_install_directory_reconciles_old_owned_launchers(self) -> None:
        home = self.tmp_path / "home-prefix-move-same"
        old_install_dir = home / ".local" / "bin-old"
        new_install_dir = home / ".local" / "bin-new"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=old_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=new_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        removed = {item.path.name for item in second.launcher_statuses if item.state == "removed"}
        self.assertIn("flow-status", removed)
        self.assertFalse((old_install_dir / "flow-status").exists())
        self.assertTrue((new_install_dir / "flow-status").exists())

    def test_prefix_and_install_directory_changed_together_reconciles_old_owned_launchers(self) -> None:
        home = self.tmp_path / "home-prefix-move-both"
        old_install_dir = home / ".local" / "bin-old"
        new_install_dir = home / ".local" / "bin-new"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=old_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=new_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        removed = {item.path.name for item in second.launcher_statuses if item.state == "removed"}
        self.assertIn("flow-status", removed)
        self.assertFalse((old_install_dir / "flow-status").exists())
        self.assertTrue((new_install_dir / "ai-flow-status").exists())

    def test_modified_old_install_directory_launcher_is_preserved_and_reported_after_move(self) -> None:
        home = self.tmp_path / "home-prefix-move-preserve"
        old_install_dir = home / ".local" / "bin-old"
        new_install_dir = home / ".local" / "bin-new"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=old_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        modified = old_install_dir / "flow-status"
        modified.unlink()
        modified.write_text("#!/usr/bin/env sh\necho old-dir divergent\n", encoding="utf-8")

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=new_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        preserved = [item for item in second.launcher_statuses if item.path == modified]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].state, "preserved-divergent")
        self.assertTrue(modified.exists())
        self.assertTrue((new_install_dir / "flow-status").exists())

    def test_old_matching_launchers_removed_and_new_record_contains_only_new_set_after_move(self) -> None:
        home = self.tmp_path / "home-prefix-move-record"
        old_install_dir = home / ".local" / "bin-old"
        new_install_dir = home / ".local" / "bin-new"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=old_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=new_install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=home)
        record_data = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record_data["selected_prefix"], "flow")
        self.assertEqual(record_data["install_directory"], str(new_install_dir))

        recorded_paths = set(record_data["owned_launchers"].keys())
        self.assertTrue(recorded_paths)
        self.assertTrue(all(path.startswith(str(new_install_dir) + os.sep) for path in recorded_paths))
        self.assertFalse(any(path.startswith(str(old_install_dir) + os.sep) for path in recorded_paths))

    def test_modified_obsolete_launcher_is_preserved_and_reported(self) -> None:
        home = self.tmp_path / "home-prefix-modified"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        modified = install_dir / "flow-status"
        modified.unlink()
        modified.write_text("#!/usr/bin/env sh\necho divergent\n", encoding="utf-8")

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertTrue(modified.exists())
        preserved = [item for item in second.launcher_statuses if item.path == modified]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].state, "preserved-divergent")

    def test_owned_obsolete_flow_review_launcher_is_removed(self) -> None:
        home = self.tmp_path / "home-obsolete-review-removed"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        launcher_path = install_dir / "flow-review"
        launcher_text = render_posix_launcher(
            repo_root=self.repo_root,
            python_executable=Path("/usr/bin/python3"),
            command_name="flow-review",
            flow_direct_command="review",
        )
        launcher_path.write_text(launcher_text, encoding="utf-8")
        launcher_path.chmod(0o755)
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(launcher_path): hashlib.sha256(launcher_text.encode("utf-8")).hexdigest()},
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        removed = [item for item in result.launcher_statuses if item.path == launcher_path]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].state, "removed")
        self.assertFalse(launcher_path.exists())

    def test_modified_owned_obsolete_flow_review_launcher_is_preserved_and_reported(self) -> None:
        home = self.tmp_path / "home-obsolete-review-preserved"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        launcher_path = install_dir / "flow-review"
        launcher_text = render_posix_launcher(
            repo_root=self.repo_root,
            python_executable=Path("/usr/bin/python3"),
            command_name="flow-review",
            flow_direct_command="review",
        )
        launcher_path.write_text(launcher_text + "# user change\n", encoding="utf-8")
        launcher_path.chmod(0o755)
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(launcher_path): hashlib.sha256(launcher_text.encode("utf-8")).hexdigest()},
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        preserved = [item for item in result.launcher_statuses if item.path == launcher_path]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].state, "preserved-divergent")
        self.assertTrue(launcher_path.exists())

    def test_custom_prefix_obsolete_review_launchers_follow_same_cleanup_rules(self) -> None:
        for divergent in (False, True):
            with self.subTest(divergent=divergent):
                home = self.tmp_path / f"home-obsolete-custom-{int(divergent)}"
                install_dir = home / ".local" / "bin"
                install_dir.mkdir(parents=True, exist_ok=True)

                launcher_path = install_dir / "ai-flow-review"
                launcher_text = render_posix_launcher(
                    repo_root=self.repo_root,
                    python_executable=Path("/usr/bin/python3"),
                    command_name="ai-flow-review",
                    flow_direct_command="review",
                )
                file_text = launcher_text + ("# user change\n" if divergent else "")
                launcher_path.write_text(file_text, encoding="utf-8")
                launcher_path.chmod(0o755)
                self._write_prefix_ownership_record(
                    home=home,
                    selected_prefix="ai-flow",
                    platform="posix",
                    install_directory=install_dir,
                    owned_launchers={str(launcher_path): hashlib.sha256(launcher_text.encode("utf-8")).hexdigest()},
                )

                result = run_bootstrap(
                    platform="posix",
                    repo_root=self.repo_root,
                    prefix="ai-flow",
                    install_directory=install_dir,
                    home=home,
                    shell_program="/bin/bash",
                    config_path=self.config_path,
                )

                statuses = [item for item in result.launcher_statuses if item.path == launcher_path]
                self.assertEqual(len(statuses), 1)
                self.assertEqual(
                    statuses[0].state,
                    "preserved-divergent" if divergent else "removed",
                )
                self.assertEqual(launcher_path.exists(), divergent)

    def test_unrelated_review_like_files_remain_untouched_during_obsolete_cleanup(self) -> None:
        home = self.tmp_path / "home-obsolete-review-unrelated"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)

        obsolete_path = install_dir / "flow-review"
        obsolete_text = render_posix_launcher(
            repo_root=self.repo_root,
            python_executable=Path("/usr/bin/python3"),
            command_name="flow-review",
            flow_direct_command="review",
        )
        obsolete_path.write_text(obsolete_text, encoding="utf-8")
        obsolete_path.chmod(0o755)

        unrelated_a = install_dir / "flow-review-custom"
        unrelated_b = install_dir / "ai-flow-review-custom"
        unrelated_a.write_text("user script a\n", encoding="utf-8")
        unrelated_b.write_text("user script b\n", encoding="utf-8")

        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(obsolete_path): hashlib.sha256(obsolete_text.encode("utf-8")).hexdigest()},
        )

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertFalse(obsolete_path.exists())
        self.assertEqual(unrelated_a.read_text(encoding="utf-8"), "user script a\n")
        self.assertEqual(unrelated_b.read_text(encoding="utf-8"), "user script b\n")

    def test_obsolete_launcher_with_invalid_utf8_bytes_is_preserved_and_reported(self) -> None:
        home = self.tmp_path / "home-prefix-invalid-utf8"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        modified = install_dir / "flow-status"
        modified.unlink()
        modified.write_bytes(b"#!/usr/bin/env sh\n\xff\xfeinvalid\n")

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertTrue(modified.exists())
        preserved = [item for item in second.launcher_statuses if item.path == modified]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].state, "preserved-divergent")

    def test_unrelated_similarly_named_files_are_preserved(self) -> None:
        home = self.tmp_path / "home-prefix-unrelated"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        unrelated_a = install_dir / "flow-custom"
        unrelated_b = install_dir / "ai-flow-custom"
        unrelated_a.write_text("user script\n", encoding="utf-8")
        unrelated_b.write_text("another user script\n", encoding="utf-8")

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        self.assertEqual(unrelated_a.read_text(encoding="utf-8"), "user script\n")
        self.assertEqual(unrelated_b.read_text(encoding="utf-8"), "another user script\n")

    def test_prefix_same_reinstall_is_idempotent(self) -> None:
        home = self.tmp_path / "home-prefix-idempotent"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        states = [item.state for item in second.launcher_statuses]
        self.assertTrue(states)
        self.assertTrue(all(state == "up-to-date" for state in states))

    def test_prefix_ownership_record_updates_after_successful_reconciliation(self) -> None:
        home = self.tmp_path / "home-prefix-record"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=home)
        first_text = record_path.read_text(encoding="utf-8")
        self.assertIn('"selected_prefix": "flow"', first_text)

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        second_text = record_path.read_text(encoding="utf-8")
        self.assertIn('"selected_prefix": "ai-flow"', second_text)

    def test_malformed_record_rejects_path_outside_install_directory(self) -> None:
        home = self.tmp_path / "home-record-outside"
        install_dir = home / ".local" / "bin"
        outside_path = self.tmp_path / "outside" / "flow-status"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(outside_path): "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_malformed_record_rejects_non_normalized_path(self) -> None:
        home = self.tmp_path / "home-record-nonnormal"
        install_dir = home / ".local" / "bin"
        non_normalized = f"{install_dir}/subdir/../flow-status"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={non_normalized: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_malformed_record_rejects_different_prefix(self) -> None:
        home = self.tmp_path / "home-record-prefix"
        install_dir = home / ".local" / "bin"
        mismatched = install_dir / "ai-flow-status"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(mismatched): "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_malformed_record_rejects_unknown_command(self) -> None:
        home = self.tmp_path / "home-record-command"
        install_dir = home / ".local" / "bin"
        unknown = install_dir / "flow-unknown"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(unknown): "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_malformed_record_rejects_invalid_platform_extension(self) -> None:
        home = self.tmp_path / "home-record-ext"
        install_dir = home / ".local" / "bin"
        bad_ext = install_dir / "flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="posix",
            install_directory=install_dir,
            owned_launchers={str(bad_ext): "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_accepts_valid_cmd_launcher(self) -> None:
        home = self.tmp_path / "home-record-windows-valid"
        install_directory = r"C:\Users\Example\.local\bin"
        launcher_path = r"C:\Users\Example\.local\bin\flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={launcher_path: "0" * 64},
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=home / ".local" / "bin",
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        self.assertTrue(any(item.path.name == "flow-status" for item in result.launcher_statuses))

    def test_windows_record_accepts_valid_ps1_launcher(self) -> None:
        home = self.tmp_path / "home-record-windows-valid-ps1"
        install_directory = r"C:\Users\Example\.local\bin"
        launcher_path = r"C:\Users\Example\.local\bin\flow-diff.ps1"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={launcher_path: "0" * 64},
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=home / ".local" / "bin",
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        self.assertTrue(any(item.path.name == "flow-status" for item in result.launcher_statuses))

    def test_windows_record_accepts_case_insensitive_prefix_command_and_extension(self) -> None:
        home = self.tmp_path / "home-record-windows-casing"
        install_directory = r"C:\Users\Example\.local\bin"
        launchers = {
            r"C:\Users\Example\.local\bin\Flow-Status.CMD": "0" * 64,
            r"C:\Users\Example\.local\bin\FLOW-DIFF.ps1": "1" * 64,
        }
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers=launchers,
        )

        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=home / ".local" / "bin",
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        self.assertTrue(any(item.path.name == "flow-status" for item in result.launcher_statuses))

    def test_windows_record_rejects_path_outside_recorded_install_directory(self) -> None:
        home = self.tmp_path / "home-record-windows-outside"
        install_directory = r"C:\Users\Example\.local\bin"
        outside_path = r"C:\Users\Example\Other\flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={outside_path: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_rejects_path_below_recorded_install_directory(self) -> None:
        home = self.tmp_path / "home-record-windows-below"
        install_directory = r"C:\Users\Example\.local\bin"
        nested_path = r"C:\Users\Example\.local\bin\nested\flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={nested_path: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_rejects_non_normalized_windows_path(self) -> None:
        home = self.tmp_path / "home-record-windows-nonnormal"
        install_directory = r"C:\Users\Example\.local\bin"
        non_normalized = r"C:\Users\Example\.local\bin\sub\..\flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={non_normalized: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_rejects_mismatched_prefix(self) -> None:
        home = self.tmp_path / "home-record-windows-prefix"
        install_directory = r"C:\Users\Example\.local\bin"
        mismatched = r"C:\Users\Example\.local\bin\ai-flow-status.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={mismatched: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_rejects_unknown_command(self) -> None:
        home = self.tmp_path / "home-record-windows-command"
        install_directory = r"C:\Users\Example\.local\bin"
        unknown = r"C:\Users\Example\.local\bin\flow-unknown.cmd"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={unknown: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_windows_record_rejects_invalid_windows_launcher_extension(self) -> None:
        home = self.tmp_path / "home-record-windows-extension"
        install_directory = r"C:\Users\Example\.local\bin"
        bad_extension = r"C:\Users\Example\.local\bin\flow-status.bat"
        self._write_prefix_ownership_record(
            home=home,
            selected_prefix="flow",
            platform="windows",
            install_directory=install_directory,
            owned_launchers={bad_extension: "0" * 64},
        )

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                prefix="flow",
                install_directory=home / ".local" / "bin",
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

    def test_prefixed_launchers_show_direct_help_and_forward_exit_codes(self) -> None:
        home = self.tmp_path / "home-prefix-exec"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            launcher = install_dir / f"flow-{command}"
            with self.subTest(command=command):
                help_run = subprocess.run(
                    [str(launcher), "--help"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    cwd=str(self.tmp_path),
                )
                self.assertEqual(help_run.returncode, 0)
                self.assertIn(f"Usage: flow-{command}", help_run.stdout)
                self.assertNotIn(f"Usage: flow-{command} {command}", help_run.stdout)

        malformed_arguments: dict[str, list[str]] = {
            "start": [],
            "patch": [],
            "status": ["--bogus"],
            "diff": ["--bogus"],
            "commit": ["extra"],
            "reset": ["extra"],
            "promote": [],
            "complete": ["extra"],
            "block": [],
            "resume": [],
            "ticket-create": [],
            "ticket-show": [],
            "ticket-query": ["--bogus"],
        }

        for command, arguments in malformed_arguments.items():
            launcher = install_dir / f"flow-{command}"
            with self.subTest(malformed_usage=command):
                failure_run = subprocess.run(
                    [str(launcher), *arguments],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    cwd=str(self.tmp_path),
                )
                self.assertNotEqual(failure_run.returncode, 0)
                self.assertIn(f"Usage: flow-{command}", failure_run.stderr)
                self.assertNotIn(f"Usage: flow-{command} {command}", failure_run.stderr)

        status_launcher = install_dir / "flow-status"
        status_run = subprocess.run(
            [str(status_launcher), "--verbose"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
        )
        self.assertEqual(status_run.returncode, 0)
        self.assertIn("Workflow:", status_run.stdout)

    def test_ticket_launchers_help_do_not_repeat_command_name(self) -> None:
        home = self.tmp_path / "home-prefix-ticket-help"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        for command in ("ticket-create", "ticket-show", "ticket-query"):
            launcher = install_dir / f"flow-{command}"
            with self.subTest(command=command):
                help_run = subprocess.run(
                    [str(launcher), "--help"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    cwd=str(self.tmp_path),
                )
                self.assertEqual(help_run.returncode, 0)
                self.assertEqual(help_run.stderr, "")
                self.assertIn(f"Usage: flow-{command}", help_run.stdout)
                self.assertNotIn(f"Usage: flow-{command} {command}", help_run.stdout)

    def test_custom_prefix_ticket_create_help_uses_name_once(self) -> None:
        home = self.tmp_path / "home-prefix-ticket-help-custom"
        install_dir = home / ".local" / "bin"

        run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="ai-flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        launcher = install_dir / "ai-flow-ticket-create"
        help_run = subprocess.run(
            [str(launcher), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.tmp_path),
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertEqual(help_run.stderr, "")
        self.assertIn("Usage: ai-flow-ticket-create", help_run.stdout)
        self.assertNotIn("Usage: ai-flow-ticket-create ticket-create", help_run.stdout)


if __name__ == "__main__":
    unittest.main()