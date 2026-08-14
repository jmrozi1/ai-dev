from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .json_files import JsonFileError, load_json_object, write_json_object_atomic
from .workflow_state import WorkflowState


PROMOTION_SYNC_RECORD_PATH = ".ai-dev/promotion-sync.json"


class PromotionSyncError(Exception):
    """Raised when promotion synchronization state is invalid or unavailable."""


@dataclass(frozen=True)
class PromotionSyncRecord:
    status: str
    main_branch: str
    scratch_branch: str
    promoted_main_commit: str
    remote_name: str
    upstream_ref: str
    active_issue_number: int | None = None
    patch_description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "status": self.status,
            "mainBranch": self.main_branch,
            "scratchBranch": self.scratch_branch,
            "promotedMainCommit": self.promoted_main_commit,
            "remote": self.remote_name,
            "upstreamRef": self.upstream_ref,
        }
        if self.active_issue_number is not None:
            payload["activeIssueNumber"] = self.active_issue_number
        if self.patch_description is not None:
            payload["patchDescription"] = self.patch_description
        return payload


def promotion_sync_record_path(repo_root: Path) -> Path:
    return repo_root / PROMOTION_SYNC_RECORD_PATH


def _non_empty_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PromotionSyncError(f"{key} must be a non-empty string.")
    return value.strip()


def _record_from_payload(payload: dict[str, object]) -> PromotionSyncRecord:
    allowed_keys = {
        "version",
        "status",
        "mainBranch",
        "scratchBranch",
        "promotedMainCommit",
        "remote",
        "upstreamRef",
        "activeIssueNumber",
        "patchDescription",
    }
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise PromotionSyncError(f"unknown key(s): {', '.join(unknown_keys)}.")
    if payload.get("version") != 1:
        raise PromotionSyncError("version must be 1.")

    status = _non_empty_string(payload, "status")
    if status not in {"pending", "synchronized"}:
        raise PromotionSyncError("status must be pending or synchronized.")

    main_branch = _non_empty_string(payload, "mainBranch")
    scratch_branch = _non_empty_string(payload, "scratchBranch")
    if main_branch == scratch_branch:
        raise PromotionSyncError("mainBranch and scratchBranch must differ.")

    promoted_main_commit = _non_empty_string(payload, "promotedMainCommit")
    remote_name = _non_empty_string(payload, "remote")
    if remote_name == ".":
        raise PromotionSyncError("remote must name a configured remote.")

    upstream_ref = _non_empty_string(payload, "upstreamRef")
    if not upstream_ref.startswith("refs/heads/") or upstream_ref == "refs/heads/":
        raise PromotionSyncError("upstreamRef must name a remote branch ref.")

    active_issue_number = payload.get("activeIssueNumber")
    patch_description = payload.get("patchDescription")
    if (active_issue_number is None) == (patch_description is None):
        raise PromotionSyncError(
            "exactly one of activeIssueNumber or patchDescription is required."
        )
    if active_issue_number is not None:
        if not isinstance(active_issue_number, int) or isinstance(active_issue_number, bool) or active_issue_number <= 0:
            raise PromotionSyncError("activeIssueNumber must be a positive integer.")
    if patch_description is not None:
        if not isinstance(patch_description, str) or not patch_description.strip():
            raise PromotionSyncError("patchDescription must be a non-empty string.")
        patch_description = patch_description.strip()

    return PromotionSyncRecord(
        status=status,
        main_branch=main_branch,
        scratch_branch=scratch_branch,
        promoted_main_commit=promoted_main_commit,
        remote_name=remote_name,
        upstream_ref=upstream_ref,
        active_issue_number=active_issue_number,
        patch_description=patch_description,
    )


def load_promotion_sync_record(repo_root: Path) -> PromotionSyncRecord | None:
    path = promotion_sync_record_path(repo_root)
    try:
        payload = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise PromotionSyncError(str(exc)) from exc
    if not payload:
        return None
    return _record_from_payload(payload)


def save_promotion_sync_record(repo_root: Path, record: PromotionSyncRecord) -> None:
    try:
        write_json_object_atomic(promotion_sync_record_path(repo_root), record.to_dict())
    except JsonFileError as exc:
        raise PromotionSyncError(str(exc)) from exc


def clear_promotion_sync_record(repo_root: Path) -> None:
    try:
        promotion_sync_record_path(repo_root).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PromotionSyncError(f"Cannot clear promotion synchronization state: {exc}") from exc


def promotion_sync_record_matches_state(
    record: PromotionSyncRecord,
    state: WorkflowState,
    *,
    promoted_main_commit: str,
) -> bool:
    if record.main_branch != state.main_branch or record.scratch_branch != state.scratch_branch:
        return False
    if record.promoted_main_commit != promoted_main_commit:
        return False
    if state.active_issue_number is not None:
        return record.active_issue_number == state.active_issue_number and record.patch_description is None
    if state.patch_description is not None:
        return record.patch_description == state.patch_description and record.active_issue_number is None
    return False