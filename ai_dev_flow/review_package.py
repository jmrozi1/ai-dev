from __future__ import annotations

from pathlib import Path
import json

from .json_files import JsonFileError, write_text_atomic
from .review_context import ReviewContext, review_context_payload
from .review_paths import ReviewArtifactPaths


class ReviewPackageError(Exception):
    """Raised for deterministic review package write failures."""


def _render_acceptance_criteria_block(context: ReviewContext) -> str:
    if context.acceptance_criteria_status != "available_local":
        return "(unavailable locally)"

    if not context.acceptance_criteria_lines:
        return "(Acceptance criteria section is present but empty)"

    return "\n".join(context.acceptance_criteria_lines)


def render_changes_diff(context: ReviewContext) -> str:
    lines: list[str] = [
        "# AI Dev Review Changes",
        f"# Scope: {context.scope}",
        "",
    ]

    if context.scope == "workflow":
        lines.extend(
            [
                f"## Committed workflow diff: {context.committed.reference}",
                "",
            ]
        )
        if context.committed.diff_text:
            lines.append(context.committed.diff_text.rstrip("\n"))
        else:
            lines.append("(none)")
        lines.extend(["", f"## Staged overlay diff: {context.overlay.reference}", ""])
        if context.overlay.diff_text:
            lines.append(context.overlay.diff_text.rstrip("\n"))
        else:
            lines.append("(none)")
    else:
        lines.extend(
            [
                f"## Staged checkpoint diff: {context.overlay.reference}",
                "",
            ]
        )
        if context.overlay.diff_text:
            lines.append(context.overlay.diff_text.rstrip("\n"))
        else:
            lines.append("(none)")

    lines.append("")
    return "\n".join(lines)


def _render_instruction_references(context: ReviewContext) -> str:
    if not context.instruction_reference_paths:
        return "- (none discovered)"
    return "\n".join(f"- {path}" for path in context.instruction_reference_paths)


def _render_diagnostics(context: ReviewContext) -> str:
    if not context.diagnostics:
        return "- (none)"
    return "\n".join(f"- {item}" for item in context.diagnostics)


def _render_package_markdown(
    *,
    review_id: str,
    context: ReviewContext,
) -> str:
    scope_explanation = (
        "Includes committed workflow diff and staged overlay diff as separate sections."
        if context.scope == "workflow"
        else "Includes staged checkpoint diff only."
    )

    return "\n".join(
        [
            f"# AI Dev Review Package: {review_id}",
            "",
            "## Metadata",
            "",
            f"- Review-ID: {review_id}",
            f"- Scope: {context.scope}",
            f"- Command: {context.command}",
            f"- Workflow-Type: {context.workflow_type}",
            f"- Main-Branch: {context.main_branch}",
            f"- Scratch-Branch: {context.scratch_branch}",
            f"- Current-Branch: {context.current_branch}",
            f"- Checkpoint: {context.checkpoint}",
            f"- Issue-Number: {context.active_issue_number if context.active_issue_number is not None else '(not applicable)'}",
            f"- Issue-Title: {context.active_issue_title or '(not applicable)'}",
            f"- Issue-URL: {context.active_issue_url or '(not applicable)'}",
            f"- Patch-Description: {context.patch_description or '(not applicable)'}",
            "",
            "## Acceptance Criteria",
            "",
            f"- Status: {context.acceptance_criteria_status}",
            f"- Heading: {context.acceptance_criteria_heading}",
            "",
            _render_acceptance_criteria_block(context),
            "",
            "## Change Package",
            "",
            f"- Changes-Diff-Path: {context.artifacts.changes_diff_path}",
            f"- Changes-Diff-SHA256: {context.changes_diff_sha256}",
            f"- Committed-Path-Count: {len(context.committed.paths)}",
            f"- Overlay-Path-Count: {len(context.overlay.paths)}",
            f"- Total-Unique-Path-Count: {len(context.all_paths)}",
            f"- Scope-Explanation: {scope_explanation}",
            "",
            "## Review Instruction References",
            "",
            _render_instruction_references(context),
            "",
            "## Diagnostics",
            "",
            _render_diagnostics(context),
            "",
        ]
    )


def _render_package_json(review_id: str, context: ReviewContext) -> dict[str, object]:
    payload = review_context_payload(context)
    payload["review_id"] = review_id
    return payload


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def create_review_package(
    *,
    repo_root: Path,
    review_paths: ReviewArtifactPaths,
    review_id: str,
    context: ReviewContext,
    changes_diff_text: str,
) -> None:
    package_markdown = _render_package_markdown(review_id=review_id, context=context)
    package_json_payload = _render_package_json(review_id=review_id, context=context)
    package_json_text = json.dumps(
        package_json_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"

    if review_paths.review_root_absolute_path.exists():
        required_existing = [
            review_paths.package_markdown_absolute_path,
            review_paths.package_json_absolute_path,
            review_paths.changes_diff_absolute_path,
        ]
        if not all(path.exists() for path in required_existing):
            raise ReviewPackageError(
                "Cannot overwrite immutable review package directory: "
                f"{review_paths.review_root_relative_path}"
            )

        try:
            existing_package_markdown = review_paths.package_markdown_absolute_path.read_text(
                encoding="utf-8"
            )
            existing_package_json = review_paths.package_json_absolute_path.read_text(
                encoding="utf-8"
            )
            existing_changes_diff = review_paths.changes_diff_absolute_path.read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            raise ReviewPackageError(
                "Cannot read immutable review package artifacts for collision check: "
                f"{review_paths.review_root_relative_path}. {exc}"
            ) from exc

        expected_package_json = json.dumps(
            package_json_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"

        if (
            existing_package_markdown == package_markdown
            and existing_package_json == expected_package_json
            and existing_changes_diff == changes_diff_text
        ):
            return

        raise ReviewPackageError(
            "Cannot overwrite immutable review package directory: "
            f"{review_paths.review_root_relative_path}"
        )

    artifact_targets = [
        review_paths.package_markdown_absolute_path,
        review_paths.package_json_absolute_path,
        review_paths.changes_diff_absolute_path,
    ]

    for artifact_path in artifact_targets:
        if artifact_path.exists():
            raise ReviewPackageError(
                "Cannot overwrite immutable review artifact: "
                f"{artifact_path.relative_to(repo_root).as_posix()}"
            )

    written_paths: list[Path] = []

    try:
        write_text_atomic(review_paths.package_markdown_absolute_path, package_markdown)
        written_paths.append(review_paths.package_markdown_absolute_path)

        write_text_atomic(review_paths.package_json_absolute_path, package_json_text)
        written_paths.append(review_paths.package_json_absolute_path)

        write_text_atomic(review_paths.changes_diff_absolute_path, changes_diff_text)
        written_paths.append(review_paths.changes_diff_absolute_path)
    except JsonFileError as exc:
        for written_path in reversed(written_paths):
            try:
                _remove_file_if_exists(written_path)
            except OSError:
                pass
        try:
            review_paths.review_root_absolute_path.rmdir()
        except OSError:
            pass
        raise ReviewPackageError(str(exc)) from exc
