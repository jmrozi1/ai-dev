from __future__ import annotations

import unittest

from ai_dev_flow.review_context import (
    build_review_context,
    build_review_id,
    extract_acceptance_criteria_section,
    review_context_payload,
)


class ReviewContextTests(unittest.TestCase):
    def _context(self, *, committed_sha: str, overlay_sha: str, changes_sha: str) -> object:
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
            committed_paths=["b.txt", "a.txt"],
            committed_diff_text="diff --git a/a.txt b/a.txt\n",
            committed_diff_sha256=committed_sha,
            overlay_reference="HEAD -> index",
            overlay_paths=["a.txt", "wip.txt"],
            overlay_diff_text="diff --git a/wip.txt b/wip.txt\n",
            overlay_diff_sha256=overlay_sha,
            all_paths=["a.txt", "b.txt", "wip.txt"],
            changes_diff_sha256=changes_sha,
            instruction_reference_paths=[
                "ai-dev-core/workflows/review/finding-template.md",
                "ai-dev-core/workflows/review/review-documentation.md",
            ],
            diagnostics=[],
            review_root_path=".ai-dev/review",
            package_markdown_path=".ai-dev/review/package.md",
            package_json_path=".ai-dev/review/package.json",
            changes_diff_path=".ai-dev/review/changes.diff",
            canonical_report_path=".ai-dev/review/report.md",
        )

    def test_acceptance_criteria_extraction_case_insensitive_stops_at_same_level(self) -> None:
        body = (
            "# Issue 16\n\n"
            "## ACCEPTANCE CRITERIA\n"
            "- [ ] deterministic package\n"
            "- [ ] immutable artifacts\n\n"
            "## Notes\n"
            "more context\n"
        )

        section = extract_acceptance_criteria_section(body)
        self.assertTrue(section.found)
        self.assertEqual(section.heading, "ACCEPTANCE CRITERIA")
        self.assertEqual(
            section.lines,
            ("- [ ] deterministic package", "- [ ] immutable artifacts"),
        )

    def test_acceptance_criteria_missing(self) -> None:
        section = extract_acceptance_criteria_section("# Issue\n\n## Summary\nnone\n")
        self.assertFalse(section.found)
        self.assertEqual(section.lines, ())

    def test_review_id_is_stable_for_same_semantic_context(self) -> None:
        context = self._context(
            committed_sha="a" * 64,
            overlay_sha="b" * 64,
            changes_sha="c" * 64,
        )
        # Artifact paths include an ID placeholder, but ID hash excludes these to avoid circularity.
        altered = build_review_context(
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
            committed_paths=["a.txt", "b.txt"],
            committed_diff_text="diff --git a/a.txt b/a.txt\n",
            committed_diff_sha256="a" * 64,
            overlay_reference="HEAD -> index",
            overlay_paths=["a.txt", "wip.txt"],
            overlay_diff_text="diff --git a/wip.txt b/wip.txt\n",
            overlay_diff_sha256="b" * 64,
            all_paths=["a.txt", "b.txt", "wip.txt"],
            changes_diff_sha256="c" * 64,
            instruction_reference_paths=[
                "ai-dev-core/workflows/review/review-documentation.md",
                "ai-dev-core/workflows/review/finding-template.md",
            ],
            diagnostics=[],
            review_root_path=".ai-dev/review",
            package_markdown_path=".ai-dev/review/package.md",
            package_json_path=".ai-dev/review/package.json",
            changes_diff_path=".ai-dev/review/changes.diff",
            canonical_report_path=".ai-dev/review/report.md",
        )

        self.assertEqual(build_review_id(context), build_review_id(altered))

    def test_review_id_changes_when_scoped_digests_change(self) -> None:
        left = self._context(
            committed_sha="a" * 64,
            overlay_sha="b" * 64,
            changes_sha="c" * 64,
        )
        right = self._context(
            committed_sha="d" * 64,
            overlay_sha="b" * 64,
            changes_sha="c" * 64,
        )
        self.assertNotEqual(build_review_id(left), build_review_id(right))

    def test_review_id_changes_when_authoritative_changes_diff_digest_changes(self) -> None:
        left = self._context(
            committed_sha="a" * 64,
            overlay_sha="b" * 64,
            changes_sha="c" * 64,
        )
        right = self._context(
            committed_sha="a" * 64,
            overlay_sha="b" * 64,
            changes_sha="d" * 64,
        )
        self.assertNotEqual(build_review_id(left), build_review_id(right))

    def test_payload_shape_is_structured_and_no_duplicate_context_blocks(self) -> None:
        context = self._context(
            committed_sha="a" * 64,
            overlay_sha="b" * 64,
            changes_sha="c" * 64,
        )
        payload = review_context_payload(context)
        self.assertEqual(payload["schema_version"], 1)
        for key in [
            "scope",
            "workflow",
            "repository",
            "ticket",
            "acceptance_criteria",
            "changes",
            "instructions",
            "artifacts",
            "diagnostics",
        ]:
            self.assertIn(key, payload)

        self.assertNotIn("context", payload)
        self.assertNotIn("stablePayload", payload)


if __name__ == "__main__":
    unittest.main()
