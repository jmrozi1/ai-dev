from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


class BlockedWorkflowsError(Exception):
    """Raised for blocked workflow persistence and validation errors."""


@dataclass(frozen=True)
class BlockedWorkflowRecord:
    issue_number: int
    issue_title: str
    issue_url: str
    reason: str
    blocked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issueNumber": self.issue_number,
            "issueTitle": self.issue_title,
            "issueUrl": self.issue_url,
            "reason": self.reason,
            "blockedAt": self.blocked_at,
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_record(record_data: dict[str, Any], *, context: str) -> BlockedWorkflowRecord:
    required = ["issueNumber", "issueTitle", "issueUrl", "reason", "blockedAt"]
    missing = [name for name in required if name not in record_data]
    if missing:
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: missing required key(s): {', '.join(missing)}"
        )

    issue_number = record_data["issueNumber"]
    if not _is_int(issue_number) or issue_number <= 0:
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: issueNumber must be a positive integer."
        )

    issue_title = record_data["issueTitle"]
    if not isinstance(issue_title, str) or not issue_title.strip():
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: issueTitle cannot be empty."
        )

    issue_url = record_data["issueUrl"]
    if not isinstance(issue_url, str) or not issue_url.strip():
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: issueUrl cannot be empty."
        )
    parsed = urlparse(issue_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: issueUrl must be a valid HTTP(S) URL."
        )

    reason = record_data["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: reason cannot be empty."
        )

    blocked_at = record_data["blockedAt"]
    if not isinstance(blocked_at, str) or not blocked_at.strip():
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: blockedAt cannot be empty."
        )

    iso_value = blocked_at.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise BlockedWorkflowsError(
            f"Invalid blocked workflow record in {context}: blockedAt must be an ISO-8601 timestamp."
        ) from exc

    return BlockedWorkflowRecord(
        issue_number=issue_number,
        issue_title=issue_title.strip(),
        issue_url=issue_url.strip(),
        reason=reason.strip(),
        blocked_at=blocked_at.strip(),
    )


def _normalize_document(data: dict[str, Any], *, context: str) -> list[BlockedWorkflowRecord]:
    unknown_keys = sorted(name for name in data if name != "blockedWorkflows")
    if unknown_keys:
        raise BlockedWorkflowsError(
            f"Unknown blocked workflow key(s) in {context}: {', '.join(unknown_keys)}"
        )

    records = data.get("blockedWorkflows", [])
    if not isinstance(records, list):
        raise BlockedWorkflowsError(
            f"Invalid blocked workflows in {context}: blockedWorkflows must be an array."
        )

    normalized: list[BlockedWorkflowRecord] = []
    seen: set[int] = set()
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            raise BlockedWorkflowsError(
                f"Invalid blocked workflow record in {context} record #{index + 1}: expected a JSON object."
            )

        record = _normalize_record(item, context=f"{context} record #{index + 1}")
        if record.issue_number in seen:
            raise BlockedWorkflowsError(
                f"Invalid blocked workflows in {context}: duplicate issueNumber {record.issue_number}."
            )
        seen.add(record.issue_number)
        normalized.append(record)

    return normalized


def _to_document(records: list[BlockedWorkflowRecord]) -> dict[str, Any]:
    return {"blockedWorkflows": [item.to_dict() for item in records]}


def load_blocked_workflows(path: Path) -> list[BlockedWorkflowRecord]:
    try:
        data = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise BlockedWorkflowsError(str(exc)) from exc

    return _normalize_document(data, context=str(path))


def save_blocked_workflows(path: Path, records: list[BlockedWorkflowRecord]) -> None:
    document = _to_document(records)
    try:
        write_json_object_atomic(path, document)
    except JsonFileError as exc:
        raise BlockedWorkflowsError(str(exc)) from exc


def upsert_blocked_workflow(path: Path, record: BlockedWorkflowRecord) -> None:
    records = load_blocked_workflows(path)
    updated: list[BlockedWorkflowRecord] = []
    replaced = False
    for item in records:
        if item.issue_number == record.issue_number:
            updated.append(record)
            replaced = True
        else:
            updated.append(item)

    if not replaced:
        updated.append(record)

    save_blocked_workflows(path, updated)


def remove_blocked_workflow(path: Path, issue_number: int) -> None:
    records = load_blocked_workflows(path)
    updated = [item for item in records if item.issue_number != issue_number]
    save_blocked_workflows(path, updated)


def get_blocked_workflow(path: Path, issue_number: int) -> BlockedWorkflowRecord | None:
    for item in load_blocked_workflows(path):
        if item.issue_number == issue_number:
            return item
    return None


def format_blocked_summary_lines(path: Path) -> list[str]:
    records = sorted(load_blocked_workflows(path), key=lambda item: item.issue_number)
    if not records:
        return ["  none"]

    lines: list[str] = []
    for item in records:
        if item.issue_title:
            lines.append(f"  issue {item.issue_number} - {item.issue_title}")
        else:
            lines.append(f"  issue {item.issue_number}")
        lines.append(f"    reason: {item.reason}")

    return lines
