from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

from .review_paths import ReviewPathError, build_review_artifact_paths
from .review_task_generation import build_review_task_id


REVIEW_ID_PATTERN = re.compile(r"^review-[0-9a-f]{16}$")
_CURRENT_TASK_FIELD_PATTERN = re.compile(r"^- (Task-ID|Task-Type|Task-File):\s*(.+?)\s*$")


class ReviewManifestError(Exception):
    """Raised for review manifest and pointer resolution errors."""


@dataclass(frozen=True)
class CurrentTaskPointer:
    task_id: str
    task_type: str
    task_file: str


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
    task_id = expected_review_task_id(review_id)
    return f".ai-dev/tasks/{task_id}.md"


def review_verification_markdown_path(review_id: str) -> str:
    normalized = validate_review_id(review_id)
    return f".ai-dev/reviews/{normalized}/verification.md"


def review_verification_json_path(review_id: str) -> str:
    normalized = validate_review_id(review_id)
    return f".ai-dev/reviews/{normalized}/verification.json"


def expected_review_artifact_paths(repo_root: Path, review_id: str):
    normalized = validate_review_id(review_id)
    try:
        return build_review_artifact_paths(repo_root, normalized)
    except ReviewPathError as exc:
        raise ReviewManifestError(str(exc)) from exc


def _load_current_task_pointer(repo_root: Path) -> CurrentTaskPointer:
    pointer_path = repo_root / ".ai-dev" / "current-task.md"
    if not pointer_path.exists():
        raise ReviewManifestError(
            "Cannot resolve current review: .ai-dev/current-task.md does not exist."
        )

    try:
        text = pointer_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewManifestError(
            f"Cannot resolve current review: failed to read .ai-dev/current-task.md: {exc}"
        ) from exc

    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _CURRENT_TASK_FIELD_PATTERN.fullmatch(line)
        if match is None:
            continue
        values[match.group(1)] = match.group(2)

    missing = [field for field in ("Task-ID", "Task-Type", "Task-File") if field not in values]
    if missing:
        raise ReviewManifestError(
            "Cannot resolve current review: .ai-dev/current-task.md is missing field(s): "
            + ", ".join(missing)
            + "."
        )

    return CurrentTaskPointer(
        task_id=values["Task-ID"],
        task_type=values["Task-Type"],
        task_file=values["Task-File"],
    )


def resolve_current_review_id(repo_root: Path) -> str:
    pointer = _load_current_task_pointer(repo_root)

    if pointer.task_type != "review":
        raise ReviewManifestError(
            "Current task is not review. Run `flow review` first. "
            f"Task-Type is {pointer.task_type!r}."
        )

    task_id = pointer.task_id.strip()
    if not task_id.endswith("-task"):
        raise ReviewManifestError(
            "Current review task pointer is malformed: Task-ID must end with '-task'."
        )

    review_id_candidate = task_id[: -len("-task")]
    review_id = validate_review_id(review_id_candidate)
    expected_task_id = expected_review_task_id(review_id)
    if task_id != expected_task_id:
        raise ReviewManifestError(
            "Current review task pointer is inconsistent: "
            f"expected Task-ID {expected_task_id!r}, got {task_id!r}."
        )

    task_file = validate_repo_relative_path(pointer.task_file, label="Task-File")
    expected_file = expected_review_task_file(review_id)
    if task_file != expected_file:
        raise ReviewManifestError(
            "Current review task pointer is inconsistent: "
            f"expected Task-File {expected_file!r}, got {task_file!r}."
        )

    return review_id
