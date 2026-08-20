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
        "stackedHandoff",
        "stackedResume",
    }
)


class WorkflowStateError(Exception):
    """Raised for invalid workflow state."""


@dataclass(frozen=True)
class StackedHandoff:
    relationship: str
    prerequisite_for_issue_number: int
    inherited_base_commit: str
    inherited_base_tree: str
    suspended_issue_number: int
    suspended_issue_title: str
    suspended_issue_url: str | None
    suspended_ticket_reference: TicketReference
    suspended_checkpoint: int
    suspended_commit: str
    suspended_tree: str
    suspended_base_commit: str
    suspended_ref_name: str

    def to_dict(self) -> dict[str, Any]:
        suspended_issue: dict[str, Any] = {
            "issueNumber": self.suspended_issue_number,
            "issueTitle": self.suspended_issue_title,
            "ticket": self.suspended_ticket_reference.to_dict(),
            "checkpoint": self.suspended_checkpoint,
            "commit": self.suspended_commit,
            "tree": self.suspended_tree,
            "baseCommit": self.suspended_base_commit,
            "refName": self.suspended_ref_name,
        }
        if self.suspended_issue_url is not None:
            suspended_issue["issueUrl"] = self.suspended_issue_url
        return {
            "relationship": self.relationship,
            "prerequisiteForIssueNumber": self.prerequisite_for_issue_number,
            "inheritedBase": {
                "commit": self.inherited_base_commit,
                "tree": self.inherited_base_tree,
            },
            "suspendedIssue": suspended_issue,
        }


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
    stacked_handoff: StackedHandoff | None = None
    stacked_resume: dict[str, Any] | None = None

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

        if self.stacked_handoff is not None:
            payload["stackedHandoff"] = self.stacked_handoff.to_dict()

        if self.stacked_resume is not None:
            payload["stackedResume"] = self.stacked_resume

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
    has_stacked_handoff = "stackedHandoff" in state_data
    has_stacked_resume = "stackedResume" in state_data

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

    if has_stacked_handoff and not has_issue_number:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: stackedHandoff requires activeIssueNumber."
        )
    if has_stacked_handoff and has_patch_description:
        raise WorkflowStateError(
            f"Invalid workflow state in {context}: stackedHandoff cannot be used with patchDescription."
        )
    stacked_resume: dict[str, Any] | None = None
    if has_stacked_resume:
        raw_resume = state_data["stackedResume"]
        if not isinstance(raw_resume, dict):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedResume must be an object."
            )
        required_resume = {
            "suspendedIssueNumber",
            "promotedMainCommit",
            "suspendedCommit",
            "suspendedRefName",
            "checkpoint",
        }
        if set(raw_resume) != required_resume:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedResume has malformed fields."
            )
        if (
            not _is_int(raw_resume["suspendedIssueNumber"])
            or raw_resume["suspendedIssueNumber"] <= 0
            or not _is_int(raw_resume["checkpoint"])
            or raw_resume["checkpoint"] < 0
            or any(
                not isinstance(raw_resume[key], str) or not raw_resume[key].strip()
                for key in ("promotedMainCommit", "suspendedCommit", "suspendedRefName")
            )
        ):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedResume contains invalid values."
            )
        stacked_resume = dict(raw_resume)

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

    stacked_handoff: StackedHandoff | None = None
    if has_stacked_handoff:
        handoff_data = state_data["stackedHandoff"]
        if not isinstance(handoff_data, dict):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff must be an object."
            )
        unknown_handoff_keys = sorted(
            name for name in handoff_data
            if name not in {"relationship", "prerequisiteForIssueNumber", "inheritedBase", "suspendedIssue"}
        )
        if unknown_handoff_keys:
            raise WorkflowStateError(
                f"Unknown stacked handoff key(s) in {context}: {', '.join(unknown_handoff_keys)}"
            )
        if handoff_data.get("relationship") != "prerequisite":
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff relationship must be prerequisite."
            )
        prerequisite_for = handoff_data.get("prerequisiteForIssueNumber")
        if not _is_int(prerequisite_for) or prerequisite_for <= 0:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff prerequisiteForIssueNumber must be a positive integer."
            )
        inherited_base = handoff_data.get("inheritedBase")
        suspended_issue = handoff_data.get("suspendedIssue")
        if not isinstance(inherited_base, dict) or not isinstance(suspended_issue, dict):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff inheritedBase and suspendedIssue must be objects."
            )
        if set(inherited_base) != {"commit", "tree"}:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff inheritedBase must contain commit and tree."
            )
        for name in ("commit", "tree"):
            if not isinstance(inherited_base[name], str) or not inherited_base[name].strip():
                raise WorkflowStateError(
                    f"Invalid workflow state in {context}: stackedHandoff inheritedBase {name} must be a non-empty string."
                )
        required_suspended = {"issueNumber", "issueTitle", "ticket", "checkpoint", "commit", "tree", "baseCommit", "refName"}
        if set(suspended_issue) - (required_suspended | {"issueUrl"}) or not required_suspended.issubset(suspended_issue):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspendedIssue has malformed fields."
            )
        suspended_number = suspended_issue["issueNumber"]
        suspended_title = suspended_issue["issueTitle"]
        suspended_checkpoint = suspended_issue["checkpoint"]
        if not _is_int(suspended_number) or suspended_number <= 0 or not isinstance(suspended_title, str) or not suspended_title.strip() or not _is_int(suspended_checkpoint) or suspended_checkpoint < 0:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspendedIssue identity or checkpoint is invalid."
            )
        try:
            suspended_ticket = normalize_ticket_reference_data(
                suspended_issue["ticket"],
                context=f"stacked handoff suspended ticket in {context}",
            )
        except (TicketModelError, TypeError) as exc:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended ticket is invalid."
            ) from exc
        if suspended_ticket.ticket_id != str(suspended_number):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended ticket must match issueNumber."
            )
        strings = [suspended_issue[name] for name in ("commit", "tree", "baseCommit")]
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended commit identities must be non-empty strings."
            )
        ref_name = suspended_issue["refName"]
        if not isinstance(ref_name, str) or not ref_name.startswith("refs/ai-dev/suspended/"):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended refName is invalid."
            )
        if ref_name != f"refs/ai-dev/suspended/{suspended_number}":
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended refName must match issueNumber."
            )
        suspended_url = suspended_issue.get("issueUrl")
        if suspended_url is not None and (not isinstance(suspended_url, str) or not suspended_url.strip()):
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff suspended issueUrl must be a non-empty string."
            )
        if suspended_number == active_issue_number:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff issues must be distinct."
            )
        if prerequisite_for != suspended_number:
            raise WorkflowStateError(
                f"Invalid workflow state in {context}: stackedHandoff prerequisiteForIssueNumber must match suspendedIssue.issueNumber."
            )
        stacked_handoff = StackedHandoff(
            relationship="prerequisite",
            prerequisite_for_issue_number=prerequisite_for,
            inherited_base_commit=inherited_base["commit"].strip(),
            inherited_base_tree=inherited_base["tree"].strip(),
            suspended_issue_number=suspended_number,
            suspended_issue_title=suspended_title.strip(),
            suspended_issue_url=suspended_url.strip() if isinstance(suspended_url, str) else None,
            suspended_ticket_reference=suspended_ticket,
            suspended_checkpoint=suspended_checkpoint,
            suspended_commit=suspended_issue["commit"].strip(),
            suspended_tree=suspended_issue["tree"].strip(),
            suspended_base_commit=suspended_issue["baseCommit"].strip(),
            suspended_ref_name=ref_name,
        )

    return WorkflowState(
        main_branch=main_branch,
        scratch_branch=scratch_branch,
        checkpoint=checkpoint,
        active_issue_number=active_issue_number,
        active_issue_title=active_issue_title,
        active_issue_url=active_issue_url,
        ticket_reference=ticket_reference,
        patch_description=patch_description,
        stacked_handoff=stacked_handoff,
        stacked_resume=stacked_resume,
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
