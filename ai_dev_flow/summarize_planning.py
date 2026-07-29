from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath, Path

from .summarize_config import SummarizeRule, load_repository_summarize_config
from .summarize_discovery import discover_source_paths
from .summarize_glob import matches_glob, normalize_path_text, rule_specificity_key, validate_requested_glob


class SummarizePlanningError(Exception):
    """Raised when deterministic summarize planning fails."""


@dataclass(frozen=True)
class SummarizePlanEntry:
    source_path: str
    output_path: str
    instructions: tuple[str, ...]
    matched_rule_indexes: tuple[int, ...]


@dataclass(frozen=True)
class SummarizePlan:
    requested_glob: str
    entries: tuple[SummarizePlanEntry, ...]
    rule_count: int
    source_count: int
    matched_rule_count: int
    plan_id: str


def summary_output_path_for_source(source_path: str) -> str:
    normalized = normalize_path_text(source_path)
    if not normalized:
        raise SummarizePlanningError("source path cannot be empty.")

    source = PurePosixPath(normalized)
    if source.is_absolute() or ".." in source.parts:
        raise SummarizePlanningError(f"source path must remain inside repository root: {source_path}")

    output = PurePosixPath(".ai-dev") / "summaries" / f"{source.as_posix()}.md"
    normalized_output = output.as_posix()

    if ".." in PurePosixPath(normalized_output).parts:
        raise SummarizePlanningError(f"computed output path escaped repository: {normalized_output}")

    return normalized_output


def resolve_matching_rules(source_path: str, rules: tuple[SummarizeRule, ...]) -> tuple[SummarizeRule, ...]:
    normalized_source_path = normalize_path_text(source_path)
    matched = [rule for rule in rules if matches_glob(normalized_source_path, rule.match)]
    matched.sort(key=lambda rule: rule_specificity_key(rule.match, rule.index))
    return tuple(matched)


def _compute_plan_id(requested_glob: str, entries: tuple[SummarizePlanEntry, ...]) -> str:
    payload = {
        "requested_glob": requested_glob,
        "entries": [
            {
                "source_path": entry.source_path,
                "output_path": entry.output_path,
                "instructions": list(entry.instructions),
                "matched_rule_indexes": list(entry.matched_rule_indexes),
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_summarize_plan(repo_root: Path, requested_glob: str) -> SummarizePlan:
    normalized_glob = validate_requested_glob(requested_glob)
    summarize_config = load_repository_summarize_config(repo_root)
    source_paths = discover_source_paths(repo_root, normalized_glob)

    entries: list[SummarizePlanEntry] = []
    output_paths: set[str] = set()
    matched_rule_total = 0

    for source_path in source_paths:
        output_path = summary_output_path_for_source(source_path)
        if output_path in output_paths:
            raise SummarizePlanningError(
                f"Summary output collision detected for source {source_path}: {output_path}"
            )

        output_paths.add(output_path)

        matched_rules = resolve_matching_rules(source_path, summarize_config.rules)
        instructions = tuple(rule.instructions for rule in matched_rules)
        matched_rule_indexes = tuple(rule.index for rule in matched_rules)
        matched_rule_total += len(matched_rules)

        entries.append(
            SummarizePlanEntry(
                source_path=source_path,
                output_path=output_path,
                instructions=instructions,
                matched_rule_indexes=matched_rule_indexes,
            )
        )

    frozen_entries = tuple(entries)

    return SummarizePlan(
        requested_glob=normalized_glob,
        entries=frozen_entries,
        rule_count=len(summarize_config.rules),
        source_count=len(frozen_entries),
        matched_rule_count=matched_rule_total,
        plan_id=_compute_plan_id(normalized_glob, frozen_entries),
    )
