from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.bootstrap import (
    BootstrapError,
    OWNERSHIP_MARKER,
    _render_path_guidance,
    _ensure_owned_launcher_text,
    _is_path_on_path,
    _paths_equal,
    render_cmd_launcher,
    render_posix_launcher,
    render_powershell_launcher,
    run_bootstrap,
)
from ai_dev_flow.update_installation import load_installation_source_record


class BootstrapCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.config_path = self.tmp_path / "cfg" / "config.yaml"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

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

    def test_owned_launcher_detection(self) -> None:
        _ensure_owned_launcher_text(f"#!/usr/bin/env sh\n# {OWNERSHIP_MARKER}\n", Path("/tmp/x"))
        _ensure_owned_launcher_text(f"# {OWNERSHIP_MARKER}\nWrite-Output 'ok'\n", Path("/tmp/x.ps1"))
        _ensure_owned_launcher_text(f"@echo off\n:: {OWNERSHIP_MARKER}\n", Path("/tmp/x.cmd"))
        with self.assertRaises(BootstrapError):
            _ensure_owned_launcher_text("#!/usr/bin/env sh\n# not-owned\n", Path("/tmp/y"))
        with self.assertRaises(BootstrapError):
            _ensure_owned_launcher_text(
                f"#!/usr/bin/env sh\necho {OWNERSHIP_MARKER}\n",
                Path("/tmp/z"),
            )

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
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("aliases: {}\n", encoding="utf-8")

        first = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value="/usr/bin:/bin",
        )
        self.assertFalse(first.config_created)
        launcher_path = install_dir / "ai-dev"
        self.assertTrue(launcher_path.exists())
        self.assertTrue(launcher_path.stat().st_mode & 0o111)

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value=f"{install_dir}:/usr/bin:/bin",
        )
        states = [item.state for item in second.launcher_statuses]
        self.assertEqual(states, ["up-to-date"])
        self.assertTrue(second.install_dir_on_path)

    def test_non_owned_collision_is_rejected(self) -> None:
        home = self.tmp_path / "home-collision"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "ai-dev"
        collision_path.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        with self.assertRaises(BootstrapError):
            run_bootstrap(
                platform="posix",
                repo_root=self.repo_root,
                command_name="ai-dev",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
            )

        self.assertEqual(collision_path.read_text(encoding="utf-8"), "#!/usr/bin/env sh\necho custom\n")

    def test_non_owned_collision_with_marker_mention_is_rejected(self) -> None:
        home = self.tmp_path / "home-collision-marker"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision_path = install_dir / "ai-dev"
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
                command_name="ai-dev",
                install_directory=install_dir,
                home=home,
                shell_program="/bin/bash",
                config_path=self.config_path,
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

    def test_invalid_command_name_rejected_before_filesystem_work(self) -> None:
        invalid_names = ["flow", "../x", "./ai-dev", "/tmp/x", "ai dev", " ai-dev", "ai-dev "]
        for invalid_name in invalid_names:
            with self.subTest(command_name=invalid_name):
                with self.assertRaises(BootstrapError) as ctx:
                    run_bootstrap(
                        platform="posix",
                        repo_root=self.tmp_path / "missing-repo",
                        command_name=invalid_name,
                        config_path=self.config_path,
                    )

                self.assertIn("Unsupported command name", str(ctx.exception))

    def test_windows_install_renders_cmd_and_ps1_without_eval(self) -> None:
        home = self.tmp_path / "home-win"
        install_dir = home / ".local" / "bin"
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
        self.assertFalse(result.install_dir_on_path)

        ps1_path = install_dir / "ai-dev.ps1"
        cmd_path = install_dir / "ai-dev.cmd"
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
        launcher_path = install_dir / "ai-dev"
        original = "#!/usr/bin/env sh\n# AI_DEV_LAUNCHER_V1\necho old\n"
        launcher_path.write_text(original, encoding="utf-8")
        launcher_path.chmod(0o755)

        with patch("ai_dev_flow.bootstrap._set_executable_if_needed", side_effect=RuntimeError("chmod fail")):
            with self.assertRaises(BootstrapError):
                run_bootstrap(
                    platform="posix",
                    repo_root=self.repo_root,
                    command_name="ai-dev",
                    install_directory=install_dir,
                    home=home,
                    shell_program="/bin/bash",
                    config_path=self.config_path,
                )

        self.assertEqual(launcher_path.read_text(encoding="utf-8"), original)

    def test_posix_launcher_end_to_end_executes_and_forwards_exit_codes(self) -> None:
        home = self.tmp_path / "home-e2e"
        install_dir = home / "bin"
        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            path_value="/usr/bin:/bin",
        )
        self.assertEqual(result.command_name, "ai-dev")

        launcher = install_dir / "ai-dev"
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
        self.assertIn("config", help_run.stdout)

        invalid_token = "not-a-real-ai-dev-command"
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
        self.assertIn(invalid_token, invalid_run.stderr)

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

    def test_bootstrap_records_installation_source_metadata(self) -> None:
        home = self.tmp_path / "home-meta"
        install_dir = home / ".local" / "bin"
        result = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
            update_branch="main",
            update_remote="origin",
        )

        record = load_installation_source_record(result.installation_source_path)
        self.assertTrue(result.installation_source_path.exists())
        self.assertEqual(record.source_repository, self.repo_root.resolve())
        self.assertEqual(record.branch, "main")
        self.assertEqual(record.remote, "origin")

    def test_bootstrap_metadata_refresh_is_idempotent(self) -> None:
        home = self.tmp_path / "home-meta-repeat"
        install_dir = home / ".local" / "bin"

        first = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        first_text = first.installation_source_path.read_text(encoding="utf-8")

        second = run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )
        second_text = second.installation_source_path.read_text(encoding="utf-8")

        self.assertEqual(first.installation_source_path, second.installation_source_path)
        self.assertEqual(first_text, second_text)

    def test_bootstrap_records_canonical_git_root_from_symlink(self) -> None:
        home = self.tmp_path / "home-subdir"
        install_dir = home / ".local" / "bin"
        symlink_root = self.tmp_path / "repo-link"
        symlink_root.symlink_to(self.repo_root, target_is_directory=True)

        result = run_bootstrap(
            platform="posix",
            repo_root=symlink_root,
            command_name="ai-dev",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            config_path=self.config_path,
        )

        record = load_installation_source_record(result.installation_source_path)
        self.assertEqual(record.source_repository, self.repo_root.resolve())


if __name__ == "__main__":
    unittest.main()