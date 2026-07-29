from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .json_files import JsonFileError, write_text_atomic
from .summarize_glob import normalize_path_text
from .summarize_manifest import SummarizeManifest, SummarizeManifestEntry, SummarizeManifestError, load_summarize_manifest


class SummarizeVerificationError(Exception):
    """Raised for summarize verification failures."""


OUTPUT_STATUS_VALID = "valid"
OUTPUT_STATUS_MISSING = "missing"
OUTPUT_STATUS_UNREADABLE = "unreadable"
OUTPUT_STATUS_EMPTY = "empty"
OUTPUT_STATUS_MALFORMED_HEADER = "malformed-header"
OUTPUT_STATUS_WRONG_SOURCE_MARKER = "wrong-source-marker"
OUTPUT_STATUS_MISSING_GENERATOR_MARKER = "missing-generator-marker"
OUTPUT_STATUS_WRONG_PLAN_MARKER = "wrong-plan-marker"
OUTPUT_STATUS_STALE = "stale"

SOURCE_STATUS_UNCHANGED = "unchanged"
SOURCE_STATUS_CHANGED = "changed"
SOURCE_STATUS_MISSING = "missing"
SOURCE_STATUS_NOT_REGULAR = "not-regular-file"
SOURCE_STATUS_UNREADABLE = "unreadable"

BATCH_STATUS_COMPLETE = "complete"
BATCH_STATUS_PARTIAL = "partial"
BATCH_STATUS_FAILED = "failed"
BATCH_STATUS_UNTOUCHED = "untouched"

OVERALL_STATUS_COMPLETE = "complete"
OVERALL_STATUS_PARTIAL = "partial"
OVERALL_STATUS_FAILED = "failed"
OVERALL_STATUS_STALE = "stale"

_CURRENT_TASK_FIELD_PATTERN = re.compile(r"^- (Task-ID|Task-Type|Task-File):\s*(.+?)\s*$")


@dataclass(frozen=True)
class SourceVerificationState:
    source_path: str
    expected_digest_sha256: str
    current_digest_sha256: str | None
    expected_size_bytes: int
    current_size_bytes: int | None
    status: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class OutputVerificationState:
    source_path: str
    output_path: str
    status: str
    reason_codes: tuple[str, ...]
    message: str
    exists: bool


@dataclass(frozen=True)
class BatchVerificationState:
    batch_index: int
    task_id: str
    task_file: str
    expected_count: int
    valid_count: int
    invalid_count: int
    status: str


@dataclass(frozen=True)
class UnexpectedOutputState:
    output_path: str
    detected_plan_id: str | None


@dataclass(frozen=True)
class SummarizeVerificationResult:
    plan_id: str
    requested_glob: str
    overall_status: str
    source_states: tuple[SourceVerificationState, ...]
    output_states: tuple[OutputVerificationState, ...]
    batch_states: tuple[BatchVerificationState, ...]
    unexpected_outputs: tuple[UnexpectedOutputState, ...]
    expected_source_count: int
    valid_output_count: int
    missing_output_count: int
    malformed_output_count: int
    stale_source_count: int
    unexpected_output_count: int
    recommended_next_action: str


def summarize_verification_markdown_path(plan_id: str) -> str:
    return f".ai-dev/summarize/{plan_id}/verification.md"


def summarize_verification_json_path(plan_id: str) -> str:
    return f".ai-dev/summarize/{plan_id}/verification.json"


def _current_task_fields(repo_root: Path) -> dict[str, str]:
    pointer_path = repo_root / ".ai-dev" / "current-task.md"
    if not pointer_path.exists():
        raise SummarizeVerificationError(
            "Cannot resolve current summarize plan: .ai-dev/current-task.md does not exist."
        )

    try:
        text = pointer_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SummarizeVerificationError(
            f"Cannot resolve current summarize plan: failed to read .ai-dev/current-task.md: {exc}"
        ) from exc

    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _CURRENT_TASK_FIELD_PATTERN.fullmatch(line)
        if match is None:
            continue
        values[match.group(1)] = match.group(2)

    required = ("Task-ID", "Task-Type", "Task-File")
    missing = [item for item in required if item not in values]
    if missing:
        raise SummarizeVerificationError(
            "Cannot resolve current summarize plan: .ai-dev/current-task.md is missing field(s): "
            + ", ".join(missing)
            + "."
        )

    return values


def resolve_current_summarize_plan_id(repo_root: Path) -> str:
    fields = _current_task_fields(repo_root)
    task_type = fields["Task-Type"]
    task_id = fields["Task-ID"]

    if task_type != "summarize":
        raise SummarizeVerificationError(
            f"Current task is not summarize: Task-Type is {task_type!r}."
        )

    if not task_id.startswith("summarize-") or not task_id.endswith("-coordinator"):
        raise SummarizeVerificationError(
            f"Current summarize task is not a coordinator task: Task-ID is {task_id!r}."
        )

    plan_id = task_id[len("summarize-") : -len("-coordinator")]
    if not plan_id:
        raise SummarizeVerificationError(
            f"Cannot resolve summarize plan ID from Task-ID: {task_id!r}."
        )

    expected_task_file = f".ai-dev/tasks/{task_id}.md"
    if fields["Task-File"] != expected_task_file:
        raise SummarizeVerificationError(
            "Current summarize coordinator pointer is inconsistent: "
            f"expected Task-File {expected_task_file!r}, got {fields['Task-File']!r}."
        )

    return plan_id


def _source_current_state(repo_root: Path, entry: SummarizeManifestEntry) -> SourceVerificationState:
    source_absolute_path = repo_root / entry.source_path
    if not source_absolute_path.exists():
        return SourceVerificationState(
            source_path=entry.source_path,
            expected_digest_sha256=entry.source_digest_sha256,
            current_digest_sha256=None,
            expected_size_bytes=entry.source_size_bytes,
            current_size_bytes=None,
            status=SOURCE_STATUS_MISSING,
            reason_codes=("source-missing",),
        )

    if not source_absolute_path.is_file():
        return SourceVerificationState(
            source_path=entry.source_path,
            expected_digest_sha256=entry.source_digest_sha256,
            current_digest_sha256=None,
            expected_size_bytes=entry.source_size_bytes,
            current_size_bytes=None,
            status=SOURCE_STATUS_NOT_REGULAR,
            reason_codes=("source-not-regular-file",),
        )

    try:
        source_bytes = source_absolute_path.read_bytes()
    except OSError:
        return SourceVerificationState(
            source_path=entry.source_path,
            expected_digest_sha256=entry.source_digest_sha256,
            current_digest_sha256=None,
            expected_size_bytes=entry.source_size_bytes,
            current_size_bytes=None,
            status=SOURCE_STATUS_UNREADABLE,
            reason_codes=("source-unreadable",),
        )

    current_digest = hashlib.sha256(source_bytes).hexdigest()
    current_size = len(source_bytes)
    if current_digest != entry.source_digest_sha256:
        return SourceVerificationState(
            source_path=entry.source_path,
            expected_digest_sha256=entry.source_digest_sha256,
            current_digest_sha256=current_digest,
            expected_size_bytes=entry.source_size_bytes,
            current_size_bytes=current_size,
            status=SOURCE_STATUS_CHANGED,
            reason_codes=("source-digest-mismatch",),
        )

    if current_size != entry.source_size_bytes:
        return SourceVerificationState(
            source_path=entry.source_path,
            expected_digest_sha256=entry.source_digest_sha256,
            current_digest_sha256=current_digest,
            expected_size_bytes=entry.source_size_bytes,
            current_size_bytes=current_size,
            status=SOURCE_STATUS_CHANGED,
            reason_codes=("source-size-mismatch",),
        )

    return SourceVerificationState(
        source_path=entry.source_path,
        expected_digest_sha256=entry.source_digest_sha256,
        current_digest_sha256=current_digest,
        expected_size_bytes=entry.source_size_bytes,
        current_size_bytes=current_size,
        status=SOURCE_STATUS_UNCHANGED,
        reason_codes=(),
    )


def _first_heading_line(lines: list[str]) -> str | None:
    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.startswith("#"):
            return trimmed
    return None


def _extract_marker_values(lines: list[str], prefix: str) -> list[str]:
    values: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            values.append(line[len(prefix) :])
    return values


def _validate_output(
    *,
    repo_root: Path,
    entry: SummarizeManifestEntry,
    plan_id: str,
    source_state: SourceVerificationState,
) -> OutputVerificationState:
    output_absolute_path = repo_root / entry.output_path
    if not output_absolute_path.exists():
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_MISSING,
            reason_codes=("output-missing",),
            message="Expected summary output file is missing.",
            exists=False,
        )

    if not output_absolute_path.is_file():
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_UNREADABLE,
            reason_codes=("output-not-regular-file",),
            message="Expected summary output path is not a regular file.",
            exists=False,
        )

    try:
        text = output_absolute_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_UNREADABLE,
            reason_codes=("output-invalid-utf8",),
            message="Summary output cannot be decoded as UTF-8.",
            exists=True,
        )
    except OSError as exc:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_UNREADABLE,
            reason_codes=("output-unreadable",),
            message=f"Summary output cannot be read: {exc}",
            exists=True,
        )

    if not text.strip():
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_EMPTY,
            reason_codes=("output-empty",),
            message="Summary output is empty after trimming.",
            exists=True,
        )

    lines = text.splitlines()

    first_heading = _first_heading_line(lines)
    if first_heading != "# Summary":
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_MALFORMED_HEADER,
            reason_codes=("missing-or-invalid-summary-heading",),
            message="First meaningful heading must be '# Summary'.",
            exists=True,
        )

    source_values = _extract_marker_values(lines, "Source: ")
    if not source_values:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_WRONG_SOURCE_MARKER,
            reason_codes=("missing-source-marker",),
            message="Missing required source marker.",
            exists=True,
        )

    if any(value != entry.source_path for value in source_values):
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_WRONG_SOURCE_MARKER,
            reason_codes=("wrong-source-marker",),
            message="Source marker does not match expected source path.",
            exists=True,
        )

    if len(set(source_values)) != 1:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_WRONG_SOURCE_MARKER,
            reason_codes=("inconsistent-source-marker",),
            message="Source markers are duplicated with inconsistent values.",
            exists=True,
        )

    expected_generator_marker = "Generated-By: ai-dev summarize"
    generator_values = [line for line in lines if line.startswith("Generated-By:")]
    if not generator_values:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_MISSING_GENERATOR_MARKER,
            reason_codes=("missing-generator-marker",),
            message="Missing required generator marker.",
            exists=True,
        )

    has_expected_generator = any(value == expected_generator_marker for value in generator_values)
    if not has_expected_generator:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_MISSING_GENERATOR_MARKER,
            reason_codes=("missing-generator-marker",),
            message="Missing required generator marker.",
            exists=True,
        )

    if any(value != expected_generator_marker for value in generator_values):
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_MISSING_GENERATOR_MARKER,
            reason_codes=("inconsistent-generator-marker",),
            message="Generated-By markers contain conflicting values.",
            exists=True,
        )

    plan_values = _extract_marker_values(lines, "Plan-ID: ")
    if not plan_values:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_WRONG_PLAN_MARKER,
            reason_codes=("missing-plan-marker",),
            message="Missing required plan marker.",
            exists=True,
        )

    if any(value != plan_id for value in plan_values) or len(set(plan_values)) != 1:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_WRONG_PLAN_MARKER,
            reason_codes=("wrong-plan-marker",),
            message="Plan marker does not match the verified plan.",
            exists=True,
        )

    if source_state.status != SOURCE_STATUS_UNCHANGED:
        return OutputVerificationState(
            source_path=entry.source_path,
            output_path=entry.output_path,
            status=OUTPUT_STATUS_STALE,
            reason_codes=("source-changed-after-preparation",),
            message="Summary output is stale because source changed after preparation.",
            exists=True,
        )

    return OutputVerificationState(
        source_path=entry.source_path,
        output_path=entry.output_path,
        status=OUTPUT_STATUS_VALID,
        reason_codes=(),
        message="Summary output passed structural verification.",
        exists=True,
    )


def _try_detect_plan_id(repo_root: Path, output_relative_path: str) -> str | None:
    output_path = repo_root / output_relative_path
    try:
        text = output_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None

    for line in text.splitlines():
        if line.startswith("Plan-ID: "):
            candidate = line[len("Plan-ID: ") :].strip()
            if candidate:
                return candidate

    return None


def _unexpected_outputs(
    *,
    repo_root: Path,
    expected_output_paths: set[str],
) -> tuple[UnexpectedOutputState, ...]:
    summary_root = repo_root / ".ai-dev" / "summaries"
    if not summary_root.exists():
        return ()

    discovered: list[UnexpectedOutputState] = []
    for candidate in sorted(summary_root.rglob("*")):
        if not candidate.is_file():
            continue

        try:
            relative = candidate.relative_to(repo_root).as_posix()
        except ValueError:
            continue

        normalized = normalize_path_text(relative)
        if normalized in expected_output_paths:
            continue

        detected_plan = _try_detect_plan_id(repo_root, normalized)
        discovered.append(
            UnexpectedOutputState(
                output_path=relative,
                detected_plan_id=detected_plan,
            )
        )

    return tuple(sorted(discovered, key=lambda item: item.output_path))


def _batch_state(
    *,
    manifest: SummarizeManifest,
    output_state_by_output_path: dict[str, OutputVerificationState],
    source_state_by_source_path: dict[str, SourceVerificationState],
) -> tuple[BatchVerificationState, ...]:
    results: list[BatchVerificationState] = []
    for batch in manifest.batches:
        output_states = [output_state_by_output_path[entry.output_path] for entry in batch.entries]
        source_states = [source_state_by_source_path[entry.source_path] for entry in batch.entries]

        expected_count = len(output_states)
        valid_count = sum(1 for state in output_states if state.status == OUTPUT_STATUS_VALID)
        invalid_count = expected_count - valid_count

        has_source_failures = any(state.status != SOURCE_STATUS_UNCHANGED for state in source_states)
        all_missing = all(state.status == OUTPUT_STATUS_MISSING for state in output_states)
        any_existing_invalid = any(
            state.exists and state.status != OUTPUT_STATUS_VALID for state in output_states
        )

        if valid_count == expected_count and not has_source_failures:
            status = BATCH_STATUS_COMPLETE
        elif all_missing and not any_existing_invalid and not has_source_failures:
            status = BATCH_STATUS_UNTOUCHED
        elif valid_count == 0 and invalid_count > 0:
            status = BATCH_STATUS_FAILED
        else:
            status = BATCH_STATUS_PARTIAL

        results.append(
            BatchVerificationState(
                batch_index=batch.batch_index,
                task_id=batch.task_id,
                task_file=batch.task_file,
                expected_count=expected_count,
                valid_count=valid_count,
                invalid_count=invalid_count,
                status=status,
            )
        )

    return tuple(results)


def _overall_status(
    *,
    source_states: tuple[SourceVerificationState, ...],
    output_states: tuple[OutputVerificationState, ...],
    batch_states: tuple[BatchVerificationState, ...],
    unexpected_output_count: int,
) -> str:
    if any(state.status != SOURCE_STATUS_UNCHANGED for state in source_states):
        return OVERALL_STATUS_STALE

    if any(state.status == OUTPUT_STATUS_STALE for state in output_states):
        return OVERALL_STATUS_STALE

    if any(state.status == BATCH_STATUS_FAILED for state in batch_states):
        return OVERALL_STATUS_FAILED

    if (
        any(state.status in {BATCH_STATUS_PARTIAL, BATCH_STATUS_UNTOUCHED} for state in batch_states)
        or unexpected_output_count > 0
    ):
        return OVERALL_STATUS_PARTIAL

    return OVERALL_STATUS_COMPLETE


def _recommended_next_action(
    *,
    stale_source_count: int,
    missing_output_count: int,
    malformed_output_count: int,
    unexpected_output_count: int,
) -> str:
    if stale_source_count > 0:
        return "Regenerate summarize tasks because one or more source files changed after preparation."
    if missing_output_count > 0:
        return "Execute missing summarize batch tasks to produce the missing outputs."
    if malformed_output_count > 0:
        return "Repair malformed summaries so required markers and structure are present."
    if unexpected_output_count > 0:
        return "Inspect or remove unexpected summary outputs under .ai-dev/summaries/."
    return "Verification is complete; no further summarize action is required."


def verify_summarize_plan(repo_root: Path, manifest: SummarizeManifest) -> SummarizeVerificationResult:
    source_states = tuple(_source_current_state(repo_root, entry) for entry in manifest.entries)
    source_state_by_source_path = {state.source_path: state for state in source_states}

    output_states = tuple(
        _validate_output(
            repo_root=repo_root,
            entry=entry,
            plan_id=manifest.plan_id,
            source_state=source_state_by_source_path[entry.source_path],
        )
        for entry in manifest.entries
    )
    output_state_by_output_path = {state.output_path: state for state in output_states}

    batch_states = _batch_state(
        manifest=manifest,
        output_state_by_output_path=output_state_by_output_path,
        source_state_by_source_path=source_state_by_source_path,
    )

    expected_output_paths = {entry.output_path for entry in manifest.entries}
    unexpected_outputs = _unexpected_outputs(repo_root=repo_root, expected_output_paths=expected_output_paths)

    valid_output_count = sum(1 for state in output_states if state.status == OUTPUT_STATUS_VALID)
    missing_output_count = sum(1 for state in output_states if state.status == OUTPUT_STATUS_MISSING)
    malformed_output_count = sum(
        1
        for state in output_states
        if state.status
        not in {
            OUTPUT_STATUS_VALID,
            OUTPUT_STATUS_MISSING,
            OUTPUT_STATUS_STALE,
        }
    )
    stale_source_count = sum(1 for state in source_states if state.status != SOURCE_STATUS_UNCHANGED)
    unexpected_output_count = len(unexpected_outputs)

    overall_status = _overall_status(
        source_states=source_states,
        output_states=output_states,
        batch_states=batch_states,
        unexpected_output_count=unexpected_output_count,
    )

    return SummarizeVerificationResult(
        plan_id=manifest.plan_id,
        requested_glob=manifest.requested_glob,
        overall_status=overall_status,
        source_states=source_states,
        output_states=output_states,
        batch_states=batch_states,
        unexpected_outputs=unexpected_outputs,
        expected_source_count=len(manifest.entries),
        valid_output_count=valid_output_count,
        missing_output_count=missing_output_count,
        malformed_output_count=malformed_output_count,
        stale_source_count=stale_source_count,
        unexpected_output_count=unexpected_output_count,
        recommended_next_action=_recommended_next_action(
            stale_source_count=stale_source_count,
            missing_output_count=missing_output_count,
            malformed_output_count=malformed_output_count,
            unexpected_output_count=unexpected_output_count,
        ),
    )


def verification_result_json(result: SummarizeVerificationResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_id": result.plan_id,
        "overall_status": result.overall_status,
        "counts": {
            "expected_source_count": result.expected_source_count,
            "valid_output_count": result.valid_output_count,
            "missing_output_count": result.missing_output_count,
            "malformed_output_count": result.malformed_output_count,
            "stale_source_count": result.stale_source_count,
            "unexpected_output_count": result.unexpected_output_count,
            "batch_counts": {
                "complete": sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_COMPLETE),
                "partial": sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_PARTIAL),
                "failed": sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_FAILED),
                "untouched": sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_UNTOUCHED),
            },
        },
        "sources": [
            {
                "source_path": item.source_path,
                "status": item.status,
                "expected_digest_sha256": item.expected_digest_sha256,
                "current_digest_sha256": item.current_digest_sha256,
                "expected_size_bytes": item.expected_size_bytes,
                "current_size_bytes": item.current_size_bytes,
                "reason_codes": list(item.reason_codes),
            }
            for item in result.source_states
        ],
        "outputs": [
            {
                "source_path": item.source_path,
                "output_path": item.output_path,
                "status": item.status,
                "reason_codes": list(item.reason_codes),
                "message": item.message,
            }
            for item in result.output_states
        ],
        "batches": [
            {
                "batch_index": item.batch_index,
                "task_id": item.task_id,
                "task_file": item.task_file,
                "expected_count": item.expected_count,
                "valid_count": item.valid_count,
                "invalid_count": item.invalid_count,
                "status": item.status,
            }
            for item in result.batch_states
        ],
        "unexpected_outputs": [
            {
                "output_path": item.output_path,
                "detected_plan_id": item.detected_plan_id,
            }
            for item in result.unexpected_outputs
        ],
        "recommended_next_action": result.recommended_next_action,
    }


def render_verification_markdown(result: SummarizeVerificationResult) -> str:
    batch_complete_count = sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_COMPLETE)
    batch_partial_count = sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_PARTIAL)
    batch_failed_count = sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_FAILED)
    batch_untouched_count = sum(1 for batch in result.batch_states if batch.status == BATCH_STATUS_UNTOUCHED)

    lines: list[str] = [
        f"# Summarize Verification Report: {result.plan_id}",
        "",
        "## Summary",
        "",
        f"- Plan-ID: {result.plan_id}",
        f"- Requested-Glob: {result.requested_glob}",
        f"- Overall-Status: {result.overall_status}",
        f"- Expected-Source-Count: {result.expected_source_count}",
        f"- Valid-Output-Count: {result.valid_output_count}",
        f"- Missing-Output-Count: {result.missing_output_count}",
        f"- Malformed-Output-Count: {result.malformed_output_count}",
        f"- Stale-Source-Count: {result.stale_source_count}",
        f"- Unexpected-Output-Count: {result.unexpected_output_count}",
        f"- Batch-Complete-Count: {batch_complete_count}",
        f"- Batch-Partial-Count: {batch_partial_count}",
        f"- Batch-Failed-Count: {batch_failed_count}",
        f"- Batch-Untouched-Count: {batch_untouched_count}",
        "",
        "## Source State",
        "",
    ]

    for item in result.source_states:
        lines.append(f"- Source-Path: {item.source_path}")
        lines.append(f"  - Status: {item.status}")
        lines.append(f"  - Expected-Digest-SHA256: {item.expected_digest_sha256}")
        lines.append(f"  - Current-Digest-SHA256: {item.current_digest_sha256 or '(unavailable)'}")
        lines.append(f"  - Expected-Size-Bytes: {item.expected_size_bytes}")
        lines.append(f"  - Current-Size-Bytes: {item.current_size_bytes if item.current_size_bytes is not None else '(unavailable)'}")
        lines.append(
            "  - Reason-Codes: "
            + (", ".join(item.reason_codes) if item.reason_codes else "(none)")
        )

    lines.extend(["", "## Expected Outputs", ""])

    for item in result.output_states:
        lines.append(f"- Source-Path: {item.source_path}")
        lines.append(f"  - Output-Path: {item.output_path}")
        lines.append(f"  - Status: {item.status}")
        lines.append(
            "  - Reason-Codes: "
            + (", ".join(item.reason_codes) if item.reason_codes else "(none)")
        )
        lines.append(f"  - Message: {item.message}")

    lines.extend(["", "## Batch Status", ""])

    for item in result.batch_states:
        lines.append(f"- Batch-Index: {item.batch_index}")
        lines.append(f"  - Task-ID: {item.task_id}")
        lines.append(f"  - Task-File: {item.task_file}")
        lines.append(f"  - Status: {item.status}")
        lines.append(f"  - Expected-Count: {item.expected_count}")
        lines.append(f"  - Valid-Count: {item.valid_count}")
        lines.append(f"  - Invalid-Count: {item.invalid_count}")

    lines.extend(["", "## Unexpected Outputs", ""])
    if not result.unexpected_outputs:
        lines.append("- (none)")
    else:
        for item in result.unexpected_outputs:
            if item.detected_plan_id:
                lines.append(
                    f"- {item.output_path} (Detected-Plan-ID: {item.detected_plan_id})"
                )
            else:
                lines.append(f"- {item.output_path}")

    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            result.recommended_next_action,
            "",
        ]
    )

    return "\n".join(lines)


def write_verification_artifacts(
    *,
    repo_root: Path,
    result: SummarizeVerificationResult,
) -> tuple[str, str]:
    markdown_relative_path = summarize_verification_markdown_path(result.plan_id)
    json_relative_path = summarize_verification_json_path(result.plan_id)

    markdown_absolute_path = repo_root / markdown_relative_path
    json_absolute_path = repo_root / json_relative_path

    markdown_text = render_verification_markdown(result)
    json_text = json.dumps(
        verification_result_json(result),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"

    try:
        write_text_atomic(markdown_absolute_path, markdown_text)
        write_text_atomic(json_absolute_path, json_text)
    except JsonFileError as exc:
        raise SummarizeVerificationError(str(exc)) from exc

    return markdown_relative_path, json_relative_path


def run_summarize_verification(
    *,
    repo_root: Path,
    plan_id: str,
) -> tuple[SummarizeVerificationResult, str, str]:
    try:
        manifest = load_summarize_manifest(repo_root, plan_id)
    except SummarizeManifestError as exc:
        raise SummarizeVerificationError(str(exc)) from exc

    result = verify_summarize_plan(repo_root, manifest)
    markdown_path, json_path = write_verification_artifacts(repo_root=repo_root, result=result)
    return result, markdown_path, json_path
