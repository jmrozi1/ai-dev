"""Controller-owned session bindings: one managed session, one exact assignment."""

from __future__ import annotations

# A binding is the durable proof that a specific provider session belongs to a
# specific project, ticket, Flow workspace, rail, role, and rail iteration.
#
# It has two phases because the facts arrive in two moments. The assignment and the
# preassigned session id exist *before* any process does, so they are reserved
# first -- that is what stops a launch from racing ahead of the record explaining
# it. Process identity (pid, its host, the process start time) cannot exist until
# a spawn has succeeded, so it is attached afterwards, atomically, to that exact
# reservation. A reserved record therefore carries no process fields at all rather
# than placeholder ones: a fabricated pid would be indistinguishable from an
# observed one, and every later liveness answer would be built on it.
#
# Identity is proved, never inferred. Workspace ownership comes from the shared
# claim registry through `workspaces.verify_workspace_ticket_identity`, and the
# recorded path is only where that workspace currently lives. Transcript content,
# terminal position, selected tab, window focus, and conversation are not evidence
# here and are deliberately absent from the record.
#
# The store is controller-local state. It is not product state and it is not
# control-plane state, so it never participates in the publication, ownership, or
# freshness rules that belong to `control_plane`. Controller-local is not
# single-process, though: more than one process may hold a store at one root, and
# reserving is a read-decide-write over every record rather than over one file. So
# the reservation commit takes the same exclusive-create boundary the allowance
# store already uses -- without it, two controllers each counting the last free slot
# would each create a different session id and admit one agent past the ceiling.

from dataclasses import dataclass, replace
import errno
import json
import os
from pathlib import Path
import re
import socket
import time
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

# `reserved` and `bound` are both nonterminal; only `unbound` is terminal.
# Bindings are never deleted: a consumed session id stays consumed, which is what
# makes "rebinding needs a new preassigned session id" provable rather than merely
# intended. An abandoned reservation is terminalized, not removed.
BINDING_STATE_RESERVED = "reserved"
BINDING_STATE_BOUND = "bound"
BINDING_STATE_UNBOUND = "unbound"
BINDING_STATES = (BINDING_STATE_RESERVED, BINDING_STATE_BOUND, BINDING_STATE_UNBOUND)
NONTERMINAL_BINDING_STATES = frozenset({BINDING_STATE_RESERVED, BINDING_STATE_BOUND})

# Facts that only a successful spawn can supply. They are absent while reserved,
# complete once bound, and all-or-nothing in every state.
PROCESS_FIELD_NAMES = ("pid", "pidDomain", "startedAt", "boundAt")

_OBJECT_NAME = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_ASSIGNMENT_KEYS = (
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
    "launchedAtHead",
    "reservedAt",
)
_RECORD_KEYS = _ASSIGNMENT_KEYS + PROCESS_FIELD_NAMES
_ITERATION_KEYS = ("rail", "blob")

# Exactly what one held reservation-commit lock records. The shape is the accepted
# allowance-store lock's, deliberately, so there is one lock record to understand
# rather than two spellings of the same thing.
_LOCK_KEYS = ("version", "generation", "pid", "acquiredAt", "operation")


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
REASON_CONCURRENCY_CEILING = "concurrency-ceiling-reached"
REASON_MALFORMED_RECORD = "malformed-record"
REASON_UNREADABLE_RECORD = "unreadable-record"
REASON_STORE_WRITE_FAILED = "store-write-failed"
REASON_STORE_LOCKED = "store-locked"
REASON_LOCK_MALFORMED = "store-lock-malformed"
REASON_LOCK_LOST = "store-lock-lost"
REASON_UNKNOWN_SESSION = "unknown-session"
REASON_ALREADY_UNBOUND = "already-unbound"
REASON_NOT_RESERVED = "not-reserved"
REASON_ITERATION_MISMATCH = "iteration-mismatch"


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
    """One session's assignment, plus its process identity once one exists.

    The four process fields are `None` for a reservation and complete for a
    binding. They are never partially populated, in any state.
    """

    project: str
    ticket: str
    workspace_key: str
    worktree_id: str
    workspace_path: str
    rail: str
    role: str
    iteration: RailIteration
    session_id: str
    launched_at_head: str
    reserved_at: str
    state: str = BINDING_STATE_RESERVED
    pid: Optional[int] = None
    pid_domain: Optional[str] = None
    started_at: Optional[str] = None
    bound_at: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state not in NONTERMINAL_BINDING_STATES

    @property
    def is_reserved(self) -> bool:
        return self.state == BINDING_STATE_RESERVED

    @property
    def has_process_identity(self) -> bool:
        return self.pid is not None

    def to_dict(self) -> dict:
        payload = {
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
            "launchedAtHead": self.launched_at_head,
            "reservedAt": self.reserved_at,
        }
        # Absent rather than null: there is no such thing as a half-known process.
        if self.has_process_identity:
            payload["pid"] = self.pid
            payload["pidDomain"] = self.pid_domain
            payload["startedAt"] = self.started_at
            payload["boundAt"] = self.bound_at
        return payload


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
    launched_at_head: str,
    reserved_at: str,
    state: str = BINDING_STATE_RESERVED,
    pid: Any = None,
    pid_domain: Any = None,
    started_at: Any = None,
    bound_at: Any = None,
) -> BindingRecord:
    """Validate one record whole. Missing or partial identity fails closed."""
    rail_id = _require_slug(rail, field="rail")
    resolved_state = _require_state(state)
    process = _require_process_identity(
        pid=pid,
        pid_domain=pid_domain,
        started_at=started_at,
        bound_at=bound_at,
        state=resolved_state,
    )
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
        launched_at_head=_require_object_name(launched_at_head, field="launchedAtHead"),
        reserved_at=_require_timestamp(reserved_at, field="reservedAt"),
        state=resolved_state,
        pid=process[0],
        pid_domain=process[1],
        started_at=process[2],
        bound_at=process[3],
    )


def _require_process_identity(*, pid, pid_domain, started_at, bound_at, state):
    """Process identity is all four fields or none, and `bound` demands all four.

    Half a process identity is worse than none: it would let a caller record a pid
    with no host to interpret it on, or a bound state with nothing to observe.
    """
    supplied = [
        value is not None for value in (pid, pid_domain, started_at, bound_at)
    ]
    if not any(supplied):
        if state == BINDING_STATE_BOUND:
            raise SessionBindingError(
                REASON_INVALID_IDENTITY,
                "a bound binding requires {0}.".format(", ".join(PROCESS_FIELD_NAMES)),
            )
        return (None, None, None, None)
    if not all(supplied):
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "process identity must supply all of {0} or none of them.".format(
                ", ".join(PROCESS_FIELD_NAMES)
            ),
        )
    if state == BINDING_STATE_RESERVED:
        raise SessionBindingError(
            REASON_INVALID_IDENTITY,
            "a reserved binding precedes its process and must carry no {0}.".format(
                ", ".join(PROCESS_FIELD_NAMES)
            ),
        )
    return (
        _require_pid(pid),
        _require_text(pid_domain, field="pidDomain"),
        _require_timestamp(started_at, field="startedAt"),
        _require_timestamp(bound_at, field="boundAt"),
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
    missing = sorted(set(_ASSIGNMENT_KEYS) - set(payload))
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
            launched_at_head=payload["launchedAtHead"],
            reserved_at=payload["reservedAt"],
            state=payload["state"],
            pid=payload.get("pid"),
            pid_domain=payload.get("pidDomain"),
            started_at=payload.get("startedAt"),
            bound_at=payload.get("boundAt"),
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

    @property
    def lock_path(self) -> Path:
        """Where the reservation-commit lock lives: beside the records, never among them.

        `records()` refuses any file in the bindings directory it cannot parse as a
        binding, so a lock kept inside it would turn every held lock into a
        store-wide read failure.
        """
        return self.root / "{0}.lock".format(BINDINGS_DIRECTORY_NAME)

    # -- exclusive boundary -----------------------------------------------

    def _acquire(self, operation: str) -> str:
        """Take exclusive ownership of one count-then-create decision.

        This is the accepted `claude_allowance_store` boundary applied to the store
        that needed it. `write_new` is atomic per session id, which stops two
        controllers from creating the *same* binding; it does nothing about two
        controllers that each counted the same occupancy and then created
        *different* bindings, which is exactly how a seventh agent gets past a
        ceiling of six. Exclusive creation of one lock file is the boundary that
        does, because the decision and the creation then happen inside it.

        An existing lock is never read, aged out, or broken here, and no ownership
        is inferred from its recorded pid or timestamp: held and malformed are the
        same answer -- refuse -- because a lock whose owner cannot be proven is
        exactly when guessing is most expensive. The refusal leaves the store
        byte-unchanged, so the caller may retry with the same preassigned session
        id safely.
        """
        generation = os.urandom(16).hex()
        payload = json.dumps(
            {"version": 1, "generation": generation, "pid": os.getpid(),
             "acquiredAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "operation": operation},
            indent=2, sort_keys=True,
        ) + "\n"
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise SessionBindingError(
                    REASON_STORE_LOCKED,
                    "{0} is held by another controller; retry with the same "
                    "preassigned session id".format(self.lock_path),
                ) from exc
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED,
                "cannot create {0}: {1}".format(self.lock_path, exc),
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Close before unlinking. If `os.fdopen` itself failed the descriptor is
            # still open, and on Windows an open handle refuses the unlink -- which
            # would leave a lock file no owner is behind and lock the store shut.
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(str(self.lock_path))
            except OSError:
                pass
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED,
                "cannot write {0}: {1}".format(self.lock_path, exc),
            ) from exc
        return generation

    def _release(self, generation: str) -> None:
        """Release only the generation this call acquired."""
        try:
            text = self.lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SessionBindingError(
                REASON_LOCK_LOST, "{0} vanished before release".format(self.lock_path)
            ) from None
        except OSError as exc:
            raise SessionBindingError(
                REASON_UNREADABLE_RECORD,
                "cannot read {0}: {1}".format(self.lock_path, exc),
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SessionBindingError(
                REASON_LOCK_MALFORMED,
                "{0} is invalid JSON: {1}".format(self.lock_path, exc.msg),
            ) from exc
        if not isinstance(payload, dict) or set(payload) != set(_LOCK_KEYS):
            raise SessionBindingError(
                REASON_LOCK_MALFORMED, "{0} is not a lock record".format(self.lock_path)
            )
        if payload.get("generation") != generation:
            raise SessionBindingError(
                REASON_LOCK_LOST,
                "{0} was replaced since this call acquired it".format(self.lock_path),
            )
        try:
            os.unlink(str(self.lock_path))
        except OSError as exc:
            raise SessionBindingError(
                REASON_STORE_WRITE_FAILED,
                "cannot release {0}: {1}".format(self.lock_path, exc),
            ) from exc

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


def reserve_binding(
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
    launched_at_head: str,
    reserved_at: str,
    ceiling: int,
) -> BindingRecord:
    """Phase one: claim the assignment and the preassigned session id, before launch.

    Nothing about a process is known yet and nothing about one is recorded. The
    reservation exists so that a spawn can never get ahead of the durable record
    that explains it -- if the spawn then fails, what remains is an explicit
    reserved record, not an unexplained live session.

    Every refusal leaves the store exactly as it was.
    """
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
        launched_at_head=launched_at_head,
        reserved_at=reserved_at,
        state=BINDING_STATE_RESERVED,
    )

    # Checked before the lock, so a malformed ceiling costs neither a lock nor a
    # write. Whether the ceiling is a usable number is a caller fault that no amount
    # of exclusivity would change.
    if type(ceiling) is not int or ceiling < 1:
        raise SessionBindingError(
            REASON_CONCURRENCY_CEILING,
            "the configured concurrency ceiling must be a positive whole number of "
            "agents, got {0!r}.".format(ceiling),
        )

    # Everything from here to the creation is one decision, and it is taken under
    # the store's exclusive boundary. Counting nonterminal records and then creating
    # a new one is a read-decide-write, and atomic per-session-id creation does not
    # serialize it: two controllers that each counted five of six permitted agents
    # would each create a different session id and leave seven. The lock is what
    # makes the count the reserving controller acted on still true when it writes.
    generation = store._acquire("reserve_binding")
    try:
        existing = store.read(record.session_id)
        if existing is not None:
            raise SessionBindingError(
                REASON_DUPLICATE_SESSION_ID,
                "session {0} is already {1} on rail {2}.".format(
                    record.session_id, existing.state, existing.rail
                ),
            )

        held = find_binding_for_iteration(
            store, project=record.project, ticket=record.ticket, iteration=record.iteration
        )
        if held:
            raise SessionBindingError(
                REASON_DUPLICATE_RAIL_ITERATION,
                "rail {0} at iteration {1} is already held by session {2} ({3}); unbind it "
                "before rebinding with a new session id.".format(
                    record.rail, record.iteration.blob, held[0].session_id, held[0].state
                ),
            )

        # Re-read at the commit point, not carried from the authorization that led
        # here. `authorize` decided against occupancy as it stood then; between that
        # decision and this write another reservation may have taken the last slot, and
        # a reservation is exactly what would still be invisible as a process. Counting
        # nonterminal records here can only overstate occupancy, never understate it,
        # which is the safe direction for a hard limit.
        occupied = [other for other in store.records() if not other.is_terminal]
        if len(occupied) >= ceiling:
            raise SessionBindingError(
                REASON_CONCURRENCY_CEILING,
                "{0} of {1} permitted managed agents already hold bindings ({2}); "
                "reserving another would admit one past the ceiling.".format(
                    len(occupied), ceiling,
                    ", ".join(sorted(other.session_id for other in occupied)),
                ),
            )

        store.write_new(record)
    finally:
        store._release(generation)
    return record


def attach_process(
    store: BindingStore,
    session_id: str,
    *,
    pid: int,
    pid_domain: str,
    started_at: str,
    bound_at: str,
    expected_iteration: Optional[RailIteration] = None,
) -> BindingRecord:
    """Phase two: attach the identity of the process that a successful spawn produced.

    Only a reserved record may be attached to, and the transition is a single
    atomic replacement of that record. Supplying `expected_iteration` proves the
    caller is attaching to the reservation it made, which matters because the
    orchestrator may rewrite the rail while a spawn is in flight.
    """
    record = store.read(session_id)
    if record is None:
        raise SessionBindingError(
            REASON_UNKNOWN_SESSION, "no binding for session {0}.".format(session_id)
        )
    if not record.is_reserved:
        raise SessionBindingError(
            REASON_NOT_RESERVED,
            "session {0} is {1}; only a reserved binding may take process "
            "identity.".format(record.session_id, record.state),
        )
    if expected_iteration is not None and record.iteration != expected_iteration:
        raise SessionBindingError(
            REASON_ITERATION_MISMATCH,
            "session {0} is reserved for iteration {1}, not {2}.".format(
                record.session_id, record.iteration.blob, expected_iteration.blob
            ),
        )

    bound = build_record(
        project=record.project,
        ticket=record.ticket,
        workspace_key=record.workspace_key,
        worktree_id=record.worktree_id,
        workspace_path=record.workspace_path,
        rail=record.rail,
        role=record.role,
        iteration=record.iteration,
        session_id=record.session_id,
        launched_at_head=record.launched_at_head,
        reserved_at=record.reserved_at,
        state=BINDING_STATE_BOUND,
        pid=pid,
        pid_domain=pid_domain,
        started_at=started_at,
        bound_at=bound_at,
    )
    store.replace_record(bound)
    return bound


def unbind_session(store: BindingStore, session_id: str) -> BindingRecord:
    """Terminalize a reserved or bound binding. The session id stays consumed.

    Removing the record instead would make the id reusable, and a reused id is
    exactly the ambiguity this store exists to refuse. Rebinding therefore always
    means a new preassigned session id and a new record. An abandoned reservation
    -- one whose spawn never succeeded -- ends here too, and stays visible as what
    it was rather than disappearing.
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
