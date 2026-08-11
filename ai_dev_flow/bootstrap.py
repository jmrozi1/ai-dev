from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
from typing import TextIO

from .json_files import JsonFileError, load_json_object, write_json_object_atomic, write_text_atomic


OWNERSHIP_MARKER = "AI_DEV_LAUNCHER_V1"
LEGACY_MANAGED_LAUNCHER_MARKER = "AI_DEV_MANAGED_LAUNCHER_V1"
CANONICAL_COMMAND_NAME = "ai-dev"
_PREFIX_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")
_DIRECT_FLOW_ROUTE_TOKEN = "__ai_dev_flow_exec__"
PREFIX_LAUNCHER_OWNERSHIP_VERSION = 1
_SYMLINK_OWNERSHIP_PREFIX = "symlink:"
OBSOLETE_FIXED_FLOW_EXECUTABLE_COMMANDS = frozenset({"review"})
LEGACY_RETIRED_FLOW_LAUNCHER_NAMES = (
    "flow",
    "flow-help",
    "flow-review",
    "flow-task-prepare",
)


class BootstrapError(Exception):
    """Raised for bootstrap installation failures."""


@dataclass(frozen=True)
class LauncherInstallStatus:
    path: Path
    state: str


@dataclass(frozen=True)
class BootstrapResult:
    platform: str
    command_name: str
    repo_root: Path
    python_executable: Path
    install_directory: Path
    launcher_statuses: tuple[LauncherInstallStatus, ...]
    install_dir_on_path: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _SnapshotEntry:
    exists: bool
    text: str
    mode: int | None


@dataclass(frozen=True)
class _CollisionValidationResult:
    snapshots: dict[Path, _SnapshotEntry]
    force_replaced_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _PrefixLauncherOwnershipRecord:
    version: int
    selected_prefix: str
    platform: str
    install_directory: str
    owned_launchers: dict[str, str]


def _quote_posix_single(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell_single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _escape_cmd_value(value: str) -> str:
    return value.replace("%", "%%")


def _canonical_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_owned_posix_launcher_text(text: str) -> bool:
    lines = _canonical_newlines(text).split("\n")
    return len(lines) >= 2 and lines[0] == "#!/usr/bin/env sh" and lines[1] == f"# {OWNERSHIP_MARKER}"


def _is_owned_powershell_launcher_text(text: str) -> bool:
    lines = _canonical_newlines(text).split("\n")
    return len(lines) >= 1 and lines[0] == f"# {OWNERSHIP_MARKER}"


def _is_owned_cmd_launcher_text(text: str) -> bool:
    lines = _canonical_newlines(text).split("\n")
    return (
        len(lines) >= 2
        and lines[0].lower() == "@echo off"
        and lines[1] == f":: {OWNERSHIP_MARKER}"
    )


def _validate_command_name(command_name: str) -> None:
    if command_name == CANONICAL_COMMAND_NAME:
        return
    raise BootstrapError(
        "Unsupported command name for bootstrap: "
        f"{command_name!r}. Expected exactly {CANONICAL_COMMAND_NAME!r}."
    )


def _validate_prefix(prefix: str) -> str:
    cleaned = prefix.strip()
    if cleaned != prefix or not cleaned:
        raise BootstrapError("Invalid prefix: value must be non-empty and may not include surrounding whitespace.")

    if not _PREFIX_PATTERN.fullmatch(cleaned):
        raise BootstrapError(
            "Invalid prefix. Use only letters, numbers, and hyphen, with no path separators or metacharacters."
        )

    return cleaned


def _fixed_flow_launcher_commands() -> tuple[str, ...]:
    # Import locally to avoid bootstrap/cli import-order coupling.
    from .cli import FIXED_FLOW_EXECUTABLE_COMMANDS

    return FIXED_FLOW_EXECUTABLE_COMMANDS


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_module_for_platform(platform: str):
    return ntpath if platform == "windows" else posixpath


def _normalize_path_text_for_platform(path_text: str, *, platform: str, casefold: bool = False) -> str:
    path_module = _path_module_for_platform(platform)
    normalized = path_module.normpath(path_text)
    if casefold and platform == "windows":
        return ntpath.normcase(normalized)
    return normalized


def _pure_path_for_platform(path_text: str, *, platform: str) -> PureWindowsPath | PurePosixPath:
    if platform == "windows":
        return PureWindowsPath(path_text)
    return PurePosixPath(path_text)


def _is_normalized_path_text(path_text: str, *, platform: str) -> bool:
    return _normalize_path_text_for_platform(path_text, platform=platform) == path_text


def _validate_recorded_launcher_path(
    *,
    record_path: Path,
    platform: str,
    selected_prefix: str,
    install_directory_text: str,
    launcher_path_text: str,
) -> str:
    launcher_path = _pure_path_for_platform(launcher_path_text, platform=platform)
    if not launcher_path.is_absolute():
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {record_path}: launcher paths must be absolute."
        )
    if not _is_normalized_path_text(launcher_path_text, platform=platform):
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {record_path}: launcher paths must be normalized."
        )

    install_directory = _pure_path_for_platform(install_directory_text, platform=platform)
    launcher_parent = str(launcher_path.parent)
    expected_parent = str(install_directory)
    same_parent = (
        _normalize_path_text_for_platform(launcher_parent, platform=platform, casefold=True)
        == _normalize_path_text_for_platform(expected_parent, platform=platform, casefold=True)
    )
    if not same_parent:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {record_path}: launcher path {launcher_path} "
            "must be a direct child of install_directory."
        )

    fixed_commands = set(_fixed_flow_launcher_commands())
    accepted_record_commands = fixed_commands | OBSOLETE_FIXED_FLOW_EXECUTABLE_COMMANDS
    windows_command_names = {command.lower() for command in fixed_commands}
    windows_accepted_command_names = {command.lower() for command in accepted_record_commands}
    name = launcher_path.name
    base_name = name
    if platform == "posix":
        if name.endswith(".cmd") or name.endswith(".ps1"):
            raise BootstrapError(
                f"Invalid prefix launcher ownership record in {record_path}: POSIX launcher {name} "
                "must not include an extension."
            )
    else:
        lower_name = name.lower()
        if lower_name.endswith(".cmd"):
            base_name = name[:-4]
        elif lower_name.endswith(".ps1"):
            base_name = name[:-4]
        else:
            raise BootstrapError(
                f"Invalid prefix launcher ownership record in {record_path}: Windows launcher {name} "
                "must end with .cmd or .ps1."
            )

    expected_prefix = f"{selected_prefix}-"
    if platform == "windows":
        has_expected_prefix = base_name.lower().startswith(expected_prefix.lower())
    else:
        has_expected_prefix = base_name.startswith(expected_prefix)
    if not has_expected_prefix:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {record_path}: launcher {name} "
            f"does not match selected_prefix {selected_prefix!r}."
        )

    command = base_name[len(expected_prefix) :]
    if platform == "windows":
        command_known = command.lower() in windows_accepted_command_names
    else:
        command_known = command in accepted_record_commands
    if not command_known:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {record_path}: launcher {name} "
            f"references unknown fixed command {command!r}."
        )

    return str(launcher_path)


def resolve_prefix_launcher_ownership_path(
    *,
    os_name: str | None = None,
    home: Path | None = None,
    appdata: str | None = None,
    xdg_config_home: str | None = None,
) -> Path:
    resolved_os = os.name if os_name is None else os_name
    resolved_home = Path.home() if home is None else home
    resolved_home = resolved_home.expanduser().resolve()

    resolved_appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    resolved_xdg = xdg_config_home if xdg_config_home is not None else os.environ.get("XDG_CONFIG_HOME")

    if resolved_os == "nt":
        appdata_text = (resolved_appdata or "").strip()
        if appdata_text:
            base_dir = Path(appdata_text).expanduser() / "ai-dev"
        else:
            base_dir = resolved_home / "AppData" / "Roaming" / "ai-dev"
    else:
        xdg_text = (resolved_xdg or "").strip()
        if xdg_text:
            base_dir = Path(xdg_text).expanduser() / "ai-dev"
        else:
            base_dir = resolved_home / ".config" / "ai-dev"

    return base_dir / "prefixed-launcher-ownership.json"


def resolve_legacy_installation_manifest_path(
    *,
    os_name: str | None = None,
    home: Path | None = None,
    appdata: str | None = None,
    xdg_config_home: str | None = None,
) -> Path:
    return resolve_prefix_launcher_ownership_path(
        os_name=os_name,
        home=home,
        appdata=appdata,
        xdg_config_home=xdg_config_home,
    ).with_name("installation-manifest.json")


def _load_legacy_managed_launchers(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        raw = load_json_object(path, missing_default={})
    except JsonFileError:
        return {}

    if not isinstance(raw, dict):
        return {}

    managed_launchers = raw.get("managed_launchers")
    if not isinstance(managed_launchers, dict):
        return {}

    digests: dict[str, str] = {}
    for path_text, digest in managed_launchers.items():
        if not isinstance(path_text, str) or not path_text:
            continue
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            continue
        digests[path_text] = digest

    return digests


def _matches_legacy_retired_flow_launcher_shape(
    *,
    launcher_name: str,
    text: str,
) -> bool:
    expected_exec_line_by_name = {
        "flow": "exec 'ai-dev' 'flow' \"$@\"",
        "flow-help": "exec 'ai-dev' 'flow' '--help' \"$@\"",
        "flow-review": "exec 'ai-dev' 'flow' 'review' \"$@\"",
        "flow-task-prepare": "exec 'ai-dev' 'flow' 'task-prepare' \"$@\"",
    }

    expected_exec_line = expected_exec_line_by_name.get(launcher_name)
    if expected_exec_line is None:
        return False

    lines = _canonical_newlines(text).splitlines()
    if len(lines) < 4:
        return False

    if lines[0] != "#!/usr/bin/env sh":
        return False
    if lines[1] != f"# {LEGACY_MANAGED_LAUNCHER_MARKER}":
        return False
    if "set -eu" not in lines:
        return False
    return expected_exec_line in lines


def _load_prefix_launcher_ownership(path: Path) -> _PrefixLauncherOwnershipRecord | None:
    if not path.exists():
        return None

    try:
        raw = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise BootstrapError(str(exc)) from exc

    if not isinstance(raw, dict):
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: expected object.")

    version = raw.get("version")
    if version != PREFIX_LAUNCHER_OWNERSHIP_VERSION:
        raise BootstrapError(
            f"Unsupported prefix launcher ownership record version in {path}: {version!r}."
        )

    selected_prefix_raw = raw.get("selected_prefix")
    if not isinstance(selected_prefix_raw, str) or not selected_prefix_raw:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: selected_prefix must be non-empty string."
        )
    selected_prefix = _validate_prefix(selected_prefix_raw)

    platform = raw.get("platform")
    if platform not in {"posix", "windows"}:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: platform must be 'posix' or 'windows'."
        )

    install_directory_text = raw.get("install_directory")
    if not isinstance(install_directory_text, str) or not install_directory_text:
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: install_directory must be non-empty string."
        )
    if not _is_normalized_path_text(install_directory_text, platform=platform):
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: install_directory must be normalized."
        )

    install_directory = _pure_path_for_platform(install_directory_text, platform=platform)
    if not install_directory.is_absolute():
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: install_directory must be absolute."
        )

    owned_launchers_raw = raw.get("owned_launchers")
    if not isinstance(owned_launchers_raw, dict):
        raise BootstrapError(
            f"Invalid prefix launcher ownership record in {path}: owned_launchers must be an object."
        )

    owned_launchers: dict[str, str] = {}
    for path_text, digest in owned_launchers_raw.items():
        if not isinstance(path_text, str) or not path_text:
            raise BootstrapError(
                f"Invalid prefix launcher ownership record in {path}: launcher path keys must be non-empty strings."
            )
        if not isinstance(digest, str):
            raise BootstrapError(
                f"Invalid prefix launcher ownership record in {path}: digest must be a string."
            )
        if not (
            re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            or digest.startswith(_SYMLINK_OWNERSHIP_PREFIX)
        ):
            raise BootstrapError(
                f"Invalid prefix launcher ownership record in {path}: digest must be lowercase SHA-256 or symlink marker."
            )
        launcher_path = _validate_recorded_launcher_path(
            record_path=path,
            platform=platform,
            selected_prefix=selected_prefix,
            install_directory_text=str(install_directory),
            launcher_path_text=path_text,
        )
        owned_launchers[launcher_path] = digest

    return _PrefixLauncherOwnershipRecord(
        version=version,
        selected_prefix=selected_prefix,
        platform=platform,
        install_directory=str(install_directory),
        owned_launchers=dict(sorted(owned_launchers.items(), key=lambda item: str(item[0]))),
    )


def _save_prefix_launcher_ownership(
    path: Path,
    record: _PrefixLauncherOwnershipRecord,
) -> None:
    payload = {
        "version": record.version,
        "selected_prefix": record.selected_prefix,
        "platform": record.platform,
        "install_directory": record.install_directory,
        "owned_launchers": {
            launcher_path: digest
            for launcher_path, digest in sorted(
                record.owned_launchers.items(),
                key=lambda item: str(item[0]),
            )
        },
    }
    try:
        write_json_object_atomic(path, payload)
    except JsonFileError as exc:
        raise BootstrapError(str(exc)) from exc


def _is_interactive_input(stream: TextIO) -> bool:
    is_tty = getattr(stream, "isatty", None)
    if is_tty is None:
        return False
    try:
        return bool(is_tty())
    except Exception:
        return False


def _confirm_replace_conflicting_launcher(
    *,
    path: Path,
    force: bool,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool | None,
    emit_force_notice: bool,
) -> bool:
    if force:
        if emit_force_notice:
            print(
                f"bootstrap: Force-replacing conflicting launcher at {path}.",
                file=output_stream,
            )
        return True

    interactive_mode = _is_interactive_input(input_stream) if interactive is None else interactive
    if not interactive_mode:
        raise BootstrapError(
            "Cannot prompt to replace conflicting launcher at "
            f"{path} in noninteractive mode. Rerun with --force to replace it."
        )

    print(
        "An existing non-AI-Dev or modified launcher was found at:\n"
        f"  {path}\n\n"
        "Replace it? [y/N] ",
        end="",
        file=output_stream,
    )
    response = input_stream.readline()
    normalized = response.strip().lower()
    if normalized in {"y", "yes"}:
        return False

    raise BootstrapError(
        f"Preserved conflicting launcher at {path}; installation aborted."
    )


def _normalize_path(path_text: str, *, windows: bool) -> str:
    if windows:
        cleaned = path_text.strip().strip('"').strip("'").rstrip("\\/")
        return ntpath.normcase(ntpath.normpath(cleaned))
    return posixpath.normpath(path_text.strip())


def _paths_equal(left: Path, right: Path, *, windows: bool) -> bool:
    return _normalize_path(str(left), windows=windows) == _normalize_path(
        str(right), windows=windows
    )


def _is_path_on_path(
    directory: Path,
    *,
    path_value: str,
    windows: bool,
) -> bool:
    delimiter = ";" if windows else ":"
    target = _normalize_path(str(directory), windows=windows)
    for raw_entry in path_value.split(delimiter):
        if not raw_entry.strip():
            continue
        candidate = _normalize_path(raw_entry, windows=windows)
        if candidate == target:
            return True
    return False


def _prepend_pythonpath(repo_root: Path, env: dict[str, str]) -> dict[str, str]:
    updated = dict(env)
    existing = updated.get("PYTHONPATH", "")
    root_text = str(repo_root)
    if existing:
        updated["PYTHONPATH"] = f"{root_text}{os.path.pathsep}{existing}"
    else:
        updated["PYTHONPATH"] = root_text
    return updated


def _resolve_python_interpreter(
    *,
    explicit: str | None,
    platform: str,
) -> Path:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
        resolved = shutil.which(explicit)
        if resolved:
            return Path(resolved).resolve()

        candidate_path = Path(explicit).expanduser()
        if candidate_path.exists() and candidate_path.is_file():
            return candidate_path.resolve()

        raise BootstrapError(
            f"Explicit Python interpreter was not found: {explicit}"
        )

    env_python = os.environ.get("AI_DEV_PYTHON", "").strip()
    if env_python and env_python not in candidates:
        candidates.append(env_python)

    if sys.executable and sys.executable not in candidates:
        candidates.append(sys.executable)

    if platform == "windows":
        if "python" not in candidates:
            candidates.append("python")
    else:
        if "python3" not in candidates:
            candidates.append("python3")
        if "python" not in candidates:
            candidates.append("python")

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return Path(resolved).resolve()

        candidate_path = Path(candidate).expanduser()
        if candidate_path.exists() and candidate_path.is_file():
            return candidate_path.resolve()

    if platform == "windows":
        raise BootstrapError(
            "Python 3 was not found. Set AI_DEV_PYTHON, or ensure python is available on PATH."
        )

    raise BootstrapError(
        "Python 3 was not found. Set AI_DEV_PYTHON, or ensure python3 is available on PATH."
    )


def _validate_python_runtime(python_executable: Path, repo_root: Path) -> None:
    env = _prepend_pythonpath(repo_root, os.environ)
    check_script = (
        "import sys\n"
        "if sys.version_info < (3, 8):\n"
        "    raise SystemExit(9)\n"
        "import ai_dev_flow.cli\n"
    )

    try:
        completed = subprocess.run(
            [str(python_executable), "-c", check_script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
    except OSError as exc:
        raise BootstrapError(
            f"Cannot execute Python interpreter {python_executable}: {exc}"
        ) from exc

    if completed.returncode == 0:
        return

    if completed.returncode == 9:
        raise BootstrapError(
            f"Python interpreter {python_executable} is too old. Python 3.8+ is required."
        )

    stderr = completed.stderr.strip()
    if not stderr:
        stderr = "Unknown error while importing ai_dev_flow.cli"
    raise BootstrapError(
        "Python runtime validation failed for "
        f"{python_executable}: {stderr}"
    )


def _validate_repo_root(repo_root: Path) -> None:
    if not repo_root.exists() or not repo_root.is_dir():
        raise BootstrapError(f"Repository root does not exist: {repo_root}")

    required_paths = (
        repo_root / "ai_dev_flow" / "__init__.py",
        repo_root / "ai_dev_flow" / "cli.py",
    )
    for required in required_paths:
        if not required.exists() or not required.is_file():
            raise BootstrapError(f"Required source file not found: {required}")


def render_posix_launcher(
    *,
    repo_root: Path,
    python_executable: Path,
    command_name: str,
    flow_direct_command: str | None = None,
) -> str:
    repo_q = _quote_posix_single(str(repo_root))
    py_q = _quote_posix_single(str(python_executable))
    command_q = _quote_posix_single(command_name)
    direct_exec_argv = ""
    if flow_direct_command is not None:
        direct_exec_argv = (
            f" {_quote_posix_single(_DIRECT_FLOW_ROUTE_TOKEN)}"
            f" {_quote_posix_single(flow_direct_command)}"
        )
    return (
        "#!/usr/bin/env sh\n"
        f"# {OWNERSHIP_MARKER}\n"
        "set -eu\n\n"
        f"REPO_ROOT={repo_q}\n"
        f"PYTHON_EXECUTABLE={py_q}\n"
        f"FLOW_COMMAND_NAME={command_q}\n"
        "export FLOW_COMMAND_NAME\n\n"
        "if [ -n \"${PYTHONPATH:-}\" ]; then\n"
        "    PYTHONPATH=\"${REPO_ROOT}:${PYTHONPATH}\"\n"
        "else\n"
        "    PYTHONPATH=\"${REPO_ROOT}\"\n"
        "fi\n"
        "export PYTHONPATH\n\n"
        f"exec \"${{PYTHON_EXECUTABLE}}\" -m ai_dev_flow.cli{direct_exec_argv} \"$@\"\n"
    )


def render_powershell_launcher(
    *,
    repo_root: Path,
    python_executable: Path,
    command_name: str,
    flow_direct_command: str | None = None,
) -> str:
    repo_q = _quote_powershell_single(str(repo_root))
    py_q = _quote_powershell_single(str(python_executable))
    name_q = _quote_powershell_single(command_name)
    direct_exec_argv = ""
    if flow_direct_command is not None:
        command_q = _quote_powershell_single(flow_direct_command)
        token_q = _quote_powershell_single(_DIRECT_FLOW_ROUTE_TOKEN)
        direct_exec_argv = f" {token_q} {command_q}"
    return (
        f"# {OWNERSHIP_MARKER}\n"
        "$previousErrorActionPreference = $ErrorActionPreference\n"
        "$ErrorActionPreference = 'Stop'\n\n"
        f"$repoRoot = {repo_q}\n"
        f"$pythonExecutable = {py_q}\n"
        "$hadFlowCommandName = Test-Path Env:FLOW_COMMAND_NAME\n"
        "$previousFlowCommandName = $env:FLOW_COMMAND_NAME\n"
        f"$env:FLOW_COMMAND_NAME = {name_q}\n"
        "$previousPythonPath = $env:PYTHONPATH\n\n"
        "if ([string]::IsNullOrEmpty($previousPythonPath)) {\n"
        "    $env:PYTHONPATH = $repoRoot\n"
        "}\n"
        "else {\n"
        "    $env:PYTHONPATH = \"$repoRoot$([System.IO.Path]::PathSeparator)$previousPythonPath\"\n"
        "}\n\n"
        "$downstreamExitCode = 1\n\n"
        "try {\n"
        f"    & $pythonExecutable -m ai_dev_flow.cli{direct_exec_argv} @args\n"
        "    if ($null -eq $LASTEXITCODE) {\n"
        "        $downstreamExitCode = 0\n"
        "    }\n"
        "    else {\n"
        "        $downstreamExitCode = [int]$LASTEXITCODE\n"
        "    }\n"
        "}\n"
        "finally {\n"
        "    $ErrorActionPreference = $previousErrorActionPreference\n"
        "    $env:PYTHONPATH = $previousPythonPath\n"
        "    if ($hadFlowCommandName) {\n"
        "        $env:FLOW_COMMAND_NAME = $previousFlowCommandName\n"
        "    }\n"
        "    else {\n"
        "        Remove-Item Env:FLOW_COMMAND_NAME -ErrorAction SilentlyContinue\n"
        "    }\n"
        "    $global:LASTEXITCODE = $downstreamExitCode\n"
        "}\n"
        "return\n"
    )


def render_cmd_launcher(
    *,
    repo_root: Path,
    python_executable: Path,
    command_name: str,
    flow_direct_command: str | None = None,
) -> str:
    repo_v = _escape_cmd_value(str(repo_root))
    py_v = _escape_cmd_value(str(python_executable))
    name_v = _escape_cmd_value(command_name)
    direct_exec_argv = ""
    if flow_direct_command is not None:
        token_v = _escape_cmd_value(_DIRECT_FLOW_ROUTE_TOKEN)
        direct_v = _escape_cmd_value(flow_direct_command)
        direct_exec_argv = f' "{token_v}" "{direct_v}"'
    return (
        "@echo off\n"
        f":: {OWNERSHIP_MARKER}\n"
        "setlocal\n"
        f"set \"REPO_ROOT={repo_v}\"\n"
        f"set \"FLOW_COMMAND_NAME={name_v}\"\n"
        "if defined PYTHONPATH (\n"
        "    set \"PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%\"\n"
        ") else (\n"
        "    set \"PYTHONPATH=%REPO_ROOT%\"\n"
        ")\n"
        f"\"{py_v}\" -m ai_dev_flow.cli{direct_exec_argv} %*\n"
        "set \"AI_DEV_EXIT=%ERRORLEVEL%\"\n"
        "endlocal & exit /b %AI_DEV_EXIT%\n"
    )


def _build_launcher_map(
    *,
    platform: str,
    repo_root: Path,
    python_executable: Path,
    install_directory: Path,
    command_name: str,
) -> dict[Path, str]:
    if platform == "windows":
        ps1_path = install_directory / f"{command_name}.ps1"
        cmd_path = install_directory / f"{command_name}.cmd"
        return {
            ps1_path: render_powershell_launcher(
                repo_root=repo_root,
                python_executable=python_executable,
                command_name=command_name,
                flow_direct_command=None,
            ),
            cmd_path: render_cmd_launcher(
                repo_root=repo_root,
                python_executable=python_executable,
                command_name=command_name,
                flow_direct_command=None,
            ),
        }

    launcher_path = install_directory / command_name
    return {
        launcher_path: render_posix_launcher(
            repo_root=repo_root,
            python_executable=python_executable,
            command_name=command_name,
            flow_direct_command=None,
        )
    }


def _build_prefixed_flow_launcher_map(
    *,
    platform: str,
    repo_root: Path,
    python_executable: Path,
    install_directory: Path,
    prefix: str,
) -> dict[Path, str]:
    launcher_map: dict[Path, str] = {}
    for flow_command in _fixed_flow_launcher_commands():
        launcher_name = f"{prefix}-{flow_command}"
        if platform == "windows":
            ps1_path = install_directory / f"{launcher_name}.ps1"
            cmd_path = install_directory / f"{launcher_name}.cmd"
            launcher_map[ps1_path] = render_powershell_launcher(
                repo_root=repo_root,
                python_executable=python_executable,
                command_name=launcher_name,
                flow_direct_command=flow_command,
            )
            launcher_map[cmd_path] = render_cmd_launcher(
                repo_root=repo_root,
                python_executable=python_executable,
                command_name=launcher_name,
                flow_direct_command=flow_command,
            )
            continue

        path = install_directory / launcher_name
        launcher_map[path] = render_posix_launcher(
            repo_root=repo_root,
            python_executable=python_executable,
            command_name=launcher_name,
            flow_direct_command=flow_command,
        )

    return launcher_map


def _build_posix_prefixed_flow_symlink_map(
    *,
    repo_root: Path,
    install_directory: Path,
    prefix: str,
) -> dict[Path, Path]:
    symlink_map: dict[Path, Path] = {}
    scripts_root = repo_root / "skills" / "flow" / "scripts"
    for flow_command in _fixed_flow_launcher_commands():
        launcher_name = f"{prefix}-{flow_command}"
        launcher_path = install_directory / launcher_name
        target_path = (scripts_root / f"flow-{flow_command}").resolve()
        if not target_path.exists() or not target_path.is_file():
            raise BootstrapError(
                f"Canonical flow skill command target not found: {target_path}"
            )
        symlink_map[launcher_path] = target_path
    return symlink_map


def _path_exists_or_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _normalized_absolute_path_text_for_platform(path: Path, *, platform: str) -> str:
    absolute = path.expanduser().resolve(strict=False)
    return _normalize_path_text_for_platform(str(absolute), platform=platform)


def _normalized_literal_symlink_target_text(path: Path, *, platform: str) -> str | None:
    if not path.is_symlink():
        return None
    try:
        target_text = os.readlink(path)
    except OSError:
        return None
    target_path = Path(target_text)
    if not target_path.is_absolute():
        target_path = path.parent / target_path
    return _normalize_path_text_for_platform(str(target_path), platform=platform)


def _normalized_symlink_target_text(path: Path, *, platform: str) -> str | None:
    literal_target = _normalized_literal_symlink_target_text(path, platform=platform)
    if literal_target is None:
        return None
    return _normalized_absolute_path_text_for_platform(Path(literal_target), platform=platform)


def _symlink_ownership_value(target_path: Path, *, platform: str) -> str:
    normalized_target = _normalized_absolute_path_text_for_platform(
        target_path,
        platform=platform,
    )
    return f"{_SYMLINK_OWNERSHIP_PREFIX}{normalized_target}"


def _owned_entry_matches_existing_path(
    *,
    path: Path,
    ownership_value: str,
    platform: str,
) -> bool:
    if ownership_value.startswith(_SYMLINK_OWNERSHIP_PREFIX):
        expected_target = ownership_value[len(_SYMLINK_OWNERSHIP_PREFIX) :]
        actual_target = _normalized_symlink_target_text(path, platform=platform)
        return actual_target == expected_target

    if not path.is_file():
        return False
    try:
        return _sha256_file(path) == ownership_value
    except OSError:
        return False


def _install_posix_symlink_launchers(
    *,
    symlink_map: dict[Path, Path],
    prior_record: _PrefixLauncherOwnershipRecord | None,
    force: bool,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool | None,
    emit_force_notice: bool,
) -> tuple[LauncherInstallStatus, ...]:
    statuses: list[LauncherInstallStatus] = []
    prior_owned = prior_record.owned_launchers if prior_record is not None else {}

    for path, target in symlink_map.items():
        if not _path_exists_or_symlink(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.symlink_to(target)
            except OSError as exc:
                raise BootstrapError(f"Cannot create symlink launcher {path}: {exc}") from exc
            statuses.append(LauncherInstallStatus(path=path, state="installed"))
            continue

        actual_target = _normalized_symlink_target_text(path, platform="posix")
        expected_target = _normalized_absolute_path_text_for_platform(target, platform="posix")
        if actual_target == expected_target:
            statuses.append(LauncherInstallStatus(path=path, state="up-to-date"))
            continue

        ownership_value = prior_owned.get(str(path))
        owned_and_provable = (
            ownership_value is not None
            and _owned_entry_matches_existing_path(
                path=path,
                ownership_value=ownership_value,
                platform="posix",
            )
        )

        if not owned_and_provable:
            if not path.is_file() and not path.is_symlink():
                raise BootstrapError(
                    "Cannot install launcher because destination exists and is neither a regular file nor a symlink: "
                    f"{path}"
                )
            _confirm_replace_conflicting_launcher(
                path=path,
                force=force,
                input_stream=input_stream,
                output_stream=output_stream,
                interactive=interactive,
                emit_force_notice=emit_force_notice,
            )

        previous_is_symlink = path.is_symlink()
        previous_link_target: str | None = None
        previous_bytes: bytes | None = None
        previous_mode: int | None = None
        try:
            previous_mode = path.lstat().st_mode
            if previous_is_symlink:
                previous_link_target = os.readlink(path)
            elif path.is_file():
                previous_bytes = path.read_bytes()
        except OSError as exc:
            raise BootstrapError(f"Cannot snapshot existing launcher {path}: {exc}") from exc

        try:
            path.unlink()
        except OSError as exc:
            raise BootstrapError(f"Cannot replace launcher {path}: {exc}") from exc
        try:
            path.symlink_to(target)
        except OSError as exc:
            rollback_error: Exception | None = None
            try:
                if previous_is_symlink and previous_link_target is not None:
                    path.symlink_to(previous_link_target)
                elif previous_bytes is not None:
                    path.write_bytes(previous_bytes)
                    if previous_mode is not None:
                        path.chmod(previous_mode)
            except Exception as rollback_exc:  # pragma: no cover - best-effort rollback
                rollback_error = rollback_exc

            if rollback_error is not None:
                raise BootstrapError(
                    f"Cannot create symlink launcher {path}: {exc}; rollback failed: {rollback_error}"
                ) from exc
            raise BootstrapError(f"Cannot create symlink launcher {path}: {exc}") from exc
        statuses.append(LauncherInstallStatus(path=path, state="updated"))

    return tuple(statuses)


def _reconcile_recorded_owned_launchers(
    *,
    prior_record: _PrefixLauncherOwnershipRecord | None,
    desired_paths: set[Path],
    normalized_platform: str,
    install_dir: Path,
) -> tuple[LauncherInstallStatus, ...]:
    if prior_record is None:
        return ()

    if prior_record.platform != normalized_platform:
        return ()

    desired_path_texts = {
        _normalize_path_text_for_platform(
            str(path),
            platform=normalized_platform,
            casefold=(normalized_platform == "windows"),
        )
        for path in desired_paths
    }

    statuses: list[LauncherInstallStatus] = []
    obsolete_paths = sorted(
        path
        for path in prior_record.owned_launchers
        if _normalize_path_text_for_platform(
            path,
            platform=normalized_platform,
            casefold=(normalized_platform == "windows"),
        )
        not in desired_path_texts
    )

    for obsolete_path_text in obsolete_paths:
        obsolete_path = Path(obsolete_path_text)
        expected_digest = prior_record.owned_launchers[obsolete_path_text]
        if not _path_exists_or_symlink(obsolete_path):
            statuses.append(LauncherInstallStatus(path=obsolete_path, state="already-absent"))
            continue

        if expected_digest.startswith(_SYMLINK_OWNERSHIP_PREFIX):
            expected_target = expected_digest[len(_SYMLINK_OWNERSHIP_PREFIX) :]
            actual_target = _normalized_symlink_target_text(
                obsolete_path,
                platform=normalized_platform,
            )
            if actual_target != expected_target:
                statuses.append(LauncherInstallStatus(path=obsolete_path, state="preserved-divergent"))
                continue
            try:
                obsolete_path.unlink()
            except OSError as exc:
                raise BootstrapError(f"Cannot remove obsolete owned launcher {obsolete_path}: {exc}") from exc
            statuses.append(LauncherInstallStatus(path=obsolete_path, state="removed"))
            continue

        if not obsolete_path.is_file():
            statuses.append(LauncherInstallStatus(path=obsolete_path, state="preserved-divergent"))
            continue

        try:
            actual_digest = _sha256_file(obsolete_path)
        except OSError:
            statuses.append(LauncherInstallStatus(path=obsolete_path, state="preserved-divergent"))
            continue

        if actual_digest != expected_digest:
            statuses.append(LauncherInstallStatus(path=obsolete_path, state="preserved-divergent"))
            continue

        try:
            obsolete_path.unlink()
        except OSError as exc:
            raise BootstrapError(f"Cannot remove obsolete owned launcher {obsolete_path}: {exc}") from exc
        statuses.append(LauncherInstallStatus(path=obsolete_path, state="removed"))

    return tuple(statuses)


def _reconcile_legacy_retired_flow_launchers(
    *,
    legacy_managed_launchers: dict[str, str],
    normalized_platform: str,
    install_dir: Path,
) -> tuple[LauncherInstallStatus, ...]:
    if normalized_platform != "posix":
        return ()

    normalized_legacy_digests = {
        _normalize_path_text_for_platform(path_text, platform=normalized_platform): digest
        for path_text, digest in legacy_managed_launchers.items()
    }

    statuses: list[LauncherInstallStatus] = []
    for launcher_name in LEGACY_RETIRED_FLOW_LAUNCHER_NAMES:
        launcher_path = install_dir / launcher_name
        normalized_path = _normalize_path_text_for_platform(
            str(launcher_path),
            platform=normalized_platform,
        )

        expected_digest = normalized_legacy_digests.get(normalized_path)
        if expected_digest is None:
            continue

        if not launcher_path.exists():
            statuses.append(LauncherInstallStatus(path=launcher_path, state="already-absent"))
            continue

        if not launcher_path.is_file():
            statuses.append(LauncherInstallStatus(path=launcher_path, state="preserved-divergent"))
            continue

        try:
            actual_digest = _sha256_file(launcher_path)
        except OSError:
            statuses.append(LauncherInstallStatus(path=launcher_path, state="preserved-divergent"))
            continue

        if actual_digest != expected_digest:
            statuses.append(LauncherInstallStatus(path=launcher_path, state="preserved-divergent"))
            continue

        try:
            current_text = launcher_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            statuses.append(LauncherInstallStatus(path=launcher_path, state="preserved-divergent"))
            continue

        if not _matches_legacy_retired_flow_launcher_shape(
            launcher_name=launcher_name,
            text=current_text,
        ):
            statuses.append(LauncherInstallStatus(path=launcher_path, state="preserved-divergent"))
            continue

        try:
            launcher_path.unlink()
        except OSError as exc:
            raise BootstrapError(f"Cannot remove obsolete owned launcher {launcher_path}: {exc}") from exc
        statuses.append(LauncherInstallStatus(path=launcher_path, state="removed"))

    return tuple(statuses)


def _validate_launcher_collisions(launcher_map: dict[Path, str]) -> dict[Path, _SnapshotEntry]:
    result = _validate_launcher_collisions_with_replacement(
        launcher_map=launcher_map,
        force=False,
        input_stream=sys.stdin,
        output_stream=sys.stderr,
        interactive=False,
        emit_force_notice=True,
    )
    return result.snapshots


def _validate_launcher_collisions_with_replacement(
    *,
    launcher_map: dict[Path, str],
    force: bool,
    input_stream: TextIO,
    output_stream: TextIO,
    interactive: bool | None,
    emit_force_notice: bool,
) -> _CollisionValidationResult:
    snapshots: dict[Path, _SnapshotEntry] = {}
    force_replaced_paths: list[Path] = []
    for path, rendered in launcher_map.items():
        if not path.exists():
            snapshots[path] = _SnapshotEntry(exists=False, text="", mode=None)
            continue

        if not path.is_file():
            raise BootstrapError(
                "Cannot install launcher because destination exists and is not a regular file: "
                f"{path}"
            )

        try:
            existing_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(f"Cannot read existing launcher {path}: {exc}") from exc

        if existing_text != rendered:
            force_replaced = _confirm_replace_conflicting_launcher(
                path=path,
                force=force,
                input_stream=input_stream,
                output_stream=output_stream,
                interactive=interactive,
                emit_force_notice=emit_force_notice,
            )
            if force_replaced:
                force_replaced_paths.append(path)

        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise BootstrapError(f"Cannot stat existing launcher {path}: {exc}") from exc

        snapshots[path] = _SnapshotEntry(exists=True, text=existing_text, mode=mode)

    return _CollisionValidationResult(
        snapshots=snapshots,
        force_replaced_paths=tuple(force_replaced_paths),
    )


def _set_executable_if_needed(path: Path, *, platform: str) -> None:
    if platform == "windows":
        return

    try:
        current_mode = path.stat().st_mode
        path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        raise BootstrapError(f"Cannot set executable permissions on {path}: {exc}") from exc


def _restore_launchers(
    *,
    snapshots: dict[Path, _SnapshotEntry],
    platform: str,
) -> list[str]:
    failures: list[str] = []
    for path, snapshot in snapshots.items():
        try:
            if snapshot.exists:
                write_text_atomic(path, snapshot.text)
                if snapshot.mode is not None:
                    path.chmod(snapshot.mode)
            elif path.exists():
                path.unlink()
        except Exception as exc:
            failures.append(f"restore launcher {path}: {exc}")
    return failures


def _install_launchers(
    *,
    launcher_map: dict[Path, str],
    snapshots: dict[Path, _SnapshotEntry],
    platform: str,
) -> tuple[LauncherInstallStatus, ...]:
    statuses: list[LauncherInstallStatus] = []
    for path, rendered in launcher_map.items():
        snapshot = snapshots[path]
        if snapshot.exists and snapshot.text == rendered:
            statuses.append(LauncherInstallStatus(path=path, state="up-to-date"))
            continue

        try:
            write_text_atomic(path, rendered)
            _set_executable_if_needed(path, platform=platform)
            confirmed = path.read_text(encoding="utf-8")
            if confirmed != rendered:
                raise BootstrapError(f"Launcher verification mismatch after writing {path}")
        except Exception as exc:
            rollback_failures = _restore_launchers(snapshots=snapshots, platform=platform)
            if rollback_failures:
                details = "; ".join(rollback_failures)
                raise BootstrapError(f"Cannot install launcher {path}: {exc}; rollback failures: {details}") from exc
            raise BootstrapError(f"Cannot install launcher {path}: {exc}") from exc

        state = "updated" if snapshot.exists else "installed"
        statuses.append(LauncherInstallStatus(path=path, state=state))

    return tuple(statuses)


def run_bootstrap(
    *,
    platform: str,
    repo_root: Path,
    prefix: str | None = None,
    explicit_python: str | None = None,
    install_directory: Path | None = None,
    home: Path | None = None,
    shell_program: str | None = None,
    user_profile: str | None = None,
    config_path: Path | None = None,
    path_value: str | None = None,
    force: bool = False,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    interactive: bool | None = None,
    emit_force_replacement_notices: bool = True,
) -> BootstrapResult:
    cleaned_prefix = _validate_prefix("flow" if prefix is None else prefix)

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"posix", "windows"}:
        raise BootstrapError(
            f"Unsupported bootstrap platform: {platform!r}. Expected 'posix' or 'windows'."
        )

    windows = normalized_platform == "windows"
    os_name = "nt" if windows else "posix"
    repo_root_resolved = repo_root.expanduser().resolve()
    _validate_repo_root(repo_root_resolved)

    python_executable = _resolve_python_interpreter(
        explicit=explicit_python,
        platform=normalized_platform,
    )
    _validate_python_runtime(python_executable, repo_root_resolved)

    home_path = home.expanduser().resolve() if home is not None else Path.home()
    install_dir = (
        install_directory.expanduser().resolve()
        if install_directory is not None
        else (home_path / ".local" / "bin").resolve()
    )

    prefix_ownership_path = resolve_prefix_launcher_ownership_path(
        os_name=os_name,
        home=home_path,
    )

    canonical_repo_root = repo_root_resolved

    launcher_map = _build_prefixed_flow_launcher_map(
        platform=normalized_platform,
        repo_root=canonical_repo_root,
        python_executable=python_executable,
        install_directory=install_dir,
        prefix=cleaned_prefix,
    )

    if input_stream is None:
        input_stream = sys.stdin
    if output_stream is None:
        output_stream = sys.stderr

    collision_validation = None
    snapshots: dict[Path, _SnapshotEntry] = {}
    posix_symlink_map: dict[Path, Path] = {}
    if normalized_platform == "posix":
        posix_symlink_map = _build_posix_prefixed_flow_symlink_map(
            repo_root=canonical_repo_root,
            install_directory=install_dir,
            prefix=cleaned_prefix,
        )
    else:
        collision_validation = _validate_launcher_collisions_with_replacement(
            launcher_map=launcher_map,
            force=force,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            emit_force_notice=emit_force_replacement_notices,
        )
        snapshots = collision_validation.snapshots

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"Cannot create install directory {install_dir}: {exc}") from exc

    prior_prefix_ownership = _load_prefix_launcher_ownership(prefix_ownership_path)
    legacy_manifest_path = resolve_legacy_installation_manifest_path(
        os_name=os_name,
        home=home_path,
    )
    legacy_managed_launchers = _load_legacy_managed_launchers(legacy_manifest_path)

    if normalized_platform == "posix":
        install_statuses = _install_posix_symlink_launchers(
            symlink_map=posix_symlink_map,
            prior_record=prior_prefix_ownership,
            force=force,
            input_stream=input_stream,
            output_stream=output_stream,
            interactive=interactive,
            emit_force_notice=emit_force_replacement_notices,
        )
        desired_paths = set(posix_symlink_map.keys())
    else:
        install_statuses = _install_launchers(
            launcher_map=launcher_map,
            snapshots=snapshots,
            platform=normalized_platform,
        )
        desired_paths = set(launcher_map.keys())

    cleanup_statuses = _reconcile_recorded_owned_launchers(
        prior_record=prior_prefix_ownership,
        desired_paths=desired_paths,
        normalized_platform=normalized_platform,
        install_dir=install_dir,
    )
    legacy_cleanup_statuses = _reconcile_legacy_retired_flow_launchers(
        legacy_managed_launchers=legacy_managed_launchers,
        normalized_platform=normalized_platform,
        install_dir=install_dir,
    )

    statuses = (*install_statuses, *cleanup_statuses, *legacy_cleanup_statuses)

    ownership_record = _PrefixLauncherOwnershipRecord(
        version=PREFIX_LAUNCHER_OWNERSHIP_VERSION,
        selected_prefix=cleaned_prefix,
        platform=normalized_platform,
        install_directory=str(install_dir),
        owned_launchers=(
            {
                str(path): _symlink_ownership_value(target, platform=normalized_platform)
                for path, target in posix_symlink_map.items()
            }
            if normalized_platform == "posix"
            else {
                str(path): _sha256_text(text)
                for path, text in launcher_map.items()
            }
        ),
    )
    _save_prefix_launcher_ownership(prefix_ownership_path, ownership_record)

    effective_path = os.environ.get("PATH", "") if path_value is None else path_value
    on_path = _is_path_on_path(
        install_dir,
        path_value=effective_path,
        windows=windows,
    )

    warnings: list[str] = []
    if collision_validation is not None:
        warnings.extend(
            f"Force-replaced conflicting launcher at {path}."
            for path in collision_validation.force_replaced_paths
        )
    if not on_path:
        warnings.append("Install directory is not currently on PATH.")

    return BootstrapResult(
        platform=normalized_platform,
        command_name=f"{cleaned_prefix}-*",
        repo_root=canonical_repo_root,
        python_executable=python_executable,
        install_directory=install_dir,
        launcher_statuses=statuses,
        install_dir_on_path=on_path,
        warnings=tuple(warnings),
    )


def _render_path_guidance(install_directory: Path, *, windows: bool) -> str:
    if windows:
        path_literal = _quote_powershell_single(str(install_directory))
        return (
            "Install directory is not on PATH. Add it manually by appending this "
            "directory to your User PATH in Windows Environment Variables. "
            "For the current PowerShell session only, you can run:\n"
            f"  $env:Path = {path_literal} + ';' + $env:Path"
        )

    return (
        "Install directory is not on PATH. Add it manually, for example:\n"
        f"  export PATH={_quote_posix_single(str(install_directory))}:\"$PATH\""
    )


def _print_result(result: BootstrapResult) -> None:
    print("Bootstrap complete.")
    print(f"Platform: {result.platform}")
    print(f"Command name: {result.command_name}")
    for launcher_status in result.launcher_statuses:
        print(f"Launcher: {launcher_status.path} ({launcher_status.state})")

    print(f"Repository root: {result.repo_root}")
    print(f"Python interpreter: {result.python_executable}")
    print(f"Install directory: {result.install_directory}")
    if result.install_dir_on_path:
        print("PATH status: install directory is on PATH")
    else:
        windows = result.platform == "windows"
        print("PATH status: install directory is not on PATH")
        print(_render_path_guidance(result.install_directory, windows=windows))

    print(
        "Source binding: launcher points to this repository root. "
        "If the checkout is moved or removed, rerun bootstrap to repair the launcher."
    )


def _print_concise_result(result: BootstrapResult) -> None:
    if result.warnings:
        print("AI Dev installation completed with warnings.")
        for warning in result.warnings:
            print(f"Warning: {warning}")
        return
    print("AI Dev installation completed successfully.")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.bootstrap",
        description="Install or update ai-dev launchers.",
    )
    parser.add_argument(
        "--platform",
        choices=("posix", "windows"),
        default=("windows" if os.name == "nt" else "posix"),
        help="Bootstrap target platform semantics.",
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        help="Absolute repository root used by the launcher.",
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        default=None,
        help="Preferred Python interpreter for launcher execution.",
    )
    parser.add_argument(
        "--install-dir",
        default=None,
        help="Override launcher installation directory.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Override user home for path modeling.",
    )
    parser.add_argument(
        "--shell-program",
        default=None,
        help="Override shell program when resolving POSIX profile path.",
    )
    parser.add_argument(
        "--user-profile",
        default=None,
        help="Override USERPROFILE for Windows path modeling.",
    )
    parser.add_argument(
        "--config-path",
        default=None,
        help="Override user config path during bootstrap.",
    )
    parser.add_argument(
        "--path-value",
        default=None,
        help="Override PATH value used for diagnostics.",
    )
    parser.add_argument(
        "--prefix",
        default="flow",
        help=(
            "Install fixed flow launchers under <prefix>-<command> names. "
            "Example: --prefix flow installs flow-start, flow-status, etc."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace conflicting launcher files without prompting.",
    )
    parser.add_argument(
        "--installer-output",
        choices=("detailed", "concise"),
        default="detailed",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        parsed = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 2

    try:
        result = run_bootstrap(
            platform=parsed.platform,
            repo_root=Path(parsed.repo_root),
            prefix=parsed.prefix,
            explicit_python=parsed.python_executable,
            install_directory=(None if parsed.install_dir is None else Path(parsed.install_dir)),
            home=(None if parsed.home is None else Path(parsed.home)),
            shell_program=parsed.shell_program,
            user_profile=parsed.user_profile,
            config_path=(None if parsed.config_path is None else Path(parsed.config_path)),
            path_value=parsed.path_value,
            force=parsed.force,
            emit_force_replacement_notices=(parsed.installer_output == "detailed"),
        )
    except BootstrapError as exc:
        if parsed.installer_output == "concise":
            print(f"AI Dev installation failed: {exc}", file=sys.stderr)
        else:
            print(f"bootstrap: {exc}", file=sys.stderr)
        return 1
    except JsonFileError as exc:
        if parsed.installer_output == "concise":
            print(f"AI Dev installation failed: {exc}", file=sys.stderr)
        else:
            print(f"bootstrap: {exc}", file=sys.stderr)
        return 1

    if parsed.installer_output == "concise":
        _print_concise_result(result)
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())