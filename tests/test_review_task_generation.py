from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_dev_flow.review_context import build_review_context
from ai_dev_flow.review_paths import build_review_artifact_paths
from ai_dev_flow.review_task_generation import (
    ReviewTaskGenerationError,
    create_review_task_file,
    plan_review_task,
    render_review_task_markdown,
)


class ReviewTaskGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _context(self, *, review_root: str) -> object:
        return build_review_context(
            scope="workflow",
            command="flow review --all",
            workflow_type="issue",
            main_branch="main",
            scratch_branch="scratch",
            current_branch="scratch",
            checkpoint=2,
            active_issue_number=16,
            active_issue_title="Review migration",
            active_issue_url="https://example.test/issues/16",
            patch_description=None,
            issue_description_status="available_local",
            issue_description_source="issues/16.md",
            acceptance_criteria_status="available_local",
            acceptance_criteria_heading="Acceptance Criteria",
            acceptance_criteria_lines=["- [ ] One", "- [ ] Two"],
            committed_reference="main...scratch",
            committed_paths=["src/a.py"],
            committed_diff_text="diff --git a/src/a.py b/src/a.py\n+DISTINCTIVE-DIFF-LINE-XYZ\n",
            committed_diff_sha256="a" * 64,
            overlay_reference="HEAD -> index",
            overlay_paths=["src/wip.py"],
            overlay_diff_text="diff --git a/src/wip.py b/src/wip.py\n+overlay\n",
            overlay_diff_sha256="b" * 64,
            all_paths=["src/a.py", "src/wip.py"],
            changes_diff_sha256="c" * 64,
            instruction_reference_paths=["ai-dev-core/workflows/review/review-documentation.md"],
            diagnostics=[],
            review_root_path=review_root,
            package_markdown_path=f"{review_root}/package.md",
            package_json_path=f"{review_root}/package.json",
            changes_diff_path=f"{review_root}/changes.diff",
            canonical_report_path=f"{review_root}/report.md",
        )

    def test_plan_review_task_is_deterministic(self) -> None:
        planned_first = plan_review_task(
            repo_root=self.repo_root,
            review_id="review-1234abcd5678ef90",
            requested_command="flow review --all",
        )
        planned_second = plan_review_task(
            repo_root=self.repo_root,
            review_id="review-1234abcd5678ef90",
            requested_command="flow review --all",
        )

        self.assertEqual(planned_first.task_id, "review-1234abcd5678ef90-task")
        self.assertEqual(
            planned_first.repository_relative_path,
            ".ai-dev/review/task.md",
        )
        self.assertEqual(planned_first.task_id, planned_second.task_id)
        self.assertEqual(planned_first.repository_relative_path, planned_second.repository_relative_path)

    def test_render_review_task_contains_required_contract_and_references(self) -> None:
        review_id = "review-1234abcd5678ef90"
        review_paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(review_root=review_paths.review_root_relative_path)
        planned = plan_review_task(
            repo_root=self.repo_root,
            review_id=review_id,
            requested_command="flow review --all",
        )

        text = render_review_task_markdown(
            planned_task=planned,
            review_paths=review_paths,
            context=context,
        )

        self.assertIn(f"- Task-ID: {planned.task_id}", text)
        self.assertIn("- Task-Type: review", text)
        self.assertIn(f"- Review-ID: {review_id}", text)
        self.assertIn("- Review-Scope: workflow", text)
        self.assertIn(f"- Package-Markdown-Path: {review_paths.package_markdown_relative_path}", text)
        self.assertIn(f"- Package-JSON-Path: {review_paths.package_json_relative_path}", text)
        self.assertIn(f"- Changes-Diff-Path: {review_paths.changes_diff_relative_path}", text)
        self.assertIn(f"- Review-Report-Path: {review_paths.canonical_report_relative_path}", text)
        self.assertIn("Acceptance criteria coverage", text)
        self.assertIn("Blocking findings", text)
        self.assertIn("Non-blocking findings", text)
        self.assertIn("Uncertainties/missing context", text)
        self.assertIn("## Required Report Contract", text)
        self.assertIn("# AI Dev Review Report", text)
        self.assertIn("Status: pass | pass-with-notes | blocked", text)
        self.assertGreaterEqual(
            text.count("Do not modify source files, package files, workflow state, Git state, or generated task files."),
            2,
        )

    def test_render_review_task_does_not_embed_authoritative_diff_contents(self) -> None:
        review_id = "review-2234abcd5678ef90"
        review_paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(review_root=review_paths.review_root_relative_path)
        planned = plan_review_task(
            repo_root=self.repo_root,
            review_id=review_id,
            requested_command="flow review --all",
        )

        text = render_review_task_markdown(
            planned_task=planned,
            review_paths=review_paths,
            context=context,
        )

        self.assertNotIn("DISTINCTIVE-DIFF-LINE-XYZ", text)
        self.assertNotIn("diff --git", text)

    def test_render_review_task_is_deterministic(self) -> None:
        review_id = "review-3234abcd5678ef90"
        review_paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(review_root=review_paths.review_root_relative_path)
        planned = plan_review_task(
            repo_root=self.repo_root,
            review_id=review_id,
            requested_command="flow review --all",
        )

        first = render_review_task_markdown(
            planned_task=planned,
            review_paths=review_paths,
            context=context,
        )
        second = render_review_task_markdown(
            planned_task=planned,
            review_paths=review_paths,
            context=context,
        )

        self.assertEqual(first, second)

    def test_create_review_task_file_is_idempotent_and_rejects_divergence(self) -> None:
        review_id = "review-4234abcd5678ef90"
        review_paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(review_root=review_paths.review_root_relative_path)
        planned = plan_review_task(
            repo_root=self.repo_root,
            review_id=review_id,
            requested_command="flow review --all",
        )
        markdown = render_review_task_markdown(
            planned_task=planned,
            review_paths=review_paths,
            context=context,
        )

        created_first = create_review_task_file(planned_task=planned, markdown_text=markdown)
        created_second = create_review_task_file(planned_task=planned, markdown_text=markdown)

        self.assertTrue(created_first)
        self.assertFalse(created_second)

        with self.assertRaises(ReviewTaskGenerationError):
            create_review_task_file(
                planned_task=planned,
                markdown_text=markdown + "\nDIVERGENT\n",
            )


if __name__ == "__main__":
    unittest.main()
