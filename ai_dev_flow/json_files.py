from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


class JsonFileError(Exception):
    """Raised for user-facing JSON file failures."""


def load_json_object(path: Path, *, missing_default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(missing_default)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JsonFileError(f"Cannot read {path}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonFileError(
            f"Invalid JSON in {path}: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(data, dict):
        raise JsonFileError(
            f"Invalid configuration in {path}: expected a JSON object."
        )

    return data


def write_json_object_atomic(path: Path, data: dict[str, Any]) -> None:
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
            temporary_path = Path(handle.name)

        os.replace(temporary_path, path)
    except Exception as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

        raise JsonFileError(f"Cannot write {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str) -> None:
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
            handle.write(text)
            temporary_path = Path(handle.name)

        os.replace(temporary_path, path)
    except Exception as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

        raise JsonFileError(f"Cannot write {path}: {exc}") from exc
