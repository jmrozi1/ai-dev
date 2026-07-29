from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .summarize_glob import SummarizeGlobError, validate_rule_match_glob


class SummarizeConfigError(Exception):
    """Raised for summarize repository configuration errors."""


@dataclass(frozen=True)
class SummarizeRule:
    index: int
    match: str
    instructions: str


@dataclass(frozen=True)
class SummarizeConfig:
    rules: tuple[SummarizeRule, ...]
    batch_max_files: int
    repository_config_path: Path | None


_REPO_CONFIG_FILE_NAME = ".ai-dev.yaml"
_SUMMARIZE_SECTION_KEY = "summarize"
_RULES_KEY = "rules"
_BATCH_KEY = "batch"
_MAX_FILES_KEY = "max_files"
_RULE_ALLOWED_KEYS = frozenset({"match", "instructions"})
_BATCH_ALLOWED_KEYS = frozenset({_MAX_FILES_KEY})
_SUMMARIZE_ALLOWED_KEYS = frozenset({_RULES_KEY, _BATCH_KEY})
DEFAULT_SUMMARIZE_BATCH_MAX_FILES = 20


def _type_name(value: object) -> str:
    return type(value).__name__


def _repo_config_path(repo_root: Path) -> Path:
    return repo_root / _REPO_CONFIG_FILE_NAME


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SummarizeConfigError(f"Cannot read configuration file {path}: {exc}") from exc

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SummarizeConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at <root>: expected mapping, got {_type_name(loaded)}."
        )

    return loaded


def _validate_rule(path: Path, rule_data: object, index: int) -> SummarizeRule:
    field_prefix = f"{_SUMMARIZE_SECTION_KEY}.{_RULES_KEY}[{index}]"
    if not isinstance(rule_data, dict):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}: expected mapping, got {_type_name(rule_data)}."
        )

    unknown_rule_keys = sorted(name for name in rule_data if name not in _RULE_ALLOWED_KEYS)
    if unknown_rule_keys:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}: unknown key(s): {', '.join(unknown_rule_keys)}. "
            "Expected keys: match, instructions."
        )

    if "match" not in rule_data:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.match: missing required key."
        )

    if "instructions" not in rule_data:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.instructions: missing required key."
        )

    match = rule_data["match"]
    if not isinstance(match, str):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.match: expected string, got {_type_name(match)}."
        )

    instructions = rule_data["instructions"]
    if not isinstance(instructions, str):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.instructions: expected string, got {_type_name(instructions)}."
        )

    try:
        normalized_match = validate_rule_match_glob(match)
    except SummarizeGlobError as exc:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.match: {exc}"
        ) from exc

    normalized_instructions = instructions.strip()
    if not normalized_instructions:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {field_prefix}.instructions: expected non-empty string."
        )

    return SummarizeRule(
        index=index,
        match=normalized_match,
        instructions=normalized_instructions,
    )


def _validate_batch_settings(path: Path, section_data: dict[str, Any]) -> int:
    batch_data = section_data.get(_BATCH_KEY, {})
    if batch_data is None:
        batch_data = {}

    if not isinstance(batch_data, dict):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {_SUMMARIZE_SECTION_KEY}.{_BATCH_KEY}: "
            f"expected mapping, got {_type_name(batch_data)}."
        )

    unknown_batch_keys = sorted(name for name in batch_data if name not in _BATCH_ALLOWED_KEYS)
    if unknown_batch_keys:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {_SUMMARIZE_SECTION_KEY}.{_BATCH_KEY}: "
            f"unknown key(s): {', '.join(unknown_batch_keys)}. Expected keys: max_files."
        )

    max_files = batch_data.get(_MAX_FILES_KEY, DEFAULT_SUMMARIZE_BATCH_MAX_FILES)
    if isinstance(max_files, bool) or not isinstance(max_files, int):
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {_SUMMARIZE_SECTION_KEY}.{_BATCH_KEY}.{_MAX_FILES_KEY}: "
            f"expected integer greater than zero, got {_type_name(max_files)}."
        )

    if max_files <= 0:
        raise SummarizeConfigError(
            f"Invalid configuration in {path} at {_SUMMARIZE_SECTION_KEY}.{_BATCH_KEY}.{_MAX_FILES_KEY}: "
            "expected integer greater than zero."
        )

    return max_files


def load_repository_summarize_config(repo_root: Path) -> SummarizeConfig:
    config_path = _repo_config_path(repo_root)
    root = _read_yaml_mapping(config_path)

    section_data = root.get(_SUMMARIZE_SECTION_KEY, {})
    if section_data is None:
        section_data = {}

    if not isinstance(section_data, dict):
        raise SummarizeConfigError(
            f"Invalid configuration in {config_path} at {_SUMMARIZE_SECTION_KEY}: "
            f"expected mapping, got {_type_name(section_data)}."
        )

    unknown_section_keys = sorted(name for name in section_data if name not in _SUMMARIZE_ALLOWED_KEYS)
    if unknown_section_keys:
        raise SummarizeConfigError(
            f"Invalid configuration in {config_path} at {_SUMMARIZE_SECTION_KEY}: "
            f"unknown key(s): {', '.join(unknown_section_keys)}. Expected keys: batch, rules."
        )

    batch_max_files = _validate_batch_settings(config_path, section_data)

    rules_value = section_data.get(_RULES_KEY, [])
    if rules_value is None:
        rules_value = []

    if not isinstance(rules_value, list):
        raise SummarizeConfigError(
            f"Invalid configuration in {config_path} at {_SUMMARIZE_SECTION_KEY}.{_RULES_KEY}: "
            f"expected list, got {_type_name(rules_value)}."
        )

    normalized_rules = tuple(_validate_rule(config_path, rule_data, index) for index, rule_data in enumerate(rules_value))

    return SummarizeConfig(
        rules=normalized_rules,
        batch_max_files=batch_max_files,
        repository_config_path=config_path if config_path.exists() else None,
    )
