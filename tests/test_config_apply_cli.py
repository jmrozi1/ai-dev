from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import yaml
import subprocess

from ai_dev_flow import cli
from ai_dev_flow.managed_installation import ManagedInstallationPaths


class ApplyCliTests(unittest.TestCase):
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

    def test_apply_help_works_outside_repo(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)

        code, out, err = self._invoke(outside, "apply", "--help")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Usage: ai-dev apply", out)
        self.assertIn("This command is idempotent", out)

    def test_apply_succeeds_without_repo(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow-status: \"ai-dev flow status\"\n"
            "  shellPath:\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin",
            manifest_path=self.tmp_path / "state" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc",
            windows=False,
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.managed_installation.resolve_managed_installation_paths",
                return_value=paths,
            ),
        ):
            code, out, err = self._invoke(outside, "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Managed launchers:", out)
        self.assertIn("created: 1", out)
        self.assertIn("Manifest:", out)

    def test_config_apply_compatibility_route_still_works(self) -> None:
        outside = self.tmp_path / "outside-compat"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-compat" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "installation:\n"
            "  aliases:\n"
            "    commands: {}\n"
            "  shellPath:\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin-compat",
            manifest_path=self.tmp_path / "state-compat" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc-compat",
            windows=False,
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.managed_installation.resolve_managed_installation_paths",
                return_value=paths,
            ),
        ):
            code, out, err = self._invoke(outside, "config", "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Managed launchers:", out)

    def test_apply_reports_explicit_descendant_suppression(self) -> None:
        outside = self.tmp_path / "outside-suppression"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-suppression" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      flow: \"ai-dev flow\"\n"
            "      flow-status: \"ai-dev flow review\"\n"
            "  shellPath:\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin-suppression",
            manifest_path=self.tmp_path / "state-suppression" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc-suppression",
            windows=False,
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.managed_installation.resolve_managed_installation_paths",
                return_value=paths,
            ),
        ):
            code, out, err = self._invoke(outside, "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("suppressed descendants: 1", out)
        self.assertIn("suppressed: flow-status", out)

    def test_apply_reports_missing_authoritative_expansion_source(self) -> None:
        outside = self.tmp_path / "outside-unavailable"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-unavailable" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "installation:\n"
            "  aliases:\n"
            "    commands:\n"
            "      custom: \"python -m mytool\"\n"
            "  shellPath:\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin-unavailable",
            manifest_path=self.tmp_path / "state-unavailable" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc-unavailable",
            windows=False,
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.managed_installation.resolve_managed_installation_paths",
                return_value=paths,
            ),
        ):
            code, out, err = self._invoke(outside, "apply")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("no authoritative expansion source: 1", out)
        self.assertIn("roots without source: custom", out)

    def test_apply_creates_default_config_and_reapply_is_noop(self) -> None:
        outside = self.tmp_path / "outside-default"
        outside.mkdir(parents=True)
        config_path = self.tmp_path / "cfg-default" / "config.yaml"

        paths = ManagedInstallationPaths(
            launcher_directory=self.tmp_path / "bin-default",
            manifest_path=self.tmp_path / "state-default" / "installation-manifest.json",
            bashrc_path=self.tmp_path / ".bashrc-default",
            windows=False,
        )

        with (
            patch.dict(os.environ, {"AI_DEV_CONFIG": str(config_path)}, clear=False),
            patch(
                "ai_dev_flow.managed_installation.resolve_managed_installation_paths",
                return_value=paths,
            ),
        ):
            first_code, first_out, first_err = self._invoke(outside, "apply")
            second_code, second_out, second_err = self._invoke(outside, "apply")

        self.assertEqual(first_code, 0)
        self.assertEqual(first_err, "")
        self.assertTrue(config_path.exists())
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["installation"]["aliases"]["enabled"], True)
        self.assertEqual(loaded["installation"]["aliases"]["expand_subcommands"], True)
        self.assertEqual(set(loaded["installation"]["aliases"]["commands"].keys()), {"flow"})
        self.assertIn("created: 13", first_out)
        self.assertTrue((paths.launcher_directory / "flow-status").exists())

        completion = subprocess.run(
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
        discovered = set(line.strip() for line in completion.stdout.splitlines() if line.strip())
        self.assertIn("flow-status", discovered)

        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        self.assertIn("created: 0", second_out)
        self.assertIn("updated: 0", second_out)
        self.assertIn("removed: 0", second_out)


if __name__ == "__main__":
    unittest.main()
