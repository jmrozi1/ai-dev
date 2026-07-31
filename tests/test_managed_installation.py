from __future__ import annotations

from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow.managed_installation import (
    InstallationConfigError,
    ManagedInstallationError,
    ManagedInstallationPaths,
    apply_installation_reconciliation,
    load_desired_installation_state,
)


class ManagedInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.config_path = self.tmp_path / "config.yaml"
        self.paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin",
            manifest_path=self.tmp_path / "state" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc",
            windows=False,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _load(self) -> object:
        return load_desired_installation_state(
            self.config_path,
            case_insensitive_names=False,
        )

    def _write_config(self, text: str) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(text, encoding="utf-8")

    def test_default_configuration_includes_managed_aliases_and_shell_path(self) -> None:
        self._write_config("")
        desired = self._load()
        self.assertTrue(desired.aliases_enabled)
        self.assertTrue(desired.shell_path_enabled)
        self.assertIn("flow", desired.alias_commands)
        self.assertIn("flow-commit", desired.alias_commands)

    def test_alias_name_validation(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      bad/name: \"ai-dev flow\"\n"
        )
        with self.assertRaises(InstallationConfigError):
            self._load()

    def test_empty_mapping_rejected(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"\"\n"
        )
        with self.assertRaises(InstallationConfigError):
            self._load()

    def test_duplicate_destination_rejected_on_windows_normalization(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      Flow: \"ai-dev flow\"\n"
            "      flow: \"ai-dev flow status\"\n"
        )
        with self.assertRaises(InstallationConfigError):
            load_desired_installation_state(self.config_path, case_insensitive_names=True)

    def test_launcher_creation_and_manifest(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        desired = self._load()

        summary = apply_installation_reconciliation(desired, paths=self.paths)

        launcher = self.paths.launcher_directory / "flow-status"
        self.assertEqual(summary.launchers_created, 1)
        self.assertTrue(launcher.exists())
        self.assertTrue(self.paths.manifest_path.exists())

    def test_launcher_update_when_mapping_changes(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        desired = self._load()
        apply_installation_reconciliation(desired, paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow review\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        desired2 = self._load()
        summary = apply_installation_reconciliation(desired2, paths=self.paths)

        self.assertEqual(summary.launchers_updated, 1)

    def test_idempotent_noop(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        desired = self._load()
        first = apply_installation_reconciliation(desired, paths=self.paths)
        second = apply_installation_reconciliation(desired, paths=self.paths)

        self.assertEqual(first.launchers_created, 1)
        self.assertEqual(second.launchers_created, 0)
        self.assertEqual(second.launchers_updated, 0)
        self.assertEqual(second.launchers_removed, 0)

    def test_stale_managed_launcher_removed(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      floow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)

        self.assertEqual(summary.launchers_removed, 1)
        self.assertTrue((self.paths.launcher_directory / "flow").exists())
        self.assertFalse((self.paths.launcher_directory / "floow").exists())

    def test_preserves_unrelated_launcher_files(self) -> None:
        unrelated = self.paths.launcher_directory / "my-tool"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("#!/usr/bin/env sh\n", encoding="utf-8")

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertTrue(unrelated.exists())

    def test_refuses_to_overwrite_unowned_existing_target(self) -> None:
        target = self.paths.launcher_directory / "flow-status"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("user file\n", encoding="utf-8")

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )

        with self.assertRaises(ManagedInstallationError):
            apply_installation_reconciliation(self._load(), paths=self.paths)

    def test_disabling_aliases_removes_managed_and_keeps_primary(self) -> None:
        primary = self.paths.launcher_directory / "ai-dev"
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_text("#!/usr/bin/env sh\n", encoding="utf-8")

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)

        self.assertEqual(summary.launchers_removed, 1)
        self.assertTrue(primary.exists())

    def test_manifest_not_updated_on_failure(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        desired = self._load()

        with patch("ai_dev_flow.managed_installation.write_json_object_atomic", side_effect=RuntimeError("boom")):
            with self.assertRaises(ManagedInstallationError):
                apply_installation_reconciliation(desired, paths=self.paths)

        self.assertFalse(self.paths.manifest_path.exists())
        self.assertFalse((self.paths.launcher_directory / "flow-status").exists())

    def test_linux_launcher_is_executable(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        launcher = self.paths.launcher_directory / "flow-status"
        mode = launcher.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_linux_launcher_argument_forwarding(self) -> None:
        capture = self.tmp_path / "capture.sh"
        output = self.tmp_path / "args.txt"
        capture.write_text(
            "#!/usr/bin/env sh\n"
            "printf '%s\\n' \"$1\" > \"$3\"\n"
            "printf '%s\\n' \"$2\" >> \"$3\"\n",
            encoding="utf-8",
        )
        capture.chmod(capture.stat().st_mode | stat.S_IXUSR)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            f"    commands:\n      flow-status: \"{capture}\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        launcher = self.paths.launcher_directory / "flow-status"

        subprocess.run([str(launcher), "alpha beta", "gamma", str(output)], check=True)
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "alpha beta")
        self.assertEqual(lines[1], "gamma")

    def test_path_block_insert_idempotent_and_remove(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: true\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        first = self.paths.bashrc_path.read_text(encoding="utf-8")
        self.assertIn("# >>> ai-dev managed PATH >>>", first)

        apply_installation_reconciliation(self._load(), paths=self.paths)
        second = self.paths.bashrc_path.read_text(encoding="utf-8")
        self.assertEqual(first, second)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        removed = self.paths.bashrc_path.read_text(encoding="utf-8")
        self.assertNotIn("# >>> ai-dev managed PATH >>>", removed)

    def test_refuses_unmanaged_exact_match_path_block_without_claiming_ownership(self) -> None:
        self.paths.bashrc_path.write_text(
            "# >>> ai-dev managed PATH >>>\n"
            "export PATH=\"$HOME/.local/bin:$PATH\"\n"
            "# <<< ai-dev managed PATH <<<\n",
            encoding="utf-8",
        )
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: true\n"
        )

        with self.assertRaises(ManagedInstallationError) as context:
            apply_installation_reconciliation(self._load(), paths=self.paths)

        self.assertIn("existing unmanaged PATH block", str(context.exception))
        self.assertIn("will not claim ownership automatically", str(context.exception))
        self.assertFalse(self.paths.manifest_path.exists())

    def test_preserves_unrelated_bashrc_content(self) -> None:
        self.paths.bashrc_path.write_text("export FOO=bar\n", encoding="utf-8")
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: true\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        text = self.paths.bashrc_path.read_text(encoding="utf-8")
        self.assertIn("export FOO=bar", text)

    def test_missing_bashrc_is_supported(self) -> None:
        self.assertFalse(self.paths.bashrc_path.exists())
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "  shellPath:\n"
            "    enabled: true\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertTrue(self.paths.bashrc_path.exists())

    def test_windows_launcher_generation_mocked(self) -> None:
        windows_paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "win-bin",
            manifest_path=self.tmp_path / "win-state" / "installation-manifest.json",
            bashrc_path=None,
            windows=True,
        )
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=windows_paths)
        launcher = windows_paths.launcher_directory / "flow-status.cmd"
        self.assertTrue(launcher.exists())
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("AI_DEV_MANAGED_LAUNCHER_V1", text)
        self.assertIn('"ai-dev" "flow" "status" %*', text)
        self.assertEqual(summary.path_status, "disabled")


if __name__ == "__main__":
    unittest.main()
