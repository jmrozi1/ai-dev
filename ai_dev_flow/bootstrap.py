from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import posixpath
import shutil
import stat
import subprocess
import sys

from .alias_installation import (
    AliasInstallationError,
    resolve_installer_paths,
    resolve_manifest_path,
)
from .editable_config import EditableConfigError, ensure_editable_user_config
from .json_files import JsonFileError, write_text_atomic


OWNERSHIP_MARKER = "AI_DEV_LAUNCHER_V1"
CANONICAL_COMMAND_NAME = "ai-dev"


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
    config_path: Path
    config_created: bool
    manifest_path: Path
    alias_file_path: Path | None
    profile_path: Path | None
    launcher_statuses: tuple[LauncherInstallStatus, ...]
    install_dir_on_path: bool


@dataclass(frozen=True)
class _SnapshotEntry:
    exists: bool
    text: str
    mode: int | None


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


def _ensure_owned_launcher_text(text: str, path: Path) -> None:
    lower_name = path.name.lower()
    if lower_name.endswith(".ps1"):
        if _is_owned_powershell_launcher_text(text):
            return
    elif lower_name.endswith(".cmd"):
        if _is_owned_cmd_launcher_text(text):
            return
    elif _is_owned_posix_launcher_text(text):
        return

    raise BootstrapError(
        "Refusing to overwrite non-AI-Dev launcher at "
        f"{path}. Move or remove it manually, then rerun bootstrap."
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
) -> str:
    repo_q = _quote_posix_single(str(repo_root))
    py_q = _quote_posix_single(str(python_executable))
    command_q = _quote_posix_single(command_name)
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
        "exec \"${PYTHON_EXECUTABLE}\" -m ai_dev_flow.cli \"$@\"\n"
    )


def render_powershell_launcher(
    *,
    repo_root: Path,
    python_executable: Path,
    command_name: str,
) -> str:
    repo_q = _quote_powershell_single(str(repo_root))
    py_q = _quote_powershell_single(str(python_executable))
    name_q = _quote_powershell_single(command_name)
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
        "    & $pythonExecutable -m ai_dev_flow.cli @args\n"
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
) -> str:
    repo_v = _escape_cmd_value(str(repo_root))
    py_v = _escape_cmd_value(str(python_executable))
    name_v = _escape_cmd_value(command_name)
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
        f"\"{py_v}\" -m ai_dev_flow.cli %*\n"
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
            ),
            cmd_path: render_cmd_launcher(
                repo_root=repo_root,
                python_executable=python_executable,
                command_name=command_name,
            ),
        }

    launcher_path = install_directory / command_name
    return {
        launcher_path: render_posix_launcher(
            repo_root=repo_root,
            python_executable=python_executable,
            command_name=command_name,
        )
    }


def _validate_launcher_collisions(launcher_map: dict[Path, str]) -> dict[Path, _SnapshotEntry]:
    snapshots: dict[Path, _SnapshotEntry] = {}
    for path, _ in launcher_map.items():
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

        _ensure_owned_launcher_text(existing_text, path)

        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise BootstrapError(f"Cannot stat existing launcher {path}: {exc}") from exc

        snapshots[path] = _SnapshotEntry(exists=True, text=existing_text, mode=mode)

    return snapshots


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


@contextmanager
def _temporary_config_override(config_path: Path | None):
    if config_path is None:
        yield
        return

    previous = os.environ.get("AI_DEV_CONFIG")
    os.environ["AI_DEV_CONFIG"] = str(config_path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AI_DEV_CONFIG", None)
        else:
            os.environ["AI_DEV_CONFIG"] = previous


def run_bootstrap(
    *,
    platform: str,
    repo_root: Path,
    command_name: str = "ai-dev",
    explicit_python: str | None = None,
    install_directory: Path | None = None,
    home: Path | None = None,
    shell_program: str | None = None,
    user_profile: str | None = None,
    config_path: Path | None = None,
    path_value: str | None = None,
) -> BootstrapResult:
    _validate_command_name(command_name)

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

    launcher_map = _build_launcher_map(
        platform=normalized_platform,
        repo_root=repo_root_resolved,
        python_executable=python_executable,
        install_directory=install_dir,
        command_name=command_name,
    )

    snapshots = _validate_launcher_collisions(launcher_map)

    manifest_path = resolve_manifest_path(
        os_name=os_name,
        home=home_path,
        user_profile=user_profile,
    )

    alias_file_path: Path | None = None
    profile_path: Path | None = None
    try:
        resolved_paths = resolve_installer_paths(
            os_name=os_name,
            shell_program=shell_program,
            home=home_path,
            user_profile=user_profile,
        )
        alias_file_path = resolved_paths.alias_file_path
        profile_path = resolved_paths.profile_path
    except AliasInstallationError:
        # Bootstrap should still succeed when shell-specific alias profile cannot be resolved.
        pass

    with _temporary_config_override(config_path):
        try:
            editable_config = ensure_editable_user_config()
        except EditableConfigError as exc:
            raise BootstrapError(str(exc)) from exc

    try:
        install_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapError(f"Cannot create install directory {install_dir}: {exc}") from exc

    statuses = _install_launchers(
        launcher_map=launcher_map,
        snapshots=snapshots,
        platform=normalized_platform,
    )

    effective_path = os.environ.get("PATH", "") if path_value is None else path_value
    on_path = _is_path_on_path(
        install_dir,
        path_value=effective_path,
        windows=windows,
    )

    return BootstrapResult(
        platform=normalized_platform,
        command_name=command_name,
        repo_root=repo_root_resolved,
        python_executable=python_executable,
        install_directory=install_dir,
        config_path=editable_config.config_path,
        config_created=editable_config.created,
        manifest_path=manifest_path,
        alias_file_path=alias_file_path,
        profile_path=profile_path,
        launcher_statuses=statuses,
        install_dir_on_path=on_path,
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
    print(f"Config path: {result.config_path}")
    if result.config_created:
        print("Config status: created")
    else:
        print("Config status: preserved")

    print(f"Manifest path: {result.manifest_path}")
    if result.alias_file_path is not None:
        print(f"Alias file path: {result.alias_file_path}")
    else:
        print("Alias file path: unresolved (shell-specific path not available)")

    if result.profile_path is not None:
        print(f"Profile path: {result.profile_path}")
    else:
        print("Profile path: unresolved (shell-specific path not available)")

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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.bootstrap",
        description="Install or update the canonical ai-dev launcher.",
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
        "--command-name",
        default=CANONICAL_COMMAND_NAME,
        choices=(CANONICAL_COMMAND_NAME,),
        help="Launcher command name to install (default: ai-dev).",
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
            command_name=parsed.command_name,
            explicit_python=parsed.python_executable,
            install_directory=(None if parsed.install_dir is None else Path(parsed.install_dir)),
            home=(None if parsed.home is None else Path(parsed.home)),
            shell_program=parsed.shell_program,
            user_profile=parsed.user_profile,
            config_path=(None if parsed.config_path is None else Path(parsed.config_path)),
            path_value=parsed.path_value,
        )
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1
    except (JsonFileError, EditableConfigError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1

    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())