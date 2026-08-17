from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.bootstrap import cleanup_managed_launchers, resolve_prefix_launcher_ownership_path


class LauncherCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.home = self.tmp_path / "home"
        self.install_dir = self.home / ".local" / "bin"
        self.install_dir.mkdir(parents=True)
        self.targets = self.tmp_path / "targets"
        self.targets.mkdir()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_record(self, owned_launchers: dict[str, str]) -> Path:
        record_path = resolve_prefix_launcher_ownership_path(os_name="posix", home=self.home)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "selected_prefix": "flow",
                    "platform": "posix",
                    "install_directory": str(self.install_dir),
                    "owned_launchers": owned_launchers,
                }
            ),
            encoding="utf-8",
        )
        return record_path

    def test_cleanup_removes_only_proven_owned_launchers(self) -> None:
        start_target = self.targets / "flow-start"
        expected_status_target = self.targets / "flow-status"
        divergent_target = self.targets / "custom-status"
        for target in (start_target, expected_status_target, divergent_target):
            target.write_text("target\n", encoding="utf-8")
        start = self.install_dir / "flow-start"
        status = self.install_dir / "flow-status"
        unrelated = self.install_dir / "flow-custom"
        start.symlink_to(start_target)
        status.symlink_to(divergent_target)
        unrelated.write_text("preserve\n", encoding="utf-8")
        record_path = self._write_record(
            {
                str(start): f"symlink:{start_target}",
                str(status): f"symlink:{expected_status_target}",
            }
        )

        result = cleanup_managed_launchers(platform="posix", home=self.home)

        states = {item.path.name: item.state for item in result.launcher_statuses}
        self.assertEqual(states, {"flow-start": "removed", "flow-status": "preserved-divergent"})
        self.assertFalse(start.exists())
        self.assertTrue(status.is_symlink())
        self.assertEqual(status.resolve(), divergent_target)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve\n")
        remaining = json.loads(record_path.read_text(encoding="utf-8"))["owned_launchers"]
        self.assertEqual(remaining, {str(status): f"symlink:{expected_status_target}"})

    def test_cleanup_without_ownership_record_preserves_matching_names(self) -> None:
        unrelated = self.install_dir / "flow-start"
        unrelated.write_text("user command\n", encoding="utf-8")

        result = cleanup_managed_launchers(platform="posix", home=self.home)

        self.assertEqual(result.launcher_statuses, ())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user command\n")


if __name__ == "__main__":
    unittest.main()