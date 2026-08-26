from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Protocol, Union
from urllib.parse import urlparse

from .json_files import JsonFileError, load_json_object, write_json_object_atomic
from .repository import primary_worktree_root
from .ticket_config import (
    GitHubCurrentTicketConfiguration,
    GitHubTicketConfiguration,
    LocalTicketConfiguration,
    TicketConfiguration,
)
from .tickets import (
    Ticket,
    TicketLifecycleState,
    TicketModelError,
    TicketReference,
    TicketWorkflowState,
    normalize_github_repository,
    normalize_ticket_data,
)


class TicketProviderError(Exception):
    """Raised for ticket provider boundary and resolution failures."""

class TicketProvider(Protocol):
    def create(
        self,
        *,
        title: str,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
    ) -> TicketReference: ...

    def get(self, ticket_id: str) -> Ticket: ...

    def query(
        self,
        *,
        lifecycle_state: TicketLifecycleState | None = None,
        workflow_state: TicketWorkflowState | None = None,
        labels: tuple[str, ...] = (),
        query_text: str | None = None,
    ) -> list[Ticket]: ...

    def update(
        self,
        reference: TicketReference,
        *,
        title: str | None = None,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] | None = None,
        labels: tuple[str, ...] | None = None,
    ) -> Ticket: ...

    def mark_active(self, reference: TicketReference) -> Ticket: ...

    def deactivate(
        self,
        reference: TicketReference,
        previous_labels: tuple[str, ...] = (),
    ) -> Ticket: ...

    def block(self, reference: TicketReference, reason: str) -> Ticket: ...

    def resume(self, reference: TicketReference) -> Ticket: ...

    def complete(self, reference: TicketReference) -> Ticket: ...


class GitHubCurrentRepositoryResolver(Protocol):
    def resolve_current_repository(self) -> str: ...


def _run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _now_utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local_tickets_directory_for_repo_root(repo_root: Path, configured_path: str) -> Path:
    """Resolve the local ticket catalogue for whichever worktree is asking.

    The catalogue is repository-level state, not per-worktree state, so a
    concurrent ticket workspace must read the same tickets as every other
    worktree. A store the asking worktree already holds stays authoritative for
    it -- that is the ordinary single-worktree case, and a tracked store
    genuinely does belong to each worktree -- otherwise the primary worktree's
    store is shared.
    """
    local_directory = repo_root / configured_path
    if local_directory.exists():
        return local_directory

    primary_root = primary_worktree_root(repo_root)
    if primary_root is None:
        return local_directory
    if Path(os.path.abspath(str(primary_root))) == Path(os.path.abspath(str(repo_root))):
        return local_directory

    primary_directory = primary_root / configured_path
    if primary_directory.is_dir():
        return primary_directory
    return local_directory


def _parse_positive_numeric_ticket_id(value: str, *, context: str) -> int:
    if not value.isdigit() or value.startswith("0"):
        raise TicketProviderError(
            f"Invalid {context}: ticket id must be a positive integer without leading zeros."
        )

    numeric = int(value)
    if numeric <= 0:
        raise TicketProviderError(
            f"Invalid {context}: ticket id must be a positive integer."
        )

    return numeric


def _ticket_file_path(tickets_directory: Path, ticket_id: str) -> Path:
    return tickets_directory / f"{ticket_id}.json"


def _write_json_object_atomic_no_replace(path: Path, data: dict[str, object]) -> bool:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.tmp-",
            delete=False,
            newline="\n",
        ) as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)

        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False

        return True
    except OSError as exc:
        raise TicketProviderError(f"Cannot write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _ticket_to_storage_payload(ticket: Ticket) -> dict[str, object]:
    payload: dict[str, object] = {
        "reference": ticket.reference.to_dict(),
        "title": ticket.title,
        "lifecycleState": ticket.lifecycle_state,
        "workflowState": ticket.workflow_state,
    }
    if ticket.body is not None:
        payload["body"] = ticket.body
    if ticket.acceptance_criteria:
        payload["acceptanceCriteria"] = list(ticket.acceptance_criteria)
    if ticket.labels:
        payload["labels"] = list(ticket.labels)
    if ticket.block_reason is not None:
        payload["blockReason"] = ticket.block_reason
    if ticket.created_at is not None:
        payload["createdAt"] = ticket.created_at
    if ticket.updated_at is not None:
        payload["updatedAt"] = ticket.updated_at
    if ticket.closed_at is not None:
        payload["closedAt"] = ticket.closed_at
    return payload


class LocalTicketProvider:
    def __init__(self, *, repo_root: Path, tickets_path: str) -> None:
        self._tickets_path = tickets_path
        self._tickets_directory = _local_tickets_directory_for_repo_root(repo_root, tickets_path)

    def _load_ticket_records(self) -> dict[str, Ticket]:
        if not self._tickets_directory.exists():
            return {}

        if not self._tickets_directory.is_dir():
            raise TicketProviderError(
                f"Invalid local tickets path: {self._tickets_directory} is not a directory."
            )

        loaded: dict[str, Ticket] = {}
        for path in sorted(self._tickets_directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            if path.suffix.lower() != ".json":
                continue

            if not path.is_file():
                raise TicketProviderError(
                    f"Invalid local ticket file entry: {path} is not a regular file."
                )

            file_stem = path.stem
            numeric_id = _parse_positive_numeric_ticket_id(
                file_stem,
                context=f"ticket filename {path.name}",
            )
            canonical_ticket_id = str(numeric_id)
            if file_stem != canonical_ticket_id:
                raise TicketProviderError(
                    f"Invalid ticket filename {path.name}: use canonical id {canonical_ticket_id}.json"
                )

            try:
                payload = load_json_object(path, missing_default={})
            except JsonFileError as exc:
                raise TicketProviderError(str(exc)) from exc

            if not payload:
                raise TicketProviderError(f"Invalid local ticket file {path}: expected a JSON object.")

            try:
                ticket = normalize_ticket_data(
                    payload,
                    context=f"local ticket file {path}",
                )
            except TicketModelError as exc:
                raise TicketProviderError(str(exc)) from exc

            if ticket.reference.provider != "local":
                raise TicketProviderError(
                    f"Invalid local ticket file {path}: ticket reference provider must be local."
                )

            if ticket.reference.ticket_id != canonical_ticket_id:
                raise TicketProviderError(
                    f"Invalid local ticket file {path}: ticketId must equal filename id {canonical_ticket_id}."
                )

            loaded[canonical_ticket_id] = ticket

        return loaded

    def _max_existing_ticket_id(self, tickets: dict[str, Ticket]) -> int:
        if not tickets:
            return 0
        return max(int(ticket_id) for ticket_id in tickets)

    def create(
        self,
        *,
        title: str,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
    ) -> TicketReference:
        normalized_title = title.strip()
        if not normalized_title:
            raise TicketProviderError("Ticket title cannot be empty.")

        tickets = self._load_ticket_records()
        ticket_id = str(self._max_existing_ticket_id(tickets) + 1)
        now = _now_utc_iso_timestamp()

        ticket = Ticket(
            reference=TicketReference(
                provider="local",
                ticket_id=ticket_id,
                path=self._tickets_path,
            ),
            title=normalized_title,
            body=body,
            acceptance_criteria=acceptance_criteria,
            labels=labels,
            lifecycle_state="open",
            workflow_state="inactive",
            block_reason=None,
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        payload = _ticket_to_storage_payload(ticket)
        destination_path = _ticket_file_path(self._tickets_directory, ticket_id)
        wrote = _write_json_object_atomic_no_replace(destination_path, payload)
        if not wrote:
            raise TicketProviderError(
                f"Cannot create local ticket {ticket_id}: {destination_path} already exists."
            )
        return ticket.reference

    def get(self, ticket_id: str) -> Ticket:
        normalized_id = ticket_id.strip()
        if not normalized_id:
            raise TicketProviderError("ticket_id cannot be empty.")

        _parse_positive_numeric_ticket_id(normalized_id, context="ticket id")

        tickets = self._load_ticket_records()
        ticket = tickets.get(normalized_id)
        if ticket is not None:
            return ticket

        raise TicketProviderError(f"Local ticket not found: {normalized_id}")

    def query(
        self,
        *,
        lifecycle_state: TicketLifecycleState | None = None,
        workflow_state: TicketWorkflowState | None = None,
        labels: tuple[str, ...] = (),
        query_text: str | None = None,
    ) -> list[Ticket]:
        effective_lifecycle = lifecycle_state if lifecycle_state is not None else "open"
        normalized_labels = tuple(label.strip() for label in labels if label.strip())
        normalized_query = query_text.strip().lower() if isinstance(query_text, str) else ""

        matched: list[Ticket] = []
        for ticket in self._load_ticket_records().values():
            if ticket.lifecycle_state != effective_lifecycle:
                continue
            if workflow_state is not None and ticket.workflow_state != workflow_state:
                continue
            if normalized_labels and not all(label in ticket.labels for label in normalized_labels):
                continue
            if normalized_query:
                haystack_parts = [ticket.title, ticket.body or "", *ticket.acceptance_criteria]
                haystack = "\n".join(part for part in haystack_parts if part).lower()
                if normalized_query not in haystack:
                    continue
            matched.append(ticket)

        matched.sort(key=lambda item: int(item.reference.ticket_id))
        return matched

    def _write_ticket(self, ticket: Ticket) -> None:
        destination_path = _ticket_file_path(
            self._tickets_directory,
            ticket.reference.ticket_id,
        )
        try:
            write_json_object_atomic(destination_path, _ticket_to_storage_payload(ticket))
        except JsonFileError as exc:
            raise TicketProviderError(str(exc)) from exc

    def _require_local_reference(self, reference: TicketReference) -> Ticket:
        if reference.provider != "local":
            raise TicketProviderError("Local ticket provider requires local ticket references.")
        if reference.path != self._tickets_path:
            raise TicketProviderError(
                "Local ticket provider requires a reference bound to the same ticket store."
            )

        ticket = self.get(reference.ticket_id)
        if ticket.reference.provider != "local":
            raise TicketProviderError("Local ticket provider loaded a non-local ticket reference.")
        if ticket.reference.path != self._tickets_path:
            raise TicketProviderError(
                "Local ticket provider loaded a ticket reference for a different ticket store."
            )
        return ticket

    def update(
        self,
        reference: TicketReference,
        *,
        title: str | None = None,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] | None = None,
        labels: tuple[str, ...] | None = None,
    ) -> Ticket:
        ticket = self._require_local_reference(reference)
        now = _now_utc_iso_timestamp()

        updated = Ticket(
            reference=ticket.reference,
            title=title if isinstance(title, str) else ticket.title,
            body=body if body is not None else ticket.body,
            acceptance_criteria=(
                acceptance_criteria if acceptance_criteria is not None else ticket.acceptance_criteria
            ),
            labels=labels if labels is not None else ticket.labels,
            lifecycle_state=ticket.lifecycle_state,
            workflow_state=ticket.workflow_state,
            block_reason=ticket.block_reason,
            created_at=ticket.created_at,
            updated_at=now,
            closed_at=ticket.closed_at,
        )
        self._write_ticket(updated)
        return updated

    def mark_active(self, reference: TicketReference) -> Ticket:
        ticket = self._require_local_reference(reference)
        now = _now_utc_iso_timestamp()
        updated = Ticket(
            reference=ticket.reference,
            title=ticket.title,
            body=ticket.body,
            acceptance_criteria=ticket.acceptance_criteria,
            labels=ticket.labels,
            lifecycle_state=ticket.lifecycle_state,
            workflow_state="active",
            block_reason=None,
            created_at=ticket.created_at,
            updated_at=now,
            closed_at=ticket.closed_at,
        )
        self._write_ticket(updated)
        return updated

    def deactivate(
        self,
        reference: TicketReference,
        previous_labels: tuple[str, ...] = (),
    ) -> Ticket:
        ticket = self._require_local_reference(reference)
        now = _now_utc_iso_timestamp()
        workflow_state = "inactive"
        if "active" in previous_labels:
            workflow_state = "active"
        elif "blocked" in previous_labels:
            workflow_state = "blocked"
        updated = Ticket(
            reference=ticket.reference,
            title=ticket.title,
            body=ticket.body,
            acceptance_criteria=ticket.acceptance_criteria,
            labels=previous_labels,
            lifecycle_state=ticket.lifecycle_state,
            workflow_state=workflow_state,
            block_reason=None,
            created_at=ticket.created_at,
            updated_at=now,
            closed_at=ticket.closed_at,
        )
        self._write_ticket(updated)
        return updated

    def block(self, reference: TicketReference, reason: str) -> Ticket:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise TicketProviderError("Block reason cannot be empty.")

        ticket = self._require_local_reference(reference)
        now = _now_utc_iso_timestamp()
        updated = Ticket(
            reference=ticket.reference,
            title=ticket.title,
            body=ticket.body,
            acceptance_criteria=ticket.acceptance_criteria,
            labels=ticket.labels,
            lifecycle_state=ticket.lifecycle_state,
            workflow_state="blocked",
            block_reason=normalized_reason,
            created_at=ticket.created_at,
            updated_at=now,
            closed_at=ticket.closed_at,
        )
        self._write_ticket(updated)
        return updated

    def resume(self, reference: TicketReference) -> Ticket:
        return self.mark_active(reference)

    def complete(self, reference: TicketReference) -> Ticket:
        ticket = self._require_local_reference(reference)
        now = _now_utc_iso_timestamp()
        updated = Ticket(
            reference=ticket.reference,
            title=ticket.title,
            body=ticket.body,
            acceptance_criteria=ticket.acceptance_criteria,
            labels=ticket.labels,
            lifecycle_state="closed",
            workflow_state="inactive",
            block_reason=None,
            created_at=ticket.created_at,
            updated_at=now,
            closed_at=now,
        )
        self._write_ticket(updated)
        return updated


def _extract_first_http_url(output: str) -> str | None:
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("https://") or text.startswith("http://"):
            return text
    return None


def _extract_issue_number_from_url(url: str) -> str:
    parsed = urlparse(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 4 or segments[-2] != "issues":
        raise TicketProviderError(
            f"Invalid GitHub issue URL from gh output: {url}"
        )
    ticket_id = segments[-1]
    _parse_positive_numeric_ticket_id(ticket_id, context="GitHub issue url")
    return ticket_id


_ACCEPTANCE_CRITERIA_HEADER = "## Acceptance criteria"


def _is_heading_of_level_at_most_two(line: str) -> bool:
    text = line.lstrip()
    if not text.startswith("#"):
        return False

    level = 0
    for character in text:
        if character == "#":
            level += 1
            continue
        break

    if level == 0 or level > 2:
        return False

    if len(text) == level:
        return True

    return text[level].isspace()


def _is_acceptance_criteria_heading(line: str) -> bool:
    return line.strip().lower() == _ACCEPTANCE_CRITERIA_HEADER.lower()


def _parse_acceptance_checkbox_line(line: str) -> str | None:
    candidate = line.strip()
    if candidate.startswith("- [ ] "):
        value = candidate[6:].strip()
        return value or None
    if candidate.startswith("- [x] ") or candidate.startswith("- [X] "):
        value = candidate[6:].strip()
        return value or None
    return None


def _render_github_issue_body(
    *,
    body: str | None,
    acceptance_criteria: tuple[str, ...],
) -> str | None:
    normalized_body = body.strip() if isinstance(body, str) else ""
    normalized_criteria = tuple(item.strip() for item in acceptance_criteria if item.strip())

    if not normalized_body and not normalized_criteria:
        return None

    criteria_block = ""
    if normalized_criteria:
        bullet_lines = "\n".join(f"- [ ] {item}" for item in normalized_criteria)
        criteria_block = f"{_ACCEPTANCE_CRITERIA_HEADER}\n{bullet_lines}"

    if normalized_body and criteria_block:
        return f"{normalized_body}\n\n{criteria_block}"
    if normalized_body:
        return normalized_body
    return criteria_block


def _parse_github_issue_body(body: str | None) -> tuple[str | None, tuple[str, ...]]:
    if not isinstance(body, str):
        return None, ()

    text = body.strip()
    if not text:
        return None, ()

    lines = text.splitlines()

    section_start = -1
    for index, line in enumerate(lines):
        if _is_acceptance_criteria_heading(line):
            section_start = index
            break

    if section_start < 0:
        return text, ()

    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        if _is_heading_of_level_at_most_two(lines[index]):
            section_end = index
            break

    criteria: list[str] = []
    for line in lines[section_start + 1:section_end]:
        item = _parse_acceptance_checkbox_line(line)
        if item:
            criteria.append(item)

    body_without_section_lines = [
        line
        for index, line in enumerate(lines)
        if index < section_start or index >= section_end
    ]

    trimmed_start = 0
    trimmed_end = len(body_without_section_lines)
    while trimmed_start < trimmed_end and not body_without_section_lines[trimmed_start].strip():
        trimmed_start += 1
    while trimmed_end > trimmed_start and not body_without_section_lines[trimmed_end - 1].strip():
        trimmed_end -= 1

    normalized_body = "\n".join(body_without_section_lines[trimmed_start:trimmed_end]).strip()
    return (normalized_body or None), tuple(criteria)


def _normalize_github_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()

    labels: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        normalized = name.strip()
        if normalized:
            labels.append(normalized)
    return tuple(labels)


def _workflow_state_from_github_labels(labels: tuple[str, ...]) -> TicketWorkflowState:
    label_set = set(labels)
    if "blocked" in label_set:
        return "blocked"
    if "active" in label_set:
        return "active"
    return "inactive"


def _normalize_github_lifecycle_state(value: object, *, context: str) -> TicketLifecycleState:
    if not isinstance(value, str):
        raise TicketProviderError(f"Invalid {context}: missing GitHub issue state.")

    normalized = value.strip().lower()
    if normalized in {"open", "closed"}:
        return normalized

    raise TicketProviderError(
        f"Invalid {context}: unsupported GitHub issue state {value!r}."
    )


def _normalize_github_issue_ticket(
    payload: object,
    *,
    repository: str,
    context: str,
) -> Ticket:
    if not isinstance(payload, dict):
        raise TicketProviderError(f"Invalid {context}: expected a JSON object.")

    number = payload.get("number")
    if not isinstance(number, int) or number <= 0:
        raise TicketProviderError(f"Invalid {context}: missing positive numeric issue number.")

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TicketProviderError(f"Invalid {context}: missing title.")

    url_value = payload.get("url")
    url = url_value.strip() if isinstance(url_value, str) else None

    lifecycle_state = _normalize_github_lifecycle_state(payload.get("state"), context=context)
    labels = _normalize_github_labels(payload.get("labels"))
    workflow_state = _workflow_state_from_github_labels(labels)
    body, acceptance_criteria = _parse_github_issue_body(payload.get("body"))

    try:
        return Ticket(
            reference=TicketReference(
                provider="github",
                ticket_id=str(number),
                repository=repository,
                url=url,
            ),
            title=title,
            body=body,
            acceptance_criteria=acceptance_criteria,
            labels=labels,
            lifecycle_state=lifecycle_state,
            workflow_state=workflow_state,
            block_reason=None,
            created_at=payload.get("createdAt") if isinstance(payload.get("createdAt"), str) else None,
            updated_at=payload.get("updatedAt") if isinstance(payload.get("updatedAt"), str) else None,
            closed_at=payload.get("closedAt") if isinstance(payload.get("closedAt"), str) else None,
        )
    except TicketModelError as exc:
        raise TicketProviderError(str(exc)) from exc


_GITHUB_GRAPHQL_ISSUES_QUERY = """
query($owner: String!, $repo: String!, $state: IssueState!, $after: String) {
  repository(owner: $owner, name: $repo) {
        issues(first: 100, states: [$state], after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        url
        state
        body
        createdAt
        updatedAt
        closedAt
        labels(first: 100) {
          nodes {
            name
          }
        }
      }
    }
  }
}
""".strip()


def _graphql_state_for_lifecycle(state: TicketLifecycleState) -> str:
    if state == "open":
        return "OPEN"
    if state == "closed":
        return "CLOSED"
    raise TicketProviderError(f"Unsupported lifecycle state for GitHub query: {state}")


def _normalize_graphql_issue_node(node: object) -> dict[str, object]:
    if not isinstance(node, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: issue node must be an object.")

    labels_payload = node.get("labels")
    labels_nodes: list[dict[str, object]] = []
    if isinstance(labels_payload, dict):
        nodes = labels_payload.get("nodes")
        if isinstance(nodes, list):
            labels_nodes = [item for item in nodes if isinstance(item, dict)]

    normalized: dict[str, object] = {
        "number": node.get("number"),
        "title": node.get("title"),
        "url": node.get("url"),
        "state": node.get("state"),
        "body": node.get("body"),
        "createdAt": node.get("createdAt"),
        "updatedAt": node.get("updatedAt"),
        "closedAt": node.get("closedAt"),
        "labels": labels_nodes,
    }
    return normalized


def _extract_graphql_issues_page(payload: object) -> tuple[list[dict[str, object]], bool, str | None]:
    if not isinstance(payload, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: expected a JSON object.")

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first_error = errors[0]
        if isinstance(first_error, dict):
            message = first_error.get("message")
            if isinstance(message, str) and message.strip():
                raise TicketProviderError(f"GitHub GraphQL query failed: {message.strip()}")
        raise TicketProviderError("GitHub GraphQL query failed.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: missing data object.")

    repository = data.get("repository")
    if repository is None:
        raise TicketProviderError("GitHub GraphQL query returned no repository data.")
    if not isinstance(repository, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: repository must be an object.")

    issues = repository.get("issues")
    if not isinstance(issues, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: issues must be an object.")

    nodes = issues.get("nodes")
    if not isinstance(nodes, list):
        raise TicketProviderError("Invalid GitHub GraphQL response: issues.nodes must be a list.")

    page_info = issues.get("pageInfo")
    if not isinstance(page_info, dict):
        raise TicketProviderError("Invalid GitHub GraphQL response: issues.pageInfo must be an object.")

    has_next_page = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")

    if not isinstance(has_next_page, bool):
        raise TicketProviderError("Invalid GitHub GraphQL response: pageInfo.hasNextPage must be a boolean.")

    if end_cursor is not None and not isinstance(end_cursor, str):
        raise TicketProviderError("Invalid GitHub GraphQL response: pageInfo.endCursor must be a string or null.")

    if has_next_page and (end_cursor is None or not end_cursor.strip()):
        raise TicketProviderError("Invalid GitHub GraphQL response: missing pagination cursor.")

    normalized_nodes = [_normalize_graphql_issue_node(item) for item in nodes]
    return normalized_nodes, has_next_page, end_cursor


def _resolve_github_repository_from_remote_url(remote_url: str) -> str:
    normalized = remote_url.strip()
    if not normalized:
        raise TicketProviderError("Cannot resolve github-current repository: remote URL is empty.")

    path = ""
    if normalized.startswith("git@github.com:"):
        path = normalized.split(":", 1)[1]
    elif normalized.startswith("ssh://"):
        parsed = urlparse(normalized)
        if parsed.hostname != "github.com":
            raise TicketProviderError(
                "Cannot resolve github-current repository: origin remote is not a github.com URL."
            )
        path = parsed.path.lstrip("/")
    else:
        parsed = urlparse(normalized)
        if parsed.scheme in {"http", "https"} and parsed.hostname == "github.com":
            path = parsed.path.lstrip("/")

    if not path:
        raise TicketProviderError(
            "Cannot resolve github-current repository: origin remote is not a supported GitHub URL."
        )

    if path.endswith(".git"):
        path = path[:-4]

    try:
        return normalize_github_repository(path, context="github-current repository")
    except TicketModelError as exc:
        raise TicketProviderError(str(exc)) from exc


@dataclass(frozen=True)
class GitRemoteGitHubCurrentRepositoryResolver:
    repo_root: Path

    def resolve_current_repository(self) -> str:
        completed = _run_command(
            ["git", "-C", str(self.repo_root), "remote", "get-url", "origin"]
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            if not message:
                message = "origin remote is not configured"
            raise TicketProviderError(
                f"Cannot resolve github-current repository from current project: {message}"
            )

        return _resolve_github_repository_from_remote_url(completed.stdout)


class GitHubTicketProvider:
    def __init__(self, *, binding: GitHubTicketProviderBinding) -> None:
        self._binding = binding

    def _resolve_repository(self) -> str:
        reference = self._binding.resolve_reference("1")
        assert reference.repository is not None
        return reference.repository

    def _run_gh(self, arguments: list[str], *, action: str) -> subprocess.CompletedProcess[str]:
        try:
            return _run_command(arguments)
        except FileNotFoundError as exc:
            raise TicketProviderError(
                "GitHub CLI (gh) is required for ticket commands."
            ) from exc

    def create(
        self,
        *,
        title: str,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
    ) -> TicketReference:
        normalized_title = title.strip()
        if not normalized_title:
            raise TicketProviderError("Ticket title cannot be empty.")

        repository = self._resolve_repository()
        arguments = [
            "gh",
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            normalized_title,
        ]

        rendered_body = _render_github_issue_body(
            body=body,
            acceptance_criteria=acceptance_criteria,
        )
        if rendered_body is not None:
            arguments.extend(["--body", rendered_body])

        normalized_labels = [label.strip() for label in labels if label.strip()]
        if normalized_labels:
            arguments.extend(["--label", ",".join(normalized_labels)])

        completed = self._run_gh(arguments, action="create")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise TicketProviderError(
                f"GitHub ticket create failed for {repository}: {message}"
            )

        issue_url = _extract_first_http_url(completed.stdout)
        if issue_url is None:
            raise TicketProviderError(
                f"GitHub ticket create failed for {repository}: missing issue URL in gh output."
            )

        issue_number = _extract_issue_number_from_url(issue_url)
        return self._binding.resolve_reference(issue_number, url=issue_url)

    def get(self, ticket_id: str) -> Ticket:
        normalized_id = ticket_id.strip()
        _parse_positive_numeric_ticket_id(normalized_id, context="ticket id")

        repository = self._resolve_repository()
        completed = self._run_gh(
            [
                "gh",
                "issue",
                "view",
                normalized_id,
                "--repo",
                repository,
                "--json",
                "number,title,url,state,body,labels,createdAt,updatedAt,closedAt",
            ],
            action="show",
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise TicketProviderError(
                f"GitHub ticket lookup failed for {repository}#{normalized_id}: {message}"
            )

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TicketProviderError(
                f"GitHub ticket lookup failed for {repository}#{normalized_id}: invalid JSON response."
            ) from exc

        return _normalize_github_issue_ticket(
            payload,
            repository=repository,
            context=f"GitHub issue {repository}#{normalized_id}",
        )

    def query(
        self,
        *,
        lifecycle_state: TicketLifecycleState | None = None,
        workflow_state: TicketWorkflowState | None = None,
        labels: tuple[str, ...] = (),
        query_text: str | None = None,
    ) -> list[Ticket]:
        repository = self._resolve_repository()
        state = lifecycle_state if lifecycle_state is not None else "open"
        owner, repo_name = repository.split("/", 1)
        graphql_state = _graphql_state_for_lifecycle(state)

        issue_payloads: list[dict[str, object]] = []
        cursor_argument = "null"

        while True:
            completed = self._run_gh(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={_GITHUB_GRAPHQL_ISSUES_QUERY}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo_name}",
                    "-F",
                    f"state={graphql_state}",
                    "-F",
                    f"after={cursor_argument}",
                ],
                action="query",
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise TicketProviderError(
                    f"GitHub ticket query failed for {repository}: {message}"
                )

            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise TicketProviderError(
                    f"GitHub ticket query failed for {repository}: invalid JSON response."
                ) from exc

            page_nodes, has_next_page, end_cursor = _extract_graphql_issues_page(payload)
            issue_payloads.extend(page_nodes)

            if not has_next_page:
                break

            assert end_cursor is not None
            cursor_argument = end_cursor

        normalized_labels = tuple(label.strip() for label in labels if label.strip())
        normalized_query = query_text.strip().lower() if isinstance(query_text, str) else ""

        tickets: list[Ticket] = []
        for index, item in enumerate(issue_payloads):
            ticket = _normalize_github_issue_ticket(
                item,
                repository=repository,
                context=f"GitHub issue list item {index} for {repository}",
            )
            if workflow_state is not None and ticket.workflow_state != workflow_state:
                continue
            if normalized_labels and not all(label in ticket.labels for label in normalized_labels):
                continue
            if normalized_query:
                haystack_parts = [ticket.title, ticket.body or "", *ticket.acceptance_criteria]
                haystack = "\n".join(part for part in haystack_parts if part).lower()
                if normalized_query not in haystack:
                    continue
            tickets.append(ticket)

        tickets.sort(key=lambda item: int(item.reference.ticket_id))
        return tickets

    def _reference_repository(self, reference: TicketReference) -> str:
        if reference.provider != "github" or not reference.repository:
            raise TicketProviderError("GitHub ticket provider requires github ticket references.")
        return normalize_github_repository(reference.repository, context="ticket reference")

    def _reconcile_workflow_label(
        self,
        *,
        repository: str,
        issue_number: str,
        target_label: str,
        labels: tuple[str, ...],
    ) -> None:
        workflow_labels = ["active", "blocked", "backlog"]
        labels_to_remove = [
            label
            for label in workflow_labels
            if label != target_label and label in labels
        ]
        add_needed = target_label not in labels

        if not add_needed and not labels_to_remove:
            return

        arguments = [
            "gh",
            "issue",
            "edit",
            issue_number,
            "--repo",
            repository,
        ]
        if add_needed:
            arguments.extend(["--add-label", target_label])
        if labels_to_remove:
            arguments.extend(["--remove-label", ",".join(labels_to_remove)])

        completed = self._run_gh(arguments, action=f"mark {target_label}")
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise TicketProviderError(
                f"GitHub ticket transition failed for {repository}#{issue_number}: {message}"
            )

    def _remove_workflow_membership_labels(
        self,
        *,
        repository: str,
        issue_number: str,
        labels: tuple[str, ...],
    ) -> None:
        labels_to_remove = [
            label
            for label in ("active", "blocked", "backlog")
            if label in labels
        ]
        if not labels_to_remove:
            return

        completed = self._run_gh(
            [
                "gh",
                "issue",
                "edit",
                issue_number,
                "--repo",
                repository,
                "--remove-label",
                ",".join(labels_to_remove),
            ],
            action="complete-remove-workflow-labels",
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise TicketProviderError(
                f"GitHub ticket complete failed for {repository}#{issue_number}: failed to remove workflow labels: {message}"
            )

    def update(
        self,
        reference: TicketReference,
        *,
        title: str | None = None,
        body: str | None = None,
        acceptance_criteria: tuple[str, ...] | None = None,
        labels: tuple[str, ...] | None = None,
    ) -> Ticket:
        raise TicketProviderError("GitHub ticket update is not implemented.")

    def mark_active(self, reference: TicketReference) -> Ticket:
        repository = self._reference_repository(reference)
        ticket = self.get(reference.ticket_id)
        self._reconcile_workflow_label(
            repository=repository,
            issue_number=reference.ticket_id,
            target_label="active",
            labels=ticket.labels,
        )
        return self.get(reference.ticket_id)

    def deactivate(
        self,
        reference: TicketReference,
        previous_labels: tuple[str, ...] = (),
    ) -> Ticket:
        repository = self._reference_repository(reference)
        if "active" in previous_labels:
            target_label = "active"
        elif "blocked" in previous_labels:
            target_label = "blocked"
        elif "backlog" in previous_labels:
            target_label = "backlog"
        else:
            self._remove_workflow_membership_labels(
                repository=repository,
                issue_number=reference.ticket_id,
                labels=self.get(reference.ticket_id).labels,
            )
            return self.get(reference.ticket_id)
        self._reconcile_workflow_label(
            repository=repository,
            issue_number=reference.ticket_id,
            target_label=target_label,
            labels=self.get(reference.ticket_id).labels,
        )
        return self.get(reference.ticket_id)

    def block(self, reference: TicketReference, reason: str) -> Ticket:
        repository = self._reference_repository(reference)
        ticket = self.get(reference.ticket_id)
        self._reconcile_workflow_label(
            repository=repository,
            issue_number=reference.ticket_id,
            target_label="blocked",
            labels=ticket.labels,
        )
        return self.get(reference.ticket_id)

    def resume(self, reference: TicketReference) -> Ticket:
        return self.mark_active(reference)

    def complete(self, reference: TicketReference) -> Ticket:
        repository = self._reference_repository(reference)
        ticket = self.get(reference.ticket_id)

        if ticket.lifecycle_state != "closed":
            completed = self._run_gh(
                [
                    "gh",
                    "issue",
                    "close",
                    reference.ticket_id,
                    "--repo",
                    repository,
                ],
                action="complete",
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise TicketProviderError(
                    f"GitHub ticket complete failed for {repository}#{reference.ticket_id}: {message}"
                )

            ticket = self.get(reference.ticket_id)

        if ticket.lifecycle_state != "closed":
            raise TicketProviderError(
                f"GitHub ticket complete failed for {repository}#{reference.ticket_id}: ticket is not closed after completion."
            )

        self._remove_workflow_membership_labels(
            repository=repository,
            issue_number=reference.ticket_id,
            labels=ticket.labels,
        )
        closed_ticket = self.get(reference.ticket_id)

        if closed_ticket.lifecycle_state != "closed":
            raise TicketProviderError(
                f"GitHub ticket complete failed for {repository}#{reference.ticket_id}: ticket is not closed after completion."
            )

        if closed_ticket.workflow_state != "inactive":
            raise TicketProviderError(
                f"GitHub ticket complete failed for {repository}#{reference.ticket_id}: ticket workflow state is not inactive after completion."
            )

        return closed_ticket


@dataclass(frozen=True)
class DeferredGitHubCurrentRepositoryResolver:
    def resolve_current_repository(self) -> str:
        raise TicketProviderError(
            "github-current repository resolution is not available."
        )


@dataclass(frozen=True)
class LocalTicketProviderBinding:
    tickets_path: str


@dataclass(frozen=True)
class GitHubTicketProviderBinding:
    repository: str | None
    repository_mode: str
    repository_resolver: GitHubCurrentRepositoryResolver | None = None

    def resolve_reference(self, ticket_id: str, *, url: str | None = None) -> TicketReference:
        normalized_id = ticket_id.strip()
        if not normalized_id:
            raise TicketProviderError("ticket_id cannot be empty.")

        repository = self.repository
        if repository is None:
            if self.repository_resolver is None:
                raise TicketProviderError(
                    "github-current requires repository resolution before ticket references can be used."
                )
            repository = normalize_github_repository(
                self.repository_resolver.resolve_current_repository(),
                context="github-current repository",
            )

        return TicketReference(
            provider="github",
            ticket_id=normalized_id,
            repository=repository,
            url=url,
        )


ResolvedTicketProvider = Union[LocalTicketProviderBinding, GitHubTicketProviderBinding]


def instantiate_local_ticket_provider(
    *,
    repo_root: Path,
    binding: LocalTicketProviderBinding,
) -> LocalTicketProvider:
    return LocalTicketProvider(repo_root=repo_root, tickets_path=binding.tickets_path)


def resolve_ticket_provider_for_reference(
    *,
    repo_root: Path,
    reference: TicketReference,
) -> TicketProvider:
    if reference.provider == "github":
        if reference.repository is None:
            raise TicketProviderError(
                "GitHub ticket reference is missing repository."
            )

        return GitHubTicketProvider(
            binding=GitHubTicketProviderBinding(
                repository=reference.repository,
                repository_mode="explicit",
                repository_resolver=None,
            )
        )

    if reference.provider == "local":
        if reference.path is None:
            raise TicketProviderError(
                "Local ticket reference is missing ticket store path."
            )

        return instantiate_local_ticket_provider(
            repo_root=repo_root,
            binding=LocalTicketProviderBinding(tickets_path=reference.path),
        )

    raise TicketProviderError(f"Unsupported ticket reference provider: {reference.provider}")


def instantiate_ticket_provider(
    *,
    repo_root: Path,
    config: TicketConfiguration,
    github_current_resolver: GitHubCurrentRepositoryResolver | None = None,
) -> TicketProvider:
    resolved = resolve_ticket_provider(
        config,
        github_current_resolver=github_current_resolver,
    )
    if isinstance(resolved, LocalTicketProviderBinding):
        return instantiate_local_ticket_provider(repo_root=repo_root, binding=resolved)

    if isinstance(resolved, GitHubTicketProviderBinding):
        resolver = resolved.repository_resolver
        if resolved.repository_mode == "current" and (
            resolver is None or isinstance(resolver, DeferredGitHubCurrentRepositoryResolver)
        ):
            resolver = GitRemoteGitHubCurrentRepositoryResolver(repo_root=repo_root)

        return GitHubTicketProvider(
            binding=GitHubTicketProviderBinding(
                repository=resolved.repository,
                repository_mode=resolved.repository_mode,
                repository_resolver=resolver,
            )
        )

    raise TicketProviderError("Unsupported ticket provider binding.")


def resolve_ticket_provider(
    config: TicketConfiguration,
    *,
    github_current_resolver: GitHubCurrentRepositoryResolver | None = None,
) -> ResolvedTicketProvider:
    if isinstance(config, LocalTicketConfiguration):
        return LocalTicketProviderBinding(
            tickets_path=config.path,
        )

    if isinstance(config, GitHubTicketConfiguration):
        return GitHubTicketProviderBinding(
            repository=config.repository,
            repository_mode="explicit",
            repository_resolver=None,
        )

    if isinstance(config, GitHubCurrentTicketConfiguration):
        resolver = github_current_resolver or DeferredGitHubCurrentRepositoryResolver()
        return GitHubTicketProviderBinding(
            repository=None,
            repository_mode="current",
            repository_resolver=resolver,
        )

    raise TicketProviderError(f"Unsupported ticket provider configuration: {config!r}")
