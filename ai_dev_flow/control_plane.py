"""Git-backed current-state collaboration surface for orchestrator/executor rails."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from .json_files import JsonFileError, load_json_object, write_text_atomic
from .progress_record import (
    CONFIDENCES,
    PROGRESS_FILENAME,
    ProgressRecordError,
    empty_document,
    validate_document,
)
from .repository import (
    RepositoryError,
    config_file_for_repo_root,
    resolve_repo_root,
    workflow_state_file_for_repo_root,
)


class ControlPlaneError(Exception):
    """Raised for user-facing control-plane failures."""


# The control plane stores only current state; Git history preserves prior versions,
# so artifacts never become transcripts or append-only logs. One owning role per
# artifact means ordinary collaboration cannot produce an ambiguous shared mutation.
# The executor proposes; the orchestrator accepts.
ARTIFACT_OWNERS: dict[str, str] = {
    "state": "orchestrator",
    "rail": "orchestrator",
    "handoff": "executor",
    "decision": "orchestrator",
    "evidence": "evidence",
    "progress": "orchestrator",
}

ARTIFACT_FILENAMES: dict[str, str] = {
    "state": "state.md",
    "rail": "rail.md",
    "handoff": "handoff.md",
    "decision": "decision.json",
    "evidence": "evidence.json",
    "progress": PROGRESS_FILENAME,
}

RAIL_SCOPED_ARTIFACTS = frozenset({"rail", "handoff", "decision", "evidence"})

# The order a rail's artifacts are listed in. Named separately from the owner and
# filename maps because listing is presentation, and a reader should not have to
# guess that a dict's insertion order is load-bearing.
LISTED_RAIL_ARTIFACTS = ("rail", "handoff", "decision", "evidence")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_HEX_ONLY = re.compile(r"^[0-9a-f-]+$")

MAX_EVIDENCE_STRING = 240
MAX_EVIDENCE_OBSERVATIONS = 50

# Only these keys may enter Git. Anything else is refused rather than trimmed.
EVIDENCE_ALLOWED_KEYS = frozenset({"schemaVersion", "provenance", "sourceHealth", "observations"})
PROVENANCE_ALLOWED_KEYS = frozenset({"source", "sessionId", "turnId", "collectedAt"})
SOURCE_HEALTH_ALLOWED_KEYS = frozenset({"status", "detail"})
OBSERVATION_ALLOWED_KEYS = frozenset({"kind", "status", "count", "durationSeconds", "detail"})
SOURCE_HEALTH_STATUSES = frozenset({"validated", "partial", "unavailable", "error"})

# Named for actionable errors; the allowlist above is what actually enforces.
EVIDENCE_DENIED_KEYS = frozenset({
    "prompt", "prompts", "response", "responses", "command", "commands",
    "output", "stdout", "stderr", "transcript", "transcripts", "log", "logs",
    "telemetry", "toolResult", "toolResults", "diagnostics", "content",
})


# The durable human-attention record. Until this artifact existed, "a person is
# needed here" was something a reader had to infer from a rail being blocked, from
# an error string, or from prose that happened to end in a question mark. Every one
# of those inferences manufactures an interruption nobody raised, so the fact is
# now published explicitly, by the only role that can raise it, or it does not
# exist. `session_lifecycle.RailFacts.pending_human_decision` already stated the
# rule; this is the artifact that finally supplies it.
#
# Its bounds are deliberately no looser than `decision_queue`'s, so a record that
# validates here always constructs a `PendingDecision` -- the failure shows up at
# publication, where an orchestrator can fix it, rather than at render time.
DECISION_SCHEMA_VERSION = 1

MAX_DECISION_IDENTITY = 80
MAX_DECISION_RAIL = 200
MAX_DECISION_TITLE = 120
MAX_DECISION_EXPLANATION = 2000
MAX_DECISION_TIMESTAMP = 80
MAX_DECISION_EVIDENCE_REFERENCES = 8
MAX_DECISION_EVIDENCE_LABEL = 80
MAX_DECISION_EVIDENCE_LOCATOR = 200
MAX_DECISION_BLOCKER_STRING = 240

DECISION_ALLOWED_KEYS = frozenset({
    "schemaVersion", "decisionId", "project", "ticket", "rail", "raisedAt",
    "title", "explanation", "evidence", "blocker",
})
DECISION_EVIDENCE_ALLOWED_KEYS = frozenset({"label", "locator"})

# D8's blocker shape: the five things a human needs in order to clear a
# permission, configuration, capability, credential, or environment obstacle
# without first reconstructing what happened. All five or none -- a half-described
# blocker is the kind of item that sits in a queue being re-read and never acted on.
DECISION_BLOCKER_ALLOWED_KEYS = frozenset({
    "kind", "whatFailed", "missingCapability", "humanChange", "stateChanged", "nextAction",
})
DECISION_BLOCKER_KINDS = frozenset({
    "permission", "configuration", "capability", "credential", "environment",
})

_DECISION_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


CONTROL_PLANE_CONFIG_KEY = "controlPlane"
CONFIG_ALLOWED_KEYS = frozenset({"repository", "project", "ticket"})


class ControlPlaneConfig:
    """Where this product repository's coordination state lives."""

    def __init__(self, repository: Path, project: str, ticket: str) -> None:
        self.repository = repository
        self.project = project
        self.ticket = ticket


def _derive_ticket(product_repo_root: Path) -> str | None:
    try:
        state = load_json_object(workflow_state_file_for_repo_root(product_repo_root), missing_default={})
    except JsonFileError:
        return None
    issue_number = state.get("activeIssueNumber")
    if isinstance(issue_number, int) and not isinstance(issue_number, bool) and issue_number > 0:
        return f"issue-{issue_number}"
    return None


def resolve_control_plane_config(product_repo_root: Path) -> ControlPlaneConfig | None:
    """Return the configured control plane, or None when none is configured.

    A fresh agent must be able to find its coordination repository without being
    told in conversation. No configuration means the repository-local tasking
    rail remains the assignment; that is a supported outcome, not an error.
    """
    config_path = config_file_for_repo_root(product_repo_root)
    try:
        payload = load_json_object(config_path, missing_default={})
    except JsonFileError as exc:
        raise ControlPlaneError(str(exc)) from exc
    block = payload.get(CONTROL_PLANE_CONFIG_KEY)
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ControlPlaneError(f"Invalid {CONTROL_PLANE_CONFIG_KEY} in {config_path}: expected an object.")
    unknown = sorted(set(block) - CONFIG_ALLOWED_KEYS)
    if unknown:
        raise ControlPlaneError(
            f"Invalid {CONTROL_PLANE_CONFIG_KEY} in {config_path}: unknown key(s): {', '.join(unknown)}."
        )
    repository = block.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ControlPlaneError(f"Invalid {CONTROL_PLANE_CONFIG_KEY} in {config_path}: repository is required.")
    project = block.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ControlPlaneError(f"Invalid {CONTROL_PLANE_CONFIG_KEY} in {config_path}: project is required.")
    ticket = block.get("ticket")
    if ticket is None:
        ticket = _derive_ticket(product_repo_root)
    if not isinstance(ticket, str) or not ticket.strip():
        raise ControlPlaneError(
            f"Invalid {CONTROL_PLANE_CONFIG_KEY} in {config_path}: ticket is required when no "
            "active issue workflow can supply it."
        )
    resolved = Path(repository.strip()).expanduser()
    if not resolved.is_absolute():
        resolved = (product_repo_root / resolved).resolve()
    return ControlPlaneConfig(resolved, validate_identifier(project, label="project"), validate_identifier(ticket, label="ticket"))


def _git(repo_root: Path, arguments: list[str], *, check: bool = True) -> str:
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
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise ControlPlaneError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def resolve_coordination_repo(path: Path) -> Path:
    """Resolve an explicitly supplied coordination repository path."""
    if not path.is_dir():
        raise ControlPlaneError(f"Coordination repository does not exist: {path}")
    inside = _git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside != "true":
        raise ControlPlaneError(f"Coordination path is not a Git repository: {path}")
    return Path(_git(path, ["rev-parse", "--show-toplevel"]))


def validate_identifier(value: str, *, label: str) -> str:
    """Accept stable semantic identifiers; refuse session/agent/process shapes."""
    candidate = value.strip()
    if not _SLUG.match(candidate):
        raise ControlPlaneError(
            f"Invalid {label} '{value}': use a stable semantic slug of 3-64 "
            "lowercase letters, digits, or hyphens."
        )
    if not any(character.isalpha() for character in candidate):
        raise ControlPlaneError(f"Invalid {label} '{value}': must contain letters.")
    compact = candidate.replace("-", "")
    if len(compact) >= 24 and _HEX_ONLY.match(candidate):
        raise ControlPlaneError(
            f"Invalid {label} '{value}': looks like a session, agent, or process "
            "identifier. Use a stable semantic work identifier."
        )
    return candidate


def scope_directory(repo_root: Path, *, project: str, ticket: str) -> Path:
    return repo_root / validate_identifier(project, label="project") / validate_identifier(ticket, label="ticket")


def scope_relative(project: str, ticket: str) -> str:
    return (
        f"{validate_identifier(project, label='project')}/"
        f"{validate_identifier(ticket, label='ticket')}"
    )


def artifact_relative(*, project: str, ticket: str, artifact: str, rail: str | None) -> str:
    if artifact not in ARTIFACT_FILENAMES:
        raise ControlPlaneError(
            f"Unknown artifact '{artifact}': expected one of {', '.join(sorted(ARTIFACT_FILENAMES))}."
        )
    scope = scope_relative(project, ticket)
    if artifact in RAIL_SCOPED_ARTIFACTS:
        if rail is None:
            raise ControlPlaneError(f"Artifact '{artifact}' requires a rail identifier.")
        rail_id = validate_identifier(rail, label="rail identifier")
        return f"{scope}/rails/{rail_id}/{ARTIFACT_FILENAMES[artifact]}"
    if rail is not None:
        raise ControlPlaneError(f"Artifact '{artifact}' is scope-level and takes no rail identifier.")
    return f"{scope}/{ARTIFACT_FILENAMES[artifact]}"


def artifact_path(
    repo_root: Path, *, project: str, ticket: str, artifact: str, rail: str | None
) -> Path:
    return repo_root / artifact_relative(project=project, ticket=ticket, artifact=artifact, rail=rail)


def require_owner(artifact: str, role: str) -> None:
    owner = ARTIFACT_OWNERS.get(artifact)
    if owner is None:
        raise ControlPlaneError(f"Unknown artifact '{artifact}'.")
    if role != owner:
        raise ControlPlaneError(
            f"Role '{role}' cannot publish '{artifact}': it is owned by '{owner}'. "
            "The executor proposes; the orchestrator accepts."
        )


def _bounded_string(
    value: Any,
    *,
    field: str,
    subject: str = "Provider evidence",
    limit: int = MAX_EVIDENCE_STRING,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPlaneError(f"{subject} field '{field}' must be a non-empty string.")
    if len(value) > limit:
        raise ControlPlaneError(
            f"{subject} field '{field}' exceeds {limit} characters; "
            "publish a bounded projection, not raw content."
        )
    return value


def _reject_unknown(
    payload: dict[str, Any],
    allowed: Iterable[str],
    *,
    where: str,
    subject: str = "Provider evidence",
) -> None:
    """Refuse anything outside the allowlist, and name raw-content keys specifically.

    Both intakes share it: the denial list is a property of what may enter Git at
    all, not of which artifact happens to be arriving.
    """
    allowed_set = set(allowed)
    denied = sorted(set(payload) & EVIDENCE_DENIED_KEYS)
    if denied:
        raise ControlPlaneError(
            f"{subject} {where} contains excluded raw content key(s): {', '.join(denied)}. "
            "Raw prompts, responses, commands, tool results, transcripts, logs, and telemetry "
            "never enter the control plane."
        )
    unknown = sorted(set(payload) - allowed_set)
    if unknown:
        raise ControlPlaneError(
            f"{subject} {where} contains non-allowlisted key(s): {', '.join(unknown)}."
        )


def validate_evidence_projection(payload: Any) -> dict[str, Any]:
    """Validate a provider-neutral bounded projection with provenance and health.

    This intake owns no provider parsing or correlation. It only proves that a
    projection is bounded, attributed, and free of raw diagnostic content.
    """
    if not isinstance(payload, dict):
        raise ControlPlaneError("Provider evidence must be a JSON object.")
    _reject_unknown(payload, EVIDENCE_ALLOWED_KEYS, where="root")

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ControlPlaneError("Provider evidence requires a provenance object.")
    _reject_unknown(provenance, PROVENANCE_ALLOWED_KEYS, where="provenance")
    _bounded_string(provenance.get("source"), field="provenance.source")
    _bounded_string(provenance.get("collectedAt"), field="provenance.collectedAt")
    for optional in ("sessionId", "turnId"):
        if optional in provenance:
            _bounded_string(provenance[optional], field=f"provenance.{optional}")

    health = payload.get("sourceHealth")
    if not isinstance(health, dict):
        raise ControlPlaneError("Provider evidence requires a sourceHealth object.")
    _reject_unknown(health, SOURCE_HEALTH_ALLOWED_KEYS, where="sourceHealth")
    status = health.get("status")
    if status not in SOURCE_HEALTH_STATUSES:
        raise ControlPlaneError(
            f"Provider evidence sourceHealth.status must be one of {', '.join(sorted(SOURCE_HEALTH_STATUSES))}."
        )
    if "detail" in health:
        _bounded_string(health["detail"], field="sourceHealth.detail")

    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise ControlPlaneError("Provider evidence observations must be a list.")
    if len(observations) > MAX_EVIDENCE_OBSERVATIONS:
        raise ControlPlaneError(
            f"Provider evidence carries {len(observations)} observations; "
            f"the bounded intake accepts at most {MAX_EVIDENCE_OBSERVATIONS}."
        )
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ControlPlaneError(f"Provider evidence observation {index} must be an object.")
        _reject_unknown(observation, OBSERVATION_ALLOWED_KEYS, where=f"observation {index}")
        _bounded_string(observation.get("kind"), field=f"observation {index} kind")
        for optional in ("status", "detail"):
            if optional in observation:
                _bounded_string(observation[optional], field=f"observation {index} {optional}")
        for numeric in ("count", "durationSeconds"):
            if numeric in observation and not isinstance(observation[numeric], (int, float)):
                raise ControlPlaneError(f"Provider evidence observation {index} {numeric} must be numeric.")
            if isinstance(observation.get(numeric), bool):
                raise ControlPlaneError(f"Provider evidence observation {index} {numeric} must be numeric.")
    return payload


def _decision_evidence(entries: Any) -> list[dict[str, Any]]:
    """Bounded pointers to evidence, never the evidence itself."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ControlPlaneError("Human-decision record evidence must be a list.")
    if len(entries) > MAX_DECISION_EVIDENCE_REFERENCES:
        raise ControlPlaneError(
            f"Human-decision record carries {len(entries)} evidence references; the bounded "
            f"record accepts at most {MAX_DECISION_EVIDENCE_REFERENCES}."
        )
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ControlPlaneError(f"Human-decision record evidence {index} must be an object.")
        _reject_unknown(
            entry, DECISION_EVIDENCE_ALLOWED_KEYS,
            where=f"evidence {index}", subject="Human-decision record",
        )
        _bounded_string(
            entry.get("label"), field=f"evidence {index} label",
            subject="Human-decision record", limit=MAX_DECISION_EVIDENCE_LABEL,
        )
        _bounded_string(
            entry.get("locator"), field=f"evidence {index} locator",
            subject="Human-decision record", limit=MAX_DECISION_EVIDENCE_LOCATOR,
        )
    return entries


def _decision_blocker(blocker: Any) -> dict[str, Any] | None:
    """The D8 blocker block: absent, or complete. There is no partial version."""
    if blocker is None:
        return None
    if not isinstance(blocker, dict):
        raise ControlPlaneError("Human-decision record blocker must be an object.")
    _reject_unknown(
        blocker, DECISION_BLOCKER_ALLOWED_KEYS,
        where="blocker", subject="Human-decision record",
    )
    missing = sorted(DECISION_BLOCKER_ALLOWED_KEYS - set(blocker))
    if missing:
        raise ControlPlaneError(
            f"Human-decision record blocker is missing required key(s): {', '.join(missing)}. "
            "A partly described blocker cannot be cleared without asking again."
        )
    if blocker.get("kind") not in DECISION_BLOCKER_KINDS:
        raise ControlPlaneError(
            "Human-decision record blocker kind must be one of "
            f"{', '.join(sorted(DECISION_BLOCKER_KINDS))}."
        )
    # An explicit boolean, because "the worktree may have changed" is the one
    # answer a person cannot act on.
    if not isinstance(blocker.get("stateChanged"), bool):
        raise ControlPlaneError(
            "Human-decision record blocker stateChanged must be true or false."
        )
    for field in ("whatFailed", "missingCapability", "humanChange", "nextAction"):
        _bounded_string(
            blocker.get(field), field=f"blocker.{field}",
            subject="Human-decision record", limit=MAX_DECISION_BLOCKER_STRING,
        )
    return blocker


def validate_decision_record(payload: Any) -> dict[str, Any]:
    """Validate the explicit human-attention record an orchestrator published.

    Strict for the same reason the evidence intake is strict, and then some: this
    record is the sole thing in the system permitted to say a person is needed, so
    an unbounded or unattributed one would put an interruption in front of a human
    on the strength of text nobody vouched for.
    """
    subject = "Human-decision record"
    if not isinstance(payload, dict):
        raise ControlPlaneError(f"{subject} must be a JSON object.")
    _reject_unknown(payload, DECISION_ALLOWED_KEYS, where="root", subject=subject)

    version = payload.get("schemaVersion")
    if isinstance(version, bool) or version != DECISION_SCHEMA_VERSION:
        raise ControlPlaneError(
            f"{subject} schemaVersion must be exactly {DECISION_SCHEMA_VERSION}."
        )

    # Identity and routing use the control plane's own identifier shape, so a
    # session, agent, or process id cannot become a decision's name.
    validate_identifier(_bounded_string(
        payload.get("decisionId"), field="decisionId", subject=subject,
        limit=MAX_DECISION_IDENTITY,
    ), label="decision identity")
    for field in ("project", "ticket", "rail"):
        validate_identifier(_bounded_string(
            payload.get(field), field=field, subject=subject,
            limit=MAX_DECISION_RAIL,
        ), label=f"decision {field}")

    raised_at = _bounded_string(
        payload.get("raisedAt"), field="raisedAt", subject=subject,
        limit=MAX_DECISION_TIMESTAMP,
    )
    # The exact shape `session_lifecycle` parses, so the age a screen displays is
    # derived by the accepted helper rather than by a second timestamp reader.
    if not _DECISION_TIMESTAMP.match(raised_at):
        raise ControlPlaneError(
            f"{subject} raisedAt must be a UTC timestamp like 2026-08-26T00:00:00Z; "
            f"got '{raised_at}'."
        )

    _bounded_string(payload.get("title"), field="title", subject=subject, limit=MAX_DECISION_TITLE)
    _bounded_string(
        payload.get("explanation"), field="explanation", subject=subject,
        limit=MAX_DECISION_EXPLANATION,
    )
    _decision_evidence(payload.get("evidence"))
    _decision_blocker(payload.get("blocker"))
    return payload


PROCEED_SEQUENCE_FILENAME = "proceed-sequence.txt"
MAX_ALLOCATION_ATTEMPTS = 5
_SEQUENCE_VALUE = re.compile(r"^(0|[1-9][0-9]*)\n$")


def proceed_sequence_relative(project: str, ticket: str) -> str:
    return f"{scope_relative(project, ticket)}/{PROCEED_SEQUENCE_FILENAME}"


def parse_proceed_sequence(text: str | None) -> int:
    """Exactly one non-negative decimal integer and a final newline."""
    if text is None:
        raise ControlPlaneError(
            f"Cannot allocate a handoff indicator: {PROCEED_SEQUENCE_FILENAME} is missing. "
            "The orchestrator initializes the counter."
        )
    match = _SEQUENCE_VALUE.match(text)
    if match is None:
        raise ControlPlaneError(
            f"Cannot allocate a handoff indicator: {PROCEED_SEQUENCE_FILENAME} must hold exactly "
            "one non-negative decimal integer and a final newline."
        )
    return int(match.group(1))


def _git_capture(
    repo_root: Path, arguments: list[str], *, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = None
    if env is not None:
        merged = {**os.environ, **env}
    return subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=merged,
    )


def _build_counter_commit(repo_root: Path, *, base: str, relative: str, value: int) -> str:
    """Build the successor commit with plumbing so no local ref, index, or worktree moves."""
    blob = _git_capture(repo_root, ["hash-object", "-w", "--stdin"], stdin=f"{value}\n")
    if blob.returncode != 0:
        raise ControlPlaneError(f"Cannot allocate a handoff indicator: {blob.stderr.strip()}")
    blob_id = blob.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="control-plane-index-") as temporary:
        env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        for arguments in (
            ["read-tree", base],
            ["update-index", "--add", "--cacheinfo", f"100644,{blob_id},{relative}"],
        ):
            completed = _git_capture(repo_root, arguments, env=env)
            if completed.returncode != 0:
                raise ControlPlaneError(f"Cannot allocate a handoff indicator: {completed.stderr.strip()}")
        tree = _git_capture(repo_root, ["write-tree"], env=env)
        if tree.returncode != 0:
            raise ControlPlaneError(f"Cannot allocate a handoff indicator: {tree.stderr.strip()}")

    commit = _git_capture(
        repo_root,
        ["commit-tree", tree.stdout.strip(), "-p", base, "-m", f"Allocate handoff indicator {value}"],
    )
    if commit.returncode != 0:
        raise ControlPlaneError(f"Cannot allocate a handoff indicator: {commit.stderr.strip()}")
    return commit.stdout.strip()


def allocate_proceed_number(
    repo_root: Path, *, project: str, ticket: str, attempts: int = MAX_ALLOCATION_ATTEMPTS
) -> int:
    """Compare-and-swap the ticket counter against fresh remote state.

    Mechanical current state only: not a queue, lease, heartbeat, worker
    identity, history, or authorization source. Allocation happens after durable
    publication, never before.
    """
    relative = proceed_sequence_relative(project, ticket)
    branch = _git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    upstream = _tracked_upstream(repo_root, branch) if branch and branch != "HEAD" else None
    if upstream is None:
        raise ControlPlaneError(
            "Cannot allocate a handoff indicator: the coordination repository has no tracked "
            "upstream, so no compare-and-swap is possible."
        )
    remote_name, _, remote_branch = upstream.partition("/")

    for _ in range(max(1, attempts)):
        try:
            _git(repo_root, ["fetch", "--quiet", remote_name])
        except ControlPlaneError as exc:
            raise ControlPlaneError(
                f"Cannot allocate a handoff indicator: coordination remote '{remote_name}' "
                f"could not be fetched. {exc}"
            ) from exc

        base = _git(repo_root, ["rev-parse", upstream])
        local = _git(repo_root, ["rev-parse", branch], check=False)
        if local and not branch_contains(repo_root, ancestor=local, descendant=upstream):
            raise ControlPlaneError(
                f"Cannot allocate a handoff indicator: '{branch}' holds commits that are not on "
                f"{upstream}. Publish and push them first so the shared counter advances from "
                "the authoritative state."
            )

        source = ReadSource(repo_root, base, base)
        allocated = parse_proceed_sequence(source.read(relative)) + 1
        candidate = _build_counter_commit(repo_root, base=base, relative=relative, value=allocated)
        pushed = _git_capture(repo_root, ["push", remote_name, f"{candidate}:refs/heads/{remote_branch}"])
        if pushed.returncode == 0:
            return allocated
        stderr = pushed.stderr.lower()
        if "rejected" not in stderr and "non-fast-forward" not in stderr and "fetch first" not in stderr:
            raise ControlPlaneError(f"Cannot allocate a handoff indicator: {pushed.stderr.strip()}")

    raise ControlPlaneError(
        f"Cannot allocate a handoff indicator: {attempts} compare-and-swap attempts were all "
        "rejected by a concurrent allocator. No number was allocated."
    )


def branch_contains(repo_root: Path, *, ancestor: str, descendant: str) -> bool:
    completed = _git_capture(repo_root, ["merge-base", "--is-ancestor", ancestor, descendant])
    return completed.returncode == 0


def _tracked_upstream(repo_root: Path, branch: str) -> str | None:
    upstream = _git(
        repo_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"],
        check=False,
    )
    return upstream or None


def resolve_current_head(repo_root: Path) -> str:
    """Resolve the head fresh from the repository on every call; never cached."""
    return _git(repo_root, ["rev-parse", "HEAD"], check=False) or ""


# A Git operation in progress means the checkout is mid-edit, so no state it reports
# is a settled answer about what publication would land on.
ACTIVE_OPERATION_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)


def active_git_operation(repo_root: Path) -> str | None:
    """Name the in-progress Git operation, if any, so publication can refuse to guess."""
    git_dir = _git(repo_root, ["rev-parse", "--git-dir"], check=False)
    if not git_dir:
        return None
    root = Path(git_dir)
    if not root.is_absolute():
        root = repo_root / root
    for marker in ACTIVE_OPERATION_MARKERS:
        if (root / marker).exists():
            return marker
    return None


def worktree_is_clean(repo_root: Path) -> bool:
    """Report whether the checkout carries no staged, unstaged, or untracked change."""
    completed = _git_capture(repo_root, ["status", "--porcelain", "--untracked-files=all"])
    if completed.returncode != 0:
        raise ControlPlaneError(f"Cannot inspect the coordination checkout: {completed.stderr.strip()}")
    return not completed.stdout.strip()


def _reconcile_strictly_behind(repo_root: Path, *, branch: str, upstream: str, remote_head: str) -> None:
    """Fast-forward a clean, strictly behind branch onto freshly fetched upstream state.

    This is the one unambiguous case: nothing local is at stake, so requiring a human
    to type the same fast-forward adds no judgment. Every other shape still fails
    closed, and the advance itself is fast-forward only, never a rebase, merge, or reset.
    """
    operation = active_git_operation(repo_root)
    if operation is not None:
        raise ControlPlaneError(
            f"Cannot publish: coordination upstream {upstream} is ahead of {branch}, but a Git "
            f"operation ({operation}) is in progress. Finish or abort it, then republish."
        )
    if not worktree_is_clean(repo_root):
        raise ControlPlaneError(
            f"Cannot publish: coordination upstream {upstream} is ahead of {branch}, but the "
            "coordination checkout has uncommitted or untracked changes. Clear them, then "
            "republish."
        )

    advanced = _git_capture(repo_root, ["merge", "--ff-only", "--quiet", upstream])
    if advanced.returncode != 0:
        raise ControlPlaneError(
            f"Cannot publish: {branch} could not be fast-forwarded to {upstream}. "
            f"{advanced.stderr.strip() or advanced.stdout.strip()}"
        )
    if _git(repo_root, ["rev-parse", branch]) != remote_head:
        raise ControlPlaneError(
            f"Cannot publish: {branch} did not land on {upstream} after fast-forwarding. "
            "Re-read the current state and republish against it."
        )


def ensure_publishable(repo_root: Path) -> str:
    """Freshly resolve remote state and refuse to publish onto stale history.

    A clean, strictly behind branch is reconciled by fast-forward first, so publication
    lands on the freshly resolved upstream state rather than on stale history.
    """
    branch = _git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    upstream = _tracked_upstream(repo_root, branch) if branch and branch != "HEAD" else None
    if upstream is not None:
        remote = upstream.split("/", 1)[0]
        try:
            _git(repo_root, ["fetch", "--quiet", remote])
        except ControlPlaneError as exc:
            raise ControlPlaneError(
                f"Cannot publish: coordination remote '{remote}' could not be fetched. {exc}"
            ) from exc
        local_head = _git(repo_root, ["rev-parse", branch])
        remote_head = _git(repo_root, ["rev-parse", upstream])
        if local_head != remote_head:
            base = _git(repo_root, ["merge-base", branch, upstream], check=False)
            if base == local_head:
                _reconcile_strictly_behind(
                    repo_root, branch=branch, upstream=upstream, remote_head=remote_head
                )
            elif base != remote_head:
                raise ControlPlaneError(
                    f"Cannot publish: {branch} and {upstream} have diverged. "
                    "Re-read the current state and reconcile before republishing."
                )
    return resolve_current_head(repo_root)


def publish(
    repo_root: Path,
    *,
    project: str,
    ticket: str,
    artifact: str,
    role: str,
    content: str,
    rail: str | None = None,
    expected_head: str | None = None,
) -> tuple[Path, str]:
    """Replace one owned artifact with current state and commit only that path.

    The progress record is deliberately not publishable here. It is one half of an
    acceptance, and `accept` writes it together with the accepted state in a single
    commit; letting it be published alone would restore exactly the drift that
    pairing them removes.
    """
    if artifact == "progress":
        raise ControlPlaneError(
            "Cannot publish the progress record directly: it is written with the accepted "
            "state by `accept`, in one commit, so the two cannot disagree."
        )
    require_owner(artifact, role)
    target = artifact_path(repo_root, project=project, ticket=ticket, artifact=artifact, rail=rail)

    if artifact in ("evidence", "decision"):
        subject = {
            "evidence": "Provider evidence",
            "decision": "Human-decision record",
        }[artifact]
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ControlPlaneError(f"{subject} is not valid JSON: {exc.msg}") from exc
        if artifact == "evidence":
            validate_evidence_projection(payload)
        else:
            validate_decision_record(payload)
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    current_head = ensure_publishable(repo_root)
    if expected_head is not None and expected_head.strip() and expected_head.strip() != current_head:
        raise ControlPlaneError(
            f"Cannot publish: expected head {expected_head.strip()} but the coordination "
            f"repository is at {current_head or 'an empty history'}. Re-read the current "
            "state and republish against it."
        )

    try:
        write_text_atomic(target, content if content.endswith("\n") else content + "\n")
    except JsonFileError as exc:
        raise ControlPlaneError(str(exc)) from exc

    relative = target.relative_to(repo_root).as_posix()
    _git(repo_root, ["add", "--", relative])
    if not _git(repo_root, ["diff", "--cached", "--name-only", "--", relative], check=False):
        return target, current_head
    scope = f"{project}/{ticket}"
    subject = f"{role}: {artifact}" + (f" {rail}" if rail else "") + f" ({scope})"
    _git(repo_root, ["commit", "--quiet", "-m", subject, "--", relative])
    return target, resolve_current_head(repo_root)


# --------------------------------------------------------------------------
# The supported progress action
# --------------------------------------------------------------------------
#
# Accepting a numeric checkpoint is a durable orchestrator transition, and this
# is that transition -- the whole of it, in one commit.
#
# The ticket's acceptance convention is unchanged: accepted state is published to
# the control plane as `state.md`, exactly as every accepted checkpoint before
# this one was. What is added is a structured record of the same act, written by
# this one action into the same commit -- both paths or neither.
#
# There is exactly one acceptance *fact*, and it lives in the record. `accept` is
# the only action that writes it, it advances only forwards, and it is checked
# against product history before it lands. `state.md` carries the prose a person
# reads, which is never parsed and never was authority -- so a later reconciliation
# that republishes `state.md` alone, as reconciliations must be able to do, can
# restate the story but cannot move the accepted checkpoint. That is the whole of
# the guarantee: two descriptions, one fact, and no second thing to keep in step.
#
# The limit, stated rather than implied: nothing stops a person writing prose that
# contradicts the record. Prose is commentary, and the measure never reads it.
#
# The record it publishes carries no instant, because the commit it creates is
# the instant. `progress_store` reads those commits back through
# `git log -1 --format=%cI`, which is why a stated time cannot enter the measure
# even in principle: there is no field to state one in.
#
# Publishing a product checkpoint does not reach this action, which is what keeps
# a published-but-unaccepted checkpoint incapable of advancing the numerator.


def _accepted_checkpoint(record: dict[str, Any]) -> int:
    accepted = record.get("accepted")
    return 0 if accepted is None else int(accepted["checkpoint"])


def _completed_named(record: dict[str, Any]) -> int:
    named = record.get("named")
    return 0 if named is None else int(named["checkpoint"])


def read_progress_record(source: ReadSource, *, project: str, ticket: str) -> dict[str, Any]:
    """The ticket's currently published progress record, or the empty one."""
    relative = artifact_relative(
        project=project, ticket=ticket, artifact="progress", rail=None
    )
    text = source.read(relative)
    if text is None:
        return empty_document()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControlPlaneError(
            f"Published progress for {scope_relative(project, ticket)} is not valid JSON: {exc.msg}"
        ) from exc
    try:
        return validate_document(payload)
    except ProgressRecordError as exc:
        raise ControlPlaneError(
            f"Published progress for {scope_relative(project, ticket)} is refused: {exc}"
        ) from exc


def _checkpoint_subject(product_repo: Path, commit: str) -> str:
    """The Flow subject of one product commit, read from the product repository."""
    completed = _git_capture(product_repo, ["log", "-1", "--format=%s", commit, "--"])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise ControlPlaneError(
            f"Cannot accept: commit {commit} is not present in the product repository "
            f"{product_repo}. {detail}"
        )
    return completed.stdout.strip()


_FLOW_SUBJECT = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")


def _lineage_checkpoints(product_repo: Path, *, since: str, until: str) -> list[int]:
    """The numeric Flow subjects on the product lineage `since..until`, oldest first.

    Read from product history and nothing else. Commits that are not Flow
    checkpoints -- a merge, a note, anything whose subject is not a bare decimal
    number -- are not checkpoints and are skipped rather than refused; only what
    the product actually numbered counts. `--topo-order --reverse` puts a parent
    before its child, so the list is the order the checkpoints were reached
    rather than the order their commit dates happen to sort in.
    """
    completed = _git_capture(
        product_repo,
        ["log", "--topo-order", "--reverse", "--format=%s", f"{since}..{until}", "--"],
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise ControlPlaneError(
            f"Cannot accept: the product lineage {since}..{until} could not be read from "
            f"{product_repo}. {detail}"
        )
    return [
        int(subject)
        for subject in (line.strip() for line in completed.stdout.splitlines())
        if _FLOW_SUBJECT.match(subject)
    ]


def _require_contiguous_lineage(
    product_repo: Path,
    *,
    standing: int,
    standing_commit: str | None,
    checkpoint: int,
    commit: str,
) -> None:
    """Refuse unless the range this acceptance makes accepted is real product lineage.

    One acceptance may advance the accepted checkpoint by more than one, and when
    it does, every checkpoint it steps over becomes accepted by that same act.
    The record therefore has to be able to stand for the whole range `standing+1
    .. checkpoint` -- so this proves the range from product history before the
    record is allowed to imply it, rather than letting the reader manufacture the
    integers in between.

    Three things are established, all from the product repository:

    - the previously accepted product commit **is an ancestor of** the one being
      accepted, so the two are one lineage and not two branches;
    - the numeric Flow subjects reachable from the new commit but not from the
      previously accepted one are **exactly** `standing+1, standing+2, ... checkpoint`,
      in that order, so every newly accepted checkpoint exists on that lineage and
      its number is read from product history rather than stated;
    - nothing else numbered is in there, so a checkpoint cannot be hidden by the
      jump, and a checkpoint that does not exist cannot be invented by it.

    The first acceptance into an empty record has no previously accepted commit
    to measure against. It stands for itself alone, and no earlier checkpoint is
    implied by it -- which is why adopting this record mid-ticket does not
    retroactively claim checkpoints nobody published a record for.
    """
    if standing_commit is None:
        return
    if not branch_contains(product_repo, ancestor=standing_commit, descendant=commit):
        raise ControlPlaneError(
            f"Cannot accept: the accepted checkpoint {standing} at {standing_commit} is not an "
            f"ancestor of {commit} in {product_repo}. Acceptance advances one product lineage, "
            "so the checkpoints between them can be read from history rather than assumed."
        )
    reached = _lineage_checkpoints(product_repo, since=standing_commit, until=commit)
    expected = list(range(standing + 1, checkpoint + 1))
    if reached != expected:
        raise ControlPlaneError(
            f"Cannot accept: accepting checkpoint {checkpoint} over the accepted checkpoint "
            f"{standing} makes {_range_phrase(expected)} accepted, but the product lineage "
            f"{standing_commit}..{commit} carries {_range_phrase(reached)}. The accepted range "
            "is read from product history; it is never manufactured."
        )


def _range_phrase(checkpoints: list[int]) -> str:
    if not checkpoints:
        return "no numbered checkpoint"
    return "checkpoint " + ", ".join(str(number) for number in checkpoints)


def _publish_acceptance(
    repo_root: Path,
    *,
    project: str,
    ticket: str,
    state: str,
    document: dict[str, Any],
    expected_head: str | None,
) -> tuple[Path, str]:
    """Write the accepted state and its derived record, and commit them together.

    One commit, two paths. `publish` deliberately commits a single artifact, which
    is right for every artifact whose truth stands alone; acceptance is the one
    transition whose two representations must not be separable, so it gets this
    instead of two calls. A caller cannot land one and lose the other: either the
    commit exists with both paths in it, or nothing moved.
    """
    state_target = artifact_path(
        repo_root, project=project, ticket=ticket, artifact="state", rail=None
    )
    record_target = artifact_path(
        repo_root, project=project, ticket=ticket, artifact="progress", rail=None
    )

    current_head = ensure_publishable(repo_root)
    if expected_head is not None and expected_head.strip() and expected_head.strip() != current_head:
        raise ControlPlaneError(
            f"Cannot accept: expected head {expected_head.strip()} but the coordination "
            f"repository is at {current_head or 'an empty history'}. Re-read the current "
            "state and accept against it."
        )

    body = state if state.endswith("\n") else state + "\n"
    record = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        write_text_atomic(state_target, body)
        write_text_atomic(record_target, record)
    except JsonFileError as exc:
        raise ControlPlaneError(str(exc)) from exc

    relatives = [
        state_target.relative_to(repo_root).as_posix(),
        record_target.relative_to(repo_root).as_posix(),
    ]
    _git(repo_root, ["add", "--"] + relatives)
    if not _git(repo_root, ["diff", "--cached", "--name-only", "--"] + relatives, check=False):
        return record_target, current_head
    scope = f"{project}/{ticket}"
    subject = f"{ARTIFACT_OWNERS['progress']}: accept ({scope})"
    _git(repo_root, ["commit", "--quiet", "-m", subject, "--"] + relatives)
    return record_target, resolve_current_head(repo_root)


def accept_progress(
    repo_root: Path,
    *,
    project: str,
    ticket: str,
    state: str,
    remaining: int,
    confidence: str,
    note: str = "",
    checkpoint: int | None = None,
    commit: str | None = None,
    named: int | None = None,
    named_total: int | None = None,
    product_repo: Path | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    """Publish the ticket's current progress as one durable orchestrator transition.

    One act, three possible facts, and the estimate always restated. A checkpoint
    acceptance moves the numerator, a named completion moves the roadmap position,
    and the projection is reconsidered every time -- because D11 asks that it be
    reconsidered at every acceptance, and a record that could omit it would let
    "did not reconsider" and "reconsidered and preserved" look alike.

    The accepted state and its derived record are published together, in one
    commit, by this one action. Neither can move without the other, so the prose a
    person reads and the record a machine reads cannot drift apart -- not because
    a caller is trusted to run two commands, but because no supported action
    writes either alone.

    What is *not* stated is what makes the record derived rather than a second
    truth. There is no instant parameter: the commit this creates carries the time. The
    projection's basis is not a parameter either: it is the accepted checkpoint
    standing in the very record being published. And an accepted checkpoint is
    cross-checked against the product repository -- the commit must exist there
    and its Flow subject must be that checkpoint number -- so the number is
    checked against durable product history rather than taken on the caller's
    word.

    One acceptance may advance the accepted checkpoint by more than one, and when
    it does, every checkpoint it steps over becomes accepted by this same act and
    is derived back out of the record as such. So the whole range it makes
    accepted is proved against the product lineage first: see
    `_require_contiguous_lineage`. A jump the product history does not actually
    carry is refused, never filled in.

    Publication is fail-closed against a lost record. The currently published
    state is read first and its head is carried into `publish`, so a record that
    landed in between refuses this one rather than overwriting it. That, and not
    a lock, is what makes a concurrent acceptance impossible to lose: the two
    writers are serialized by the coordination repository's own history.
    """
    source = resolve_read_source(repo_root)
    current = read_progress_record(source, project=project, ticket=ticket)

    accepted = current["accepted"]
    if checkpoint is None:
        if commit is not None:
            raise ControlPlaneError(
                "Cannot accept: --commit names the product checkpoint being accepted, so it "
                "needs --checkpoint. Omit both to reconsider the estimate alone."
            )
    else:
        if commit is None:
            raise ControlPlaneError(
                "Cannot accept: --checkpoint needs the --commit it accepts, so the recorded "
                "acceptance can be checked against product history."
            )
        standing = _accepted_checkpoint(current)
        if checkpoint <= standing:
            raise ControlPlaneError(
                f"Cannot accept: checkpoint {checkpoint} does not follow the accepted "
                f"checkpoint {standing}. Acceptance advances; it never repeats or regresses."
            )
        root = Path(product_repo).expanduser() if product_repo is not None else resolve_repo_root()
        subject = _checkpoint_subject(root, commit)
        if subject != str(checkpoint):
            raise ControlPlaneError(
                f"Cannot accept: commit {commit} has Flow subject {subject!r}, not "
                f"{str(checkpoint)!r}. The accepted checkpoint number is read from product "
                "history, not stated."
            )
        _require_contiguous_lineage(
            root,
            standing=standing,
            standing_commit=None if accepted is None else accepted["commit"],
            checkpoint=checkpoint,
            commit=commit,
        )
        accepted = {"checkpoint": checkpoint, "commit": commit}

    completion = current["named"]
    if named is None:
        if named_total is not None:
            raise ControlPlaneError(
                "Cannot accept: --named-total states the roadmap size a named completion was "
                "reached on, so it needs --named."
            )
    else:
        if named_total is None:
            raise ControlPlaneError(
                "Cannot accept: --named needs --named-total, because a roadmap may honestly "
                "grow and a completion means nothing without the size it was reached on."
            )
        standing = _completed_named(current)
        # Named completion is the contiguous prefix of the roadmap, so a
        # completion follows the standing one by exactly one. Strictly
        # increasing is not enough: 1 -> 3 would leave the served page
        # asserting that named checkpoint 2 was completed with nothing
        # recording it. The first completion into an empty record has no
        # standing one to follow and stands alone -- the record may be adopted
        # part-way through a roadmap, and doing so claims no earlier checkpoint.
        # This action deliberately has no grouped named completion; one act
        # completes one named checkpoint.
        if named <= standing or (standing and named != standing + 1):
            raise ControlPlaneError(
                f"Cannot accept: named checkpoint {named} does not follow the completed named "
                f"checkpoint {standing}. Completed named checkpoints are the contiguous prefix "
                "of the roadmap; completion advances one checkpoint at a time."
            )
        completion = {"checkpoint": named, "total": named_total}

    proposed = {
        "schemaVersion": current["schemaVersion"],
        "accepted": accepted,
        "named": completion,
        "projection": {"confidence": confidence, "note": note, "remaining": remaining},
    }
    try:
        document = validate_document(proposed)
    except ProgressRecordError as exc:
        raise ControlPlaneError(f"Cannot accept: {exc}") from exc

    target, head = _publish_acceptance(
        repo_root,
        project=project,
        ticket=ticket,
        state=state,
        document=document,
        expected_head=source.head,
    )
    return target, head, document


class ReadSource:
    """What a read actually returns: a fetched revision, or the local worktree.

    Guidance requires a fresh durable read, so a clone behind its upstream must
    serve the fetched upstream content. Reads never move the branch, index, or
    worktree; only remote-tracking refs advance.
    """

    def __init__(self, repo_root: Path, revision: str | None, head: str) -> None:
        self.repo_root = repo_root
        self.revision = revision
        self.head = head

    def read(self, relative: str) -> str | None:
        if self.revision is None:
            try:
                return (self.repo_root / relative).read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ControlPlaneError(f"Cannot read {relative}: {exc}") from exc
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "show", f"{self.revision}:{relative}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        return completed.stdout if completed.returncode == 0 else None

    def exists(self, relative: str) -> bool:
        if self.revision is None:
            return (self.repo_root / relative).is_file()
        completed = subprocess.run(
            ["git", "-C", str(self.repo_root), "cat-file", "-e", f"{self.revision}:{relative}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return completed.returncode == 0

    def blob_sha(self, relative: str) -> str | None:
        """Git blob id of one artifact at the revision this read actually serves.

        Rail authorizations are versioned by content, so the controller needs the
        exact object name of a `rail.md` to pin the iteration it acted on. Text
        alone cannot supply it, and hashing the text elsewhere would reinvent Git
        object naming outside the module that owns control-plane reads.
        """
        if self.revision is None:
            path = self.repo_root / relative
            if not path.is_file():
                return None
            return _git(self.repo_root, ["hash-object", "--", str(path)], check=False) or None
        return _git(
            self.repo_root,
            ["rev-parse", "--verify", "--quiet", f"{self.revision}:{relative}"],
            check=False,
        ) or None

    def rails(self, scope: str) -> list[str]:
        if self.revision is None:
            rails_root = self.repo_root / scope / "rails"
            if not rails_root.is_dir():
                return []
            return sorted(path.name for path in rails_root.glob("*") if path.is_dir())
        listing = _git(
            self.repo_root,
            ["ls-tree", "-d", "--name-only", f"{self.revision}:{scope}/rails"],
            check=False,
        )
        return sorted(line.strip().rstrip("/") for line in listing.splitlines() if line.strip())


def rail_blob_sha(source: ReadSource, *, project: str, ticket: str, rail: str) -> str | None:
    """The iteration id of one rail: the blob its authorization was read from."""
    return source.blob_sha(
        artifact_relative(project=project, ticket=ticket, artifact="rail", rail=rail)
    )


def rail_handoff_publication(
    source: ReadSource, *, project: str, ticket: str, rail: str
) -> tuple[str, bool]:
    """Where one rail's executor handoff lives, and whether it is published.

    Presence and location, deliberately nothing about the content. A handoff is
    executor-authored evidence whose contract is read by the reviewer and
    orchestrator loop; a second reader that judged its prose would be a second
    opinion about work this module does not own.
    """
    relative = artifact_relative(project=project, ticket=ticket, artifact="handoff", rail=rail)
    return relative, source.exists(relative)


def resolve_read_source(repo_root: Path) -> ReadSource:
    """Resolve tracked remote state freshly, or fail closed on ambiguity."""
    local_head = resolve_current_head(repo_root)
    remotes = [name for name in _git(repo_root, ["remote"], check=False).splitlines() if name.strip()]
    if not remotes:
        return ReadSource(repo_root, None, local_head)

    branch = _git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    upstream = _tracked_upstream(repo_root, branch) if branch and branch != "HEAD" else None
    if upstream is None:
        raise ControlPlaneError(
            f"Cannot read control-plane state: '{branch or 'HEAD'}' has no tracked upstream "
            f"although remote(s) {', '.join(remotes)} are configured. A read cannot prove "
            "freshness against an unknown upstream."
        )

    remote_name = upstream.split("/", 1)[0]
    try:
        _git(repo_root, ["fetch", "--quiet", remote_name])
    except ControlPlaneError as exc:
        raise ControlPlaneError(
            f"Cannot read control-plane state: coordination remote '{remote_name}' could not be "
            f"fetched, so freshness is unproven. {exc}"
        ) from exc

    local = _git(repo_root, ["rev-parse", branch])
    remote = _git(repo_root, ["rev-parse", upstream])
    if local == remote:
        return ReadSource(repo_root, remote, remote)
    base = _git(repo_root, ["merge-base", branch, upstream], check=False)
    if base == local:
        # Behind upstream: serve the fetched state without moving anything local.
        return ReadSource(repo_root, remote, remote)
    if base == remote:
        raise ControlPlaneError(
            f"Cannot read control-plane state: '{branch}' has unpublished local commits ahead of "
            f"{upstream}. Push them so the shared surface is authoritative, then read again."
        )
    raise ControlPlaneError(
        f"Cannot read control-plane state: '{branch}' and {upstream} have diverged. "
        "Reconcile the coordination repository before reading."
    )


RAIL_STATUSES = ("ready", "running", "blocked", "completed")
_RAIL_HEADER_KEYS = {"status", "role", "depends on", "shared resource"}


class RailState:
    """Deterministic facts about one rail. Recommendations are not facts."""

    def __init__(
        self,
        identifier: str,
        status: str,
        artifacts: list[str],
        depends_on: list[str],
        shared_resource: str | None,
        proposed_status: str | None = None,
        role: str | None = None,
    ) -> None:
        self.identifier = identifier
        self.status = status
        self.artifacts = artifacts
        self.depends_on = depends_on
        self.shared_resource = shared_resource
        # Descriptive assignment metadata, not a controller-managed session role.
        # A rail may name `evidence-worker`, or name nothing at all; deciding what
        # that permits belongs to `authorization`, not to this reader.
        self.role = role
        # What the executor's handoff claims. Evidence, never acceptance.
        self.proposed_status = proposed_status

    @property
    def unreconciled(self) -> bool:
        return self.proposed_status is not None and self.proposed_status != self.status


def _parse_rail_header(
    text: str, *, rail_id: str
) -> tuple[str, list[str], str | None, str | None]:
    """Read the small current-state header the rail files already carry."""
    header: dict[str, str] = {}
    role_headers = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower().lstrip("#").strip()
        if key in _RAIL_HEADER_KEYS:
            if key == "role":
                role_headers += 1
            header[key] = value.strip()

    # Role became authorization-sensitive, so "last one wins" is no longer a safe
    # way to read two of them. This is prospective integrity: no published rail
    # carries a second `Role:`, and one appearing now would be ambiguous about
    # which assignment a controller action was authorized against.
    if role_headers > 1:
        raise ControlPlaneError(
            f"Rail '{rail_id}' declares {role_headers} 'Role:' headers; exactly one "
            "assignment must be unambiguous."
        )

    status = header.get("status", "").lower()
    if status not in RAIL_STATUSES:
        raise ControlPlaneError(
            f"Rail '{rail_id}' has status '{header.get('status', '')or 'missing'}'; "
            f"expected one of {', '.join(RAIL_STATUSES)}."
        )

    raw_depends = header.get("depends on", "").strip()
    depends_on: list[str] = []
    if raw_depends and raw_depends.lower() != "none":
        for entry in raw_depends.split(","):
            candidate = entry.strip().strip("`")
            if not candidate:
                continue
            dependency = validate_identifier(candidate, label="rail dependency")
            if dependency == rail_id:
                raise ControlPlaneError(f"Rail '{rail_id}' declares a dependency on itself.")
            depends_on.append(dependency)

    raw_resource = header.get("shared resource", "").strip()
    resource = None
    if raw_resource and raw_resource.lower() != "none":
        resource = validate_identifier(raw_resource.strip("`"), label="shared resource")

    # Optional on purpose. Most published rails predate this header, and some name
    # a role no controller manages; refusing either would make whole scopes
    # unreadable to every reader, which is a much worse failure than an
    # unenforced field. The shape is still checked so the token that reaches an
    # authorization decision is a normalized identifier and nothing else.
    raw_role = header.get("role", "").strip().strip("`")
    role = None
    if raw_role and raw_role.lower() != "none":
        role = validate_identifier(raw_role.lower(), label="rail role")
    return status, depends_on, resource, role


def _parse_handoff_status(text: str | None) -> str | None:
    """Read the status a handoff proposes, without letting it break the read.

    A handoff is executor-authored evidence. Malformed or absent status is
    reported as unrecognized rather than raising, so one executor cannot block
    the orchestrator from reading every other rail.
    """
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        key, separator, value = stripped.partition(":")
        if separator and key.strip().lower() == "status":
            candidate = value.strip().lower()
            return candidate if candidate in RAIL_STATUSES else "unrecognized"
    return None


def collect_rail_states(source: ReadSource, *, project: str, ticket: str) -> list[RailState]:
    """Surface every rail's current status, dependencies, and shared resource.

    This reports facts only. Whether to continue, launch, or hold a rail is
    orchestrator judgment and is deliberately not computed here.
    """
    scope = scope_relative(project, ticket)
    states: list[RailState] = []
    for rail_id in source.rails(scope):
        validate_identifier(rail_id, label="rail identifier")

        def relative(artifact: str, rail: str = rail_id) -> str:
            return artifact_relative(project=project, ticket=ticket, artifact=artifact, rail=rail)

        authorization = source.read(relative("rail"))
        if authorization is None:
            raise ControlPlaneError(
                f"Rail '{rail_id}' has no orchestrator authorization. An executor cannot "
                "create a rail; publish the authorization or remove the directory."
            )
        status, depends_on, resource, role = _parse_rail_header(authorization, rail_id=rail_id)
        artifacts = [name for name in LISTED_RAIL_ARTIFACTS if source.exists(relative(name))]
        proposed = _parse_handoff_status(source.read(relative("handoff")))
        states.append(
            RailState(rail_id, status, artifacts, depends_on, resource, proposed, role)
        )

    known = {state.identifier for state in states}
    for state in states:
        unknown = [name for name in state.depends_on if name not in known]
        if unknown:
            raise ControlPlaneError(
                f"Rail '{state.identifier}' depends on unknown rail(s): {', '.join(unknown)}."
            )
    _reject_dependency_cycles(states)
    return states


def _reject_dependency_cycles(states: list[RailState]) -> None:
    dependencies = {state.identifier: state.depends_on for state in states}
    visiting: set[str] = set()
    settled: set[str] = set()

    def walk(identifier: str, trail: list[str]) -> None:
        if identifier in settled:
            return
        if identifier in visiting:
            cycle = " -> ".join(trail + [identifier])
            raise ControlPlaneError(f"Rail dependencies are contradictory: {cycle}.")
        visiting.add(identifier)
        for dependency in dependencies.get(identifier, []):
            walk(dependency, trail + [identifier])
        visiting.discard(identifier)
        settled.add(identifier)

    for state in states:
        walk(state.identifier, [])


def _render_rail_index(states: list[RailState]) -> list[str]:
    by_identifier = {state.identifier: state for state in states}
    lines: list[str] = []
    for state in states:
        artifacts = ", ".join(state.artifacts) if state.artifacts else "none"
        lines.append(f"- {state.identifier}: {state.status}; artifacts: {artifacts}")
        if state.unreconciled:
            lines.append(
                f"    UNRECONCILED: rail authorizes '{state.status}' but the handoff proposes "
                f"'{state.proposed_status}'; the orchestrator must reconcile before relying on this status"
            )
        if state.depends_on:
            rendered = ", ".join(
                f"{name} ({by_identifier[name].status})" for name in state.depends_on
            )
            satisfied = all(by_identifier[name].status == "completed" for name in state.depends_on)
            lines.append(f"    depends on: {rendered}")
            lines.append(f"    dependencies satisfied: {'yes' if satisfied else 'no'}")
        if state.shared_resource is not None:
            contenders = [
                other.identifier
                for other in states
                if other.identifier != state.identifier
                and other.shared_resource == state.shared_resource
                and other.status == "running"
            ]
            lines.append(f"    shared resource: {state.shared_resource}")
            if contenders:
                lines.append(f"    resource in use by: {', '.join(contenders)}")
    return lines


def render_status(repo_root: Path, *, project: str, ticket: str) -> str:
    """Bounded orchestrator read: accepted state plus the rails that exist."""
    source = resolve_read_source(repo_root)
    scope = scope_relative(project, ticket)
    lines = [
        "# Control Plane State",
        "",
        f"scope: {scope}",
        f"head: {source.head or 'empty history'}",
        "",
        "## Accepted State",
        "",
    ]
    state = source.read(artifact_relative(project=project, ticket=ticket, artifact="state", rail=None))
    lines.append(state.rstrip("\n") if state else "no accepted state published")
    lines.extend(["", "## Rails Present", ""])
    states = collect_rail_states(source, project=project, ticket=ticket)
    unreconciled = [state.identifier for state in states if state.unreconciled]
    lines.insert(4, f"unreconciled rails: {len(unreconciled)}" + (
        f" ({', '.join(unreconciled)})" if unreconciled else ""
    ))
    if not states:
        lines.append("none")
    else:
        lines.extend(_render_rail_index(states))
    return "\n".join(lines)


def render_rail(repo_root: Path, *, project: str, ticket: str, rail: str) -> str:
    """Bounded executor read: one rail only, never siblings or accepted state."""
    source = resolve_read_source(repo_root)
    rail_id = validate_identifier(rail, label="rail identifier")

    def relative(artifact: str) -> str:
        return artifact_relative(project=project, ticket=ticket, artifact=artifact, rail=rail_id)

    authorization = source.read(relative("rail"))
    if authorization is None:
        raise ControlPlaneError(f"No authorized rail '{rail_id}' in {scope_relative(project, ticket)}.")
    handoff = source.read(relative("handoff"))
    lines = [
        "# Control Plane Rail",
        "",
        f"scope: {scope_relative(project, ticket)}",
        f"rail: {rail_id}",
        f"head: {source.head or 'empty history'}",
        f"provider evidence: {'present' if source.exists(relative('evidence')) else 'absent'}",
        "",
        "## Authorization",
        "",
        authorization.rstrip("\n"),
        "",
        "## Current Handoff",
        "",
        handoff.rstrip("\n") if handoff else "no handoff published",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_dev_flow.control_plane",
        description="Read and publish current collaboration state in an explicitly supplied coordination repository.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scope(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo", help="Coordination repository path; defaults to the configured control plane.")
        subparser.add_argument("--project", help="Stable project identifier; defaults to the configured control plane.")
        subparser.add_argument("--ticket", help="Stable ticket identifier; defaults to the configured control plane.")

    status_parser = subparsers.add_parser("status", help="Print accepted state and the rail index.")
    add_scope(status_parser)

    rail_parser = subparsers.add_parser("rail", help="Print one rail's authorization and current handoff.")
    add_scope(rail_parser)
    rail_parser.add_argument("--rail", required=True, help="Stable semantic rail identifier.")

    publish_parser = subparsers.add_parser("publish", help="Replace one owned artifact with current state.")
    add_scope(publish_parser)
    publish_parser.add_argument("--artifact", required=True, choices=sorted(ARTIFACT_FILENAMES))
    publish_parser.add_argument("--role", required=True, choices=sorted(set(ARTIFACT_OWNERS.values())))
    publish_parser.add_argument("--file", required=True, help="File holding the current content to publish.")
    publish_parser.add_argument("--rail", help="Stable semantic rail identifier for rail-scoped artifacts.")
    publish_parser.add_argument("--expected-head", help="Head the caller last read; publication fails closed if stale.")

    accept_parser = subparsers.add_parser(
        "accept",
        help="Publish the ticket's current progress: the accepted checkpoint, the completed "
             "named checkpoint, and the reconsidered estimate.",
    )
    add_scope(accept_parser)
    accept_parser.add_argument(
        "--state-file",
        dest="state_file",
        required=True,
        help="File holding the accepted state to publish; it lands in the same commit as the record.",
    )
    accept_parser.add_argument("--checkpoint", type=int, help="Numeric Flow checkpoint being accepted.")
    accept_parser.add_argument("--commit", help="Full object name of the product commit being accepted.")
    accept_parser.add_argument("--named", type=int, help="Named ticket checkpoint being completed.")
    accept_parser.add_argument("--named-total", type=int, dest="named_total", help="Named roadmap size at that completion.")
    accept_parser.add_argument("--remaining", type=int, required=True, help="Numeric checkpoints the orchestrator now projects as remaining.")
    accept_parser.add_argument("--confidence", required=True, choices=list(CONFIDENCES), help="Projection confidence.")
    accept_parser.add_argument("--note", default="", help="One bounded line saying why the estimate stands where it does.")
    accept_parser.add_argument("--product-repo", dest="product_repo", help="Product repository the accepted commit is read from; defaults to the current one.")

    proceed_parser = subparsers.add_parser(
        "proceed", help="Allocate the next handoff indicator after durable publication."
    )
    add_scope(proceed_parser)

    config_parser = subparsers.add_parser("config", help="Report whether a control plane is configured here.")
    config_parser.add_argument("--repo", help=argparse.SUPPRESS)
    config_parser.add_argument("--project", help=argparse.SUPPRESS)
    config_parser.add_argument("--ticket", help=argparse.SUPPRESS)
    return parser


def _resolve_scope(arguments: argparse.Namespace) -> tuple[Path, str, str]:
    """Prefer explicit arguments; otherwise discover the configured control plane."""
    repo, project, ticket = arguments.repo, arguments.project, arguments.ticket
    if not (repo and project and ticket):
        try:
            product_root = resolve_repo_root()
        except RepositoryError as exc:
            raise ControlPlaneError(
                "No control-plane scope was supplied and the current directory is not "
                f"inside a repository. {exc}"
            ) from exc
        configured = resolve_control_plane_config(product_root)
        if configured is None:
            raise ControlPlaneError(
                f"No control plane is configured in {config_file_for_repo_root(product_root)}. "
                "Use the repository-local tasking rail, or supply --repo, --project, and --ticket."
            )
        repo = repo or str(configured.repository)
        project = project or configured.project
        ticket = ticket or configured.ticket
    return resolve_coordination_repo(Path(repo).expanduser()), project, ticket


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if arguments.command == "config":
        product_root = resolve_repo_root()
        configured = resolve_control_plane_config(product_root)
        if configured is None:
            print("control plane: not configured")
            print(f"rail: {product_root / '.ai-dev' / 'tasking.md'}")
            return 0
        print("control plane: configured")
        print(f"repository: {configured.repository}")
        print(f"project: {configured.project}")
        print(f"ticket: {configured.ticket}")
        return 0

    repo_root, project, ticket = _resolve_scope(arguments)

    if arguments.command == "status":
        print(render_status(repo_root, project=project, ticket=ticket))
        return 0
    if arguments.command == "rail":
        print(render_rail(repo_root, project=project, ticket=ticket, rail=arguments.rail))
        return 0
    if arguments.command == "accept":
        state_source = Path(arguments.state_file).expanduser()
        try:
            state_content = state_source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ControlPlaneError(f"Cannot read {state_source}: {exc}") from exc
        target, head, document = accept_progress(
            repo_root,
            project=project,
            ticket=ticket,
            state=state_content,
            remaining=arguments.remaining,
            confidence=arguments.confidence,
            note=arguments.note,
            checkpoint=arguments.checkpoint,
            commit=arguments.commit,
            named=arguments.named,
            named_total=arguments.named_total,
            product_repo=Path(arguments.product_repo) if arguments.product_repo else None,
        )
        accepted = document["accepted"]
        named = document["named"]
        print(f"published: {target.relative_to(repo_root).as_posix()}")
        print(
            "accepted checkpoint: "
            + ("none" if accepted is None else f"{accepted['checkpoint']} at {accepted['commit']}")
        )
        print(
            "named checkpoint: "
            + ("none" if named is None else f"{named['checkpoint']} of {named['total']}")
        )
        print(
            "projected remaining: {0} ({1} confidence)".format(
                document["projection"]["remaining"], document["projection"]["confidence"]
            )
        )
        print(f"head: {head or 'empty history'}")
        return 0
    if arguments.command == "proceed":
        # Allocation must already have succeeded before anything is printed.
        allocated = allocate_proceed_number(repo_root, project=project, ticket=ticket)
        print(f"proceed {allocated}")
        return 0

    source = Path(arguments.file).expanduser()
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ControlPlaneError(f"Cannot read {source}: {exc}") from exc
    target, head = publish(
        repo_root,
        project=project,
        ticket=ticket,
        artifact=arguments.artifact,
        role=arguments.role,
        content=content,
        rail=arguments.rail,
        expected_head=arguments.expected_head,
    )
    print(f"published: {target.relative_to(repo_root).as_posix()}")
    print(f"head: {head or 'empty history'}")
    return 0


def run() -> None:
    try:
        status = main()
    except (ControlPlaneError, RepositoryError) as exc:
        print(f"control-plane: {exc}", file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    run()
