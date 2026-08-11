from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from .json_files import JsonFileError, load_json_object
from .repository import config_file_for_repo_root
from .tickets import (
    TicketModelError,
    normalize_github_repository,
    normalize_repository_relative_ticket_path,
)


class TicketConfigError(Exception):
    """Raised when repository ticket configuration is missing or invalid."""


@dataclass(frozen=True)
class LocalTicketConfiguration:
    provider: str
    path: str


@dataclass(frozen=True)
class GitHubTicketConfiguration:
    provider: str
    repository: str


@dataclass(frozen=True)
class GitHubCurrentTicketConfiguration:
    provider: str


TicketConfiguration = Union[
    LocalTicketConfiguration,
    GitHubTicketConfiguration,
    GitHubCurrentTicketConfiguration,
]


def _invalid(message: str, *, path: Path) -> TicketConfigError:
    return TicketConfigError(f"Invalid ticket configuration in {path}: {message}")


def _normalize_tickets_block(path: Path, payload: dict[str, Any]) -> TicketConfiguration:
    tickets_data = payload.get("tickets")
    if not isinstance(tickets_data, dict):
        raise _invalid("missing required object: tickets", path=path)

    provider_value = tickets_data.get("provider")
    if not isinstance(provider_value, str) or not provider_value.strip():
        raise _invalid("tickets.provider is required", path=path)

    provider = provider_value.strip()

    if provider == "local":
        raw_path = tickets_data.get("path")
        if not isinstance(raw_path, str):
            raise _invalid("local provider requires tickets.path", path=path)
        try:
            local_path = normalize_repository_relative_ticket_path(
                raw_path,
                context="tickets.path",
            )
        except TicketModelError as exc:
            raise _invalid(str(exc), path=path) from exc

        if "repository" in tickets_data:
            raise _invalid("local provider does not allow tickets.repository", path=path)

        return LocalTicketConfiguration(provider="local", path=local_path)

    if provider == "github":
        repository_value = tickets_data.get("repository")
        if not isinstance(repository_value, str):
            raise _invalid("github provider requires tickets.repository", path=path)
        try:
            repository = normalize_github_repository(
                repository_value,
                context="tickets.repository",
            )
        except TicketModelError as exc:
            raise _invalid(str(exc), path=path) from exc

        if "path" in tickets_data:
            raise _invalid("github provider does not allow tickets.path", path=path)

        return GitHubTicketConfiguration(provider="github", repository=repository)

    if provider == "github-current":
        if "repository" in tickets_data:
            raise _invalid(
                "github-current provider does not allow tickets.repository",
                path=path,
            )
        if "path" in tickets_data:
            raise _invalid("github-current provider does not allow tickets.path", path=path)

        return GitHubCurrentTicketConfiguration(provider="github-current")

    raise _invalid(f"unsupported provider '{provider}'", path=path)


def load_ticket_configuration(path: Path) -> TicketConfiguration:
    try:
        payload = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise TicketConfigError(str(exc)) from exc

    if not payload:
        raise TicketConfigError(f"Missing ticket configuration file: {path}")

    return _normalize_tickets_block(path, payload)


def load_ticket_configuration_for_repo_root(repo_root: Path) -> TicketConfiguration:
    return load_ticket_configuration(config_file_for_repo_root(repo_root))
