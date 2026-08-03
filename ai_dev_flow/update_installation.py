from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import sys
from typing import Any

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


INSTALLATION_SOURCE_VERSION = 1
DEFAULT_UPDATE_BRANCH = "main"
DEFAULT_UPDATE_REMOTE = "origin"


class UpdateInstallationError(Exception):
    """Raised for update metadata and update execution failures."""


@dataclass(frozen=True)
class InstallationSourceRecord:
    version: int
    source_repository: Path
    branch: str
    remote: str


@dataclass(frozen=True)
class UpdateSourceResult:
    source_status: str
    source_from: str | None
    source_to: str | None
    source_repo: Path
    branch: str
    remote: str


@dataclass(frozen=True)
class LauncherRefreshResult:
    status: str
    detail: str


@dataclass(frozen=True)
class ApplyRefreshResult:
    status: str
    detail: str


@dataclass(frozen=True)
class UpdateExecutionResult:
    source: UpdateSourceResult
    launcher: LauncherRefreshResult
    apply: ApplyRefreshResult


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_python_module(
    repo_root: Path,
    module: str,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{repo_root}{os.path.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = str(repo_root)

    return subprocess.run(
        [sys.executable, "-m", module, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _normalize_repo_root(repo_root: Path) -> Path:
    completed = _run_git(repo_root, ["rev-parse", "--show-toplevel"])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(
            f"Cannot resolve Git repository root for {repo_root}: {message}"
        )

    return Path(completed.stdout.strip()).expanduser().resolve()


def _verify_remote_exists(repo_root: Path, remote: str) -> None:
    completed = _run_git(repo_root, ["remote", "get-url", remote])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(
            f"Configured update remote is not available in {repo_root}: {remote}. {message}"
        )


def _verify_branch_exists(repo_root: Path, branch: str) -> None:
    completed = _run_git(repo_root, ["show-ref", "--verify", f"refs/heads/{branch}"])
    if completed.returncode != 0:
        raise UpdateInstallationError(
            f"Configured update branch does not exist locally in {repo_root}: {branch}."
        )


def _ensure_clean_source_checkout(repo_root: Path) -> None:
    completed = _run_git(repo_root, ["status", "--short", "--untracked-files=all"])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(
            f"Cannot inspect local changes in {repo_root}: {message}"
        )

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return

    preview = "\n".join(lines[:10])
    raise UpdateInstallationError(
        "Local changes detected in recorded source checkout.\n"
        f"Recorded source repository: {repo_root}\n"
        "AI Dev will not stash, reset, or discard local changes automatically.\n"
        "Git status hint:\n"
        f"{preview}"
    )


def _resolve_git_dir(repo_root: Path) -> Path:
    completed = _run_git(repo_root, ["rev-parse", "--git-dir"])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(
            f"Cannot resolve Git directory for {repo_root}: {message}"
        )

    git_dir_text = completed.stdout.strip()
    git_dir = Path(git_dir_text).expanduser()
    if not git_dir.is_absolute():
        git_dir = (repo_root / git_dir).resolve()
    else:
        git_dir = git_dir.resolve()
    return git_dir


def _ensure_no_active_operations(repo_root: Path) -> None:
    sentinels: list[tuple[str, str]] = [
        ("merge", "MERGE_HEAD"),
        ("rebase", "rebase-apply"),
        ("rebase", "rebase-merge"),
        ("cherry-pick", "CHERRY_PICK_HEAD"),
        ("revert", "REVERT_HEAD"),
        ("bisect", "BISECT_LOG"),
    ]

    active: list[str] = []
    git_dir = _resolve_git_dir(repo_root)
    for label, sentinel in sentinels:
        if (git_dir / sentinel).exists() and label not in active:
            active.append(label)

    if active:
        raise UpdateInstallationError(
            "Cannot update source checkout while Git has active operation(s): "
            f"{', '.join(active)}."
        )


def _resolve_platform_default_metadata_path(
    *,
    os_name: str,
    home: Path,
    appdata: str | None,
    xdg_config_home: str | None,
) -> Path:
    windows = os_name == "nt"
    if windows:
        appdata_text = (appdata or "").strip()
        if appdata_text:
            return Path(appdata_text).expanduser() / "ai-dev" / "installation-source.json"
        return home / "AppData" / "Roaming" / "ai-dev" / "installation-source.json"

    xdg_text = (xdg_config_home or "").strip()
    if xdg_text:
        return Path(xdg_text).expanduser() / "ai-dev" / "installation-source.json"
    return home / ".config" / "ai-dev" / "installation-source.json"


def resolve_installation_source_path(
    *,
    os_name: str | None = None,
    home: Path | None = None,
    appdata: str | None = None,
    xdg_config_home: str | None = None,
) -> Path:
    resolved_os = os.name if os_name is None else os_name
    resolved_home = Path.home() if home is None else home
    resolved_home = resolved_home.expanduser().resolve()

    resolved_appdata = appdata
    resolved_xdg = xdg_config_home
    if resolved_appdata is None:
        resolved_appdata = os.environ.get("APPDATA")
    if resolved_xdg is None:
        resolved_xdg = os.environ.get("XDG_CONFIG_HOME")

    return _resolve_platform_default_metadata_path(
        os_name=resolved_os,
        home=resolved_home,
        appdata=resolved_appdata,
        xdg_config_home=resolved_xdg,
    )


def _validate_record_payload(payload: dict[str, Any], path: Path) -> InstallationSourceRecord:
    version = payload.get("version")
    if version != INSTALLATION_SOURCE_VERSION:
        raise UpdateInstallationError(
            f"Unsupported installation source metadata version in {path}: {version!r}."
        )

    source_repository_raw = payload.get("source_repository")
    if not isinstance(source_repository_raw, str) or not source_repository_raw.strip():
        raise UpdateInstallationError(
            f"Invalid installation source metadata in {path}: source_repository must be a non-empty string."
        )

    source_repository = Path(source_repository_raw).expanduser()
    if not source_repository.is_absolute():
        raise UpdateInstallationError(
            f"Invalid installation source metadata in {path}: source_repository must be an absolute path."
        )

    branch = payload.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise UpdateInstallationError(
            f"Invalid installation source metadata in {path}: branch must be a non-empty string."
        )

    remote = payload.get("remote")
    if not isinstance(remote, str) or not remote.strip():
        raise UpdateInstallationError(
            f"Invalid installation source metadata in {path}: remote must be a non-empty string."
        )

    return InstallationSourceRecord(
        version=INSTALLATION_SOURCE_VERSION,
        source_repository=source_repository.resolve(),
        branch=branch.strip(),
        remote=remote.strip(),
    )


def load_installation_source_record(path: Path) -> InstallationSourceRecord:
    if not path.exists():
        raise UpdateInstallationError(
            "Installation source metadata is missing.\n"
            f"Expected metadata file: {path}\n"
            "Rerun bootstrap from a valid AI Dev checkout to repair installation metadata."
        )

    try:
        payload = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise UpdateInstallationError(str(exc)) from exc

    return _validate_record_payload(payload, path)


def write_installation_source_record(
    *,
    metadata_path: Path,
    source_repository: Path,
    branch: str,
    remote: str,
) -> None:
    source_abs = source_repository.expanduser().resolve()
    if not source_abs.is_absolute():
        raise UpdateInstallationError(
            f"Installation source path must be absolute: {source_repository}"
        )

    payload: dict[str, Any] = {
        "version": INSTALLATION_SOURCE_VERSION,
        "source_repository": str(source_abs),
        "branch": branch,
        "remote": remote,
    }
    try:
        write_json_object_atomic(metadata_path, payload)
    except JsonFileError as exc:
        raise UpdateInstallationError(str(exc)) from exc


def refresh_installation_source_record(
    *,
    repo_root: Path,
    metadata_path: Path,
    branch: str,
    remote: str,
) -> Path:
    canonical_repo_root = _normalize_repo_root(repo_root)
    if not branch.strip():
        raise UpdateInstallationError("Approved update branch must be non-empty.")
    if not remote.strip():
        raise UpdateInstallationError("Approved update remote must be non-empty.")

    write_installation_source_record(
        metadata_path=metadata_path,
        source_repository=canonical_repo_root,
        branch=branch.strip(),
        remote=remote.strip(),
    )

    return canonical_repo_root


def _resolve_commit(repo_root: Path, revision: str) -> str:
    completed = _run_git(repo_root, ["rev-parse", "--verify", revision])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(message)
    return completed.stdout.strip()


def _git_current_branch(repo_root: Path) -> str:
    completed = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
        raise UpdateInstallationError(f"Cannot determine current branch in {repo_root}: {message}")
    return completed.stdout.strip()


def _git_is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    completed = _run_git(repo_root, ["merge-base", "--is-ancestor", ancestor, descendant])
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    message = completed.stderr.strip() or completed.stdout.strip() or "Unknown Git error"
    raise UpdateInstallationError(message)


def _run_launcher_refresh(repo_root: Path, *, branch: str, remote: str) -> LauncherRefreshResult:
    platform = "windows" if os.name == "nt" else "posix"
    completed = _run_python_module(
        repo_root,
        "ai_dev_flow.bootstrap",
        [
            "--platform",
            platform,
            "--repo-root",
            str(repo_root),
            "--command-name",
            "ai-dev",
            "--update-branch",
            branch,
            "--update-remote",
            remote,
        ],
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "bootstrap failed"
        return LauncherRefreshResult(status="failed", detail=detail)

    states: list[str] = []
    for line in completed.stdout.splitlines():
        if line.startswith("Launcher:") and "(" in line and line.endswith(")"):
            states.append(line.rsplit("(", 1)[1][:-1])

    if any(state in {"installed", "updated"} for state in states):
        return LauncherRefreshResult(status="updated", detail="launcher refreshed")
    return LauncherRefreshResult(status="unchanged", detail="launcher already up to date")


def _run_apply_with_updated_source(repo_root: Path) -> ApplyRefreshResult:
    completed = _run_python_module(repo_root, "ai_dev_flow.cli", ["apply"])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "apply failed"
        retry_command = (
            f"Retry with: PYTHONPATH='{repo_root}' {sys.executable} -m ai_dev_flow.cli apply"
        )
        return ApplyRefreshResult(
            status="failed",
            detail=f"{detail}\n{retry_command}",
        )

    detail = completed.stdout.strip() or "apply completed"
    return ApplyRefreshResult(status="completed", detail=detail)


def run_update_from_record(metadata_path: Path) -> UpdateExecutionResult:
    record = load_installation_source_record(metadata_path)
    source_repo = record.source_repository

    if not source_repo.exists() or not source_repo.is_dir():
        raise UpdateInstallationError(
            "Recorded source repository path is missing.\n"
            f"Recorded source repository: {source_repo}\n"
            "Rerun bootstrap from a valid AI Dev checkout to repair installation metadata."
        )

    canonical_root = _normalize_repo_root(source_repo)

    _verify_remote_exists(canonical_root, record.remote)
    _verify_branch_exists(canonical_root, record.branch)
    _ensure_no_active_operations(canonical_root)
    _ensure_clean_source_checkout(canonical_root)

    current_branch = _git_current_branch(canonical_root)
    if current_branch != record.branch:
        switched = _run_git(canonical_root, ["checkout", record.branch])
        if switched.returncode != 0:
            message = switched.stderr.strip() or switched.stdout.strip() or "Unknown Git error"
            raise UpdateInstallationError(
                f"Cannot switch to approved branch {record.branch} in {canonical_root}: {message}"
            )

    local_before = _resolve_commit(canonical_root, record.branch)

    fetched = _run_git(canonical_root, ["fetch", record.remote])
    if fetched.returncode != 0:
        message = fetched.stderr.strip() or fetched.stdout.strip() or "Unknown Git/network error"
        raise UpdateInstallationError(
            f"Cannot fetch {record.remote} for {canonical_root}: {message}"
        )

    remote_ref = f"{record.remote}/{record.branch}"
    remote_head = _resolve_commit(canonical_root, remote_ref)

    source_result: UpdateSourceResult
    if local_before == remote_head:
        source_result = UpdateSourceResult(
            source_status="already up to date",
            source_from=local_before,
            source_to=remote_head,
            source_repo=canonical_root,
            branch=record.branch,
            remote=record.remote,
        )
    else:
        if not _git_is_ancestor(canonical_root, local_before, remote_head):
            raise UpdateInstallationError(
                "Cannot fast-forward recorded source checkout because local and remote branches diverged.\n"
                f"Recorded source repository: {canonical_root}\n"
                f"Local branch: {record.branch} ({local_before})\n"
                f"Remote branch: {remote_ref} ({remote_head})\n"
                "AI Dev will not merge, rebase, reset, or force-update automatically."
            )

        merged = _run_git(canonical_root, ["merge", "--ff-only", remote_ref])
        if merged.returncode != 0:
            message = merged.stderr.strip() or merged.stdout.strip() or "Unknown Git error"
            raise UpdateInstallationError(
                f"Cannot fast-forward {record.branch} from {remote_ref}: {message}"
            )

        local_after = _resolve_commit(canonical_root, record.branch)
        source_result = UpdateSourceResult(
            source_status="fast-forwarded",
            source_from=local_before,
            source_to=local_after,
            source_repo=canonical_root,
            branch=record.branch,
            remote=record.remote,
        )

    launcher_result = _run_launcher_refresh(
        canonical_root,
        branch=record.branch,
        remote=record.remote,
    )
    if launcher_result.status == "failed":
        apply_result = ApplyRefreshResult(
            status="failed",
            detail="apply was not executed because launcher refresh failed",
        )
    else:
        apply_result = _run_apply_with_updated_source(canonical_root)

    return UpdateExecutionResult(
        source=source_result,
        launcher=launcher_result,
        apply=apply_result,
    )
