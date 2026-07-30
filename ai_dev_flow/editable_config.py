from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml

from .task_config import default_user_config_text, resolve_user_config_path


class EditableConfigError(Exception):
    """Raised for editable user config path/create failures."""


@dataclass(frozen=True)
class EditableConfigState:
    config_path: Path
    created: bool


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve(strict=False)
    except OSError as exc:
        raise EditableConfigError(f"Cannot resolve config path {expanded}: {exc}") from exc


def _create_file_exclusive(path: Path, text: str) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        if not path.is_file():
            raise EditableConfigError(
                f"Config path is not a regular file after create race: {path}"
            )
        return False
    except OSError as exc:
        raise EditableConfigError(f"Cannot create default config file {path}: {exc}") from exc

    try:
        with os.fdopen(file_descriptor, mode="w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except Exception as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise EditableConfigError(f"Cannot write default config file {path}: {exc}") from exc
    return True


def ensure_editable_user_config() -> EditableConfigState:
    config_path = _absolute_path(resolve_user_config_path())

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EditableConfigError(
            f"Cannot create config parent directory {config_path.parent}: {exc}"
        ) from exc

    if config_path.exists():
        if not config_path.is_file():
            raise EditableConfigError(f"Config path is not a regular file: {config_path}")
        return EditableConfigState(config_path=config_path, created=False)

    created = _create_file_exclusive(config_path, default_user_config_text())
    return EditableConfigState(config_path=config_path, created=created)


def resolve_configured_editor_command(config_path: Path) -> tuple[str | None, str | None]:
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Cannot read AI Dev config {config_path} to resolve editor.command: {exc}"

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"Invalid YAML in AI Dev config {config_path}; ignoring editor.command: {exc}"

    if loaded is None:
        return None, None
    if not isinstance(loaded, dict):
        return None, f"Invalid AI Dev config root in {config_path}; expected mapping for editor.command lookup."

    editor_section: Any = loaded.get("editor")
    if editor_section is None:
        return None, None
    if not isinstance(editor_section, dict):
        return None, f"Invalid AI Dev config in {config_path} at editor: expected mapping."

    command_value = editor_section.get("command")
    if command_value is None:
        return None, None
    if not isinstance(command_value, str):
        return None, f"Invalid AI Dev config in {config_path} at editor.command: expected string or null."

    normalized_command = command_value.strip()
    if not normalized_command:
        return None, f"Invalid AI Dev config in {config_path} at editor.command: expected non-empty string."

    return normalized_command, None
