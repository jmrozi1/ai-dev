from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ai_dev_flow.bootstrap import (
    BootstrapError,
    resolve_prefix_launcher_ownership_path,
    run_bootstrap,
)
from ai_dev_flow.cli import FIXED_FLOW_EXECUTABLE_COMMANDS


class BootstrapSkillSymlinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        source_repo_root = Path(__file__).resolve().parents[1]
        self.repo_root = self.tmp_path / "repo-fixture"
        shutil.copytree(source_repo_root / "ai_dev_flow", self.repo_root / "ai_dev_flow")
        shutil.copytree(source_repo_root / "tools" / "bootstrap", self.repo_root / "tools" / "bootstrap")
        shutil.copytree(source_repo_root / "skills" / "copilot" / "flow" / "scripts", self.repo_root / "skills" / "copilot" / "flow" / "scripts")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_posix_bootstrap(self, *, home: Path, install_dir: Path):
        return run_bootstrap(
            platform="posix",
            repo_root=self.repo_root,
            prefix="flow",
            install_directory=install_dir,
            home=home,
            shell_program="/bin/bash",
            interactive=False,
        )

    def test_missing_link_creation(self) -> None:
        home = self.tmp_path / "home-create"
        install_dir = home / ".local" / "bin"

        result = self._run_posix_bootstrap(home=home, install_dir=install_dir)

        states = {item.path.name: item.state for item in result.launcher_statuses}
        for command in FIXED_FLOW_EXECUTABLE_COMMANDS:
            launcher = install_dir / f"flow-{command}"
            self.assertTrue(launcher.exists())
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(states[f"flow-{command}"], "installed")

    def test_correct_link_noop(self) -> None:
        home = self.tmp_path / "home-noop"
        install_dir = home / ".local" / "bin"

        self._run_posix_bootstrap(home=home, install_dir=install_dir)
        second = self._run_posix_bootstrap(home=home, install_dir=install_dir)

        expected_paths = {install_dir / f"flow-{command}" for command in FIXED_FLOW_EXECUTABLE_COMMANDS}
        states = [
            item.state
            for item in second.launcher_statuses
            if item.path in expected_paths
        ]
        self.assertEqual(len(states), len(FIXED_FLOW_EXECUTABLE_COMMANDS))
        self.assertTrue(all(state == "up-to-date" for state in states))

    def test_managed_stale_or_broken_link_repair(self) -> None:
        home = self.tmp_path / "home-repair"
        install_dir = home / ".local" / "bin"

        self._run_posix_bootstrap(home=home, install_dir=install_dir)

        launcher = install_dir / "flow-start"
        stale_target = home / "stale-managed-target"
        launcher.unlink()

        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=home)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["owned_launchers"][str(launcher)] = f"symlink:{stale_target.resolve()}"
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        launcher.symlink_to(stale_target)

        repaired = self._run_posix_bootstrap(home=home, install_dir=install_dir)
        repaired_states = {item.path.name: item.state for item in repaired.launcher_statuses}
        self.assertEqual(repaired_states["flow-start"], "updated")

        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertIn(str(launcher), record["owned_launchers"])
        self.assertTrue(record["owned_launchers"][str(launcher)].startswith("symlink:"))

    def test_retargeted_managed_symlink_is_preserved_divergent(self) -> None:
        home = self.tmp_path / "home-retargeted"
        install_dir = home / ".local" / "bin"

        self._run_posix_bootstrap(home=home, install_dir=install_dir)

        launcher = install_dir / "flow-start"
        unrelated_target = home / "unrelated-target.sh"
        unrelated_target.write_text("#!/usr/bin/env sh\necho unrelated\n", encoding="utf-8")

        launcher.unlink()
        launcher.symlink_to(unrelated_target)

        with self.assertRaises(BootstrapError):
            self._run_posix_bootstrap(home=home, install_dir=install_dir)

        self.assertTrue(launcher.is_symlink())
        self.assertEqual(launcher.resolve(), unrelated_target.resolve())

    def test_unrelated_existing_file_fails_closed(self) -> None:
        home = self.tmp_path / "home-unrelated"
        install_dir = home / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        collision = install_dir / "flow-start"
        collision.write_text("#!/usr/bin/env sh\necho custom\n", encoding="utf-8")

        with self.assertRaises(BootstrapError):
            self._run_posix_bootstrap(home=home, install_dir=install_dir)

        self.assertEqual(collision.read_text(encoding="utf-8"), "#!/usr/bin/env sh\necho custom\n")

    def test_visible_failure_when_target_disappears(self) -> None:
        home = self.tmp_path / "home-fail-loud"
        install_dir = home / ".local" / "bin"

        self._run_posix_bootstrap(home=home, install_dir=install_dir)
        launcher = install_dir / "flow-start"
        self.assertTrue(launcher.is_symlink())

        target = Path(launcher.resolve(strict=False))
        backup = target.with_suffix(target.suffix + ".bak-test")
        target.rename(backup)
        try:
            with self.assertRaises(FileNotFoundError):
                subprocess.run(
                    [str(launcher), "--help"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
        finally:
            backup.rename(target)

    def test_exact_thirteen_command_surface_installed(self) -> None:
        home = self.tmp_path / "home-surface"
        install_dir = home / ".local" / "bin"

        self._run_posix_bootstrap(home=home, install_dir=install_dir)

        expected_names = {f"flow-{command}" for command in FIXED_FLOW_EXECUTABLE_COMMANDS}
        actual_names = {path.name for path in install_dir.iterdir()}
        self.assertEqual(actual_names, expected_names)
        for name in expected_names:
            self.assertTrue((install_dir / name).is_symlink())


if __name__ == "__main__":
    unittest.main()
