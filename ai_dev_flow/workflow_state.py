from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .json_files import JsonFileError, load_json_object, write_json_object_atomic
from .tickets import (
    TicketModelError,
    TicketReference,
    normalize_ticket_reference_data,
)


DEFAULT_MAIN_BRANCH = "main"
DEFAULT_SCRATCH_BRANCH = "scratch"
DEFAULT_CHECKPOINT = 0

ALLOWED_KEYS = frozenset(
    {
        "activeIssueNumber",
        "activeIssueTitle",
        "activeIssueUrl",
        "ticket",
        "patchDescription",
        "mainBranch",
        "scratchBranch",
        "checkpoint",
    }
)


class WorkflowStateError(Exception):
    """Raised for invalid workflow state."""


@dataclass(frozen=True)
class WorkflowState:
    main_branch: str = DEFAULT_MAIN_BRANCH
    scratch_branch: str = DEFAULT_SCRATCH_BRANCH
    checkpoint: int = DEFAULT_CHECKPOINT
    active_issue_number: int | None = None
    active_issue_title: str | None = None
    active_issue_url: str | None = None
    ticket_reference: TicketReference | None = None
    patch_description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mainBranch": self.main_branch,
            "scratchBranch": self.scratch_branch,
            "checkpoint": self.checkpoint,
        }

        if self.active_issue_number is not None:
            payload["activeIssueNumber"] = self.active_issue_number

        if self.active_issue_title is not None:
            payload["activeIssueTitle"] = self.active_issue_title

        if self.active_issue_url is not None:
            payload["activeIssueUrl"] = self.active_issue_url

        if self.ticket_reference is not None:
            payload["ticket"] = self.ticket_reference.to_dict()

        if self.patch_description is not None:
            payload["patchDescription"] = self.patch_description

        return payload


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_and_validate(
    state_data: dict[str, Any],
    *,
    context: str,
) -> WorkflowState:
    unknown_keys = sorted(name for name in state_data if name not in ALLOWED_KEYS)
    if unknown_keys:
        raise WorkflowStateError(
            f"Unknown workflow state key(s) in {context}: "
            f"{', '.join(unknown_keys)}"
        )

    main_branch_value = state_data.get("mainBranch", DEFAULT_MAIN_BRANCH)
    if not isinstance(main_branch_value, str):
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "mainBranch must be a string."
        )

    main_branch = main_branch_value.strip()
    if not main_branch:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "mainBranch cannot be empty."
        )

    scratch_branch_value = state_data.get(
        "scratchBranch",
        DEFAULT_SCRATCH_BRANCH,
    )
    if not isinstance(scratch_branch_value, str):
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "scratchBranch must be a string."
        )

    scratch_branch = scratch_branch_value.strip()
    if not scratch_branch:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "scratchBranch cannot be empty."
        )

    checkpoint = state_data.get("checkpoint", DEFAULT_CHECKPOINT)
    if not _is_int(checkpoint) or checkpoint < 0:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "checkpoint must be a non-negative integer."
        )

    has_issue_number = "activeIssueNumber" in state_data
    has_issue_title = "activeIssueTitle" in state_data
    has_issue_url = "activeIssueUrl" in state_data
    has_ticket_reference = "ticket" in state_data
    has_patch_description = "patchDescription" in state_data

    if has_issue_number and has_patch_description:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "activeIssueNumber and patchDescription cannot both be set."
        )

    if has_ticket_reference and has_patch_description:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "ticket and patchDescription cannot both be set."
        )

    active_issue_number: int | None = None
    if has_issue_number:
        issue_number = state_data["activeIssueNumber"]
        if not _is_int(issue_number) or issue_number <= 0:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueNumber must be a positive integer."
            )

        active_issue_number = issue_number

    active_issue_title: str | None = None
    if has_issue_title:
        issue_title_value = state_data["activeIssueTitle"]

        if not isinstance(issue_title_value, str):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueTitle must be a string."
            )

        issue_title = issue_title_value.strip()
        if not issue_title:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueTitle cannot be empty."
            )

        active_issue_title = issue_title

    if has_issue_title and not has_issue_number:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "activeIssueTitle requires activeIssueNumber."
        )

    active_issue_url: str | None = None
    if has_issue_url:
        issue_url_value = state_data["activeIssueUrl"]

        if not isinstance(issue_url_value, str):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueUrl must be a string."
            )

        issue_url = issue_url_value.strip()
        if not issue_url:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueUrl cannot be empty."
            )

        from urllib.parse import urlparse

        parsed_url = urlparse(issue_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "activeIssueUrl must be a valid HTTP(S) URL."
            )

        active_issue_url = issue_url

    if has_issue_url and not has_issue_number:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: "
            "activeIssueUrl requires activeIssueNumber."
        )

    ticket_reference: TicketReference | None = None
    if has_ticket_reference:
        reference_data = state_data["ticket"]
        if not isinstance(reference_data, dict):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "ticket must be an object."
            )

        try:
            ticket_reference = normalize_ticket_reference_data(
                reference_data,
                context=f"workflow ticket in {context}",
            )
        except TicketModelError as exc:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: {exc}"
            ) from exc

    if ticket_reference is not None and active_issue_number is not None:
        if ticket_reference.ticket_id != str(active_issue_number):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "ticket.ticketId must match activeIssueNumber when both are set."
            )

    patch_description: str | None = None
    if has_patch_description:
        patch_description_value = state_data["patchDescription"]

        if not isinstance(patch_description_value, str):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "patchDescription must be a string."
            )

        description = patch_description_value.strip()
        if not description:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: "
                "patchDescription cannot be empty."
            )

        patch_description = description

    return WorkflowState(
        main_branch=main_branch,
        scratch_branch=scratch_branch,
        checkpoint=checkpoint,
        active_issue_number=active_issue_number,
        active_issue_title=active_issue_title,
        active_issue_url=active_issue_url,
        ticket_reference=ticket_reference,
        patch_description=patch_description,
    )


def load_state(path: Path) -> WorkflowState:
    try:
        data = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        message = str(exc).replace(
            f"Invalid configuration in {path}: expected a JSON object.",
            f"Invalid workflow state in {path}: expected a JSON object.",
        )
        raise WorkflowStateError(message) from exc

    return normalize_and_validate(data, context=str(path))


def save_state(path: Path, state: WorkflowState) -> None:
    try:
        write_json_object_atomic(path, state.to_dict())
    except JsonFileError as exc:
        message = str(exc)
        prefix = f"Cannot write {path}:"
        if message.startswith(prefix):
            message = message.replace(
                prefix,
                f"Cannot write workflow state to {path}:",
                1,
            )

        raise WorkflowStateError(message) from exc


def clear_state(path: Path) -> WorkflowState:
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise WorkflowStateError(
                f"Cannot clear workflow state at {path}: {exc}"
            ) from exc

    return WorkflowState()
