#!/usr/bin/env python3
"""
Cross-platform clipboard wrapper for the canonical Flow report.

Obtains the exact canonical report and copies it to the system clipboard
using platform-native backends (Windows, Linux, or OSC 52).
"""
from __future__ import annotations

import base64
import ctypes
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
    """Windows clipboard backend using ctypes with proper UTF-16LE encoding."""

    name = "Windows clipboard (ctypes)"

    @staticmethod
    def _get_winapi():
        """Return the default Windows Win32 API handles."""
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = ctypes.c_bool

        return type("WinApi", (), {"kernel32": kernel32, "user32": user32})()

    @staticmethod
    def copy(content: str, api: object | None = None) -> bool:
        """Copy content to Windows clipboard."""
        try:
            api = api or WindowsClipboard._get_winapi()
            kernel32 = api.kernel32
            user32 = api.user32

            # Encode as UTF-16LE with a two-byte null terminator.
            text_utf16 = content.encode("utf-16-le")
            text_len = len(text_utf16) + 2

            hglob = kernel32.GlobalAlloc(0x0002, text_len)
            if not hglob:
                return False

            lpglob = kernel32.GlobalLock(hglob)
            if not lpglob:
                kernel32.GlobalFree(hglob)
                return False

            ctypes.memmove(lpglob, text_utf16, len(text_utf16))
            ctypes.memmove(lpglob + len(text_utf16), b"\x00\x00", 2)
            kernel32.GlobalUnlock(hglob)

            if not user32.OpenClipboard(None):
                kernel32.GlobalFree(hglob)
                return False

            try:
                if not user32.EmptyClipboard():
                    kernel32.GlobalFree(hglob)
                    return False

                CF_UNICODETEXT = 13
                result = user32.SetClipboardData(CF_UNICODETEXT, hglob)
                if not result:
                    kernel32.GlobalFree(hglob)
                    return False
                return True
            finally:
                user32.CloseClipboard()
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
    # Operational policy, not a proven renderer-derived maximum:
    # realistic Flow reports land around 3-15 KB, and we fail closed above this bound
    # without truncation so the printable recovery output remains recoverable.
    MAX_OSC52_BYTES = 16384

    @staticmethod
    def copy(content: str, control_stream=None) -> bool:
        """Copy content via OSC 52 control sequence."""
        close_stream = False
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

            if control_stream is None:
                control_stream = open("/dev/tty", "wb")
                close_stream = True
            control_stream.write(osc52_sequence.encode("utf-8"))
            control_stream.flush()

            return True
        except Exception:
            return False
        finally:
            if close_stream:
                control_stream.close()


def _get_ai_dev_root() -> Path | None:
    """
    Find the AI Dev repository root by traversing up from this script.

    Returns:
        Path to the AI Dev root, or None if not found.
    """
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    # Traverse up at most 5 levels (scripts/ -> flow/ -> copilot/ -> skills/ -> ai-dev/ -> possible parent)
    for _ in range(5):
        if (current / "ai_dev_flow").exists():
            return current
        current = current.parent
    return None


def get_canonical_report(repo_root: Path | None = None, original_cwd: Path | None = None) -> str | None:
    """
    Obtain the canonical report from the existing Python renderer.

    The report is generated using the current Python interpreter and the AI Dev CLI,
    executed from the caller's original working directory (not the AI Dev repo).

    Args:
        repo_root: Repository root for report context. If None, uses cwd.
        original_cwd: The working directory to use for execution.
                      If None, uses the current working directory.

    Returns:
        The canonical report content, or None if unavailable.
    """
    try:
        if repo_root is None:
            repo_root = Path.cwd()

        if original_cwd is None:
            original_cwd = Path.cwd()

        # Find AI Dev root for module lookup
        ai_dev_root = _get_ai_dev_root()
        if not ai_dev_root:
            return None

        python_executable = sys.executable
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        parts = [str(ai_dev_root)]
        if existing_pythonpath:
            parts.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(p for p in parts if p)

        result = subprocess.run(
            [python_executable, "-m", "ai_dev_flow.cli", "__ai_dev_flow_exec__", "report"],
            cwd=str(original_cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        stdout_raw = result.stdout
        stderr_raw = result.stderr
        stdout = stdout_raw.decode("utf-8") if isinstance(stdout_raw, (bytes, bytearray)) else (stdout_raw or "")
        stderr = stderr_raw.decode("utf-8", errors="replace") if isinstance(stderr_raw, (bytes, bytearray)) else (stderr_raw or "")
        if result.returncode != 0:
            return None

        return stdout
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


def copy_report_to_clipboard(
    repo_root: Path | None = None,
    original_cwd: Path | None = None,
    backends: list[Callable[[str], bool]] | None = None,
    report: str | None = None,
) -> tuple[bool, str]:
    """
    Copy the canonical report to the clipboard.

    The caller's original working directory is preserved for report generation.

    Args:
        repo_root: The repository root directory (defaults to cwd).
        original_cwd: The caller's original working directory (defaults to cwd).
        backends: List of clipboard backend functions to try (for testing).

    Returns:
        Tuple of (success: bool, message: str).
        On failure, the report is printed to stdout for manual recovery.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    if original_cwd is None:
        original_cwd = Path.cwd()

    if report is None:
        report = get_canonical_report(repo_root, original_cwd)
    if report is None:
        return False, "Error: Could not obtain canonical report. Ensure flow-report is available."

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
    report = get_canonical_report()
    if report is None:
        print("Error: Could not obtain canonical report. Ensure flow-report is available.", file=sys.stderr)
        return 1

    print(report, end="", file=sys.stdout)
    success, message = copy_report_to_clipboard(report=report)

    if success:
        print(message, file=sys.stderr)
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
