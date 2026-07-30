from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml


ALIAS_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALIASES_SECTION_KEY = "aliases"

# Public top-level commands supported by the flow CLI.
SUPPORTED_ALIAS_TARGETS = frozenset(
    {
        "start",
        "patch",
        "task-prepare",
        "summarize",
        "summarize-verify",
        "review-verify",
        "status",
        "review",
        "commit",
        "reset",
        "promote",
        "complete",
        "block",
        "resume",
        "config",
        "get",
        "set",
        "unset",
        "showreport",
        "help",
    }
)

_REJECTED_NAME_CHARACTERS = set(" \t\n\r/\\\"'`$&|;<>*?[]{}()")
_RESERVED_ALIAS_NAMES = frozenset({"ai-dev", "aidev", "ai_dev"})


class AliasConfigError(Exception):
    """Raised for invalid user alias configuration."""


@dataclass(frozen=True)
class DesiredAliasState:
    aliases: dict[str, str]



def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AliasConfigError(f"Cannot read AI Dev config {path}: {exc}") from exc

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise AliasConfigError(f"Invalid YAML in AI Dev config {path}: {exc}") from exc

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise AliasConfigError(
            f"Invalid AI Dev config in {path} at <root>: expected mapping for alias lookup."
        )

    return loaded


def _is_valid_alias_name(name: str) -> bool:
    return ALIAS_NAME_PATTERN.match(name) is not None


def _validate_alias_name(name: str, *, path: Path, field_path: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name must be non-empty."
        )

    if normalized in _RESERVED_ALIAS_NAMES:
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name {normalized!r} is reserved."
        )

    if any(character in _REJECTED_NAME_CHARACTERS for character in normalized):
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name contains unsupported characters."
        )

    if not _is_valid_alias_name(normalized):
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name must match {ALIAS_NAME_PATTERN.pattern}."
        )

    if normalized == "ai-dev":
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name cannot be 'ai-dev'."
        )

    return normalized


def _validate_alias_target(target: str, *, path: Path, field_path: str) -> str:
    normalized = target.strip()
    if not normalized:
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias target must be non-empty."
        )

    if any(character.isspace() for character in normalized):
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias target must be exactly one command name."
        )

    if any(character in "\"'`$&|;<>" for character in normalized):
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: alias target contains unsupported shell syntax."
        )

    if normalized not in SUPPORTED_ALIAS_TARGETS:
        supported = ", ".join(sorted(SUPPORTED_ALIAS_TARGETS))
        raise AliasConfigError(
            f"Invalid configuration in {path} at {field_path}: unknown command {normalized!r}. "
            f"Supported commands: {supported}."
        )

    return normalized


def load_desired_alias_state(
    config_path: Path,
    *,
    case_insensitive_names: bool,
) -> DesiredAliasState:
    loaded = _read_yaml_mapping(config_path)

    aliases_data = loaded.get(ALIASES_SECTION_KEY, {})
    if aliases_data is None:
        aliases_data = {}

    if not isinstance(aliases_data, dict):
        raise AliasConfigError(
            f"Invalid configuration in {config_path} at {ALIASES_SECTION_KEY}: expected mapping."
        )

    normalized_aliases: dict[str, str] = {}
    normalized_name_index: dict[str, str] = {}

    for raw_name, raw_target in aliases_data.items():
        if not isinstance(raw_name, str):
            raise AliasConfigError(
                f"Invalid configuration in {config_path} at {ALIASES_SECTION_KEY}: alias names must be strings."
            )
        if not isinstance(raw_target, str):
            raise AliasConfigError(
                f"Invalid configuration in {config_path} at {ALIASES_SECTION_KEY}.{raw_name}: alias targets must be strings."
            )

        field_path = f"{ALIASES_SECTION_KEY}.{raw_name}"
        alias_name = _validate_alias_name(raw_name, path=config_path, field_path=field_path)
        alias_target = _validate_alias_target(raw_target, path=config_path, field_path=field_path)

        name_key = alias_name.casefold() if case_insensitive_names else alias_name
        existing = normalized_name_index.get(name_key)
        if existing is not None and existing != alias_name:
            raise AliasConfigError(
                f"Invalid configuration in {config_path} at {ALIASES_SECTION_KEY}: duplicate alias names after normalization: "
                f"{existing!r}, {alias_name!r}."
            )

        normalized_name_index[name_key] = alias_name
        normalized_aliases[alias_name] = alias_target

    ordered = dict(sorted(normalized_aliases.items(), key=lambda item: item[0]))
    return DesiredAliasState(aliases=ordered)
