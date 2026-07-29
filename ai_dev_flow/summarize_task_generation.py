from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json

from .json_files import JsonFileError, write_json_object_atomic, write_text_atomic
from .summarize_batching import SummarizeBatch
from .summarize_planning import SummarizePlan, SummarizePlanEntry
from .task_artifacts import PlannedGeneratedTask, TaskArtifactError, plan_generated_task


class SummarizeTaskGenerationError(Exception):
    """Raised when summarize task generation fails."""


@dataclass(frozen=True)
class SummarizePreparedTasks:
    plan_id: str
    requested_glob: str
    coordinator_task_id: str
    coordinator_task_path: str
    batch_task_paths: tuple[str, ...]
    manifest_path: str
    batch_count: int
    source_count: int


@dataclass(frozen=True)
class SummarizePlannedArtifacts:
    coordinator_planned: PlannedGeneratedTask
    batch_plans: tuple[PlannedGeneratedTask, ...]
    batch_task_paths: tuple[str, ...]
    manifest_relative_path: str
    manifest_path: Path


def summarize_coordinator_task_id(plan_id: str) -> str:
    return f"summarize-{plan_id}-coordinator"


def summarize_manifest_path(plan_id: str) -> str:
    return f".ai-dev/summarize/{plan_id}/manifest.json"


def render_expected_output_manifest_json(batch: SummarizeBatch) -> str:
    payload = {
        "plan_id": batch.plan_id,
        "batch_index": batch.batch_index,
        "outputs": [
            {
                "source_path": entry.source_path,
                "output_path": entry.output_path,
            }
            for entry in batch.entries
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def plan_summarize_task_artifacts(
    *,
    repo_root: Path,
    plan: SummarizePlan,
    batches: tuple[SummarizeBatch, ...],
) -> SummarizePlannedArtifacts:
    if not batches:
        raise SummarizeTaskGenerationError("summarize task preparation requires at least one batch.")

    try:
        coordinator_planned = plan_generated_task(
            repo_root=repo_root,
            task_id=summarize_coordinator_task_id(plan.plan_id),
            task_type="summarize",
            requested_command=f"flow summarize {plan.requested_glob}",
        )
        batch_plans = tuple(
            plan_generated_task(
                repo_root=repo_root,
                task_id=batch.task_id,
                task_type="summarize",
                requested_command=f"flow summarize {plan.requested_glob}",
            )
            for batch in batches
        )
    except TaskArtifactError as exc:
        raise SummarizeTaskGenerationError(str(exc)) from exc

    manifest_relative_path = summarize_manifest_path(plan.plan_id)
    manifest_path = repo_root / manifest_relative_path
    batch_task_paths = tuple(planned.repository_relative_path for planned in batch_plans)

    return SummarizePlannedArtifacts(
        coordinator_planned=coordinator_planned,
        batch_plans=batch_plans,
        batch_task_paths=batch_task_paths,
        manifest_relative_path=manifest_relative_path,
        manifest_path=manifest_path,
    )


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _current_task_pointer(task_id: str, task_type: str, task_file: str) -> str:
    return (
        "# Current AI Dev Task\n\n"
        f"- Task-ID: {task_id}\n"
        f"- Task-Type: {task_type}\n"
        f"- Task-File: {task_file}\n"
    )


def _summary_output_requirements(plan_id: str, source_path: str) -> str:
    return (
        "# Summary\n\n"
        f"Source: {source_path}\n"
        "Generated-By: ai-dev summarize\n"
        f"Plan-ID: {plan_id}\n"
    )


def _source_snapshot_for_entry(
    *,
    repo_root: Path,
    entry: SummarizePlanEntry,
    batch_index: int,
) -> dict[str, object]:
    source_absolute_path = repo_root / entry.source_path
    if not source_absolute_path.exists():
        raise SummarizeTaskGenerationError(
            f"Cannot prepare summarize manifest snapshot: source file does not exist: {entry.source_path}"
        )

    if not source_absolute_path.is_file():
        raise SummarizeTaskGenerationError(
            f"Cannot prepare summarize manifest snapshot: source path is not a regular file: {entry.source_path}"
        )

    try:
        source_bytes = source_absolute_path.read_bytes()
    except OSError as exc:
        raise SummarizeTaskGenerationError(
            f"Cannot prepare summarize manifest snapshot: unable to read source file {entry.source_path}: {exc}"
        ) from exc

    return {
        "source_path": entry.source_path,
        "output_path": entry.output_path,
        "source_digest_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_size_bytes": len(source_bytes),
        "batch_index": batch_index,
        "matched_rule_indexes": list(entry.matched_rule_indexes),
    }


def _source_snapshots_for_batches(
    *,
    repo_root: Path,
    batches: tuple[SummarizeBatch, ...],
) -> tuple[tuple[dict[str, object], ...], ...]:
    return tuple(
        tuple(
            _source_snapshot_for_entry(
                repo_root=repo_root,
                entry=entry,
                batch_index=batch.batch_index,
            )
            for entry in batch.entries
        )
        for batch in batches
    )


def render_summarize_batch_task_markdown(
    *,
    batch: SummarizeBatch,
    planned_task: PlannedGeneratedTask,
    requested_glob: str,
) -> str:
    lines: list[str] = [
        f"# AI Dev Generated Task: {planned_task.task_id}",
        "",
        "## Metadata",
        "",
        f"- Task-ID: {planned_task.task_id}",
        "- Task-Type: summarize",
        f"- Task-File: {planned_task.repository_relative_path}",
        f"- Requested-Glob: {requested_glob}",
        f"- Plan-ID: {batch.plan_id}",
        f"- Batch-Index: {batch.batch_index}",
        f"- Batch-Total: {batch.batch_count}",
        f"- Batch-ID: {batch.batch_id}",
        f"- Source-Count: {batch.source_count}",
        "",
        "## Execution Instructions",
        "",
        "1. Read only the listed source files.",
        "2. Do not modify source files.",
        "3. Write summaries only to the exact listed output paths.",
        "4. Create parent directories as needed.",
        "5. Use UTF-8 Markdown.",
        "6. Follow only the listed instructions and references.",
        "7. Report uncertainty explicitly.",
        "8. Do not silently skip files.",
        "9. Do not alter unrelated files.",
        "10. Provide a completion report as required below.",
        "",
        "## Per-Source Manifest",
        "",
    ]

    for index, entry in enumerate(batch.entries, start=1):
        lines.append(f"### Source {index:03d}")
        lines.append("")
        lines.append(f"- Source-Path: {entry.source_path}")
        lines.append(f"- Output-Path: {entry.output_path}")
        if entry.matched_rule_indexes:
            indexes = ", ".join(str(item) for item in entry.matched_rule_indexes)
            lines.append(f"- Matched-Rule-Indexes: {indexes}")
        else:
            lines.append("- Matched-Rule-Indexes: (none)")
        lines.append(f"- Required-Source-Marker: Source: {entry.source_path}")
        lines.append(f"- Required-Summary-Header: # Summary")
        lines.append("- Required-Generated-By: Generated-By: ai-dev summarize")
        lines.append(f"- Required-Plan-ID: Plan-ID: {batch.plan_id}")
        lines.append("- Required-Summary-Structure:")
        lines.append("")
        lines.append("```markdown")
        lines.append(_summary_output_requirements(batch.plan_id, entry.source_path).rstrip())
        lines.append("```")
        lines.append("")
        lines.append("Ordered instructions:")
        if entry.instructions:
            for instruction_index, instruction in enumerate(entry.instructions, start=1):
                lines.append(f"{instruction_index}. {instruction}")
        else:
            lines.append("1. (no additional summarize rule instructions)")
        lines.append("")

    lines.extend(
        [
            "## Expected Output Manifest",
            "",
            "```json",
            render_expected_output_manifest_json(batch),
            "```",
            "",
            "## Completion Report Requirements",
            "",
            "Report all of the following explicitly:",
            "1. files summarized",
            "2. files skipped",
            "3. uncertainties",
            "4. failures",
            "5. outputs written",
            "",
        ]
    )

    return "\n".join(lines)


def render_summarize_coordinator_task_markdown(
    *,
    plan: SummarizePlan,
    planned_task: PlannedGeneratedTask,
    batch_task_paths: tuple[str, ...],
    manifest_path: str,
) -> str:
    lines = [
        f"# AI Dev Generated Task: {planned_task.task_id}",
        "",
        "## Metadata",
        "",
        f"- Task-ID: {planned_task.task_id}",
        "- Task-Type: summarize",
        f"- Task-File: {planned_task.repository_relative_path}",
        f"- Requested-Glob: {plan.requested_glob}",
        f"- Plan-ID: {plan.plan_id}",
        f"- Batch-Count: {len(batch_task_paths)}",
        f"- Source-Count: {plan.source_count}",
        f"- Manifest-Path: {manifest_path}",
        "",
        "## Execution Instructions",
        "",
        "Execute all listed batch task files in order.",
        "Do not skip batches.",
        "Do not modify source files.",
        "Use the expected output manifest in each batch task.",
        "",
        "## Ordered Batch Tasks",
        "",
    ]

    for batch_index, batch_path in enumerate(batch_task_paths, start=1):
        lines.append(f"{batch_index}. {batch_path}")

    lines.extend(
        [
            "",
            "## Completion Expectation",
            "",
            "Report completion only after all batch tasks are executed.",
            "",
        ]
    )

    return "\n".join(lines)


def _summarize_manifest_json(
    *,
    plan: SummarizePlan,
    coordinator_task_path: str,
    batch_task_paths: tuple[str, ...],
    batches: tuple[SummarizeBatch, ...],
    source_snapshots_by_batch: tuple[tuple[dict[str, object], ...], ...],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plan_id": plan.plan_id,
        "requested_glob": plan.requested_glob,
        "coordinator_task": coordinator_task_path,
        "batch_tasks": list(batch_task_paths),
        "batches": [
            {
                "batch_index": batch.batch_index,
                "batch_count": batch.batch_count,
                "batch_id": batch.batch_id,
                "task_id": batch.task_id,
                "task_file": batch_task_paths[index],
                "source_count": batch.source_count,
                "entries": list(source_snapshots_by_batch[index]),
            }
            for index, batch in enumerate(batches)
        ],
    }


def prepare_summarize_task_artifacts(
    *,
    repo_root: Path,
    plan: SummarizePlan,
    batches: tuple[SummarizeBatch, ...],
    planned_artifacts: SummarizePlannedArtifacts | None = None,
) -> SummarizePreparedTasks:
    planned = planned_artifacts or plan_summarize_task_artifacts(
        repo_root=repo_root,
        plan=plan,
        batches=batches,
    )
    coordinator_planned = planned.coordinator_planned
    batch_plans = planned.batch_plans
    manifest_relative_path = planned.manifest_relative_path
    manifest_path = planned.manifest_path

    collisions = [
        planned.repository_relative_path
        for planned in (coordinator_planned, *batch_plans)
        if planned.absolute_path.exists()
    ]
    if collisions:
        raise SummarizeTaskGenerationError(
            "Cannot overwrite immutable task file(s): " + ", ".join(collisions)
        )

    if manifest_path.exists():
        raise SummarizeTaskGenerationError(
            f"Cannot overwrite immutable summarize manifest: {manifest_relative_path}"
        )

    batch_task_paths = planned.batch_task_paths
    source_snapshots_by_batch = _source_snapshots_for_batches(
        repo_root=repo_root,
        batches=batches,
    )

    coordinator_markdown = render_summarize_coordinator_task_markdown(
        plan=plan,
        planned_task=coordinator_planned,
        batch_task_paths=batch_task_paths,
        manifest_path=manifest_relative_path,
    )
    batch_markdowns = tuple(
        render_summarize_batch_task_markdown(
            batch=batch,
            planned_task=planned,
            requested_glob=plan.requested_glob,
        )
        for batch, planned in zip(batches, batch_plans)
    )

    created_paths: list[Path] = []
    manifest_written = False
    try:
        write_text_atomic(coordinator_planned.absolute_path, coordinator_markdown)
        created_paths.append(coordinator_planned.absolute_path)

        for planned, markdown in zip(batch_plans, batch_markdowns):
            write_text_atomic(planned.absolute_path, markdown)
            created_paths.append(planned.absolute_path)

        write_json_object_atomic(
            manifest_path,
            _summarize_manifest_json(
                plan=plan,
                coordinator_task_path=coordinator_planned.repository_relative_path,
                batch_task_paths=batch_task_paths,
                batches=batches,
                source_snapshots_by_batch=source_snapshots_by_batch,
            ),
        )
        manifest_written = True

        write_text_atomic(
            repo_root / ".ai-dev" / "current-task.md",
            _current_task_pointer(
                coordinator_planned.task_id,
                coordinator_planned.task_type,
                coordinator_planned.repository_relative_path,
            ),
        )
    except JsonFileError as exc:
        cleanup_errors: list[str] = []
        for created_path in reversed(created_paths):
            try:
                _remove_if_exists(created_path)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{created_path}: {cleanup_exc}")

        if manifest_written:
            try:
                _remove_if_exists(manifest_path)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{manifest_path}: {cleanup_exc}")

        if cleanup_errors:
            raise SummarizeTaskGenerationError(
                f"{exc} Cleanup failure while rolling back summarize task preparation: "
                + "; ".join(cleanup_errors)
            ) from exc

        raise SummarizeTaskGenerationError(
            f"{exc} Rolled back summarize task preparation for plan {plan.plan_id}."
        ) from exc

    return SummarizePreparedTasks(
        plan_id=plan.plan_id,
        requested_glob=plan.requested_glob,
        coordinator_task_id=coordinator_planned.task_id,
        coordinator_task_path=coordinator_planned.repository_relative_path,
        batch_task_paths=batch_task_paths,
        manifest_path=manifest_relative_path,
        batch_count=len(batches),
        source_count=plan.source_count,
    )
