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
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .control_plane import (
    ControlPlaneError,
    RailState,
    allocate_proceed_number,
    artifact_relative,
    collect_rail_states,
    parse_proceed_sequence,
    proceed_sequence_relative,
    publish as control_plane_publish,
    resolve_coordination_repo,
    resolve_read_source,
)
from .json_files import JsonFileError, load_json_object, write_text_atomic
from .repository import RepositoryError, resolve_repo_root, workflow_state_file_for_repo_root
from .ticket_status import TicketStatusError, render_active_ticket_status
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

    # Atomic replace: a failed write must never partially truncate unrelated
    # user instructions that this module does not own.
    try:
        write_text_atomic(path, composed)
    except (JsonFileError, OSError) as exc:
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


# Coordination git helpers ----------------------------------------------------


def _coordination_git(cache: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cache), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ClaudeActivationError(
            f"git {' '.join(arguments)} failed in {cache}: "
            f"{completed.stdout.strip() or completed.returncode}"
        )
    return completed.stdout.strip()


def read_proceed_receipt(cache: Path, *, project: str, ticket: str) -> int:
    """Current receipt value. A receipt is evidence of publication, never authority."""
    try:
        source = resolve_read_source(resolve_coordination_repo(cache))
        return parse_proceed_sequence(source.read(proceed_sequence_relative(project, ticket)))
    except ControlPlaneError as exc:
        raise ClaudeActivationError(f"Cannot read the proceed receipt: {exc}") from exc


# Status ----------------------------------------------------------------------


def render_status(
    repo_root: Path,
    *,
    home: Path | None = None,
    cache: Path | None = None,
    coordination_repository: str = DEFAULT_COORDINATION_REPOSITORY,
) -> str:
    """Contextual status for the repository the caller is standing in.

    Every fact is delegated to an existing reader. Source health is reported
    rather than guessed, so an unreachable control plane is visible instead of
    silently rendering a partial picture.
    """
    lines: list[str] = []
    identity = resolve_product_identity(repo_root)

    lines.append(f"repository : {identity.repository}")
    lines.append(f"project    : {identity.project}")
    lines.append(f"ticket     : {identity.ticket}")

    try:
        ticket_status = render_active_ticket_status(repo_root)
        for line in ticket_status.splitlines():
            if line.strip():
                lines.append(f"  {line.rstrip()}")
    except (TicketStatusError, OSError) as exc:
        lines.append(f"  ticket status unavailable: {exc}")

    resolved_cache = (
        cache if cache is not None else resolve_control_plane_cache(coordination_repository, home=home)
    )
    lines.append(f"cache      : {resolved_cache}")

    if not resolved_cache.exists():
        lines.append("source     : UNAVAILABLE - control-plane cache is missing")
        lines.append("rail       : unknown until the control plane is reachable")
        return "\n".join(lines)

    try:
        head = _coordination_git(resolved_cache, ["rev-parse", "--short", "HEAD"])
        lines.append(f"source     : cache at {head}")
    except ClaudeActivationError as exc:
        lines.append(f"source     : UNAVAILABLE - {exc}")
        return "\n".join(lines)

    try:
        rail = resolve_authorized_rail(
            resolved_cache, project=identity.project, ticket=identity.ticket
        )
        lines.append(f"rail       : {rail.identifier} ({rail.status})")
    except ClaudeActivationError as exc:
        lines.append(f"rail       : UNAUTHORIZED - {exc}")

    try:
        receipt = read_proceed_receipt(
            resolved_cache, project=identity.project, ticket=identity.ticket
        )
        lines.append(f"receipt    : proceed {receipt} (publication receipt only, not authorization)")
    except ClaudeActivationError as exc:
        lines.append(f"receipt    : unavailable - {exc}")

    return "\n".join(lines)


# Executor handoff publication ------------------------------------------------


def publish_executor_handoff(
    repo_root: Path,
    *,
    content_file: Path,
    rail: str | None = None,
    home: Path | None = None,
    cache: Path | None = None,
    coordination_repository: str = DEFAULT_COORDINATION_REPOSITORY,
) -> dict:
    """Own the whole executor stop-boundary transaction, in order.

    discover -> publish -> push -> verify remote readability -> allocate receipt.

    The receipt is allocated only after the handoff is durably readable from the
    remote, because a receipt is evidence that publication happened. Any failure
    stops with an actionable error; nothing is ever written to a product
    repository as a fallback and no receipt is invented.
    """
    resolved_cache = (
        cache if cache is not None else resolve_control_plane_cache(coordination_repository, home=home)
    )

    # 1. Fresh authorization, never the caller's assumption.
    discovered = discover(
        repo_root, home=home, coordination_repository=coordination_repository, cache=resolved_cache
    )
    authorized_rail = discovered["railId"]
    if rail is not None and rail != authorized_rail:
        raise ClaudeActivationError(
            f"Refusing to publish: {rail} is not the authorized rail. "
            f"{authorized_rail} is currently authorized for "
            f"{discovered['project']}/{discovered['ticket']}."
        )

    if not content_file.is_file():
        raise ClaudeActivationError(f"Handoff content file does not exist: {content_file}")
    content = content_file.read_text(encoding="utf-8")

    project = discovered["project"]
    ticket = discovered["ticket"]
    coordination = resolve_coordination_repo(resolved_cache)

    # 2/3. Publish through the existing ownership rules; publish commits only.
    try:
        target, head = control_plane_publish(
            coordination,
            project=project,
            ticket=ticket,
            artifact="handoff",
            role="executor",
            content=content,
            rail=authorized_rail,
        )
    except ControlPlaneError as exc:
        raise ClaudeActivationError(f"Cannot publish the executor handoff: {exc}") from exc

    relative = artifact_relative(
        project=project, ticket=ticket, artifact="handoff", rail=authorized_rail
    )

    # 4. Durable push. Publication is not complete until the remote has it.
    branch = _coordination_git(coordination, ["rev-parse", "--abbrev-ref", "HEAD"])
    _coordination_git(coordination, ["push", "origin", f"HEAD:{branch}"])

    # 5. Prove the remote actually serves the published content.
    _coordination_git(coordination, ["fetch", "--quiet", "origin"])
    remote_head = _coordination_git(coordination, ["rev-parse", f"origin/{branch}"])
    local_head = _coordination_git(coordination, ["rev-parse", "HEAD"])
    if remote_head != local_head:
        raise ClaudeActivationError(
            f"Refusing to allocate a receipt: {coordination_repository} is at {remote_head} "
            f"but the published commit is {local_head}. The handoff is not durably published."
        )
    verify = subprocess.run(
        ["git", "-C", str(coordination), "cat-file", "-e", f"origin/{branch}:{relative}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise ClaudeActivationError(
            f"Refusing to allocate a receipt: {relative} is not readable from "
            f"origin/{branch} after push."
        )

    # 6. Only now may the shared compare-and-swap allocator run.
    try:
        receipt = allocate_proceed_number(coordination, project=project, ticket=ticket)
    except ControlPlaneError as exc:
        raise ClaudeActivationError(
            f"Handoff published and durable, but the receipt could not be allocated: {exc}"
        ) from exc

    return {
        "railId": authorized_rail,
        "project": project,
        "ticket": ticket,
        "handoffPath": relative,
        "publishedFile": str(target),
        "coordinationHead": head,
        "remoteHead": remote_head,
        "proceed": receipt,
    }


# Review evidence -------------------------------------------------------------


REVIEW_EVIDENCE_RELATIVE = "skills/copilot/auto-review/scripts/review-evidence"


def resolve_posix_shell() -> str:
    """Locate a POSIX shell, preferring the one Git already ships.

    A bare "bash" on Windows can resolve to the WSL launcher in System32, which
    cannot execute a Windows-path script. Git is necessarily present for any AI
    Dev repository, so its own bash is the deterministic choice.
    """
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"

    completed = subprocess.run(
        ["git", "--exec-path"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        candidate = Path(completed.stdout.strip())
        while candidate.parent != candidate:
            bash = candidate / "bin" / "bash.exe"
            if bash.is_file():
                return str(bash)
            candidate = candidate.parent

    discovered = shutil.which("bash")
    if discovered and "System32" not in discovered:
        return discovered

    raise ClaudeActivationError(
        "Cannot locate a POSIX shell for the review-evidence helper. Install Git for "
        "Windows, or run the helper from a shell that provides bash."
    )


def run_review_evidence(repo_root: Path, *, mode: str) -> int:
    """Invoke the canonical review-evidence helper with a real interpreter.

    The shared helper invokes ``python3``. On Windows that name can resolve to a
    store alias stub rather than an interpreter, which the helper then misreports
    as corrupt Flow state. This path supplies the already bootstrap-selected
    interpreter for the duration of the call instead of editing that helper,
    whose defect is owned elsewhere.
    """
    helper = repo_root / REVIEW_EVIDENCE_RELATIVE
    if not helper.is_file():
        raise ClaudeActivationError(f"Review evidence helper not found: {helper}")

    with tempfile.TemporaryDirectory() as shim_dir:
        shim = Path(shim_dir) / "python3"
        shim.write_text(
            "#!/usr/bin/env bash\nexec " + shlex.quote(sys.executable) + ' "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)

        environment = dict(os.environ)
        environment["PATH"] = str(shim.parent) + os.pathsep + environment.get("PATH", "")

        completed = subprocess.run(
            [resolve_posix_shell(), str(helper), "--mode", mode],
            cwd=str(repo_root),
            env=environment,
            check=False,
        )
    return completed.returncode


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

    status_parser = subparsers.add_parser(
        "status", help="Contextual AI Dev status for the current repository."
    )
    status_parser.add_argument("--repo-root", help="Product repository; defaults to the current one.")
    status_parser.add_argument("--cache", help="Control-plane cache path override.")

    publish_parser = subparsers.add_parser(
        "publish", help="Publish, push, verify, and receipt the executor handoff."
    )
    publish_parser.add_argument("--file", required=True, help="Handoff content to publish.")
    publish_parser.add_argument("--rail", help="Expected rail id; must match the authorized rail.")
    publish_parser.add_argument("--repo-root", help="Product repository; defaults to the current one.")
    publish_parser.add_argument("--cache", help="Control-plane cache path override.")

    evidence_parser = subparsers.add_parser(
        "review-evidence", help="Run the canonical review-evidence helper."
    )
    evidence_parser.add_argument(
        "--mode", choices=("checkpoint", "promotion"), default="checkpoint"
    )
    evidence_parser.add_argument("--repo-root", help="Product repository; defaults to the current one.")

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

    if arguments.command == "status":
        print(
            render_status(
                repo_root,
                cache=Path(arguments.cache).expanduser() if arguments.cache else None,
            )
        )
        return 0

    if arguments.command == "review-evidence":
        return run_review_evidence(repo_root, mode=arguments.mode)

    if arguments.command == "publish":
        result = publish_executor_handoff(
            repo_root,
            content_file=Path(arguments.file).expanduser(),
            rail=arguments.rail,
            cache=Path(arguments.cache).expanduser() if arguments.cache else None,
        )
        print(f"published : {result['handoffPath']}")
        print(f"rail      : {result['railId']}")
        print(f"remote    : {result['remoteHead']}")
        print()
        print(f"proceed {result['proceed']}")
        return 0

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
