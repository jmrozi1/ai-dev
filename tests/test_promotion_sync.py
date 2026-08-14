from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_dev_flow.promotion_sync import (
    PromotionSyncError,
    PromotionSyncRecord,
    clear_promotion_sync_record,
    load_promotion_sync_record,
    promotion_sync_record_matches_state,
    promotion_sync_record_path,
    save_promotion_sync_record,
)
from ai_dev_flow.workflow_state import WorkflowState


class PromotionSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _record(self) -> PromotionSyncRecord:
        return PromotionSyncRecord(
            status="pending",
            main_branch="main",
            scratch_branch="scratch",
            promoted_main_commit="a" * 40,
            remote_name="shared",
            upstream_ref="refs/heads/main",
            active_issue_number=36,
        )

    def test_round_trip_matches_active_issue_workflow(self) -> None:
        record = self._record()

        save_promotion_sync_record(self.repo, record)
        loaded = load_promotion_sync_record(self.repo)

        self.assertEqual(loaded, record)
        assert loaded is not None
        self.assertTrue(
            promotion_sync_record_matches_state(
                loaded,
                WorkflowState(active_issue_number=36),
                promoted_main_commit="a" * 40,
            )
        )
        self.assertFalse(
            promotion_sync_record_matches_state(
                loaded,
                WorkflowState(active_issue_number=37),
                promoted_main_commit="a" * 40,
            )
        )

    def test_clear_removes_record_and_missing_record_loads_as_none(self) -> None:
        save_promotion_sync_record(self.repo, self._record())

        clear_promotion_sync_record(self.repo)

        self.assertIsNone(load_promotion_sync_record(self.repo))
        self.assertFalse(promotion_sync_record_path(self.repo).exists())

    def test_invalid_record_is_rejected(self) -> None:
        path = promotion_sync_record_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "pending",
                    "mainBranch": "main",
                    "scratchBranch": "scratch",
                    "promotedMainCommit": "a" * 40,
                    "remote": "shared",
                    "upstreamRef": "refs/heads/main",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(PromotionSyncError, "exactly one"):
            load_promotion_sync_record(self.repo)


if __name__ == "__main__":
    unittest.main()