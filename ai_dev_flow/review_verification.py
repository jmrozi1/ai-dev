from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .json_files import JsonFileError, write_text_atomic
from .review_manifest import (
    ReviewManifestError,
    expected_review_artifact_paths,
    expected_review_task_file,
    expected_review_task_id,
    review_verification_json_path,
    review_verification_markdown_path,
    validate_repo_relative_path,
    validate_review_id,
)


class ReviewVerificationError(Exception):
    """Raised for review verification failures."""


ARTIFACT_STATE_VALID = "valid"
ARTIFACT_STATE_MISSING = "missing"
ARTIFACT_STATE_NOT_REGULAR = "not-regular-file"
ARTIFACT_STATE_UNREADABLE = "unreadable"
ARTIFACT_STATE_DIGEST_MISMATCH = "digest-mismatch"
ARTIFACT_STATE_INVALID = "invalid"

REPORT_STATE_VALID = "valid"
REPORT_STATE_MISSING = "missing"
REPORT_STATE_NOT_REGULAR = "not-regular-file"
REPORT_STATE_UNREADABLE = "unreadable"
REPORT_STATE_EMPTY = "empty"
REPORT_STATE_INVALID_HEADING = "invalid-heading"
REPORT_STATE_MISSING_REVIEW_ID = "missing-review-id"
REPORT_STATE_WRONG_REVIEW_ID = "wrong-review-id"
REPORT_STATE_MISSING_GENERATOR = "missing-generator-marker"
REPORT_STATE_WRONG_GENERATOR = "wrong-generator-marker"
REPORT_STATE_MISSING_PACKAGE_PATH = "missing-package-path"
REPORT_STATE_WRONG_PACKAGE_PATH = "wrong-package-path"
REPORT_STATE_MISSING_DECISION = "missing-decision"
REPORT_STATE_INVALID_DECISION = "invalid-decision"
REPORT_STATE_MISSING_SECTION = "missing-section"
REPORT_STATE_DUPLICATE_SECTION = "duplicate-section"
REPORT_STATE_SECTION_ORDER_INVALID = "section-order-invalid"
REPORT_STATE_MULTIPLE_DECISIONS = "multiple-decision-statuses"
REPORT_STATE_DECISION_OUTSIDE_SECTION = "decision-status-outside-decision-section"

OVERALL_STATUS_COMPLETE = "complete"
OVERALL_STATUS_INCOMPLETE = "incomplete"
OVERALL_STATUS_INVALID = "invalid"

DECISION_PASS = "pass"
DECISION_PASS_WITH_NOTES = "pass-with-notes"
DECISION_BLOCKED = "blocked"
ALLOWED_DECISIONS = {DECISION_PASS, DECISION_PASS_WITH_NOTES, DECISION_BLOCKED}

REQUIRED_REPORT_SECTIONS = (
    "## Decision",
    "## Blocking Findings",
    "## Non-Blocking Findings",
    "## Acceptance Criteria Assessment",
    "## Test Assessment",
    "## Uncertainties and Missing Context",
    "## Summary",
)
_LEVEL2_HEADING_PATTERN = re.compile(r"^## [^#].*$")


@dataclass(frozen=True)
class ArtifactVerificationState:
    artifact_name: str
    expected_path: str
    status: str
    reason_codes: tuple[str, ...]
    expected_digest_sha256: str | None = None
    actual_digest_sha256: str | None = None


@dataclass(frozen=True)
class ReportVerificationState:
    expected_path: str
    status: str
    reason_codes: tuple[str, ...]
    message: str
    decision_status: str | None


@dataclass(frozen=True)
class ReviewVerificationResult:
    review_id: str
    overall_status: str
    package_path: str
    task_path: str
    report_path: str
    package_state: ArtifactVerificationState
    task_state: ArtifactVerificationState
    report_state: ReportVerificationState
    review_decision: str | None
    recommended_next_action: str


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_review_package(
    *,
    repo_root: Path,
    review_id: str,
    package_path: str,
    package_json_path: str,
    changes_diff_path: str,
) -> ArtifactVerificationState:
    package_abs = repo_root / package_path
    package_json_abs = repo_root / package_json_path
    changes_diff_abs = repo_root / changes_diff_path

    for name, absolute in (("package", package_abs), ("package-json", package_json_abs), ("changes-diff", changes_diff_abs)):
        if not absolute.exists():
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_MISSING,
                reason_codes=(f"{name}-missing",),
            )
        if not absolute.is_file():
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_NOT_REGULAR,
                reason_codes=(f"{name}-not-regular-file",),
            )

    try:
        package_text = package_abs.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("package-markdown-invalid-utf8",),
        )
    except OSError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("package-markdown-unreadable",),
        )

    package_digest = _sha256_hex(package_text.encode("utf-8"))

    try:
        package_json_text = package_json_abs.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("package-json-invalid-utf8",),
            actual_digest_sha256=package_digest,
        )
    except OSError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("package-json-unreadable",),
            actual_digest_sha256=package_digest,
        )

    try:
        changes_diff_bytes = changes_diff_abs.read_bytes()
    except OSError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("changes-diff-unreadable",),
            actual_digest_sha256=package_digest,
        )

    try:
        payload = json.loads(package_json_text)
    except json.JSONDecodeError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-json-invalid-json",),
            actual_digest_sha256=package_digest,
        )

    if not isinstance(payload, dict):
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-json-not-object",),
            actual_digest_sha256=package_digest,
        )

    schema_version = payload.get("schema_version")
    if schema_version is None:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("missing-schema-version",),
            actual_digest_sha256=package_digest,
        )
    if not isinstance(schema_version, int):
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("invalid-schema-version-type",),
            actual_digest_sha256=package_digest,
        )
    if schema_version != 1:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("unsupported-schema-version",),
            actual_digest_sha256=package_digest,
        )

    required_field_types: dict[str, type[object]] = {
        "scope": str,
        "command": str,
        "workflow": dict,
        "repository": dict,
        "ticket": dict,
        "acceptance_criteria": dict,
        "changes": dict,
        "instructions": dict,
        "artifacts": dict,
        "diagnostics": list,
        "review_id": str,
    }
    for field_name, expected_type in required_field_types.items():
        if field_name not in payload:
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=("missing-package-field", field_name),
                actual_digest_sha256=package_digest,
            )
        if not isinstance(payload[field_name], expected_type):
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=("invalid-package-field-type", field_name),
                actual_digest_sha256=package_digest,
            )

    if payload.get("review_id") != review_id:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-review-id-mismatch",),
            actual_digest_sha256=package_digest,
        )

    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)

    artifact_expected_paths = {
        "review_root_path": ".ai-dev/review",
        "package_markdown_path": package_path,
        "package_json_path": package_json_path,
        "changes_diff_path": changes_diff_path,
        "canonical_report_path": ".ai-dev/review/report.md",
    }
    for field_name, expected_value in artifact_expected_paths.items():
        raw_value = artifacts.get(field_name)
        if not isinstance(raw_value, str):
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=("invalid-artifact-path-field", field_name),
                actual_digest_sha256=package_digest,
            )

        try:
            normalized_value = validate_repo_relative_path(raw_value, label=f"artifacts.{field_name}")
        except ReviewManifestError:
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=("artifact-path-traversal-or-absolute", field_name),
                actual_digest_sha256=package_digest,
            )

        if normalized_value != expected_value:
            return ArtifactVerificationState(
                artifact_name="package",
                expected_path=package_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=("artifact-path-mismatch", field_name),
                actual_digest_sha256=package_digest,
            )

    changes = payload["changes"]
    assert isinstance(changes, dict)
    changes_diff_path_value = changes.get("changes_diff_path")
    if not isinstance(changes_diff_path_value, str):
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("invalid-changes-diff-path-field",),
            actual_digest_sha256=package_digest,
        )
    try:
        normalized_changes_diff_path = validate_repo_relative_path(
            changes_diff_path_value,
            label="changes.changes_diff_path",
        )
    except ReviewManifestError:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("changes-diff-path-traversal-or-absolute",),
            actual_digest_sha256=package_digest,
        )
    if normalized_changes_diff_path != changes_diff_path:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("changes-diff-path-mismatch",),
            actual_digest_sha256=package_digest,
        )

    expected_changes_digest = changes.get("changes_diff_sha256")
    if not isinstance(expected_changes_digest, str):
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("invalid-changes-diff-digest-field",),
            actual_digest_sha256=package_digest,
        )

    actual_changes_digest = _sha256_hex(changes_diff_bytes)
    if expected_changes_digest != actual_changes_digest:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_DIGEST_MISMATCH,
            reason_codes=("changes-diff-digest-mismatch",),
            expected_digest_sha256=expected_changes_digest,
            actual_digest_sha256=actual_changes_digest,
        )

    package_lines = package_text.splitlines()
    review_marker_lines = [line for line in package_lines if line.startswith("- Review-ID: ")]
    if not review_marker_lines:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-missing-review-id-marker",),
            actual_digest_sha256=package_digest,
        )
    if len(review_marker_lines) > 1:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-duplicate-review-id-marker",),
            actual_digest_sha256=package_digest,
        )
    if review_marker_lines[0] != f"- Review-ID: {review_id}":
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-review-id-mismatch",),
            actual_digest_sha256=package_digest,
        )

    diff_marker_lines = [line for line in package_lines if line.startswith("- Changes-Diff-Path: ")]
    if not diff_marker_lines:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-missing-diff-path-marker",),
            actual_digest_sha256=package_digest,
        )
    if len(diff_marker_lines) > 1:
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-duplicate-diff-path-marker",),
            actual_digest_sha256=package_digest,
        )
    if diff_marker_lines[0] != f"- Changes-Diff-Path: {changes_diff_path}":
        return ArtifactVerificationState(
            artifact_name="package",
            expected_path=package_path,
            status=ARTIFACT_STATE_INVALID,
            reason_codes=("package-markdown-diff-path-mismatch",),
            actual_digest_sha256=package_digest,
        )

    return ArtifactVerificationState(
        artifact_name="package",
        expected_path=package_path,
        status=ARTIFACT_STATE_VALID,
        reason_codes=(),
        expected_digest_sha256=actual_changes_digest,
        actual_digest_sha256=package_digest,
    )


def _verify_review_task(
    *,
    repo_root: Path,
    review_id: str,
    task_path: str,
    expected_package_path: str,
    expected_report_path: str,
) -> ArtifactVerificationState:
    task_absolute = repo_root / task_path
    if not task_absolute.exists():
        return ArtifactVerificationState(
            artifact_name="task",
            expected_path=task_path,
            status=ARTIFACT_STATE_MISSING,
            reason_codes=("task-missing",),
        )
    if not task_absolute.is_file():
        return ArtifactVerificationState(
            artifact_name="task",
            expected_path=task_path,
            status=ARTIFACT_STATE_NOT_REGULAR,
            reason_codes=("task-not-regular-file",),
        )

    try:
        task_text = task_absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ArtifactVerificationState(
            artifact_name="task",
            expected_path=task_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("task-invalid-utf8",),
        )
    except OSError:
        return ArtifactVerificationState(
            artifact_name="task",
            expected_path=task_path,
            status=ARTIFACT_STATE_UNREADABLE,
            reason_codes=("task-unreadable",),
        )

    task_digest = _sha256_hex(task_text.encode("utf-8"))
    lines = task_text.splitlines()

    expected_markers = {
        "task-heading": (
            "# AI Dev Generated Task: ",
            f"# AI Dev Generated Task: {expected_review_task_id(review_id)}",
        ),
        "task-id": ("- Task-ID: ", f"- Task-ID: {expected_review_task_id(review_id)}"),
        "task-type": ("- Task-Type: ", "- Task-Type: review"),
        "task-file": ("- Task-File: ", f"- Task-File: {task_path}"),
        "review-id": ("- Review-ID: ", f"- Review-ID: {review_id}"),
        "package-markdown-path": (
            "- Package-Markdown-Path: ",
            f"- Package-Markdown-Path: {expected_package_path}",
        ),
        "package-json-path": (
            "- Package-JSON-Path: ",
            "- Package-JSON-Path: .ai-dev/review/package.json",
        ),
        "changes-diff-path": (
            "- Changes-Diff-Path: ",
            "- Changes-Diff-Path: .ai-dev/review/changes.diff",
        ),
        "review-report-path": (
            "- Review-Report-Path: ",
            f"- Review-Report-Path: {expected_report_path}",
        ),
    }

    for marker_name, (prefix, expected_line) in expected_markers.items():
        marker_lines = [line for line in lines if line.startswith(prefix)]
        if not marker_lines:
            return ArtifactVerificationState(
                artifact_name="task",
                expected_path=task_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=(f"task-missing-{marker_name}",),
                actual_digest_sha256=task_digest,
            )
        if len(marker_lines) > 1:
            return ArtifactVerificationState(
                artifact_name="task",
                expected_path=task_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=(f"task-duplicate-{marker_name}",),
                actual_digest_sha256=task_digest,
            )
        if marker_lines[0] != expected_line:
            return ArtifactVerificationState(
                artifact_name="task",
                expected_path=task_path,
                status=ARTIFACT_STATE_INVALID,
                reason_codes=(f"task-mismatch-{marker_name}",),
                actual_digest_sha256=task_digest,
            )

    return ArtifactVerificationState(
        artifact_name="task",
        expected_path=task_path,
        status=ARTIFACT_STATE_VALID,
        reason_codes=(),
        actual_digest_sha256=task_digest,
    )


def _report_lines(repo_root: Path, expected_path: str) -> tuple[str | None, str | None, list[str] | None]:
    absolute = repo_root / expected_path
    if not absolute.exists():
        return REPORT_STATE_MISSING, "Review report file is missing.", None
    if not absolute.is_file():
        return REPORT_STATE_NOT_REGULAR, "Review report path is not a regular file.", None

    try:
        text = absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return REPORT_STATE_UNREADABLE, "Review report is not valid UTF-8.", None
    except OSError as exc:
        return REPORT_STATE_UNREADABLE, f"Review report cannot be read: {exc}", None

    if not text.strip():
        return REPORT_STATE_EMPTY, "Review report is empty.", None

    return None, None, text.splitlines()


def _first_meaningful_line(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _single_prefixed_value(lines: list[str], prefix: str) -> tuple[str | None, bool]:
    values = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        return None, len(values) > 1
    return values[0], False


def _decision_from_lines(lines: list[str]) -> tuple[str | None, str | None]:
    decisions: list[tuple[str, str]] = []
    current_h2: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if _LEVEL2_HEADING_PATTERN.match(line):
            current_h2 = line
            continue
        if line.startswith("- Status:"):
            decisions.append((current_h2 or "", line[len("- Status:") :].strip()))

    if not decisions:
        return None, REPORT_STATE_MISSING_DECISION

    outside_decision = [value for section, value in decisions if section != "## Decision"]
    if outside_decision:
        return outside_decision[0], REPORT_STATE_DECISION_OUTSIDE_SECTION
    if len(decisions) > 1:
        return decisions[0][1], REPORT_STATE_MULTIPLE_DECISIONS

    decision = decisions[0][1]
    if decision not in ALLOWED_DECISIONS:
        return decision, REPORT_STATE_INVALID_DECISION
    return decision, None


def _validate_required_sections(lines: list[str]) -> str | None:
    section_indices: dict[str, list[int]] = {heading: [] for heading in REQUIRED_REPORT_SECTIONS}
    for index, line in enumerate(lines):
        if line in section_indices:
            section_indices[line].append(index)

    for heading in REQUIRED_REPORT_SECTIONS:
        indices = section_indices[heading]
        if not indices:
            return REPORT_STATE_MISSING_SECTION
        if len(indices) > 1:
            return REPORT_STATE_DUPLICATE_SECTION

    ordered = [section_indices[heading][0] for heading in REQUIRED_REPORT_SECTIONS]
    if ordered != sorted(ordered):
        return REPORT_STATE_SECTION_ORDER_INVALID
    return None


def _verify_report(
    *,
    repo_root: Path,
    review_id: str,
    expected_report_path: str,
    expected_package_path: str,
) -> ReportVerificationState:
    status, message, lines = _report_lines(repo_root, expected_report_path)
    if lines is None:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=status or REPORT_STATE_UNREADABLE,
            reason_codes=(status or REPORT_STATE_UNREADABLE,),
            message=message or "Review report is unreadable.",
            decision_status=None,
        )

    first_line = _first_meaningful_line(lines)
    if first_line != "# AI Dev Review Report":
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_INVALID_HEADING,
            reason_codes=("invalid-report-heading",),
            message="Report must start with '# AI Dev Review Report'.",
            decision_status=None,
        )

    section_status = _validate_required_sections(lines)
    if section_status is not None:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=section_status,
            reason_codes=(section_status,),
            message="Report required sections are missing, duplicated, or out of order.",
            decision_status=None,
        )

    review_value, review_inconsistent = _single_prefixed_value(lines, "Review-ID: ")
    if review_value is None and not review_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_MISSING_REVIEW_ID,
            reason_codes=("missing-review-id",),
            message="Report is missing Review-ID marker.",
            decision_status=None,
        )
    if review_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_REVIEW_ID,
            reason_codes=("inconsistent-review-id",),
            message="Report contains inconsistent Review-ID markers.",
            decision_status=None,
        )
    if review_value != review_id:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_REVIEW_ID,
            reason_codes=("wrong-review-id",),
            message="Report Review-ID does not match requested review.",
            decision_status=None,
        )

    generator_value, generator_inconsistent = _single_prefixed_value(lines, "Generated-By: ")
    if generator_value is None and not generator_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_MISSING_GENERATOR,
            reason_codes=("missing-generated-by",),
            message="Report is missing Generated-By marker.",
            decision_status=None,
        )
    if generator_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_GENERATOR,
            reason_codes=("inconsistent-generated-by",),
            message="Report contains inconsistent Generated-By markers.",
            decision_status=None,
        )
    if generator_value != "external AI review":
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_GENERATOR,
            reason_codes=("wrong-generated-by",),
            message="Generated-By marker must be 'external AI review'.",
            decision_status=None,
        )

    package_value, package_inconsistent = _single_prefixed_value(lines, "Package-Path: ")
    if package_value is None and not package_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_MISSING_PACKAGE_PATH,
            reason_codes=("missing-package-path",),
            message="Report is missing Package-Path marker.",
            decision_status=None,
        )
    if package_inconsistent:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_PACKAGE_PATH,
            reason_codes=("inconsistent-package-path",),
            message="Report contains inconsistent Package-Path markers.",
            decision_status=None,
        )
    if package_value != expected_package_path:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_WRONG_PACKAGE_PATH,
            reason_codes=("wrong-package-path",),
            message="Package-Path marker does not match expected package markdown path.",
            decision_status=None,
        )

    decision, decision_status = _decision_from_lines(lines)
    if decision_status == REPORT_STATE_MISSING_DECISION:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_MISSING_DECISION,
            reason_codes=("missing-decision-status",),
            message="Report is missing decision status line '- Status: ...'.",
            decision_status=None,
        )
    if decision_status == REPORT_STATE_MULTIPLE_DECISIONS:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_MULTIPLE_DECISIONS,
            reason_codes=("duplicate-decision-status",),
            message="Report must contain exactly one decision status in the Decision section.",
            decision_status=None,
        )
    if decision_status == REPORT_STATE_DECISION_OUTSIDE_SECTION:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_DECISION_OUTSIDE_SECTION,
            reason_codes=("decision-status-outside-decision-section",),
            message="Decision status line must appear only inside the Decision section.",
            decision_status=None,
        )
    if decision_status == REPORT_STATE_INVALID_DECISION:
        return ReportVerificationState(
            expected_path=expected_report_path,
            status=REPORT_STATE_INVALID_DECISION,
            reason_codes=("invalid-decision-status",),
            message="Decision status must be one of: pass, pass-with-notes, blocked.",
            decision_status=None,
        )

    return ReportVerificationState(
        expected_path=expected_report_path,
        status=REPORT_STATE_VALID,
        reason_codes=(),
        message="Review report passed structural verification.",
        decision_status=decision,
    )


def _overall_status(
    *,
    package_state: ArtifactVerificationState,
    task_state: ArtifactVerificationState,
    report_state: ReportVerificationState,
) -> str:
    if package_state.status != ARTIFACT_STATE_VALID or task_state.status != ARTIFACT_STATE_VALID:
        return OVERALL_STATUS_INVALID
    if report_state.status in {
        REPORT_STATE_MISSING,
        REPORT_STATE_NOT_REGULAR,
        REPORT_STATE_UNREADABLE,
        REPORT_STATE_EMPTY,
    }:
        return OVERALL_STATUS_INCOMPLETE
    if report_state.status != REPORT_STATE_VALID:
        return OVERALL_STATUS_INVALID
    return OVERALL_STATUS_COMPLETE


def _recommended_next_action(
    *,
    package_state: ArtifactVerificationState,
    task_state: ArtifactVerificationState,
    report_state: ReportVerificationState,
) -> str:
    if package_state.status == ARTIFACT_STATE_MISSING:
        return "Regenerate the review package with `flow review` before verifying again."
    if package_state.status != ARTIFACT_STATE_VALID:
        return "Restore immutable package.md/package.json to their original digests before verifying again."

    if task_state.status == ARTIFACT_STATE_MISSING:
        return "Regenerate the review task with `flow review` before verifying again."
    if task_state.status != ARTIFACT_STATE_VALID:
        return "Restore the immutable review task markdown to its original digest before verifying again."

    if report_state.status == REPORT_STATE_MISSING:
        return "Run the generated review task and write the review report to the expected report path."
    if report_state.status in {REPORT_STATE_UNREADABLE, REPORT_STATE_NOT_REGULAR, REPORT_STATE_EMPTY}:
        return "Rewrite the review report as valid UTF-8 markdown at the expected report path."
    if report_state.status in {
        REPORT_STATE_INVALID_HEADING,
        REPORT_STATE_MISSING_SECTION,
        REPORT_STATE_DUPLICATE_SECTION,
        REPORT_STATE_SECTION_ORDER_INVALID,
        REPORT_STATE_MISSING_REVIEW_ID,
        REPORT_STATE_WRONG_REVIEW_ID,
        REPORT_STATE_MISSING_GENERATOR,
        REPORT_STATE_WRONG_GENERATOR,
        REPORT_STATE_MISSING_PACKAGE_PATH,
        REPORT_STATE_WRONG_PACKAGE_PATH,
        REPORT_STATE_MISSING_DECISION,
        REPORT_STATE_INVALID_DECISION,
        REPORT_STATE_MULTIPLE_DECISIONS,
        REPORT_STATE_DECISION_OUTSIDE_SECTION,
    }:
        return "Fix the report contract markers and decision section, then rerun `flow review-verify`."

    if report_state.decision_status == DECISION_BLOCKED:
        return "Address blocking findings before checkpoint or commit."

    return "Review verification is complete."


def verify_review(repo_root: Path, review_id: str) -> ReviewVerificationResult:
    normalized_review_id = validate_review_id(review_id)

    paths = expected_review_artifact_paths(repo_root, normalized_review_id)
    expected_task_file = expected_review_task_file(normalized_review_id)

    package_state = _verify_review_package(
        repo_root=repo_root,
        review_id=normalized_review_id,
        package_path=paths.package_markdown_relative_path,
        package_json_path=paths.package_json_relative_path,
        changes_diff_path=paths.changes_diff_relative_path,
    )

    task_state = _verify_review_task(
        repo_root=repo_root,
        review_id=normalized_review_id,
        task_path=expected_task_file,
        expected_package_path=paths.package_markdown_relative_path,
        expected_report_path=paths.canonical_report_relative_path,
    )

    report_state = _verify_report(
        repo_root=repo_root,
        review_id=normalized_review_id,
        expected_report_path=paths.canonical_report_relative_path,
        expected_package_path=paths.package_markdown_relative_path,
    )

    overall_status = _overall_status(
        package_state=package_state,
        task_state=task_state,
        report_state=report_state,
    )

    return ReviewVerificationResult(
        review_id=normalized_review_id,
        overall_status=overall_status,
        package_path=paths.package_markdown_relative_path,
        task_path=expected_task_file,
        report_path=paths.canonical_report_relative_path,
        package_state=package_state,
        task_state=task_state,
        report_state=report_state,
        review_decision=report_state.decision_status,
        recommended_next_action=_recommended_next_action(
            package_state=package_state,
            task_state=task_state,
            report_state=report_state,
        ),
    )


def verification_result_json(result: ReviewVerificationResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "review_id": result.review_id,
        "overall_status": result.overall_status,
        "review_decision": result.review_decision,
        "paths": {
            "package": result.package_path,
            "task": result.task_path,
            "report": result.report_path,
        },
        "package": {
            "status": result.package_state.status,
            "path": result.package_state.expected_path,
            "reason_codes": list(result.package_state.reason_codes),
            "expected_digest_sha256": result.package_state.expected_digest_sha256,
            "actual_digest_sha256": result.package_state.actual_digest_sha256,
        },
        "task": {
            "status": result.task_state.status,
            "path": result.task_state.expected_path,
            "reason_codes": list(result.task_state.reason_codes),
            "actual_digest_sha256": result.task_state.actual_digest_sha256,
        },
        "report": {
            "status": result.report_state.status,
            "path": result.report_state.expected_path,
            "reason_codes": list(result.report_state.reason_codes),
            "message": result.report_state.message,
            "review_decision": result.report_state.decision_status,
        },
        "recommended_next_action": result.recommended_next_action,
    }


def render_verification_markdown(result: ReviewVerificationResult) -> str:
    lines = [
        f"# Review Verification Report: {result.review_id}",
        "",
        "## Summary",
        "",
        f"- Review-ID: {result.review_id}",
        f"- Overall-Status: {result.overall_status}",
        f"- Review-Decision: {result.review_decision or '(unavailable)'}",
        f"- Package-Path: {result.package_path}",
        f"- Task-Path: {result.task_path}",
        f"- Report-Path: {result.report_path}",
        "",
        "## Package Integrity",
        "",
        f"- Status: {result.package_state.status}",
        f"- Path: {result.package_state.expected_path}",
        "- Reason-Codes: "
        + (", ".join(result.package_state.reason_codes) if result.package_state.reason_codes else "(none)"),
        f"- Expected-Digest-SHA256: {result.package_state.expected_digest_sha256 or '(unavailable)'}",
        f"- Actual-Digest-SHA256: {result.package_state.actual_digest_sha256 or '(unavailable)'}",
        "",
        "## Task Integrity",
        "",
        f"- Status: {result.task_state.status}",
        f"- Path: {result.task_state.expected_path}",
        "- Reason-Codes: "
        + (", ".join(result.task_state.reason_codes) if result.task_state.reason_codes else "(none)"),
        f"- Actual-Digest-SHA256: {result.task_state.actual_digest_sha256 or '(unavailable)'}",
        "",
        "## Report Verification",
        "",
        f"- Status: {result.report_state.status}",
        f"- Path: {result.report_state.expected_path}",
        "- Reason-Codes: "
        + (", ".join(result.report_state.reason_codes) if result.report_state.reason_codes else "(none)"),
        f"- Review-Decision: {result.report_state.decision_status or '(unavailable)'}",
        f"- Message: {result.report_state.message}",
        "",
        "## Recommended Next Action",
        "",
        result.recommended_next_action,
        "",
    ]
    return "\n".join(lines)


def write_verification_artifacts(
    *,
    repo_root: Path,
    result: ReviewVerificationResult,
) -> tuple[str, str]:
    markdown_relative_path = review_verification_markdown_path(result.review_id)
    json_relative_path = review_verification_json_path(result.review_id)

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
        raise ReviewVerificationError(str(exc)) from exc

    return markdown_relative_path, json_relative_path


def run_review_verification(
    *,
    repo_root: Path,
    review_id: str,
) -> tuple[ReviewVerificationResult, str, str]:
    try:
        normalized_review_id = validate_review_id(review_id)
    except ReviewManifestError as exc:
        raise ReviewVerificationError(str(exc)) from exc

    result = verify_review(repo_root, normalized_review_id)
    markdown_path, json_path = write_verification_artifacts(repo_root=repo_root, result=result)
    return result, markdown_path, json_path
