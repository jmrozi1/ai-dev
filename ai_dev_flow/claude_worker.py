"""The controller-owned worker: one process this controller started, and can prove it did."""

from __future__ import annotations

# Binding proves *which* session an assignment belongs to. This module proves the
# controller owns the process running it. Those are different claims, and only the
# second one survives a restart, a stray terminal, or a transcript that says
# otherwise -- because it rests on a parent-owned process handle in a process group
# this controller created, not on a lookup that could match somebody else's session.
#
# Three things follow from that, and they shape everything below.
#
# The worker is spawned without a shell, in its own POSIX session, so there is no
# intermediate process to lose the handle to and no shell to reinterpret an
# argument. The child environment is *constructed*, not inherited: an ambient
# `ANTHROPIC_API_KEY` or `CLAUDE_CODE_USE_BEDROCK` would silently move the session
# to a different credential or provider than the one the human authorized, so their
# mere presence is fatal. And the reservation is attached to a real pid only after
# the worker reports readiness -- before that moment there is no process identity to
# record, and inventing one would make every later liveness answer fiction.
#
# The protocol carries validated request data in and compact facts out. Assistant
# text, tool logs, and transcripts stay inside the worker and are reduced to
# booleans before they cross the pipe, because durable collaboration state is the
# handoff an executor publishes, not what a provider happened to say.

from dataclasses import dataclass
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .claude_runtime import (
    ClaudeRuntimeError,
    RuntimeRequest,
    build_option_fields,
    interpret_result,
    launch_request,
    require_supported_sdk,
    resume_request,
)
from .session_binding import (
    BINDING_STATE_RESERVED,
    BindingRecord,
    BindingStore,
    RailIteration,
    SessionBindingError,
    attach_process,
    current_pid_domain,
)


WORKER_MODULE = "ai_dev_flow.claude_worker"
PROTOCOL_VERSION = 1

MESSAGE_READY = "ready"
MESSAGE_RESULT = "result"
MESSAGE_ERROR = "error"
MESSAGE_STOPPED = "stopped"

COMMAND_LAUNCH = "launch"
COMMAND_RESUME = "resume"
COMMAND_SHUTDOWN = "shutdown"

# Any of these moves the session to a credential or provider route nobody
# authorized on this rail. Presence is checked; values are never read, logged, or
# copied anywhere.
FORBIDDEN_ENVIRONMENT_NAMES = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

# The child gets these and nothing else. `HOME` is what makes the default
# subscription route reachable; the rest are the operating-system, network, and
# locale inputs a real run needs. Everything absent from this tuple is dropped
# rather than passed through, so a new ambient variable cannot become load-bearing
# by accident.
PRESERVED_ENVIRONMENT_NAMES = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

DEFAULT_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class ClaudeWorkerError(Exception):
    """A fail-closed worker refusal carrying one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


REASON_SELECTOR_PRESENT = "credential-selector-present"
REASON_BINDING_NOT_RESERVED = "binding-not-reserved"
REASON_ITERATION_MISMATCH = "iteration-mismatch"
REASON_SPAWN_FAILED = "spawn-failed"
REASON_READINESS_FAILED = "readiness-failed"
REASON_WORKER_EXITED = "worker-exited"
REASON_PROTOCOL_VIOLATION = "protocol-violation"
REASON_COMMAND_TIMEOUT = "command-timeout"
REASON_WORKER_FATAL = "worker-fatal"
REASON_SHUTDOWN_INCOMPLETE = "shutdown-incomplete"
REASON_SDK_UNAVAILABLE = "sdk-unavailable"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------


def inspect_credential_selectors(source: Mapping) -> Tuple[str, ...]:
    """Names of authorized-route overrides present in an environment. Values untouched."""
    return tuple(name for name in FORBIDDEN_ENVIRONMENT_NAMES if name in source)


def build_worker_environment(
    source: Mapping, *, package_root: Any, extra: Optional[Mapping] = None
) -> dict:
    """Construct the child's environment from an explicit allowlist.

    Constructed rather than filtered-from-inherited: a deny list would let the next
    provider or credential variable through by default, and this is exactly the
    place where "by default" means an unauthorized account pays for the run.

    This is upstream of the SDK's `env={}` option, which merges with whatever the
    worker already has. That option adds nothing and removes nothing; the isolation
    is here, before the process exists.
    """
    present = inspect_credential_selectors(source)
    if present:
        raise ClaudeWorkerError(
            REASON_SELECTOR_PRESENT,
            "the controller environment sets {0}, which would move the worker to a "
            "credential or provider route this rail did not authorize. Only the "
            "presence of these names was inspected.".format(", ".join(present)),
        )

    environment = {}
    for name in PRESERVED_ENVIRONMENT_NAMES:
        value = source.get(name)
        if value is not None:
            environment[name] = value
    # The worker is started with `-m`, so it must be able to import this package
    # without depending on what its working directory happens to contain.
    environment["PYTHONPATH"] = str(package_root)
    if extra:
        for name, value in extra.items():
            if name in FORBIDDEN_ENVIRONMENT_NAMES:
                raise ClaudeWorkerError(
                    REASON_SELECTOR_PRESENT,
                    "refusing to add {0} to the worker environment.".format(name),
                )
            environment[str(name)] = str(value)
    return environment


# ---------------------------------------------------------------------------
# Controller side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerHandle:
    """A process this controller started and still holds directly."""

    process: Any
    pid: int
    pgid: int
    started_at: str
    sdk_version: Optional[str]
    sdk_detail: Optional[str]

    @property
    def sdk_available(self) -> bool:
        return self.sdk_version is not None


def _read_message(handle_stream, *, deadline: float, process) -> dict:
    """Read one newline-delimited JSON message, or fail closed on time or exit."""
    selector = selectors.DefaultSelector()
    try:
        selector.register(handle_stream, selectors.EVENT_READ)
    except (ValueError, OSError) as exc:
        # A closed or detached pipe is the worker being gone, not a programming
        # error to raise through. Shutdown depends on this staying fail-closed.
        selector.close()
        raise ClaudeWorkerError(
            REASON_WORKER_EXITED, "the worker's output stream is unusable: {0}".format(exc)
        ) from exc
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClaudeWorkerError(
                    REASON_COMMAND_TIMEOUT, "the worker did not answer within its bound."
                )
            if not selector.select(timeout=min(remaining, 0.5)):
                if process.poll() is not None:
                    raise ClaudeWorkerError(
                        REASON_WORKER_EXITED,
                        "the worker exited with code {0} before answering.".format(
                            process.returncode
                        ),
                    )
                continue
            line = handle_stream.readline()
            if not line:
                raise ClaudeWorkerError(
                    REASON_WORKER_EXITED,
                    "the worker closed its output stream (exit code {0}).".format(
                        process.poll()
                    ),
                )
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise ClaudeWorkerError(
                    REASON_PROTOCOL_VIOLATION,
                    "the worker emitted a non-JSON line: {0}".format(exc),
                ) from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                raise ClaudeWorkerError(
                    REASON_PROTOCOL_VIOLATION, "the worker emitted a malformed message."
                )
            return payload
    finally:
        selector.close()


def _close_streams(process) -> None:
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _terminate_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        for _ in range(20):
            if not process_group_alive(pgid):
                return
            time.sleep(0.05)


def process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def spawn_worker(
    *, environment: Mapping, cwd: Any, executable: Optional[str] = None,
    argv: Optional[Sequence[str]] = None,
):
    """Start the worker directly, in its own session. No shell, ever.

    `start_new_session` gives the child its own process group and session, so the
    controller can signal the whole tree it owns and later prove that tree is gone.
    A shell would insert a process the controller does not hold and would
    reinterpret arguments it already validated.
    """
    command = list(argv) if argv is not None else [
        executable or sys.executable, "-m", WORKER_MODULE
    ]
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            bufsize=1,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise ClaudeWorkerError(
            REASON_SPAWN_FAILED, "cannot start the worker: {0}".format(exc)
        ) from exc


def start_worker(
    store: BindingStore,
    record: BindingRecord,
    *,
    expected_iteration: RailIteration,
    package_root: Any,
    environment_source: Optional[Mapping] = None,
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    spawn=None,
    now=None,
) -> Tuple[WorkerHandle, BindingRecord]:
    """Launch one owned worker for one reservation, then attach its real identity.

    Order is the whole point. The reservation already exists; the process does not.
    If the spawn or the readiness handshake fails, the reservation is left exactly
    as it was -- an explicit record of a launch that did not happen, rather than a
    binding pointing at a pid that never existed.
    """
    clock = now if now is not None else _utc_now
    current = store.read(record.session_id)
    if current is None or current.state != BINDING_STATE_RESERVED:
        raise ClaudeWorkerError(
            REASON_BINDING_NOT_RESERVED,
            "session {0} is {1}; only a reserved binding may start a worker.".format(
                record.session_id, current.state if current is not None else "unknown"
            ),
        )
    if current.iteration != expected_iteration:
        raise ClaudeWorkerError(
            REASON_ITERATION_MISMATCH,
            "session {0} is reserved for iteration {1}, not {2}.".format(
                current.session_id, current.iteration.blob, expected_iteration.blob
            ),
        )

    environment = build_worker_environment(
        os.environ if environment_source is None else environment_source,
        package_root=package_root,
    )
    starter = spawn if spawn is not None else spawn_worker
    process = starter(environment=environment, cwd=current.workspace_path)

    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = process.pid

    try:
        message = _read_message(
            process.stdout, deadline=time.monotonic() + ready_timeout, process=process
        )
        if message.get("type") != MESSAGE_READY:
            raise ClaudeWorkerError(
                REASON_READINESS_FAILED,
                "the worker answered {0!r} instead of readiness.".format(message.get("type")),
            )
        if message.get("protocol") != PROTOCOL_VERSION:
            raise ClaudeWorkerError(
                REASON_PROTOCOL_VIOLATION,
                "the worker speaks protocol {0!r}, not {1}.".format(
                    message.get("protocol"), PROTOCOL_VERSION
                ),
            )
        reported_pid = message.get("pid")
        if reported_pid != process.pid:
            raise ClaudeWorkerError(
                REASON_READINESS_FAILED,
                "the worker reports pid {0!r} but the controller started {1}.".format(
                    reported_pid, process.pid
                ),
            )
        started_at = message.get("started_at")
        if not isinstance(started_at, str) or not started_at:
            raise ClaudeWorkerError(
                REASON_READINESS_FAILED, "the worker reported no start timestamp."
            )
    except ClaudeWorkerError:
        _terminate_group(pgid)
        _close_streams(process)
        process.poll()
        raise

    handle = WorkerHandle(
        process=process,
        pid=process.pid,
        pgid=pgid,
        started_at=started_at,
        sdk_version=message.get("sdk_version") if isinstance(message.get("sdk_version"), str) else None,
        sdk_detail=message.get("sdk_detail") if isinstance(message.get("sdk_detail"), str) else None,
    )

    try:
        bound = attach_process(
            store,
            current.session_id,
            pid=process.pid,
            pid_domain=current_pid_domain(),
            started_at=started_at,
            bound_at=clock(),
            expected_iteration=expected_iteration,
        )
    except SessionBindingError:
        _terminate_group(pgid)
        _close_streams(process)
        process.poll()
        raise
    return handle, bound


def require_worker_sdk(handle: WorkerHandle) -> str:
    """The worker's own SDK verdict, surfaced before any provider command is sent."""
    if not handle.sdk_available:
        raise ClaudeWorkerError(
            REASON_SDK_UNAVAILABLE,
            handle.sdk_detail or "the worker reports no usable claude-agent-sdk.",
        )
    return handle.sdk_version or ""


def _send(handle: WorkerHandle, payload: Mapping) -> None:
    try:
        handle.process.stdin.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.process.stdin.flush()
    except (OSError, ValueError) as exc:
        raise ClaudeWorkerError(
            REASON_WORKER_EXITED, "cannot write to the worker: {0}".format(exc)
        ) from exc


def run_request(
    handle: WorkerHandle,
    request: RuntimeRequest,
    *,
    prompt: str,
    markers: Sequence[str] = (),
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> dict:
    """Send one validated request and return the compact facts the worker reduces it to."""
    command = COMMAND_LAUNCH if request.is_launch else COMMAND_RESUME
    _send(
        handle,
        {
            "type": command,
            "protocol": PROTOCOL_VERSION,
            "mode": request.mode,
            "session_id": request.session_id,
            "prompt": prompt,
            "markers": list(markers),
            "options": build_option_fields(request),
        },
    )
    message = _read_message(
        handle.process.stdout, deadline=time.monotonic() + timeout, process=handle.process
    )
    if message.get("type") == MESSAGE_ERROR:
        raise ClaudeWorkerError(
            REASON_WORKER_FATAL,
            "{0}: {1}".format(message.get("reason"), message.get("detail")),
        )
    if message.get("type") != MESSAGE_RESULT:
        raise ClaudeWorkerError(
            REASON_PROTOCOL_VIOLATION,
            "expected a result, got {0!r}.".format(message.get("type")),
        )
    if message.get("session_id") != request.session_id:
        raise ClaudeWorkerError(
            REASON_PROTOCOL_VIOLATION,
            "the worker answered for session {0!r}, not {1}.".format(
                message.get("session_id"), request.session_id
            ),
        )
    return message


def shutdown_worker(
    handle: WorkerHandle, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
) -> dict:
    """Ask the worker to stop, then prove the process group it owned is gone.

    Bounded on purpose: this is the end of one proof, not a stop/recovery policy.
    Whether an unfinished turn should be resumable, and what a disconnected session
    means, belong to lifecycle reconciliation.
    """
    graceful = False
    try:
        _send(handle, {"type": COMMAND_SHUTDOWN, "protocol": PROTOCOL_VERSION})
        message = _read_message(
            handle.process.stdout, deadline=time.monotonic() + timeout, process=handle.process
        )
        graceful = message.get("type") == MESSAGE_STOPPED
    except ClaudeWorkerError:
        graceful = False

    try:
        handle.process.stdin.close()
    except OSError:
        pass
    try:
        handle.process.wait(timeout=timeout)
    except Exception:
        _terminate_group(handle.pgid)
    if process_group_alive(handle.pgid):
        _terminate_group(handle.pgid)

    _close_streams(handle.process)

    alive = process_group_alive(handle.pgid)
    if alive:
        raise ClaudeWorkerError(
            REASON_SHUTDOWN_INCOMPLETE,
            "process group {0} is still alive after shutdown.".format(handle.pgid),
        )
    return {
        "graceful": graceful,
        "exit_code": handle.process.returncode,
        "process_group_gone": True,
    }


def build_launch_request(record: BindingRecord, **kwargs: Any) -> RuntimeRequest:
    return launch_request(record, **kwargs)


def build_resume_request(record: BindingRecord, **kwargs: Any) -> RuntimeRequest:
    return resume_request(record, **kwargs)


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


REQUIRED_OPTION_INVARIANTS = (
    ("setting_sources", []),
    ("strict_mcp_config", True),
    ("permission_mode", "dontAsk"),
    ("continue_conversation", False),
    ("fork_session", False),
)


def _check_option_invariants(options: Any) -> dict:
    """Re-assert the isolation invariants on receipt.

    The controller built these, but the worker is the process that would actually
    load an ambient source, so it refuses rather than trusting what arrived.
    """
    if not isinstance(options, dict):
        raise ClaudeWorkerError(REASON_PROTOCOL_VIOLATION, "options must be an object.")
    for name, expected in REQUIRED_OPTION_INVARIANTS:
        if options.get(name) != expected:
            raise ClaudeWorkerError(
                REASON_PROTOCOL_VIOLATION,
                "option {0} is {1!r}, not {2!r}.".format(name, options.get(name), expected),
            )
    if options.get("mcp_servers"):
        raise ClaudeWorkerError(REASON_PROTOCOL_VIOLATION, "mcp_servers must be empty.")
    has_launch = bool(options.get("session_id"))
    has_resume = bool(options.get("resume"))
    if has_launch == has_resume:
        raise ClaudeWorkerError(
            REASON_PROTOCOL_VIOLATION,
            "exactly one of session_id and resume must be set.",
        )
    return options


def _emit(payload: Mapping, stream=None) -> None:
    target = stream if stream is not None else sys.stdout
    target.write(json.dumps(payload, sort_keys=True) + "\n")
    target.flush()


def _probe_sdk() -> Tuple[Optional[str], Optional[str]]:
    try:
        return require_supported_sdk(), None
    except ClaudeRuntimeError as exc:
        return None, "{0}: {1}".format(exc.reason, exc.detail)


def _scan_markers(text: str, markers: Iterable, seen: dict) -> None:
    """Record only whether a marker appeared. The text itself is never kept."""
    for marker in markers:
        if marker and marker in text:
            seen[marker] = True


def _run_provider(command: Mapping) -> dict:
    """Drive one bounded SDK call and reduce it to facts. Lazy import by design."""
    from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: WPS433

    options_fields = _check_option_invariants(command.get("options"))
    markers = [m for m in command.get("markers", []) if isinstance(m, str)]
    seen = {}
    observed = {}

    options = ClaudeAgentOptions(**options_fields)

    import asyncio

    async def drive() -> None:
        async for message in query(prompt=command.get("prompt", ""), options=options):
            for block in getattr(message, "content", []) or []:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    _scan_markers(text, markers, seen)
            if type(message).__name__ == "ResultMessage":
                observed.update(
                    {
                        "session_id": getattr(message, "session_id", None),
                        "subtype": getattr(message, "subtype", None),
                        "is_error": bool(getattr(message, "is_error", False)),
                        "num_turns": getattr(message, "num_turns", None),
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                    }
                )
                text = getattr(message, "result", None)
                if isinstance(text, str):
                    _scan_markers(text, markers, seen)

    asyncio.run(drive())
    if not observed:
        raise ClaudeWorkerError(
            REASON_WORKER_FATAL, "the provider returned no result message."
        )
    return {"observed": observed, "markers": {m: bool(seen.get(m)) for m in markers}}


def _result_payload(command: Mapping, produced: Mapping) -> dict:
    request = _request_view(command)
    reduced = interpret_result(request, produced["observed"])
    return {
        "type": MESSAGE_RESULT,
        "protocol": PROTOCOL_VERSION,
        "mode": reduced.mode,
        "session_id": reduced.session_id,
        "subtype": reduced.subtype,
        "is_error": reduced.is_error,
        "num_turns": reduced.num_turns,
        "total_cost_usd": reduced.total_cost_usd,
        "markers": produced["markers"],
    }


class _RequestView:
    """The two fields `interpret_result` needs, without rebuilding a full request.

    Rebuilding one here would mean re-validating controller-owned filesystem
    assets from inside the worker, which is both redundant and the wrong side of
    the boundary.
    """

    def __init__(self, session_id: str, mode: str) -> None:
        self.session_id = session_id
        self.mode = mode


def _request_view(command: Mapping) -> Any:
    session_id = command.get("session_id")
    mode = command.get("mode")
    if not isinstance(session_id, str) or not isinstance(mode, str):
        raise ClaudeWorkerError(REASON_PROTOCOL_VIOLATION, "the command names no session.")
    return _RequestView(session_id, mode)


def serve(stdin=None, stdout=None) -> int:
    """The worker entry point: announce identity, then answer bounded commands."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    sdk_version, sdk_detail = _probe_sdk()
    _emit(
        {
            "type": MESSAGE_READY,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "started_at": _utc_now(),
            "sdk_version": sdk_version,
            "sdk_detail": sdk_detail,
        },
        sink,
    )

    for line in source:
        if not line.strip():
            continue
        try:
            command = json.loads(line)
        except ValueError as exc:
            _emit(
                {"type": MESSAGE_ERROR, "reason": REASON_PROTOCOL_VIOLATION,
                 "detail": str(exc)}, sink,
            )
            continue
        kind = command.get("type") if isinstance(command, dict) else None
        if kind == COMMAND_SHUTDOWN:
            _emit({"type": MESSAGE_STOPPED, "protocol": PROTOCOL_VERSION}, sink)
            return 0
        if kind not in (COMMAND_LAUNCH, COMMAND_RESUME):
            _emit(
                {"type": MESSAGE_ERROR, "reason": REASON_PROTOCOL_VIOLATION,
                 "detail": "unsupported command {0!r}".format(kind)}, sink,
            )
            continue
        try:
            _emit(_result_payload(command, _run_provider(command)), sink)
        except (ClaudeWorkerError, ClaudeRuntimeError) as exc:
            _emit(
                {"type": MESSAGE_ERROR, "reason": exc.reason, "detail": exc.detail}, sink
            )
        except ImportError as exc:
            _emit(
                {"type": MESSAGE_ERROR, "reason": REASON_SDK_UNAVAILABLE,
                 "detail": "claude-agent-sdk is not importable: {0}".format(exc)}, sink,
            )
        except Exception as exc:  # noqa: BLE001 - the worker must never die silently
            _emit(
                {"type": MESSAGE_ERROR, "reason": REASON_WORKER_FATAL,
                 "detail": "{0}: {1}".format(type(exc).__name__, exc)}, sink,
            )
    return 0


if __name__ == "__main__":
    sys.exit(serve())
