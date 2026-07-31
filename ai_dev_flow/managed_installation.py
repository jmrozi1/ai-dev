from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import posixpath
import ntpath
import re
import shlex
import stat
from typing import Any

import yaml

from .json_files import JsonFileError, load_json_object, write_json_object_atomic, write_text_atomic


MANAGED_LAUNCHER_MARKER = "AI_DEV_MANAGED_LAUNCHER_V1"
MANAGED_PATH_BEGIN = "# >>> ai-dev managed PATH >>>"
MANAGED_PATH_END = "# <<< ai-dev managed PATH <<<"
MANIFEST_VERSION = 1

ALIAS_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]*$"
_RESERVED_ALIAS_NAMES = frozenset({"ai-dev", "aidev", "ai_dev"})

DEFAULT_MANAGED_ALIAS_COMMANDS: dict[str, str] = {
    "flow": "ai-dev flow",
    "flow-start": "ai-dev flow start",
    "flow-patch": "ai-dev flow patch",
    "flow-task-prepare": "ai-dev flow task-prepare",
    "flow-status": "ai-dev flow status",
    "flow-review": "ai-dev flow review",
    "flow-commit": "ai-dev flow commit",
    "flow-reset": "ai-dev flow reset",
    "flow-promote": "ai-dev flow promote",
    "flow-complete": "ai-dev flow complete",
    "flow-block": "ai-dev flow block",
    "flow-resume": "ai-dev flow resume",
}


class InstallationConfigError(Exception):
    """Raised for invalid managed installation configuration."""


class ManagedInstallationError(Exception):
    """Raised for managed installation reconciliation failures."""


@dataclass(frozen=True)
class DesiredInstallationState:
    aliases_enabled: bool
    alias_commands: dict[str, str]
    shell_path_enabled: bool


@dataclass(frozen=True)
class ManagedInstallationPaths:
    launcher_directory: Path
    manifest_path: Path
    bashrc_path: Path | None
    windows: bool


@dataclass(frozen=True)
class ManagedInstallationSummary:
    launchers_created: int
    launchers_updated: int
    launchers_removed: int
    launchers_unchanged: int
    path_status: str
    manifest_status: str
    launcher_directory: Path
    manifest_path: Path
    bashrc_path: Path | None


@dataclass(frozen=True)
class _Manifest:
    version: int
    managed_launchers: dict[str, str]
    managed_path_block_file: str | None
    managed_path_block_sha256: str | None


@dataclass(frozen=True)
class _SnapshotEntry:
    exists: bool
    text: str
    mode: int | None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _normalize_path_for_compare(path: Path, *, windows: bool) -> str:
    raw = str(path)
    if windows:
        return ntpath.normcase(ntpath.normpath(raw))
    return posixpath.normpath(raw)


def _parse_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallationConfigError(f"Cannot read AI Dev config {path}: {exc}") from exc

    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise InstallationConfigError(f"Invalid YAML in AI Dev config {path}: {exc}") from exc

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise InstallationConfigError(
            f"Invalid AI Dev config in {path} at <root>: expected mapping."
        )

    return loaded


def _require_mapping(value: Any, *, path: Path, field_path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: expected mapping."
        )
    return value


def _require_bool(value: Any, *, path: Path, field_path: str) -> bool:
    if not isinstance(value, bool):
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: expected boolean."
        )
    return value


def _validate_alias_name(name: str, *, path: Path, field_path: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name must be non-empty."
        )

    if normalized in _RESERVED_ALIAS_NAMES:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name {normalized!r} is reserved."
        )

    if re.match(ALIAS_NAME_PATTERN, normalized) is None:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: alias name must match {ALIAS_NAME_PATTERN}."
        )

    return normalized


def _parse_command_tokens(command_text: str, *, path: Path, field_path: str) -> list[str]:
    normalized = command_text.strip()
    if not normalized:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: command mapping must be non-empty."
        )

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: cannot parse command mapping: {exc}"
        ) from exc

    if not tokens:
        raise InstallationConfigError(
            f"Invalid configuration in {path} at {field_path}: command mapping must include at least one token."
        )

    for token in tokens:
        if "\x00" in token:
            raise InstallationConfigError(
                f"Invalid configuration in {path} at {field_path}: command mapping contains NUL bytes."
            )

    return tokens


def load_desired_installation_state(
    config_path: Path,
    *,
    case_insensitive_names: bool,
) -> DesiredInstallationState:
    loaded = _parse_yaml_mapping(config_path)

    installation = _require_mapping(
        loaded.get("installation"),
        path=config_path,
        field_path="installation",
    )

    aliases_section = _require_mapping(
        installation.get("aliases"),
        path=config_path,
        field_path="installation.aliases",
    )

    shell_path_section = _require_mapping(
        installation.get("shellPath"),
        path=config_path,
        field_path="installation.shellPath",
    )

    aliases_enabled = True
    if "enabled" in aliases_section:
        aliases_enabled = _require_bool(
            aliases_section["enabled"],
            path=config_path,
            field_path="installation.aliases.enabled",
        )

    shell_path_enabled = True
    if "enabled" in shell_path_section:
        shell_path_enabled = _require_bool(
            shell_path_section["enabled"],
            path=config_path,
            field_path="installation.shellPath.enabled",
        )

    commands_raw = aliases_section.get("commands")
    commands_map: dict[str, str]
    if commands_raw is None:
        commands_map = dict(DEFAULT_MANAGED_ALIAS_COMMANDS) if aliases_enabled else {}
    else:
        commands_map = {}
        commands_obj = _require_mapping(
            commands_raw,
            path=config_path,
            field_path="installation.aliases.commands",
        )
        normalized_name_index: dict[str, str] = {}
        for raw_name, raw_command in commands_obj.items():
            if not isinstance(raw_name, str):
                raise InstallationConfigError(
                    f"Invalid configuration in {config_path} at installation.aliases.commands: alias names must be strings."
                )
            if not isinstance(raw_command, str):
                raise InstallationConfigError(
                    f"Invalid configuration in {config_path} at installation.aliases.commands.{raw_name}: command mappings must be strings."
                )

            field_path = f"installation.aliases.commands.{raw_name}"
            alias_name = _validate_alias_name(raw_name, path=config_path, field_path=field_path)
            _parse_command_tokens(raw_command, path=config_path, field_path=field_path)
            name_key = alias_name.casefold() if case_insensitive_names else alias_name
            existing = normalized_name_index.get(name_key)
            if existing is not None and existing != alias_name:
                raise InstallationConfigError(
                    f"Invalid configuration in {config_path} at installation.aliases.commands: duplicate alias names after normalization: "
                    f"{existing!r}, {alias_name!r}."
                )
            normalized_name_index[name_key] = alias_name
            commands_map[alias_name] = raw_command.strip()

        commands_map = dict(sorted(commands_map.items(), key=lambda item: item[0]))

    return DesiredInstallationState(
        aliases_enabled=aliases_enabled,
        alias_commands=commands_map,
        shell_path_enabled=shell_path_enabled,
    )


def resolve_managed_installation_paths(
    *,
    os_name: str | None = None,
    home: Path | None = None,
    local_appdata: str | None = None,
) -> ManagedInstallationPaths:
    resolved_os = os.name if os_name is None else os_name
    windows = resolved_os == "nt"

    home_path = Path.home() if home is None else home
    home_path = home_path.expanduser().resolve()

    if windows:
        appdata_text = local_appdata
        if appdata_text is None:
            appdata_text = os.environ.get("LOCALAPPDATA", "").strip()
        if appdata_text:
            launcher_directory = Path(appdata_text).expanduser() / "ai-dev" / "bin"
        else:
            launcher_directory = home_path / "AppData" / "Local" / "ai-dev" / "bin"
        manifest_path = home_path / ".ai-dev" / "installation-manifest.json"
        return ManagedInstallationPaths(
            launcher_directory=launcher_directory,
            manifest_path=manifest_path,
            bashrc_path=None,
            windows=True,
        )

    launcher_directory = home_path / ".local" / "bin"
    manifest_path = home_path / ".config" / "ai-dev" / "installation-manifest.json"
    bashrc_path = home_path / ".bashrc"
    return ManagedInstallationPaths(
        launcher_directory=launcher_directory,
        manifest_path=manifest_path,
        bashrc_path=bashrc_path,
        windows=False,
    )


def _load_manifest(path: Path) -> _Manifest:
    if not path.exists():
        return _Manifest(
            version=MANIFEST_VERSION,
            managed_launchers={},
            managed_path_block_file=None,
            managed_path_block_sha256=None,
        )

    try:
        payload = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise ManagedInstallationError(str(exc)) from exc

    version = payload.get("version")
    if version != MANIFEST_VERSION:
        raise ManagedInstallationError(
            f"Invalid installation manifest in {path}: unsupported version {version!r}."
        )

    raw_launchers = payload.get("managed_launchers")
    if not isinstance(raw_launchers, dict):
        raise ManagedInstallationError(
            f"Invalid installation manifest in {path}: field 'managed_launchers' must be an object."
        )

    managed_launchers: dict[str, str] = {}
    for raw_path, raw_digest in raw_launchers.items():
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            raise ManagedInstallationError(
                f"Invalid installation manifest in {path}: managed launcher entries must be string path->digest pairs."
            )
        managed_launchers[raw_path] = raw_digest

    managed_path_block_file = payload.get("managed_path_block_file")
    if managed_path_block_file is not None and not isinstance(managed_path_block_file, str):
        raise ManagedInstallationError(
            f"Invalid installation manifest in {path}: field 'managed_path_block_file' must be string or null."
        )

    managed_path_block_sha256 = payload.get("managed_path_block_sha256")
    if managed_path_block_sha256 is not None and not isinstance(managed_path_block_sha256, str):
        raise ManagedInstallationError(
            f"Invalid installation manifest in {path}: field 'managed_path_block_sha256' must be string or null."
        )

    return _Manifest(
        version=MANIFEST_VERSION,
        managed_launchers=dict(sorted(managed_launchers.items(), key=lambda item: item[0])),
        managed_path_block_file=managed_path_block_file,
        managed_path_block_sha256=managed_path_block_sha256,
    )


def _save_manifest(path: Path, manifest: _Manifest) -> None:
    payload: dict[str, Any] = {
        "version": manifest.version,
        "managed_launchers": dict(sorted(manifest.managed_launchers.items(), key=lambda item: item[0])),
        "managed_path_block_file": manifest.managed_path_block_file,
        "managed_path_block_sha256": manifest.managed_path_block_sha256,
    }
    try:
        write_json_object_atomic(path, payload)
    except JsonFileError as exc:
        raise ManagedInstallationError(str(exc)) from exc


def _quote_posix_single(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_posix_launcher(command_text: str) -> str:
    tokens = shlex.split(command_text, posix=True)
    quoted = " ".join(_quote_posix_single(token) for token in tokens)
    return (
        "#!/usr/bin/env sh\n"
        f"# {MANAGED_LAUNCHER_MARKER}\n"
        "set -eu\n"
        f"exec {quoted} \"$@\"\n"
    )


def _escape_cmd_token(token: str) -> str:
    return token.replace("%", "%%").replace('"', '""')


def _render_cmd_launcher(command_text: str) -> str:
    tokens = shlex.split(command_text, posix=True)
    command = " ".join(f'"{_escape_cmd_token(token)}"' for token in tokens)
    return (
        "@echo off\n"
        f":: {MANAGED_LAUNCHER_MARKER}\n"
        "setlocal\n"
        f"{command} %*\n"
        "set \"AI_DEV_EXIT=%ERRORLEVEL%\"\n"
        "endlocal & exit /b %AI_DEV_EXIT%\n"
    )


def _is_managed_launcher_text(text: str) -> bool:
    lines = text.splitlines()
    if not lines:
        return False
    first_two = "\n".join(lines[:2])
    return MANAGED_LAUNCHER_MARKER in first_two


def _find_path_block(text: str) -> tuple[int, int] | None:
    begin_positions: list[int] = []
    end_positions: list[int] = []

    start = 0
    while True:
        index = text.find(MANAGED_PATH_BEGIN, start)
        if index == -1:
            break
        begin_positions.append(index)
        start = index + len(MANAGED_PATH_BEGIN)

    start = 0
    while True:
        index = text.find(MANAGED_PATH_END, start)
        if index == -1:
            break
        end_positions.append(index)
        start = index + len(MANAGED_PATH_END)

    if not begin_positions and not end_positions:
        return None

    if len(begin_positions) != 1 or len(end_positions) != 1:
        raise ManagedInstallationError(
            "Malformed managed PATH block in .bashrc: expected zero or one marker block."
        )

    begin_index = begin_positions[0]
    end_index = end_positions[0]
    if end_index < begin_index:
        raise ManagedInstallationError(
            "Malformed managed PATH block in .bashrc: end marker appears before begin marker."
        )

    return begin_index, end_index


def _managed_block_bounds(text: str, begin_index: int, end_index: int) -> tuple[int, int]:
    start = begin_index
    if begin_index > 0 and text[begin_index - 1] == "\n":
        start = begin_index - 1

    end_line_end = text.find("\n", end_index)
    if end_line_end == -1:
        end_line_end = len(text)
    else:
        end_line_end += 1

    return start, end_line_end


def _render_path_block(*, include_leading_separator: bool) -> str:
    prefix = "\n" if include_leading_separator else ""
    return (
        f"{prefix}{MANAGED_PATH_BEGIN}\n"
        "export PATH=\"$HOME/.local/bin:$PATH\"\n"
        f"{MANAGED_PATH_END}\n"
    )


def _compute_path_update(
    *,
    bashrc_text: str,
    shell_path_enabled: bool,
    manifest: _Manifest,
    bashrc_path: Path,
) -> tuple[str, str, str | None]:
    found = _find_path_block(bashrc_text)
    if found is None:
        if shell_path_enabled:
            block = _render_path_block(include_leading_separator=bool(bashrc_text))
            return f"{bashrc_text}{block}", "added", block
        return bashrc_text, "disabled", None

    begin_index, end_index = found
    managed_start, managed_end = _managed_block_bounds(bashrc_text, begin_index, end_index)
    existing_block = bashrc_text[managed_start:managed_end]

    owned = (
        manifest.managed_path_block_file == str(bashrc_path)
        and manifest.managed_path_block_sha256 == _sha256_text(existing_block)
    )

    desired_block = _render_path_block(include_leading_separator=(managed_start < begin_index))

    if shell_path_enabled:
        if not owned:
            if existing_block == desired_block:
                raise ManagedInstallationError(
                    f"Found existing unmanaged PATH block in {bashrc_path}; AI Dev will not claim ownership automatically."
                )
            raise ManagedInstallationError(
                f"Refusing to replace unmanaged PATH block in {bashrc_path}; AI Dev will not claim ownership automatically."
            )
        if existing_block == desired_block:
            return bashrc_text, "unchanged", existing_block
        updated = f"{bashrc_text[:managed_start]}{desired_block}{bashrc_text[managed_end:]}"
        return updated, "updated", desired_block

    if not owned:
        raise ManagedInstallationError(
            f"Refusing to remove unmanaged PATH block in {bashrc_path}."
        )

    updated = f"{bashrc_text[:managed_start]}{bashrc_text[managed_end:]}"
    return updated, "removed", None


def _write_text(path: Path, text: str) -> None:
    try:
        write_text_atomic(path, text)
    except JsonFileError as exc:
        raise ManagedInstallationError(str(exc)) from exc


def _set_executable(path: Path) -> None:
    try:
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        raise ManagedInstallationError(f"Cannot set executable mode on {path}: {exc}") from exc


def _snapshot(paths: list[Path]) -> dict[Path, _SnapshotEntry]:
    snapshot: dict[Path, _SnapshotEntry] = {}
    for path in sorted(set(paths), key=lambda item: str(item)):
        if path.exists():
            try:
                mode = path.stat().st_mode
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ManagedInstallationError(f"Cannot snapshot {path}: {exc}") from exc
            snapshot[path] = _SnapshotEntry(exists=True, text=text, mode=mode)
        else:
            snapshot[path] = _SnapshotEntry(exists=False, text="", mode=None)
    return snapshot


def _restore(snapshot: dict[Path, _SnapshotEntry]) -> list[str]:
    failures: list[str] = []
    for path, entry in snapshot.items():
        try:
            if entry.exists:
                _write_text(path, entry.text)
                if entry.mode is not None:
                    path.chmod(entry.mode)
            elif path.exists():
                path.unlink()
        except Exception as exc:  # pragma: no cover - best effort path
            failures.append(f"restore {path}: {exc}")
    return failures


def apply_installation_reconciliation(
    desired: DesiredInstallationState,
    *,
    paths: ManagedInstallationPaths | None = None,
) -> ManagedInstallationSummary:
    resolved_paths = paths or resolve_managed_installation_paths()
    manifest = _load_manifest(resolved_paths.manifest_path)

    desired_launchers: dict[Path, str] = {}
    if desired.aliases_enabled:
        for alias_name, command_text in desired.alias_commands.items():
            _parse_command_tokens(
                command_text,
                path=resolved_paths.manifest_path,
                field_path=f"installation.aliases.commands.{alias_name}",
            )
            launcher_name = f"{alias_name}.cmd" if resolved_paths.windows else alias_name
            target_path = (resolved_paths.launcher_directory / launcher_name).resolve()
            if resolved_paths.windows:
                rendered = _render_cmd_launcher(command_text)
            else:
                rendered = _render_posix_launcher(command_text)
            desired_launchers[target_path] = rendered

    manifest_launchers: dict[Path, str] = {
        Path(raw_path): digest for raw_path, digest in manifest.managed_launchers.items()
    }

    for target_path in desired_launchers:
        if target_path.exists() and not target_path.is_file():
            raise ManagedInstallationError(
                f"Cannot manage launcher because destination is not a regular file: {target_path}"
            )

        if target_path.exists():
            recorded_digest = manifest_launchers.get(target_path)
            if recorded_digest is None:
                raise ManagedInstallationError(
                    f"Refusing to overwrite unowned existing launcher: {target_path}"
                )

            existing_text = _read_text_or_empty(target_path)
            if _sha256_text(existing_text) != recorded_digest or not _is_managed_launcher_text(existing_text):
                raise ManagedInstallationError(
                    f"Refusing to update divergent managed launcher: {target_path}"
                )

    stale_paths = [
        path for path in manifest_launchers if path not in desired_launchers
    ]
    for stale_path in stale_paths:
        if not stale_path.exists():
            continue
        existing_text = _read_text_or_empty(stale_path)
        expected_digest = manifest_launchers[stale_path]
        if _sha256_text(existing_text) != expected_digest or not _is_managed_launcher_text(existing_text):
            raise ManagedInstallationError(
                f"Refusing to remove divergent managed launcher: {stale_path}"
            )

    bashrc_path = resolved_paths.bashrc_path
    path_status = "disabled"
    updated_bashrc_text: str | None = None
    block_text_for_manifest: str | None = None
    if not resolved_paths.windows and bashrc_path is not None:
        current_bashrc = _read_text_or_empty(bashrc_path)
        updated_bashrc_text, path_status, block_text_for_manifest = _compute_path_update(
            bashrc_text=current_bashrc,
            shell_path_enabled=desired.shell_path_enabled,
            manifest=manifest,
            bashrc_path=bashrc_path,
        )
    elif resolved_paths.windows:
        path_status = "disabled"

    to_snapshot = list(desired_launchers.keys()) + stale_paths
    if not resolved_paths.windows and bashrc_path is not None:
        to_snapshot.append(bashrc_path)
    snapshot = _snapshot(to_snapshot)
    manifest_snapshot_exists = resolved_paths.manifest_path.exists()
    manifest_snapshot_text = _read_text_or_empty(resolved_paths.manifest_path)

    created = 0
    updated = 0
    removed = 0
    unchanged = 0

    try:
        resolved_paths.launcher_directory.mkdir(parents=True, exist_ok=True)

        for target_path, rendered in desired_launchers.items():
            if target_path.exists():
                previous = _read_text_or_empty(target_path)
                if previous == rendered:
                    unchanged += 1
                    continue
                updated += 1
            else:
                created += 1

            _write_text(target_path, rendered)
            if not resolved_paths.windows:
                _set_executable(target_path)

        for stale_path in stale_paths:
            if stale_path.exists():
                stale_path.unlink()
                removed += 1

        if not resolved_paths.windows and bashrc_path is not None and updated_bashrc_text is not None:
            if not updated_bashrc_text.endswith("\n"):
                updated_bashrc_text = f"{updated_bashrc_text}\n"
            _write_text(bashrc_path, updated_bashrc_text)

        final_managed: dict[str, str] = {}
        for target_path in sorted(desired_launchers.keys(), key=lambda item: str(item)):
            final_text = _read_text_or_empty(target_path)
            final_managed[str(target_path)] = _sha256_text(final_text)

        path_file_for_manifest: str | None = None
        path_digest_for_manifest: str | None = None
        if not resolved_paths.windows and bashrc_path is not None and desired.shell_path_enabled and block_text_for_manifest is not None:
            path_file_for_manifest = str(bashrc_path)
            path_digest_for_manifest = _sha256_text(block_text_for_manifest)

        next_manifest = _Manifest(
            version=MANIFEST_VERSION,
            managed_launchers=final_managed,
            managed_path_block_file=path_file_for_manifest,
            managed_path_block_sha256=path_digest_for_manifest,
        )

        manifest_status = "unchanged" if manifest == next_manifest else "updated"
        if manifest_status == "updated":
            _save_manifest(resolved_paths.manifest_path, next_manifest)

    except Exception as exc:
        rollback_failures = _restore(snapshot)
        try:
            if manifest_snapshot_exists:
                _write_text(resolved_paths.manifest_path, manifest_snapshot_text)
            elif resolved_paths.manifest_path.exists():
                resolved_paths.manifest_path.unlink()
        except Exception as manifest_exc:  # pragma: no cover - best effort path
            rollback_failures.append(f"restore manifest {resolved_paths.manifest_path}: {manifest_exc}")

        if rollback_failures:
            joined = "; ".join(rollback_failures)
            raise ManagedInstallationError(f"{exc}; rollback failures: {joined}") from exc

        if isinstance(exc, ManagedInstallationError):
            raise
        raise ManagedInstallationError(str(exc)) from exc

    return ManagedInstallationSummary(
        launchers_created=created,
        launchers_updated=updated,
        launchers_removed=removed,
        launchers_unchanged=unchanged,
        path_status=path_status,
        manifest_status=manifest_status,
        launcher_directory=resolved_paths.launcher_directory,
        manifest_path=resolved_paths.manifest_path,
        bashrc_path=resolved_paths.bashrc_path,
    )
