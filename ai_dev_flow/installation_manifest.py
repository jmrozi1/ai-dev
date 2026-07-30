from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any

from .json_files import JsonFileError, load_json_object, write_json_object_atomic


MANIFEST_VERSION = 1


class InstallationManifestError(Exception):
    """Raised for managed alias installation manifest errors."""


@dataclass(frozen=True)
class InstallationManifest:
    version: int
    profile_path: Path
    profile_sha256: str
    alias_file_path: Path
    alias_file_sha256: str
    aliases: dict[str, str]


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _require_sha256_field(payload: dict[str, Any], key: str, *, path: Path) -> str:
    value = _require_str_field(payload, key, path=path)
    if _SHA256_PATTERN.match(value) is None:
        raise InstallationManifestError(
            f"Invalid manifest in {path}: field {key!r} must be a lowercase 64-char SHA-256 digest."
        )
    return value


def _require_normalized_absolute_path(payload: dict[str, Any], key: str, *, path: Path) -> Path:
    value = _require_str_field(payload, key, path=path)
    parsed = Path(value)
    if not parsed.is_absolute():
        raise InstallationManifestError(
            f"Invalid manifest in {path}: field {key!r} must be an absolute path."
        )

    normalized = os.path.normpath(value)
    if normalized != value:
        raise InstallationManifestError(
            f"Invalid manifest in {path}: field {key!r} must be normalized."
        )

    return parsed



def _require_str_field(payload: dict[str, Any], key: str, *, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InstallationManifestError(
            f"Invalid manifest in {path}: field {key!r} must be a non-empty string."
        )
    return value


def _require_alias_map(payload: dict[str, Any], *, path: Path) -> dict[str, str]:
    aliases = payload.get("aliases")
    if not isinstance(aliases, dict):
        raise InstallationManifestError(
            f"Invalid manifest in {path}: field 'aliases' must be an object."
        )

    normalized: dict[str, str] = {}
    for key, value in aliases.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InstallationManifestError(
                f"Invalid manifest in {path}: aliases keys and values must be strings."
            )
        normalized[key] = value

    return dict(sorted(normalized.items(), key=lambda item: item[0]))


def load_manifest(path: Path) -> InstallationManifest | None:
    if not path.exists():
        return None

    try:
        raw = load_json_object(path, missing_default={})
    except JsonFileError as exc:
        raise InstallationManifestError(str(exc)) from exc

    if not isinstance(raw, dict):
        raise InstallationManifestError(f"Invalid manifest in {path}: expected JSON object.")

    version = raw.get("version")
    if version != MANIFEST_VERSION:
        raise InstallationManifestError(
            f"Invalid manifest in {path}: unsupported version {version!r}."
        )

    profile_path = _require_normalized_absolute_path(raw, "profile_path", path=path)
    profile_sha256 = _require_sha256_field(raw, "profile_sha256", path=path)
    alias_file_path = _require_normalized_absolute_path(raw, "alias_file_path", path=path)
    alias_file_sha256 = _require_sha256_field(raw, "alias_file_sha256", path=path)
    aliases = _require_alias_map(raw, path=path)

    return InstallationManifest(
        version=version,
        profile_path=profile_path,
        profile_sha256=profile_sha256,
        alias_file_path=alias_file_path,
        alias_file_sha256=alias_file_sha256,
        aliases=aliases,
    )


def save_manifest(path: Path, manifest: InstallationManifest) -> None:
    payload = {
        "version": manifest.version,
        "profile_path": str(manifest.profile_path),
        "profile_sha256": manifest.profile_sha256,
        "alias_file_path": str(manifest.alias_file_path),
        "alias_file_sha256": manifest.alias_file_sha256,
        "aliases": dict(sorted(manifest.aliases.items(), key=lambda item: item[0])),
    }

    try:
        write_json_object_atomic(path, payload)
    except JsonFileError as exc:
        raise InstallationManifestError(str(exc)) from exc
