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
from unittest.mock import patch

from ai_dev_flow import cli
from ai_dev_flow.summarize_batching import build_summarize_batches
from ai_dev_flow.summarize_manifest import load_summarize_manifest
from ai_dev_flow.summarize_planning import build_summarize_plan
from ai_dev_flow.summarize_task_generation import prepare_summarize_task_artifacts


class SummarizeVerifyCliTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Summarize Verify CLI Tests")
        self._run_git(repo_root, "config", "user.email", "summarize-verify-cli-tests@example.com")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial")

        return repo_root

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        previous_cwd = Path.cwd()
        previous_argv = list(sys.argv)
        had_command_name = "FLOW_COMMAND_NAME" in os.environ
        previous_command_name = os.environ.get("FLOW_COMMAND_NAME")

        stdout = io.StringIO()
        stderr = io.StringIO()

        os.environ["FLOW_COMMAND_NAME"] = "flow"
        sys.argv = ["flow", *arguments]
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

    def _prepare_manifest(self, repo_root: Path, *, source_count: int = 1, max_files: int = 2):
        (repo_root / ".ai-dev.yaml").write_text(
            "summarize:\n"
            "  batch:\n"
            f"    max_files: {max_files}\n"
            "  rules:\n"
            "    - match: \"src/*.py\"\n"
            "      instructions: \"rule\"\n",
            encoding="utf-8",
        )

        source_root = repo_root / "src"
        source_root.mkdir(parents=True, exist_ok=True)
        for index in range(source_count):
            (source_root / f"file-{index:02d}.py").write_text(f"print({index})\n", encoding="utf-8")

        plan = build_summarize_plan(repo_root, "src/*.py")
        batches = build_summarize_batches(plan, max_files=max_files)
        prepare_summarize_task_artifacts(repo_root=repo_root, plan=plan, batches=batches)
        return load_summarize_manifest(repo_root, plan.plan_id)

    def _write_valid_outputs(self, repo_root: Path, manifest) -> None:
        for entry in manifest.entries:
            target = repo_root / entry.output_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "# Summary\n\n"
                f"Source: {entry.source_path}\n"
                "Generated-By: ai-dev summarize\n"
                f"Plan-ID: {manifest.plan_id}\n",
                encoding="utf-8",
            )

    def test_explicit_plan_id_complete_exit_zero_and_reports_written(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-explicit")
        manifest = self._prepare_manifest(repo_root)
        self._write_valid_outputs(repo_root, manifest)

        user_config = self.tmp_path / "reports-path-only.yaml"
        user_config.write_text(
            "reports:\n"
            "  presentation: path-only\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "summarize-verify", manifest.plan_id)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Summarize verification status for plan", out)
        self.assertIn("complete", out)
        self.assertIn("Verification report: .ai-dev/summarize/", out)
        self.assertIn("Verification JSON: .ai-dev/summarize/", out)

        markdown_path = repo_root / ".ai-dev" / "summarize" / manifest.plan_id / "verification.md"
        json_path = repo_root / ".ai-dev" / "summarize" / manifest.plan_id / "verification.json"
        self.assertTrue(markdown_path.exists())
        self.assertTrue(json_path.exists())

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["overall_status"], "complete")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            second_code, second_out, second_err = self._invoke(repo_root, "summarize-verify", manifest.plan_id)

        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        self.assertEqual(second_out, out)

    def test_omitted_plan_id_uses_current_summarize_coordinator(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-current")
        manifest = self._prepare_manifest(repo_root)
        self._write_valid_outputs(repo_root, manifest)

        code, out, err = self._invoke(repo_root, "summarize-verify")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(manifest.plan_id, out)

    def test_current_task_not_summarize_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-not-summarize")
        (repo_root / ".ai-dev").mkdir(parents=True, exist_ok=True)
        (repo_root / ".ai-dev" / "current-task.md").write_text(
            "# Current AI Dev Task\n\n"
            "- Task-ID: review-task\n"
            "- Task-Type: review\n"
            "- Task-File: .ai-dev/tasks/review-task.md\n",
            encoding="utf-8",
        )

        code, out, err = self._invoke(repo_root, "summarize-verify")

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Current task is not summarize", err)

    def test_incomplete_verification_returns_one(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-incomplete")
        manifest = self._prepare_manifest(repo_root, source_count=2, max_files=2)
        # Do not create outputs.

        code, out, err = self._invoke(repo_root, "summarize-verify", manifest.plan_id)

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("partial", out)

    def test_unexpected_outputs_make_verify_exit_one_until_removed(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-unexpected-partial")
        manifest = self._prepare_manifest(repo_root, source_count=1)
        self._write_valid_outputs(repo_root, manifest)

        unexpected = repo_root / ".ai-dev" / "summaries" / "extra.md"
        unexpected.parent.mkdir(parents=True, exist_ok=True)
        unexpected.write_text(
            "# Summary\n\nSource: src/extra.py\nGenerated-By: ai-dev summarize\nPlan-ID: other\n",
            encoding="utf-8",
        )

        first_code, first_out, first_err = self._invoke(repo_root, "summarize-verify", manifest.plan_id)
        self.assertEqual(first_code, 1)
        self.assertEqual(first_err, "")
        self.assertIn("partial", first_out)

        payload = json.loads(
            (repo_root / ".ai-dev" / "summarize" / manifest.plan_id / "verification.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["overall_status"], "partial")

        markdown = (repo_root / ".ai-dev" / "summarize" / manifest.plan_id / "verification.md").read_text(encoding="utf-8")
        self.assertIn("Overall-Status: partial", markdown)

        unexpected.unlink()
        second_code, second_out, second_err = self._invoke(repo_root, "summarize-verify", manifest.plan_id)
        self.assertEqual(second_code, 0)
        self.assertEqual(second_err, "")
        self.assertIn("complete", second_out)

    def test_missing_and_corrupt_manifest_use_error_path(self) -> None:
        repo_root = self._init_repo("repo-cli-verify-bad-manifest")

        missing_code, missing_out, missing_err = self._invoke(repo_root, "summarize-verify", "missingplan")
        self.assertEqual(missing_code, 1)
        self.assertEqual(missing_out, "")
        self.assertIn("does not exist", missing_err)

        plan_id = "badplan"
        manifest_path = repo_root / ".ai-dev" / "summarize" / plan_id / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{bad json\n", encoding="utf-8")

        bad_code, bad_out, bad_err = self._invoke(repo_root, "summarize-verify", plan_id)
        self.assertEqual(bad_code, 1)
        self.assertEqual(bad_out, "")
        self.assertIn("Invalid JSON", bad_err)


if __name__ == "__main__":
    unittest.main()
