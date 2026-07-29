from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .summarize_planning import SummarizePlan, SummarizePlanEntry


class SummarizeBatchingError(Exception):
    """Raised when summarize batch planning fails."""


@dataclass(frozen=True)
class SummarizeBatch:
    plan_id: str
    batch_index: int
    batch_count: int
    entries: tuple[SummarizePlanEntry, ...]
    batch_id: str
    task_id: str
    expected_output_paths: tuple[str, ...]
    source_count: int


def _validate_max_files(max_files: int) -> None:
    if isinstance(max_files, bool) or not isinstance(max_files, int):
        raise SummarizeBatchingError("summarize batch max_files must be an integer greater than zero.")

    if max_files <= 0:
        raise SummarizeBatchingError("summarize batch max_files must be greater than zero.")


def _batch_id_for_entries(plan_id: str, batch_index: int, entries: tuple[SummarizePlanEntry, ...]) -> str:
    payload = {
        "plan_id": plan_id,
        "batch_index": batch_index,
        "entries": [
            {
                "source_path": entry.source_path,
                "output_path": entry.output_path,
                "matched_rule_indexes": list(entry.matched_rule_indexes),
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_summarize_batches(plan: SummarizePlan, *, max_files: int) -> tuple[SummarizeBatch, ...]:
    _validate_max_files(max_files)

    entries = plan.entries
    if not entries:
        raise SummarizeBatchingError("summarize plan has no source entries to batch.")

    slices = [entries[index : index + max_files] for index in range(0, len(entries), max_files)]
    batch_count = len(slices)

    batches: list[SummarizeBatch] = []
    for raw_index, batch_entries in enumerate(slices):
        batch_index = raw_index + 1
        frozen_entries = tuple(batch_entries)
        batch_id = _batch_id_for_entries(plan.plan_id, batch_index, frozen_entries)
        task_id = f"summarize-{plan.plan_id}-batch-{batch_index:03d}"

        batches.append(
            SummarizeBatch(
                plan_id=plan.plan_id,
                batch_index=batch_index,
                batch_count=batch_count,
                entries=frozen_entries,
                batch_id=batch_id,
                task_id=task_id,
                expected_output_paths=tuple(entry.output_path for entry in frozen_entries),
                source_count=len(frozen_entries),
            )
        )

    return tuple(batches)
