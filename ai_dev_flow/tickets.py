from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse


TicketProviderName = Literal["local", "github"]
TicketLifecycleState = Literal["open", "closed"]
TicketWorkflowState = Literal["inactive", "active", "blocked"]


class TicketModelError(Exception):
    """Raised for normalized ticket model and reference validation failures."""


def normalize_github_repository(value: str, *, context: str) -> str:
    repository = value.strip()
    if not repository:
        raise TicketModelError(f"Invalid {context}: repository cannot be empty.")

    if repository.count("/") != 1:
        raise TicketModelError(
            f"Invalid {context}: repository must use owner/repo format."
        )

    owner, repo = repository.split("/", 1)
    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if not owner or not repo:
        raise TicketModelError(
            f"Invalid {context}: repository must use owner/repo format."
        )

    if any(character not in allowed_chars for character in owner):
        raise TicketModelError(
            f"Invalid {context}: repository owner contains unsupported characters."
        )
    if any(character not in allowed_chars for character in repo):
        raise TicketModelError(
            f"Invalid {context}: repository name contains unsupported characters."
        )

    return f"{owner}/{repo}"


def normalize_repository_relative_ticket_path(value: str, *, context: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise TicketModelError(f"Invalid {context}: path cannot be empty.")

    normalized = PurePosixPath(candidate)
    if normalized.is_absolute():
        raise TicketModelError(
            f"Invalid {context}: path must be repository-relative, not absolute."
        )

    parts = normalized.parts
    if not parts:
        raise TicketModelError(f"Invalid {context}: path cannot be empty.")

    if any(part == ".." for part in parts):
        raise TicketModelError(
            f"Invalid {context}: path cannot traverse parent directories."
        )

    if any(part.strip() == "" for part in parts):
        raise TicketModelError(f"Invalid {context}: path contains empty segments.")

    normalized_text = normalized.as_posix()
    if normalized_text in {".", ""}:
        raise TicketModelError(f"Invalid {context}: path cannot resolve to repository root.")

    return normalized_text


def _normalize_ticket_identifier(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise TicketModelError(f"Invalid {context}: ticketId must be a string.")

    ticket_id = value.strip()
    if not ticket_id:
        raise TicketModelError(f"Invalid {context}: ticketId cannot be empty.")

    return ticket_id


def _normalize_optional_url(value: Any, *, context: str) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise TicketModelError(f"Invalid {context}: url must be a string when provided.")

    url = value.strip()
    if not url:
        raise TicketModelError(f"Invalid {context}: url cannot be empty when provided.")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise TicketModelError(f"Invalid {context}: url must be a valid HTTP(S) URL.")

    return url


@dataclass(frozen=True)
class TicketReference:
    provider: TicketProviderName
    ticket_id: str
    repository: str | None = None
    url: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip() if isinstance(self.provider, str) else self.provider
        if provider not in {"local", "github"}:
            raise TicketModelError(
                f"Invalid ticket reference: unsupported provider '{self.provider}'."
            )

        ticket_id = _normalize_ticket_identifier(self.ticket_id, context="ticket reference")

        repository: str | None = None
        path: str | None = None
        if provider == "github":
            if not isinstance(self.repository, str):
                raise TicketModelError(
                    "Invalid ticket reference: github ticket reference requires repository."
                )
            repository = normalize_github_repository(self.repository, context="ticket reference")
            if self.path is not None:
                raise TicketModelError(
                    "Invalid ticket reference: github ticket reference does not allow path."
                )
        elif self.repository is not None:
            raise TicketModelError(
                "Invalid ticket reference: local ticket reference does not allow repository."
            )
        elif self.path is not None:
            path = normalize_repository_relative_ticket_path(self.path, context="ticket reference")

        url = _normalize_optional_url(self.url, context="ticket reference")

        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "ticket_id", ticket_id)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "path", path)

    def to_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {
            "provider": self.provider,
            "ticketId": self.ticket_id,
        }
        if self.repository is not None:
            payload["repository"] = self.repository
        if self.url is not None:
            payload["url"] = self.url
        if self.path is not None:
            payload["path"] = self.path
        return payload


def normalize_ticket_reference_data(
    reference_data: dict[str, Any],
    *,
    context: str,
) -> TicketReference:
    if not isinstance(reference_data, dict):
        raise TicketModelError(f"Invalid {context}: ticket reference must be an object.")

    provider_value = reference_data.get("provider")
    if not isinstance(provider_value, str) or not provider_value.strip():
        raise TicketModelError(f"Invalid {context}: provider is required.")

    provider = provider_value.strip()
    if provider not in {"local", "github"}:
        raise TicketModelError(f"Invalid {context}: unsupported provider '{provider}'.")

    ticket_id = _normalize_ticket_identifier(reference_data.get("ticketId"), context=context)
    repository_value = reference_data.get("repository")
    repository: str | None = None
    path_value = reference_data.get("path")
    path: str | None = None

    if provider == "github":
        if not isinstance(repository_value, str):
            raise TicketModelError(
                f"Invalid {context}: github ticket reference requires repository."
            )
        repository = normalize_github_repository(repository_value, context=context)
        if path_value is not None:
            raise TicketModelError(
                f"Invalid {context}: github ticket reference does not allow path."
            )
    else:
        if repository_value is not None:
            raise TicketModelError(
                f"Invalid {context}: local ticket reference does not allow repository."
            )
        if path_value is not None:
            if not isinstance(path_value, str):
                raise TicketModelError(
                    f"Invalid {context}: path must be a string when provided."
                )
            path = normalize_repository_relative_ticket_path(path_value, context=context)

    url = _normalize_optional_url(reference_data.get("url"), context=context)

    unknown_keys = sorted(
        key
        for key in reference_data
        if key not in {"provider", "ticketId", "repository", "url", "path"}
    )
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise TicketModelError(f"Invalid {context}: unknown ticket reference key(s): {joined}")

    return TicketReference(
        provider=provider,
        ticket_id=ticket_id,
        repository=repository,
        url=url,
        path=path,
    )


def _normalize_optional_string(value: Any, *, field_name: str, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TicketModelError(f"Invalid {context}: {field_name} must be a string.")
    text = value.strip()
    if not text:
        return None
    return text


def _normalize_string_list(value: Any, *, field_name: str, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TicketModelError(f"Invalid {context}: {field_name} must be a list.")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TicketModelError(
                f"Invalid {context}: {field_name} entries must be strings."
            )
        text = item.strip()
        if text:
            normalized.append(text)
    return tuple(normalized)


@dataclass(frozen=True)
class Ticket:
    reference: TicketReference
    title: str
    body: str | None
    acceptance_criteria: tuple[str, ...]
    labels: tuple[str, ...]
    lifecycle_state: TicketLifecycleState
    workflow_state: TicketWorkflowState
    block_reason: str | None
    created_at: str | None
    updated_at: str | None
    closed_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, TicketReference):
            raise TicketModelError("Invalid ticket: reference must be a TicketReference.")

        title = _normalize_optional_string(self.title, field_name="title", context="ticket")
        if title is None:
            raise TicketModelError("Invalid ticket: title is required.")

        if self.lifecycle_state not in {"open", "closed"}:
            raise TicketModelError(
                "Invalid ticket: lifecycle_state must be 'open' or 'closed'."
            )

        if self.workflow_state not in {"inactive", "active", "blocked"}:
            raise TicketModelError(
                "Invalid ticket: workflow_state must be inactive, active, or blocked."
            )

        acceptance_criteria = _normalize_string_list(
            list(self.acceptance_criteria),
            field_name="acceptance_criteria",
            context="ticket",
        )
        labels = _normalize_string_list(
            list(self.labels),
            field_name="labels",
            context="ticket",
        )

        object.__setattr__(self, "title", title)
        object.__setattr__(
            self,
            "body",
            _normalize_optional_string(self.body, field_name="body", context="ticket"),
        )
        object.__setattr__(self, "acceptance_criteria", acceptance_criteria)
        object.__setattr__(self, "labels", labels)
        object.__setattr__(
            self,
            "block_reason",
            _normalize_optional_string(
                self.block_reason,
                field_name="block_reason",
                context="ticket",
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            _normalize_optional_string(self.created_at, field_name="created_at", context="ticket"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _normalize_optional_string(self.updated_at, field_name="updated_at", context="ticket"),
        )
        object.__setattr__(
            self,
            "closed_at",
            _normalize_optional_string(self.closed_at, field_name="closed_at", context="ticket"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reference": self.reference.to_dict(),
            "title": self.title,
            "lifecycleState": self.lifecycle_state,
            "workflowState": self.workflow_state,
        }
        if self.body is not None:
            payload["body"] = self.body
        if self.acceptance_criteria:
            payload["acceptanceCriteria"] = list(self.acceptance_criteria)
        if self.labels:
            payload["labels"] = list(self.labels)
        if self.block_reason is not None:
            payload["blockReason"] = self.block_reason
        if self.created_at is not None:
            payload["createdAt"] = self.created_at
        if self.updated_at is not None:
            payload["updatedAt"] = self.updated_at
        if self.closed_at is not None:
            payload["closedAt"] = self.closed_at
        return payload


def normalize_ticket_data(ticket_data: dict[str, Any], *, context: str) -> Ticket:
    if not isinstance(ticket_data, dict):
        raise TicketModelError(f"Invalid {context}: ticket must be an object.")

    reference_data = ticket_data.get("reference")
    reference = normalize_ticket_reference_data(reference_data, context=f"{context}.reference")

    title_value = ticket_data.get("title")
    if not isinstance(title_value, str) or not title_value.strip():
        raise TicketModelError(f"Invalid {context}: title is required.")
    title = title_value.strip()

    lifecycle_value = ticket_data.get("lifecycleState")
    if lifecycle_value not in {"open", "closed"}:
        raise TicketModelError(
            f"Invalid {context}: lifecycleState must be 'open' or 'closed'."
        )

    workflow_value = ticket_data.get("workflowState")
    if workflow_value not in {"inactive", "active", "blocked"}:
        raise TicketModelError(
            f"Invalid {context}: workflowState must be inactive, active, or blocked."
        )

    return Ticket(
        reference=reference,
        title=title,
        body=_normalize_optional_string(ticket_data.get("body"), field_name="body", context=context),
        acceptance_criteria=_normalize_string_list(
            ticket_data.get("acceptanceCriteria"),
            field_name="acceptanceCriteria",
            context=context,
        ),
        labels=_normalize_string_list(ticket_data.get("labels"), field_name="labels", context=context),
        lifecycle_state=lifecycle_value,
        workflow_state=workflow_value,
        block_reason=_normalize_optional_string(
            ticket_data.get("blockReason"),
            field_name="blockReason",
            context=context,
        ),
        created_at=_normalize_optional_string(
            ticket_data.get("createdAt"),
            field_name="createdAt",
            context=context,
        ),
        updated_at=_normalize_optional_string(
            ticket_data.get("updatedAt"),
            field_name="updatedAt",
            context=context,
        ),
        closed_at=_normalize_optional_string(
            ticket_data.get("closedAt"),
            field_name="closedAt",
            context=context,
        ),
    )
