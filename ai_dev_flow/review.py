from __future__ import annotations

from pathlib import Path

class ReviewError(Exception):
    """Raised for review generation or output failures."""


def resolve_review_output_path(
    repo_root: Path,
    configured_out: str | None,
) -> Path | None:
    if configured_out is None:
        return None

    destination = Path(configured_out)
    if destination.is_absolute():
        return destination

    return repo_root / destination