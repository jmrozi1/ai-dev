from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from pathlib import PurePosixPath
import re

from .summarize_glob import normalize_path_text
from .summarize_task_generation import summarize_manifest_path


_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class SummarizeManifestError(Exception):
    """Raised for summarize manifest parsing and validation failures."""


@dataclass(frozen=True)
class SummarizeManifestEntry:
    source_path: str
    output_path: str
    source_digest_sha256: str
    source_size_bytes: int
    batch_index: int
    matched_rule_indexes: tuple[int, ...]


@dataclass(frozen=True)
class SummarizeManifestBatch:
    batch_index: int
    batch_count: int
    batch_id: str
    task_id: str
    task_file: str
    source_count: int
    entries: tuple[SummarizeManifestEntry, ...]


@dataclass(frozen=True)
class SummarizeManifest:
    schema_version: int
    plan_id: str
    requested_glob: str
    coordinator_task: str
    batch_tasks: tuple[str, ...]
    batches: tuple[SummarizeManifestBatch, ...]
    entries: tuple[SummarizeManifestEntry, ...]
    manifest_relative_path: str
    manifest_absolute_path: Path


def _type_name(value: object) -> str:
    return type(value).__name__


def _validate_repo_relative_path(value: object, *, field_path: str) -> str:
    if not isinstance(value, str):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected string, got {_type_name(value)}."
        )

    normalized = normalize_path_text(value)
    if not normalized:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: path cannot be empty."
        )

    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: path must be repository-relative without traversal: {value!r}."
        )

    return value


def _require_output_under_summary_root(path_text: str, *, field_path: str) -> None:
    normalized = normalize_path_text(path_text)
    if normalized == ".ai-dev/summaries":
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: output path must point to a file under .ai-dev/summaries/."
        )

    if not normalized.startswith(".ai-dev/summaries/"):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: output path must remain under .ai-dev/summaries/: {path_text!r}."
        )


def _validate_non_empty_string(value: object, *, field_path: str) -> str:
    if not isinstance(value, str):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected string, got {_type_name(value)}."
        )

    if not value.strip():
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected non-empty string."
        )

    return value


def _validate_non_negative_int(value: object, *, field_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected integer, got {_type_name(value)}."
        )

    if value < 0:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected integer >= 0."
        )

    return value


def _validate_positive_int(value: object, *, field_path: str) -> int:
    parsed = _validate_non_negative_int(value, field_path=field_path)
    if parsed <= 0:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected integer > 0."
        )
    return parsed


def _parse_entry(entry_data: object, *, field_path: str, parent_batch_index: int) -> SummarizeManifestEntry:
    if not isinstance(entry_data, dict):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected object, got {_type_name(entry_data)}."
        )

    source_path = _validate_repo_relative_path(
        entry_data.get("source_path"),
        field_path=f"{field_path}.source_path",
    )
    output_path = _validate_repo_relative_path(
        entry_data.get("output_path"),
        field_path=f"{field_path}.output_path",
    )
    _require_output_under_summary_root(output_path, field_path=f"{field_path}.output_path")

    digest = _validate_non_empty_string(
        entry_data.get("source_digest_sha256"),
        field_path=f"{field_path}.source_digest_sha256",
    )
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.source_digest_sha256: expected 64 hex characters."
        )

    source_size_bytes = _validate_non_negative_int(
        entry_data.get("source_size_bytes"),
        field_path=f"{field_path}.source_size_bytes",
    )

    batch_index = _validate_positive_int(
        entry_data.get("batch_index"),
        field_path=f"{field_path}.batch_index",
    )
    if batch_index != parent_batch_index:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.batch_index: {batch_index} does not match parent batch index {parent_batch_index}."
        )

    raw_indexes = entry_data.get("matched_rule_indexes")
    if not isinstance(raw_indexes, list):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.matched_rule_indexes: expected list, got {_type_name(raw_indexes)}."
        )

    matched_rule_indexes: list[int] = []
    for index, value in enumerate(raw_indexes):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SummarizeManifestError(
                f"Invalid summarize manifest at {field_path}.matched_rule_indexes[{index}]: expected integer >= 0."
            )
        matched_rule_indexes.append(value)

    return SummarizeManifestEntry(
        source_path=source_path,
        output_path=output_path,
        source_digest_sha256=digest.lower(),
        source_size_bytes=source_size_bytes,
        batch_index=batch_index,
        matched_rule_indexes=tuple(matched_rule_indexes),
    )


def _parse_batch(
    batch_data: object,
    *,
    field_path: str,
    expected_batch_index: int,
    expected_batch_count: int,
    expected_task_file: str,
) -> SummarizeManifestBatch:
    if not isinstance(batch_data, dict):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}: expected object, got {_type_name(batch_data)}."
        )

    batch_index = _validate_positive_int(batch_data.get("batch_index"), field_path=f"{field_path}.batch_index")
    if batch_index != expected_batch_index:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.batch_index: expected {expected_batch_index}, got {batch_index}."
        )

    batch_count = _validate_positive_int(batch_data.get("batch_count"), field_path=f"{field_path}.batch_count")
    if batch_count != expected_batch_count:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.batch_count: expected {expected_batch_count}, got {batch_count}."
        )

    batch_id = _validate_non_empty_string(batch_data.get("batch_id"), field_path=f"{field_path}.batch_id")
    task_id = _validate_non_empty_string(batch_data.get("task_id"), field_path=f"{field_path}.task_id")

    task_file = _validate_repo_relative_path(batch_data.get("task_file"), field_path=f"{field_path}.task_file")
    if task_file != expected_task_file:
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.task_file: expected {expected_task_file!r}, got {task_file!r}."
        )

    source_count = _validate_non_negative_int(
        batch_data.get("source_count"),
        field_path=f"{field_path}.source_count",
    )

    raw_entries = batch_data.get("entries")
    if not isinstance(raw_entries, list):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.entries: expected list, got {_type_name(raw_entries)}."
        )

    entries = tuple(
        _parse_entry(entry, field_path=f"{field_path}.entries[{index}]", parent_batch_index=batch_index)
        for index, entry in enumerate(raw_entries)
    )
    if source_count != len(entries):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at {field_path}.source_count: expected {len(entries)} from entries, got {source_count}."
        )

    return SummarizeManifestBatch(
        batch_index=batch_index,
        batch_count=batch_count,
        batch_id=batch_id,
        task_id=task_id,
        task_file=task_file,
        source_count=source_count,
        entries=entries,
    )


def parse_summarize_manifest(
    *,
    manifest_data: object,
    expected_plan_id: str,
    manifest_relative_path: str,
    manifest_absolute_path: Path,
) -> SummarizeManifest:
    if not isinstance(manifest_data, dict):
        raise SummarizeManifestError(
            f"Invalid summarize manifest in {manifest_absolute_path}: expected JSON object at root."
        )

    schema_version = _validate_positive_int(
        manifest_data.get("schema_version", 1),
        field_path="schema_version",
    )
    plan_id = _validate_non_empty_string(manifest_data.get("plan_id"), field_path="plan_id")
    if plan_id != expected_plan_id:
        raise SummarizeManifestError(
            f"Invalid summarize manifest in {manifest_absolute_path}: plan_id {plan_id!r} does not match requested plan {expected_plan_id!r}."
        )

    requested_glob = _validate_non_empty_string(
        manifest_data.get("requested_glob"),
        field_path="requested_glob",
    )
    coordinator_task = _validate_repo_relative_path(
        manifest_data.get("coordinator_task"),
        field_path="coordinator_task",
    )

    batch_tasks_data = manifest_data.get("batch_tasks")
    if not isinstance(batch_tasks_data, list):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at batch_tasks: expected list, got {_type_name(batch_tasks_data)}."
        )
    if not batch_tasks_data:
        raise SummarizeManifestError("Invalid summarize manifest at batch_tasks: expected at least one batch task.")

    batch_tasks = tuple(
        _validate_repo_relative_path(item, field_path=f"batch_tasks[{index}]")
        for index, item in enumerate(batch_tasks_data)
    )

    batches_data = manifest_data.get("batches")
    if not isinstance(batches_data, list):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at batches: expected list, got {_type_name(batches_data)}."
        )

    if len(batches_data) != len(batch_tasks):
        raise SummarizeManifestError(
            f"Invalid summarize manifest at batches: expected {len(batch_tasks)} batch object(s), got {len(batches_data)}."
        )

    batches = tuple(
        _parse_batch(
            batch,
            field_path=f"batches[{index}]",
            expected_batch_index=index + 1,
            expected_batch_count=len(batch_tasks),
            expected_task_file=batch_tasks[index],
        )
        for index, batch in enumerate(batches_data)
    )

    flattened_entries = tuple(entry for batch in batches for entry in batch.entries)

    source_paths = [entry.source_path for entry in flattened_entries]
    output_paths = [entry.output_path for entry in flattened_entries]

    if len(set(source_paths)) != len(source_paths):
        raise SummarizeManifestError("Invalid summarize manifest: duplicate source_path detected.")
    if len(set(output_paths)) != len(output_paths):
        raise SummarizeManifestError("Invalid summarize manifest: duplicate output_path detected.")

    return SummarizeManifest(
        schema_version=schema_version,
        plan_id=plan_id,
        requested_glob=requested_glob,
        coordinator_task=coordinator_task,
        batch_tasks=batch_tasks,
        batches=batches,
        entries=flattened_entries,
        manifest_relative_path=manifest_relative_path,
        manifest_absolute_path=manifest_absolute_path,
    )


def load_summarize_manifest(repo_root: Path, plan_id: str) -> SummarizeManifest:
    manifest_relative_path = summarize_manifest_path(plan_id)
    manifest_absolute_path = repo_root / manifest_relative_path

    if not manifest_absolute_path.exists():
        raise SummarizeManifestError(
            f"Summarize manifest does not exist for plan {plan_id}: {manifest_relative_path}"
        )

    try:
        raw_text = manifest_absolute_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SummarizeManifestError(
            f"Cannot read summarize manifest {manifest_relative_path}: {exc}"
        ) from exc

    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SummarizeManifestError(
            f"Invalid JSON in summarize manifest {manifest_relative_path}: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    return parse_summarize_manifest(
        manifest_data=decoded,
        expected_plan_id=plan_id,
        manifest_relative_path=manifest_relative_path,
        manifest_absolute_path=manifest_absolute_path,
    )
