from __future__ import annotations

from dataclasses import dataclass
import platform
import shutil
import subprocess
import sys
from typing import Protocol


SUPPORTED_DELIVERY_VALUES = (
    "stdout",
    "file-only",
    "clipboard",
    "clipboard+stdout",
)


class ClipboardDeliveryError(Exception):
    """Raised when clipboard copy fails in platform delivery code."""


class DeliveryAdapter(Protocol):
    def deliver(self, invocation_text: str) -> None:
        pass


class ClipboardWriter(Protocol):
    def copy_text(self, text: str) -> None:
        pass


@dataclass(frozen=True)
class StdoutDeliveryAdapter:
    def deliver(self, invocation_text: str) -> None:
        print(invocation_text)


@dataclass(frozen=True)
class FileOnlyDeliveryAdapter:
    def deliver(self, invocation_text: str) -> None:
        _ = invocation_text


@dataclass(frozen=True)
class PlatformClipboardWriter:
    timeout_seconds: float = 5.0

    def _commands_for_platform(self) -> list[list[str]]:
        system = platform.system()
        if system == "Linux":
            return [
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]

        if system == "Darwin":
            return [["pbcopy"]]

        if system == "Windows":
            return [
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::In.ReadToEnd() | Set-Clipboard",
                ],
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::In.ReadToEnd() | Set-Clipboard",
                ],
            ]

        return []

    def _resolve_command(self) -> list[str]:
        commands = self._commands_for_platform()
        if not commands:
            raise ClipboardDeliveryError(
                f"Clipboard delivery is not supported on platform: {platform.system()}."
            )

        for command in commands:
            executable = command[0]
            if shutil.which(executable):
                return command

        candidate_names = ", ".join(command[0] for command in commands)
        raise ClipboardDeliveryError(
            "No supported clipboard command is available. "
            f"Tried: {candidate_names}."
        )

    def copy_text(self, text: str) -> None:
        command = self._resolve_command()
        try:
            completed = subprocess.run(
                command,
                input=text,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClipboardDeliveryError(
                f"Clipboard command timed out after {self.timeout_seconds} seconds: {command[0]}"
            ) from exc
        except OSError as exc:
            raise ClipboardDeliveryError(
                f"Failed to start clipboard command {command[0]}: {exc}"
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            detail = stderr if stderr else f"exit code {completed.returncode}"
            raise ClipboardDeliveryError(
                f"Clipboard command {command[0]} failed: {detail}"
            )


@dataclass(frozen=True)
class ClipboardDeliveryAdapter:
    clipboard_writer: ClipboardWriter
    echo_stdout: bool
    fallback_to_stdout: bool

    def deliver(self, invocation_text: str) -> None:
        if self.echo_stdout:
            print(invocation_text)

        try:
            self.clipboard_writer.copy_text(invocation_text)
            print("Invocation copied to clipboard.")
            return
        except ClipboardDeliveryError as exc:
            if self.echo_stdout:
                print(
                    "Warning: clipboard delivery failed; invocation was still written to stdout: "
                    f"{exc}",
                    file=sys.stderr,
                )
                return

            if self.fallback_to_stdout:
                print(f"Warning: clipboard delivery failed: {exc}", file=sys.stderr)
                print("Clipboard delivery failed; falling back to stdout.")
                print(invocation_text)
                return

            raise


def build_delivery_adapter(
    delivery_mode: str,
    *,
    clipboard_writer: ClipboardWriter | None = None,
) -> DeliveryAdapter:
    if delivery_mode == "stdout":
        return StdoutDeliveryAdapter()

    if delivery_mode == "file-only":
        return FileOnlyDeliveryAdapter()

    if delivery_mode == "clipboard":
        effective_clipboard_writer = clipboard_writer or PlatformClipboardWriter()
        return ClipboardDeliveryAdapter(
            clipboard_writer=effective_clipboard_writer,
            echo_stdout=False,
            fallback_to_stdout=True,
        )

    if delivery_mode == "clipboard+stdout":
        effective_clipboard_writer = clipboard_writer or PlatformClipboardWriter()
        return ClipboardDeliveryAdapter(
            clipboard_writer=effective_clipboard_writer,
            echo_stdout=True,
            fallback_to_stdout=False,
        )

    raise ValueError(
        f"Unsupported delivery mode: {delivery_mode}. Supported modes: {', '.join(SUPPORTED_DELIVERY_VALUES)}."
    )
