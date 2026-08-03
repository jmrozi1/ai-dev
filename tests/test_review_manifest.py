from __future__ import annotations

import json
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

    def _write_current_review_package(self, payload: dict[str, object]) -> None:
        review_dir = self.repo_root / ".ai-dev" / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "package.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

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
        self._write_current_review_package(
            {
                "schema_version": 1,
                "review_id": "review-0123456789abcdef",
            }
        )

        self.assertEqual(resolve_current_review_id(self.repo_root), "review-0123456789abcdef")

    def test_resolve_current_review_id_rejects_missing_rolling_review(self) -> None:
        with self.assertRaisesRegex(ReviewManifestError, "No current review"):
            resolve_current_review_id(self.repo_root)

    def test_resolve_current_review_id_rejects_missing_review_id(self) -> None:
        self._write_current_review_package(
            {
                "schema_version": 1,
            }
        )

        with self.assertRaisesRegex(ReviewManifestError, "missing review_id"):
            resolve_current_review_id(self.repo_root)

    def test_resolve_current_review_id_rejects_invalid_review_id(self) -> None:
        self._write_current_review_package(
            {
                "schema_version": 1,
                "review_id": "bad-id",
            }
        )

        with self.assertRaisesRegex(ReviewManifestError, "Invalid review identifier"):
            resolve_current_review_id(self.repo_root)


if __name__ == "__main__":
    unittest.main()
