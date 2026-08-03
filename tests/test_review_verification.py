from __future__ import annotations

import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ai_dev_flow.review_paths import build_review_artifact_paths
from ai_dev_flow.review_verification import (
    OVERALL_STATUS_COMPLETE,
    OVERALL_STATUS_INCOMPLETE,
    OVERALL_STATUS_INVALID,
    REPORT_STATE_DECISION_OUTSIDE_SECTION,
    REPORT_STATE_DUPLICATE_SECTION,
    REPORT_STATE_MISSING_DECISION,
    REPORT_STATE_MISSING_SECTION,
    REPORT_STATE_MULTIPLE_DECISIONS,
    REPORT_STATE_SECTION_ORDER_INVALID,
    REPORT_STATE_VALID,
    render_verification_markdown,
    verification_result_json,
    verify_review,
    write_verification_artifacts,
)


class ReviewVerificationTests(unittest.TestCase):
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
        self._run_git(repo_root, "config", "user.name", "Review Verification Tests")
        self._run_git(repo_root, "config", "user.email", "review-verification-tests@example.com")

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

    def _prepare_review(self, repo_root: Path) -> str:
        (repo_root / "change.txt").write_text("content\n", encoding="utf-8")
        code, _, err = self._invoke(repo_root, "review")
        self.assertEqual(code, 0, msg=err)

        payload = json.loads(
            (repo_root / ".ai-dev" / "review" / "package.json").read_text(encoding="utf-8")
        )
        return payload["review_id"]

    def _invoke(self, cwd: Path, *arguments: str) -> tuple[int, str, str]:
        from ai_dev_flow import cli

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

    def _write_report(self, repo_root: Path, review_id: str, *, decision: str | None) -> None:
        paths = build_review_artifact_paths(repo_root, review_id)
        decision_line = "" if decision is None else f"- Status: {decision}\n"
        report_text = (
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n"
            f"{decision_line}\n"
            "## Blocking Findings\n\n"
            "## Non-Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n"
        )
        paths.canonical_report_absolute_path.write_text(report_text, encoding="utf-8")

    def test_verify_review_complete_and_deterministic_artifacts(self) -> None:
        repo_root = self._init_repo("repo-review-verify-complete")
        self._activate_issue_workflow(repo_root, issue_number=101)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_COMPLETE)
        self.assertEqual(result.report_state.status, REPORT_STATE_VALID)
        self.assertEqual(result.review_decision, "pass")

        markdown_first = render_verification_markdown(result)
        markdown_second = render_verification_markdown(result)
        self.assertEqual(markdown_first, markdown_second)

        payload = verification_result_json(result)
        self.assertEqual(payload["overall_status"], OVERALL_STATUS_COMPLETE)
        self.assertEqual(payload["review_decision"], "pass")
        self.assertNotIn("decision_status", payload)

        markdown_path, json_path = write_verification_artifacts(repo_root=repo_root, result=result)
        self.assertTrue((repo_root / markdown_path).exists())
        self.assertTrue((repo_root / json_path).exists())

    def test_verify_review_blocked_decision_is_complete(self) -> None:
        repo_root = self._init_repo("repo-review-verify-blocked")
        self._activate_issue_workflow(repo_root, issue_number=102)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="blocked")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_COMPLETE)
        self.assertEqual(result.review_decision, "blocked")
        self.assertEqual(
            result.recommended_next_action,
            "Address blocking findings before checkpoint or commit.",
        )

    def test_verify_review_missing_report_is_incomplete(self) -> None:
        repo_root = self._init_repo("repo-review-verify-missing-report")
        self._activate_issue_workflow(repo_root, issue_number=103)
        review_id = self._prepare_review(repo_root)

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INCOMPLETE)
        self.assertEqual(result.report_state.status, "missing")

    def test_verify_review_pass_with_notes_is_complete(self) -> None:
        repo_root = self._init_repo("repo-review-verify-pass-with-notes")
        self._activate_issue_workflow(repo_root, issue_number=105)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass-with-notes")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_COMPLETE)
        self.assertEqual(result.review_decision, "pass-with-notes")

    def test_verify_review_package_integrity_failure_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-package-invalid")
        self._activate_issue_workflow(repo_root, issue_number=106)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.changes_diff_absolute_path.write_text("corrupted\n", encoding="utf-8")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.package_state.status, "digest-mismatch")

    def test_verify_review_task_integrity_failure_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-task-invalid")
        self._activate_issue_workflow(repo_root, issue_number=107)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        task_path = repo_root / ".ai-dev" / "review" / "task.md"
        original = task_path.read_text(encoding="utf-8")
        task_path.write_text(original.replace("- Review-ID: ", "- Review-ID: mismatch-", 1), encoding="utf-8")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.task_state.status, "invalid")

    def test_verify_review_report_missing_required_section_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-report-missing-section")
        self._activate_issue_workflow(repo_root, issue_number=108)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        original = paths.canonical_report_absolute_path.read_text(encoding="utf-8")
        paths.canonical_report_absolute_path.write_text(
            original.replace("## Test Assessment\n\n", "", 1),
            encoding="utf-8",
        )

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_MISSING_SECTION)

    def test_verify_review_report_duplicate_section_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-report-duplicate-section")
        self._activate_issue_workflow(repo_root, issue_number=109)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        original = paths.canonical_report_absolute_path.read_text(encoding="utf-8")
        paths.canonical_report_absolute_path.write_text(
            original + "\n## Summary\n",
            encoding="utf-8",
        )

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_DUPLICATE_SECTION)

    def test_verify_review_report_out_of_order_sections_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-report-order")
        self._activate_issue_workflow(repo_root, issue_number=110)
        review_id = self._prepare_review(repo_root)

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n"
            "- Status: pass\n\n"
            "## Non-Blocking Findings\n\n"
            "## Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n",
            encoding="utf-8",
        )

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_SECTION_ORDER_INVALID)

    def test_verify_review_decision_outside_decision_section_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-decision-outside")
        self._activate_issue_workflow(repo_root, issue_number=111)
        review_id = self._prepare_review(repo_root)

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n\n"
            "## Blocking Findings\n"
            "- Status: blocked\n\n"
            "## Non-Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n",
            encoding="utf-8",
        )

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_DECISION_OUTSIDE_SECTION)

    def test_verify_review_multiple_decisions_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-multi-decision")
        self._activate_issue_workflow(repo_root, issue_number=112)
        review_id = self._prepare_review(repo_root)

        paths = build_review_artifact_paths(repo_root, review_id)
        paths.canonical_report_absolute_path.write_text(
            "# AI Dev Review Report\n\n"
            f"Review-ID: {review_id}\n"
            "Generated-By: external AI review\n"
            f"Package-Path: {paths.package_markdown_relative_path}\n\n"
            "## Decision\n"
            "- Status: pass\n"
            "- Status: blocked\n\n"
            "## Blocking Findings\n\n"
            "## Non-Blocking Findings\n\n"
            "## Acceptance Criteria Assessment\n\n"
            "## Test Assessment\n\n"
            "## Uncertainties and Missing Context\n\n"
            "## Summary\n",
            encoding="utf-8",
        )

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_MULTIPLE_DECISIONS)

    def test_verify_review_package_missing_schema_version_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-schema-missing")
        self._activate_issue_workflow(repo_root, issue_number=113)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        payload = json.loads(paths.package_json_absolute_path.read_text(encoding="utf-8"))
        del payload["schema_version"]
        paths.package_json_absolute_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertIn("missing-schema-version", result.package_state.reason_codes)

    def test_verify_review_package_artifact_path_traversal_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-artifact-traversal")
        self._activate_issue_workflow(repo_root, issue_number=114)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        paths = build_review_artifact_paths(repo_root, review_id)
        payload = json.loads(paths.package_json_absolute_path.read_text(encoding="utf-8"))
        payload["artifacts"]["package_json_path"] = "../escape/package.json"
        paths.package_json_absolute_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertIn("artifact-path-traversal-or-absolute", result.package_state.reason_codes)

    def test_verify_review_task_duplicate_marker_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-task-duplicate-marker")
        self._activate_issue_workflow(repo_root, issue_number=115)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision="pass")

        task_path = repo_root / ".ai-dev" / "review" / "task.md"
        original = task_path.read_text(encoding="utf-8")
        task_path.write_text(original + f"\n- Review-ID: {review_id}\n", encoding="utf-8")

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertIn("task-duplicate-review-id", result.task_state.reason_codes)

    def test_verify_review_missing_decision_is_invalid(self) -> None:
        repo_root = self._init_repo("repo-review-verify-missing-decision")
        self._activate_issue_workflow(repo_root, issue_number=104)
        review_id = self._prepare_review(repo_root)
        self._write_report(repo_root, review_id, decision=None)

        result = verify_review(repo_root, review_id)
        self.assertEqual(result.overall_status, OVERALL_STATUS_INVALID)
        self.assertEqual(result.report_state.status, REPORT_STATE_MISSING_DECISION)


if __name__ == "__main__":
    unittest.main()
