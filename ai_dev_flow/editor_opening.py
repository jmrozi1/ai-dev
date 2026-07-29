from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
import os
import platform
import shlex
import shutil
import subprocess


DEFAULT_EDITOR_WAIT_TIMEOUT_SECONDS = 2.0
TERMINAL_EDITOR_BASENAMES = frozenset({"vi", "vim", "nvim", "nano", "emacs"})


@dataclass(frozen=True)
class EditorOpenResult:
    opened: bool
    warning: str | None = None


@dataclass(frozen=True)
class EditorCommandResolution:
    command_argv: list[str] | None
    source: str | None
    warnings: tuple[str, ...] = ()


class EditorOpener(Protocol):
    def open_path(self, target_path: Path) -> EditorOpenResult:
        pass


@dataclass(frozen=True)
class NullEditorOpener:
    warnings: tuple[str, ...]

    def open_path(self, target_path: Path) -> EditorOpenResult:
        _ = target_path
        joined = "; ".join(self.warnings) if self.warnings else "No editor command is available on this machine."
        return EditorOpenResult(opened=False, warning=joined)


def _command_requests_wait(command_argv: list[str]) -> bool:
    wait_flags = {"--wait", "-w", "/wait", "/w"}
    for token in command_argv[1:]:
        if token.lower() in wait_flags:
            return True
    return False


def _is_terminal_editor(executable: str) -> bool:
    basename = Path(executable).name.lower()
    if basename.endswith(".exe"):
        basename = basename[:-4]
    return basename in TERMINAL_EDITOR_BASENAMES


def _parse_command(command_text: str) -> list[str]:
    normalized = command_text.strip()
    if not normalized:
        raise ValueError("command cannot be empty")

    # Keep parsing shell-safe while respecting Windows quoting rules.
    command = shlex.split(normalized, posix=(os.name != "nt"))
    if not command:
        raise ValueError("command cannot be empty")
    return command


def _command_available(executable: str) -> bool:
    if any(separator in executable for separator in ("/", "\\")):
        return Path(executable).exists()
    return shutil.which(executable) is not None


def resolve_editor_command(
    configured_command: str | None,
    *,
    env: Mapping[str, str] | None = None,
    system_name: str | None = None,
) -> EditorCommandResolution:
    environment = env if env is not None else os.environ
    current_system = system_name or platform.system()
    warnings: list[str] = []

    if configured_command is not None:
        try:
            configured_argv = _parse_command(configured_command)
        except ValueError as exc:
            warnings.append(f"Invalid configured editor.command: {exc}.")
        else:
            if _command_available(configured_argv[0]):
                return EditorCommandResolution(
                    command_argv=configured_argv,
                    source="editor.command",
                    warnings=tuple(warnings),
                )

            warnings.append(
                f"Configured editor command is not available: {configured_argv[0]}."
            )

    for environment_key in ("VISUAL", "EDITOR"):
        value = environment.get(environment_key, "").strip()
        if not value:
            continue

        try:
            candidate = _parse_command(value)
        except ValueError as exc:
            warnings.append(f"Invalid {environment_key} command: {exc}.")
            continue

        if _command_available(candidate[0]):
            return EditorCommandResolution(
                command_argv=candidate,
                source=environment_key,
                warnings=tuple(warnings),
            )

        warnings.append(f"{environment_key} command is not available: {candidate[0]}.")

    platform_fallback = "notepad" if current_system == "Windows" else "vi"
    if _command_available(platform_fallback):
        return EditorCommandResolution(
            command_argv=[platform_fallback],
            source="platform-fallback",
            warnings=tuple(warnings),
        )

    warnings.append("No editor command is available on this machine.")
    return EditorCommandResolution(
        command_argv=None,
        source=None,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class SubprocessEditorOpener:
    command_argv: list[str]
    wait_timeout_seconds: float = DEFAULT_EDITOR_WAIT_TIMEOUT_SECONDS
    resolution_warnings: tuple[str, ...] = ()

    def open_path(self, target_path: Path) -> EditorOpenResult:
        command = [*self.command_argv, str(target_path)]

        if _is_terminal_editor(self.command_argv[0]):
            try:
                process = subprocess.Popen(command)  # noqa: S603
            except OSError as exc:
                warnings = list(self.resolution_warnings)
                warnings.append(f"Failed to launch editor command {self.command_argv[0]}: {exc}")
                return EditorOpenResult(opened=False, warning="; ".join(warnings))

            returncode = process.wait()
            if returncode == 0:
                warning_text = "; ".join(self.resolution_warnings) if self.resolution_warnings else None
                return EditorOpenResult(opened=True, warning=warning_text)

            warnings = list(self.resolution_warnings)
            warnings.append(f"Editor command {self.command_argv[0]} failed: exit code {returncode}")
            return EditorOpenResult(opened=False, warning="; ".join(warnings))

        try:
            process = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            warnings = list(self.resolution_warnings)
            warnings.append(f"Failed to launch editor command {self.command_argv[0]}: {exc}")
            return EditorOpenResult(
                opened=False,
                warning="; ".join(warnings),
            )

        if not _command_requests_wait(self.command_argv):
            warning_text = "; ".join(self.resolution_warnings) if self.resolution_warnings else None
            return EditorOpenResult(opened=True, warning=warning_text)

        try:
            returncode = process.wait(timeout=self.wait_timeout_seconds)
        except subprocess.TimeoutExpired:
            # Treat timeout as successful launch for explicit wait-based editors.
            warning_text = "; ".join(self.resolution_warnings) if self.resolution_warnings else None
            return EditorOpenResult(opened=True, warning=warning_text)

        if returncode == 0:
            warning_text = "; ".join(self.resolution_warnings) if self.resolution_warnings else None
            return EditorOpenResult(opened=True, warning=warning_text)

        warnings = list(self.resolution_warnings)
        warnings.append(f"Editor command {self.command_argv[0]} failed: exit code {returncode}")
        return EditorOpenResult(
            opened=False,
            warning="; ".join(warnings),
        )


def build_editor_opener(
    configured_command: str | None,
    *,
    env: Mapping[str, str] | None = None,
    system_name: str | None = None,
    wait_timeout_seconds: float = DEFAULT_EDITOR_WAIT_TIMEOUT_SECONDS,
) -> EditorOpener:
    resolution = resolve_editor_command(
        configured_command,
        env=env,
        system_name=system_name,
    )
    if resolution.command_argv is None:
        return NullEditorOpener(warnings=resolution.warnings)

    return SubprocessEditorOpener(
        command_argv=resolution.command_argv,
        wait_timeout_seconds=wait_timeout_seconds,
        resolution_warnings=resolution.warnings,
    )
