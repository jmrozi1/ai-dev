from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import re
import sys

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


PREFIX_LAUNCHER_OWNERSHIP_VERSION = 1
_SYMLINK_OWNERSHIP_PREFIX = "symlink:"
_RETIRED_FLOW_COMMANDS = frozenset(
    {
        "start",
        "patch",
        "status",
        "diff",
        "commit",
        "reset",
        "promote",
        "complete",
        "block",
        "resume",
        "ticket-create",
        "ticket-show",
        "ticket-query",
        "review",
    }
)
_PREFIX_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")


class BootstrapError(Exception):
    """Raised when managed launcher cleanup cannot proceed safely."""


@dataclass(frozen=True)
class LauncherInstallStatus:
    path: Path
    state: str


@dataclass(frozen=True)
class LauncherCleanupResult:
    platform: str
    ownership_path: Path
    launcher_statuses: tuple[LauncherInstallStatus, ...]


@dataclass(frozen=True)
class _PrefixLauncherOwnershipRecord:
    selected_prefix: str
    platform: str
    install_directory: str
    owned_launchers: dict[str, str]


def _path_module(platform: str):
    return ntpath if platform == "windows" else posixpath


def _normalize(path_text: str, *, platform: str, casefold: bool = False) -> str:
    normalized = _path_module(platform).normpath(path_text)
    if casefold and platform == "windows":
        return ntpath.normcase(normalized)
    return normalized


def _pure_path(path_text: str, *, platform: str) -> PureWindowsPath | PurePosixPath:
    return PureWindowsPath(path_text) if platform == "windows" else PurePosixPath(path_text)


def _validate_prefix(prefix: object, *, record_path: Path) -> str:
    if not isinstance(prefix, str) or not _PREFIX_PATTERN.fullmatch(prefix):
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: invalid selected_prefix.")
    return prefix


def resolve_prefix_launcher_ownership_path(
    *,
    os_name: str | None = None,
    home: Path | None = None,
    appdata: str | None = None,
    xdg_config_home: str | None = None,
) -> Path:
    resolved_os = os.name if os_name is None else os_name
    resolved_home = (Path.home() if home is None else home).expanduser().resolve()
    if resolved_os == "nt":
        base = Path(appdata or os.environ.get("APPDATA") or resolved_home / "AppData" / "Roaming") / "ai-dev"
    else:
        base = Path(xdg_config_home or os.environ.get("XDG_CONFIG_HOME") or resolved_home / ".config") / "ai-dev"
    return base / "prefixed-launcher-ownership.json"


def _validate_record_path(
    *,
    record_path: Path,
    platform: str,
    selected_prefix: str,
    install_directory: str,
    launcher_path_text: str,
) -> str:
    launcher_path = _pure_path(launcher_path_text, platform=platform)
    install_path = _pure_path(install_directory, platform=platform)
    if not launcher_path.is_absolute() or _normalize(launcher_path_text, platform=platform) != launcher_path_text:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: launcher paths must be normalized absolute paths.")
    if str(launcher_path.parent) != str(install_path):
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: launcher must be a direct child of install_directory.")
    name = launcher_path.name
    base_name = name
    if platform == "windows":
        if name.lower().endswith(".cmd") or name.lower().endswith(".ps1"):
            base_name = name.rsplit(".", 1)[0]
        else:
            raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: Windows launchers must use .cmd or .ps1.")
    elif name.endswith(".cmd") or name.endswith(".ps1"):
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: POSIX launchers may not use extensions.")
    prefix = f"{selected_prefix}-"
    starts_with_prefix = base_name.lower().startswith(prefix.lower()) if platform == "windows" else base_name.startswith(prefix)
    if not starts_with_prefix:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: launcher name does not match selected_prefix.")
    command = base_name[len(prefix) :]
    known = command.lower() in {item.lower() for item in _RETIRED_FLOW_COMMANDS} if platform == "windows" else command in _RETIRED_FLOW_COMMANDS
    if not known:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {record_path}: launcher references unknown retired command.")
    return str(launcher_path)


def _load_record(path: Path) -> _PrefixLauncherOwnershipRecord | None:
    if not path.exists():
        return None
    try:
        raw = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise BootstrapError(str(exc)) from exc
    if not isinstance(raw, dict) or raw.get("version") != PREFIX_LAUNCHER_OWNERSHIP_VERSION:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}.")
    platform = raw.get("platform")
    if platform not in {"posix", "windows"}:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: unsupported platform.")
    selected_prefix = _validate_prefix(raw.get("selected_prefix"), record_path=path)
    install_directory = raw.get("install_directory")
    if not isinstance(install_directory, str) or not _pure_path(install_directory, platform=platform).is_absolute():
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: install_directory must be absolute.")
    if _normalize(install_directory, platform=platform) != install_directory:
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: install_directory must be normalized.")
    raw_launchers = raw.get("owned_launchers")
    if not isinstance(raw_launchers, dict):
        raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: owned_launchers must be an object.")
    launchers: dict[str, str] = {}
    for path_text, ownership in raw_launchers.items():
        if not isinstance(path_text, str) or not isinstance(ownership, str):
            raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: invalid ownership entry.")
        if not (ownership.startswith(_SYMLINK_OWNERSHIP_PREFIX) or re.fullmatch(r"[0-9a-f]{64}", ownership)):
            raise BootstrapError(f"Invalid prefix launcher ownership record in {path}: invalid ownership marker.")
        normalized_path = _validate_record_path(
            record_path=path,
            platform=platform,
            selected_prefix=selected_prefix,
            install_directory=install_directory,
            launcher_path_text=path_text,
        )
        launchers[normalized_path] = ownership
    return _PrefixLauncherOwnershipRecord(selected_prefix, platform, install_directory, dict(sorted(launchers.items())))


def _path_exists_or_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _actual_symlink_target(path: Path, *, platform: str) -> str | None:
    if not path.is_symlink():
        return None
    try:
        target = Path(os.readlink(path))
    except OSError:
        return None
    if not target.is_absolute():
        target = path.parent / target
    return _normalize(str(target.resolve(strict=False)), platform=platform)


def _ownership_matches(path: Path, ownership: str, *, platform: str) -> bool:
    if ownership.startswith(_SYMLINK_OWNERSHIP_PREFIX):
        return _actual_symlink_target(path, platform=platform) == ownership[len(_SYMLINK_OWNERSHIP_PREFIX) :]
    if not path.is_file():
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == ownership
    except OSError:
        return False


def _save_record(path: Path, record: _PrefixLauncherOwnershipRecord) -> None:
    payload = {
        "version": PREFIX_LAUNCHER_OWNERSHIP_VERSION,
        "selected_prefix": record.selected_prefix,
        "platform": record.platform,
        "install_directory": record.install_directory,
        "owned_launchers": record.owned_launchers,
    }
    try:
        write_json_object_atomic(path, payload)
    except JsonFileError as exc:
        raise BootstrapError(str(exc)) from exc


def cleanup_managed_launchers(*, platform: str, home: Path | None = None) -> LauncherCleanupResult:
    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"posix", "windows"}:
        raise BootstrapError("Unsupported cleanup platform; expected 'posix' or 'windows'.")
    os_name = "nt" if normalized_platform == "windows" else "posix"
    ownership_path = resolve_prefix_launcher_ownership_path(os_name=os_name, home=home)
    record = _load_record(ownership_path)
    if record is None or record.platform != normalized_platform:
        return LauncherCleanupResult(normalized_platform, ownership_path, ())

    remaining: dict[str, str] = {}
    statuses: list[LauncherInstallStatus] = []
    for path_text, ownership in record.owned_launchers.items():
        path = Path(path_text)
        if not _path_exists_or_symlink(path):
            statuses.append(LauncherInstallStatus(path, "already-absent"))
            continue
        if not _ownership_matches(path, ownership, platform=normalized_platform):
            remaining[path_text] = ownership
            statuses.append(LauncherInstallStatus(path, "preserved-divergent"))
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise BootstrapError(f"Cannot remove owned launcher {path}: {exc}") from exc
        statuses.append(LauncherInstallStatus(path, "removed"))

    if remaining:
        _save_record(
            ownership_path,
            _PrefixLauncherOwnershipRecord(record.selected_prefix, record.platform, record.install_directory, remaining),
        )
    else:
        try:
            ownership_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BootstrapError(f"Cannot remove empty launcher ownership record {ownership_path}: {exc}") from exc
    return LauncherCleanupResult(normalized_platform, ownership_path, tuple(statuses))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.bootstrap",
        description="Remove AI Dev-managed legacy Flow launchers.",
    )
    parser.add_argument("--platform", choices=("posix", "windows"), default=("windows" if os.name == "nt" else "posix"))
    parser.add_argument("--home", default=None, help="Optional home directory containing the launcher ownership record.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = cleanup_managed_launchers(
            platform=args.platform,
            home=None if args.home is None else Path(args.home),
        )
    except BootstrapError as exc:
        print(f"launcher-cleanup: {exc}", file=sys.stderr)
        return 1
    for status in result.launcher_statuses:
        print(f"{status.state}: {status.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
