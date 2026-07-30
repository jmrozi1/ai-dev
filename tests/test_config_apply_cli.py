from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.alias_installation import AliasInstallerPaths


class ConfigApplyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = "ai-dev"
        sys.argv = ["ai-dev", *arguments]
        os.chdir(cwd)

        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.run()
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 1
                else:
                    code = 0
        finally:
            os.chdir(previous_cwd)
            sys.argv = previous_argv
            if had_command_name:
                assert previous_command_name is not None
                os.environ["FLOW_COMMAND_NAME"] = previous_command_name
            else:
                os.environ.pop("FLOW_COMMAND_NAME", None)

        return code, stdout.getvalue(), stderr.getvalue()

    def test_config_apply_succeeds_without_repo(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("aliases:\n  gs: status\n", encoding="utf-8")

        profile_path = self.tmp_path / "profile.rc"
        alias_path = self.tmp_path / "aliases.sh"
        manifest_path = self.tmp_path / "manifest.json"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.alias_installation.resolve_manifest_path",
                return_value=manifest_path,
            ),
            patch(
                "ai_dev_flow.alias_installation.resolve_installer_paths",
                return_value=AliasInstallerPaths(
                    profile_path=profile_path,
                    alias_file_path=alias_path,
                    manifest_path=manifest_path,
                ),
            ),
        ):
            code, out, err = self._invoke(outside, "config", "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Managed aliases configured: 1", out)
        self.assertIn("Result: applied", out)
        self.assertTrue(alias_path.exists())
        self.assertTrue(profile_path.exists())
        self.assertTrue(manifest_path.exists())

    def test_config_apply_remove_all_result(self) -> None:
        outside = self.tmp_path / "outside-remove"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-remove" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("aliases:\n  gs: status\n", encoding="utf-8")

        profile_path = self.tmp_path / "profile-remove.rc"
        alias_path = self.tmp_path / "aliases-remove.sh"
        manifest_path = self.tmp_path / "manifest-remove.json"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.alias_installation.resolve_manifest_path",
                return_value=manifest_path,
            ),
            patch(
                "ai_dev_flow.alias_installation.resolve_installer_paths",
                return_value=AliasInstallerPaths(
                    profile_path=profile_path,
                    alias_file_path=alias_path,
                    manifest_path=manifest_path,
                ),
            ),
        ):
            first_code, _, first_err = self._invoke(outside, "config", "apply")
            self.assertEqual(first_code, 0)
            self.assertEqual(first_err, "")

            config_path.write_text("aliases: {}\n", encoding="utf-8")
            code, out, err = self._invoke(outside, "config", "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Result: removed-all", out)
        self.assertIn("Updated: alias-file, profile, manifest-removed", out)

    def test_config_apply_noop_result(self) -> None:
        outside = self.tmp_path / "outside-noop"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-noop" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("aliases:\n  gs: status\n", encoding="utf-8")

        profile_path = self.tmp_path / "profile-noop.rc"
        alias_path = self.tmp_path / "aliases-noop.sh"
        manifest_path = self.tmp_path / "manifest-noop.json"

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.alias_installation.resolve_manifest_path",
                return_value=manifest_path,
            ),
            patch(
                "ai_dev_flow.alias_installation.resolve_installer_paths",
                return_value=AliasInstallerPaths(
                    profile_path=profile_path,
                    alias_file_path=alias_path,
                    manifest_path=manifest_path,
                ),
            ),
        ):
            first_code, _, first_err = self._invoke(outside, "config", "apply")
            self.assertEqual(first_code, 0)
            self.assertEqual(first_err, "")

            code, out, err = self._invoke(outside, "config", "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Result: no-op", out)
        self.assertIn("Updated: none", out)

    def test_config_apply_reports_migrated_with_previous_paths(self) -> None:
        outside = self.tmp_path / "outside-migrated"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-migrated" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("aliases:\n  gs: status\n", encoding="utf-8")

        home = self.tmp_path / "home-migrated"
        old_paths = AliasInstallerPaths(
            profile_path=home / ".bashrc",
            alias_file_path=home / ".config" / "ai-dev" / "aliases.sh",
            manifest_path=home / ".config" / "ai-dev" / "managed-aliases-manifest.json",
        )
        new_paths = AliasInstallerPaths(
            profile_path=home / ".zshrc",
            alias_file_path=home / ".config" / "ai-dev" / "aliases.sh",
            manifest_path=home / ".config" / "ai-dev" / "managed-aliases-manifest.json",
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.alias_installation.resolve_manifest_path",
                return_value=old_paths.manifest_path,
            ),
            patch(
                "ai_dev_flow.alias_installation.resolve_installer_paths",
                return_value=old_paths,
            ),
        ):
            first_code, _, first_err = self._invoke(outside, "config", "apply")
            self.assertEqual(first_code, 0)
            self.assertEqual(first_err, "")

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.alias_installation.resolve_manifest_path",
                return_value=new_paths.manifest_path,
            ),
            patch(
                "ai_dev_flow.alias_installation.resolve_installer_paths",
                return_value=new_paths,
            ),
        ):
            code, out, err = self._invoke(outside, "config", "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Result: migrated", out)
        self.assertIn(f"Previous profile: {old_paths.profile_path}", out)
        self.assertIn(f"Profile file: {new_paths.profile_path}", out)

    def test_config_apply_usage_error(self) -> None:
        outside = self.tmp_path / "outside-usage"
        outside.mkdir(parents=True)

        code, out, err = self._invoke(outside, "config", "nope")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Usage: ai-dev config [apply]", err)


if __name__ == "__main__":
    unittest.main()
