from __future__ import annotations

import string
from pathlib import Path

from .task_config import TaskConfigError


ALLOWED_INVOCATION_VARIABLES = frozenset({"task_file", "task_id", "task_type"})


def validate_invocation_template(
    template: str,
    *,
    config_path: Path | None,
    config_field_path: str,
) -> None:
    formatter = string.Formatter()
    unknown: list[str] = []
    source = str(config_path) if config_path is not None else "built-in defaults"

    try:
        parsed_fields = list(formatter.parse(template))
    except ValueError as exc:
        raise TaskConfigError(
            f"Invalid configuration in {source} at {config_field_path}: malformed template: {exc}."
        ) from exc

    for _, field_name, _, _ in parsed_fields:
        if field_name is None:
            continue

        if field_name not in ALLOWED_INVOCATION_VARIABLES:
            unknown.append(field_name)

    if unknown:
        raise TaskConfigError(
            f"Invalid configuration in {source} at {config_field_path}: unknown template variable(s): {', '.join(sorted(set(unknown)))}. "
            "Expected variables: task_file, task_id, task_type."
        )


def render_invocation(
    template: str,
    *,
    task_file: str,
    task_id: str,
    task_type: str,
    config_path: Path | None,
    config_field_path: str = "ai.invocation",
) -> str:
    validate_invocation_template(
        template,
        config_path=config_path,
        config_field_path=config_field_path,
    )

    source = str(config_path) if config_path is not None else "built-in defaults"

    try:
        rendered = template.format(
            task_file=task_file,
            task_id=task_id,
            task_type=task_type,
        )
    except KeyError as exc:
        raise TaskConfigError(
            f"Invalid configuration in {source} at {config_field_path}: missing template variable {exc}."
        ) from exc
    except ValueError as exc:
        raise TaskConfigError(
            f"Invalid configuration in {source} at {config_field_path}: malformed template: {exc}."
        ) from exc

    return rendered
