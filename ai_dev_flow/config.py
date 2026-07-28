from __future__ import annotations

from pathlib import Path
from typing import Any

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


SUPPORTED_KEY = "out"


class ConfigError(Exception):
    """Raised for invalid flow configuration."""


def validate_config_key(command: str, key: str) -> None:
    if key != SUPPORTED_KEY:
        raise ConfigError(
            f"Unknown configuration key for {command}: {key}. "
            "Supported key: out."
        )


def validate_out_value(value: str) -> str:
    if not value.strip():
        raise ConfigError("out value cannot be empty.")

    return value


def normalize_config(data: dict[str, Any], path: Path) -> dict[str, str]:
    unknown_keys = sorted(name for name in data if name != SUPPORTED_KEY)
    if unknown_keys:
        raise ConfigError(
            f"Unknown configuration key(s) in {path}: "
            f"{', '.join(unknown_keys)}"
        )

    normalized: dict[str, str] = {}

    if SUPPORTED_KEY in data:
        out_value = data[SUPPORTED_KEY]

        if not isinstance(out_value, str):
            raise ConfigError(
                f"Invalid configuration in {path}: out must be a string."
            )

        if not out_value.strip():
            raise ConfigError(
                f"Invalid configuration in {path}: out cannot be empty."
            )

        normalized[SUPPORTED_KEY] = out_value

    return normalized


def load_config(path: Path) -> dict[str, str]:
    try:
        data = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise ConfigError(str(exc)) from exc

    return normalize_config(data, path)


def get_out(path: Path) -> str | None:
    return load_config(path).get(SUPPORTED_KEY)


def set_out(path: Path, value: str) -> str:
    validated = validate_out_value(value)
    config = load_config(path)
    config[SUPPORTED_KEY] = validated

    try:
        write_json_object_atomic(path, config)
    except JsonFileError as exc:
        raise ConfigError(str(exc)) from exc

    return validated


def unset_out(path: Path) -> bool:
    config = load_config(path)

    if SUPPORTED_KEY not in config:
        return False

    del config[SUPPORTED_KEY]

    if config:
        try:
            write_json_object_atomic(path, config)
        except JsonFileError as exc:
            raise ConfigError(str(exc)) from exc
    elif path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise ConfigError(f"Cannot remove {path}: {exc}") from exc

    return True
