from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_dev_flow import cli


class FlowNamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_git(self, repo_root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()

    def _init_repo(self, name: str) -> Path:
        repo_root = self.tmp_path / name
        repo_root.mkdir(parents=True)

        self._run_git(repo_root, "init", "-q")
        self._run_git(repo_root, "config", "user.name", "Flow Namespace Tests")
        self._run_git(repo_root, "config", "user.email", "flow-namespace-tests@example.com")
        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")

        return repo_root

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

    def test_top_level_help_emphasizes_flow_and_separates_compatibility(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)

        code, stdout, stderr = self._invoke(outside, "-h")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        self.assertIn("Usage: ai-dev <command> [options]", stdout)
        self.assertIn("Commands:\n  flow", stdout)
        self.assertIn("\n  apply", stdout)
        self.assertIn("Compatibility routes (temporary during Issue #19 migration):", stdout)

        commands_section = stdout.split("Commands:\n", 1)[1].split(
            "\n\nCompatibility routes", 1
        )[0]
        self.assertNotIn("\n  start", commands_section)

    def test_flow_help_lists_lifecycle_commands_in_deterministic_order(self) -> None:
        outside = self.tmp_path / "outside-flow"
        outside.mkdir(parents=True)

        code, stdout, stderr = self._invoke(outside, "flow", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        self.assertIn("Usage: ai-dev flow <command> [options]", stdout)
        expected_order = [
            "start",
            "patch",
            "task-prepare",
            "status",
            "review",
            "commit",
            "reset",
            "promote",
            "complete",
            "block",
            "resume",
        ]
        positions = [stdout.index(f"  {name}") for name in expected_order]
        self.assertEqual(positions, sorted(positions))

    def test_flow_with_no_subcommand_shows_flow_help(self) -> None:
        outside = self.tmp_path / "outside-no-subcommand"
        outside.mkdir(parents=True)

        code, stdout, stderr = self._invoke(outside, "flow")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Usage: ai-dev flow <command> [options]", stdout)

    def test_unknown_flow_subcommand_returns_non_zero_with_guidance(self) -> None:
        outside = self.tmp_path / "outside-unknown"
        outside.mkdir(parents=True)

        code, stdout, stderr = self._invoke(outside, "flow", "unknown")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("ai-dev flow: unknown command: unknown", stderr)
        self.assertIn("Run ai-dev flow --help for usage.", stderr)

    def test_nested_and_compatibility_routes_match_for_status(self) -> None:
        repo_root = self._init_repo("repo-status")

        compatibility_code, compatibility_stdout, compatibility_stderr = self._invoke(
            repo_root, "status"
        )
        nested_code, nested_stdout, nested_stderr = self._invoke(
            repo_root, "flow", "status"
        )

        self.assertEqual(compatibility_code, 0)
        self.assertEqual(nested_code, 0)
        self.assertEqual(nested_stdout, compatibility_stdout)
        self.assertEqual(nested_stderr, compatibility_stderr)

    def test_registry_metadata_drives_help_lists(self) -> None:
        outside = self.tmp_path / "outside-registry"
        outside.mkdir(parents=True)

        top_code, top_stdout, top_stderr = self._invoke(outside, "help")
        flow_code, flow_stdout, flow_stderr = self._invoke(outside, "flow", "--help")

        self.assertEqual(top_code, 0)
        self.assertEqual(top_stderr, "")
        self.assertEqual(flow_code, 0)
        self.assertEqual(flow_stderr, "")

        for command in cli.TOP_LEVEL_CANONICAL_COMMANDS:
            self.assertIn(f"  {command}", top_stdout)

        for command in cli.FLOW_LIFECYCLE_COMMANDS:
            self.assertIn(f"  {command}", flow_stdout)

        expected_compatibility = tuple(
            spec.name
            for spec in sorted(cli.COMMAND_SPECS, key=lambda item: item.order)
            if spec.compatibility_top_level and spec.help_visible
        )
        self.assertEqual(cli.TOP_LEVEL_COMPATIBILITY_COMMANDS, expected_compatibility)


if __name__ == "__main__":
    unittest.main()
