"""The Agent SDK request boundary: exactly what the controller asks for, and nothing ambient."""

from __future__ import annotations

# Issue #55's controller launches executors through the Python Claude Agent SDK.
# The danger is not what the request says -- it is what the SDK would otherwise
# pick up on its own. Loaded without constraint, a session inherits user, project,
# and local settings files, whatever `.mcp.json` is reachable, plugin-declared
# hooks and MCP servers, and permission arrays nobody in this system authorized.
# Any of those can widen the executor past its rail.
#
# So this module states every relevant option explicitly, including the ones whose
# SDK default already happens to be empty. An option that is merely defaulted is
# an option a future SDK release may default differently; an option written down
# is a contract a test can hold.
#
# Two things are deliberately not here. There is no live invocation path that
# tests exercise -- the SDK is imported lazily or injected, so neither unit nor
# integration tests need it installed. And no provider output is retained beyond
# the identity and bounds needed to reconcile a run: transcripts are not
# collaboration state and never become durable.

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Tuple

from .session_binding import (
    BINDING_STATE_BOUND,
    BINDING_STATE_RESERVED,
    BindingRecord,
    RailIteration,
    validate_session_id,
)


DISTRIBUTION_NAME = "claude-agent-sdk"

# Python Agent SDK 0.1.59 and earlier ignore `setting_sources=[]`, so on those
# versions the single option this whole boundary rests on silently does nothing.
# There is no way to detect that at runtime from inside a session, which is why
# the version is a hard precondition rather than a warning.
MINIMUM_SDK_VERSION = (0, 1, 60)

MODE_LAUNCH = "launch"
MODE_RESUME = "resume"
RUNTIME_MODES = (MODE_LAUNCH, MODE_RESUME)

PERMISSION_MODE = "dontAsk"

PLUGIN_MANIFEST_DIRECTORY = ".claude-plugin"
PLUGIN_MANIFEST_FILENAME = "plugin.json"
PLUGIN_SKILLS_DIRECTORY = "skills"
SKILL_FILENAME = "SKILL.md"

# A plugin root may hold only its manifest directory and its skills directory.
# Everything else Claude Code auto-discovers from a plugin root -- `hooks/`,
# `agents/`, `commands/`, `.mcp.json`, `.lsp.json`, `bin/`, and the rest -- would
# activate capability this rail never authorized, so their presence is fatal
# rather than ignored.
ALLOWED_PLUGIN_ENTRIES = frozenset({PLUGIN_MANIFEST_DIRECTORY, PLUGIN_SKILLS_DIRECTORY})

# The manifest may describe the plugin and nothing more. `skills`, `commands`,
# `agents`, `hooks`, `mcpServers`, and `lspServers` all redirect component
# discovery to arbitrary paths, so a manifest carrying them can reintroduce
# exactly what the directory scan above rejects.
ALLOWED_MANIFEST_KEYS = frozenset({"name", "displayName", "version", "description"})

_VERSION_PART = re.compile(r"^[0-9]+$")


class ClaudeRuntimeError(Exception):
    """A fail-closed runtime refusal carrying one stable machine-readable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


REASON_SDK_MISSING = "sdk-missing"
REASON_SDK_VERSION_UNSUPPORTED = "sdk-version-unsupported"
REASON_SDK_VERSION_UNREADABLE = "sdk-version-unreadable"
REASON_INVALID_MODE = "invalid-mode"
REASON_BINDING_NOT_RESERVED = "binding-not-reserved"
REASON_BINDING_NOT_BOUND = "binding-not-bound"
REASON_SESSION_MISMATCH = "session-mismatch"
REASON_WORKSPACE_MISMATCH = "workspace-mismatch"
REASON_INVALID_ALLOWED_TOOLS = "invalid-allowed-tools"
REASON_INVALID_BOUNDS = "invalid-bounds"
REASON_ASSET_MISSING = "asset-missing"
REASON_ASSET_OUTSIDE_CONTROLLER_ROOT = "asset-outside-controller-root"
REASON_ASSET_INSIDE_WORKSPACE = "asset-inside-workspace"
REASON_PLUGIN_SURFACE_UNEXPECTED = "plugin-surface-unexpected"
REASON_PLUGIN_MANIFEST_UNEXPECTED = "plugin-manifest-unexpected"
REASON_PLUGIN_SKILL_MISSING = "plugin-skill-missing"
REASON_RESULT_SESSION_MISMATCH = "result-session-mismatch"


# ---------------------------------------------------------------------------
# SDK availability
# ---------------------------------------------------------------------------


def parse_version(text: Any) -> Tuple[int, ...]:
    """Read the numeric release of a version string, refusing what it cannot read.

    Only the leading numeric components are compared; a suffix such as `1.2.3rc1`
    is not a release this boundary will accept on faith.
    """
    if not isinstance(text, str) or not text.strip():
        raise ClaudeRuntimeError(
            REASON_SDK_VERSION_UNREADABLE, "SDK version {0!r} is not a string.".format(text)
        )
    parts = text.strip().split(".")
    numbers = []
    for part in parts:
        if not _VERSION_PART.match(part):
            raise ClaudeRuntimeError(
                REASON_SDK_VERSION_UNREADABLE,
                "SDK version {0!r} has a non-numeric component {1!r}; its ordering "
                "against {2} cannot be established.".format(
                    text, part, ".".join(str(number) for number in MINIMUM_SDK_VERSION)
                ),
            )
        numbers.append(int(part))
    return tuple(numbers)


def require_supported_sdk(version_reader=None, *, distribution: str = DISTRIBUTION_NAME) -> str:
    """Prove an adequate SDK is installed without importing it.

    Reading distribution metadata rather than importing keeps this check cheap and
    keeps an unusable SDK from executing any of its own import-time code.
    """
    reader = version_reader if version_reader is not None else _installed_version
    try:
        raw = reader(distribution)
    except ClaudeRuntimeError:
        raise
    except Exception as exc:
        raise ClaudeRuntimeError(
            REASON_SDK_MISSING,
            "the {0} distribution is not installed for this interpreter ({1}). Install it "
            "in the controller environment; this rail does not install packages.".format(
                distribution, exc
            ),
        ) from exc

    resolved = parse_version(raw)
    if resolved < MINIMUM_SDK_VERSION:
        raise ClaudeRuntimeError(
            REASON_SDK_VERSION_UNSUPPORTED,
            "{0} {1} ignores an empty setting_sources, so filesystem settings would load "
            "anyway; {2} or newer is required.".format(
                distribution, raw, ".".join(str(number) for number in MINIMUM_SDK_VERSION)
            ),
        )
    return raw.strip()


def _installed_version(distribution: str) -> str:
    from importlib import metadata

    return metadata.version(distribution)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _real(path: Any) -> str:
    """Resolve symlinks before any containment question is asked.

    Comparing unresolved paths would let a link inside the controller root point
    at the product worktree and pass every check below.
    """
    return os.path.realpath(os.path.abspath(str(path)))


def _is_within(candidate: str, ancestor: str) -> bool:
    if candidate == ancestor:
        return True
    return candidate.startswith(ancestor.rstrip(os.sep) + os.sep)


def validate_controller_asset(
    path: Any, *, controller_root: Any, workspace_path: Any, label: str
) -> str:
    """Prove one input is controller-owned and not reachable from the product tree.

    An executor that can edit its own prompt or its own skill has no bounded
    authorization at all, so both must live under a root the controller owns and
    the workspace cannot reach.
    """
    resolved = _real(path)
    root = _real(controller_root)
    workspace = _real(workspace_path)

    # Workspace containment is checked first: when both rules are broken, the fact
    # worth reporting is that the executor could edit this, not merely that it sits
    # in the wrong directory. A symlink out of the controller root lands here too.
    if _is_within(resolved, workspace):
        raise ClaudeRuntimeError(
            REASON_ASSET_INSIDE_WORKSPACE,
            "{0} {1} is inside the product workspace {2}; an executor could rewrite "
            "its own instructions.".format(label, resolved, workspace),
        )
    if not _is_within(resolved, root):
        raise ClaudeRuntimeError(
            REASON_ASSET_OUTSIDE_CONTROLLER_ROOT,
            "{0} {1} is outside the controller-owned root {2}.".format(label, resolved, root),
        )
    if not os.path.exists(resolved):
        raise ClaudeRuntimeError(
            REASON_ASSET_MISSING,
            "{0} {1} does not exist. The SDK skips a missing plugin path silently, so "
            "this is checked before invocation.".format(label, resolved),
        )
    return resolved


def validate_plugin_surface(plugin_root: Any, *, expected_skill: str) -> str:
    """Prove the plugin carries exactly one skill and no other capability."""
    root = Path(_real(plugin_root))
    if not root.is_dir():
        raise ClaudeRuntimeError(
            REASON_ASSET_MISSING, "plugin root {0} is not a directory.".format(root)
        )

    unexpected = sorted(
        entry.name for entry in root.iterdir() if entry.name not in ALLOWED_PLUGIN_ENTRIES
    )
    if unexpected:
        raise ClaudeRuntimeError(
            REASON_PLUGIN_SURFACE_UNEXPECTED,
            "plugin {0} carries unauthorized entr(ies): {1}. Only {2} are permitted.".format(
                root, ", ".join(unexpected), " and ".join(sorted(ALLOWED_PLUGIN_ENTRIES))
            ),
        )

    manifest_directory = root / PLUGIN_MANIFEST_DIRECTORY
    if manifest_directory.exists():
        _validate_manifest(manifest_directory)

    skills_root = root / PLUGIN_SKILLS_DIRECTORY
    if not skills_root.is_dir():
        raise ClaudeRuntimeError(
            REASON_PLUGIN_SKILL_MISSING,
            "plugin {0} has no {1}/ directory.".format(root, PLUGIN_SKILLS_DIRECTORY),
        )
    present = sorted(entry.name for entry in skills_root.iterdir())
    if present != [expected_skill]:
        raise ClaudeRuntimeError(
            REASON_PLUGIN_SKILL_MISSING,
            "plugin {0} exposes skill(s) {1}; exactly [{2}] was expected.".format(
                root, present or "none", expected_skill
            ),
        )
    if not (skills_root / expected_skill / SKILL_FILENAME).is_file():
        raise ClaudeRuntimeError(
            REASON_PLUGIN_SKILL_MISSING,
            "plugin {0} skill '{1}' has no {2}.".format(root, expected_skill, SKILL_FILENAME),
        )
    return str(root)


def _validate_manifest(manifest_directory: Path) -> None:
    entries = sorted(entry.name for entry in manifest_directory.iterdir())
    if entries != [PLUGIN_MANIFEST_FILENAME]:
        raise ClaudeRuntimeError(
            REASON_PLUGIN_MANIFEST_UNEXPECTED,
            "{0} holds {1}; only {2} is permitted.".format(
                manifest_directory, ", ".join(entries) or "nothing", PLUGIN_MANIFEST_FILENAME
            ),
        )
    manifest_path = manifest_directory / PLUGIN_MANIFEST_FILENAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClaudeRuntimeError(
            REASON_PLUGIN_MANIFEST_UNEXPECTED,
            "cannot read plugin manifest {0}: {1}".format(manifest_path, exc),
        ) from exc
    if not isinstance(payload, dict):
        raise ClaudeRuntimeError(
            REASON_PLUGIN_MANIFEST_UNEXPECTED,
            "plugin manifest {0} is not a JSON object.".format(manifest_path),
        )
    unknown = sorted(set(payload) - ALLOWED_MANIFEST_KEYS)
    if unknown:
        raise ClaudeRuntimeError(
            REASON_PLUGIN_MANIFEST_UNEXPECTED,
            "plugin manifest {0} declares {1}, which redirect component discovery "
            "outside the validated directory layout.".format(manifest_path, ", ".join(unknown)),
        )


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeRequest:
    """One immutable launch-or-resume invocation, fully validated before it exists."""

    mode: str
    workspace_path: str
    workspace_key: str
    worktree_id: str
    session_id: str
    role: str
    iteration: RailIteration
    controller_root: str
    prompt_file: str
    plugin_root: str
    expected_skill: str
    allowed_tools: Tuple[str, ...]
    max_turns: int
    max_budget_usd: float
    cli_path: Optional[str] = None

    @property
    def is_launch(self) -> bool:
        return self.mode == MODE_LAUNCH


def _require_allowed_tools(values: Any) -> Tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ClaudeRuntimeError(
            REASON_INVALID_ALLOWED_TOOLS, "allowed_tools must be a sequence of tool rules."
        )
    resolved = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ClaudeRuntimeError(
                REASON_INVALID_ALLOWED_TOOLS,
                "allowed_tools entries must be non-empty strings; got {0!r}.".format(value),
            )
        resolved.append(value.strip())
    if not resolved:
        raise ClaudeRuntimeError(
            REASON_INVALID_ALLOWED_TOOLS,
            "allowed_tools is empty. Under {0} that denies every tool, which is a "
            "misconfigured rail rather than a runnable one.".format(PERMISSION_MODE),
        )
    duplicates = sorted({name for name in resolved if resolved.count(name) > 1})
    if duplicates:
        raise ClaudeRuntimeError(
            REASON_INVALID_ALLOWED_TOOLS,
            "allowed_tools repeats {0}.".format(", ".join(duplicates)),
        )
    return tuple(resolved)


def _require_bounds(max_turns: Any, max_budget_usd: Any) -> Tuple[int, float]:
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0:
        raise ClaudeRuntimeError(
            REASON_INVALID_BOUNDS, "max_turns must be a positive integer; got {0!r}.".format(max_turns)
        )
    if isinstance(max_budget_usd, bool) or not isinstance(max_budget_usd, (int, float)):
        raise ClaudeRuntimeError(
            REASON_INVALID_BOUNDS,
            "max_budget_usd must be a number; got {0!r}.".format(max_budget_usd),
        )
    if max_budget_usd <= 0:
        raise ClaudeRuntimeError(
            REASON_INVALID_BOUNDS,
            "max_budget_usd must be positive; got {0!r}.".format(max_budget_usd),
        )
    return max_turns, float(max_budget_usd)


def _build_request(
    record: BindingRecord,
    *,
    mode: str,
    controller_root: Any,
    prompt_file: Any,
    plugin_root: Any,
    expected_skill: str,
    allowed_tools: Iterable,
    max_turns: int,
    max_budget_usd: float,
    workspace_path: Any = None,
    cli_path: Any = None,
) -> RuntimeRequest:
    workspace = _real(workspace_path if workspace_path is not None else record.workspace_path)
    if workspace != _real(record.workspace_path):
        raise ClaudeRuntimeError(
            REASON_WORKSPACE_MISMATCH,
            "session {0} is bound to workspace {1}, not {2}.".format(
                record.session_id, record.workspace_path, workspace
            ),
        )

    resolved_prompt = validate_controller_asset(
        prompt_file, controller_root=controller_root, workspace_path=workspace,
        label="system prompt file",
    )
    resolved_plugin = validate_controller_asset(
        plugin_root, controller_root=controller_root, workspace_path=workspace,
        label="plugin root",
    )
    validate_plugin_surface(resolved_plugin, expected_skill=expected_skill)

    turns, budget = _require_bounds(max_turns, max_budget_usd)
    return RuntimeRequest(
        mode=mode,
        workspace_path=workspace,
        workspace_key=record.workspace_key,
        worktree_id=record.worktree_id,
        session_id=validate_session_id(record.session_id),
        role=record.role,
        iteration=record.iteration,
        controller_root=_real(controller_root),
        prompt_file=resolved_prompt,
        plugin_root=resolved_plugin,
        expected_skill=expected_skill,
        allowed_tools=_require_allowed_tools(allowed_tools),
        max_turns=turns,
        max_budget_usd=budget,
        cli_path=str(cli_path) if cli_path is not None else None,
    )


def launch_request(record: BindingRecord, **kwargs: Any) -> RuntimeRequest:
    """Build the one launch this reservation authorizes.

    Launch is built from a *reserved* record, never a bound one: a bound record
    already has a process, so launching from it would start a second session
    under one session id.
    """
    if record.state != BINDING_STATE_RESERVED:
        raise ClaudeRuntimeError(
            REASON_BINDING_NOT_RESERVED,
            "session {0} is {1}; only a reserved binding authorizes a launch.".format(
                record.session_id, record.state
            ),
        )
    return _build_request(record, mode=MODE_LAUNCH, **kwargs)


def resume_request(record: BindingRecord, **kwargs: Any) -> RuntimeRequest:
    """Build the one exact resume this binding authorizes.

    Resume is built from a *bound* record, so the session being resumed is one
    this controller observed starting. There is no most-recent fallback anywhere
    in this module: `continue_conversation` would silently pick whatever session
    last ran in the directory, which is precisely the routing-by-inference the
    ticket forbids.
    """
    if record.state != BINDING_STATE_BOUND:
        raise ClaudeRuntimeError(
            REASON_BINDING_NOT_BOUND,
            "session {0} is {1}; only a bound binding authorizes an exact resume.".format(
                record.session_id, record.state
            ),
        )
    return _build_request(record, mode=MODE_RESUME, **kwargs)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def build_option_fields(request: RuntimeRequest) -> dict:
    """The exact `ClaudeAgentOptions` keyword arguments this request implies.

    Returned as plain data so the contract is testable without the SDK installed.
    Every ambient source is named and closed here rather than left to a default.
    """
    if request.mode not in RUNTIME_MODES:
        raise ClaudeRuntimeError(
            REASON_INVALID_MODE,
            "mode must be one of {0}; got {1!r}.".format(", ".join(RUNTIME_MODES), request.mode),
        )

    fields = {
        "cwd": request.workspace_path,
        # The empty list is the whole isolation story: no user, project, or local
        # settings file is read, so no permission array or hook can arrive from disk.
        "setting_sources": [],
        "system_prompt": {"type": "file", "path": request.prompt_file},
        "plugins": [{"type": "local", "path": request.plugin_root}],
        "mcp_servers": {},
        "strict_mcp_config": True,
        "permission_mode": PERMISSION_MODE,
        "allowed_tools": list(request.allowed_tools),
        "disallowed_tools": [],
        "add_dirs": [],
        "env": {},
        "extra_args": {},
        "hooks": None,
        "agents": None,
        "fallback_model": None,
        "max_turns": request.max_turns,
        "max_budget_usd": request.max_budget_usd,
        # Never a fallback route: continuing would resume whatever session last ran
        # here, and forking would mint an id the binding does not name.
        "continue_conversation": False,
        "fork_session": False,
    }

    if request.is_launch:
        fields["session_id"] = request.session_id
        fields["resume"] = None
    else:
        fields["session_id"] = None
        fields["resume"] = request.session_id

    if request.cli_path is not None:
        fields["cli_path"] = request.cli_path
    return fields


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeResult:
    """What a run is allowed to leave behind: identity, outcome, and spend.

    No transcript, no assistant text, no tool log. Provider output is not
    collaboration state, and the handoff the executor publishes is.
    """

    session_id: str
    mode: str
    subtype: Optional[str]
    is_error: bool
    num_turns: Optional[int] = None
    total_cost_usd: Optional[float] = None


def interpret_result(request: RuntimeRequest, observed: Mapping) -> RuntimeResult:
    """Reduce a provider result to the binding-relevant facts, refusing a stranger.

    The observed session id must equal the one this controller assigned. A
    different id means the SDK started or resumed some other session, and nothing
    downstream -- liveness, continuation, unbinding -- would be about the session
    the binding names.
    """
    observed_id = observed.get("session_id")
    if observed_id != request.session_id:
        raise ClaudeRuntimeError(
            REASON_RESULT_SESSION_MISMATCH,
            "requested session {0} but the provider reported {1!r}.".format(
                request.session_id, observed_id
            ),
        )
    subtype = observed.get("subtype")
    return RuntimeResult(
        session_id=request.session_id,
        mode=request.mode,
        subtype=subtype if isinstance(subtype, str) else None,
        is_error=bool(observed.get("is_error", False)) or subtype != "success",
        num_turns=observed.get("num_turns") if isinstance(observed.get("num_turns"), int) else None,
        total_cost_usd=(
            float(observed["total_cost_usd"])
            if isinstance(observed.get("total_cost_usd"), (int, float))
            and not isinstance(observed.get("total_cost_usd"), bool)
            else None
        ),
    )
