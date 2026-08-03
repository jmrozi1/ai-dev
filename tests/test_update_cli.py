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
from ai_dev_flow.update_installation import (
    ApplyRefreshResult,
    LauncherRefreshResult,
    UpdateExecutionResult,
    UpdateInstallationError,
    UpdateSourceResult,
)


class UpdateCliTests(unittest.TestCase):
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

    def test_update_help_works_outside_repo(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)

        code, out, err = self._invoke(outside, "update", "--help")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Usage: ai-dev update", out)
        self.assertIn("will not stash, reset, clean, merge", out)

    def test_update_refuses_positional_arguments(self) -> None:
        outside = self.tmp_path / "outside-args"
        outside.mkdir(parents=True)

        code, out, err = self._invoke(outside, "update", "now")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Usage: ai-dev update", err)

    def test_update_reports_phase_status_and_partial_failure(self) -> None:
        outside = self.tmp_path / "outside-phases"
        outside.mkdir(parents=True)
        source_repo = self.tmp_path / "source"
        source_repo.mkdir(parents=True)

        with (
            patch(
                "ai_dev_flow.cli.resolve_installation_source_path",
                return_value=self.tmp_path / "state" / "installation-source.json",
            ),
            patch(
                "ai_dev_flow.cli.run_update_from_record",
                return_value=UpdateExecutionResult(
                    source=UpdateSourceResult(
                        source_status="fast-forwarded",
                        source_from="abc",
                        source_to="def",
                        source_repo=source_repo,
                        branch="main",
                        remote="origin",
                    ),
                    launcher=LauncherRefreshResult(status="updated", detail="launcher refreshed"),
                    apply=ApplyRefreshResult(status="failed", detail="Retry with: ..."),
                ),
            ),
        ):
            code, out, err = self._invoke(outside, "update")

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("Update source:", out)
        self.assertIn("fast-forwarded abc -> def", out)
        self.assertIn("Launcher refresh:", out)
        self.assertIn("updated", out)
        self.assertIn("Apply:", out)
        self.assertIn("failed", out)
        self.assertIn("Retry with: ...", out)

    def test_update_reports_preflight_error(self) -> None:
        outside = self.tmp_path / "outside-failure"
        outside.mkdir(parents=True)

        with (
            patch(
                "ai_dev_flow.cli.resolve_installation_source_path",
                return_value=self.tmp_path / "state" / "installation-source.json",
            ),
            patch(
                "ai_dev_flow.cli.run_update_from_record",
                side_effect=UpdateInstallationError("missing metadata"),
            ),
        ):
            code, out, err = self._invoke(outside, "update")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Update source:\n  failed\n", err)
        self.assertNotIn("failed before mutation", err)
        self.assertIn("missing metadata", err)


if __name__ == "__main__":
    unittest.main()
