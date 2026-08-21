#!/usr/bin/env python3
"""
Cross-platform clipboard wrapper for the canonical Flow report.

Obtains the exact canonical report and copies it to the system clipboard
using platform-native backends (Windows, Linux, or OSC 52).
"""
from __future__ import annotations

import base64
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Protocol


class ClipboardBackend(Protocol):
    """Protocol for clipboard backend functions."""

    def copy(self, content: str) -> bool:
        """Copy content to clipboard. Returns True on success."""
        ...

    @property
    def name(self) -> str:
        """Name of the backend for diagnostics."""
        ...


class WindowsClipboard:
    """Windows clipboard backend using ctypes."""

    name = "Windows clipboard (ctypes)"

    @staticmethod
    def copy(content: str) -> bool:
        """Copy content to Windows clipboard."""
        try:
            import ctypes

            # Allocate memory for the text
            text_bytes = content.encode("utf-8")
            text_len = len(text_bytes) + 1

            # Allocate global memory
            hglob = ctypes.windll.kernel32.GlobalAlloc(0x0002, text_len)
            if not hglob:
                return False

            # Lock and copy
            lpglob = ctypes.windll.kernel32.GlobalLock(hglob)
            if not lpglob:
                ctypes.windll.kernel32.GlobalFree(hglob)
                return False

            ctypes.memmove(lpglob, text_bytes, text_len - 1)
            ctypes.windll.kernel32.GlobalUnlock(hglob)

            # Open clipboard, set data, close
            if not ctypes.windll.user32.OpenClipboard(None):
                ctypes.windll.kernel32.GlobalFree(hglob)
                return False

            ctypes.windll.user32.EmptyClipboard()
            cf_text = 1  # CF_TEXT
            result = ctypes.windll.user32.SetClipboardData(cf_text, hglob)
            ctypes.windll.user32.CloseClipboard()

            return bool(result)
        except Exception:
            return False


class WlCopyBackend:
    """Linux wl-copy backend for Wayland."""

    name = "wl-copy (Wayland)"

    @staticmethod
    def copy(content: str) -> bool:
        """Copy content via wl-copy."""
        try:
            subprocess.run(
                ["wl-copy"],
                input=content.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False


class XclipBackend:
    """Linux xclip backend."""

    name = "xclip -selection clipboard"

    @staticmethod
    def copy(content: str) -> bool:
        """Copy content via xclip."""
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=content.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False


class XselBackend:
    """Linux xsel backend."""

    name = "xsel --clipboard --input"

    @staticmethod
    def copy(content: str) -> bool:
        """Copy content via xsel."""
        try:
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=content.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False


class OSC52Backend:
    """OSC 52 control sequence backend for terminal-based copying."""

    name = "OSC 52 (terminal)"
    # Safe byte limit for OSC 52: 8KB to account for terminal/multiplexer overhead
    MAX_OSC52_BYTES = 8192

    @staticmethod
    def copy(content: str) -> bool:
        """Copy content via OSC 52 control sequence."""
        try:
            content_bytes = content.encode("utf-8")

            # Check size limit
            if len(content_bytes) > OSC52Backend.MAX_OSC52_BYTES:
                return False

            # Encode: UTF-8 -> Base64
            encoded = base64.b64encode(content_bytes).decode("ascii")

            # OSC 52 control sequence: ESC ] 52 ; c ; <base64> ST
            # Where ST is either BEL (\a) or ESC \ (recommended)
            osc52_sequence = f"\x1b]52;c;{encoded}\x1b\\"

            # Write to terminal
            sys.stdout.buffer.write(osc52_sequence.encode("utf-8"))
            sys.stdout.buffer.flush()

            return True
        except Exception:
            return False


def get_canonical_report(repo_root: Path) -> str | None:
    """
    Obtain the canonical report from the existing renderer.
    
    Args:
        repo_root: The repository root directory.
        
    Returns:
        The canonical report content, or None if unavailable.
    """
    try:
        # Resolve the flow-report script relative to this script's location
        script_dir = Path(__file__).parent.resolve()
        report_script = script_dir / "flow-report"

        if not report_script.exists():
            return None

        # Run the canonical report renderer
        result = subprocess.run(
            [str(report_script)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Preserve exact output including final newline if present
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def get_clipboard_backends() -> list[Callable[[str], bool]]:
    """Get platform-specific clipboard backends in priority order."""
    backends: list[Callable[[str], bool]] = []

    system = platform.system()

    if system == "Windows":
        backends.append(WindowsClipboard.copy)
    elif system == "Darwin":
        # macOS: pbcopy
        if shutil.which("pbcopy"):
            def pbcopy_backend(content: str) -> bool:
                try:
                    subprocess.run(
                        ["pbcopy"],
                        input=content.encode("utf-8"),
                        check=True,
                        timeout=2,
                    )
                    return True
                except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                    return False

            backends.append(pbcopy_backend)
    else:
        # Linux: try native backends in priority order
        backends.append(WlCopyBackend.copy)
        backends.append(XclipBackend.copy)
        backends.append(XselBackend.copy)

    # OSC 52 as fallback for all platforms
    backends.append(OSC52Backend.copy)

    return backends


def get_backend_names() -> list[str]:
    """Get names of all available backends for diagnostics."""
    names: list[str] = []
    system = platform.system()

    if system == "Windows":
        names.append(WindowsClipboard.name)
    elif system == "Darwin":
        if shutil.which("pbcopy"):
            names.append("pbcopy (macOS)")
    else:
        names.append(WlCopyBackend.name)
        names.append(XclipBackend.name)
        names.append(XselBackend.name)

    names.append(OSC52Backend.name)
    return names


def copy_report_to_clipboard(repo_root: Path | None = None, backends: list[Callable[[str], bool]] | None = None) -> tuple[bool, str]:
    """
    Copy the canonical report to the clipboard.

    Args:
        repo_root: The repository root directory (defaults to cwd).
        backends: List of clipboard backend functions to try (for testing).

    Returns:
        Tuple of (success: bool, message: str).
    """
    if repo_root is None:
        repo_root = Path.cwd()

    # Get the canonical report
    report = get_canonical_report(repo_root)
    if report is None:
        return False, "Error: Could not obtain canonical report. Ensure flow-report is available."

    # Check OSC 52 size limit early if it's the only backend
    if backends is None:
        backends = get_clipboard_backends()

    # Try each backend
    for backend in backends:
        try:
            if backend(report):
                # Determine which backend was used
                backend_name = getattr(backend, "__name__", "clipboard")
                if hasattr(backend, "__self__"):
                    backend_name = backend.__self__.name
                elif backend is OSC52Backend.copy:
                    backend_name = OSC52Backend.name
                elif backend is WlCopyBackend.copy:
                    backend_name = WlCopyBackend.name
                elif backend is XclipBackend.copy:
                    backend_name = XclipBackend.name
                elif backend is XselBackend.copy:
                    backend_name = XselBackend.name
                elif backend is WindowsClipboard.copy:
                    backend_name = WindowsClipboard.name
                else:
                    backend_name = "clipboard"

                return True, f"Report copied to {backend_name}."
        except Exception:
            continue

    # All backends failed
    available_backends = get_backend_names()
    system = platform.system()

    if system == "Windows":
        guidance = (
            "Windows clipboard requires ctypes support (usually built-in).\n"
            "Ensure your Python installation includes the ctypes module."
        )
    elif system == "Darwin":
        guidance = "macOS requires pbcopy (included in standard installs)."
    else:
        guidance = (
            "Linux requires one of: wl-copy (Wayland), xclip, xsel, or a VS Code terminal supporting OSC 52.\n"
            f"Tried: {', '.join(available_backends)}\n"
            "Install one of these tools or run in a compatible VS Code integrated terminal."
        )

    return False, f"Error: All clipboard backends failed.\n{guidance}"


def main() -> int:
    """Main entry point."""
    success, message = copy_report_to_clipboard()

    if success:
        print(message, file=sys.stdout)
        return 0
    else:
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
