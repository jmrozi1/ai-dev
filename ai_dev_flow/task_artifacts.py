from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from .json_files import JsonFileError, write_text_atomic


class TaskArtifactError(Exception):
    """Raised for generated-task artifact errors."""


TASK_ID_MAX_LENGTH = 128
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_ID_FORMAT_DESCRIPTION = (
    "must start with a letter or digit, contain only letters, digits, '.', '-', '_', "
    "must not be '.' or '..', and be at most 128 characters"
)


@dataclass(frozen=True)
class GeneratedTask:
    task_id: str
    task_type: str
    created_at_utc: str
    requested_command: str
    absolute_path: Path
    repository_relative_path: str


@dataclass(frozen=True)
class PlannedGeneratedTask:
    task_id: str
    task_type: str
    requested_command: str
    absolute_path: Path
    repository_relative_path: str


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_non_empty(label: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise TaskArtifactError(f"{label} cannot be empty.")
    return normalized


def _normalize_task_id(value: str) -> str:
    normalized = _normalize_non_empty("task id", value)
    if normalized in {".", ".."}:
        raise TaskArtifactError(
            f"Invalid task id {normalized!r}: {TASK_ID_FORMAT_DESCRIPTION}."
        )

    if len(normalized) > TASK_ID_MAX_LENGTH or TASK_ID_PATTERN.fullmatch(normalized) is None:
        raise TaskArtifactError(
            f"Invalid task id {normalized!r}: {TASK_ID_FORMAT_DESCRIPTION}."
        )

    return normalized


def _remove_generated_task_file(task_path: Path) -> None:
    try:
        task_path.unlink()
    except FileNotFoundError:
        pass


def plan_generated_task(
    *,
    repo_root: Path,
    task_id: str,
    task_type: str,
    requested_command: str,
) -> PlannedGeneratedTask:
    normalized_task_id = _normalize_task_id(task_id)
    normalized_task_type = _normalize_non_empty("task type", task_type)
    normalized_requested_command = _normalize_non_empty("requested command", requested_command)

    task_relative_path = f".ai-dev/tasks/{normalized_task_id}.md"
    task_path = repo_root / ".ai-dev" / "tasks" / f"{normalized_task_id}.md"

    return PlannedGeneratedTask(
        task_id=normalized_task_id,
        task_type=normalized_task_type,
        requested_command=normalized_requested_command,
        absolute_path=task_path,
        repository_relative_path=task_relative_path,
    )


def _task_markdown(
    *,
    task_id: str,
    task_type: str,
    created_at_utc: str,
    repository_relative_path: str,
    requested_command: str,
    task_body: str,
    constraints: str,
    expected_output: str,
) -> str:
    lines: list[str] = [
        f"# AI Dev Generated Task: {task_id}",
        "",
        "## Metadata",
        "",
        f"- Task-ID: {task_id}",
        f"- Task-Type: {task_type}",
        f"- Created-UTC: {created_at_utc}",
        f"- Task-File: {repository_relative_path}",
        f"- Requested-Command: {requested_command}",
        "",
        "## Task",
        "",
        task_body.rstrip(),
        "",
        "## Constraints",
        "",
        constraints.rstrip(),
        "",
        "## Expected Output",
        "",
        expected_output.rstrip(),
        "",
    ]

    return "\n".join(lines)


def create_generated_task(
    *,
    repo_root: Path,
    task_id: str,
    task_type: str,
    requested_command: str,
    task_body: str,
    constraints: str,
    expected_output: str,
) -> GeneratedTask:
    planned_task = plan_generated_task(
        repo_root=repo_root,
        task_id=task_id,
        task_type=task_type,
        requested_command=requested_command,
    )
    task_path = planned_task.absolute_path
    task_relative_path = planned_task.repository_relative_path
    normalized_task_id = planned_task.task_id
    normalized_task_type = planned_task.task_type
    normalized_requested_command = planned_task.requested_command

    if task_path.exists():
        raise TaskArtifactError(
            f"Cannot overwrite immutable task file: {task_relative_path}"
        )

    created_at_utc = _timestamp_utc()
    markdown = _task_markdown(
        task_id=normalized_task_id,
        task_type=normalized_task_type,
        created_at_utc=created_at_utc,
        repository_relative_path=task_relative_path,
        requested_command=normalized_requested_command,
        task_body=task_body or "(no task body provided)",
        constraints=constraints or "(none)",
        expected_output=expected_output or "(none)",
    )

    try:
        write_text_atomic(task_path, markdown)
    except JsonFileError as exc:
        raise TaskArtifactError(str(exc)) from exc

    pointer_path = repo_root / ".ai-dev" / "current-task.md"
    pointer_text = (
        "# Current AI Dev Task\n\n"
        f"- Task-ID: {normalized_task_id}\n"
        f"- Task-Type: {normalized_task_type}\n"
        f"- Task-File: {task_relative_path}\n"
    )

    try:
        write_text_atomic(pointer_path, pointer_text)
    except JsonFileError as exc:
        try:
            _remove_generated_task_file(task_path)
        except OSError as cleanup_exc:
            raise TaskArtifactError(
                f"{exc} Cleanup failure while removing newly created task file "
                f"{task_relative_path}: {cleanup_exc}"
            ) from exc
        raise TaskArtifactError(
            f"{exc} Rolled back newly created task file: {task_relative_path}"
        ) from exc

    return GeneratedTask(
        task_id=normalized_task_id,
        task_type=normalized_task_type,
        created_at_utc=created_at_utc,
        requested_command=normalized_requested_command,
        absolute_path=task_path,
        repository_relative_path=task_relative_path,
    )
