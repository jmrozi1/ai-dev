"""Concurrent Flow workspaces: shared claim registry and worktree identity."""

from __future__ import annotations

# One repository may host several independent Flow workspaces, each a linked Git
# worktree with its own `.ai-dev` state. Workspace *identity* is the canonical
# ticket reference; the filesystem path is only where that workspace currently
# lives. The registry here is an ownership index and nothing else: it never
# stores checkpoint, blocked, review, or synchronization state, all of which
# stay in each workspace's own `.ai-dev` directory.

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
from typing import Any, Optional

from .json_files import JsonFileError, write_json_object_atomic
from .tickets import TicketReference


REGISTRY_DIRECTORY_NAME = "ai-dev"
CLAIMS_DIRECTORY_NAME = "claims"

CLAIM_STATUS_CREATING = "creating"
CLAIM_STATUS_ACTIVE = "active"
CLAIM_STATUSES = frozenset({CLAIM_STATUS_CREATING, CLAIM_STATUS_ACTIVE})

_CLAIM_REQUIRED_KEYS = frozenset(
    {"version", "status", "token", "key", "provider", "ticketId", "createdAt"}
)
_CLAIM_ALLOWED_KEYS = _CLAIM_REQUIRED_KEYS | frozenset(
    {
        "repository",
        "intendedPath",
        "intendedBranch",
        "worktreeId",
        "hostname",
        "pid",
    }
)


class WorkspaceError(Exception):
    """Raised for user-facing workspace registry and worktree failures."""


class ClaimOccupiedError(WorkspaceError):
    """Raised when a ticket is already claimed by some other workspace."""


def _run_git(repo_root: Path, arguments: list, *, check: bool):
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise WorkspaceError(stderr or f"git {' '.join(arguments)} failed.")
    return completed


def _now_utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Git common directory and worktree identity
# ---------------------------------------------------------------------------


def git_common_dir(repo_root: Path) -> Path:
    """Resolve the directory shared by every worktree of this repository."""
    completed = _run_git(repo_root, ["rev-parse", "--git-common-dir"], check=True)
    resolved = completed.stdout.strip()
    if not resolved:
        raise WorkspaceError("Cannot resolve the shared Git directory for this repository.")

    candidate = Path(resolved)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return Path(os.path.abspath(str(candidate)))


def git_dir_for_repo_root(repo_root: Path) -> Path:
    completed = _run_git(repo_root, ["rev-parse", "--git-dir"], check=True)
    resolved = completed.stdout.strip()
    if not resolved:
        raise WorkspaceError("Cannot resolve the Git directory for this repository.")
    candidate = Path(resolved)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return Path(os.path.abspath(str(candidate)))


def worktree_id_for_repo_root(repo_root: Path) -> Optional[str]:
    """Git's own identifier for a linked worktree, or None for the primary one.

    Git derives this from the worktree directory basename and keeps it stable
    across `git worktree move`, which is why it -- not the path -- authorizes
    ownership.
    """
    git_dir = git_dir_for_repo_root(repo_root)
    common_dir = git_common_dir(repo_root)
    if Path(os.path.normpath(str(git_dir))) == Path(os.path.normpath(str(common_dir))):
        return None

    parent = git_dir.parent
    if parent.name != "worktrees":
        return None
    return git_dir.name


@dataclass(frozen=True)
class WorktreeEntry:
    worktree_id: Optional[str]
    path: Path
    branch: Optional[str]
    prunable: bool
    is_primary: bool


def _registered_worktree_paths(common_dir: Path) -> dict:
    """Map worktree id -> recorded path, read from Git's own registry."""
    registry = common_dir / "worktrees"
    mapping = {}
    if not registry.is_dir():
        return mapping

    for entry in sorted(registry.iterdir()):
        gitdir_file = entry / "gitdir"
        if not gitdir_file.is_file():
            continue
        try:
            recorded = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not recorded:
            continue
        recorded_path = Path(recorded)
        if recorded_path.name == ".git":
            recorded_path = recorded_path.parent
        mapping[entry.name] = Path(os.path.abspath(str(recorded_path)))
    return mapping


def list_worktrees(repo_root: Path) -> list:
    """Live worktrees, with ids resolved through Git's registry.

    `git worktree list --porcelain` reports paths but not ids, so ids come from
    `<common>/worktrees/<id>/gitdir`, which Git rewrites on `git worktree move`.
    """
    common_dir = git_common_dir(repo_root)
    id_by_path = {}
    for worktree_id, recorded_path in _registered_worktree_paths(common_dir).items():
        id_by_path[str(recorded_path)] = worktree_id

    completed = _run_git(repo_root, ["worktree", "list", "--porcelain"], check=True)

    entries = []
    current: dict = {}

    def flush() -> None:
        if not current.get("path"):
            return
        path = Path(os.path.abspath(str(current["path"])))
        entries.append(
            WorktreeEntry(
                worktree_id=id_by_path.get(str(path)),
                path=path,
                branch=current.get("branch"),
                prunable=bool(current.get("prunable")),
                is_primary=len(entries) == 0,
            )
        )
        current.clear()

    for line in completed.stdout.splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith("worktree "):
            flush()
            current["path"] = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            reference = line[len("branch ") :].strip()
            prefix = "refs/heads/"
            if reference.startswith(prefix):
                reference = reference[len(prefix) :]
            current["branch"] = reference
        elif line.startswith("prunable"):
            current["prunable"] = True
    flush()

    return entries


def resolve_worktree_entry(repo_root: Path, worktree_id: str) -> Optional[WorktreeEntry]:
    for entry in list_worktrees(repo_root):
        if entry.worktree_id == worktree_id:
            return entry
    return None


def worktree_containing_path(repo_root: Path, path: Path) -> Optional[WorktreeEntry]:
    """The worktree whose root contains `path`, deepest root wins."""
    target = Path(os.path.abspath(str(path)))
    best = None
    best_depth = -1
    for entry in list_worktrees(repo_root):
        if _path_is_within(target, entry.path):
            depth = len(entry.path.parts)
            if depth > best_depth:
                best = entry
                best_depth = depth
    return best


def _path_is_within(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate_text = os.path.normcase(os.path.abspath(str(candidate)))
        ancestor_text = os.path.normcase(os.path.abspath(str(ancestor)))
    except (OSError, ValueError):
        return False
    if candidate_text == ancestor_text:
        return True
    return candidate_text.startswith(ancestor_text.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# Canonical identity and claim filenames
# ---------------------------------------------------------------------------


def canonical_ticket_key(reference: TicketReference) -> str:
    if reference.provider == "github":
        return "github:{0}#{1}".format(reference.repository, reference.ticket_id)
    return "{0}:{1}".format(reference.provider, reference.ticket_id)


def claim_filename(key: str) -> str:
    """Complete SHA-256 digest of the canonical key. No cosmetic prefix."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "{0}.json".format(digest)


def registry_directory(repo_root: Path) -> Path:
    return git_common_dir(repo_root) / REGISTRY_DIRECTORY_NAME


def claims_directory(repo_root: Path) -> Path:
    return registry_directory(repo_root) / CLAIMS_DIRECTORY_NAME


def claim_path(repo_root: Path, key: str) -> Path:
    return claims_directory(repo_root) / claim_filename(key)


# ---------------------------------------------------------------------------
# Claim records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimRecord:
    status: str
    token: str
    key: str
    provider: str
    ticket_id: str
    created_at: str
    repository: Optional[str] = None
    intended_path: Optional[str] = None
    intended_branch: Optional[str] = None
    worktree_id: Optional[str] = None
    hostname: Optional[str] = None
    pid: Optional[int] = None

    def to_dict(self) -> dict:
        payload = {
            "version": 1,
            "status": self.status,
            "token": self.token,
            "key": self.key,
            "provider": self.provider,
            "ticketId": self.ticket_id,
            "createdAt": self.created_at,
        }
        for name, value in (
            ("repository", self.repository),
            ("intendedPath", self.intended_path),
            ("intendedBranch", self.intended_branch),
            ("worktreeId", self.worktree_id),
            ("hostname", self.hostname),
            ("pid", self.pid),
        ):
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class MalformedClaim:
    """An unreadable claim. Treated as occupied, never as stale."""

    path: Path
    detail: str


def _generate_token() -> str:
    return os.urandom(16).hex()


def _current_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _record_from_payload(payload: Any, path: Path):
    if not isinstance(payload, dict):
        return MalformedClaim(path=path, detail="claim is not a JSON object")

    unknown = sorted(set(payload) - _CLAIM_ALLOWED_KEYS)
    if unknown:
        return MalformedClaim(path=path, detail="unknown key(s): " + ", ".join(unknown))

    missing = sorted(_CLAIM_REQUIRED_KEYS - set(payload))
    if missing:
        return MalformedClaim(path=path, detail="missing field(s): " + ", ".join(missing))

    if payload.get("version") != 1:
        return MalformedClaim(path=path, detail="version must be 1")

    status = payload.get("status")
    if status not in CLAIM_STATUSES:
        return MalformedClaim(path=path, detail="status must be creating or active")

    for name in ("token", "key", "provider", "ticketId", "createdAt"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            return MalformedClaim(path=path, detail="{0} must be a non-empty string".format(name))

    pid = payload.get("pid")
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        return MalformedClaim(path=path, detail="pid must be a positive integer")

    worktree_id = payload.get("worktreeId")
    if status == CLAIM_STATUS_ACTIVE and (not isinstance(worktree_id, str) or not worktree_id.strip()):
        return MalformedClaim(path=path, detail="active claim requires worktreeId")

    return ClaimRecord(
        status=status,
        token=payload["token"].strip(),
        key=payload["key"].strip(),
        provider=payload["provider"].strip(),
        ticket_id=payload["ticketId"].strip(),
        created_at=payload["createdAt"].strip(),
        repository=payload.get("repository"),
        intended_path=payload.get("intendedPath"),
        intended_branch=payload.get("intendedBranch"),
        worktree_id=worktree_id,
        hostname=payload.get("hostname"),
        pid=pid,
    )


def read_claim(repo_root: Path, key: str):
    """Return a ClaimRecord, a MalformedClaim, or None when unclaimed."""
    path = claim_path(repo_root, key)
    return read_claim_file(path)


def read_claim_file(path: Path):
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return MalformedClaim(path=path, detail="cannot read claim: {0}".format(exc))
    if not text.strip():
        return MalformedClaim(path=path, detail="claim file is empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return MalformedClaim(path=path, detail="invalid JSON: {0}".format(exc.msg))
    return _record_from_payload(payload, path)


def _write_claim_exclusive(path: Path, record: ClaimRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise ClaimOccupiedError(
                "Ticket {0} is already claimed at {1}.".format(record.key, path)
            ) from exc
        raise WorkspaceError("Cannot create claim {0}: {1}".format(path, exc)) from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise WorkspaceError("Cannot write claim {0}: {1}".format(path, exc)) from exc


def reserve_claim(
    repo_root: Path,
    *,
    reference: TicketReference,
    intended_path: Path,
    intended_branch: str,
) -> ClaimRecord:
    """Phase A: reserve the ticket before any branch or worktree exists."""
    key = canonical_ticket_key(reference)
    record = ClaimRecord(
        status=CLAIM_STATUS_CREATING,
        token=_generate_token(),
        key=key,
        provider=reference.provider,
        ticket_id=reference.ticket_id,
        created_at=_now_utc_iso(),
        repository=reference.repository,
        intended_path=str(Path(os.path.abspath(str(intended_path)))),
        intended_branch=intended_branch,
        hostname=_current_hostname(),
        pid=os.getpid(),
    )
    _write_claim_exclusive(claim_path(repo_root, key), record)
    return record


def create_active_claim(
    repo_root: Path,
    *,
    reference: TicketReference,
    worktree_id: Optional[str],
    workspace_path: Path,
    branch: str,
) -> ClaimRecord:
    """Create an already-active claim. Used by adopt and by flow-start."""
    key = canonical_ticket_key(reference)
    record = ClaimRecord(
        status=CLAIM_STATUS_ACTIVE,
        token=_generate_token(),
        key=key,
        provider=reference.provider,
        ticket_id=reference.ticket_id,
        created_at=_now_utc_iso(),
        repository=reference.repository,
        intended_path=str(Path(os.path.abspath(str(workspace_path)))),
        intended_branch=branch,
        worktree_id=worktree_id if worktree_id else _PRIMARY_WORKTREE_ID,
        hostname=_current_hostname(),
        pid=os.getpid(),
    )
    _write_claim_exclusive(claim_path(repo_root, key), record)
    return record


# The primary worktree has no Git-assigned id; this stands in for it so an
# active claim always carries an authorization anchor.
_PRIMARY_WORKTREE_ID = ":primary:"

PRIMARY_WORKTREE_ID = _PRIMARY_WORKTREE_ID


def effective_worktree_id(repo_root: Path) -> str:
    resolved = worktree_id_for_repo_root(repo_root)
    return resolved if resolved else _PRIMARY_WORKTREE_ID


def claim_is_live(repo_root: Path, record: ClaimRecord, path: Path) -> bool:
    """A live claim is healthy and still owned; only stale claims may be pruned.

    A workflow blocked with flow-block keeps its claim on purpose: the ticket
    stays owned by that workspace so no other workspace can start it. Such a
    claim is live, and recovery is workspace-local -- resume it and end it
    through the ordinary lifecycle -- never a registry-level deletion.
    """
    return evaluate_claim(repo_root, record, path).state != "stale"


def acquire_active_claim(
    repo_root: Path,
    *,
    reference: TicketReference,
    worktree_id: Optional[str],
    workspace_path: Path,
    branch: str,
) -> ClaimRecord:
    """Take an active claim, reusing this workspace's own existing claim.

    Re-entrancy matters after a partial failure: if a command acquired a claim
    and then failed before persisting workflow state, retrying it from the same
    workspace must succeed instead of colliding with its own record.
    """
    key = canonical_ticket_key(reference)
    owner = worktree_id if worktree_id else _PRIMARY_WORKTREE_ID
    existing = read_claim_file(claim_path(repo_root, key))

    if isinstance(existing, MalformedClaim):
        raise WorkspaceError(
            "ticket claim at {0} is unreadable ({1}); it is treated as occupied "
            "until explicit recovery".format(existing.path, existing.detail)
        )

    if existing is not None:
        if existing.worktree_id == owner:
            return existing
        raise ClaimOccupiedError(
            "ticket is already claimed by workspace {0}.".format(
                existing.intended_path or existing.worktree_id or "unknown"
            )
        )

    return create_active_claim(
        repo_root,
        reference=reference,
        worktree_id=worktree_id,
        workspace_path=workspace_path,
        branch=branch,
    )


def promote_claim(
    repo_root: Path,
    *,
    record: ClaimRecord,
    worktree_id: str,
    workspace_path: Path,
) -> ClaimRecord:
    """Phase C: atomically turn a reservation into an active claim."""
    path = claim_path(repo_root, record.key)
    current = read_claim_file(path)
    if current is None:
        raise WorkspaceError(
            "Cannot promote claim {0}: the reservation is gone.".format(path)
        )
    if isinstance(current, MalformedClaim):
        raise WorkspaceError(
            "Cannot promote claim {0}: {1}".format(path, current.detail)
        )
    if current.token != record.token:
        raise WorkspaceError(
            "Cannot promote claim {0}: it is owned by another reservation.".format(path)
        )

    promoted = replace(
        current,
        status=CLAIM_STATUS_ACTIVE,
        worktree_id=worktree_id,
        intended_path=str(Path(os.path.abspath(str(workspace_path)))),
    )
    try:
        write_json_object_atomic(path, promoted.to_dict())
    except JsonFileError as exc:
        raise WorkspaceError("Cannot promote claim {0}: {1}".format(path, exc)) from exc
    return promoted


def release_claim(
    repo_root: Path,
    *,
    key: str,
    token: str,
    worktree_id: Optional[str] = None,
) -> bool:
    """Delete a claim owned by this workspace.

    Authorization is the worktree id: only the workspace an active claim names
    may release it, so reading another workspace's record -- token included --
    never confers the right to free its ticket. The token is a narrower guard:
    it proves the caller is acting on the same record generation it read, so a
    claim replaced in between is never deleted by a stale in-memory copy. A
    reservation has no worktree id yet, so there the token is the only proof
    available and is therefore sufficient.
    """
    path = claim_path(repo_root, key)
    current = read_claim_file(path)
    if current is None:
        return False
    if isinstance(current, MalformedClaim):
        raise WorkspaceError(
            "Refusing to release claim {0}: {1}. Use prune --claim to recover it.".format(
                path, current.detail
            )
        )
    if current.worktree_id is not None:
        if worktree_id is None or current.worktree_id != worktree_id:
            raise WorkspaceError(
                "Refusing to release claim {0}: it is owned by workspace {1}, not {2}.".format(
                    path, current.worktree_id, worktree_id or "an unidentified workspace"
                )
            )
    if current.token != token:
        raise WorkspaceError(
            "Refusing to release claim {0}: the record was replaced since it was read.".format(
                path
            )
        )
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkspaceError("Cannot release claim {0}: {1}".format(path, exc)) from exc
    return True


def force_remove_claim(path: Path) -> None:
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceError("Cannot remove claim {0}: {1}".format(path, exc)) from exc


def list_claim_files(repo_root: Path) -> list:
    directory = claims_directory(repo_root)
    if not directory.is_dir():
        return []
    return sorted(item for item in directory.iterdir() if item.is_file())


# ---------------------------------------------------------------------------
# Liveness and staleness
# ---------------------------------------------------------------------------


def process_is_absent(pid: Optional[int], hostname: Optional[str]):
    """True when the owner is provably gone, False when alive, None when unknown.

    Deliberately portable: same-host PID absence is the only proof accepted. PID
    reuse is not detectable without process start times, which this version does
    not collect, so a reused PID reads as alive and merely over-blocks.
    """
    if pid is None:
        return None
    if not hostname or hostname != _current_hostname():
        return None
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return None
    return False


@dataclass(frozen=True)
class ClaimStatus:
    record: ClaimRecord
    path: Path
    state: str
    live_path: Optional[Path]
    detail: str = ""


def evaluate_claim(repo_root: Path, record: ClaimRecord, path: Path) -> ClaimStatus:
    """Classify a claim as active, creating, or stale.

    A recorded path that no longer exists never makes a claim stale; the live
    path is re-resolved from Git's worktree registry instead.
    """
    if record.status == CLAIM_STATUS_ACTIVE:
        if record.worktree_id == _PRIMARY_WORKTREE_ID:
            entries = list_worktrees(repo_root)
            primary = entries[0] if entries else None
            return ClaimStatus(
                record=record,
                path=path,
                state=CLAIM_STATUS_ACTIVE,
                live_path=primary.path if primary is not None else None,
            )

        entry = resolve_worktree_entry(repo_root, record.worktree_id or "")
        if entry is None:
            return ClaimStatus(
                record=record,
                path=path,
                state="stale",
                live_path=None,
                detail="worktree {0} is no longer registered".format(record.worktree_id),
            )
        if entry.prunable:
            return ClaimStatus(
                record=record,
                path=path,
                state="stale",
                live_path=entry.path,
                detail="worktree {0} is prunable".format(record.worktree_id),
            )
        return ClaimStatus(
            record=record,
            path=path,
            state=CLAIM_STATUS_ACTIVE,
            live_path=entry.path,
        )

    absent = process_is_absent(record.pid, record.hostname)
    if absent is True:
        return ClaimStatus(
            record=record,
            path=path,
            state="stale",
            live_path=None,
            detail="reserving process {0} is gone".format(record.pid),
        )
    if absent is None:
        return ClaimStatus(
            record=record,
            path=path,
            state=CLAIM_STATUS_CREATING,
            live_path=None,
            detail="owner liveness could not be established",
        )
    return ClaimStatus(
        record=record,
        path=path,
        state=CLAIM_STATUS_CREATING,
        live_path=None,
        detail="reserving process {0} is running".format(record.pid),
    )


def describe_occupancy(
    repo_root: Path,
    key: str,
    *,
    requesting_worktree_id: Optional[str] = None,
) -> Optional[str]:
    """Human-readable reason a ticket cannot be claimed, or None when free.

    A workspace is never occupied by its own claim, so retrying a command that
    failed after acquisition is idempotent rather than permanently blocked.
    """
    path = claim_path(repo_root, key)
    current = read_claim_file(path)
    if current is None:
        return None
    if (
        not isinstance(current, MalformedClaim)
        and requesting_worktree_id is not None
        and current.worktree_id == requesting_worktree_id
    ):
        return None
    if isinstance(current, MalformedClaim):
        return (
            "ticket claim at {0} is unreadable ({1}); it is treated as occupied "
            "until explicit recovery".format(path, current.detail)
        )
    status = evaluate_claim(repo_root, current, path)
    if status.state == "stale":
        return None
    location = status.live_path or current.intended_path or "an unknown path"
    if status.state == CLAIM_STATUS_CREATING:
        return "ticket is being reserved for {0} ({1})".format(location, status.detail)
    return "ticket is already active in workspace {0} on branch {1}".format(
        location, current.intended_branch or "an unknown branch"
    )


# ---------------------------------------------------------------------------
# Workspace-aware configuration seeding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigRelocation:
    config: dict
    relocated: bool
    detail: str


def relocate_config_for_workspace(
    config: dict,
    *,
    source_root: Path,
    target_root: Path,
) -> ConfigRelocation:
    """Move repository-local output into the new workspace; leave external paths.

    Only `out` is workspace-relative today. A value inside the source workspace
    is rewritten to the equivalent path inside the target so two workspaces
    never share one output artifact; anything outside is deliberate and kept.
    """
    updated = dict(config)
    out_value = config.get("out")
    if not isinstance(out_value, str) or not out_value.strip():
        return ConfigRelocation(config=updated, relocated=False, detail="no out path configured")

    out_path = Path(os.path.abspath(os.path.expanduser(out_value.strip())))
    if not _path_is_within(out_path, source_root):
        return ConfigRelocation(
            config=updated,
            relocated=False,
            detail="out is outside the source workspace and was preserved: {0}".format(out_value),
        )

    relative = os.path.relpath(str(out_path), str(Path(os.path.abspath(str(source_root)))))
    relocated_path = Path(os.path.abspath(os.path.join(str(target_root), relative)))
    updated["out"] = str(relocated_path)
    return ConfigRelocation(
        config=updated,
        relocated=True,
        detail="out relocated to {0}".format(relocated_path),
    )


@dataclass(frozen=True)
class ForeignOutFinding:
    out_path: Path
    owning_worktree: WorktreeEntry


def find_foreign_out_path(
    repo_root: Path,
    config: dict,
    *,
    workspace_root: Path,
):
    """Detect an `out` artifact that lives inside a different worktree.

    Sharing one output file between two live workspaces silently interleaves
    their results, so adoption reports it instead of accepting it.
    """
    out_value = config.get("out")
    if not isinstance(out_value, str) or not out_value.strip():
        return None

    out_path = Path(os.path.abspath(os.path.expanduser(out_value.strip())))
    if _path_is_within(out_path, workspace_root):
        return None

    owner = worktree_containing_path(repo_root, out_path)
    if owner is None:
        return None
    if _path_is_within(Path(os.path.abspath(str(workspace_root))), owner.path) and _path_is_within(
        owner.path, Path(os.path.abspath(str(workspace_root)))
    ):
        return None
    return ForeignOutFinding(out_path=out_path, owning_worktree=owner)


# ---------------------------------------------------------------------------
# Deterministic naming
# ---------------------------------------------------------------------------


_BRANCH_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _sanitize_branch_segment(value: str) -> str:
    lowered = value.strip().lower()
    converted = "".join(character if character in _BRANCH_SAFE else "-" for character in lowered)
    while "--" in converted:
        converted = converted.replace("--", "-")
    return converted.strip("-")


def workspace_branch_name(reference: TicketReference) -> str:
    """Identity -> branch name. A pure function, validated, never mangled."""
    if reference.provider == "github":
        owner, _, repo = (reference.repository or "").partition("/")
        candidate = "flow/github/{0}/{1}/{2}".format(owner, repo, reference.ticket_id)
    else:
        digest = hashlib.sha256(canonical_ticket_key(reference).encode("utf-8")).hexdigest()[:8]
        sanitized = _sanitize_branch_segment(reference.ticket_id)
        candidate = "flow/{0}/{1}-{2}".format(
            reference.provider, sanitized if sanitized else "ticket", digest
        )
    return candidate


def validate_branch_name(repo_root: Path, branch_name: str) -> None:
    completed = _run_git(repo_root, ["check-ref-format", "--branch", branch_name], check=False)
    if completed.returncode != 0:
        raise WorkspaceError(
            "Refusing derived branch name {0!r}: Git rejects it as a branch name.".format(
                branch_name
            )
        )


def default_workspace_path(repo_root: Path, reference: TicketReference) -> Path:
    """Sibling of the primary worktree. Never nested inside the repository."""
    entries = list_worktrees(repo_root)
    primary = entries[0].path if entries else Path(os.path.abspath(str(repo_root)))
    suffix = _sanitize_branch_segment(reference.ticket_id) or "ticket"
    return primary.parent / "{0}-issue-{1}".format(primary.name, suffix)
