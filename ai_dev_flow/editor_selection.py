from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os
import platform
import shlex
import shutil

from .editor_opening import DEFAULT_EDITOR_WAIT_TIMEOUT_SECONDS, SubprocessEditorOpener


@dataclass(frozen=True)
class EditorCandidate:
    command_argv: list[str]
    source: str


@dataclass(frozen=True)
class EditorSelectionResult:
    candidate: EditorCandidate | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class EditorLaunchResult:
    opened: bool
    status: str
    command_display: str | None
    warning: str | None


def _is_windows(system_name: str) -> bool:
    return system_name.lower().startswith("win")


def _format_command(command_argv: list[str]) -> str:
    try:
        return shlex.join(command_argv)
    except AttributeError:
        return " ".join(command_argv)


def _tokenize_windows_command(command_text: str) -> list[str]:
    tokens: list[str] = []
    length = len(command_text)
    index = 0

    while index < length:
        while index < length and command_text[index].isspace():
            index += 1
        if index >= length:
            break

        token_parts: list[str] = []
        in_quotes = False

        while index < length:
            character = command_text[index]

            if character == "\\":
                slash_count = 0
                while index < length and command_text[index] == "\\":
                    slash_count += 1
                    index += 1

                if index < length and command_text[index] == '"':
                    token_parts.append("\\" * (slash_count // 2))
                    if slash_count % 2 == 0:
                        in_quotes = not in_quotes
                    else:
                        token_parts.append('"')
                    index += 1
                else:
                    token_parts.append("\\" * slash_count)
                continue

            if character == '"':
                in_quotes = not in_quotes
                index += 1
                continue

            if character.isspace() and not in_quotes:
                break

            token_parts.append(character)
            index += 1

        if in_quotes:
            raise ValueError("unbalanced double quotes")

        tokens.append("".join(token_parts))

        while index < length and command_text[index].isspace():
            index += 1

    return tokens


def _parse_editor_command(command_text: str, *, system_name: str) -> list[str]:
    normalized = command_text.strip()
    if not normalized:
        raise ValueError("command cannot be empty")

    if _is_windows(system_name):
        command_argv = _tokenize_windows_command(normalized)
    else:
        command_argv = shlex.split(normalized, posix=True)

    if not command_argv:
        raise ValueError("command cannot be empty")
    return command_argv


def _command_available(executable: str) -> bool:
    if any(separator in executable for separator in ("/", "\\")):
        path = Path(executable)
        return path.exists() and path.is_file()
    return shutil.which(executable) is not None


def _candidate_from_command(
    command_text: str,
    *,
    source: str,
    system_name: str,
) -> tuple[EditorCandidate | None, str | None]:
    try:
        command_argv = _parse_editor_command(command_text, system_name=system_name)
    except ValueError as exc:
        return None, f"Invalid {source} editor command: {exc}."

    executable = command_argv[0]
    if any(separator in executable for separator in ("/", "\\")):
        executable_path = Path(executable)
        if not executable_path.exists():
            return None, f"{source} editor command is not available: {executable}."
        if not executable_path.is_file():
            return None, f"{source} editor command is not a regular file: {executable}."
    elif not _command_available(executable):
        return None, f"{source} editor command is not available: {executable}."

    return EditorCandidate(command_argv=command_argv, source=source), None


def select_editor_candidate(
    configured_editor_command: str | None,
    *,
    env: Mapping[str, str] | None = None,
    system_name: str | None = None,
) -> EditorSelectionResult:
    environment = env if env is not None else os.environ
    selected_system = system_name or platform.system()
    warnings: list[str] = []

    if configured_editor_command is not None:
        candidate, warning = _candidate_from_command(
            configured_editor_command,
            source="editor.command",
            system_name=selected_system,
        )
        if candidate is not None:
            return EditorSelectionResult(candidate=candidate, warnings=tuple(warnings))
        if warning:
            warnings.append(warning)

    for environment_key in ("VISUAL", "EDITOR"):
        value = environment.get(environment_key, "").strip()
        if not value:
            continue

        candidate, warning = _candidate_from_command(
            value,
            source=environment_key,
            system_name=selected_system,
        )
        if candidate is not None:
            return EditorSelectionResult(candidate=candidate, warnings=tuple(warnings))
        if warning:
            warnings.append(warning)

    if _is_windows(selected_system):
        return EditorSelectionResult(
            candidate=EditorCandidate(command_argv=["notepad.exe"], source="platform-default"),
            warnings=tuple(warnings),
        )

    if _command_available("vi"):
        return EditorSelectionResult(
            candidate=EditorCandidate(command_argv=["vi"], source="platform-default"),
            warnings=tuple(warnings),
        )

    warnings.append("No editor candidate is available from editor.command, VISUAL, EDITOR, or platform defaults.")
    return EditorSelectionResult(candidate=None, warnings=tuple(warnings))


def _join_warnings(warnings: list[str]) -> str | None:
    if not warnings:
        return None
    return "; ".join(warnings)


def launch_selected_editor(
    config_path: Path,
    selection: EditorSelectionResult,
    *,
    wait_timeout_seconds: float = DEFAULT_EDITOR_WAIT_TIMEOUT_SECONDS,
) -> EditorLaunchResult:
    if selection.candidate is None:
        return EditorLaunchResult(
            opened=False,
            status="no-editor-candidate",
            command_display=None,
            warning=_join_warnings(list(selection.warnings)),
        )

    command_display = _format_command(selection.candidate.command_argv)
    opener = SubprocessEditorOpener(
        command_argv=selection.candidate.command_argv,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    open_result = opener.open_path(config_path)

    warnings = list(selection.warnings)
    if open_result.warning:
        warnings.append(open_result.warning)

    if open_result.opened:
        return EditorLaunchResult(
            opened=True,
            status="opened",
            command_display=command_display,
            warning=_join_warnings(warnings),
        )

    joined_warning = _join_warnings(warnings)
    lowered = (joined_warning or "").lower()
    if "no such file or directory" in lowered or "not found" in lowered:
        status = "command-unavailable"
    else:
        status = "launch-failed"

    return EditorLaunchResult(
        opened=False,
        status=status,
        command_display=command_display,
        warning=joined_warning,
    )
