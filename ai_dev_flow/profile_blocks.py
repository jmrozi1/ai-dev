from __future__ import annotations

from dataclasses import dataclass


MARKER_BEGIN = "# >>> ai-dev managed aliases >>>"
MARKER_END = "# <<< ai-dev managed aliases <<<"


class ProfileBlockError(Exception):
    """Raised for malformed or unsafe profile block operations."""


@dataclass(frozen=True)
class ProfileUpdateResult:
    text: str
    changed: bool


def _find_marker_positions(text: str) -> tuple[int, int, int, int]:
    begin_count = text.count(MARKER_BEGIN)
    end_count = text.count(MARKER_END)

    if begin_count != end_count:
        raise ProfileBlockError(
            "Malformed AI Dev marker block in profile: begin/end marker counts do not match."
        )

    if begin_count > 1:
        raise ProfileBlockError(
            "Malformed AI Dev marker block in profile: duplicate managed marker blocks found."
        )

    if begin_count == 0:
        return -1, -1, 0, 0

    begin_index = text.find(MARKER_BEGIN)
    end_index = text.find(MARKER_END)
    if end_index < begin_index:
        raise ProfileBlockError(
            "Malformed AI Dev marker block in profile: end marker appears before begin marker."
        )

    return begin_index, end_index, begin_count, end_count


def _managed_block_bounds(profile_text: str, begin_index: int, end_index: int) -> tuple[int, int]:
    start = begin_index
    if begin_index > 0 and profile_text[begin_index - 1] == "\n":
        # The separator newline immediately before marker is AI Dev-owned.
        start = begin_index - 1

    end_line_end = profile_text.find("\n", end_index)
    if end_line_end == -1:
        end_line_end = len(profile_text)
    else:
        end_line_end += 1

    return start, end_line_end


def _render_managed_block(content_line: str, *, include_leading_separator: bool) -> str:
    prefix = "\n" if include_leading_separator else ""
    return f"{prefix}{MARKER_BEGIN}\n{content_line}\n{MARKER_END}\n"


def upsert_managed_block(profile_text: str, content_line: str) -> ProfileUpdateResult:
    begin_index, end_index, begin_count, _ = _find_marker_positions(profile_text)

    if begin_count == 0:
        block = _render_managed_block(
            content_line,
            include_leading_separator=bool(profile_text),
        )
        return ProfileUpdateResult(text=f"{profile_text}{block}", changed=True)

    managed_start, managed_end = _managed_block_bounds(profile_text, begin_index, end_index)
    block = _render_managed_block(
        content_line,
        include_leading_separator=(managed_start < begin_index),
    )
    existing = profile_text[managed_start:managed_end]
    if existing == block:
        return ProfileUpdateResult(text=profile_text, changed=False)

    updated = f"{profile_text[:managed_start]}{block}{profile_text[managed_end:]}"
    return ProfileUpdateResult(text=updated, changed=True)


def remove_managed_block(profile_text: str) -> ProfileUpdateResult:
    begin_index, end_index, begin_count, _ = _find_marker_positions(profile_text)
    if begin_count == 0:
        return ProfileUpdateResult(text=profile_text, changed=False)

    managed_start, managed_end = _managed_block_bounds(profile_text, begin_index, end_index)
    updated = f"{profile_text[:managed_start]}{profile_text[managed_end:]}"
    return ProfileUpdateResult(text=updated, changed=True)
