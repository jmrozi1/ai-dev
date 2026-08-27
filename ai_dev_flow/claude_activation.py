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
    ReadSource,
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

# The one name the activation pointer documents and the installer provides. A
# fresh executor types this; if the two ever disagree the pointer is a lie, so
# both the block and the launchers are rendered from this constant.
AI_DEV_COMMAND_NAME = "ai-dev"

# Carried by every managed launcher. Installation replaces only files bearing
# this marker, so an unrelated command of the same name is never destroyed.
LAUNCHER_OWNERSHIP_MARKER = "AI_DEV_LAUNCHER_V1 (claude audience)"


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
            f"{AI_DEV_COMMAND_NAME} discover",
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


# Command installation --------------------------------------------------------


@dataclass(frozen=True)
class LauncherStatus:
    path: Path
    state: str


def resolve_command_directory(*, home: Path | None = None) -> Path:
    """The user-owned command directory, so installation needs no admin rights."""
    return _resolved_home(home) / ".local" / "bin"


def _launcher_platform(platform: str | None = None) -> str:
    if platform is None:
        return "windows" if os.name == "nt" else "posix"
    if platform not in {"posix", "windows"}:
        raise ClaudeActivationError(
            f"Unsupported launcher platform {platform!r}; expected 'posix' or 'windows'."
        )
    return platform


# Every launcher name this installer has ever owned, so reinstalling can retire
# the ones the current platform stopped using instead of letting them shadow it.
_LAUNCHER_SUFFIXES = ("", ".ps1", ".cmd")


def managed_launcher_names(platform: str) -> tuple[str, ...]:
    """Git Bash resolves the extensionless script through its shebang; PowerShell
    resolves the ``.ps1`` from PATH. Those two files cover both Windows shells."""
    if platform == "windows":
        return (AI_DEV_COMMAND_NAME, f"{AI_DEV_COMMAND_NAME}.ps1")
    return (AI_DEV_COMMAND_NAME,)


def render_posix_launcher(runtime_root: Path) -> str:
    """POSIX/Git Bash launcher delegating to the shared interpreter selector."""
    return "\n".join(
        (
            "#!/usr/bin/env bash",
            f"# {LAUNCHER_OWNERSHIP_MARKER}",
            f"# Managed by `{AI_DEV_COMMAND_NAME} install-command`; regenerate rather than edit.",
            "set -euo pipefail",
            "",
            f"repo_root={shlex.quote(runtime_root.as_posix())}",
            "",
            'source "$repo_root/tools/bootstrap/python_select.sh"',
            f'python_executable="$(ai_dev_select_python "{AI_DEV_COMMAND_NAME}")" || exit 1',
            "",
            "# Absolute entry-point path: sys.path[0] becomes the runtime's own directory,",
            "# never whichever repository the caller happens to be standing in.",
            'exec "$python_executable" "$repo_root/tools/claude/ai-dev-entry.py" "$@"',
            "",
        )
    )


def render_powershell_launcher(runtime_root: Path) -> str:
    """PowerShell launcher using the same interpreter selection as Git Bash."""
    return "\n".join(
        (
            f"# {LAUNCHER_OWNERSHIP_MARKER}",
            f"# Managed by `{AI_DEV_COMMAND_NAME} install-command`; regenerate rather than edit.",
            "$ErrorActionPreference = 'Stop'",
            "$repoRoot = '{}'".format(str(runtime_root).replace("'", "''")),
            ". (Join-Path $repoRoot 'tools\\bootstrap\\PythonSelection.ps1')",
            f"$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName '{AI_DEV_COMMAND_NAME}'",
            "& $pythonExecutable (Join-Path $repoRoot 'tools\\claude\\ai-dev-entry.py') @args",
            "exit $LASTEXITCODE",
            "",
        )
    )


def launcher_is_managed(path: Path) -> bool:
    """Whether this exact file is one AI Dev wrote and may therefore replace."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return LAUNCHER_OWNERSHIP_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _assert_launcher_replaceable(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if launcher_is_managed(path):
        return
    raise ClaudeActivationError(
        f"Refusing to replace {path}: it exists but is not an AI Dev managed launcher. "
        "Move it aside, then install again."
    )


def _retire_obsolete_launchers(
    directory: Path, *, keep: tuple[str, ...]
) -> tuple[LauncherStatus, ...]:
    """Remove launchers this installer owns but no longer uses; touch nothing else."""
    statuses: list[LauncherStatus] = []
    for suffix in _LAUNCHER_SUFFIXES:
        name = f"{AI_DEV_COMMAND_NAME}{suffix}"
        if name in keep:
            continue
        path = directory / name
        if not launcher_is_managed(path):
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise ClaudeActivationError(
                f"Cannot remove obsolete launcher {path}: {exc}"
            ) from exc
        statuses.append(LauncherStatus(path, "removed"))
    return tuple(statuses)


def install_ai_dev_command(
    *,
    home: Path | None = None,
    runtime_root: Path | None = None,
    platform: str | None = None,
) -> tuple[Path, tuple[LauncherStatus, ...]]:
    """Put the documented command where a fresh shell will find it.

    The runtime root is written into the launcher because the launcher lives
    outside the checkout and has nothing else to resolve against. A moved or
    re-cloned runtime is handled by installing again, never by a search path
    that could bind the command to the wrong checkout.
    """
    resolved_platform = _launcher_platform(platform)
    resolved_runtime = (
        resolve_ai_dev_runtime_root()
        if runtime_root is None
        else runtime_root.expanduser().resolve()
    )

    entry_point = resolved_runtime / "tools" / "claude" / "ai-dev-entry.py"
    if not entry_point.is_file():
        raise ClaudeActivationError(
            f"Cannot install the {AI_DEV_COMMAND_NAME} command: no Claude entry point at "
            f"{entry_point}. Install from a complete AI Dev checkout."
        )

    directory = resolve_command_directory(home=home)
    if directory.exists() and not directory.is_dir():
        raise ClaudeActivationError(
            f"Cannot install the {AI_DEV_COMMAND_NAME} command: {directory} exists and is not "
            "a directory. Move it aside, then install again."
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClaudeActivationError(f"Cannot create {directory}: {exc}") from exc

    names = managed_launcher_names(resolved_platform)
    contents = {
        AI_DEV_COMMAND_NAME: render_posix_launcher(resolved_runtime),
        f"{AI_DEV_COMMAND_NAME}.ps1": render_powershell_launcher(resolved_runtime),
    }

    # Validate every destination before writing any of them: a collision on the
    # second file must not leave a half-installed command behind.
    for name in names:
        _assert_launcher_replaceable(directory / name)

    statuses: list[LauncherStatus] = []
    for name in names:
        path = directory / name
        desired = contents[name]
        existed = path.is_file()
        try:
            current = path.read_text(encoding="utf-8") if existed else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current == desired:
            statuses.append(LauncherStatus(path, "unchanged"))
            continue
        try:
            write_text_atomic(path, desired)
        except (JsonFileError, OSError) as exc:
            raise ClaudeActivationError(f"Cannot write {path}: {exc}") from exc
        if not name.endswith(".ps1"):
            try:
                path.chmod(0o755)
            except OSError as exc:
                raise ClaudeActivationError(f"Cannot make {path} executable: {exc}") from exc
        statuses.append(LauncherStatus(path, "updated" if existed else "installed"))

    statuses.extend(_retire_obsolete_launchers(directory, keep=names))
    return directory, tuple(statuses)


def command_directory_is_on_path(directory: Path, *, path_value: str | None = None) -> bool:
    """Whether a fresh shell would resolve the command. Never mutates PATH."""
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    target = os.path.normcase(os.path.normpath(str(directory)))
    for entry in raw.split(os.pathsep):
        candidate = entry.strip()
        if not candidate:
            continue
        if os.path.normcase(os.path.normpath(os.path.expanduser(candidate))) == target:
            return True
    return False


def install_claude_host_activation(
    *, home: Path | None = None, runtime_root: Path | None = None
) -> dict:
    """The two halves of activation: the command, and the pointer that documents it."""
    pointer_outcome = sync_claude_activation(home=home)
    directory, launchers = install_ai_dev_command(home=home, runtime_root=runtime_root)
    return {
        "pointer": pointer_outcome,
        "pointerPath": resolve_claude_instruction_path(home=home),
        "commandDirectory": directory,
        "launchers": launchers,
    }


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


def resolve_control_plane_source(cache: Path) -> ReadSource:
    """The one deterministic read source every Claude-side read is served from.

    Freshness is the shared reader's model, not a second one invented here: it
    fetches the tracked upstream and serves the fetched revision when the cache
    checkout is behind, without moving anything local.
    """
    try:
        coordination = resolve_coordination_repo(cache)
    except ControlPlaneError as exc:
        raise ClaudeActivationError(
            f"Control-plane cache is not usable at {cache}. {exc}"
        ) from exc

    try:
        return resolve_read_source(coordination)
    except ControlPlaneError as exc:
        raise ClaudeActivationError(
            f"Cannot read control-plane state from {cache}. {exc}"
        ) from exc


def resolve_authorized_rail(
    cache: Path,
    *,
    project: str,
    ticket: str,
    source: ReadSource | None = None,
) -> RailState:
    """Exactly one ready rail, using the existing deterministic rail reader."""
    resolved_source = resolve_control_plane_source(cache) if source is None else source

    try:
        states = collect_rail_states(resolved_source, project=project, ticket=ticket)
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


def read_proceed_receipt(
    cache: Path,
    *,
    project: str,
    ticket: str,
    source: ReadSource | None = None,
) -> int:
    """Current receipt value. A receipt is evidence of publication, never authority."""
    resolved_source = resolve_control_plane_source(cache) if source is None else source
    try:
        return parse_proceed_sequence(
            resolved_source.read(proceed_sequence_relative(project, ticket))
        )
    except ControlPlaneError as exc:
        raise ClaudeActivationError(f"Cannot read the proceed receipt: {exc}") from exc


# Status ----------------------------------------------------------------------


def _short_revision(cache: Path, revision: str) -> str:
    if not revision:
        return "unknown"
    try:
        return _coordination_git(cache, ["rev-parse", "--short", revision])
    except ClaudeActivationError:
        return revision[:7]


def render_source_health(cache: Path, source: ReadSource) -> str:
    """Name the revision the read was actually served from, not the local checkout.

    The cache checkout deliberately stays put while reads follow the fetched
    upstream. Reporting local HEAD as the source would name a revision the
    executor never acted on, which is the one thing this line exists to prove.

    ``ReadSource.head`` is the head of the state being served, so the checkout's
    own position has to be read from Git to say honestly how far behind it is.
    """
    try:
        checkout_head = _coordination_git(cache, ["rev-parse", "HEAD"])
    except ClaudeActivationError:
        checkout_head = ""
    local = _short_revision(cache, checkout_head)

    if source.revision is None:
        return f"local cache at {local} (no coordination remote)"

    served = _short_revision(cache, source.revision)
    if source.revision == checkout_head:
        return f"fetched upstream at {served} (cache checkout in sync)"
    # resolve_read_source refuses ahead/diverged checkouts, so behind is the
    # only way the served revision and the checkout can differ here.
    return f"fetched upstream at {served} (cache checkout behind at {local})"


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
        source = resolve_control_plane_source(resolved_cache)
    except ClaudeActivationError as exc:
        lines.append(f"source     : UNAVAILABLE - {exc}")
        lines.append("rail       : unknown until the control plane is reachable")
        return "\n".join(lines)

    lines.append(f"source     : {render_source_health(resolved_cache, source)}")

    try:
        rail = resolve_authorized_rail(
            resolved_cache,
            project=identity.project,
            ticket=identity.ticket,
            source=source,
        )
        lines.append(f"rail       : {rail.identifier} ({rail.status})")
    except ClaudeActivationError as exc:
        lines.append(f"rail       : UNAUTHORIZED - {exc}")

    try:
        receipt = read_proceed_receipt(
            resolved_cache,
            project=identity.project,
            ticket=identity.ticket,
            source=source,
        )
        lines.append(f"receipt    : proceed {receipt} (publication receipt only, not authorization)")
    except ClaudeActivationError as exc:
        lines.append(f"receipt    : unavailable - {exc}")

    return "\n".join(lines)


# Executor handoff publication ------------------------------------------------


def _commit_is_on_remote(coordination: Path, *, branch: str, commit: str) -> bool:
    """Whether the remote already contains a commit, after refreshing remote state."""
    try:
        _coordination_git(coordination, ["fetch", "--quiet", "origin"])
        merge_base = subprocess.run(
            ["git", "-C", str(coordination), "merge-base", "--is-ancestor", commit,
             f"origin/{branch}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return merge_base.returncode == 0
    except ClaudeActivationError:
        return False


def _recover_after_failed_push(
    coordination: Path,
    *,
    branch: str,
    base_head: str,
    push_error: Exception,
) -> None:
    """Leave the managed cache retryable after a failed push, or prove it published.

    A push that reports failure may still have been accepted, so remote state is
    read before anything local is undone. If the commit is genuinely absent from
    the remote, only the commit this invocation created on top of ``base_head`` is
    rolled back -- never unrelated local coordination work -- so an ordinary
    ``ai-dev publish`` retry can re-discover authorization and succeed with no
    manual Git repair.
    """
    current_head = _coordination_git(coordination, ["rev-parse", "HEAD"])

    if _commit_is_on_remote(coordination, branch=branch, commit=current_head):
        # The push raced: the remote has it. Durable publication must not be
        # undone just because the client reported an error.
        return

    created_exactly_one = False
    if current_head != base_head:
        parents = _coordination_git(coordination, ["rev-list", "--parents", "-n", "1", current_head])
        parts = parents.split()
        created_exactly_one = len(parts) == 2 and parts[1] == base_head

    if not created_exactly_one:
        raise ClaudeActivationError(
            f"Cannot publish the executor handoff: {push_error}. The coordination cache at "
            f"{coordination} holds local commits this operation did not create, so it was "
            "left untouched. Reconcile it before retrying."
        )

    try:
        _coordination_git(coordination, ["reset", "--hard", base_head])
    except ClaudeActivationError as reset_error:
        raise ClaudeActivationError(
            f"Cannot publish the executor handoff: {push_error}. Rolling the coordination "
            f"cache back to {base_head} also failed ({reset_error}); repair {coordination} "
            "before retrying."
        ) from push_error

    raise ClaudeActivationError(
        f"Cannot publish the executor handoff: {push_error}. No receipt was consumed and "
        "the coordination cache was rolled back, so rerunning the same ai-dev publish "
        "command will retry cleanly."
    ) from push_error


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

    branch = _coordination_git(coordination, ["rev-parse", "--abbrev-ref", "HEAD"])
    # Remembered so a failed push can undo exactly the commit this invocation
    # created, and nothing else that happens to be in the cache.
    base_head = _coordination_git(coordination, ["rev-parse", "HEAD"])

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
    try:
        _coordination_git(coordination, ["push", "origin", f"HEAD:{branch}"])
    except ClaudeActivationError as push_error:
        _recover_after_failed_push(
            coordination,
            branch=branch,
            base_head=base_head,
            push_error=push_error,
        )

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


def resolve_ai_dev_runtime_root() -> Path:
    """The AI Dev checkout that owns this runtime, whatever repository is being reviewed."""
    return Path(__file__).resolve().parents[1]


def resolve_review_evidence_helper() -> Path:
    """Locate the canonical helper inside AI Dev, never inside the product repo.

    A supported product repository deliberately has no AI Dev helper tree. The
    helper already scopes its evidence to the current working directory, so it
    only needs to be *launched* from AI Dev code and *run* in the product
    repository.
    """
    helper = resolve_ai_dev_runtime_root() / REVIEW_EVIDENCE_RELATIVE
    if not helper.is_file():
        raise ClaudeActivationError(
            f"Review evidence helper not found in the installed AI Dev runtime: {helper}. "
            "Reinstall the AI Dev claude audience so the helper is available."
        )
    return helper


def run_review_evidence(repo_root: Path, *, mode: str) -> int:
    """Invoke the canonical review-evidence helper with a real interpreter.

    The helper comes from the AI Dev runtime that owns this module; the working
    directory stays the product repository under review, which is what the helper
    scopes its evidence to.

    The shared helper invokes ``python3``. On Windows that name can resolve to a
    store alias stub rather than an interpreter, which the helper then misreports
    as corrupt Flow state. This path supplies the already bootstrap-selected
    interpreter for the duration of the call instead of editing that helper,
    whose defect is owned elsewhere.
    """
    helper = resolve_review_evidence_helper()

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

    subparsers.add_parser(
        "install-command",
        help=f"Install or refresh the user-owned {AI_DEV_COMMAND_NAME} command on PATH.",
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

    if arguments.command == "install-command":
        directory, launchers = install_ai_dev_command()
        for launcher in launchers:
            print(f"{launcher.state}: {launcher.path}")
        if not command_directory_is_on_path(directory):
            print(
                f"warning: {directory} is not on PATH, so `{AI_DEV_COMMAND_NAME}` will not "
                "resolve in a fresh shell. Add it to PATH in your shell profile.",
                file=sys.stderr,
            )
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
