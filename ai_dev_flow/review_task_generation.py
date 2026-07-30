from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .json_files import JsonFileError, write_text_atomic
from .review_context import ReviewContext
from .review_paths import ReviewArtifactPaths
from .task_artifacts import PlannedGeneratedTask, TaskArtifactError, plan_generated_task


class ReviewTaskGenerationError(Exception):
    """Raised when deterministic review task preparation fails."""


@dataclass(frozen=True)
class PlannedReviewTask:
    review_id: str
    planned_task: PlannedGeneratedTask

    @property
    def task_id(self) -> str:
        return self.planned_task.task_id

    @property
    def task_type(self) -> str:
        return self.planned_task.task_type

    @property
    def requested_command(self) -> str:
        return self.planned_task.requested_command

    @property
    def repository_relative_path(self) -> str:
        return self.planned_task.repository_relative_path

    @property
    def absolute_path(self) -> Path:
        return self.planned_task.absolute_path


@dataclass(frozen=True)
class PreparedReviewTask:
    review_id: str
    task_id: str
    task_path: str
    review_package_path: str
    changes_diff_path: str
    report_path: str
    scope: str


def build_review_task_id(review_id: str) -> str:
    return f"{review_id}-task"


def plan_review_task(
    *,
    repo_root: Path,
    review_id: str,
    requested_command: str,
) -> PlannedReviewTask:
    task_id = build_review_task_id(review_id)
    try:
        planned = plan_generated_task(
            repo_root=repo_root,
            task_id=task_id,
            task_type="review",
            requested_command=requested_command,
        )
    except TaskArtifactError as exc:
        raise ReviewTaskGenerationError(str(exc)) from exc

    return PlannedReviewTask(review_id=review_id, planned_task=planned)


def _render_ticket_metadata(context: ReviewContext) -> list[str]:
    lines: list[str] = [f"- Workflow-Type: {context.workflow_type}"]

    if context.workflow_type == "issue":
        lines.extend(
            [
                f"- Issue-Number: {context.active_issue_number if context.active_issue_number is not None else '(not available)'}",
                f"- Issue-Title: {context.active_issue_title or '(not available)'}",
                f"- Issue-URL: {context.active_issue_url or '(not available)'}",
            ]
        )
    elif context.workflow_type == "patch":
        lines.append(f"- Patch-Description: {context.patch_description or '(not available)'}")
    else:
        lines.append("- Workflow-Metadata: (not available)")

    return lines


def render_review_task_markdown(
    *,
    planned_task: PlannedReviewTask,
    review_paths: ReviewArtifactPaths,
    context: ReviewContext,
) -> str:
    metadata_lines = [
        f"# AI Dev Generated Task: {planned_task.task_id}",
        "",
        "## Metadata",
        "",
        f"- Task-ID: {planned_task.task_id}",
        "- Task-Type: review",
        f"- Task-File: {planned_task.repository_relative_path}",
        f"- Review-ID: {planned_task.review_id}",
        f"- Review-Scope: {context.scope}",
        f"- Package-Markdown-Path: {review_paths.package_markdown_relative_path}",
        f"- Package-JSON-Path: {review_paths.package_json_relative_path}",
        f"- Changes-Diff-Path: {review_paths.changes_diff_relative_path}",
        f"- Review-Report-Path: {review_paths.canonical_report_relative_path}",
        f"- Checkpoint: {context.checkpoint}",
        f"- Main-Branch: {context.main_branch}",
        f"- Scratch-Branch: {context.scratch_branch}",
        f"- Current-Branch: {context.current_branch}",
    ]
    metadata_lines.extend(_render_ticket_metadata(context))

    execution_lines = [
        "",
        "## Execution Instructions",
        "",
        "You are performing a read-only repository review task.",
        "Do not modify source files, package files, workflow state, Git state, or generated task files.",
        "",
        "1. Read package.md and package.json for review context and scope boundaries.",
        "2. Treat changes.diff as the authoritative patch input.",
        "3. Compare implementation changes against ticket context and acceptance criteria.",
        "4. Evaluate correctness, scope control, safety/security, maintainability, test coverage, and documentation.",
        "5. Distinguish blocking findings from non-blocking findings clearly.",
        "6. Cite exact repository file paths and line ranges or diff locations where practical.",
        "7. State uncertainty and missing context explicitly.",
        "8. Do not invent repository facts or claim unobserved behavior.",
        f"9. Write the final markdown report to exactly: {review_paths.canonical_report_relative_path}",
        "10. Do not modify source files, package files, workflow state, Git state, or generated task files.",
    ]

    required_categories = [
        "",
        "## Required Review Categories",
        "",
        "- Acceptance criteria coverage",
        "- Correctness",
        "- Scope control",
        "- Safety/security",
        "- Error handling",
        "- Determinism/idempotency",
        "- Test coverage",
        "- Documentation",
        "- Backward compatibility",
        "- Blocking findings",
        "- Non-blocking findings",
        "- Uncertainties/missing context",
    ]

    report_contract = [
        "",
        "## Required Report Contract",
        "",
        f"Write UTF-8 markdown to {review_paths.canonical_report_relative_path} with this structure:",
        "",
        "```markdown",
        "# AI Dev Review Report",
        "",
        f"Review-ID: {planned_task.review_id}",
        "Generated-By: external AI review",
        f"Package-Path: {review_paths.package_markdown_relative_path}",
        "",
        "## Decision",
        "- Status: pass | pass-with-notes | blocked",
        "",
        "## Blocking Findings",
        "",
        "## Non-Blocking Findings",
        "",
        "## Acceptance Criteria Assessment",
        "",
        "## Test Assessment",
        "",
        "## Uncertainties and Missing Context",
        "",
        "## Summary",
        "```",
        "",
        "The report must be read-only output. Do not modify repository files other than writing the report markdown.",
    ]

    authoritative_refs = [
        "",
        "## Authoritative Inputs",
        "",
        f"- Package Markdown: {review_paths.package_markdown_relative_path}",
        f"- Package JSON: {review_paths.package_json_relative_path}",
        f"- Authoritative Diff: {review_paths.changes_diff_relative_path}",
        f"- Expected Report Output: {review_paths.canonical_report_relative_path}",
        "",
        "Do not embed full diff content into this task. Read the authoritative artifacts directly.",
    ]

    return "\n".join(metadata_lines + execution_lines + required_categories + report_contract + authoritative_refs) + "\n"


def create_review_task_file(
    *,
    planned_task: PlannedReviewTask,
    markdown_text: str,
) -> bool:
    task_path = planned_task.absolute_path

    if task_path.exists():
        try:
            existing = task_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReviewTaskGenerationError(
                "Cannot read immutable review task file for collision check: "
                f"{planned_task.repository_relative_path}. {exc}"
            ) from exc

        if existing == markdown_text:
            return False

        raise ReviewTaskGenerationError(
            "Cannot overwrite immutable task file: "
            f"{planned_task.repository_relative_path}"
        )

    try:
        write_text_atomic(task_path, markdown_text)
    except JsonFileError as exc:
        raise ReviewTaskGenerationError(str(exc)) from exc

    return True


def current_task_pointer_text(*, planned_task: PlannedReviewTask) -> str:
    return (
        "# Current AI Dev Task\n\n"
        f"- Task-ID: {planned_task.task_id}\n"
        "- Task-Type: review\n"
        f"- Task-File: {planned_task.repository_relative_path}\n"
    )


def write_current_task_pointer(
    *,
    repo_root: Path,
    planned_task: PlannedReviewTask,
) -> None:
    pointer_path = repo_root / ".ai-dev" / "current-task.md"
    try:
        write_text_atomic(pointer_path, current_task_pointer_text(planned_task=planned_task))
    except JsonFileError as exc:
        raise ReviewTaskGenerationError(str(exc)) from exc
