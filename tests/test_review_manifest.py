from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.review_manifest import (
    ReviewManifestError,
    resolve_current_review_id,
    validate_review_id,
)


class ReviewManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        (self.repo_root / ".ai-dev").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_current_task(self, text: str) -> None:
        (self.repo_root / ".ai-dev" / "current-task.md").write_text(text, encoding="utf-8")

    def test_validate_review_id_accepts_expected_shape(self) -> None:
        self.assertEqual(validate_review_id("review-0123456789abcdef"), "review-0123456789abcdef")

    def test_validate_review_id_rejects_invalid_values(self) -> None:
        with self.assertRaises(ReviewManifestError):
            validate_review_id("review-ABCDEF0123456789")

        with self.assertRaises(ReviewManifestError):
            validate_review_id("review-123")

        with self.assertRaises(ReviewManifestError):
            validate_review_id("../review-0123456789abcdef")

    def test_resolve_current_review_id_success(self) -> None:
        self._write_current_task(
            "# Current AI Dev Task\n\n"
            "- Task-ID: review-0123456789abcdef-task\n"
            "- Task-Type: review\n"
            "- Task-File: .ai-dev/tasks/review-0123456789abcdef-task.md\n"
        )

        self.assertEqual(resolve_current_review_id(self.repo_root), "review-0123456789abcdef")

    def test_resolve_current_review_id_rejects_non_review_pointer(self) -> None:
        self._write_current_task(
            "# Current AI Dev Task\n\n"
            "- Task-ID: summarize-plan-coordinator\n"
            "- Task-Type: summarize\n"
            "- Task-File: .ai-dev/tasks/summarize-plan-coordinator.md\n"
        )

        with self.assertRaisesRegex(ReviewManifestError, "Current task is not review"):
            resolve_current_review_id(self.repo_root)

    def test_resolve_current_review_id_rejects_inconsistent_task_file(self) -> None:
        self._write_current_task(
            "# Current AI Dev Task\n\n"
            "- Task-ID: review-0123456789abcdef-task\n"
            "- Task-Type: review\n"
            "- Task-File: .ai-dev/tasks/other.md\n"
        )

        with self.assertRaisesRegex(ReviewManifestError, "Task-File"):
            resolve_current_review_id(self.repo_root)

    def test_resolve_current_review_id_rejects_bad_task_id(self) -> None:
        self._write_current_task(
            "# Current AI Dev Task\n\n"
            "- Task-ID: review-bad-task\n"
            "- Task-Type: review\n"
            "- Task-File: .ai-dev/tasks/review-bad-task.md\n"
        )

        with self.assertRaisesRegex(ReviewManifestError, "Invalid review identifier"):
            resolve_current_review_id(self.repo_root)


if __name__ == "__main__":
    unittest.main()
