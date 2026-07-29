from __future__ import annotations

from pathlib import Path
import subprocess

from .summarize_glob import SummarizeGlobError, normalize_path_text, validate_requested_glob


DEFAULT_SUMMARIZE_EXCLUDED_PREFIXES = (
    ".ai-dev/",
    "ai-docs/",
    "artifacts/",
)


class SummarizeDiscoveryError(Exception):
    """Raised when deterministic summarize source discovery fails."""


def _normalize_repo_relative_path(path_text: str) -> str:
    normalized = normalize_path_text(path_text)
    if not normalized:
        raise SummarizeDiscoveryError("discovered source path cannot be empty.")

    if normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
        raise SummarizeDiscoveryError(f"Discovered path is outside repository root: {path_text}")

    return normalized


def _is_excluded_by_prefix(path_text: str) -> bool:
    return any(path_text == prefix.rstrip("/") or path_text.startswith(prefix) for prefix in DEFAULT_SUMMARIZE_EXCLUDED_PREFIXES)


def discover_source_paths(repo_root: Path, requested_glob: str) -> tuple[str, ...]:
    try:
        normalized_glob = validate_requested_glob(requested_glob)
    except SummarizeGlobError as exc:
        raise SummarizeDiscoveryError(str(exc)) from exc

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            f":(glob){normalized_glob}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = stderr if stderr else f"exit code {completed.returncode}"
        raise SummarizeDiscoveryError(f"Cannot discover summarize sources: {detail}")

    discovered = completed.stdout.decode("utf-8", errors="surrogateescape")
    candidates = [item for item in discovered.split("\x00") if item]

    unique_paths = {
        _normalize_repo_relative_path(path_text)
        for path_text in candidates
    }
    filtered_paths = sorted(path for path in unique_paths if not _is_excluded_by_prefix(path))

    if not filtered_paths:
        raise SummarizeDiscoveryError(f"No source files matched summarize glob: {normalized_glob!r}.")

    return tuple(filtered_paths)
