from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

import yaml
from .report_presentation import SUPPORTED_REPORT_PRESENTATION_VALUES
from .task_delivery import SUPPORTED_DELIVERY_VALUES


DEFAULT_DELIVERY = "stdout"
DEFAULT_INVOCATION = "Read and execute {task_file}"
DEFAULT_REPORT_PRESENTATION = "path-only"
ALLOWED_DELIVERY_VALUES = frozenset(SUPPORTED_DELIVERY_VALUES)
ALLOWED_REPORT_PRESENTATION_VALUES = frozenset(SUPPORTED_REPORT_PRESENTATION_VALUES)
USER_SECTION_KEY = "ai"
EDITOR_SECTION_KEY = "editor"
REPORTS_SECTION_KEY = "reports"
ALIASES_SECTION_KEY = "aliases"
INSTALLATION_SECTION_KEY = "installation"
USER_ALLOWED_ROOT_KEYS = frozenset({
    USER_SECTION_KEY,
    EDITOR_SECTION_KEY,
    REPORTS_SECTION_KEY,
    ALIASES_SECTION_KEY,
    INSTALLATION_SECTION_KEY,
})


class TaskConfigError(Exception):
    """Raised for invalid generated-task configuration."""


@dataclass(frozen=True)
class TaskConfig:
    delivery: str = DEFAULT_DELIVERY
    invocation: str = DEFAULT_INVOCATION
    editor_command: str | None = None
    report_presentation: str = DEFAULT_REPORT_PRESENTATION
    user_config_path: Path | None = None
    repository_config_path: Path | None = None
    delivery_source_path: Path | None = None
    invocation_source_path: Path | None = None
    delivery_source_field: str | None = None
    invocation_source_field: str | None = None
    editor_command_source_path: Path | None = None
    report_presentation_source_path: Path | None = None
    editor_command_source_field: str | None = None
    report_presentation_source_field: str | None = None


def resolve_user_config_path() -> Path:
    return _platform_user_config_path()


def default_user_config_text() -> str:
    return (
        "# AI Dev user configuration\n"
        "# Configure invocation delivery, report presentation, explicit editor,\n"
        "# and managed installation behavior here.\n"
        "\n"
        "ai:\n"
        "  # Template used when emitting generated task invocation text.\n"
        f"  delivery: {DEFAULT_DELIVERY}\n"
        "  # Available delivery modes: stdout, file-only, clipboard, clipboard+stdout.\n"
        f"  invocation: \"{DEFAULT_INVOCATION}\"\n"
        "\n"
        "reports:\n"
        "  # Available report presentation modes: stdout, editor, path-only.\n"
        "  presentation: path-only\n"
        "\n"
        "editor:\n"
        "  # Optional explicit editor command, for example: \"code --wait\".\n"
        "  command: null\n"
        "\n"
        "installation:\n"
        "  # Managed resources are created/updated by `ai-dev apply`, recorded as AI Dev-owned,\n"
        "  # and safely reconciled (including removal when disabled or no longer desired).\n"
        "  # Linux launchers are executable files in ~/.local/bin, enabling normal flow-<tab> completion.\n"
        "  aliases:\n"
        "    enabled: true\n"
        "    commands:\n"
        "      flow: \"ai-dev flow\"\n"
        "      flow-start: \"ai-dev flow start\"\n"
        "      flow-patch: \"ai-dev flow patch\"\n"
        "      flow-task-prepare: \"ai-dev flow task-prepare\"\n"
        "      flow-status: \"ai-dev flow status\"\n"
        "      flow-review: \"ai-dev flow review\"\n"
        "      flow-commit: \"ai-dev flow commit\"\n"
        "      flow-reset: \"ai-dev flow reset\"\n"
        "      flow-promote: \"ai-dev flow promote\"\n"
        "      flow-complete: \"ai-dev flow complete\"\n"
        "      flow-block: \"ai-dev flow block\"\n"
        "      flow-resume: \"ai-dev flow resume\"\n"
        "  shellPath:\n"
        "    # Linux only in checkpoint 2: manages ~/.bashrc PATH marker block.\n"
        "    enabled: true\n"
        "\n"
        "# Legacy alias mapping (deprecated, ignored by `ai-dev apply`).\n"
        "aliases: {}\n"
    )


def _platform_user_config_path() -> Path:
    override = os.environ.get("AI_DEV_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "ai-dev" / "config.yaml"
        return Path.home() / "AppData" / "Roaming" / "ai-dev" / "config.yaml"

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "ai-dev" / "config.yaml"

    return Path.home() / ".config" / "ai-dev" / "config.yaml"


def _repo_config_path(repo_root: Path) -> Path:
    return repo_root / ".ai-dev.yaml"


def _type_name(value: object) -> str:
    return type(value).__name__


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskConfigError(f"Cannot read configuration file {path}: {exc}") from exc

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise TaskConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at <root>: expected mapping, got {_type_name(loaded)}."
        )

    return loaded


def _validate_section(
    data: dict[str, Any],
    *,
    path: Path,
    section_key: str,
    field_prefix: str,
    strict_root: bool,
) -> dict[str, str]:
    if strict_root:
        unknown_root_keys = sorted(name for name in data if name not in USER_ALLOWED_ROOT_KEYS)
        if unknown_root_keys:
            field_path = "<root>"
            allowed = ", ".join(sorted(USER_ALLOWED_ROOT_KEYS))
            raise TaskConfigError(
                f"Invalid configuration in {path} at {field_path}: unknown key(s): {', '.join(unknown_root_keys)}. "
                f"Expected keys: {allowed}."
            )

    section_data = data.get(section_key, {})
    if not isinstance(section_data, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at {section_key}: expected mapping, got {_type_name(section_data)}."
        )

    unknown_section_keys = sorted(name for name in section_data if name not in {"delivery", "invocation"})
    if unknown_section_keys:
        raise TaskConfigError(
            f"Invalid configuration in {path} at {section_key}: unknown key(s): {', '.join(unknown_section_keys)}. "
            "Expected keys: delivery, invocation."
        )

    normalized: dict[str, str] = {}

    if "delivery" in section_data:
        delivery = section_data["delivery"]
        if not isinstance(delivery, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {field_prefix}.delivery: expected string, got {_type_name(delivery)}. "
                f"Expected one of: {', '.join(SUPPORTED_DELIVERY_VALUES)}."
            )

        normalized_delivery = delivery.strip()
        if normalized_delivery not in ALLOWED_DELIVERY_VALUES:
            raise TaskConfigError(
                f"Invalid configuration in {path} at {field_prefix}.delivery: unsupported value {normalized_delivery!r}. "
                f"Expected one of: {', '.join(SUPPORTED_DELIVERY_VALUES)}."
            )

        normalized["delivery"] = normalized_delivery

    if "invocation" in section_data:
        invocation = section_data["invocation"]
        if not isinstance(invocation, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {field_prefix}.invocation: expected string, got {_type_name(invocation)}."
            )

        if not invocation.strip():
            raise TaskConfigError(
                f"Invalid configuration in {path} at {field_prefix}.invocation: expected non-empty string."
            )

        normalized["invocation"] = invocation

    return normalized


def _validate_editor_section(
    data: dict[str, Any],
    *,
    path: Path,
) -> dict[str, str]:
    section_data = data.get(EDITOR_SECTION_KEY, {})
    if not isinstance(section_data, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at {EDITOR_SECTION_KEY}: expected mapping, got {_type_name(section_data)}."
        )

    unknown_section_keys = sorted(name for name in section_data if name not in {"command"})
    if unknown_section_keys:
        raise TaskConfigError(
            f"Invalid configuration in {path} at {EDITOR_SECTION_KEY}: unknown key(s): {', '.join(unknown_section_keys)}. "
            "Expected keys: command."
        )

    normalized: dict[str, str] = {}
    if "command" in section_data:
        command = section_data["command"]
        if command is None:
            return normalized
        if not isinstance(command, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {EDITOR_SECTION_KEY}.command: expected string, got {_type_name(command)}."
            )

        normalized_command = command.strip()
        if not normalized_command:
            raise TaskConfigError(
                f"Invalid configuration in {path} at {EDITOR_SECTION_KEY}.command: expected non-empty string."
            )

        normalized["command"] = normalized_command

    return normalized


def _validate_aliases_section(
    data: dict[str, Any],
    *,
    path: Path,
) -> None:
    aliases = data.get(ALIASES_SECTION_KEY, {})
    if not isinstance(aliases, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at {ALIASES_SECTION_KEY}: expected mapping, got {_type_name(aliases)}."
        )

    for key, value in aliases.items():
        if not isinstance(key, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {ALIASES_SECTION_KEY}: alias keys must be strings."
            )
        if not isinstance(value, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {ALIASES_SECTION_KEY}.{key}: expected string, got {_type_name(value)}."
            )
        if not value.strip():
            raise TaskConfigError(
                f"Invalid configuration in {path} at {ALIASES_SECTION_KEY}.{key}: expected non-empty string."
            )


def _validate_installation_section(
    data: dict[str, Any],
    *,
    path: Path,
) -> None:
    installation = data.get(INSTALLATION_SECTION_KEY, {})
    if not isinstance(installation, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at {INSTALLATION_SECTION_KEY}: expected mapping, got {_type_name(installation)}."
        )


def _validate_reports_section(
    data: dict[str, Any],
    *,
    path: Path,
) -> dict[str, str]:
    section_data = data.get(REPORTS_SECTION_KEY, {})
    if not isinstance(section_data, dict):
        raise TaskConfigError(
            f"Invalid configuration in {path} at {REPORTS_SECTION_KEY}: expected mapping, got {_type_name(section_data)}."
        )

    unknown_section_keys = sorted(name for name in section_data if name not in {"presentation"})
    if unknown_section_keys:
        raise TaskConfigError(
            f"Invalid configuration in {path} at {REPORTS_SECTION_KEY}: unknown key(s): {', '.join(unknown_section_keys)}. "
            "Expected keys: presentation."
        )

    normalized: dict[str, str] = {}
    if "presentation" in section_data:
        presentation = section_data["presentation"]
        if not isinstance(presentation, str):
            raise TaskConfigError(
                f"Invalid configuration in {path} at {REPORTS_SECTION_KEY}.presentation: expected string, got {_type_name(presentation)}. "
                f"Expected one of: {', '.join(SUPPORTED_REPORT_PRESENTATION_VALUES)}."
            )

        normalized_presentation = presentation.strip()
        if normalized_presentation not in ALLOWED_REPORT_PRESENTATION_VALUES:
            raise TaskConfigError(
                f"Invalid configuration in {path} at {REPORTS_SECTION_KEY}.presentation: unsupported value {normalized_presentation!r}. "
                f"Expected one of: {', '.join(SUPPORTED_REPORT_PRESENTATION_VALUES)}."
            )

        normalized["presentation"] = normalized_presentation

    return normalized


def load_task_config(repo_root: Path) -> TaskConfig:
    user_path = _platform_user_config_path()
    repo_path = _repo_config_path(repo_root)

    defaults: dict[str, str] = {
        "delivery": DEFAULT_DELIVERY,
        "invocation": DEFAULT_INVOCATION,
        "report_presentation": DEFAULT_REPORT_PRESENTATION,
    }

    user_data = _read_yaml_mapping(user_path)
    user_values = _validate_section(
        user_data,
        path=user_path,
        section_key=USER_SECTION_KEY,
        field_prefix=USER_SECTION_KEY,
        strict_root=True,
    )
    editor_values = _validate_editor_section(user_data, path=user_path)
    reports_values = _validate_reports_section(user_data, path=user_path)
    _validate_aliases_section(user_data, path=user_path)
    _validate_installation_section(user_data, path=user_path)

    # Keep reading repository config for compatibility with existing .ai-dev.yaml
    # usage, but machine-owned delivery/invocation preferences are user-only.
    _read_yaml_mapping(repo_path)

    merged = dict(defaults)
    delivery_source_path: Path | None = None
    invocation_source_path: Path | None = None
    editor_command_source_path: Path | None = None
    report_presentation_source_path: Path | None = None
    delivery_source_field: str | None = None
    invocation_source_field: str | None = None
    editor_command_source_field: str | None = None
    report_presentation_source_field: str | None = None

    if "delivery" in user_values:
        merged["delivery"] = user_values["delivery"]
        delivery_source_path = user_path
        delivery_source_field = "ai.delivery"

    if "invocation" in user_values:
        merged["invocation"] = user_values["invocation"]
        invocation_source_path = user_path
        invocation_source_field = "ai.invocation"

    editor_command: str | None = None
    if "command" in editor_values:
        editor_command = editor_values["command"]
        editor_command_source_path = user_path
        editor_command_source_field = "editor.command"

    if "presentation" in reports_values:
        merged["report_presentation"] = reports_values["presentation"]
        report_presentation_source_path = user_path
        report_presentation_source_field = "reports.presentation"

    return TaskConfig(
        delivery=merged["delivery"],
        invocation=merged["invocation"],
        editor_command=editor_command,
        report_presentation=merged["report_presentation"],
        user_config_path=user_path if user_path.exists() else None,
        repository_config_path=repo_path if repo_path.exists() else None,
        delivery_source_path=delivery_source_path,
        invocation_source_path=invocation_source_path,
        delivery_source_field=delivery_source_field,
        invocation_source_field=invocation_source_field,
        editor_command_source_path=editor_command_source_path,
        report_presentation_source_path=report_presentation_source_path,
        editor_command_source_field=editor_command_source_field,
        report_presentation_source_field=report_presentation_source_field,
    )
