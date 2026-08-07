from __future__ import annotations

import io
import json
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

    def _write_workflow_state(self, repo_root: Path) -> None:
        ai_dev_dir = repo_root / ".ai-dev"
        ai_dev_dir.mkdir(parents=True, exist_ok=True)
        (ai_dev_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_blocked_workflow(self, repo_root: Path, issue_number: int) -> None:
        ai_dev_dir = repo_root / ".ai-dev"
        ai_dev_dir.mkdir(parents=True, exist_ok=True)
        (ai_dev_dir / "blocked-workflows.json").write_text(
            json.dumps(
                {
                    "blockedWorkflows": [
                        {
                            "issueNumber": issue_number,
                            "issueTitle": f"Issue {issue_number}",
                            "issueUrl": f"https://github.com/jmrozi1/ai-dev/issues/{issue_number}",
                            "reason": "waiting",
                            "blockedAt": "2026-08-07T00:00:00Z",
                        }
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _invoke_direct(self, cwd: Path, command: str, *arguments: str) -> tuple[int, str, str]:
        return self._invoke_with_argv(
            cwd,
            f"flow-{command}",
            cli._DIRECT_FLOW_ROUTE_TOKEN,
            command,
            *arguments,
        )

    def _invoke_with_argv(self, cwd: Path, argv0: str, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = argv0
        sys.argv = [argv0, *arguments]
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

    def test_no_generic_dispatcher_contract_remains(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir(parents=True)

        for argv in ((), ("--help",), ("status",), ("commit",)):
            with self.subTest(argv=argv):
                code, stdout, stderr = self._invoke_with_argv(outside, "flow", *argv)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertNotIn("Usage: flow <command>", stderr)

    def test_legacy_flow_namespace_is_rejected(self) -> None:
        outside = self.tmp_path / "outside-legacy"
        outside.mkdir(parents=True)

        code, stdout, stderr = self._invoke_with_argv(outside, "ai-dev", "flow", "status")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("ai-dev: unknown command: flow", stderr)

    def test_command_specific_help_uses_direct_executable_name(self) -> None:
        outside = self.tmp_path / "outside-help"
        outside.mkdir(parents=True)

        for command in cli.FLOW_LIFECYCLE_COMMANDS:
            with self.subTest(command=command):
                code, stdout, stderr = self._invoke_direct(outside, command, "--help")
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertIn(f"Usage: flow-{command}", stdout)
                self.assertNotIn(f"Usage: flow-{command} {command}", stdout)

    def test_internal_direct_route_accepts_all_fixed_flow_commands(self) -> None:
        outside = self.tmp_path / "outside-direct-accept"
        outside.mkdir(parents=True)

        for command in cli.FIXED_FLOW_EXECUTABLE_COMMANDS:
            with self.subTest(command=command):
                code, stdout, stderr = self._invoke_with_argv(
                    outside,
                    f"flow-{command}",
                    cli._DIRECT_FLOW_ROUTE_TOKEN,
                    command,
                    "--help",
                )

                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertIn(f"Usage: flow-{command}", stdout)

    def test_internal_direct_route_rejects_non_fixed_flow_command(self) -> None:
        outside = self.tmp_path / "outside-direct-reject"
        outside.mkdir(parents=True)

        for command in ("review", "review-verify", "summarize", "summarize-verify"):
            with self.subTest(command=command):
                code, stdout, stderr = self._invoke_with_argv(
                    outside,
                    "flow-review",
                    cli._DIRECT_FLOW_ROUTE_TOKEN,
                    command,
                    "--help",
                )

                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn(f"Invalid internal flow executable command: {command}", stderr)

    def test_blocked_start_guidance_uses_custom_prefix_resume_executable(self) -> None:
        repo_root = self._init_repo("repo-blocked-prefix-guidance")
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        self._write_workflow_state(repo_root)
        self._write_blocked_workflow(repo_root, 9)

        code, stdout, stderr = self._invoke_with_argv(
            repo_root,
            "ai-flow-start",
            cli._DIRECT_FLOW_ROUTE_TOKEN,
            "start",
            "9",
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Cannot start workflow: issue 9 is blocked.", stderr)
        self.assertIn("Use ai-flow-resume 9.", stderr)


if __name__ == "__main__":
    unittest.main()
