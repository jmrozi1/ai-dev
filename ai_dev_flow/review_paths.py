from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath


class ReviewPathError(Exception):
    """Raised for invalid deterministic review artifact paths."""


@dataclass(frozen=True)
class ReviewArtifactPaths:
    review_id: str
    review_root_relative_path: str
    review_root_absolute_path: Path
    task_markdown_relative_path: str
    task_markdown_absolute_path: Path
    package_markdown_relative_path: str
    package_markdown_absolute_path: Path
    package_json_relative_path: str
    package_json_absolute_path: Path
    changes_diff_relative_path: str
    changes_diff_absolute_path: Path
    canonical_report_relative_path: str
    canonical_report_absolute_path: Path
    verification_markdown_relative_path: str
    verification_markdown_absolute_path: Path
    verification_json_relative_path: str
    verification_json_absolute_path: Path


def _validate_review_id(review_id: str) -> str:
    normalized = review_id.strip()
    if not normalized:
        raise ReviewPathError("Invalid review identifier: review id cannot be empty.")

    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    lowered = normalized.lower()
    if any(character not in allowed for character in lowered):
        raise ReviewPathError(
            "Invalid review identifier: only lowercase letters, digits, and '-' are allowed."
        )

    if ".." in lowered:
        raise ReviewPathError("Invalid review identifier: traversal segments are not allowed.")

    return lowered


def _validate_repo_relative_path(path_text: str) -> str:
    candidate = PurePosixPath(path_text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewPathError(
            f"Invalid review artifact path {path_text!r}: path must be repository-relative without traversal."
        )

    normalized = candidate.as_posix()
    if not normalized or normalized == ".":
        raise ReviewPathError("Invalid review artifact path: path cannot be empty.")

    return normalized


def build_review_artifact_paths(repo_root: Path, review_id: str) -> ReviewArtifactPaths:
    normalized_review_id = _validate_review_id(review_id)
    review_root_relative_path = _validate_repo_relative_path(".ai-dev/review")

    task_markdown_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/task.md"
    )

    package_markdown_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/package.md"
    )
    package_json_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/package.json"
    )
    changes_diff_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/changes.diff"
    )
    canonical_report_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/report.md"
    )
    verification_markdown_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/verification.md"
    )
    verification_json_relative_path = _validate_repo_relative_path(
        f"{review_root_relative_path}/verification.json"
    )

    return ReviewArtifactPaths(
        review_id=normalized_review_id,
        review_root_relative_path=review_root_relative_path,
        review_root_absolute_path=repo_root / review_root_relative_path,
        task_markdown_relative_path=task_markdown_relative_path,
        task_markdown_absolute_path=repo_root / task_markdown_relative_path,
        package_markdown_relative_path=package_markdown_relative_path,
        package_markdown_absolute_path=repo_root / package_markdown_relative_path,
        package_json_relative_path=package_json_relative_path,
        package_json_absolute_path=repo_root / package_json_relative_path,
        changes_diff_relative_path=changes_diff_relative_path,
        changes_diff_absolute_path=repo_root / changes_diff_relative_path,
        canonical_report_relative_path=canonical_report_relative_path,
        canonical_report_absolute_path=repo_root / canonical_report_relative_path,
        verification_markdown_relative_path=verification_markdown_relative_path,
        verification_markdown_absolute_path=repo_root / verification_markdown_relative_path,
        verification_json_relative_path=verification_json_relative_path,
        verification_json_absolute_path=repo_root / verification_json_relative_path,
    )
