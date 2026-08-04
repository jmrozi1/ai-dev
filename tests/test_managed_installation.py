from __future__ import annotations

import json
from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_dev_flow import cli
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

    def _write_fake_ai_dev(self, *, exit_code: int = 0) -> tuple[Path, Path]:
        fake_bin = self.tmp_path / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        capture = self.tmp_path / "captured-argv.txt"
        fake_ai_dev = fake_bin / "ai-dev"
        fake_ai_dev.write_text(
            "#!/usr/bin/env sh\n"
            f"outfile='{capture}'\n"
            ": > \"$outfile\"\n"
            "for arg in \"$@\"; do\n"
            "    printf '%s\\n' \"$arg\" >> \"$outfile\"\n"
            "done\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        fake_ai_dev.chmod(fake_ai_dev.stat().st_mode | stat.S_IXUSR)
        return fake_bin, capture

    def test_default_configuration_includes_managed_aliases_and_shell_path(self) -> None:
        self._write_config("")
        desired = self._load()
        self.assertTrue(desired.aliases_enabled)
        self.assertTrue(desired.expand_subcommands)
        self.assertTrue(desired.shell_path_enabled)
        self.assertIn("flow", desired.alias_commands)
        self.assertEqual(set(desired.alias_commands), {"flow"})
        self.assertEqual(desired.alias_commands["flow"], ("ai-dev", "flow"))

    def test_array_command_mapping_is_supported(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status:\n"
            "        - ai-dev\n"
            "        - flow\n"
            "        - status\n"
        )
        desired = self._load()
        self.assertEqual(desired.alias_commands["flow-status"], ("ai-dev", "flow", "status"))

    def test_array_command_mapping_preserves_exact_tokens(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status:\n"
            "        - ai-dev\n"
            "        - \"  flow  \"\n"
            "        - status\n"
        )
        desired = self._load()
        self.assertEqual(desired.alias_commands["flow-status"], ("ai-dev", "  flow  ", "status"))

    def test_string_command_mapping_is_supported(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
        )
        desired = self._load()
        self.assertEqual(desired.alias_commands["flow-status"], ("ai-dev", "flow", "status"))

    def test_enabled_false_keeps_definitions_but_disables_reconciliation(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
        )
        desired = self._load()
        self.assertFalse(desired.aliases_enabled)
        self.assertEqual(desired.alias_commands["flow-status"], ("ai-dev", "flow", "status"))

    def test_enabled_false_apply_removes_and_reenable_reinstalls_without_redefining(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: true\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        created = apply_installation_reconciliation(self._load(), paths=self.paths)
        launcher = self.paths.launcher_directory / "flow-status"
        self.assertEqual(created.launchers_created, 1)
        self.assertTrue(launcher.exists())

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: false\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        disabled = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(disabled.launchers_removed, 1)
        self.assertFalse(launcher.exists())

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: true\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        reenabled = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(reenabled.launchers_created, 1)
        self.assertTrue(launcher.exists())

    def test_unknown_aliases_key_rejected(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    unknown: true\n"
        )
        with self.assertRaises(InstallationConfigError) as ctx:
            self._load()
        self.assertIn("installation.aliases", str(ctx.exception))
        self.assertIn("unknown key", str(ctx.exception))

    def test_aliases_enabled_must_be_boolean(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: not-a-boolean\n"
        )
        with self.assertRaises(InstallationConfigError) as ctx:
            self._load()
        self.assertIn("installation.aliases.enabled", str(ctx.exception))
        self.assertIn("expected boolean", str(ctx.exception))

    def test_expand_subcommands_must_be_boolean(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    expand_subcommands: not-a-boolean\n"
        )
        with self.assertRaises(InstallationConfigError) as ctx:
            self._load()
        self.assertIn("installation.aliases.expand_subcommands", str(ctx.exception))
        self.assertIn("expected boolean", str(ctx.exception))

    def test_command_array_must_be_non_empty(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: []\n"
        )
        with self.assertRaises(InstallationConfigError):
            self._load()

    def test_command_array_tokens_must_be_non_empty_strings(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status:\n"
            "        - ai-dev\n"
            "        - \"\"\n"
        )
        with self.assertRaises(InstallationConfigError):
            self._load()

    def test_commands_must_be_mapping(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands: []\n"
        )
        with self.assertRaises(InstallationConfigError):
            self._load()

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

    def test_flow_root_expands_to_direct_descendants(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: true\n"
            "    expand_subcommands: true\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)

        expected_descendants = {
            "flow-help",
            "flow-start",
            "flow-patch",
            "flow-task-prepare",
            "flow-status",
            "flow-review",
            "flow-commit",
            "flow-reset",
            "flow-promote",
            "flow-complete",
            "flow-block",
            "flow-resume",
        }
        registry_descendants = {f"flow-{name}" for name in cli.FLOW_LIFECYCLE_COMMANDS}
        self.assertEqual(expected_descendants - {"flow-help"}, registry_descendants)
        expected_all = {"flow", *expected_descendants}

        self.assertEqual(summary.launchers_created, len(expected_all))
        self.assertEqual(summary.expanded_root_aliases, ("flow",))
        self.assertEqual(summary.generated_descendant_aliases, tuple(sorted(expected_descendants)))
        self.assertEqual(summary.suppressed_descendant_aliases, ())
        self.assertEqual(summary.expansion_unavailable_root_aliases, ())
        for alias_name in expected_all:
            self.assertTrue((self.paths.launcher_directory / alias_name).exists())

        manifest_payload = json.loads(self.paths.manifest_path.read_text(encoding="utf-8"))
        managed_keys = list(manifest_payload["managed_launchers"].keys())
        self.assertEqual(managed_keys, sorted(managed_keys))

    def test_expansion_disabled_installs_root_only(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    enabled: true\n"
            "    expand_subcommands: false\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(summary.launchers_created, 1)
        self.assertEqual(summary.generated_descendant_aliases, ())
        self.assertTrue((self.paths.launcher_directory / "flow").exists())
        self.assertFalse((self.paths.launcher_directory / "flow-help").exists())

    def test_generated_help_descendant_maps_to_flow_help(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        help_launcher = (self.paths.launcher_directory / "flow-help").read_text(encoding="utf-8")
        self.assertIn("exec 'ai-dev' 'flow' '--help' \"$@\"", help_launcher)

    def test_explicit_alias_overrides_generated_descendant_with_suppression(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "      flow-status: \"ai-dev flow review\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertIn("flow-status", summary.suppressed_descendant_aliases)
        flow_status_launcher = (self.paths.launcher_directory / "flow-status").read_text(encoding="utf-8")
        self.assertIn("exec 'ai-dev' 'flow' 'review' \"$@\"", flow_status_launcher)
        self.assertNotIn("exec 'ai-dev' 'flow' 'status' \"$@\"", flow_status_launcher)

    def test_stale_descendants_removed_when_expansion_disabled(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    expand_subcommands: true\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    expand_subcommands: false\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(summary.launchers_removed, 12)
        self.assertTrue((self.paths.launcher_directory / "flow").exists())
        self.assertFalse((self.paths.launcher_directory / "flow-help").exists())

    def test_stale_descendants_removed_when_root_removed(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands: {}\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(summary.launchers_removed, 13)
        self.assertFalse((self.paths.launcher_directory / "flow").exists())
        self.assertFalse((self.paths.launcher_directory / "flow-help").exists())

    def test_stale_descendants_removed_when_root_command_changes(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev summarize\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(summary.launchers_removed, 12)
        self.assertEqual(summary.expansion_unavailable_root_aliases, ("flow",))
        flow_launcher = (self.paths.launcher_directory / "flow").read_text(encoding="utf-8")
        self.assertIn("exec 'ai-dev' 'summarize' \"$@\"", flow_launcher)

    def test_unmanaged_collision_on_generated_descendant_fails_closed(self) -> None:
        flow_help = self.paths.launcher_directory / "flow-help"
        flow_help.parent.mkdir(parents=True, exist_ok=True)
        flow_help.write_text("user-owned\n", encoding="utf-8")

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        with self.assertRaises(ManagedInstallationError):
            apply_installation_reconciliation(self._load(), paths=self.paths)

    def test_idempotent_noop_with_expansion_enabled(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        first = apply_installation_reconciliation(self._load(), paths=self.paths)
        second = apply_installation_reconciliation(self._load(), paths=self.paths)

        self.assertEqual(first.launchers_created, 13)
        self.assertEqual(second.launchers_created, 0)
        self.assertEqual(second.launchers_updated, 0)
        self.assertEqual(second.launchers_removed, 0)
        self.assertEqual(second.launchers_unchanged, 13)

    def test_unrecognized_external_command_installs_root_only(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      custom: \"python -m mytool\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertEqual(summary.launchers_created, 1)
        self.assertEqual(summary.generated_descendant_aliases, ())
        self.assertEqual(summary.expansion_unavailable_root_aliases, ("custom",))
        self.assertTrue((self.paths.launcher_directory / "custom").exists())

    def test_windows_flow_descendant_rendering(self) -> None:
        windows_paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "win-bin-expansion",
            manifest_path=self.tmp_path / "win-state-expansion" / "installation-manifest.json",
            bashrc_path=None,
            windows=True,
        )
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
        )
        summary = apply_installation_reconciliation(self._load(), paths=windows_paths)
        self.assertEqual(summary.launchers_created, 13)
        help_launcher = (windows_paths.launcher_directory / "flow-help.cmd").read_text(encoding="utf-8")
        self.assertIn('"ai-dev" "flow" "--help" %*', help_launcher)
        self.assertTrue((windows_paths.launcher_directory / "flow-start.cmd").exists())

    def test_windows_cmd_launcher_escapes_percent_quotes_spaces_and_backslashes(self) -> None:
        windows_paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "win-bin-escape",
            manifest_path=self.tmp_path / "win-state-escape" / "installation-manifest.json",
            bashrc_path=None,
            windows=True,
        )
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      wincmd:\n"
            "        - C:\\\\Program Files\\\\Tool\\\\run.exe\n"
            "        - --name=\"A B\"\n"
            "        - 100%\n"
        )
        apply_installation_reconciliation(self._load(), paths=windows_paths)
        launcher_text = (windows_paths.launcher_directory / "wincmd.cmd").read_text(encoding="utf-8")
        self.assertIn('"C:\\\\Program Files\\\\Tool\\\\run.exe"', launcher_text)
        self.assertIn('"--name=""A B"""', launcher_text)
        self.assertIn('"100%%"', launcher_text)
        self.assertIn("%*", launcher_text)

    def test_linux_command_discovery_supports_flow_prefix_completion_mechanism(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        launcher_dir = self.paths.launcher_directory
        self.assertTrue((launcher_dir / "flow").exists())
        self.assertTrue((launcher_dir / "flow-start").exists())
        self.assertTrue((launcher_dir / "flow-status").exists())

        completed = subprocess.run(
            [
                "bash",
                "-lc",
                f'PATH="{launcher_dir}:$PATH"; compgen -c flow- | sort -u',
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        discovered = set(line.strip() for line in completed.stdout.splitlines() if line.strip())
        self.assertIn("flow-start", discovered)
        self.assertIn("flow-status", discovered)
        self.assertIn("flow-help", discovered)

    def test_generated_root_and_descendant_forward_args_and_preserve_exit_status(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        fake_bin, capture = self._write_fake_ai_dev(exit_code=37)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

        root_launcher = self.paths.launcher_directory / "flow"
        root_run = subprocess.run(
            [str(root_launcher), "alpha beta", "", "*"],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(root_run.returncode, 37)
        root_args = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(root_args, ["flow", "alpha beta", "", "*"])

        descendant_launcher = self.paths.launcher_directory / "flow-start"
        desc_run = subprocess.run(
            [str(descendant_launcher), "quoted value", "", "*"],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(desc_run.returncode, 37)
        descendant_args = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(descendant_args, ["flow", "start", "quoted value", "", "*"])

    def test_default_apply_with_missing_config_creates_one_root_and_descendants_then_noop(self) -> None:
        config_path = self.tmp_path / "cfg" / "ai-dev" / "config.yaml"
        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "default-bin",
            manifest_path=self.tmp_path / "default-state" / "installation-manifest.json",
            bashrc_path=self.tmp_path / "default-bashrc",
            windows=False,
        )

        desired = load_desired_installation_state(config_path, case_insensitive_names=False)
        self.assertEqual(set(desired.alias_commands.keys()), {"flow"})

        first = apply_installation_reconciliation(desired, paths=paths)
        self.assertEqual(first.launchers_created, 13)
        self.assertTrue((paths.launcher_directory / "flow-status").exists())

        completed = subprocess.run(
            [
                "bash",
                "-lc",
                f'PATH="{paths.launcher_directory}:$PATH"; compgen -c flow- | sort -u',
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        discovered = set(line.strip() for line in completed.stdout.splitlines() if line.strip())
        self.assertIn("flow-status", discovered)

        second = apply_installation_reconciliation(desired, paths=paths)
        self.assertEqual(second.launchers_created, 0)
        self.assertEqual(second.launchers_updated, 0)
        self.assertEqual(second.launchers_removed, 0)
        self.assertEqual(second.launchers_unchanged, 13)

    def test_windows_case_insensitive_explicit_collision_suppresses_generated_descendant(self) -> None:
        windows_paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "win-bin-casefold",
            manifest_path=self.tmp_path / "win-state-casefold" / "installation-manifest.json",
            bashrc_path=None,
            windows=True,
        )
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "      Flow-Status: \"ai-dev flow review\"\n"
        )

        summary = apply_installation_reconciliation(self._load(), paths=windows_paths)
        self.assertIn("Flow-Status", summary.suppressed_descendant_aliases)

        flow_status_launcher = windows_paths.launcher_directory / "Flow-Status.cmd"
        self.assertTrue(flow_status_launcher.exists())
        self.assertFalse((windows_paths.launcher_directory / "flow-status.cmd").exists())

        rendered = flow_status_launcher.read_text(encoding="utf-8")
        self.assertIn('"ai-dev" "flow" "review" %*', rendered)
        self.assertNotIn('"ai-dev" "flow" "status" %*', rendered)

        manifest_payload = json.loads(windows_paths.manifest_path.read_text(encoding="utf-8"))
        managed_paths = list(manifest_payload["managed_launchers"].keys())
        casefold_matches = [path for path in managed_paths if path.lower().endswith("flow-status.cmd")]
        self.assertEqual(len(casefold_matches), 1)

    def test_stale_descendant_removed_when_authoritative_surface_changes(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)
        self.assertTrue((self.paths.launcher_directory / "flow-resume").exists())

        with patch(
            "ai_dev_flow.managed_installation._flow_direct_subcommands",
            return_value=tuple(name for name in cli.FLOW_LIFECYCLE_COMMANDS if name != "resume"),
        ):
            summary = apply_installation_reconciliation(self._load(), paths=self.paths)

        self.assertEqual(summary.launchers_removed, 1)
        self.assertFalse((self.paths.launcher_directory / "flow-resume").exists())
        self.assertTrue((self.paths.launcher_directory / "flow-start").exists())
        self.assertTrue((self.paths.launcher_directory / "flow-help").exists())

        manifest_payload = json.loads(self.paths.manifest_path.read_text(encoding="utf-8"))
        managed_paths = set(manifest_payload["managed_launchers"].keys())
        removed_path = str((self.paths.launcher_directory / "flow-resume").resolve())
        self.assertNotIn(removed_path, managed_paths)

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
            "    expand_subcommands: false\n"
            "    commands:\n"
            "      floow: \"ai-dev flow\"\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )
        apply_installation_reconciliation(self._load(), paths=self.paths)

        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    expand_subcommands: false\n"
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
            "    commands: {}\n"
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
            "    commands: {}\n"
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
            "    commands: {}\n"
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
            "    commands: {}\n"
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
            "    commands: {}\n"
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
            "    commands: {}\n"
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

    def test_exact_token_spaces_preserved_in_posix_and_windows_launchers(self) -> None:
        self._write_config(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-spaces:\n"
            "        - ai-dev\n"
            "        - \"  flow  \"\n"
            "        - status\n"
            "  shellPath:\n"
            "    enabled: false\n"
        )

        desired = self._load()
        self.assertEqual(desired.alias_commands["flow-spaces"], ("ai-dev", "  flow  ", "status"))

        apply_installation_reconciliation(desired, paths=self.paths)
        posix_launcher = (self.paths.launcher_directory / "flow-spaces").read_text(encoding="utf-8")
        self.assertIn("exec 'ai-dev' '  flow  ' 'status' \"$@\"", posix_launcher)

        windows_paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "win-bin-spaces",
            manifest_path=self.tmp_path / "win-state-spaces" / "installation-manifest.json",
            bashrc_path=None,
            windows=True,
        )
        apply_installation_reconciliation(desired, paths=windows_paths)
        windows_launcher = (windows_paths.launcher_directory / "flow-spaces.cmd").read_text(encoding="utf-8")
        self.assertIn('"ai-dev" "  flow  " "status" %*', windows_launcher)


if __name__ == "__main__":
    unittest.main()
