"""Controller-owned session bindings: one managed session, one exact assignment."""

from __future__ import annotations

# A binding is the durable proof that a specific provider session belongs to a
# specific project, ticket, Flow workspace, rail, role, and rail iteration. It is
# written *before* the session exists -- the controller preassigns the session id
# -- so no managed session is ever observed without a binding to explain it.
#
# Identity is proved, never inferred. Workspace ownership comes from the shared
# claim registry through `workspaces.verify_workspace_ticket_identity`, and the
# recorded path is only where that workspace currently lives. Transcript content,
# terminal position, selected tab, window focus, and conversation are not evidence
# here and are deliberately absent from the record.
#
# The store is controller-local state. It is not product state and it is not
# control-plane state, so it never participates in the publication, ownership, or
# freshness rules that belong to `control_plane`.

from dataclasses import dataclass, replace
import errno
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, List, Optional
import uuid

from .json_files import JsonFileError, write_json_object_atomic
from .tickets import TicketReference
from .workspaces import (
    IdentityProblem,
    canonical_ticket_key,
    effective_worktree_id,
    verify_workspace_ticket_identity,
)


BINDINGS_DIRECTORY_NAME = "bindings"

ROLE_EXECUTOR = "executor"
ROLE_REVIEWER = "reviewer"
ROLE_ORCHESTRATOR = "orchestrator"
BINDING_ROLES = (ROLE_EXECUTOR, ROLE_REVIEWER, ROLE_ORCHESTRATOR)

# `bound` is the only nonterminal state. Bindings are never deleted: a consumed
# session id stays consumed, which is what makes "rebinding needs a new
# preassigned session id" provable rather than merely intended.
BINDING_STATE_BOUND = "bound"
BINDING_STATE_UNBOUND = "unbound"
BINDING_STATES = (BINDING_STATE_BOUND, BINDING_STATE_UNBOUND)
NONTERMINAL_BINDING_STATES = frozenset({BINDING_STATE_BOUND})

_OBJECT_NAME = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_RECORD_KEYS = (
    "version",
    "state",
    "project",
    "ticket",
    "workspaceKey",
    "worktreeId",
    "workspacePath",
    "rail",
    "role",
    "iteration",
    "sessionId",
    "pid",
    "pidDomain",
    "startedAt",
    "launchedAtHead",
    "boundAt",
)
_ITERATION_KEYS = ("rail", "blob")


class SessionBindingError(Exception):
    """A fail-closed binding refusal carrying one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# Stable refusal reasons. Callers branch on these; the detail text is for humans.
REASON_INVALID_IDENTITY = "invalid-identity"
REASON_INVALID_ROLE = "invalid-role"
REASON_INVALID_SESSION_ID = "invalid-session-id"
REASON_INVALID_ITERATION = "invalid-iteration"
REASON_WORKSPACE_IDENTITY_UNPROVEN = "workspace-identity-unproven"
REASON_WORKTREE_IDENTITY_MISMATCH = "worktree-identity-mismatch"
REASON_DUPLICATE_SESSION_ID = "duplicate-session-id"
REASON_DUPLICATE_RAIL_ITERATION = "duplicate-rail-iteration"
REASON_MALFORMED_RECORD = "malformed-record"
REASON_UNREADABLE_RECORD = "unreadable-record"
REASON_STORE_WRITE_FAILED = "store-write-failed"
REASON_UNKNOWN_SESSION = "unknown-session"
REASON_ALREADY_UNBOUND = "already-unbound"


def current_pid_domain() -> str:
    """The host a recorded pid is meaningful on. A pid alone identifies nothing."""
    try:
        return socket.gethostname()
    except OSError:
        return ""


@dataclass(frozen=True)
class RailIteration:
    """Which authorization a session was bound to: a rail plus its exact blob.

    The blob id of that rail's `rail.md` is the iteration counter the control
    plane already has. It is orchestrator-owned by construction and changes
    exactly when the authorization text changes, so no new counter is created.
    """

    rail: str
    blob: str

    def to_dict(self) -> dict:
        return {"rail": self.rail, "blob": self.blob}


@dataclass(frozen=True)
class BindingRecord:
    project: str
    ticket: str
    workspace_key: str
    worktree_id: str
    workspace_path: str
    rail: str
    role: str
    iteration: RailIteration
    session_id: str
    pid: int
    pid_domain: str
    started_at: str
    launched_at_head: str
    bound_at: str
    state: str = BINDING_STATE_BOUND

    @property
    def is_terminal(self) -> bool:
        return self.state not in NONTERMINAL_BINDING_STATES

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "state": self.state,
            "project": self.project,
            "ticket": self.ticket,
            "workspaceKey": self.workspace_key,
            "worktreeId": self.worktree_id,
            "workspacePath": self.workspace_path,
            "rail": self.rail,
            "role": self.role,
            "iteration": self.iteration.to_dict(),
            "sessionId": self.session_id,
            "pid": self.pid,
            "pidDomain": self.pid_domain,
            "startedAt": self.started_at,
            "launchedAtHead": self.launched_at_head,
            "boundAt": self.bound_at,
        }


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def _require_slug(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SLUG.match(value.strip()):
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "{0} must be a stable slug of 3-64 lowercase letters, digits, or "
            "hyphens; got {1!r}.".format(field, value),
        )
    return value.strip()


def _require_object_name(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _OBJECT_NAME.match(value.strip().lower()):
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "{0} must be a full 40-character Git object name; got {1!r}.".format(field, value),
        )
    return value.strip().lower()


def _require_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.match(value.strip()):
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "{0} must be a UTC timestamp like 2026-08-26T00:00:00Z; got {1!r}.".format(field, value),
        )
    return value.strip()


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "{0} must be a non-empty string; got {1!r}.".format(field, value),
        )
    return value.strip()


def _require_path(value: Any, *, field: str) -> str:
    """Absolutize where a workspace lives. The path is never ownership evidence."""
    if isinstance(value, Path):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        candidate = ""
    if not candidate:
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "{0} must be a non-empty path; got {1!r}.".format(field, value),
        )
    return str(Path(os.path.abspath(candidate)))


def _require_role(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in BINDING_ROLES:
        raise SessionBindingError(
            REASON_INVALID_ROLE,
            "role must be one of {0}; got {1!r}.".format(", ".join(BINDING_ROLES), value),
        )
    return value.strip()


def _require_state(value: Any) -> str:
    if not isinstance(value, str) or value.strip() not in BINDING_STATES:
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "state must be one of {0}; got {1!r}.".format(", ".join(BINDING_STATES), value),
        )
    return value.strip()


def validate_session_id(value: Any) -> str:
    """A preassigned provider session id: a canonical lowercase UUID string.

    Requiring the canonical form keeps one session one filename, so two spellings
    of the same id can never become two bindings.
    """
    if not isinstance(value, str) or not value.strip():
        raise SessionBindingError(
            REASON_INVALID_SESSION_ID, "sessionId must be a non-empty string."
        )
    candidate = value.strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError, TypeError):
        raise SessionBindingError(
            REASON_INVALID_SESSION_ID,
            "sessionId must be a UUID; got {0!r}.".format(value),
        ) from None
    if str(parsed) != candidate:
        raise SessionBindingError(
            REASON_INVALID_SESSION_ID,
            "sessionId must use the canonical lowercase UUID form; got {0!r}.".format(value),
        )
    return candidate


def _require_pid(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SessionBindingError(
            REASON_INVALID_IDENTITY, "pid must be a positive integer; got {0!r}.".format(value)
        )
    return value


def _require_iteration(value: Any, *, rail: str) -> RailIteration:
    if isinstance(value, RailIteration):
        iteration_rail, blob = value.rail, value.blob
    elif isinstance(value, dict):
        unknown = sorted(set(value) - set(_ITERATION_KEYS))
        if unknown:
            raise SessionBindingError(
                REASON_INVALID_ITERATION,
                "iteration has unknown key(s): {0}.".format(", ".join(unknown)),
            )
        iteration_rail, blob = value.get("rail"), value.get("blob")
    else:
        raise SessionBindingError(
            REASON_INVALID_ITERATION, "iteration must be a rail slug and a rail.md blob id."
        )

    iteration_rail = _require_slug(iteration_rail, field="iteration.rail")
    blob = _require_object_name(blob, field="iteration.blob")
    if iteration_rail != rail:
        raise SessionBindingError(
            REASON_INVALID_ITERATION,
            "iteration names rail '{0}' but the binding names '{1}'.".format(iteration_rail, rail),
        )
    return RailIteration(rail=iteration_rail, blob=blob)


def build_record(
    *,
    project: str,
    ticket: str,
    workspace_key: str,
    worktree_id: str,
    workspace_path: Any,
    rail: str,
    role: str,
    iteration: Any,
    session_id: str,
    pid: int,
    pid_domain: str,
    started_at: str,
    launched_at_head: str,
    bound_at: str,
    state: str = BINDING_STATE_BOUND,
) -> BindingRecord:
    """Validate every field of one binding. Missing identity fails closed."""
    rail_id = _require_slug(rail, field="rail")
    return BindingRecord(
        project=_require_slug(project, field="project"),
        ticket=_require_slug(ticket, field="ticket"),
        workspace_key=_require_text(workspace_key, field="workspaceKey"),
        worktree_id=_require_text(worktree_id, field="worktreeId"),
        workspace_path=_require_path(workspace_path, field="workspacePath"),
        rail=rail_id,
        role=_require_role(role),
        iteration=_require_iteration(iteration, rail=rail_id),
        session_id=validate_session_id(session_id),
        pid=_require_pid(pid),
        pid_domain=_require_text(pid_domain, field="pidDomain"),
        started_at=_require_timestamp(started_at, field="startedAt"),
        launched_at_head=_require_object_name(launched_at_head, field="launchedAtHead"),
        bound_at=_require_timestamp(bound_at, field="boundAt"),
        state=_require_state(state),
    )


def _record_from_payload(payload: Any, path: Path) -> BindingRecord:
    if not isinstance(payload, dict):
        raise SessionBindingError(
            REASON_MALFORMED_RECORD, "binding {0} is not a JSON object.".format(path)
        )
    unknown = sorted(set(payload) - set(_RECORD_KEYS))
    if unknown:
        raise SessionBindingError(
            REASON_MALFORMED_RECORD,
            "binding {0} has unknown key(s): {1}.".format(path, ", ".join(unknown)),
        )
    missing = sorted(set(_RECORD_KEYS) - set(payload))
    if missing:
        raise SessionBindingError(
            REASON_MALFORMED_RECORD,
            "binding {0} is missing field(s): {1}.".format(path, ", ".join(missing)),
        )
    if payload.get("version") != 1:
        raise SessionBindingError(
            REASON_MALFORMED_RECORD, "binding {0} must declare version 1.".format(path)
        )
    try:
        return build_record(
            project=payload["project"],
            ticket=payload["ticket"],
            workspace_key=payload["workspaceKey"],
            worktree_id=payload["worktreeId"],
            workspace_path=payload["workspacePath"],
            rail=payload["rail"],
            role=payload["role"],
            iteration=payload["iteration"],
            session_id=payload["sessionId"],
            pid=payload["pid"],
            pid_domain=payload["pidDomain"],
            started_at=payload["startedAt"],
            launched_at_head=payload["launchedAtHead"],
            bound_at=payload["boundAt"],
            state=payload["state"],
        )
    except SessionBindingError as exc:
        raise SessionBindingError(
            REASON_MALFORMED_RECORD, "binding {0} is malformed: {1}".format(path, exc.detail)
        ) from exc


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class BindingStore:
    """Controller-local binding files under an explicitly supplied root.

    The root is a parameter rather than a host path so tests and later
    controller composition choose it. A malformed or unreadable record is a
    refusal, never a record that is quietly skipped: silently ignoring one would
    let a duplicate session or a second binding for a rail slip through the very
    checks this store exists to make.
    """

    def __init__(self, root: Any) -> None:
        self.root = Path(os.path.abspath(str(root)))

    @property
    def bindings_directory(self) -> Path:
        return self.root / BINDINGS_DIRECTORY_NAME

    def path_for(self, session_id: str) -> Path:
        return self.bindings_directory / "{0}.json".format(validate_session_id(session_id))

    def read(self, session_id: str) -> Optional[BindingRecord]:
        return self._read_file(self.path_for(session_id))

    def _read_file(self, path: Path) -> Optional[BindingRecord]:
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SessionBindingError(
                REASON_UNREADABLE_RECORD, "cannot read binding {0}: {1}".format(path, exc)
            ) from exc
        if not text.strip():
            raise SessionBindingError(
                REASON_MALFORMED_RECORD, "binding {0} is empty.".format(path)
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SessionBindingError(
                REASON_MALFORMED_RECORD, "binding {0} is invalid JSON: {1}".format(path, exc.msg)
            ) from exc
        record = _record_from_payload(payload, path)
        if record.session_id != path.stem:
            raise SessionBindingError(
                REASON_MALFORMED_RECORD,
                "binding {0} names session {1}.".format(path, record.session_id),
            )
        return record

    def record_files(self) -> List[Path]:
        directory = self.bindings_directory
        if not directory.is_dir():
            return []
        return sorted(item for item in directory.iterdir() if item.is_file())

    def records(self) -> List[BindingRecord]:
        """Every binding in the store. Any unreadable record fails the whole read."""
        collected = []
        for path in self.record_files():
            record = self._read_file(path)
            if record is not None:
                collected.append(record)
        return collected

    def write_new(self, record: BindingRecord) -> None:
        path = self.path_for(record.session_id)
        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise SessionBindingError(
                    REASON_DUPLICATE_SESSION_ID,
                    "session {0} is already bound at {1}.".format(record.session_id, path),
                ) from exc
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED, "cannot create binding {0}: {1}".format(path, exc)
            ) from exc

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            # A half-written binding would be indistinguishable from a real one,
            # so the failed creation leaves nothing behind.
            try:
                os.unlink(str(path))
            except OSError:
                pass
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED, "cannot write binding {0}: {1}".format(path, exc)
            ) from exc

    def replace_record(self, record: BindingRecord) -> None:
        path = self.path_for(record.session_id)
        try:
            write_json_object_atomic(path, record.to_dict())
        except JsonFileError as exc:
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED, "cannot update binding {0}: {1}".format(path, exc)
            ) from exc


# ---------------------------------------------------------------------------
# Binding operations
# ---------------------------------------------------------------------------


def find_binding_for_iteration(
    store: BindingStore, *, project: str, ticket: str, iteration: RailIteration
) -> List[BindingRecord]:
    """Nonterminal bindings claiming one project/ticket/rail iteration."""
    return [
        record
        for record in store.records()
        if not record.is_terminal
        and record.project == project
        and record.ticket == ticket
        and record.iteration == iteration
    ]


def prove_workspace_identity(
    workspace_path: Path, *, reference: TicketReference, worktree_id: str
) -> str:
    """Prove this worktree owns this ticket, then return the canonical key.

    Ownership comes from the shared claim registry, so the caller's stated
    `worktree_id` must be the one Git assigns this checkout. The path is where
    the workspace happens to live and authorizes nothing on its own.
    """
    root = Path(os.path.abspath(str(workspace_path)))
    if not root.is_dir():
        raise SessionBindingError(
            REASON_WORKSPACE_IDENTITY_UNPROVEN,
            "workspace path {0} does not exist.".format(root),
        )

    observed = effective_worktree_id(root)
    if observed != _require_text(worktree_id, field="worktreeId"):
        raise SessionBindingError(
            REASON_WORKTREE_IDENTITY_MISMATCH,
            "workspace {0} is worktree {1}, not {2}.".format(root, observed, worktree_id),
        )

    problem = verify_workspace_ticket_identity(root, reference=reference)
    if isinstance(problem, IdentityProblem):
        raise SessionBindingError(REASON_WORKSPACE_IDENTITY_UNPROVEN, problem.detail)
    return canonical_ticket_key(reference)


def create_binding(
    store: BindingStore,
    *,
    project: str,
    ticket: str,
    reference: TicketReference,
    workspace_path: Any,
    worktree_id: str,
    rail: str,
    role: str,
    iteration: Any,
    session_id: str,
    pid: int,
    launched_at_head: str,
    started_at: str,
    bound_at: str,
    pid_domain: Optional[str] = None,
) -> BindingRecord:
    """Bind one preassigned session. Every refusal leaves the store untouched."""
    rail_id = _require_slug(rail, field="rail")
    resolved_iteration = _require_iteration(iteration, rail=rail_id)
    workspace_key = prove_workspace_identity(
        Path(os.path.abspath(str(workspace_path))), reference=reference, worktree_id=worktree_id
    )

    record = build_record(
        project=project,
        ticket=ticket,
        workspace_key=workspace_key,
        worktree_id=worktree_id,
        workspace_path=workspace_path,
        rail=rail_id,
        role=role,
        iteration=resolved_iteration,
        session_id=session_id,
        pid=pid,
        pid_domain=pid_domain if pid_domain is not None else current_pid_domain(),
        started_at=started_at,
        launched_at_head=launched_at_head,
        bound_at=bound_at,
    )

    existing = store.read(record.session_id)
    if existing is not None:
        raise SessionBindingError(
            REASON_DUPLICATE_SESSION_ID,
            "session {0} is already bound to rail {1}.".format(record.session_id, existing.rail),
        )

    held = find_binding_for_iteration(
        store, project=record.project, ticket=record.ticket, iteration=record.iteration
    )
    if held:
        raise SessionBindingError(
            REASON_DUPLICATE_RAIL_ITERATION,
            "rail {0} at iteration {1} is already bound to session {2}; unbind it before "
            "rebinding with a new session id.".format(
                record.rail, record.iteration.blob, held[0].session_id
            ),
        )

    store.write_new(record)
    return record


def unbind_session(store: BindingStore, session_id: str) -> BindingRecord:
    """Move one binding to its terminal state. The session id stays consumed.

    Removing the record instead would make the id reusable, and a reused id is
    exactly the ambiguity this store exists to refuse. Rebinding therefore always
    means a new preassigned session id and a new record.
    """
    record = store.read(session_id)
    if record is None:
        raise SessionBindingError(
            REASON_UNKNOWN_SESSION, "no binding for session {0}.".format(session_id)
        )
    if record.is_terminal:
        raise SessionBindingError(
            REASON_ALREADY_UNBOUND,
            "session {0} is already {1}.".format(record.session_id, record.state),
        )
    terminal = replace(record, state=BINDING_STATE_UNBOUND)
    store.replace_record(terminal)
    return terminal
