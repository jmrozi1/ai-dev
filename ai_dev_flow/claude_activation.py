"""Claude-side installation, activation, and control-plane discovery.

Claude needs three things the other audiences already have by other means: a
host-level activation pointer so a product repository needs no repository-local
``CLAUDE.md``, one host-level clone of the coordination repository so no product
repository carries control-plane configuration, and a discovery path that turns
the current Git repository into the authorized rail.

Deterministic lifecycle behavior is not reimplemented here. Repository identity
comes from the existing ticket-provider normalization, ticket identity from Flow
workflow state, and rail authorization from :mod:`ai_dev_flow.control_plane`.
Everything in this module either resolves paths or fails closed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .control_plane import (
    ControlPlaneError,
    RailState,
    collect_rail_states,
    resolve_coordination_repo,
    resolve_read_source,
)
from .json_files import JsonFileError, load_json_object
from .repository import RepositoryError, resolve_repo_root, workflow_state_file_for_repo_root
from .ticket_providers import (
    GitRemoteGitHubCurrentRepositoryResolver,
    TicketProviderError,
)


class ClaudeActivationError(Exception):
    """Raised when Claude activation or discovery cannot proceed safely."""


DEFAULT_COORDINATION_REPOSITORY = "jmrozi1/ai-dev-control-plane"

MANAGED_BEGIN = "<!-- BEGIN ai-dev managed activation -->"
MANAGED_END = "<!-- END ai-dev managed activation -->"


# Paths -----------------------------------------------------------------------


def _resolved_home(home: Path | None) -> Path:
    resolved = Path.home() if home is None else home
    return resolved.expanduser().resolve()


def resolve_claude_instruction_path(*, home: Path | None = None) -> Path:
    """Host-level Claude instruction file, read at every session start."""
    return _resolved_home(home) / ".claude" / "CLAUDE.md"


def resolve_control_plane_cache_root(*, home: Path | None = None) -> Path:
    return _resolved_home(home) / ".ai-dev" / "control-plane"


def resolve_control_plane_cache(
    repository: str = DEFAULT_COORDINATION_REPOSITORY,
    *,
    home: Path | None = None,
) -> Path:
    """One host-level clone per coordination repository, never per product."""
    normalized = repository.strip().strip("/")
    if normalized.count("/") != 1 or not all(normalized.split("/")):
        raise ClaudeActivationError(
            f"Invalid coordination repository {repository!r}; expected owner/repo."
        )
    owner, name = normalized.split("/", 1)
    return resolve_control_plane_cache_root(home=home) / owner / name


# Activation pointer ----------------------------------------------------------


def render_activation_block() -> str:
    """The smallest durable instruction that makes bare `proceed` resolvable."""
    return "\n".join(
        (
            MANAGED_BEGIN,
            "## AI Dev executor activation",
            "",
            "In a Git repository that AI Dev supports, a bare `proceed` or `continue`",
            "is an executor instruction, not conversational continuation and not a",
            "request to recall a previous task from memory. Resolve durable state",
            "first, even immediately after `/clear`:",
            "",
            "```bash",
            "ai-dev discover",
            "```",
            "",
            "That reports the canonical repository identity, the active ticket and",
            "workspace, the control-plane scope, and the single authorized rail. Follow",
            "only that rail. If discovery fails, report exactly what it reported and",
            "stop: never substitute Claude memory, product documentation, an issue",
            "comment, a product-local handoff file, or a Flow checkpoint number.",
            MANAGED_END,
        )
    )


def _split_managed_block(text: str) -> tuple[str, str]:
    """Return (before, after) around exactly one managed block.

    Ambiguous or unbalanced markers fail closed rather than guessing which
    region belongs to AI Dev.
    """
    begins = text.count(MANAGED_BEGIN)
    ends = text.count(MANAGED_END)
    if begins != ends:
        raise ClaudeActivationError(
            "Cannot update Claude activation: unbalanced managed markers "
            f"({begins} begin, {ends} end). Repair the file by hand."
        )
    if begins > 1:
        raise ClaudeActivationError(
            "Cannot update Claude activation: multiple managed blocks are present. "
            "Repair the file by hand."
        )
    if begins == 0:
        return text, ""

    start = text.index(MANAGED_BEGIN)
    end = text.index(MANAGED_END) + len(MANAGED_END)
    if end <= start:
        raise ClaudeActivationError(
            "Cannot update Claude activation: managed end marker precedes its begin marker."
        )
    return text[:start], text[end:]


def sync_claude_activation(*, home: Path | None = None) -> str:
    """Insert or refresh only the managed block, preserving everything else."""
    path = resolve_claude_instruction_path(home=home)
    block = render_activation_block()

    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ClaudeActivationError(f"Cannot read {path}: {exc}") from exc

    before, after = _split_managed_block(existing)
    had_block = bool(existing) and (before, after) != (existing, "")

    if had_block:
        segments = [before.strip(), block, after.strip()]
    elif existing.strip():
        segments = [existing.strip(), block]
    else:
        segments = [block]
    composed = "\n\n".join(segment for segment in segments if segment) + "\n"

    if existing == composed:
        return "unchanged"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(composed, encoding="utf-8")
    except OSError as exc:
        raise ClaudeActivationError(f"Cannot write {path}: {exc}") from exc

    return "updated" if had_block else ("installed" if not existing else "inserted")


def ensure_control_plane_cache(
    repository: str = DEFAULT_COORDINATION_REPOSITORY,
    *,
    home: Path | None = None,
) -> tuple[Path, str]:
    """Establish or refresh the single host-level coordination clone.

    Uses the user's existing authenticated Git/GitHub access; no new credential
    store is introduced and nothing is written inside any product repository.
    """
    cache = resolve_control_plane_cache(repository, home=home)

    if (cache / ".git").exists():
        completed = subprocess.run(
            ["git", "-C", str(cache), "fetch", "--quiet", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ClaudeActivationError(
                f"Cannot refresh control-plane cache at {cache}: "
                f"{completed.stdout.strip() or completed.returncode}"
            )
        return cache, "refreshed"

    if cache.exists() and any(cache.iterdir()):
        raise ClaudeActivationError(
            f"Refusing to use {cache}: it exists but is not a Git clone. Move it aside."
        )

    cache.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "clone", "--quiet", f"https://github.com/{repository}.git", str(cache)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ClaudeActivationError(
            f"Cannot clone {repository} into {cache}: "
            f"{completed.stdout.strip() or completed.returncode}"
        )
    return cache, "cloned"


# Identity --------------------------------------------------------------------


@dataclass(frozen=True)
class ProductIdentity:
    repository: str
    owner: str
    project: str
    issue_number: int
    ticket: str


def resolve_product_identity(repo_root: Path) -> ProductIdentity:
    """Canonical Git identity plus the active Flow ticket, or fail closed."""
    try:
        repository = GitRemoteGitHubCurrentRepositoryResolver(
            repo_root=repo_root
        ).resolve_current_repository()
    except TicketProviderError as exc:
        raise ClaudeActivationError(
            f"Cannot resolve repository identity for {repo_root}. {exc}"
        ) from exc

    owner, name = repository.split("/", 1)

    state_path = workflow_state_file_for_repo_root(repo_root)
    try:
        state = load_json_object(state_path, missing_default={})
    except JsonFileError as exc:
        raise ClaudeActivationError(f"Cannot read Flow workflow state: {exc}") from exc

    issue_number = state.get("activeIssueNumber")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
        raise ClaudeActivationError(
            f"No active Flow issue in {state_path}. Start or resume a ticket workflow "
            "before asking for an authorized rail."
        )

    return ProductIdentity(
        repository=repository,
        owner=owner,
        project=name,
        issue_number=issue_number,
        ticket=f"issue-{issue_number}",
    )


# Authorization ---------------------------------------------------------------


def resolve_authorized_rail(
    cache: Path,
    *,
    project: str,
    ticket: str,
) -> RailState:
    """Exactly one ready rail, using the existing deterministic rail reader."""
    try:
        coordination = resolve_coordination_repo(cache)
    except ControlPlaneError as exc:
        raise ClaudeActivationError(
            f"Control-plane cache is not usable at {cache}. {exc}"
        ) from exc

    try:
        source = resolve_read_source(coordination)
        states = collect_rail_states(source, project=project, ticket=ticket)
    except ControlPlaneError as exc:
        raise ClaudeActivationError(
            f"Cannot read control-plane scope {project}/{ticket}. {exc}"
        ) from exc

    if not states:
        raise ClaudeActivationError(
            f"Control-plane scope {project}/{ticket} has no rails. The orchestrator "
            "owns rail authorization."
        )

    ready = [state for state in states if state.status == "ready"]
    if not ready:
        raise ClaudeActivationError(
            f"No rail in {project}/{ticket} is ready. Present rails: "
            + ", ".join(f"{state.identifier}={state.status}" for state in states)
        )
    if len(ready) > 1:
        raise ClaudeActivationError(
            f"More than one rail in {project}/{ticket} is ready: "
            + ", ".join(state.identifier for state in ready)
            + ". The orchestrator must name exactly one."
        )

    return ready[0]


# Discovery -------------------------------------------------------------------


def discover(
    repo_root: Path,
    *,
    home: Path | None = None,
    coordination_repository: str = DEFAULT_COORDINATION_REPOSITORY,
    cache: Path | None = None,
) -> dict:
    identity = resolve_product_identity(repo_root)
    resolved_cache = (
        cache if cache is not None else resolve_control_plane_cache(coordination_repository, home=home)
    )

    if not resolved_cache.exists():
        raise ClaudeActivationError(
            f"Control-plane cache is missing at {resolved_cache}. Install AI Dev for the "
            "claude audience, or clone the coordination repository there, so discovery "
            "has durable state to read."
        )

    rail = resolve_authorized_rail(
        resolved_cache, project=identity.project, ticket=identity.ticket
    )

    return {
        "repository": identity.repository,
        "project": identity.project,
        "ticket": identity.ticket,
        "issueNumber": identity.issue_number,
        "controlPlaneCache": str(resolved_cache),
        "coordinationRepository": coordination_repository,
        "railId": rail.identifier,
        "railStatus": rail.status,
        "railPath": f"{identity.project}/{identity.ticket}/rails/{rail.identifier}/rail.md",
        "handoffPath": f"{identity.project}/{identity.ticket}/rails/{rail.identifier}/handoff.md",
    }


# CLI -------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.claude_activation",
        description="Claude activation pointer installation and control-plane discovery.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser(
        "discover", help="Resolve repository identity, ticket, and the authorized rail."
    )
    discover_parser.add_argument("--repo-root", help="Product repository; defaults to the current one.")
    discover_parser.add_argument("--cache", help="Control-plane cache path override.")
    discover_parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")

    subparsers.add_parser(
        "install-pointer", help="Insert or refresh the managed host-level activation block."
    )

    identity_parser = subparsers.add_parser(
        "identity", help="Resolve repository, project, and ticket identity only."
    )
    identity_parser.add_argument("--repo-root", help="Product repository; defaults to the current one.")

    subparsers.add_parser("cache-path", help="Print the host-level control-plane cache path.")
    subparsers.add_parser(
        "cache-sync", help="Clone or refresh the single host-level control-plane cache."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)

    if arguments.command == "cache-path":
        print(resolve_control_plane_cache())
        return 0

    if arguments.command == "cache-sync":
        cache, outcome = ensure_control_plane_cache()
        print(f"{outcome}: {cache}")
        return 0

    if arguments.command == "install-pointer":
        outcome = sync_claude_activation()
        print(f"{outcome}: {resolve_claude_instruction_path()}")
        return 0

    if arguments.repo_root:
        repo_root = Path(arguments.repo_root).expanduser()
    else:
        try:
            repo_root = resolve_repo_root()
        except RepositoryError as exc:
            raise ClaudeActivationError(str(exc)) from exc

    if arguments.command == "identity":
        identity = resolve_product_identity(repo_root)
        print(f"repository : {identity.repository}")
        print(f"project    : {identity.project}")
        print(f"ticket     : {identity.ticket}")
        print(f"scope      : {identity.project}/{identity.ticket}")
        return 0

    result = discover(
        repo_root,
        cache=Path(arguments.cache).expanduser() if arguments.cache else None,
    )

    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(f"repository : {result['repository']}")
    print(f"scope      : {result['project']}/{result['ticket']}")
    print(f"cache      : {result['controlPlaneCache']}")
    print(f"rail       : {result['railId']} ({result['railStatus']})")
    print(f"rail file  : {result['railPath']}")
    print(f"handoff    : {result['handoffPath']}")
    return 0


def run() -> None:
    try:
        status = main()
    except (ClaudeActivationError, ControlPlaneError) as exc:
        print(f"ai-dev: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    run()
