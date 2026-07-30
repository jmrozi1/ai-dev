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
from ai_dev_flow.review_paths import build_review_artifact_paths


class _SpyPresenter:
    def __init__(self) -> None:
        self.called = 0
        self.presented_path: Path | None = None

    def present(self, report_path: Path) -> None:
        self.called += 1
        self.presented_path = report_path


class ReviewVerifyCliTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Review Verify CLI Tests")
        self._run_git(repo_root, "config", "user.email", "review-verify-cli-tests@example.com")

        (repo_root / ".gitignore").write_text(".ai-dev/workflow.json\n", encoding="utf-8")
        (repo_root / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._run_git(repo_root, "add", ".gitignore", "tracked.txt")
        self._run_git(repo_root, "commit", "-q", "-m", "initial commit")
        self._run_git(repo_root, "branch", "-M", "main")
        return repo_root

    def _activate_issue_workflow(self, repo_root: Path, issue_number: int) -> None:
        self._run_git(repo_root, "checkout", "-q", "-b", "scratch")
        workflow_path = repo_root / ".ai-dev" / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            json.dumps(
                {
                    "activeIssueNumber": issue_number,
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "checkpoint": 0,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

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

    def _prepare_review(self, repo_root: Path) -> str:
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")
        code, out, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0, msg=err)
        self.assertIn("Review task:", out)

        review_dirs = sorted(path for path in (repo_root / ".ai-dev" / "reviews").iterdir() if path.is_dir())
        self.assertTrue(review_dirs)
        return review_dirs[-1].name

    def _write_report(self, repo_root: Path, review_id: str, *, decision: str) -> None:
        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n"
            f"- Status: {decision}\n\n"
            "## Blocking Findings\n\n"
            "## Non-Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n",
            encoding="utf-8",
        )

    def test_explicit_review_id_complete_exit_zero(self) -> None:
        repo_root = self._init_repo("repo-review-verify-explicit")
        self._activate_issue_workflow(repo_root, issue_number=201)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        user_config = self.tmp_path / "reports-path-only.yaml"
        user_config.write_text("reports:\n  presentation: path-only\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(user_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "review-verify", review_id)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Review verification status for", out)
        self.assertIn("complete", out)
        self.assertIn("Review decision: pass", out)
        self.assertIn(f"Review report: .ai-dev/reviews/{review_id}/report.md", out)
        self.assertIn("Verification report: .ai-dev/reviews/", out)
        self.assertIn("Verification JSON: .ai-dev/reviews/", out)

    def test_omitted_review_id_uses_current_review_pointer(self) -> None:
        repo_root = self._init_repo("repo-review-verify-current")
        self._activate_issue_workflow(repo_root, issue_number=202)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass-with-notes")

        code, out, err = self._invoke(repo_root, "review-verify")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(review_id, out)

    def test_blocked_decision_is_complete_exit_zero(self) -> None:
        repo_root = self._init_repo("repo-review-verify-blocked-exit-zero")
        self._activate_issue_workflow(repo_root, issue_number=205)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="blocked")

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Review verification status for", out)
        self.assertIn("complete", out)
        self.assertIn("Review decision: blocked", out)

    def test_pass_with_notes_is_complete_exit_zero(self) -> None:
        repo_root = self._init_repo("repo-review-verify-pass-with-notes")
        self._activate_issue_workflow(repo_root, issue_number=206)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass-with-notes")

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Review verification status for", out)
        self.assertIn("complete", out)
        self.assertIn("Review decision: pass-with-notes", out)

    def test_current_task_not_review_is_rejected(self) -> None:
        repo_root = self._init_repo("repo-review-verify-not-review")
        (repo_root / ".ai-dev").mkdir(parents=True, exist_ok=True)
        (repo_root / ".ai-dev" / "current-task.md").write_text(
            "# Current AI Dev Task\n\n"
            "- Task-ID: summarize-sample-coordinator\n"
            "- Task-Type: summarize\n"
            "- Task-File: .ai-dev/tasks/summarize-sample-coordinator.md\n",
            encoding="utf-8",
        )

        code, out, err = self._invoke(repo_root, "review-verify")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Current task is not review", err)

    def test_invalid_report_contract_returns_one(self) -> None:
        repo_root = self._init_repo("repo-review-verify-invalid-report")
        self._activate_issue_workflow(repo_root, issue_number=203)
        review_id = self._prepare_review(repo_root)

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n"
            "- Status: invalid\n",
            encoding="utf-8",
        )

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("invalid", out)

    def test_missing_report_returns_incomplete_exit_one(self) -> None:
        repo_root = self._init_repo("repo-review-verify-missing-report")
        self._activate_issue_workflow(repo_root, issue_number=207)
        review_id = self._prepare_review(repo_root)

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("Review verification status for", out)
        self.assertIn("incomplete", out)

    def test_package_integrity_failure_returns_invalid_exit_one(self) -> None:
        repo_root = self._init_repo("repo-review-verify-package-invalid")
        self._activate_issue_workflow(repo_root, issue_number=208)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.changes_diff_absolute_path.write_text("corrupt\n", encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("invalid", out)

    def test_task_integrity_failure_returns_invalid_exit_one(self) -> None:
        repo_root = self._init_repo("repo-review-verify-task-invalid")
        self._activate_issue_workflow(repo_root, issue_number=209)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        task_path = repo_root / ".ai-dev" / "tasks" / f"{review_id}-task.md"
        original = task_path.read_text(encoding="utf-8")
        task_path.write_text(original.replace("- Review-ID: ", "- Review-ID: mismatch-", 1), encoding="utf-8")

        code, out, err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("invalid", out)

    def test_report_presenter_runs_only_for_valid_report(self) -> None:
        repo_root = self._init_repo("repo-review-verify-presenter")
        self._activate_issue_workflow(repo_root, issue_number=204)
        review_id = self._prepare_review(repo_root)

        spy = _SpyPresenter()
        with patch("ai_dev_flow.cli.build_report_presenter", return_value=spy):
            # Missing report is invalid; presenter should not be used.
            missing_code, _, missing_err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(missing_code, 1)
        self.assertEqual(missing_err, "")
        self.assertEqual(spy.called, 0)

        self._write_report(repo_root, review_id, decision="pass")
        with patch("ai_dev_flow.cli.build_report_presenter", return_value=spy):
            valid_code, _, valid_err = self._invoke(repo_root, "review-verify", review_id)
        self.assertEqual(valid_code, 0)
        self.assertEqual(valid_err, "")
        self.assertEqual(spy.called, 1)
        self.assertIsNotNone(spy.presented_path)
        assert spy.presented_path is not None
        self.assertEqual(spy.presented_path.name, "report.md")

    def test_missing_report_with_invalid_presentation_config_returns_incomplete(self) -> None:
        repo_root = self._init_repo("repo-review-verify-missing-report-invalid-config")
        self._activate_issue_workflow(repo_root, issue_number=210)
        review_id = self._prepare_review(repo_root)

        invalid_config = self.tmp_path / "invalid-presentation.yaml"
        invalid_config.write_text("reports:\n  presentation: invalid-mode\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(invalid_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "review-verify", review_id)

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("incomplete", out)
        self.assertIn("Review report path:", out)

        verification_md = repo_root / ".ai-dev" / "reviews" / review_id / "verification.md"
        verification_json = repo_root / ".ai-dev" / "reviews" / review_id / "verification.json"
        self.assertTrue(verification_md.exists())
        self.assertTrue(verification_json.exists())

    def test_invalid_report_never_builds_presenter(self) -> None:
        repo_root = self._init_repo("repo-review-verify-invalid-report-no-builder")
        self._activate_issue_workflow(repo_root, issue_number=211)
        review_id = self._prepare_review(repo_root)

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n\n"
            "## Blocking Findings\n\n"
            "## Non-Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n",
            encoding="utf-8",
        )

        with patch("ai_dev_flow.cli.build_report_presenter", side_effect=AssertionError("should not build")) as builder:
            code, out, err = self._invoke(repo_root, "review-verify", review_id)

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("invalid", out)
        self.assertEqual(builder.call_count, 0)

    def test_package_failure_independent_of_presentation_config(self) -> None:
        repo_root = self._init_repo("repo-review-verify-package-failure-invalid-config")
        self._activate_issue_workflow(repo_root, issue_number=212)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.changes_diff_absolute_path.write_text("tampered\n", encoding="utf-8")

        invalid_config = self.tmp_path / "invalid-presentation-package.yaml"
        invalid_config.write_text("reports:\n  presentation: invalid-mode\n", encoding="utf-8")

        with patch.dict(os.environ, {"AI_DEV_CONFIG": str(invalid_config)}, clear=False):
            code, out, err = self._invoke(repo_root, "review-verify", review_id)

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("invalid", out)

    def test_valid_report_presentation_failure_warns_and_falls_back_to_path(self) -> None:
        repo_root = self._init_repo("repo-review-verify-valid-presentation-failure")
        self._activate_issue_workflow(repo_root, issue_number=213)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        class _FailingPresenter:
            def present(self, report_path: Path) -> None:
                raise cli.ReportPresentationError("presenter failed")

        with patch("ai_dev_flow.cli.build_report_presenter", return_value=_FailingPresenter()):
            code, out, err = self._invoke(repo_root, "review-verify", review_id)

        self.assertEqual(code, 0)
        self.assertIn("Warning: presenter failed", err)
        self.assertIn("Review report path:", out)
        self.assertIn("complete", out)

    def test_review_verify_help_mentions_canonical_report(self) -> None:
        repo_root = self._init_repo("repo-review-verify-help")
        code, out, err = self._invoke(repo_root, "review-verify", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("present the canonical review report", out)
        self.assertNotIn("present verification.md", out)


if __name__ == "__main__":
    unittest.main()
