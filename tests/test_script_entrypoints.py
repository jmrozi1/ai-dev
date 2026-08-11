from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class ScriptEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo_root = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_fake_python(self, path: Path, *, version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ ${1:-} == '-c' ]]; then\n"
            f"  printf '%s\\n' '{version}'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ ${1:-} == '-m' ]]; then\n"
            "  if [[ -n ${AI_DEV_TEST_SELECTED_LOG:-} ]]; then\n"
            "    printf '%s %s\\n' \"$0\" \"$*\" >> \"$AI_DEV_TEST_SELECTED_LOG\"\n"
            "  fi\n"
            "  if [[ -n ${AI_DEV_TEST_FORCE_EXIT:-} ]]; then\n"
            "    exit \"$AI_DEV_TEST_FORCE_EXIT\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_shell_test(self, path: Path, *, exit_code: int = 0, marker_text: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        marker_statement = ""
        if marker_text is not None:
            marker_statement = (
                "if [[ -n ${AI_DEV_TEST_SHELL_MARKER:-} ]]; then\n"
                f"  printf '%s\\n' '{marker_text}' >> \"$AI_DEV_TEST_SHELL_MARKER\"\n"
                "fi\n"
            )
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"{marker_statement}"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _run_test_sh(self, *arguments: str, env_updates: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script = self.repo_root / "scripts" / "test.sh"
        return subprocess.run(
            ["bash", str(script), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), **(env_updates or {})},
        )

    def _run_test_ps1(self, *arguments: str, env_updates: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            raise RuntimeError("pwsh is not available")

        script = self.repo_root / "scripts" / "test.ps1"
        return subprocess.run(
            [pwsh, "-NoProfile", "-File", str(script), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), **(env_updates or {})},
        )

    def _run_install_with_fake_path(self, *, env_updates: dict[str, str], arguments: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        script = self.repo_root / "scripts" / "install.sh"
        command = ["bash", str(script)]
        if arguments:
            command.extend(arguments)
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), **env_updates},
        )
        return completed

    def _log_line_selected_python(self, line: str) -> str:
        marker = " -m "
        if marker in line:
            return line.split(marker, 1)[0]
        return line

    def test_install_sh_help(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-help"
        home_root.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["bash", str(script), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), "HOME": str(home_root)},
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: scripts/install.sh [bootstrap-options]", completed.stdout)
        self.assertIn("--force", completed.stdout)
        self.assertIn("-v", completed.stdout)
        self.assertIn("--verbose", completed.stdout)
        self.assertIn("--prefix", completed.stdout)
        self.assertIn("--home", completed.stdout)
        self.assertIn("--install-dir", completed.stdout)
        self.assertIn("flow-ticket-create", completed.stdout)
        self.assertIn("flow-ticket-show", completed.stdout)
        self.assertIn("flow-ticket-query", completed.stdout)
        self.assertFalse((home_root / ".local" / "bin").exists())

    def test_install_sh_custom_prefix_overrides_default_flow_prefix(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        fake_bin = self.tmp_path / "fake-bin-prefix"
        selected_log = self.tmp_path / "selected-prefix.log"
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        completed = subprocess.run(
            ["bash", str(script), "--prefix", "ai-flow"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(self.tmp_path / "home-prefix"),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

        selected = selected_log.read_text(encoding="utf-8")
        self.assertIn(" --prefix ai-flow", selected)

    def test_install_sh_verbose_does_not_forward_verbose_flags(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        fake_bin = self.tmp_path / "fake-bin-verbose"
        selected_log = self.tmp_path / "selected-verbose.log"
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        completed = subprocess.run(
            ["bash", str(script), "--prefix", "ai-flow", "-v"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(self.tmp_path / "home-verbose-forward"),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

        selected = selected_log.read_text(encoding="utf-8")
        self.assertIn(" --prefix ai-flow", selected)
        self.assertIn(" --installer-output detailed", selected)
        self.assertNotIn(" -v", selected)
        self.assertNotIn(" --verbose", selected)

    def test_install_sh_forwards_home_and_install_dir_options(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        fake_bin = self.tmp_path / "fake-bin-home"
        selected_log = self.tmp_path / "selected-home.log"
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        forwarded_home = self.tmp_path / "forwarded home"
        forwarded_install_dir = self.tmp_path / "forwarded bin"
        completed = subprocess.run(
            [
                "bash",
                str(script),
                "--home",
                str(forwarded_home),
                "--install-dir",
                str(forwarded_install_dir),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(self.tmp_path / "home-installer"),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)

        selected = selected_log.read_text(encoding="utf-8")
        self.assertIn(f" --home {forwarded_home}", selected)
        self.assertIn(f" --install-dir {forwarded_install_dir}", selected)

    def test_install_sh_runs_bootstrap_and_handles_space_paths(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home with spaces"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = subprocess.run(
            [
                "bash",
                str(script),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_root),
                "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertTrue((install_dir / "flow-status").exists())
        self.assertEqual(completed.stdout.strip(), "AI Dev installation completed successfully.")
        self.assertNotIn("Platform:", completed.stdout)
        self.assertNotIn("Launcher:", completed.stdout)

    def test_install_sh_verbose_short_prints_detailed_report(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-verbose-short"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = subprocess.run(
            ["bash", str(script), "-v"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_root),
                "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("Bootstrap complete.", completed.stdout)
        self.assertIn("Platform:", completed.stdout)
        self.assertIn("Launcher:", completed.stdout)

    def test_install_sh_verbose_long_is_equivalent_to_short(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_short = self.tmp_path / "home-verbose-short-equivalent"
        home_long = self.tmp_path / "home-verbose-long-equivalent"
        install_short = home_short / ".local" / "bin"
        install_long = home_long / ".local" / "bin"
        home_short.mkdir(parents=True, exist_ok=True)
        home_long.mkdir(parents=True, exist_ok=True)

        short = subprocess.run(
            ["bash", str(script), "-v"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_short),
                "PATH": f"{install_short}:{os.environ.get('PATH', '')}",
            },
        )
        long = subprocess.run(
            ["bash", str(script), "--verbose"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_long),
                "PATH": f"{install_long}:{os.environ.get('PATH', '')}",
            },
        )

        self.assertEqual(short.returncode, 0, msg=short.stderr)
        self.assertEqual(long.returncode, 0, msg=long.stderr)
        self.assertIn("Bootstrap complete.", short.stdout)
        self.assertIn("Bootstrap complete.", long.stdout)
        self.assertIn("Platform:", short.stdout)
        self.assertIn("Platform:", long.stdout)

    def test_install_sh_force_short_and_long_are_equivalent(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        fake_bin = self.tmp_path / "fake-bin-force"
        selected_log = self.tmp_path / "selected-force.log"
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        home_short = self.tmp_path / "home-force-short"
        home_long = self.tmp_path / "home-force-long"
        home_short.mkdir(parents=True, exist_ok=True)
        home_long.mkdir(parents=True, exist_ok=True)

        completed_short = subprocess.run(
            ["bash", str(script), "-f"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_short),
            },
        )
        completed_long = subprocess.run(
            ["bash", str(script), "--force"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_long),
            },
        )

        self.assertEqual(completed_short.returncode, 0, msg=completed_short.stderr)
        self.assertEqual(completed_long.returncode, 0, msg=completed_long.stderr)

        selected = selected_log.read_text(encoding="utf-8")
        self.assertIn(" --force", selected)

    def test_install_sh_force_and_verbose_combines_correctly(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-force-verbose"
        install_dir = home_root / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "flow-start").write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        completed = subprocess.run(
            ["bash", str(script), "-f", "-v"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_root),
                "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("Bootstrap complete.", completed.stdout)
        self.assertIn("Launcher:", completed.stdout)
        self.assertIn("Force-replacing conflicting launcher", completed.stderr)

    def test_install_sh_noninteractive_conflict_refuses_and_preserves_original(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-decline"
        install_dir = home_root / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        conflict = install_dir / "flow-start"
        original = "#!/usr/bin/env sh\necho custom\n"
        conflict.write_text(original, encoding="utf-8")

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_root),
                "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("AI Dev installation failed:", completed.stderr)
        self.assertIn("Cannot prompt to replace conflicting launcher", completed.stderr)
        self.assertEqual(conflict.read_text(encoding="utf-8"), original)

    def test_install_sh_force_replacement_succeeds_and_reports_warning_status(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-force-warning"
        install_dir = home_root / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "flow-start").write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        completed = subprocess.run(
            ["bash", str(script), "-f"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={
                **dict(os.environ),
                "AI_DEV_PYTHON": sys.executable,
                "HOME": str(home_root),
                "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
            },
        )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("AI Dev installation completed with warnings.", completed.stdout)
        self.assertIn("Warning: Force-replaced conflicting launcher", completed.stdout)

    def test_install_sh_second_install_up_to_date_prints_single_success_line(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        home_root = self.tmp_path / "home-second-install"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)
        env = {
            **dict(os.environ),
            "AI_DEV_PYTHON": sys.executable,
            "HOME": str(home_root),
            "PATH": f"{install_dir}:{os.environ.get('PATH', '')}",
        }

        first = subprocess.run(
            ["bash", str(script)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env=env,
        )
        second = subprocess.run(
            ["bash", str(script)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env=env,
        )

        self.assertEqual(first.returncode, 0, msg=first.stderr)
        self.assertEqual(second.returncode, 0, msg=second.stderr)
        self.assertEqual(first.stdout.strip(), "AI Dev installation completed successfully.")
        self.assertEqual(second.stdout.strip(), "AI Dev installation completed successfully.")

    def test_install_sh_failure_output_is_concise_and_nonzero(self) -> None:
        script = self.repo_root / "scripts" / "install.sh"
        completed = subprocess.run(
            ["bash", str(script), "--prefix", "bad/prefix"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env={**dict(os.environ), "AI_DEV_PYTHON": sys.executable, "HOME": str(self.tmp_path / "home-fail")},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("AI Dev installation failed:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_install_sh_skips_incompatible_python3_for_python311(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3", version="3.6.15")
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_root),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        selected = selected_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(selected)
        self.assertTrue(self._log_line_selected_python(selected[-1]).endswith("python3.11"))

    def test_install_sh_honors_compatible_ai_dev_python(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")
        explicit_python = self.tmp_path / "custom python" / "python3.12"
        self._write_fake_python(explicit_python, version="3.12.4")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_PYTHON": str(explicit_python),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_root),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        selected = selected_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(selected)
        self.assertEqual(self._log_line_selected_python(selected[-1]), str(explicit_python))

    def test_install_sh_accepts_python38(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3.13", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.12", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.11", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.10", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.9", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.8", version="3.8.18")
        self._write_fake_python(fake_bin / "python3", version="3.8.18")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_root),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        selected = selected_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(selected)
        self.assertTrue(self._log_line_selected_python(selected[-1]).endswith("python3.8"))

    def test_install_sh_accepts_python39(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3.13", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.12", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.11", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.10", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.9", version="3.9.19")
        self._write_fake_python(fake_bin / "python3.8", version="3.8.18")
        self._write_fake_python(fake_bin / "python3", version="3.9.19")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_root),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        selected = selected_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(selected)
        self.assertTrue(self._log_line_selected_python(selected[-1]).endswith("python3.9"))

    def test_install_sh_incompatible_ai_dev_python_fails_without_fallback(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        incompatible = self.tmp_path / "bad python" / "python3"
        self._write_fake_python(incompatible, version="3.6.15")
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_PYTHON": str(incompatible),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "HOME": str(home_root),
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("AI_DEV_PYTHON", completed.stderr)
        self.assertIn("Minimum supported Python version", completed.stderr)
        self.assertIn("3.8.0", completed.stderr)
        self.assertFalse(selected_log.exists())

    def test_install_sh_no_compatible_interpreter_reports_rejected_versions(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        self._write_fake_python(fake_bin / "python3.13", version="3.6.15")
        self._write_fake_python(fake_bin / "python3.12", version="3.6.15")
        self._write_fake_python(fake_bin / "python3.11", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.10", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.9", version="3.7.17")
        self._write_fake_python(fake_bin / "python3.8", version="3.7.17")
        self._write_fake_python(fake_bin / "python3", version="3.6.15")
        self._write_fake_python(fake_bin / "python", version="3.5.10")

        home_root = self.tmp_path / "home"
        install_dir = home_root / ".local" / "bin"
        home_root.mkdir(parents=True, exist_ok=True)

        completed = self._run_install_with_fake_path(
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "HOME": str(home_root),
            },
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("No compatible Python interpreter found", completed.stderr)
        self.assertIn("python3 ->", completed.stderr)
        self.assertIn("version 3.6.15", completed.stderr)
        self.assertIn("python3.11 ->", completed.stderr)
        self.assertIn("version 3.7.17", completed.stderr)
        self.assertIn("python ->", completed.stderr)
        self.assertIn("version 3.5.10", completed.stderr)

    def test_test_sh_uses_same_selection_policy(self) -> None:
        fake_bin = self.tmp_path / "fake-bin"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_bin / "python3", version="3.6.15")
        self._write_fake_python(fake_bin / "python3.11", version="3.11.9")

        flow_shell_dir = self.tmp_path / "flow-shell"
        bootstrap_shell_dir = self.tmp_path / "bootstrap-shell"
        self._write_shell_test(flow_shell_dir / "test-flow-smoke.sh")
        self._write_shell_test(bootstrap_shell_dir / "test-bootstrap-smoke.sh")

        completed = self._run_test_sh(
            "--all",
            env_updates={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "AI_DEV_TEST_FLOW_DIR": str(flow_shell_dir),
                "AI_DEV_TEST_BOOTSTRAP_DIR": str(bootstrap_shell_dir),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        selected = selected_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(selected)
        self.assertTrue(self._log_line_selected_python(selected[-1]).endswith("python3.11"))

    def test_test_sh_help(self) -> None:
        completed = self._run_test_sh("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: scripts/test.sh", completed.stdout)
        self.assertIn("unit", completed.stdout)
        self.assertIn("bootstrap", completed.stdout)
        self.assertIn("flow", completed.stdout)
        self.assertIn("integration", completed.stdout)
        self.assertIn("all", completed.stdout)
        self.assertIn("tests.test_script_entrypoints", completed.stdout)

    def test_test_sh_list(self) -> None:
        flow_shell_dir = self.tmp_path / "flow-shell"
        bootstrap_shell_dir = self.tmp_path / "bootstrap-shell"
        self._write_shell_test(flow_shell_dir / "test-flow-discovered.sh")
        self._write_shell_test(bootstrap_shell_dir / "test-bootstrap-discovered.sh")

        completed = self._run_test_sh(
            "--list",
            env_updates={
                "AI_DEV_TEST_FLOW_DIR": str(flow_shell_dir),
                "AI_DEV_TEST_BOOTSTRAP_DIR": str(bootstrap_shell_dir),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("unit:", completed.stdout)
        self.assertIn("bootstrap:", completed.stdout)
        self.assertIn("flow:", completed.stdout)
        self.assertIn("integration:", completed.stdout)
        self.assertIn("all:", completed.stdout)
        self.assertIn("test-flow-discovered.sh", completed.stdout)
        self.assertIn("test-bootstrap-discovered.sh", completed.stdout)

    def test_test_sh_unknown_suite_fails_with_usage(self) -> None:
        completed = self._run_test_sh("unknown-suite")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unknown suite", completed.stderr)
        self.assertIn("Usage: scripts/test.sh", completed.stderr)

    def test_test_sh_flow_rejects_forwarded_unittest_args(self) -> None:
        completed = self._run_test_sh("flow", "--", "-k", "smoke")
        self.assertEqual(completed.returncode, 2)
        self.assertIn('suite "flow" does not accept unittest args', completed.stderr)

    def test_test_sh_unit_suite_uses_expected_modules(self) -> None:
        fake_python = self.tmp_path / "fake python" / "python3.11"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_python, version="3.11.9")

        completed = self._run_test_sh(
            "unit",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        command_line = selected_log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("-m unittest tests.test_script_entrypoints tests.test_bootstrap tests.test_bootstrap_cli", command_line)

    def test_test_sh_integration_suite_uses_python_discovery(self) -> None:
        fake_python = self.tmp_path / "fake python" / "python3.11"
        selected_log = self.tmp_path / "selected.log"
        self._write_fake_python(fake_python, version="3.11.9")

        completed = self._run_test_sh(
            "integration",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        command_line = selected_log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("-m unittest discover -s tests -p test_*.py", command_line)

    def test_test_sh_bootstrap_suite_runs_python_and_shell(self) -> None:
        fake_python = self.tmp_path / "fake python" / "python3.11"
        selected_log = self.tmp_path / "selected.log"
        marker_file = self.tmp_path / "shell-marker.txt"
        bootstrap_shell_dir = self.tmp_path / "bootstrap shell"
        self._write_fake_python(fake_python, version="3.11.9")
        self._write_shell_test(
            bootstrap_shell_dir / "test-bootstrap-one.sh",
            marker_text="bootstrap-shell-ran",
        )

        completed = self._run_test_sh(
            "bootstrap",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "AI_DEV_TEST_BOOTSTRAP_DIR": str(bootstrap_shell_dir),
                "AI_DEV_TEST_SHELL_MARKER": str(marker_file),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        command_line = selected_log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("-m unittest tests.test_bootstrap tests.test_bootstrap_cli", command_line)
        self.assertIn("bootstrap-shell-ran", marker_file.read_text(encoding="utf-8"))

    def test_test_sh_flow_suite_discovers_and_runs_shell_scripts(self) -> None:
        flow_shell_dir = self.tmp_path / "flow shell"
        marker_file = self.tmp_path / "flow-marker.txt"
        self._write_shell_test(flow_shell_dir / "test-flow-b.sh", marker_text="flow-b")
        self._write_shell_test(flow_shell_dir / "test-flow-a.sh", marker_text="flow-a")

        completed = self._run_test_sh(
            "flow",
            env_updates={
                "AI_DEV_TEST_FLOW_DIR": str(flow_shell_dir),
                "AI_DEV_TEST_SHELL_MARKER": str(marker_file),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        marker_lines = marker_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(marker_lines, ["flow-a", "flow-b"])

    def test_test_sh_all_suite_runs_python_and_both_shell_suites(self) -> None:
        fake_python = self.tmp_path / "fake python" / "python3.11"
        selected_log = self.tmp_path / "selected.log"
        marker_file = self.tmp_path / "all-marker.txt"
        flow_shell_dir = self.tmp_path / "flow shell"
        bootstrap_shell_dir = self.tmp_path / "bootstrap shell"
        self._write_fake_python(fake_python, version="3.11.9")
        self._write_shell_test(flow_shell_dir / "test-flow-one.sh", marker_text="flow")
        self._write_shell_test(bootstrap_shell_dir / "test-bootstrap-one.sh", marker_text="bootstrap")

        completed = self._run_test_sh(
            "all",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_SELECTED_LOG": str(selected_log),
                "AI_DEV_TEST_FLOW_DIR": str(flow_shell_dir),
                "AI_DEV_TEST_BOOTSTRAP_DIR": str(bootstrap_shell_dir),
                "AI_DEV_TEST_SHELL_MARKER": str(marker_file),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        command_line = selected_log.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("-m unittest discover -s tests -p test_*.py", command_line)
        marker_lines = marker_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(marker_lines, ["bootstrap", "flow"])

    def test_test_sh_failure_propagates_from_python(self) -> None:
        fake_python = self.tmp_path / "fake python" / "python3.11"
        self._write_fake_python(fake_python, version="3.11.9")

        completed = self._run_test_sh(
            "unit",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_FORCE_EXIT": "9",
            },
        )
        self.assertEqual(completed.returncode, 9)

    def test_test_sh_failure_propagates_from_shell_suite(self) -> None:
        flow_shell_dir = self.tmp_path / "flow-shell"
        self._write_shell_test(flow_shell_dir / "test-flow-fail.sh", exit_code=7)

        completed = self._run_test_sh(
            "flow",
            env_updates={"AI_DEV_TEST_FLOW_DIR": str(flow_shell_dir)},
        )
        self.assertEqual(completed.returncode, 7)

    def test_test_sh_supports_paths_with_spaces(self) -> None:
        fake_python = self.tmp_path / "fake python path" / "python3.11"
        bootstrap_shell_dir = self.tmp_path / "bootstrap shell dir"
        marker_file = self.tmp_path / "marker file.txt"
        self._write_fake_python(fake_python, version="3.11.9")
        self._write_shell_test(bootstrap_shell_dir / "test-bootstrap-space.sh", marker_text="space-path-ok")

        completed = self._run_test_sh(
            "bootstrap",
            env_updates={
                "AI_DEV_PYTHON": str(fake_python),
                "AI_DEV_TEST_BOOTSTRAP_DIR": str(bootstrap_shell_dir),
                "AI_DEV_TEST_SHELL_MARKER": str(marker_file),
            },
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("space-path-ok", marker_file.read_text(encoding="utf-8"))

    def test_install_ps1_help(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")

        script = self.repo_root / "scripts" / "install.ps1"
        completed = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(script), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: scripts/install.ps1", completed.stdout)
        self.assertIn("python -m ai_dev_flow.bootstrap", completed.stdout)

    def test_test_ps1_help(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")

        completed = self._run_test_ps1("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage: scripts/test.ps1", completed.stdout)
        self.assertIn("unit", completed.stdout)
        self.assertIn("bootstrap", completed.stdout)
        self.assertIn("flow", completed.stdout)
        self.assertIn("integration", completed.stdout)
        self.assertIn("all", completed.stdout)
        self.assertIn("tests.test_script_entrypoints", completed.stdout)

    def test_test_ps1_list_includes_named_suites(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not available")

        completed = self._run_test_ps1("--list")
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("unit:", completed.stdout)
        self.assertIn("bootstrap:", completed.stdout)
        self.assertIn("flow:", completed.stdout)
        self.assertIn("integration:", completed.stdout)
        self.assertIn("all:", completed.stdout)

    def test_test_dispatcher_parity_policy_without_pwsh(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            self.skipTest("static parity policy check is only for no-pwsh environments")

        sh_text = (self.repo_root / "scripts" / "test.sh").read_text(encoding="utf-8")
        ps_text = (self.repo_root / "scripts" / "test.ps1").read_text(encoding="utf-8")

        for suite_name in ("unit", "bootstrap", "flow", "integration", "all"):
            self.assertIn(suite_name, sh_text)
            self.assertIn(suite_name, ps_text)

        self.assertIn("Get-Command bash", ps_text)
        self.assertIn("SKIP [", ps_text)
        self.assertIn("suite \"flow\" does not accept unittest args", sh_text)
        self.assertIn("suite \"flow\" does not accept unittest args", ps_text)

    def test_powershell_selector_policy_is_mock_covered_without_pwsh(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            self.skipTest("mock policy check is only for no-pwsh environments")

        policy_text = (self.repo_root / "tools" / "bootstrap" / "PythonSelection.ps1").read_text(encoding="utf-8")
        self.assertIn("[Version]'3.8.0'", policy_text)
        self.assertIn("AI_DEV_PYTHON", policy_text)
        self.assertIn("'python3.13'", policy_text)
        self.assertIn("'python3.12'", policy_text)
        self.assertIn("'python3.11'", policy_text)
        self.assertIn("'python3.10'", policy_text)
        self.assertIn("'python3.9'", policy_text)
        self.assertIn("'python3.8'", policy_text)
        self.assertIn("'python3'", policy_text)
        self.assertIn("'python'", policy_text)
        self.assertIn("No compatible Python interpreter found", policy_text)


if __name__ == "__main__":
    unittest.main()
