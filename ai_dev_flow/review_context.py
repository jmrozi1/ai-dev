from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re


class ReviewContextError(Exception):
    """Raised for deterministic review context errors."""


@dataclass(frozen=True)
class AcceptanceCriteriaSection:
    heading: str
    lines: tuple[str, ...]
    found: bool


@dataclass(frozen=True)
class ReviewDiffScope:
    reference: str
    paths: tuple[str, ...]
    diff_text: str
    diff_sha256: str | None


@dataclass(frozen=True)
class ReviewArtifacts:
    review_root_path: str
    package_markdown_path: str
    package_json_path: str
    changes_diff_path: str
    canonical_report_path: str


@dataclass(frozen=True)
class ReviewContext:
    scope: str
    command: str
    workflow_type: str
    main_branch: str
    scratch_branch: str
    current_branch: str
    checkpoint: int
    active_issue_number: int | None
    active_issue_title: str | None
    active_issue_url: str | None
    patch_description: str | None
    issue_description_status: str
    issue_description_source: str | None
    acceptance_criteria_status: str
    acceptance_criteria_heading: str
    acceptance_criteria_lines: tuple[str, ...]
    committed: ReviewDiffScope
    overlay: ReviewDiffScope
    all_paths: tuple[str, ...]
    changes_diff_sha256: str
    instruction_reference_paths: tuple[str, ...]
    diagnostics: tuple[str, ...]
    artifacts: ReviewArtifacts


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_non_empty_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ReviewContextError(f"{name} cannot be empty.")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _validate_digest(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    if len(normalized) != 64:
        raise ReviewContextError(f"{name} must be a 64-character lowercase hex digest.")
    allowed = set("0123456789abcdef")
    if any(character not in allowed for character in normalized):
        raise ReviewContextError(f"{name} must be a 64-character lowercase hex digest.")
    return normalized


def _normalize_repo_relative_path(path_text: str) -> str:
    if path_text == "":
        raise ReviewContextError("Repository path cannot be empty.")
    if "\x00" in path_text:
        raise ReviewContextError("Repository path cannot contain NUL bytes.")
    if path_text.startswith("/"):
        raise ReviewContextError(f"Repository path must be relative: {path_text!r}")
    if path_text.startswith("../") or "/../" in path_text:
        raise ReviewContextError(f"Repository path cannot escape repository root: {path_text!r}")
    return path_text


def _normalize_paths(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = [_normalize_repo_relative_path(path) for path in paths]
    return tuple(sorted(set(normalized)))


def _normalize_artifact_path(path_text: str) -> str:
    candidate = PurePosixPath(path_text.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReviewContextError(
            f"Artifact path must be repository-relative without traversal: {path_text!r}"
        )
    normalized = candidate.as_posix()
    if not normalized or normalized == ".":
        raise ReviewContextError("Artifact path cannot be empty.")
    return normalized


def extract_acceptance_criteria_section(issue_markdown: str) -> AcceptanceCriteriaSection:
    lines = issue_markdown.splitlines()
    target_index: int | None = None
    target_level: int | None = None
    target_heading = ""

    heading_pattern = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.IGNORECASE)

    for index, line in enumerate(lines):
        match = heading_pattern.match(line)
        if match is None:
            continue

        heading_text = match.group(2).strip().lower()
        if heading_text == "acceptance criteria":
            target_index = index
            target_level = len(match.group(1))
            target_heading = match.group(2).strip()
            break

    if target_index is None or target_level is None:
        return AcceptanceCriteriaSection(
            heading="Acceptance criteria",
            lines=(),
            found=False,
        )

    collected: list[str] = []
    for line in lines[target_index + 1 :]:
        match = heading_pattern.match(line)
        if match is not None and len(match.group(1)) <= target_level:
            break
        collected.append(line.rstrip())

    while collected and not collected[-1]:
        collected.pop()

    return AcceptanceCriteriaSection(
        heading=target_heading or "Acceptance criteria",
        lines=tuple(collected),
        found=True,
    )


def read_local_issue_markdown(repo_root: Path, issue_number: int) -> tuple[str | None, str | None]:
    candidates = [
        repo_root / "issues" / f"{issue_number}.md",
        repo_root / "docs" / "issues" / f"{issue_number}.md",
        repo_root / "tickets" / f"{issue_number}.md",
    ]

    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            return candidate.read_text(encoding="utf-8", errors="replace"), candidate.relative_to(repo_root).as_posix()
        except OSError as exc:
            raise ReviewContextError(
                f"Cannot read local issue metadata file {candidate}: {exc}"
            ) from exc

    return None, None


def build_review_context(
    *,
    scope: str,
    command: str,
    workflow_type: str,
    main_branch: str,
    scratch_branch: str,
    current_branch: str,
    checkpoint: int,
    active_issue_number: int | None,
    active_issue_title: str | None,
    active_issue_url: str | None,
    patch_description: str | None,
    issue_description_status: str,
    issue_description_source: str | None,
    acceptance_criteria_status: str,
    acceptance_criteria_heading: str,
    acceptance_criteria_lines: list[str] | tuple[str, ...],
    committed_reference: str,
    committed_paths: list[str] | tuple[str, ...],
    committed_diff_text: str,
    committed_diff_sha256: str | None,
    overlay_reference: str,
    overlay_paths: list[str] | tuple[str, ...],
    overlay_diff_text: str,
    overlay_diff_sha256: str | None,
    all_paths: list[str] | tuple[str, ...],
    changes_diff_sha256: str,
    instruction_reference_paths: list[str] | tuple[str, ...],
    diagnostics: list[str] | tuple[str, ...],
    review_root_path: str,
    package_markdown_path: str,
    package_json_path: str,
    changes_diff_path: str,
    canonical_report_path: str,
) -> ReviewContext:
    normalized_scope = scope.strip().lower()
    if normalized_scope not in {"checkpoint", "workflow"}:
        raise ReviewContextError("scope must be one of: 'checkpoint', 'workflow'.")

    normalized_workflow_type = workflow_type.strip().lower()
    if normalized_workflow_type not in {"issue", "patch", "none"}:
        raise ReviewContextError("workflow_type must be one of: 'issue', 'patch', 'none'.")

    if checkpoint < 0:
        raise ReviewContextError("checkpoint must be >= 0.")

    valid_statuses = {"available_local", "unavailable_local", "not_applicable"}
    if issue_description_status not in valid_statuses:
        raise ReviewContextError("issue_description_status is invalid.")
    if acceptance_criteria_status not in valid_statuses:
        raise ReviewContextError("acceptance_criteria_status is invalid.")

    committed_scope = ReviewDiffScope(
        reference=_normalize_non_empty_text("committed_reference", committed_reference),
        paths=_normalize_paths(committed_paths),
        diff_text=committed_diff_text,
        diff_sha256=_validate_digest("committed_diff_sha256", committed_diff_sha256),
    )
    overlay_scope = ReviewDiffScope(
        reference=_normalize_non_empty_text("overlay_reference", overlay_reference),
        paths=_normalize_paths(overlay_paths),
        diff_text=overlay_diff_text,
        diff_sha256=_validate_digest("overlay_diff_sha256", overlay_diff_sha256),
    )

    normalized_all_paths = _normalize_paths(all_paths)

    artifacts = ReviewArtifacts(
        review_root_path=_normalize_artifact_path(review_root_path),
        package_markdown_path=_normalize_artifact_path(package_markdown_path),
        package_json_path=_normalize_artifact_path(package_json_path),
        changes_diff_path=_normalize_artifact_path(changes_diff_path),
        canonical_report_path=_normalize_artifact_path(canonical_report_path),
    )

    normalized_command = _normalize_non_empty_text("command", command)
    normalized_main_branch = _normalize_non_empty_text("main_branch", main_branch)
    normalized_scratch_branch = _normalize_non_empty_text("scratch_branch", scratch_branch)
    normalized_current_branch = _normalize_non_empty_text("current_branch", current_branch)

    return ReviewContext(
        scope=normalized_scope,
        command=normalized_command,
        workflow_type=normalized_workflow_type,
        main_branch=normalized_main_branch,
        scratch_branch=normalized_scratch_branch,
        current_branch=normalized_current_branch,
        checkpoint=checkpoint,
        active_issue_number=active_issue_number,
        active_issue_title=_normalize_optional_text(active_issue_title),
        active_issue_url=_normalize_optional_text(active_issue_url),
        patch_description=_normalize_optional_text(patch_description),
        issue_description_status=issue_description_status,
        issue_description_source=_normalize_optional_text(issue_description_source),
        acceptance_criteria_status=acceptance_criteria_status,
        acceptance_criteria_heading=acceptance_criteria_heading.strip() or "Acceptance criteria",
        acceptance_criteria_lines=tuple(acceptance_criteria_lines),
        committed=committed_scope,
        overlay=overlay_scope,
        all_paths=normalized_all_paths,
        changes_diff_sha256=_validate_digest("changes_diff_sha256", changes_diff_sha256) or "",
        instruction_reference_paths=_normalize_paths(instruction_reference_paths),
        diagnostics=tuple(diagnostics),
        artifacts=artifacts,
    )


def review_context_payload(context: ReviewContext) -> dict[str, object]:
    ticket_payload: dict[str, object] = {
        "workflow_type": context.workflow_type,
        "issue_number": context.active_issue_number,
        "issue_title": context.active_issue_title,
        "issue_url": context.active_issue_url,
        "patch_description": context.patch_description,
        "issue_description_status": context.issue_description_status,
        "issue_description_source": context.issue_description_source,
    }

    acceptance_payload: dict[str, object] = {
        "status": context.acceptance_criteria_status,
        "heading": context.acceptance_criteria_heading,
        "lines": list(context.acceptance_criteria_lines),
    }

    changes_payload: dict[str, object] = {
        "committed_reference": context.committed.reference,
        "committed_paths": list(context.committed.paths),
        "committed_diff_sha256": context.committed.diff_sha256,
        "overlay_reference": context.overlay.reference,
        "overlay_paths": list(context.overlay.paths),
        "overlay_diff_sha256": context.overlay.diff_sha256,
        "all_paths": list(context.all_paths),
        "changes_diff_path": context.artifacts.changes_diff_path,
        "changes_diff_sha256": context.changes_diff_sha256,
    }

    return {
        "schema_version": 1,
        "scope": context.scope,
        "command": context.command,
        "workflow": {
            "main_branch": context.main_branch,
            "scratch_branch": context.scratch_branch,
            "current_branch": context.current_branch,
            "checkpoint": context.checkpoint,
        },
        "repository": {
            "main_branch": context.main_branch,
            "scratch_branch": context.scratch_branch,
            "current_branch": context.current_branch,
        },
        "ticket": ticket_payload,
        "acceptance_criteria": acceptance_payload,
        "changes": changes_payload,
        "instructions": {
            "reference_paths": list(context.instruction_reference_paths),
            "discovery_rule": "Existing files only from ai-dev-core/workflows/review/{review-documentation.md,finding-template.md} and vendor mirror.",
        },
        "artifacts": {
            "review_root_path": context.artifacts.review_root_path,
            "package_markdown_path": context.artifacts.package_markdown_path,
            "package_json_path": context.artifacts.package_json_path,
            "changes_diff_path": context.artifacts.changes_diff_path,
            "canonical_report_path": context.artifacts.canonical_report_path,
        },
        "diagnostics": list(context.diagnostics),
    }


def stable_review_context_payload_json(context: ReviewContext) -> str:
    payload = review_context_payload(context)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_review_id(context: ReviewContext) -> str:
    payload = review_context_payload(context)
    changes_payload = payload.get("changes")
    if isinstance(changes_payload, dict):
        changes_payload["changes_diff_path"] = ".ai-dev/review/changes.diff"
    payload["artifacts"] = {
        "review_root_path": ".ai-dev/review",
        "package_markdown_path": ".ai-dev/review/package.md",
        "package_json_path": ".ai-dev/review/package.json",
        "changes_diff_path": ".ai-dev/review/changes.diff",
        "canonical_report_path": ".ai-dev/review/report.md",
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"review-{digest[:16]}"
