from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_dev_flow.json_files import JsonFileError
from ai_dev_flow.review_context import build_review_context
from ai_dev_flow.review_package import ReviewPackageError, create_review_package, render_changes_diff
from ai_dev_flow.review_paths import build_review_artifact_paths


class ReviewPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _context(self, *, root: str) -> object:
        return build_review_context(
            scope="workflow",
            command="flow review --all",
            workflow_type="issue",
            main_branch="main",
            scratch_branch="scratch",
            current_branch="scratch",
            checkpoint=1,
            active_issue_number=16,
            active_issue_title="Review migration",
            active_issue_url="https://example.test/issues/16",
            patch_description=None,
            issue_description_status="unavailable_local",
            issue_description_source=None,
            acceptance_criteria_status="unavailable_local",
            acceptance_criteria_heading="Acceptance criteria",
            acceptance_criteria_lines=[],
            committed_reference="main...scratch",
            committed_paths=["src/app.py"],
            committed_diff_text="diff --git a/src/app.py b/src/app.py\n+one\n",
            committed_diff_sha256=hashlib.sha256("diff --git a/src/app.py b/src/app.py\n+one\n".encode("utf-8")).hexdigest(),
            overlay_reference="HEAD -> index",
            overlay_paths=["src/wip.py"],
            overlay_diff_text="diff --git a/src/wip.py b/src/wip.py\n+two\n",
            overlay_diff_sha256=hashlib.sha256("diff --git a/src/wip.py b/src/wip.py\n+two\n".encode("utf-8")).hexdigest(),
            all_paths=["src/app.py", "src/wip.py"],
            changes_diff_sha256="0" * 64,
            instruction_reference_paths=["ai-dev-core/workflows/review/review-documentation.md"],
            diagnostics=["Issue body unavailable locally; acceptance criteria extraction skipped."],
            review_root_path=root,
            package_markdown_path=f"{root}/package.md",
            package_json_path=f"{root}/package.json",
            changes_diff_path=f"{root}/changes.diff",
            canonical_report_path=f"{root}/report.md",
        )

    def test_create_review_package_writes_all_artifacts_and_no_diff_embed(self) -> None:
        review_id = "review-1234abcd5678ef90"
        paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(root=paths.review_root_relative_path)

        changes_diff_text = render_changes_diff(context)
        changes_digest = hashlib.sha256(changes_diff_text.encode("utf-8")).hexdigest()
        context = build_review_context(
            scope=context.scope,
            command=context.command,
            workflow_type=context.workflow_type,
            main_branch=context.main_branch,
            scratch_branch=context.scratch_branch,
            current_branch=context.current_branch,
            checkpoint=context.checkpoint,
            active_issue_number=context.active_issue_number,
            active_issue_title=context.active_issue_title,
            active_issue_url=context.active_issue_url,
            patch_description=context.patch_description,
            issue_description_status=context.issue_description_status,
            issue_description_source=context.issue_description_source,
            acceptance_criteria_status=context.acceptance_criteria_status,
            acceptance_criteria_heading=context.acceptance_criteria_heading,
            acceptance_criteria_lines=context.acceptance_criteria_lines,
            committed_reference=context.committed.reference,
            committed_paths=context.committed.paths,
            committed_diff_text=context.committed.diff_text,
            committed_diff_sha256=context.committed.diff_sha256,
            overlay_reference=context.overlay.reference,
            overlay_paths=context.overlay.paths,
            overlay_diff_text=context.overlay.diff_text,
            overlay_diff_sha256=context.overlay.diff_sha256,
            all_paths=context.all_paths,
            changes_diff_sha256=changes_digest,
            instruction_reference_paths=context.instruction_reference_paths,
            diagnostics=context.diagnostics,
            review_root_path=context.artifacts.review_root_path,
            package_markdown_path=context.artifacts.package_markdown_path,
            package_json_path=context.artifacts.package_json_path,
            changes_diff_path=context.artifacts.changes_diff_path,
            canonical_report_path=context.artifacts.canonical_report_path,
        )

        create_review_package(
            repo_root=self.repo_root,
            review_paths=paths,
            review_id=review_id,
            context=context,
            changes_diff_text=changes_diff_text,
        )

        self.assertTrue(paths.package_markdown_absolute_path.exists())
        self.assertTrue(paths.package_json_absolute_path.exists())
        self.assertTrue(paths.changes_diff_absolute_path.exists())

        package_text = paths.package_markdown_absolute_path.read_text(encoding="utf-8")
        self.assertIn("## Change Package", package_text)
        self.assertNotIn("```diff", package_text)
        self.assertIn("Changes-Diff-Path:", package_text)

        changes_text = paths.changes_diff_absolute_path.read_text(encoding="utf-8")
        self.assertIn("## Committed workflow diff: main...scratch", changes_text)
        self.assertIn("## Staged overlay diff: HEAD -> index", changes_text)
        self.assertIn("+one", changes_text)

        payload = json.loads(paths.package_json_absolute_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["review_id"], review_id)
        self.assertEqual(payload["changes"]["changes_diff_sha256"], changes_digest)

    def test_create_review_package_is_idempotent_for_identical_replay(self) -> None:
        review_id = "review-2234abcd5678ef90"
        paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(root=paths.review_root_relative_path)
        changes_diff_text = render_changes_diff(context)
        changes_digest = hashlib.sha256(changes_diff_text.encode("utf-8")).hexdigest()
        context = build_review_context(
            scope=context.scope,
            command=context.command,
            workflow_type=context.workflow_type,
            main_branch=context.main_branch,
            scratch_branch=context.scratch_branch,
            current_branch=context.current_branch,
            checkpoint=context.checkpoint,
            active_issue_number=context.active_issue_number,
            active_issue_title=context.active_issue_title,
            active_issue_url=context.active_issue_url,
            patch_description=context.patch_description,
            issue_description_status=context.issue_description_status,
            issue_description_source=context.issue_description_source,
            acceptance_criteria_status=context.acceptance_criteria_status,
            acceptance_criteria_heading=context.acceptance_criteria_heading,
            acceptance_criteria_lines=context.acceptance_criteria_lines,
            committed_reference=context.committed.reference,
            committed_paths=context.committed.paths,
            committed_diff_text=context.committed.diff_text,
            committed_diff_sha256=context.committed.diff_sha256,
            overlay_reference=context.overlay.reference,
            overlay_paths=context.overlay.paths,
            overlay_diff_text=context.overlay.diff_text,
            overlay_diff_sha256=context.overlay.diff_sha256,
            all_paths=context.all_paths,
            changes_diff_sha256=changes_digest,
            instruction_reference_paths=context.instruction_reference_paths,
            diagnostics=context.diagnostics,
            review_root_path=context.artifacts.review_root_path,
            package_markdown_path=context.artifacts.package_markdown_path,
            package_json_path=context.artifacts.package_json_path,
            changes_diff_path=context.artifacts.changes_diff_path,
            canonical_report_path=context.artifacts.canonical_report_path,
        )

        create_review_package(
            repo_root=self.repo_root,
            review_paths=paths,
            review_id=review_id,
            context=context,
            changes_diff_text=changes_diff_text,
        )

        create_review_package(
            repo_root=self.repo_root,
            review_paths=paths,
            review_id=review_id,
            context=context,
            changes_diff_text=changes_diff_text,
        )

    def test_create_review_package_rejects_divergent_collision(self) -> None:
        review_id = "review-3234abcd5678ef90"
        paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(root=paths.review_root_relative_path)
        changes_diff_text = render_changes_diff(context)
        changes_digest = hashlib.sha256(changes_diff_text.encode("utf-8")).hexdigest()
        context = build_review_context(
            scope=context.scope,
            command=context.command,
            workflow_type=context.workflow_type,
            main_branch=context.main_branch,
            scratch_branch=context.scratch_branch,
            current_branch=context.current_branch,
            checkpoint=context.checkpoint,
            active_issue_number=context.active_issue_number,
            active_issue_title=context.active_issue_title,
            active_issue_url=context.active_issue_url,
            patch_description=context.patch_description,
            issue_description_status=context.issue_description_status,
            issue_description_source=context.issue_description_source,
            acceptance_criteria_status=context.acceptance_criteria_status,
            acceptance_criteria_heading=context.acceptance_criteria_heading,
            acceptance_criteria_lines=context.acceptance_criteria_lines,
            committed_reference=context.committed.reference,
            committed_paths=context.committed.paths,
            committed_diff_text=context.committed.diff_text,
            committed_diff_sha256=context.committed.diff_sha256,
            overlay_reference=context.overlay.reference,
            overlay_paths=context.overlay.paths,
            overlay_diff_text=context.overlay.diff_text,
            overlay_diff_sha256=context.overlay.diff_sha256,
            all_paths=context.all_paths,
            changes_diff_sha256=changes_digest,
            instruction_reference_paths=context.instruction_reference_paths,
            diagnostics=context.diagnostics,
            review_root_path=context.artifacts.review_root_path,
            package_markdown_path=context.artifacts.package_markdown_path,
            package_json_path=context.artifacts.package_json_path,
            changes_diff_path=context.artifacts.changes_diff_path,
            canonical_report_path=context.artifacts.canonical_report_path,
        )

        create_review_package(
            repo_root=self.repo_root,
            review_paths=paths,
            review_id=review_id,
            context=context,
            changes_diff_text=changes_diff_text,
        )

        with self.assertRaises(ReviewPackageError):
            create_review_package(
                repo_root=self.repo_root,
                review_paths=paths,
                review_id=review_id,
                context=context,
                changes_diff_text=changes_diff_text + "# divergent\n",
            )

    def test_create_review_package_rejects_partial_directory(self) -> None:
        review_id = "review-4234abcd5678ef90"
        paths = build_review_artifact_paths(self.repo_root, review_id)
        paths.review_root_absolute_path.mkdir(parents=True, exist_ok=True)
        paths.package_markdown_absolute_path.write_text("partial\n", encoding="utf-8")

        context = self._context(root=paths.review_root_relative_path)
        with self.assertRaises(ReviewPackageError):
            create_review_package(
                repo_root=self.repo_root,
                review_paths=paths,
                review_id=review_id,
                context=context,
                changes_diff_text=render_changes_diff(context),
            )

    def test_create_review_package_rolls_back_on_failure(self) -> None:
        review_id = "review-5234abcd5678ef90"
        paths = build_review_artifact_paths(self.repo_root, review_id)
        context = self._context(root=paths.review_root_relative_path)

        original_write_text_atomic = __import__(
            "ai_dev_flow.review_package", fromlist=["write_text_atomic"]
        ).write_text_atomic

        def fail_on_package_json(path: Path, text: str) -> None:
            if path.name == "package.json":
                raise JsonFileError("simulated failure")
            original_write_text_atomic(path, text)

        with patch("ai_dev_flow.review_package.write_text_atomic", side_effect=fail_on_package_json):
            with self.assertRaises(ReviewPackageError):
                create_review_package(
                    repo_root=self.repo_root,
                    review_paths=paths,
                    review_id=review_id,
                    context=context,
                    changes_diff_text=render_changes_diff(context),
                )

        self.assertFalse(paths.package_markdown_absolute_path.exists())
        self.assertFalse(paths.package_json_absolute_path.exists())
        self.assertFalse(paths.changes_diff_absolute_path.exists())
        self.assertFalse(paths.review_root_absolute_path.exists())


if __name__ == "__main__":
    unittest.main()
