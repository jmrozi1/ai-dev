from __future__ import annotations

import re


_WILDCARD_CHARS = frozenset({"*", "?", "[", "]"})
_DRIVE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class SummarizeGlobError(Exception):
    """Raised when summarize glob input is invalid."""


def normalize_path_text(path_text: str) -> str:
    normalized = path_text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _has_parent_traversal(path_text: str) -> bool:
    return any(part == ".." for part in path_text.split("/"))


def _validate_glob_syntax(pattern: str, context: str) -> None:
    if "{" in pattern or "}" in pattern:
        raise SummarizeGlobError(f"{context} cannot include '{{' or '}}' brace expansion syntax.")

    index = 0
    while index < len(pattern):
        if pattern[index] != "[":
            index += 1
            continue

        closing_index = pattern.find("]", index + 1)
        if closing_index == -1:
            raise SummarizeGlobError(f"{context} contains an unclosed character class '['.")

        class_content = pattern[index + 1 : closing_index]
        if not class_content:
            raise SummarizeGlobError(f"{context} contains an empty character class '[]'.")

        if class_content == "!":
            raise SummarizeGlobError(
                f"{context} contains an invalid negated character class '[!]'."
            )

        index = closing_index + 1


def _validate_glob_pattern(glob_text: str, context: str) -> str:
    normalized = normalize_path_text(glob_text.strip())
    if not normalized:
        raise SummarizeGlobError(f"{context} cannot be empty.")

    if normalized.startswith(":"):
        raise SummarizeGlobError(f"{context} cannot start with ':'.")

    if normalized.startswith("/") or normalized.startswith("//") or _DRIVE_PATH_PATTERN.match(normalized):
        raise SummarizeGlobError(f"{context} must be repository-relative.")

    if _has_parent_traversal(normalized):
        raise SummarizeGlobError(f"{context} cannot include '..' path traversal segments.")

    _validate_glob_syntax(normalized, context)

    return normalized


def validate_requested_glob(glob_text: str) -> str:
    return _validate_glob_pattern(glob_text, "summarize glob")


def validate_rule_match_glob(glob_text: str) -> str:
    return _validate_glob_pattern(glob_text, "summarize rule match")


def _escape_regexp(value: str) -> str:
    if value in {"-", "/", "\\", "^", "$", "+", "?", ".", "(", ")", "|", "[", "]", "{", "}"}:
        return f"\\{value}"
    return value


def _character_class_to_regex(class_content: str) -> str:
    negated = class_content.startswith("!")
    members = class_content[1:] if negated else class_content

    escaped_members: list[str] = []
    for index, char in enumerate(members):
        if char == "\\" or char == "]":
            escaped_members.append(f"\\{char}")
            continue

        if index == 0 and char == "^":
            escaped_members.append("\\^")
            continue

        escaped_members.append(char)

    prefix = "^" if negated else ""
    return f"[{prefix}{''.join(escaped_members)}]"


def glob_to_regexp(glob_pattern: str) -> re.Pattern[str]:
    normalized = validate_rule_match_glob(glob_pattern)
    regex_source: list[str] = []
    index = 0

    while index < len(normalized):
        char = normalized[index]
        if char == "*":
            is_double_star = index + 1 < len(normalized) and normalized[index + 1] == "*"
            if is_double_star:
                has_following_slash = index + 2 < len(normalized) and normalized[index + 2] == "/"
                if has_following_slash:
                    regex_source.append("(?:.*/)?")
                    index += 3
                    continue

                regex_source.append(".*")
                index += 2
                continue

            regex_source.append("[^/]*")
            index += 1
            continue

        if char == "?":
            regex_source.append("[^/]")
            index += 1
            continue

        if char == "[":
            closing_index = normalized.find("]", index + 1)
            # Syntax is validated before conversion; this is a defensive guard.
            if closing_index == -1:
                raise SummarizeGlobError("glob pattern contains an unclosed character class '['.")
            class_content = normalized[index + 1 : closing_index]
            regex_source.append(_character_class_to_regex(class_content))
            index = closing_index + 1
            continue

        regex_source.append(_escape_regexp(char))
        index += 1

    try:
        return re.compile(f"^{''.join(regex_source)}$")
    except re.error as exc:
        raise SummarizeGlobError(f"invalid glob pattern: {exc}") from exc


def matches_glob(path_text: str, glob_pattern: str) -> bool:
    normalized_path = normalize_path_text(path_text)
    return glob_to_regexp(glob_pattern).match(normalized_path) is not None


def rule_specificity_key(match_glob: str, declaration_index: int) -> tuple[int, int, int]:
    normalized = normalize_path_text(match_glob)
    components = [part for part in normalized.split("/") if part]

    literal_components = 0
    wildcard_components = 0
    for component in components:
        if any(char in _WILDCARD_CHARS for char in component):
            wildcard_components += 1
        else:
            literal_components += 1

    # Lower keys come first: general rules (fewer literals, more wildcards) before specific rules.
    return (literal_components, -wildcard_components, declaration_index)
