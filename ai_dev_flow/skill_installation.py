from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import ntpath
import os
from pathlib import Path
import posixpath
import subprocess
import sys


class SkillInstallationError(Exception):
    """Raised when repository skill installation cannot proceed."""


@dataclass(frozen=True)
class SkillPackage:
    name: str
    source_directory: Path
    audience: str | None = None


@dataclass(frozen=True)
class SkillInstallStatus:
    name: str
    state: str
    destination_directory: Path


@dataclass(frozen=True)
class SkillInstallResult:
    source_root: Path
    destination_root: Path
    discovered_count: int
    installed_count: int
    updated_count: int
    unchanged_count: int
    statuses: tuple[SkillInstallStatus, ...]


SKILL_INSTALLATION_OWNERSHIP_VERSION = 1
_SYMLINK_OWNERSHIP_PREFIX = "symlink:"
_JUNCTION_OWNERSHIP_PREFIX = "junction:"
_OWNERSHIP_PREFIXES = (_SYMLINK_OWNERSHIP_PREFIX, _JUNCTION_OWNERSHIP_PREFIX)

# Windows junctions are reparse points, not symlinks: os.path.islink() is False
# for them, so they need their own detection. Junctions need no elevation, which
# symlinks do on stock Windows.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_EXTENDED_PATH_PREFIX = "\\\\?\\"

LINK_KIND_SYMLINK = "symlink"
LINK_KIND_JUNCTION = "junction"


def preferred_link_kind(platform: str) -> str:
    """Junctions on Windows, symlinks elsewhere."""
    return LINK_KIND_JUNCTION if platform == "windows" else LINK_KIND_SYMLINK


def _ownership_target_text(ownership: str) -> str:
    for prefix in _OWNERSHIP_PREFIXES:
        if ownership.startswith(prefix):
            return ownership[len(prefix):]
    return ownership


def _ownership_kind(ownership: str) -> str:
    return (
        LINK_KIND_JUNCTION
        if ownership.startswith(_JUNCTION_OWNERSHIP_PREFIX)
        else LINK_KIND_SYMLINK
    )


def _ownership_prefix_for_kind(kind: str) -> str:
    return _JUNCTION_OWNERSHIP_PREFIX if kind == LINK_KIND_JUNCTION else _SYMLINK_OWNERSHIP_PREFIX


def path_is_junction(path: Path) -> bool:
    try:
        return getattr(path.lstat(), "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, ValueError):
        return False


def path_is_managed_link(path: Path) -> bool:
    """A link this installer could own: symlink or Windows junction."""
    return path.is_symlink() or path_is_junction(path)


def read_link_target_text(path: Path) -> str | None:
    """Link target for symlinks and junctions, without the extended-path prefix."""
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target.startswith(_EXTENDED_PATH_PREFIX):
        target = target[len(_EXTENDED_PATH_PREFIX):]
    return target


def _remove_link(path: Path) -> None:
    """Remove a link without following it into the target directory."""
    if path.is_symlink():
        path.unlink()
        return
    if path_is_junction(path):
        # A junction is a directory entry; rmdir removes the link, not the target.
        os.rmdir(path)
        return
    if path.exists():
        path.unlink()


def _create_link(path: Path, target: Path, *, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == LINK_KIND_JUNCTION:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not path_is_junction(path):
            detail = completed.stdout.strip() or f"exit code {completed.returncode}"
            raise OSError(f"cannot create directory junction: {detail}")
        return
    path.symlink_to(target)


SUPPORTED_AUDIENCES = ("chatgpt", "claude", "copilot")


def discover_skill_packages(
    repo_root: Path,
    *,
    audience: str | None = None,
) -> tuple[SkillPackage, ...]:
    source_root = repo_root / "skills"
    if not source_root.exists():
        return ()
    if audience is not None and audience not in SUPPORTED_AUDIENCES:
        raise SkillInstallationError(
            f"Unsupported skill audience {audience!r}; expected one of: "
            f"{', '.join(SUPPORTED_AUDIENCES)}."
        )

    packages: list[SkillPackage] = []
    skill_files = [*source_root.glob("*/SKILL.md")]
    if audience is None:
        audiences = SUPPORTED_AUDIENCES
    else:
        audiences = (audience,)
    for package_audience in audiences:
        skill_files.extend(source_root.glob(f"{package_audience}/*/SKILL.md"))
    for skill_file in skill_files:
        if not skill_file.is_file():
            continue
        skill_directory = skill_file.parent
        relative_parts = skill_directory.relative_to(source_root).parts
        package_audience = relative_parts[0] if len(relative_parts) == 2 else None
        packages.append(
            SkillPackage(
                name=skill_directory.name,
                source_directory=skill_directory,
                audience=package_audience,
            )
        )

    return tuple(sorted(packages, key=lambda package: package.name))


def resolve_copilot_skills_root(
    *,
    home: Path | None = None,
) -> Path:
    resolved_home = Path.home() if home is None else home
    resolved_home = resolved_home.expanduser().resolve()
    return resolved_home / ".agents" / "skills"


def resolve_claude_skills_root(
    *,
    home: Path | None = None,
) -> Path:
    """Claude Code discovers personal skill packages here."""
    resolved_home = Path.home() if home is None else home
    resolved_home = resolved_home.expanduser().resolve()
    return resolved_home / ".claude" / "skills"


def resolve_skills_root_for_audience(
    *,
    audience: str,
    home: Path | None = None,
) -> Path:
    if audience == "claude":
        return resolve_claude_skills_root(home=home)
    return resolve_copilot_skills_root(home=home)


def resolve_skill_installation_ownership_path(
    *,
    home: Path | None = None,
    os_name: str | None = None,
    appdata: str | None = None,
    xdg_config_home: str | None = None,
) -> Path:
    """Resolve the ownership ledger, keeping it inside whichever home was asked for.

    Precedence, most specific first:

    1. an explicitly supplied ``appdata`` / ``xdg_config_home`` wins;
    2. otherwise an explicitly supplied ``home`` keeps the ledger under that home
       and ambient machine config is deliberately ignored;
    3. otherwise ordinary host behavior applies, using ambient config when set.

    Rule 2 exists because the skills destination already honors ``home``. Without
    it, an install aimed at an alternate home would reconcile against the real
    user's machine-global ledger and could remove packages it does not own.
    """
    resolved_os = os.name if os_name is None else os_name
    explicit_home = home is not None
    resolved_home = Path.home() if home is None else home
    resolved_home = resolved_home.expanduser().resolve()

    if resolved_os == "nt":
        explicit_config = appdata
        ambient_config = None if explicit_home else os.environ.get("APPDATA")
        home_relative = ("AppData", "Roaming", "ai-dev")
    else:
        explicit_config = xdg_config_home
        ambient_config = None if explicit_home else os.environ.get("XDG_CONFIG_HOME")
        home_relative = (".config", "ai-dev")

    config_text = (explicit_config if explicit_config is not None else ambient_config or "").strip()
    if config_text:
        base_dir = Path(config_text).expanduser() / "ai-dev"
    else:
        base_dir = resolved_home.joinpath(*home_relative)

    return base_dir / "skill-installation-ownership.json"


def _path_module_for_platform(platform: str):
    return ntpath if platform == "windows" else posixpath


def _normalize_path_text_for_platform(path_text: str, *, platform: str, casefold: bool = False) -> str:
    path_module = _path_module_for_platform(platform)
    normalized = path_module.normpath(path_text)
    if casefold and platform == "windows":
        return ntpath.normcase(normalized)
    return normalized


def _normalized_absolute_path_text_for_platform(path: Path, *, platform: str) -> str:
    absolute = path.expanduser().resolve(strict=False)
    return _normalize_path_text_for_platform(str(absolute), platform=platform)


def _normalized_path_identity_text_for_platform(path: Path, *, platform: str) -> str:
    absolute = path.expanduser().absolute()
    return _normalize_path_text_for_platform(str(absolute), platform=platform)


def _normalized_literal_symlink_target_text(path: Path, *, platform: str) -> str | None:
    if not path_is_managed_link(path):
        return None
    target_text = read_link_target_text(path)
    if target_text is None:
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


def _is_normalized_path_text(path_text: str, *, platform: str) -> bool:
    return _normalize_path_text_for_platform(path_text, platform=platform) == path_text


def _load_owned_skill_symlinks(path: Path, *, platform: str) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SkillInstallationError(f"Cannot read skill installation ownership record {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SkillInstallationError(f"Invalid skill installation ownership record {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SkillInstallationError(f"Invalid skill installation ownership record {path}: expected object.")

    version = raw.get("version")
    if version != SKILL_INSTALLATION_OWNERSHIP_VERSION:
        raise SkillInstallationError(
            f"Unsupported skill installation ownership record version in {path}: {version!r}."
        )

    owned_skills_raw = raw.get("owned_skills")
    if not isinstance(owned_skills_raw, dict):
        raise SkillInstallationError(
            f"Invalid skill installation ownership record in {path}: owned_skills must be an object."
        )

    owned_skills: dict[str, str] = {}
    for destination_path, ownership in owned_skills_raw.items():
        if not isinstance(destination_path, str) or not destination_path:
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: destination paths must be non-empty strings."
            )
        if not _is_normalized_path_text(destination_path, platform=platform):
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: destination path must be normalized."
            )
        if not Path(destination_path).is_absolute():
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: destination path must be absolute."
            )
        if not isinstance(ownership, str) or not ownership.startswith(_OWNERSHIP_PREFIXES):
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: ownership must be a symlink marker."
            )

        target_path = _ownership_target_text(ownership)
        if not target_path:
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: ownership target may not be empty."
            )
        if not _is_normalized_path_text(target_path, platform=platform):
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: ownership target must be normalized."
            )
        if not Path(target_path).is_absolute():
            raise SkillInstallationError(
                f"Invalid skill installation ownership record in {path}: ownership target must be absolute."
            )

        normalized_destination = _normalize_path_text_for_platform(destination_path, platform=platform)
        owned_skills[normalized_destination] = ownership

    return dict(sorted(owned_skills.items(), key=lambda item: item[0]))


def _save_owned_skill_symlinks(path: Path, *, owned_skills: dict[str, str]) -> None:
    payload = {
        "version": SKILL_INSTALLATION_OWNERSHIP_VERSION,
        "owned_skills": {
            destination_path: ownership
            for destination_path, ownership in sorted(owned_skills.items(), key=lambda item: item[0])
        },
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SkillInstallationError(f"Cannot write skill installation ownership record {path}: {exc}") from exc


def _symlink_ownership_value(target_path: Path, *, platform: str, kind: str = LINK_KIND_SYMLINK) -> str:
    normalized_target = _normalized_absolute_path_text_for_platform(target_path, platform=platform)
    return f"{_ownership_prefix_for_kind(kind)}{normalized_target}"


def _replace_skill_link(destination: Path, target: Path, *, kind: str) -> None:
    """Install or replace a managed link without ever destroying a working one.

    The replacement is created and validated at a staging path first. Only then
    is an existing destination removed. A failure anywhere before that leaves the
    previously installed package untouched.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.ai-dev-staging"

    if path_is_managed_link(staging):
        _remove_link(staging)
    elif staging.exists():
        raise OSError(f"staging path is occupied by unmanaged content: {staging}")

    _create_link(staging, target, kind=kind)

    # Prove the new link resolves to the intended package before touching the
    # working destination.
    staged_target = read_link_target_text(staging)
    if staged_target is None or Path(staged_target).resolve() != Path(target).resolve():
        _remove_link(staging)
        raise OSError(
            f"refusing to replace {destination}: staged link did not resolve to {target}"
        )

    if not path_is_managed_link(destination) and destination.exists():
        _remove_link(staging)
        raise OSError(f"destination is not a managed link: {destination}")

    # The working install is moved aside rather than removed, so a failure in the
    # final swap itself is still recoverable.
    backup = destination.parent / f".{destination.name}.ai-dev-backup"
    if path_is_managed_link(backup):
        _remove_link(backup)
    elif backup.exists():
        _remove_link(staging)
        raise OSError(f"backup path is occupied by unmanaged content: {backup}")

    had_destination = path_is_managed_link(destination)
    if had_destination:
        os.rename(destination, backup)

    try:
        os.rename(staging, destination)
    except OSError as swap_error:
        if had_destination:
            try:
                if not path_is_managed_link(destination) and not destination.exists():
                    os.rename(backup, destination)
            except OSError as restore_error:
                # Never discard the only surviving copy of the working install.
                raise OSError(
                    f"failed to install {destination} and could not restore the previous "
                    f"package automatically ({restore_error}). The previous working link is "
                    f"preserved at {backup}; move it back to {destination} to recover."
                ) from swap_error
        if path_is_managed_link(staging):
            _remove_link(staging)
        raise

    if had_destination and path_is_managed_link(backup):
        _remove_link(backup)


def _path_exists_or_symlink(path: Path) -> bool:  # noqa: D401 - historical name
    return path.exists() or path_is_managed_link(path)


def _reconcile_obsolete_managed_skills(
    *,
    desired_destination_keys: set[str],
    owned_skills: dict[str, str],
    platform: str,
) -> tuple[dict[str, str], tuple[SkillInstallStatus, ...]]:
    updated_owned_skills = dict(owned_skills)
    statuses: list[SkillInstallStatus] = []

    obsolete_keys = sorted(key for key in owned_skills if key not in desired_destination_keys)
    for destination_key in obsolete_keys:
        destination_path = Path(destination_key)
        ownership_value = owned_skills[destination_key]
        expected_target = _ownership_target_text(ownership_value)

        if not _path_exists_or_symlink(destination_path):
            updated_owned_skills.pop(destination_key, None)
            statuses.append(
                SkillInstallStatus(
                    name=destination_path.name,
                    state="already-absent",
                    destination_directory=destination_path,
                )
            )
            continue

        if not path_is_managed_link(destination_path):
            raise SkillInstallationError(
                "Cannot reconcile obsolete managed skill because destination is not a symlink: "
                f"{destination_path}"
            )

        actual_target = _normalized_symlink_target_text(destination_path, platform=platform)
        if actual_target != expected_target:
            raise SkillInstallationError(
                "Cannot reconcile obsolete managed skill because link target diverged: "
                f"{destination_path}"
            )

        try:
            _remove_link(destination_path)
        except OSError as exc:
            raise SkillInstallationError(
                f"Cannot remove obsolete managed skill link {destination_path}: {exc}"
            ) from exc

        updated_owned_skills.pop(destination_key, None)
        statuses.append(
            SkillInstallStatus(
                name=destination_path.name,
                state="removed",
                destination_directory=destination_path,
            )
        )

    return updated_owned_skills, tuple(statuses)


def install_skill_packages(
    *,
    repo_root: Path,
    destination_root: Path,
    home: Path | None = None,
    audience: str = "copilot",
) -> SkillInstallResult:
    packages = discover_skill_packages(repo_root, audience=audience)
    package_names = [package.name for package in packages]
    if len(package_names) != len(set(package_names)):
        duplicates = sorted(
            name for name in set(package_names) if package_names.count(name) > 1
        )
        raise SkillInstallationError(
            "Selected skill audience contains duplicate package names: "
            f"{', '.join(duplicates)}."
        )
    source_root = repo_root / "skills"
    if not source_root.exists():
        raise SkillInstallationError(f"skills directory not found: {source_root}")

    destination_root.mkdir(parents=True, exist_ok=True)

    platform = "windows" if os.name == "nt" else "posix"
    link_kind = preferred_link_kind(platform)
    ownership_path = resolve_skill_installation_ownership_path(home=home)
    owned_skills = _load_owned_skill_symlinks(ownership_path, platform=platform)
    owned_skills_updated = dict(owned_skills)

    statuses: list[SkillInstallStatus] = []
    installed_count = 0
    updated_count = 0
    unchanged_count = 0
    desired_destination_keys: set[str] = set()

    for package in packages:
        destination_directory = destination_root / package.name
        desired_target = package.source_directory.resolve()
        desired_target_text = _normalized_absolute_path_text_for_platform(desired_target, platform=platform)
        destination_key = _normalized_path_identity_text_for_platform(destination_directory, platform=platform)
        desired_destination_keys.add(destination_key)

        if not _path_exists_or_symlink(destination_directory):
            try:
                _replace_skill_link(destination_directory, desired_target, kind=link_kind)
            except OSError as exc:
                raise SkillInstallationError(
                    f"Cannot create managed skill symlink for {package.name} at {destination_directory}: {exc}"
                ) from exc

            owned_skills_updated[destination_key] = _symlink_ownership_value(
                desired_target, platform=platform, kind=link_kind
            )
            installed_count += 1
            statuses.append(
                SkillInstallStatus(
                    name=package.name,
                    state="installed",
                    destination_directory=destination_directory,
                )
            )
            continue

        if not path_is_managed_link(destination_directory):
            raise SkillInstallationError(
                f"Cannot install skill {package.name}: destination exists and is not a managed "
                f"symlink or junction: {destination_directory}"
            )

        actual_target = _normalized_symlink_target_text(destination_directory, platform=platform)
        if actual_target == desired_target_text:
            unchanged_count += 1
            statuses.append(
                SkillInstallStatus(
                    name=package.name,
                    state="unchanged",
                    destination_directory=destination_directory,
                )
            )
            continue

        recorded_ownership = owned_skills.get(destination_key)
        owned_and_proven = (
            recorded_ownership is not None
            and recorded_ownership.startswith(_OWNERSHIP_PREFIXES)
            and actual_target == _ownership_target_text(recorded_ownership)
        )
        if not owned_and_proven:
            raise SkillInstallationError(
                f"Cannot install skill {package.name}: conflicting unmanaged or divergent symlink at {destination_directory}"
            )

        try:
            _replace_skill_link(destination_directory, desired_target, kind=link_kind)
        except OSError as exc:
            raise SkillInstallationError(
                f"Cannot update managed skill link for {package.name} at {destination_directory}: {exc}"
            ) from exc

        owned_skills_updated[destination_key] = _symlink_ownership_value(
            desired_target, platform=platform, kind=link_kind
        )
        updated_count += 1
        statuses.append(
            SkillInstallStatus(
                name=package.name,
                state="updated",
                destination_directory=destination_directory,
            )
        )

    owned_skills_updated, obsolete_statuses = _reconcile_obsolete_managed_skills(
        desired_destination_keys=desired_destination_keys,
        owned_skills=owned_skills_updated,
        platform=platform,
    )
    statuses.extend(obsolete_statuses)

    _save_owned_skill_symlinks(ownership_path, owned_skills=owned_skills_updated)

    return SkillInstallResult(
        source_root=source_root,
        destination_root=destination_root,
        discovered_count=len(packages),
        installed_count=installed_count,
        updated_count=updated_count,
        unchanged_count=unchanged_count,
        statuses=tuple(statuses),
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.skill_installation",
        description="Install shared and selected-audience skill packages.",
    )
    parser.add_argument("--repo-root", required=True, help="Repository root path.")
    parser.add_argument(
        "--destination-root",
        default="",
        help="Optional explicit destination directory. Defaults to ~/.agents/skills.",
    )
    parser.add_argument(
        "--audience",
        choices=SUPPORTED_AUDIENCES,
        default="copilot",
        help="Audience to install, including shared root skills (default: copilot).",
    )
    parser.add_argument("--home", default="", help="Optional home override for destination resolution.")
    return parser.parse_args(argv)


def _print_result(result: SkillInstallResult) -> None:
    for status in result.statuses:
        print(f"{status.state.capitalize()}: {status.name}")

    print(
        "Skill installation complete: "
        f"{result.installed_count} installed, "
        f"{result.updated_count} updated, "
        f"{result.unchanged_count} unchanged."
    )
    print(f"Source: {result.source_root}")
    print(f"Destination: {result.destination_root}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    repo_root = Path(args.repo_root).expanduser().resolve()
    if args.destination_root.strip():
        destination_root = Path(args.destination_root).expanduser().resolve()
        home = Path(args.home).expanduser() if args.home.strip() else None
    else:
        home = Path(args.home).expanduser() if args.home.strip() else None
        destination_root = resolve_skills_root_for_audience(
            audience=args.audience,
            home=home,
        )

    try:
        result = install_skill_packages(
            repo_root=repo_root,
            destination_root=destination_root,
            home=home,
            audience=args.audience,
        )
    except SkillInstallationError as exc:
        print(f"skill-installation: {exc}", file=sys.stderr)
        return 1

    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
