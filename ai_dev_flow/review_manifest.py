from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re

from .review_paths import ReviewPathError, build_review_artifact_paths
from .review_task_generation import build_review_task_id


REVIEW_ID_PATTERN = re.compile(r"^review-[0-9a-f]{16}$")


class ReviewManifestError(Exception):
    """Raised for review manifest and pointer resolution errors."""


def validate_review_id(review_id: str) -> str:
    normalized = review_id.strip()
    if REVIEW_ID_PATTERN.fullmatch(normalized) is None:
        raise ReviewManifestError(
            "Invalid review identifier. Expected format: review-<16 lowercase hex chars>."
        )
    return normalized


def validate_repo_relative_path(path_text: str, *, label: str) -> str:
    normalized = path_text.strip()
    candidate = PurePosixPath(normalized)
    if not normalized:
        raise ReviewManifestError(f"{label} cannot be empty.")
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewManifestError(
            f"{label} must be repository-relative without traversal: {path_text!r}"
        )
    if candidate.as_posix() in {"", "."}:
        raise ReviewManifestError(f"{label} cannot be empty.")
    return candidate.as_posix()


def expected_review_task_id(review_id: str) -> str:
    return build_review_task_id(validate_review_id(review_id))


def expected_review_task_file(review_id: str) -> str:
    _ = expected_review_task_id(review_id)
    return ".ai-dev/review/task.md"


def review_verification_markdown_path(review_id: str) -> str:
    _ = validate_review_id(review_id)
    return ".ai-dev/review/verification.md"


def review_verification_json_path(review_id: str) -> str:
    _ = validate_review_id(review_id)
    return ".ai-dev/review/verification.json"


def expected_review_artifact_paths(repo_root: Path, review_id: str):
    normalized = validate_review_id(review_id)
    try:
        return build_review_artifact_paths(repo_root, normalized)
    except ReviewPathError as exc:
        raise ReviewManifestError(str(exc)) from exc


def resolve_current_review_id(repo_root: Path) -> str:
    package_path = repo_root / ".ai-dev" / "review" / "package.json"
    if not package_path.exists():
        raise ReviewManifestError("No current review found at .ai-dev/review/. Run `flow review` first.")
    if not package_path.is_file():
        raise ReviewManifestError(
            "No current review found at .ai-dev/review/: package.json is not a regular file."
        )

    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewManifestError(
            f"Cannot resolve current review from .ai-dev/review/package.json: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ReviewManifestError(
            "Cannot resolve current review from .ai-dev/review/package.json: expected JSON object."
        )

    review_id = payload.get("review_id")
    if not isinstance(review_id, str):
        raise ReviewManifestError(
            "Cannot resolve current review from .ai-dev/review/package.json: missing review_id."
        )

    return validate_review_id(review_id)
