from __future__ import annotations

import binascii
import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .repository import (
    blocked_workflows_file_for_repo_root,
    BranchComparison,
    RepositoryError,
    clean_untracked_non_ignored,
    checkout_branch,
    branch_is_ancestor,
    branch_exists,
    create_or_reset_branch_from_source,
    create_commit,
    compare_main_and_scratch,
    commit_count_between,
    create_managed_ref,
    compare_main_to_tracked_upstream,
    current_branch_name,
    ensure_branches_point_to_same_commit,
    ensure_local_state_excluded,
    ensure_no_active_git_operations,
    git_status_short,
    git_status_short_filtered,
    hard_reset_branch_to_revision,
    resolve_repo_root,
    resolve_commit_hash,
    resolve_commit_ish,
    revisions_share_history,
    RevisionResolutionError,
    resolve_short_commit_hash,
    resolve_tree_hash,
    restore_branch_to_revision,
    clear_diff_baseline_for_repo_root,
    max_numbered_checkpoint_relative_to_main,
    fetch_tracked_upstream,
    push_main_to_tracked_upstream,
    resolve_tracked_upstream,
    squash_merge_branch_into_current,
    stage_all_changes,
    sync_local_excludes,
    TrackedUpstream,
    workflow_state_file_for_repo_root,
    diff_baseline_file_for_repo_root,
    delete_managed_ref,
    resolve_managed_ref,
)
from .blocked_workflows import (
    BlockedWorkflowRecord,
    BlockedWorkflowsError,
    format_blocked_summary_lines,
    get_blocked_workflow,
    load_blocked_workflows,
    remove_blocked_workflow,
    save_blocked_workflows,
    upsert_blocked_workflow,
)
from .workflow_state import (
    StackedHandoff,
    WorkflowState,
    WorkflowStateError,
    clear_state,
    load_state,
    normalize_and_validate,
    save_state,
)
from .ticket_config import TicketConfigError, load_ticket_configuration_for_repo_root
from .ticket_providers import (
    TicketProvider,
    TicketProviderError,
    instantiate_ticket_provider,
    resolve_ticket_provider_for_reference,
)
from .json_files import JsonFileError, load_json_object, write_json_object_atomic
from .repository import config_file_for_repo_root
from .promotion_sync import (
    PromotionSyncError,
    PromotionSyncRecord,
    load_promotion_sync_record,
    promotion_sync_record_matches_state,
    clear_promotion_sync_record,
    save_promotion_sync_record,
)
from .copilot_report import render_latest_copilot_report
from .tickets import TicketReference


_DIRECT_FLOW_ROUTE_TOKEN = "__ai_dev_flow_exec__"
_FLOW_DIFF_BASELINE_INVALID = "Review baseline is stale or invalid; run flow-diff --refresh."
_PROMOTION_REVIEW_RECORD_PATH = ".ai-dev/promotion-review.json"
_SUSPENDED_REF_PREFIX = "refs/ai-dev/suspended/"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    canonical_namespace: str
    order: int
    handler_key: str
    compatibility_top_level: bool = False
    help_visible: bool = True
    alias_eligible: bool = True
    lifecycle_command: bool = True
    fixed_prefixed_executable: bool = False


@dataclass(frozen=True)
class _InvocationUsageContext:
    invocation_name: str
    command: str
    direct_executable_mode: bool


_ACTIVE_INVOCATION_USAGE_CONTEXT: _InvocationUsageContext | None = None


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="start",
        description="Begin new work on an unblocked issue and reset scratch from main.",
        canonical_namespace="flow",
        order=10,
        handler_key="start",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="patch",
        description="Begin or adopt a local patch workflow on scratch.",
        canonical_namespace="flow",
        order=20,
        handler_key="patch",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="status",
        description="Show the active issue and current repository state.",
        canonical_namespace="flow",
        order=30,
        handler_key="status",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="report",
        description="Render the latest completed read-only Copilot work report.",
        canonical_namespace="flow",
        order=35,
        handler_key="report",
        lifecycle_command=False,
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="diff",
        description="Show read-only workflow diffs without modifying repository state.",
        canonical_namespace="flow",
        order=40,
        handler_key="diff",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="commit",
        description="Create the next numbered checkpoint on scratch.",
        canonical_namespace="flow",
        order=60,
        handler_key="commit",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="reset",
        description="Discard scratch work and restore it from main.",
        canonical_namespace="flow",
        order=70,
        handler_key="reset",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="abandon",
        description="Stop tracking the active workflow locally without changing the ticket.",
        canonical_namespace="flow",
        order=75,
        handler_key="abandon",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="promote",
        description="Squash scratch into one permanent commit on main.",
        canonical_namespace="flow",
        order=80,
        handler_key="promote",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="complete",
        description="Clear the completed local workflow.",
        canonical_namespace="flow",
        order=90,
        handler_key="complete",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="block",
        description="Block the active issue workflow and release the active slot.",
        canonical_namespace="flow",
        order=100,
        handler_key="block",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="resume",
        description="Reactivate a previously blocked issue workflow.",
        canonical_namespace="flow",
        order=110,
        handler_key="resume",
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="ticket-create",
        description="Create a new ticket using the configured ticket provider.",
        canonical_namespace="flow",
        order=120,
        handler_key="ticket-create",
        lifecycle_command=False,
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="ticket-show",
        description="Show one ticket by ID from the configured ticket provider.",
        canonical_namespace="flow",
        order=130,
        handler_key="ticket-show",
        lifecycle_command=False,
        fixed_prefixed_executable=True,
    ),
    CommandSpec(
        name="ticket-query",
        description="Query tickets from the configured ticket provider.",
        canonical_namespace="flow",
        order=140,
        handler_key="ticket-query",
        lifecycle_command=False,
        fixed_prefixed_executable=True,
    ),
)

COMMAND_SPEC_BY_NAME: dict[str, CommandSpec] = {
    spec.name: spec for spec in COMMAND_SPECS
}

FLOW_LIFECYCLE_COMMANDS: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
    if spec.canonical_namespace == "flow" and spec.help_visible and spec.lifecycle_command
)

FIXED_FLOW_EXECUTABLE_COMMANDS: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
    if spec.fixed_prefixed_executable
)

COMMAND_HELP: dict[str, str] = {
    "start": """\
Usage: {command_name} start <issue-number>
    {command_name} start <issue-number> --prerequisite-for <active-issue>

Begin independent work on an unblocked issue from main. The prerequisite form
is only for active issue A handing off to prerequisite B; it keeps A's current
scratch tree, starts B at checkpoint 0, and preserves A's checkpoint ownership.

Options:
  -h, --help  Show this help.
""",
    "patch": """\
Usage: {command_name} patch "<description>"
       {command_name} patch --adopt "<description>"

Start a local patch workflow for small, self-contained changes, or adopt
existing scratch work without changing commits, index, or working tree.

Options:
  --adopt      Adopt existing work on scratch and preserve repository state.
  -h, --help   Show this help.
""",
    "status": """\
Usage: {command_name} status [-v|--verbose]

Show the active issue, current branch, and repository deviations.

Options:
  -v, --verbose  Show complete workflow and Git details.
  -h, --help     Show this help.
""",
        "report": """\
Usage: {command_name} report

Render the latest completed repository-scoped Copilot work turn without
modifying Flow state, logs, settings, approvals, or repository content.

Options:
    -h, --help  Show this help.
""",
    "diff": """\
Usage: {command_name} [--git|--all]
       {command_name} --refresh

Show read-only diff output for the active workflow without modifying index,
working tree, workflow state, or checkpoint state.
Normal diff is review-baseline-relative when a valid baseline exists.
Without a review baseline, normal diff shows the current full uncommitted diff.

Options:
    --refresh  Set the review baseline.
    --git      Show all current uncommitted changes, ignoring review baseline.
    --all      Show whole active workflow since main, ignoring review baseline.
    -h, --help Show this help.
""",
    "commit": """\
Usage: {command_name} commit

Create the next numbered checkpoint commit on scratch.

Options:
  -h, --help  Show this help.
""",
    "reset": """\
Usage: {command_name} reset

Discard scratch commits and working-tree changes by resetting scratch to
main while preserving the active issue.

Options:
  -h, --help  Show this help.
""",
        "abandon": """\
Usage: {command_name} abandon

Stop tracking the active workflow locally without changing the bound ticket or
repository content. Requires a clean synchronized scratch branch.

Options:
    -h, --help  Show this help.
""",
    "promote": """\
Usage: {command_name} promote "<commit-message>"

Squash the complete scratch change into one permanent commit on main, then
reset scratch to the promoted main commit. For stacked work, this publishes
the complete physical A+B tree, including A's partial work.

Options:
  -h, --help  Show this help.
""",
    "complete": """\
Usage: {command_name} complete

Clear the active local workflow after scratch and main are synchronized.
Completing a prerequisite closes only that issue; a suspended original remains
blocked with its resume metadata.

Options:
  -h, --help  Show this help.
""",
    "block": """\
Usage: {command_name} block "<reason>"

Block an active issue workflow, keep the issue open, and release the
local active workflow slot.

Options:
  -h, --help  Show this help.
""",
    "resume": """\
Usage: {command_name} resume <ticket-number>

Reactivate a blocked issue workflow as the local active issue. Resuming a
suspended original restores its historical checkpoint progression and starts a
new active scope from the promoted canonical base.

Options:
  -h, --help  Show this help.
""",
        "ticket-create": """\
    Usage: {command_name} "<title>" [--body "<text>"] [--acceptance "<criterion>"]... [--label "<label>"]...

Create a ticket using the configured ticket provider.

Options:
    --body <text>           Set ticket body text.
    --acceptance <text>     Add one acceptance criterion. Repeat to add more.
    --label <name>          Add one label. Repeat to add more.
    -h, --help              Show this help.
""",
        "ticket-show": """\
    Usage: {command_name} <ticket-id>

Show one ticket by identifier using the configured ticket provider.

Options:
    -h, --help  Show this help.
""",
        "ticket-query": """\
    Usage: {command_name} [--lifecycle <open|closed>] [--workflow <inactive|active|blocked>] [--label "<label>"]... [--query "<text>"]

Query tickets using the configured ticket provider.

Options:
    --lifecycle <state>     Filter by lifecycle state.
    --workflow <state>      Filter by workflow state.
    --label <name>          Require one label. Repeat to require more labels.
    --query <text>          Match text in ticket title, body, or acceptance criteria.
    -h, --help              Show this help.
""",
}


class FlowError(Exception):
    """A user-facing flow command error."""


def resolve_command_name() -> str:
    configured_name = os.environ.get("FLOW_COMMAND_NAME", "").strip()
    if configured_name:
        return configured_name

    invoked_name = Path(sys.argv[0]).name
    if invoked_name.lower().endswith(".py"):
        return "flow"

    return invoked_name or "flow"


def _usage_invocation_prefix(invocation_name: str, command: str) -> str:
    active = _ACTIVE_INVOCATION_USAGE_CONTEXT
    if (
        active is not None
        and active.invocation_name == invocation_name
        and active.command == command
    ):
        if active.direct_executable_mode:
            return active.invocation_name
        return f"{active.invocation_name} {active.command}"

    return f"{invocation_name} {command}"


def _usage_error(invocation_name: str, command: str, tail: str = "") -> FlowError:
    prefix = _usage_invocation_prefix(invocation_name, command)
    if tail:
        return FlowError(f"Usage: {prefix} {tail}")
    return FlowError(f"Usage: {prefix}")


def _render_command_help(
    *,
    command_name: str,
    command: str,
    direct_executable_mode: bool,
) -> str:
    template = COMMAND_HELP.get(command)
    if template is None:
        raise FlowError(f"Unknown command help target: {command}")

    rendered = template.format(command_name=command_name)
    if not direct_executable_mode:
        return rendered

    lines = rendered.splitlines()
    rewritten: list[str] = []
    expected = f"{command_name} {command}"
    for line in lines:
        prefix = ""
        usage_payload = line
        if line.startswith("Usage: "):
            prefix = "Usage: "
            usage_payload = line[len("Usage: ") :]

        stripped_payload = usage_payload.lstrip()
        leading_whitespace = usage_payload[: len(usage_payload) - len(stripped_payload)]
        if stripped_payload.startswith(expected):
            direct_prefix = _usage_invocation_prefix(command_name, command)
            stripped_payload = direct_prefix + stripped_payload[len(expected) :]
        usage_payload = f"{leading_whitespace}{stripped_payload}"
        rewritten.append(f"{prefix}{usage_payload}")

    rewritten_text = "\n".join(rewritten)
    if rendered.endswith("\n"):
        rewritten_text += "\n"
    return rewritten_text


def print_command_help(
    command_name: str,
    command: str,
    *,
    direct_executable_mode: bool = False,
) -> None:
    rendered = _render_command_help(
        command_name=command_name,
        command=command,
        direct_executable_mode=direct_executable_mode,
    )
    print(rendered, end="")


@contextmanager
def _with_invocation_usage_context(
    invocation_name: str,
    command: str,
    *,
    direct_executable_mode: bool,
):
    global _ACTIVE_INVOCATION_USAGE_CONTEXT
    previous = _ACTIVE_INVOCATION_USAGE_CONTEXT
    _ACTIVE_INVOCATION_USAGE_CONTEXT = _InvocationUsageContext(
        invocation_name=invocation_name,
        command=command,
        direct_executable_mode=direct_executable_mode,
    )
    try:
        yield
    finally:
        _ACTIVE_INVOCATION_USAGE_CONTEXT = previous


def print_unknown_command(command_name: str, command: str) -> None:
    print(f"{command_name}: unknown command: {command}", file=sys.stderr)
    print(
        "Run one of the direct executables (for example flow-status --help) for usage.",
        file=sys.stderr,
    )


def resolve_repo_root_if_available() -> Path | None:
    try:
        return resolve_repo_root()
    except RepositoryError:
        return None


def _resolve_repo_state_context() -> tuple[Path, Path, WorkflowState]:
    repo_root = resolve_repo_root()
    state_path = workflow_state_file_for_repo_root(repo_root)
    state = load_state(state_path)
    if state.stacked_handoff is not None:
        _validate_stacked_scope(repo_root, state)
    return repo_root, state_path, state


def _validate_stacked_scope(repo_root: Path, state: WorkflowState) -> None:
    handoff = state.stacked_handoff
    if handoff is None:
        return

    try:
        suspended_ref = resolve_managed_ref(repo_root, handoff.suspended_ref_name)
        if suspended_ref != handoff.suspended_commit:
            raise FlowError(
                "Invalid stacked workflow state: suspended ref does not match its persisted commit."
            )
        if handoff.inherited_base_commit != handoff.suspended_commit:
            raise FlowError(
                "Invalid stacked workflow state: inherited base does not match suspended commit."
            )
        if resolve_tree_hash(repo_root, handoff.inherited_base_commit) != handoff.inherited_base_tree:
            raise FlowError(
                "Invalid stacked workflow state: inherited base tree does not match its commit."
            )
        _ensure_main_and_scratch_branches_exist(repo_root, state)
        if not branch_is_ancestor(
            repo_root,
            ancestor_revision=handoff.inherited_base_commit,
            descendant_revision=state.scratch_branch,
        ) and resolve_commit_hash(repo_root, state.main_branch) != resolve_commit_hash(repo_root, state.scratch_branch):
            raise FlowError(
                "Invalid stacked workflow state: inherited base is not an ancestor of scratch."
            )
    except FlowError:
        raise
    except RepositoryError as exc:
        raise FlowError(
            f"Invalid stacked workflow state: inherited base or ref is unreachable. {exc}"
        ) from exc


def _validate_stacked_resume(repo_root: Path, state: WorkflowState) -> None:
    resume = state.stacked_resume
    if resume is None:
        return
    try:
        promoted_commit = resume["promotedMainCommit"]
        if resolve_commit_hash(repo_root, state.main_branch) != promoted_commit:
            raise FlowError("Invalid stacked resume state: main is not the recorded promoted commit.")
        if resolve_commit_hash(repo_root, state.scratch_branch) != promoted_commit:
            raise FlowError("Invalid stacked resume state: scratch is not the recorded promoted commit.")
        if resolve_managed_ref(repo_root, resume["suspendedRefName"]) != resume["suspendedCommit"]:
            raise FlowError("Invalid stacked resume state: suspended ref does not match its checkpoint.")
    except FlowError:
        raise
    except (KeyError, RepositoryError) as exc:
        raise FlowError(f"Invalid stacked resume state: {exc}") from exc


def _promotion_review_record_path(repo_root: Path) -> Path:
    return repo_root / ".ai-dev" / "promotion-review.json"


def _clear_promotion_review_record(repo_root: Path) -> None:
    record_path = _promotion_review_record_path(repo_root)
    try:
        record_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FlowError(f"Cannot clear promotion review state at {record_path}: {exc}") from exc


def _promotion_gate_enabled(repo_root: Path) -> bool:
    config_path = config_file_for_repo_root(repo_root)
    if not config_path.exists():
        return True

    try:
        payload = load_json_object(config_path, missing_default={})
    except JsonFileError:
        return True

    review_block = payload.get("review")
    if not isinstance(review_block, dict):
        return True

    value = review_block.get("promotionGate")
    if value is False:
        return False
    return True


def _promotion_review_record_matches_state(
    record: dict[str, object],
    state: WorkflowState,
    current_scratch_commit: str,
) -> bool:
    if record.get("version") != 1:
        return False
    if record.get("result") != "pass":
        return False
    if record.get("scratchCommit") != current_scratch_commit:
        return False
    if record.get("mainBranch") != state.main_branch:
        return False
    if record.get("scratchBranch") != state.scratch_branch:
        return False

    if state.active_issue_number is not None:
        if record.get("activeIssueNumber") != state.active_issue_number:
            return False
    elif state.patch_description is not None:
        if record.get("patchDescription") != state.patch_description:
            return False

    return True


def _require_valid_promotion_review_gate(repo_root: Path, state: WorkflowState) -> None:
    if not _promotion_gate_enabled(repo_root):
        return

    record_path = _promotion_review_record_path(repo_root)
    if not record_path.exists():
        raise FlowError(
            "Cannot promote workflow: promotion review gate requires a pass record at "
            f"{_PROMOTION_REVIEW_RECORD_PATH}."
        )

    try:
        payload = load_json_object(record_path, missing_default={})
    except JsonFileError as exc:
        raise FlowError(
            f"Cannot promote workflow: invalid promotion review gate record at {_PROMOTION_REVIEW_RECORD_PATH}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise FlowError(
            f"Cannot promote workflow: invalid promotion review gate record at {_PROMOTION_REVIEW_RECORD_PATH}."
        )

    current_scratch_commit = resolve_commit_hash(repo_root, state.scratch_branch)
    if not _promotion_review_record_matches_state(payload, state, current_scratch_commit):
        raise FlowError(
            "Cannot promote workflow: promotion review gate requires a current pass record "
            "for this workflow and scratch commit."
        )


def _require_tracked_upstream_preflight(repo_root: Path, state: WorkflowState):
    try:
        upstream = resolve_tracked_upstream(repo_root, branch_name=state.main_branch)
    except RepositoryError as exc:
        raise FlowError(
            f"Cannot promote workflow: cannot resolve tracked upstream for {state.main_branch}. {exc}"
        ) from exc
    if upstream is None:
        return None

    try:
        fetch_tracked_upstream(repo_root, upstream=upstream)
    except RepositoryError as exc:
        raise FlowError(
            "Cannot promote workflow: cannot fetch tracked upstream "
            f"{upstream.remote_name} before promotion. {exc}"
        ) from exc

    try:
        upstream_comparison = compare_main_to_tracked_upstream(
            repo_root,
            main_branch=state.main_branch,
            upstream=upstream,
        )
    except RepositoryError as exc:
        raise FlowError(
            "Cannot promote workflow: cannot resolve tracked upstream "
            f"{upstream.remote_name} after fetch. {exc}"
        ) from exc

    if upstream_comparison.relationship == "upstream-ahead":
        raise FlowError(
            "Cannot promote workflow: tracked upstream "
            f"{upstream_comparison.upstream_ref} is ahead of local {state.main_branch}. "
            "Reconcile local main before promotion."
        )
    if upstream_comparison.relationship == "diverged":
        raise FlowError(
            "Cannot promote workflow: tracked upstream "
            f"{upstream_comparison.upstream_ref} has diverged from local {state.main_branch}. "
            "Reconcile local main before promotion."
        )
    return upstream


def _pending_sync_retry_context(repo_root: Path, state: WorkflowState):
    try:
        record = load_promotion_sync_record(repo_root)
    except PromotionSyncError:
        return None
    if record is None or record.status != "pending":
        return None

    try:
        main_commit = resolve_commit_hash(repo_root, state.main_branch)
        scratch_commit = resolve_commit_hash(repo_root, state.scratch_branch)
        upstream = resolve_tracked_upstream(repo_root, branch_name=state.main_branch)
    except RepositoryError:
        return None
    if upstream is None or main_commit != scratch_commit:
        return None
    if not promotion_sync_record_matches_state(
        record,
        state,
        promoted_main_commit=main_commit,
    ):
        return None
    if record.remote_name != upstream.remote_name or record.upstream_ref != upstream.merge_ref:
        return None
    return record, upstream


def _require_synchronized_completion_state(repo_root: Path, state: WorkflowState) -> None:
    try:
        upstream = resolve_tracked_upstream(repo_root, branch_name=state.main_branch)
    except RepositoryError as exc:
        raise FlowError(
            f"Cannot complete workflow: cannot resolve tracked upstream for {state.main_branch}. {exc}"
        ) from exc
    if upstream is None:
        return

    try:
        record = load_promotion_sync_record(repo_root)
        main_commit = resolve_commit_hash(repo_root, state.main_branch)
        scratch_commit = resolve_commit_hash(repo_root, state.scratch_branch)
    except (PromotionSyncError, RepositoryError):
        record = None
        main_commit = ""
        scratch_commit = ""

    if (
        record is None
        or record.status != "synchronized"
        or main_commit != scratch_commit
        or not promotion_sync_record_matches_state(
            record,
            state,
            promoted_main_commit=main_commit,
        )
        or record.remote_name != upstream.remote_name
        or record.upstream_ref != upstream.merge_ref
    ):
        raise FlowError(
            "Cannot complete workflow: promotion to the tracked upstream has not been "
            "synchronized. Run flow-promote to retry remote synchronization."
        )


def _save_synchronized_promotion_record(repo_root: Path, record: PromotionSyncRecord) -> None:
    synchronized = replace(record, status="synchronized")
    try:
        save_promotion_sync_record(repo_root, synchronized)
    except PromotionSyncError as exc:
        raise FlowError(
            "Remote synchronization succeeded but its local state could not be recorded. "
            "Rerun flow-promote to verify synchronization. "
            f"{exc}"
        ) from exc


def _retry_pending_remote_synchronization(
    repo_root: Path,
    *,
    state: WorkflowState,
    record: PromotionSyncRecord,
    upstream: TrackedUpstream,
) -> int:
    try:
        fetch_tracked_upstream(repo_root, upstream=upstream)
        comparison = compare_main_to_tracked_upstream(
            repo_root,
            main_branch=state.main_branch,
            upstream=upstream,
        )
    except RepositoryError as exc:
        raise FlowError(
            "Cannot retry remote synchronization: unable to fetch or resolve the tracked upstream. "
            f"{exc}"
        ) from exc

    if comparison.relationship == "diverged":
        raise FlowError(
            "Cannot retry remote synchronization: tracked upstream "
            f"{comparison.upstream_ref} has diverged from promoted {state.main_branch}. "
            "Reconcile local main before retrying."
        )

    if comparison.relationship == "upstream-behind":
        try:
            push_main_to_tracked_upstream(
                repo_root,
                main_branch=state.main_branch,
                upstream=upstream,
            )
        except RepositoryError as exc:
            print("Local promotion remains complete, but remote synchronization is pending.", file=sys.stderr)
            print("Rerun flow-promote to retry remote synchronization.", file=sys.stderr)
            print(f"Remote push failed: {exc}", file=sys.stderr)
            return 1

    _save_synchronized_promotion_record(repo_root, record)
    print("Remote synchronization completed.")
    print(f"commit: {record.promoted_main_commit}")
    return 0


def _ensure_main_and_scratch_branches_differ(state: WorkflowState) -> None:
    if state.main_branch == state.scratch_branch:
        raise FlowError(
            "Invalid workflow state: mainBranch and scratchBranch must be different."
        )


def _ensure_main_and_scratch_branches_exist(
    repo_root: Path,
    state: WorkflowState,
) -> None:
    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")
    if not branch_exists(repo_root, state.scratch_branch):
        raise FlowError(f"Scratch branch does not exist locally: {state.scratch_branch}")


def _patch_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "patch", '[--adopt] "<description>"')


def _start_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "start", "<issue-number>")


def _parse_issue_number(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise _start_usage(command_name)

    issue_text = arguments[0].strip()
    if not issue_text:
        raise FlowError("issue-number must be a positive integer.")

    try:
        issue_number = int(issue_text)
    except ValueError as exc:
        raise FlowError("issue-number must be a positive integer.") from exc

    if issue_number <= 0:
        raise FlowError("issue-number must be a positive integer.")

    return issue_number


def _adopt_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "start", "<issue-number> --adopt <commit-ish>")


def _parse_adopt_start(command_name: str, arguments: list[str]) -> tuple[int, str] | None:
    if "--adopt" not in arguments:
        return None
    if "--prerequisite-for" in arguments:
        raise FlowError(
            "Cannot start workflow: --adopt and --prerequisite-for cannot be combined."
        )
    if len(arguments) != 3 or arguments[1] != "--adopt":
        raise _adopt_usage(command_name)

    issue_number = _parse_issue_number(command_name, [arguments[0]])
    target = arguments[2].strip()
    if not target:
        raise FlowError("adopt target must be a non-empty commit-ish.")

    return issue_number, target


def _parse_prerequisite_start(command_name: str, arguments: list[str]) -> tuple[int, int] | None:
    if "--prerequisite-for" not in arguments:
        return None
    if len(arguments) != 3 or arguments[1] != "--prerequisite-for":
        raise _usage_error(
            command_name,
            "start",
            "<issue-number> [--prerequisite-for <active-issue>]",
        )
    prerequisite_number = _parse_issue_number(command_name, [arguments[0]])
    active_number = _parse_issue_number(command_name, [arguments[2]])
    if prerequisite_number == active_number:
        raise FlowError("Cannot start prerequisite workflow: issue numbers must be distinct.")
    return prerequisite_number, active_number


def _resolve_issue_details_with_labels(issue_number: int) -> tuple[str, str, list[str]]:
    completed = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "title,url,labels"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise FlowError(f"GitHub issue lookup failed for #{issue_number}: {message}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FlowError(
            f"GitHub issue lookup failed for #{issue_number}: invalid JSON response."
        ) from exc

    title = payload.get("title")
    url = payload.get("url")
    labels = payload.get("labels")

    resolved_labels: list[str] = []
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    resolved_labels.append(name.strip())

    return (
        title.strip() if isinstance(title, str) else "",
        url.strip() if isinstance(url, str) else "",
        resolved_labels,
    )


def _reconcile_github_workflow_label(issue_number: int, target_label: str, labels: list[str]) -> None:
    workflow_labels = ["active", "blocked", "backlog"]
    labels_to_remove = [label for label in workflow_labels if label != target_label and label in labels]
    add_needed = target_label not in labels

    if not add_needed and not labels_to_remove:
        return

    args = ["gh", "issue", "edit", str(issue_number)]
    if add_needed:
        args.extend(["--add-label", target_label])
    if labels_to_remove:
        args.extend(["--remove-label", ",".join(labels_to_remove)])

    completed = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise FlowError(
            f"GitHub label reconciliation failed for #{issue_number}: {message}"
        )


def _now_utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _active_workflow_type(state: WorkflowState) -> str | None:
    if state.active_issue_number is not None:
        return "issue"

    if state.patch_description is not None:
        return "patch"

    return None


def _validate_patch_description(description: str) -> str:
    normalized = description.strip()
    if not normalized:
        raise FlowError("patch description cannot be empty.")

    return normalized


def _validate_patch_prerequisites(
    *,
    repo_root: Path,
    state: WorkflowState,
) -> None:
    active_workflow_type = _active_workflow_type(state)
    if active_workflow_type is not None:
        if state.active_issue_number is not None:
            raise FlowError(
                f"Cannot patch workflow: active issue {state.active_issue_number} is already set."
            )
        assert state.patch_description is not None
        raise FlowError(
            f"Cannot patch workflow: active patch {state.patch_description} is already set."
        )


def _require_idle_workflow_for_start(state: WorkflowState) -> None:
    if _active_workflow_type(state) is None:
        return

    if state.active_issue_number is not None:
        raise FlowError(
            f"Cannot start workflow: active issue {state.active_issue_number} is already set."
        )
    assert state.patch_description is not None
    raise FlowError(
        f"Cannot start workflow: active patch {state.patch_description} is already set."
    )


def _require_unblocked_issue_for_start(
    repo_root: Path,
    command_name: str,
    issue_number: int,
) -> None:
    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_record = get_blocked_workflow(blocked_file, issue_number)
    if blocked_record is None:
        return

    if "-" in command_name:
        prefix, _, _ = command_name.rpartition("-")
        resume_command = f"{prefix}-resume"
    else:
        resume_command = "flow-resume"
    raise FlowError(
        f"Cannot start workflow: issue {issue_number} is blocked. "
        f"Use {resume_command} {issue_number}."
    )


def _require_clean_repository_for_start(repo_root: Path, state: WorkflowState) -> None:
    _ensure_main_and_scratch_branches_differ(state)

    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")

    if git_status_short(repo_root):
        raise FlowError(
            "Working tree is not clean. Commit, stash, or remove changes before starting."
        )

    ensure_no_active_git_operations(repo_root)


def handle_start(command_name: str, arguments: list[str]) -> int:
    adopt_arguments = _parse_adopt_start(command_name, arguments)
    if adopt_arguments is not None:
        return _handle_adopt_start(command_name, *adopt_arguments)

    prerequisite_arguments = _parse_prerequisite_start(command_name, arguments)
    if prerequisite_arguments is not None:
        return _handle_prerequisite_start(command_name, *prerequisite_arguments)

    issue_number = _parse_issue_number(command_name, arguments)

    repo_root, state_path, state = _resolve_repo_state_context()
    _require_idle_workflow_for_start(state)
    _require_unblocked_issue_for_start(repo_root, command_name, issue_number)
    _require_clean_repository_for_start(repo_root, state)

    provider = _resolve_ticket_provider_for_repo_root(repo_root)
    try:
        ticket = provider.get(str(issue_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    if ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot start workflow: ticket {issue_number} is {ticket.lifecycle_state}."
        )

    # Validate prospective state before any git mutation.
    issue_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=ticket.title,
        active_issue_url=ticket.reference.url,
        ticket_reference=ticket.reference,
    )
    issue_state = normalize_and_validate(
        issue_state.to_dict(),
        context="start command",
    )

    checkout_branch(repo_root, state.main_branch)
    create_or_reset_branch_from_source(
        repo_root,
        branch_name=state.scratch_branch,
        source_branch=state.main_branch,
    )
    checkout_branch(repo_root, state.scratch_branch)
    ensure_branches_point_to_same_commit(
        repo_root,
        left_branch=state.main_branch,
        right_branch=state.scratch_branch,
    )

    try:
        active_ticket = provider.mark_active(ticket.reference)
    except TicketProviderError as exc:
        raise FlowError(
            f"Cannot start workflow: failed to mark ticket {issue_number} active. {exc}"
        ) from exc

    _clear_promotion_review_record(repo_root)

    issue_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=active_ticket.title,
        active_issue_url=active_ticket.reference.url,
        ticket_reference=active_ticket.reference,
    )
    issue_state = normalize_and_validate(
        issue_state.to_dict(),
        context="start command",
    )

    ensure_local_state_excluded(repo_root)
    save_state(state_path, issue_state)
    try:
        clear_promotion_sync_record(repo_root)
    except PromotionSyncError as exc:
        raise FlowError(f"Started workflow but could not clear stale promotion synchronization state. {exc}") from exc
    _clear_diff_baseline_after_success(repo_root, operation="start")

    print(f"Started issue {issue_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")

    return 0


@dataclass(frozen=True)
class AdoptionTarget:
    """A validated adoption target resolved through Flow-owned repository behavior."""

    requested: str
    commit: str
    main_branch: str
    main_commit: str


def _adoption_ancestry_relationship(
    repo_root: Path,
    *,
    main_commit: str,
    target_commit: str,
) -> str:
    if branch_is_ancestor(
        repo_root,
        ancestor_revision=target_commit,
        descendant_revision=main_commit,
    ):
        return "behind"

    if revisions_share_history(
        repo_root,
        left_revision=main_commit,
        right_revision=target_commit,
    ):
        return "diverged from"

    return "unrelated to"


def resolve_adoption_target(
    repo_root: Path,
    state: WorkflowState,
    requested_target: str,
) -> AdoptionTarget:
    try:
        commit = resolve_commit_ish(repo_root, requested_target)
    except RevisionResolutionError as exc:
        raise FlowError(f"Cannot adopt commit: {exc}") from exc

    main_commit = resolve_commit_hash(repo_root, state.main_branch)

    if commit == main_commit:
        raise FlowError(
            f"Cannot adopt commit: {requested_target} already equals "
            f"{state.main_branch} ({main_commit}). Adoption requires a commit ahead of "
            f"{state.main_branch}."
        )

    if not branch_is_ancestor(
        repo_root,
        ancestor_revision=main_commit,
        descendant_revision=commit,
    ):
        relationship = _adoption_ancestry_relationship(
            repo_root,
            main_commit=main_commit,
            target_commit=commit,
        )
        raise FlowError(
            f"Cannot adopt commit: {requested_target} resolves to {commit}, which is "
            f"{relationship} {state.main_branch} ({main_commit}). Adoption requires "
            f"{state.main_branch} to be an ancestor of the target and never fetches, "
            "merges, rebases, cherry-picks, or otherwise reconciles."
        )

    return AdoptionTarget(
        requested=requested_target,
        commit=commit,
        main_branch=state.main_branch,
        main_commit=main_commit,
    )


def _handle_adopt_start(
    command_name: str,
    issue_number: int,
    requested_target: str,
) -> int:
    repo_root, _state_path, state = _resolve_repo_state_context()
    _require_idle_workflow_for_start(state)
    _require_unblocked_issue_for_start(repo_root, command_name, issue_number)
    _require_clean_repository_for_start(repo_root, state)

    target = resolve_adoption_target(repo_root, state, requested_target)

    raise FlowError(
        "Cannot adopt commit: adopting a validated target is not implemented yet. "
        f"{target.requested} resolves to {target.commit} and satisfies every adoption "
        f"precondition for issue {issue_number}, but binding the workflow to it is not "
        "available. No repository, workflow, or ticket state was changed."
    )


def _handle_prerequisite_start(
    command_name: str,
    prerequisite_number: int,
    active_number: int,
) -> int:
    repo_root, state_path, state = _resolve_repo_state_context()
    if state.patch_description is not None:
        raise FlowError("Cannot start prerequisite workflow: patch workflows are unsupported.")
    if state.active_issue_number is None:
        raise FlowError("Cannot start prerequisite workflow: no active issue workflow exists.")
    if state.active_issue_number != active_number:
        raise FlowError(
            f"Cannot start prerequisite workflow: active issue is {state.active_issue_number}, not {active_number}."
        )
    if state.stacked_handoff is not None:
        try:
            current_ref = resolve_managed_ref(
                repo_root,
                state.stacked_handoff.suspended_ref_name,
            )
        except RepositoryError as exc:
            raise FlowError(f"Cannot start prerequisite workflow: cannot validate suspended ref. {exc}") from exc
        if current_ref != state.stacked_handoff.suspended_commit:
            raise FlowError(
                "Cannot start prerequisite workflow: persisted suspended ref does not match its commit."
            )
        raise FlowError("Cannot start prerequisite workflow: nested handoffs are unsupported.")
    if state.ticket_reference is None:
        raise FlowError("Cannot start prerequisite workflow: active issue has no ticket reference.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)
    if current_branch_name(repo_root) != state.scratch_branch:
        raise FlowError(
            f"Cannot start prerequisite workflow: current branch must be {state.scratch_branch}."
        )
    if git_status_short(repo_root):
        raise FlowError("Cannot start prerequisite workflow: repository must be clean.")
    ensure_no_active_git_operations(repo_root)
    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    if comparison.scratch_behind_main != 0 or not branch_is_ancestor(
        repo_root,
        ancestor_revision=state.main_branch,
        descendant_revision=state.scratch_branch,
    ):
        raise FlowError(
            f"Cannot start prerequisite workflow: {state.scratch_branch} must be at or ahead of {state.main_branch}."
        )

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_file_existed_before = blocked_file.exists()
    blocked_before = load_blocked_workflows(blocked_file)
    if get_blocked_workflow(blocked_file, prerequisite_number) is not None:
        raise FlowError(
            f"Cannot start prerequisite workflow: issue {prerequisite_number} is already blocked."
        )

    try:
        provider = resolve_ticket_provider_for_reference(
            repo_root=repo_root,
            reference=state.ticket_reference,
        )
        active_ticket = provider.get(str(active_number))
        prerequisite_ticket = provider.get(str(prerequisite_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc
    if active_ticket.workflow_state != "active":
        raise FlowError(
            f"Cannot start prerequisite workflow: issue {active_number} is not active in the ticket provider."
        )
    if prerequisite_ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot start prerequisite workflow: ticket {prerequisite_number} is {prerequisite_ticket.lifecycle_state}."
        )
    if prerequisite_ticket.workflow_state != "inactive":
        raise FlowError(
            f"Cannot start prerequisite workflow: issue {prerequisite_number} is already {prerequisite_ticket.workflow_state}."
        )

    try:
        inherited_commit = resolve_commit_hash(repo_root, state.scratch_branch)
        inherited_tree = resolve_tree_hash(repo_root, state.scratch_branch)
        suspended_base_commit = resolve_commit_hash(repo_root, state.main_branch)
        suspended_tree = inherited_tree
    except RepositoryError as exc:
        raise FlowError(f"Cannot start prerequisite workflow: cannot record Git identity. {exc}") from exc

    handoff = StackedHandoff(
        relationship="prerequisite",
        prerequisite_for_issue_number=active_number,
        inherited_base_commit=inherited_commit,
        inherited_base_tree=inherited_tree,
        suspended_issue_number=active_number,
        suspended_issue_title=active_ticket.title,
        suspended_issue_url=active_ticket.reference.url or state.active_issue_url,
        suspended_ticket_reference=active_ticket.reference,
        suspended_checkpoint=state.checkpoint,
        suspended_commit=inherited_commit,
        suspended_tree=suspended_tree,
        suspended_base_commit=suspended_base_commit,
        suspended_ref_name=f"{_SUSPENDED_REF_PREFIX}{active_number}",
    )
    next_state = normalize_and_validate(
        WorkflowState(
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            checkpoint=0,
            active_issue_number=prerequisite_number,
            active_issue_title=prerequisite_ticket.title,
            active_issue_url=prerequisite_ticket.reference.url,
            ticket_reference=prerequisite_ticket.reference,
            stacked_handoff=handoff,
        ).to_dict(),
        context="prerequisite start command",
    )
    blocked_record = BlockedWorkflowRecord(
        issue_number=active_number,
        issue_title=active_ticket.title,
        issue_url=active_ticket.reference.url or state.active_issue_url,
        reason=f"Suspended for prerequisite issue {prerequisite_number}.",
        blocked_at=_now_utc_iso_timestamp(),
        ticket_reference=active_ticket.reference,
    )

    ref_created = False
    provider_changed = False
    try:
        ref_created = create_managed_ref(
            repo_root,
            handoff.suspended_ref_name,
            inherited_commit,
        )
        provider_changed = True
        provider.block(active_ticket.reference, blocked_record.reason)
        provider.mark_active(prerequisite_ticket.reference)
        upsert_blocked_workflow(blocked_file, blocked_record)
        save_state(state_path, next_state)
    except (RepositoryError, TicketProviderError, BlockedWorkflowsError, WorkflowStateError) as exc:
        rollback_errors: list[str] = []
        try:
            save_state(state_path, state)
        except WorkflowStateError as rollback_exc:
            rollback_errors.append(f"workflow state rollback failed: {rollback_exc}")
        try:
            save_blocked_workflows(blocked_file, blocked_before)
            if not blocked_file_existed_before and blocked_file.exists():
                blocked_file.unlink()
        except BlockedWorkflowsError as rollback_exc:
            rollback_errors.append(f"blocked registry rollback failed: {rollback_exc}")
        except OSError as rollback_exc:
            rollback_errors.append(f"blocked registry rollback failed: {rollback_exc}")
        if provider_changed:
            try:
                provider.deactivate(
                    prerequisite_ticket.reference,
                    prerequisite_ticket.labels,
                )
            except TicketProviderError as rollback_exc:
                rollback_errors.append(f"prerequisite provider rollback failed: {rollback_exc}")
            try:
                provider.resume(active_ticket.reference)
            except TicketProviderError as rollback_exc:
                rollback_errors.append(f"active issue provider rollback failed: {rollback_exc}")
        if ref_created:
            try:
                delete_managed_ref(
                    repo_root,
                    handoff.suspended_ref_name,
                    inherited_commit,
                )
            except RepositoryError as rollback_exc:
                rollback_errors.append(f"suspended ref rollback failed: {rollback_exc}")
        message = f"Cannot start prerequisite workflow: handoff failed. {exc}"
        if rollback_errors:
            message += " Remaining recovery state: " + " | ".join(rollback_errors)
        raise FlowError(message) from exc

    print(f"Started prerequisite issue {prerequisite_number} for issue {active_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")
    print(f"inheritedBase: {inherited_commit}")
    return 0


def _print_patch_success(
    *,
    adopted: bool,
    description: str,
    main_branch: str,
    scratch_branch: str,
    checkpoint: int,
) -> None:
    if adopted:
        print(f"Adopted patch: {description}")
    else:
        print(f"Started patch: {description}")

    print(f"mainBranch: {main_branch}")
    print(f"scratchBranch: {scratch_branch}")
    print(f"checkpoint: {checkpoint}")


def handle_patch(command_name: str, arguments: list[str]) -> int:
    adopt_mode = False
    raw_description: str

    if len(arguments) == 1:
        raw_description = arguments[0]
    elif len(arguments) == 2 and arguments[0] == "--adopt":
        adopt_mode = True
        raw_description = arguments[1]
    else:
        raise _patch_usage(command_name)

    if raw_description == "--adopt":
        raise _patch_usage(command_name)

    description = _validate_patch_description(raw_description)

    repo_root, state_path, state = _resolve_repo_state_context()

    _validate_patch_prerequisites(repo_root=repo_root, state=state)

    if adopt_mode:
        current_branch = current_branch_name(repo_root)
        if current_branch != state.scratch_branch:
            raise FlowError(
                f"Cannot patch workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
            )

        if not branch_exists(repo_root, state.scratch_branch):
            raise FlowError(
                f"Scratch branch does not exist locally: {state.scratch_branch}"
            )

        checkpoint = max_numbered_checkpoint_relative_to_main(
            repo_root,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
        )
    else:
        if git_status_short(repo_root):
            raise FlowError(
                "Working tree is not clean. Commit, stash, or remove changes before starting."
            )
        checkout_branch(repo_root, state.main_branch)
        create_or_reset_branch_from_source(
            repo_root,
            branch_name=state.scratch_branch,
            source_branch=state.main_branch,
        )
        checkout_branch(repo_root, state.scratch_branch)
        checkpoint = 0

    patch_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=checkpoint,
        patch_description=description,
    )
    _clear_promotion_review_record(repo_root)
    ensure_local_state_excluded(repo_root)
    save_state(state_path, patch_state)
    if not adopt_mode:
        try:
            clear_promotion_sync_record(repo_root)
        except PromotionSyncError as exc:
            raise FlowError(f"Started patch but could not clear stale promotion synchronization state. {exc}") from exc
    _clear_diff_baseline_after_success(repo_root, operation="patch")

    _print_patch_success(
        adopted=adopt_mode,
        description=description,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=checkpoint,
    )

    return 0


def _working_tree_details(
    status_lines: list[str],
) -> tuple[list[str], list[str], list[str], int, int, int, str]:
    staged_paths: set[str] = set()
    modified_paths: set[str] = set()
    untracked_paths: set[str] = set()

    for line in status_lines:
        if len(line) < 4:
            continue

        x = line[0]
        y = line[1]
        path_text = line[3:]

        if x == "?" and y == "?":
            untracked_paths.add(path_text)
            continue

        if x != " ":
            staged_paths.add(path_text)

        if y != " ":
            modified_paths.add(path_text)

    staged = sorted(staged_paths)
    modified = sorted(modified_paths)
    untracked = sorted(untracked_paths)

    staged_count = len(staged)
    modified_count = len(modified)
    untracked_count = len(untracked)
    working_tree = "clean"
    if staged_count > 0 or modified_count > 0 or untracked_count > 0:
        working_tree = "dirty"

    return (
        staged,
        modified,
        untracked,
        staged_count,
        modified_count,
        untracked_count,
        working_tree,
    )


def _branch_relation_info(comparison: BranchComparison) -> tuple[str, int, int]:
    if not comparison.main_exists:
        return ("missing-main", 0, 0)

    if not comparison.scratch_exists:
        return ("missing-scratch", 0, 0)

    assert comparison.scratch_behind_main is not None
    assert comparison.scratch_ahead_of_main is not None
    main_only = comparison.scratch_behind_main
    scratch_only = comparison.scratch_ahead_of_main

    if main_only == 0 and scratch_only == 0:
        return ("equal", 0, 0)
    if main_only == 0:
        return ("ahead", main_only, scratch_only)
    if scratch_only == 0:
        return ("behind", main_only, scratch_only)
    return ("diverged", main_only, scratch_only)


def _relationship_line_for_default_status(
    relation_state: str,
    main_branch: str,
    scratch_branch: str,
    main_only_count: int,
    scratch_only_count: int,
) -> str:
    if relation_state == "ahead":
        if scratch_only_count == 1:
            return f"1 commit ahead of {main_branch}"
        return f"{scratch_only_count} commits ahead of {main_branch}"

    if relation_state == "behind":
        if main_only_count == 1:
            return f"1 commit behind {main_branch}"
        return f"{main_only_count} commits behind {main_branch}"

    if relation_state == "diverged":
        return (
            "Branches have diverged: "
            f"{main_only_count} on {main_branch}, {scratch_only_count} on {scratch_branch}"
        )

    return ""


def _relationship_line_for_verbose_status(
    relation_state: str,
    main_only_count: int,
    scratch_only_count: int,
    main_branch: str,
    scratch_branch: str,
) -> str:
    if relation_state == "equal":
        return f"{scratch_branch} equals {main_branch}"
    if relation_state == "ahead":
        if scratch_only_count == 1:
            return f"{scratch_branch} is 1 commit ahead of {main_branch}"
        return f"{scratch_branch} is {scratch_only_count} commits ahead of {main_branch}"
    if relation_state == "behind":
        if main_only_count == 1:
            return f"{scratch_branch} is 1 commit behind {main_branch}"
        return f"{scratch_branch} is {main_only_count} commits behind {main_branch}"
    if relation_state == "diverged":
        return (
            f"{scratch_branch} and {main_branch} have diverged: "
            f"{main_only_count} on {main_branch}, {scratch_only_count} on {scratch_branch}"
        )
    if relation_state == "missing-main":
        return f"{main_branch} branch missing"
    if relation_state == "missing-scratch":
        return f"{scratch_branch} branch missing"
    return "relationship unavailable"


def _display_branch_name(branch_name: str) -> str:
    if branch_name == "HEAD":
        return "detached HEAD"

    return branch_name


def _workflow_type(state: object) -> str:
    if state.active_issue_number is not None:
        return "issue"

    if state.patch_description is not None:
        return "patch"

    return "none"


def _print_status_path_category(category_name: str, paths: list[str]) -> None:
    if not paths:
        return
    print(f"  {category_name}:")
    for path_text in paths:
        print(f"    {path_text}")


def _print_status_count_line(count: int, label: str) -> None:
    if count == 1:
        print(f"  1 {label}")
        return
    print(f"  {count} {label}")


def _status_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "status", "[-v|--verbose]")


def _diff_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "diff", "[--git|--all] | --refresh")


def _commit_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "commit")


def _reset_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "reset")


def _complete_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "complete")


def _promote_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "promote", '"<commit-message>"')


def _block_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "block", '"<reason>"')


def _resume_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "resume", "<ticket-number>")


def _ticket_create_usage(command_name: str) -> FlowError:
    return FlowError(
        f'Usage: {command_name} "<title>" [--body "<text>"] '
        '[--acceptance "<criterion>"]... [--label "<label>"]...'
    )


def _ticket_show_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} <ticket-id>")


def _ticket_query_usage(command_name: str) -> FlowError:
    return FlowError(
        "Usage: "
        f"{command_name} "
        '[--lifecycle <open|closed>] [--workflow <inactive|active|blocked>] '
        '[--label "<label>"]... [--query "<text>"]'
    )


def _normalize_ticket_lifecycle_state(
    *,
    raw_value: str,
) -> str:
    value = raw_value.strip()
    if value in {"open", "closed"}:
        return value
    raise FlowError(
        f"Invalid lifecycle state: {raw_value}. Expected one of: open, closed."
    )


def _normalize_ticket_workflow_state(
    *,
    raw_value: str,
) -> str:
    value = raw_value.strip()
    if value in {"inactive", "active", "blocked"}:
        return value
    raise FlowError(
        f"Invalid workflow state: {raw_value}. Expected one of: inactive, active, blocked."
    )


def _resolve_ticket_provider_for_repo_root(repo_root: Path) -> TicketProvider:
    try:
        configuration = load_ticket_configuration_for_repo_root(repo_root)
    except TicketConfigError as exc:
        raise FlowError(str(exc)) from exc
    try:
        return instantiate_ticket_provider(repo_root=repo_root, config=configuration)
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc


def _restore_promote_state(
    repo_root: Path,
    *,
    original_branch: str,
    main_branch: str,
    scratch_branch: str,
    original_main_commit: str,
    original_scratch_commit: str,
) -> None:
    try:
        restore_branch_to_revision(
            repo_root,
            branch_name=main_branch,
            revision=original_main_commit,
        )
    except RepositoryError:
        pass

    try:
        restore_branch_to_revision(
            repo_root,
            branch_name=scratch_branch,
            revision=original_scratch_commit,
        )
    except RepositoryError:
        pass

    try:
        if current_branch_name(repo_root) != original_branch:
            checkout_branch(repo_root, original_branch)
    except RepositoryError:
        pass


def _promote_branch_relationship_error(
    comparison: BranchComparison,
) -> str:
    assert comparison.scratch_ahead_of_main is not None
    assert comparison.scratch_behind_main is not None

    ahead = comparison.scratch_ahead_of_main
    behind = comparison.scratch_behind_main

    if ahead == 0 and behind == 0:
        return (
            f"Cannot promote workflow: {comparison.scratch_branch} is equal to "
            f"{comparison.main_branch}."
        )

    if behind > 0 and ahead == 0:
        return (
            f"Cannot promote workflow: {comparison.scratch_branch} is behind "
            f"{comparison.main_branch} (ahead: {ahead}, behind: {behind})."
        )

    if ahead > 0 and behind > 0:
        return (
            f"Cannot promote workflow: {comparison.main_branch} and "
            f"{comparison.scratch_branch} have diverged "
            f"(scratch ahead {ahead}, behind {behind})."
        )

    if ahead > 0 and behind == 0:
        return ""

    return (
        f"Cannot promote workflow: {comparison.scratch_branch} cannot be "
        f"promoted from {comparison.main_branch}."
    )


def _commit_subject(state: WorkflowState, checkpoint: int) -> str:
    return str(checkpoint)


def _clear_diff_baseline_after_success(repo_root: Path, *, operation: str) -> None:
    try:
        clear_diff_baseline_for_repo_root(repo_root)
    except RepositoryError as exc:
        print(
            f"Warning: review-baseline cleanup failed after successful {operation}: {exc}",
            file=sys.stderr,
        )
        print(
            "Run flow-diff --refresh before relying on baseline-relative diff.",
            file=sys.stderr,
        )


def handle_commit(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _commit_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot commit workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot commit workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)
    ensure_local_state_excluded(repo_root)

    stage_all_changes(repo_root)

    staged_lines = git_status_short(repo_root)
    _, _, _, staged_count, _, _, _ = _working_tree_details(staged_lines)
    if staged_count == 0:
        raise FlowError("Cannot commit workflow: no staged changes are available.")

    next_checkpoint = state.checkpoint + 1
    commit_subject = _commit_subject(state, next_checkpoint)

    try:
        commit_hash = create_commit(repo_root, message=commit_subject)
    except RepositoryError as exc:
        raise FlowError(f"Git commit failed. {exc}") from exc
    workflow_type = _active_workflow_type(state)

    updated_state = replace(state, checkpoint=next_checkpoint)
    try:
        save_state(state_path, updated_state)
    except WorkflowStateError as exc:
        raise FlowError(
            "Checkpoint commit created but workflow-state persistence failed. "
            f"Commit: {commit_hash}. {exc}"
        ) from exc
    _clear_diff_baseline_after_success(repo_root, operation="commit")

    print(f"Created checkpoint {next_checkpoint}")
    print(f"commit: {commit_hash}")
    if workflow_type == "patch":
        assert state.patch_description is not None
        print(f"patch: {state.patch_description}")
    else:
        assert state.active_issue_number is not None
        print(f"activeIssueNumber: {state.active_issue_number}")

    return 0


def _print_promote_success(
    *,
    commit_hash: str,
    main_branch: str,
    scratch_branch: str,
    workflow_type: str,
    active_issue_number: int | None,
    patch_description: str | None,
) -> None:
    print(f"Promoted {scratch_branch} to {main_branch}")
    print(f"commit: {commit_hash}")
    print("checkpoint: 0")
    if workflow_type == "patch":
        assert patch_description is not None
        print(f"patch: {patch_description}")
    else:
        assert active_issue_number is not None
        print(f"activeIssueNumber: {active_issue_number}")


def _print_promote_partial_success(
    *,
    commit_hash: str,
    commit_message: str,
    main_branch: str,
    scratch_branch: str,
    current_branch: str,
    current_scratch_commit: str | None,
    workflow_state_updated: bool,
) -> None:
    print("Promotion partially completed.")
    print(f"Commit: {commit_hash}")
    print(f"Message: {commit_message}")
    print(f"Main branch: {main_branch}")
    print(f"Scratch branch: {scratch_branch}")
    print(f"Current branch: {current_branch}")
    print(
        "Scratch commit: "
        f"{current_scratch_commit if current_scratch_commit is not None else '(unavailable)'}"
    )
    print(
        "Workflow state updated: "
        f"{'yes' if workflow_state_updated else 'no'}"
    )


def handle_promote(command_name: str, arguments: list[str]) -> int:
    if len(arguments) > 1:
        raise _promote_usage(command_name)

    commit_message = arguments[0].strip() if arguments else ""
    if arguments and not commit_message:
        raise _promote_usage(command_name)

    if not arguments:
        try:
            repo_root_for_retry = resolve_repo_root()
            pending_record = load_promotion_sync_record(repo_root_for_retry)
        except (RepositoryError, PromotionSyncError):
            pending_record = None
        if pending_record is None or pending_record.status != "pending":
            raise _promote_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot promote workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_differ(state)

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            "Cannot promote workflow: current branch "
            f"{current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)

    excluded_paths = [".ai-dev/"]
    if git_status_short_filtered(repo_root, excluded_paths=excluded_paths):
        raise FlowError("Cannot promote workflow: repository must be clean.")

    retry_context = _pending_sync_retry_context(repo_root, state)
    if retry_context is not None:
        record, upstream = retry_context
        return _retry_pending_remote_synchronization(
            repo_root,
            state=state,
            record=record,
            upstream=upstream,
        )
    if not commit_message:
        raise _promote_usage(command_name)

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    if comparison.scratch_ahead_of_main is None or comparison.scratch_behind_main is None:
        raise FlowError(
            "Cannot promote workflow: unable to determine branch relationship."
        )

    if not branch_is_ancestor(
        repo_root,
        ancestor_revision=state.main_branch,
        descendant_revision=state.scratch_branch,
    ):
        relationship_error = _promote_branch_relationship_error(comparison)
        if relationship_error:
            raise FlowError(relationship_error)

    relationship_error = _promote_branch_relationship_error(comparison)
    if relationship_error:
        raise FlowError(relationship_error)

    upstream = _require_tracked_upstream_preflight(repo_root, state)
    _require_valid_promotion_review_gate(repo_root, state)

    if state.stacked_handoff is not None:
        print(
            "Warning: promoting this prerequisite publishes the complete physical "
            "A+B tree, including suspended issue A's partial tree."
        )

    original_branch = current_branch
    original_main_commit = resolve_commit_hash(repo_root, state.main_branch)
    original_scratch_commit = resolve_commit_hash(repo_root, state.scratch_branch)
    original_scratch_tree = resolve_tree_hash(repo_root, state.scratch_branch)

    sync_local_excludes(repo_root)

    main_commit_created = False
    workflow_state_updated = False
    commit_hash = ""
    current_scratch_commit: str | None = None

    try:
        checkout_branch(repo_root, state.main_branch)
        squash_merge_branch_into_current(repo_root, state.scratch_branch)
        create_commit(repo_root, message=commit_message)
        main_commit_created = True
        commit_hash = resolve_commit_hash(repo_root, "HEAD")

        checkout_branch(repo_root, state.scratch_branch)
        hard_reset_branch_to_revision(
            repo_root,
            branch_name=state.scratch_branch,
            revision=commit_hash,
        )

        ensure_branches_point_to_same_commit(
            repo_root,
            left_branch=state.main_branch,
            right_branch=state.scratch_branch,
        )

        if resolve_tree_hash(repo_root, state.main_branch) != original_scratch_tree:
            raise FlowError(
                "Cannot promote workflow: promoted tree does not match scratch tree."
            )

        updated_resume = state.stacked_resume
        if state.stacked_handoff is not None:
            updated_resume = {
                "suspendedIssueNumber": state.stacked_handoff.suspended_issue_number,
                "promotedMainCommit": commit_hash,
                "suspendedCommit": state.stacked_handoff.suspended_commit,
                "suspendedRefName": state.stacked_handoff.suspended_ref_name,
                "checkpoint": state.stacked_handoff.suspended_checkpoint,
            }
        if updated_resume is not None:
            updated_resume = {
                **updated_resume,
                "promotedMainCommit": commit_hash,
            }
        updated_state = replace(
            state,
            checkpoint=0,
            stacked_resume=updated_resume,
        )
        save_state(state_path, updated_state)
        workflow_state_updated = True

        if state.stacked_handoff is not None:
            blocked_file = blocked_workflows_file_for_repo_root(repo_root)
            blocked_records = load_blocked_workflows(blocked_file)
            suspended_record = get_blocked_workflow(
                blocked_file,
                state.stacked_handoff.suspended_issue_number,
            )
            if suspended_record is None:
                raise FlowError(
                    "Cannot promote workflow: suspended issue blocked record is missing."
                )
            assert updated_resume is not None
            resume_metadata = updated_resume
            updated_record = replace(
                suspended_record,
                resume_metadata=resume_metadata,
            )
            save_blocked_workflows(
                blocked_file,
                [
                    updated_record if item.issue_number == updated_record.issue_number else item
                    for item in blocked_records
                ],
            )

        sync_record: PromotionSyncRecord | None = None
        if upstream is not None:
            sync_record = PromotionSyncRecord(
                status="pending",
                main_branch=state.main_branch,
                scratch_branch=state.scratch_branch,
                promoted_main_commit=commit_hash,
                remote_name=upstream.remote_name,
                upstream_ref=upstream.merge_ref,
                active_issue_number=state.active_issue_number,
                patch_description=state.patch_description,
            )
            try:
                save_promotion_sync_record(repo_root, sync_record)
            except PromotionSyncError as exc:
                raise FlowError(
                    "Local promotion succeeded but remote synchronization state could not be recorded. "
                    f"{exc}"
                ) from exc

        _clear_promotion_review_record(repo_root)

        if current_branch_name(repo_root) != state.scratch_branch:
            raise FlowError(
                "Cannot promote workflow: scratch branch was not checked out after promotion."
            )

        _clear_diff_baseline_after_success(repo_root, operation="promote")

        if sync_record is not None:
            try:
                push_main_to_tracked_upstream(
                    repo_root,
                    main_branch=state.main_branch,
                    upstream=upstream,
                )
            except RepositoryError as exc:
                print("Local promotion succeeded, but remote synchronization failed.", file=sys.stderr)
                print("Rerun flow-promote to retry remote synchronization.", file=sys.stderr)
                print(f"Remote push failed: {exc}", file=sys.stderr)
                return 1
            _save_synchronized_promotion_record(repo_root, sync_record)

        _print_promote_success(
            commit_hash=commit_hash,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            workflow_type=_active_workflow_type(state) or "issue",
            active_issue_number=state.active_issue_number,
            patch_description=state.patch_description,
        )

        return 0
    except (RepositoryError, WorkflowStateError, FlowError) as exc:
        if not main_commit_created:
            _restore_promote_state(
                repo_root,
                original_branch=original_branch,
                main_branch=state.main_branch,
                scratch_branch=state.scratch_branch,
                original_main_commit=original_main_commit,
                original_scratch_commit=original_scratch_commit,
            )
            raise FlowError(f"Cannot promote workflow: {exc}") from exc

        try:
            current_branch_after = current_branch_name(repo_root)
        except RepositoryError:
            current_branch_after = original_branch

        try:
            current_scratch_commit = resolve_commit_hash(
                repo_root,
                state.scratch_branch,
            )
        except RepositoryError:
            current_scratch_commit = None

        _print_promote_partial_success(
            commit_hash=resolve_short_commit_hash(repo_root, commit_hash),
            commit_message=commit_message,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            current_branch=current_branch_after,
            current_scratch_commit=current_scratch_commit,
            workflow_state_updated=workflow_state_updated,
        )
        return 1


def _flow_diff_cached_changes(repo_root: Path) -> str:
    diff_completed = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--binary", "--no-ext-diff"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if diff_completed.returncode != 0:
        message = diff_completed.stderr.strip() or diff_completed.stdout.strip()
        raise FlowError(message)

    return diff_completed.stdout


def _flow_diff_workflow_changes(
    repo_root: Path,
    *,
    base_revision: str,
    scratch_branch: str,
) -> str:
    diff_completed = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--binary", "--no-ext-diff", f"{base_revision}...{scratch_branch}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if diff_completed.returncode != 0:
        message = diff_completed.stderr.strip() or diff_completed.stdout.strip()
        raise FlowError(message)

    return diff_completed.stdout


def _decode_nul_paths(raw_output: bytes) -> list[str]:
    decoded = raw_output.decode("utf-8", errors="surrogateescape")
    if not decoded:
        return []

    parts = decoded.split("\x00")
    if parts and parts[-1] == "":
        parts = parts[:-1]

    return [item for item in parts if item != ""]


def _diff_untracked_paths(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FlowError(message)
    return _decode_nul_paths(completed.stdout)


def _diff_untracked_content(repo_root: Path, paths: list[str]) -> str:
    if not paths:
        return ""

    diff_parts: list[str] = []
    for path_text in paths:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-index",
                "--",
                "/dev/null",
                path_text,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        # git diff --no-index returns 1 when differences exist; treat as success.
        if completed.returncode not in {0, 1}:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise FlowError(message)

        if completed.stdout:
            diff_parts.append(completed.stdout)

    return "".join(diff_parts)


def _parse_diff_options(command_name: str, arguments: list[str]) -> tuple[bool, bool, bool]:
    use_git = False
    include_all = False
    refresh = False

    for option in arguments:
        if option == "--git":
            if use_git:
                raise FlowError("--git may be provided at most once.")
            use_git = True
            continue
        if option == "--all":
            if include_all:
                raise FlowError("--all may be provided at most once.")
            include_all = True
            continue
        if option == "--refresh":
            if refresh:
                raise FlowError("--refresh may be provided at most once.")
            refresh = True
            continue
        raise _diff_usage(command_name)

    if refresh and use_git:
        raise FlowError("--refresh cannot be combined with --git.")
    if refresh and include_all:
        raise FlowError("--refresh cannot be combined with --all.")
    if use_git and include_all:
        raise FlowError("--git cannot be combined with --all.")

    return use_git, include_all, refresh


@dataclass(frozen=True)
class _FlowDiffStatusEntry:
    index_status: str
    worktree_status: str
    path: str
    source_path: str | None = None


def _parse_flow_diff_status_tokens(tokens: list[str]) -> list[_FlowDiffStatusEntry]:
    entries: list[_FlowDiffStatusEntry] = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        if len(record) < 4 or record[2] != " ":
            raise FlowError("Cannot parse repository status for diff baseline refresh.")

        x = record[0]
        y = record[1]
        destination_path = record[3:]
        source_path: str | None = None

        if x in {"R", "C"} or y in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise FlowError("Cannot parse renamed/copied path in diff baseline refresh.")
            source_path = tokens[index + 1]
            index += 2
        else:
            index += 1

        entries.append(
            _FlowDiffStatusEntry(
                index_status=x,
                worktree_status=y,
                path=destination_path,
                source_path=source_path,
            )
        )

    return entries


def _flow_diff_status_entries(repo_root: Path) -> list[_FlowDiffStatusEntry]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain=1",
            "--untracked-files=all",
            "-z",
            "--",
            ".",
            ":(exclude).ai-dev/**",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FlowError(message)

    tokens = _decode_nul_paths(completed.stdout)
    return _parse_flow_diff_status_tokens(tokens)


def _git_show_index_bytes(repo_root: Path, path_text: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f":{path_text}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FlowError(f"Cannot capture staged baseline content for {path_text}: {message}")
    return completed.stdout


def _git_show_tree_path_bytes(repo_root: Path, treeish: str, path_text: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{treeish}:{path_text}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout

    message = completed.stderr.decode("utf-8", errors="replace").strip()
    if (
        "exists on disk, but not in" in message
        or "does not exist in" in message
        or "pathspec" in message
    ):
        return None
    raise FlowError(f"Cannot capture baseline HEAD content for {path_text}: {message}")


def _read_working_file_bytes(repo_root: Path, path_text: str) -> bytes | None:
    absolute_path = (repo_root / path_text).resolve()
    try:
        absolute_path.relative_to(repo_root)
    except ValueError as exc:
        raise FlowError(f"Cannot capture baseline path outside repository: {path_text}") from exc

    if not absolute_path.exists() or not absolute_path.is_file():
        return None

    try:
        return absolute_path.read_bytes()
    except OSError as exc:
        raise FlowError(f"Cannot capture working baseline content for {path_text}: {exc}") from exc


def _flow_diff_current_uncommitted_changes(repo_root: Path) -> str:
    staged_diff = _flow_diff_cached_changes(repo_root)
    unstaged_completed = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--binary", "--no-ext-diff"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if unstaged_completed.returncode != 0:
        message = unstaged_completed.stderr.strip() or unstaged_completed.stdout.strip()
        raise FlowError(message)
    unstaged_diff = unstaged_completed.stdout

    untracked_paths = _diff_untracked_paths(repo_root)
    untracked_diff = _diff_untracked_content(repo_root, untracked_paths)

    return "".join(
        part
        for part in (
            staged_diff,
            unstaged_diff,
            untracked_diff,
        )
        if part
    )


def _baseline_path_set(mapping: dict[str, object], key: str) -> set[str]:
    raw_value = mapping.get(key, [])
    if not isinstance(raw_value, list):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    return {str(item) for item in raw_value}


def _baseline_working_snapshots(mapping: dict[str, object]) -> dict[str, bytes]:
    raw_snapshots = mapping.get("snapshots")
    if not isinstance(raw_snapshots, dict):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    raw_working = raw_snapshots.get("working", {})
    if not isinstance(raw_working, dict):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    decoded: dict[str, bytes] = {}
    for key, encoded in raw_working.items():
        if not isinstance(encoded, str):
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
        try:
            decoded[str(key)] = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID) from exc

    return decoded


def _baseline_rename_source_paths(mapping: dict[str, object]) -> set[str]:
    raw_status = mapping.get("status")
    if not isinstance(raw_status, dict):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    raw_rename_copy = raw_status.get("renameCopy", [])
    if not isinstance(raw_rename_copy, list):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    rename_sources: set[str] = set()
    for item in raw_rename_copy:
        if not isinstance(item, dict):
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
        source = item.get("sourcePath")
        kind = item.get("kind")
        index_status = item.get("indexStatus")
        worktree_status = item.get("worktreeStatus")
        if not isinstance(source, str):
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
        if not isinstance(kind, str):
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
        if not isinstance(index_status, str) or not isinstance(worktree_status, str):
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

        is_rename = kind == "rename" or "R" in {index_status, worktree_status}
        if is_rename:
            rename_sources.add(source)

    return rename_sources


def _validate_flow_diff_baseline_identity(
    repo_root: Path,
    state: WorkflowState,
    baseline: dict[str, object],
) -> str:
    version = baseline.get("version")
    if version != 1:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    repository = baseline.get("repository")
    workflow = baseline.get("workflow")
    if not isinstance(repository, dict) or not isinstance(workflow, dict):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    baseline_root = repository.get("root")
    baseline_head = repository.get("head")
    baseline_branch = repository.get("branch")
    if not isinstance(baseline_root, str):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if not isinstance(baseline_head, str):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if not isinstance(baseline_branch, str):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    if Path(baseline_root).resolve() != repo_root.resolve():
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if baseline_branch != current_branch_name(repo_root):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if baseline_head != resolve_commit_hash(repo_root, "HEAD"):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    current_workflow_type = _active_workflow_type(state) or "none"
    if workflow.get("type") != current_workflow_type:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if workflow.get("mainBranch") != state.main_branch:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if workflow.get("scratchBranch") != state.scratch_branch:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if workflow.get("checkpoint") != state.checkpoint:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if workflow.get("activeIssueNumber") != state.active_issue_number:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    if workflow.get("patchDescription") != state.patch_description:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    baseline_stacked = workflow.get("stackedScope")
    if state.stacked_handoff is None:
        if baseline_stacked is not None:
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)
    else:
        expected_stacked = {
            "activeIssueNumber": state.active_issue_number,
            "inheritedBaseCommit": state.stacked_handoff.inherited_base_commit,
            "suspendedIssueNumber": state.stacked_handoff.suspended_issue_number,
            "suspendedRefName": state.stacked_handoff.suspended_ref_name,
        }
        if baseline_stacked != expected_stacked:
            raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    return baseline_head


def _flow_diff_file_delta_patch(
    path_text: str,
    baseline_bytes: bytes | None,
    current_bytes: bytes | None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="flow-diff-baseline-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        before_file = temp_dir / "before"
        after_file = temp_dir / "after"

        before_arg = "/dev/null"
        after_arg = "/dev/null"

        if baseline_bytes is not None:
            before_file.write_bytes(baseline_bytes)
            before_arg = str(before_file)
        if current_bytes is not None:
            after_file.write_bytes(current_bytes)
            after_arg = "after"
        if baseline_bytes is not None:
            before_arg = "before"

        completed = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-index",
                "--",
                before_arg,
                after_arg,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            cwd=str(temp_dir),
        )

    if completed.returncode not in {0, 1}:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise FlowError(message)

    rewritten_lines: list[str] = []
    for line in completed.stdout.splitlines(keepends=True):
        if line.startswith("diff --git "):
            rewritten_lines.append(f"diff --git a/{path_text} b/{path_text}\n")
            continue
        if line.startswith("--- a/before") or line.startswith("--- a/after"):
            rewritten_lines.append(f"--- a/{path_text}\n")
            continue
        if line.startswith("+++ b/after") or line.startswith("+++ b/before"):
            rewritten_lines.append(f"+++ b/{path_text}\n")
            continue
        if line.startswith("Binary files "):
            rewritten = line.replace("a/before", f"a/{path_text}")
            rewritten = rewritten.replace("a/after", f"a/{path_text}")
            rewritten = rewritten.replace("b/before", f"b/{path_text}")
            rewritten = rewritten.replace("b/after", f"b/{path_text}")
            rewritten_lines.append(rewritten)
            continue
        rewritten_lines.append(line)

    return "".join(rewritten_lines)


def _flow_diff_relative_to_baseline(repo_root: Path, state: WorkflowState) -> str | None:
    baseline_path = diff_baseline_file_for_repo_root(repo_root)
    if not baseline_path.exists():
        return None

    try:
        baseline = load_json_object(baseline_path, missing_default={})
    except JsonFileError as exc:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID) from exc
    if not baseline:
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    baseline_head = _validate_flow_diff_baseline_identity(repo_root, state, baseline)

    baseline_status = baseline.get("status")
    if not isinstance(baseline_status, dict):
        raise FlowError(_FLOW_DIFF_BASELINE_INVALID)

    baseline_staged = _baseline_path_set(baseline_status, "staged")
    baseline_unstaged = _baseline_path_set(baseline_status, "unstaged")
    baseline_untracked = _baseline_path_set(baseline_status, "untracked")
    baseline_deleted = _baseline_path_set(baseline_status, "deleted")
    baseline_rename_sources = _baseline_rename_source_paths(baseline)
    baseline_working = _baseline_working_snapshots(baseline)

    baseline_touched = (
        baseline_staged
        | baseline_unstaged
        | baseline_untracked
        | baseline_deleted
        | baseline_rename_sources
    )

    current_entries = _flow_diff_status_entries(repo_root)
    current_changed: set[str] = set()
    for entry in current_entries:
        x = entry.index_status
        y = entry.worktree_status
        if x == "?" and y == "?":
            current_changed.add(entry.path)
            continue

        current_changed.add(entry.path)
        if entry.source_path is not None and "R" in {x, y}:
            current_changed.add(entry.source_path)

    candidate_paths = {
        path
        for path in (baseline_touched | current_changed)
        if path != ".ai-dev" and not path.startswith(".ai-dev/")
    }

    diff_parts: list[str] = []
    for path_text in sorted(candidate_paths):
        if path_text in baseline_rename_sources or path_text in baseline_deleted:
            baseline_bytes: bytes | None = None
        elif path_text in baseline_working:
            baseline_bytes = baseline_working[path_text]
        else:
            baseline_bytes = _git_show_tree_path_bytes(repo_root, baseline_head, path_text)

        current_bytes = _read_working_file_bytes(repo_root, path_text)
        if baseline_bytes == current_bytes:
            continue

        diff_parts.append(_flow_diff_file_delta_patch(path_text, baseline_bytes, current_bytes))

    return "".join(part for part in diff_parts if part)


def _flow_diff_refresh_baseline(repo_root: Path, state: WorkflowState) -> None:
    status_entries = _flow_diff_status_entries(repo_root)

    staged_paths: set[str] = set()
    unstaged_paths: set[str] = set()
    untracked_paths: set[str] = set()
    deleted_paths: set[str] = set()
    staged_deleted_paths: set[str] = set()
    unstaged_deleted_paths: set[str] = set()
    rename_copy_entries: list[dict[str, object]] = []

    for entry in status_entries:
        x = entry.index_status
        y = entry.worktree_status
        path_text = entry.path

        if x == "?" and y == "?":
            untracked_paths.add(path_text)
            continue

        if x != " ":
            staged_paths.add(path_text)
        if y != " ":
            unstaged_paths.add(path_text)
        if x == "D":
            staged_deleted_paths.add(path_text)
            deleted_paths.add(path_text)
        if y == "D":
            unstaged_deleted_paths.add(path_text)
            deleted_paths.add(path_text)

        if entry.source_path is not None:
            relation_status = x if x in {"R", "C"} else y if y in {"R", "C"} else ""
            rename_copy_entries.append(
                {
                    "kind": "rename" if relation_status == "R" else "copy" if relation_status == "C" else "rename-or-copy",
                    "sourcePath": entry.source_path,
                    "destinationPath": path_text,
                    "indexStatus": x,
                    "worktreeStatus": y,
                }
            )

    index_snapshots: dict[str, str] = {}
    for path_text in sorted(staged_paths - staged_deleted_paths):
        content = _git_show_index_bytes(repo_root, path_text)
        index_snapshots[path_text] = base64.b64encode(content).decode("ascii")

    working_snapshots: dict[str, str] = {}
    working_paths = (staged_paths | unstaged_paths | untracked_paths) - deleted_paths
    for path_text in sorted(working_paths):
        absolute_path = (repo_root / path_text).resolve()
        try:
            absolute_path.relative_to(repo_root)
        except ValueError as exc:
            raise FlowError(f"Cannot capture baseline path outside repository: {path_text}") from exc

        if not absolute_path.exists() or not absolute_path.is_file():
            continue

        try:
            content = absolute_path.read_bytes()
        except OSError as exc:
            raise FlowError(f"Cannot capture working baseline content for {path_text}: {exc}") from exc

        working_snapshots[path_text] = base64.b64encode(content).decode("ascii")

    workflow_type = _active_workflow_type(state) or "none"
    stacked_scope = None
    if state.stacked_handoff is not None:
        stacked_scope = {
            "activeIssueNumber": state.active_issue_number,
            "inheritedBaseCommit": state.stacked_handoff.inherited_base_commit,
            "suspendedIssueNumber": state.stacked_handoff.suspended_issue_number,
            "suspendedRefName": state.stacked_handoff.suspended_ref_name,
        }
    baseline_payload = {
        "version": 1,
        "capturedAt": _now_utc_iso_timestamp(),
        "repository": {
            "root": str(repo_root),
            "head": resolve_commit_hash(repo_root, "HEAD"),
            "branch": current_branch_name(repo_root),
        },
        "workflow": {
            "type": workflow_type,
            "mainBranch": state.main_branch,
            "scratchBranch": state.scratch_branch,
            "checkpoint": state.checkpoint,
            "activeIssueNumber": state.active_issue_number,
            "patchDescription": state.patch_description,
            "stackedScope": stacked_scope,
        },
        "status": {
            "staged": sorted(staged_paths),
            "unstaged": sorted(unstaged_paths),
            "untracked": sorted(untracked_paths),
            "deleted": sorted(deleted_paths),
            "stagedDeleted": sorted(staged_deleted_paths),
            "unstagedDeleted": sorted(unstaged_deleted_paths),
            "renameCopy": sorted(
                rename_copy_entries,
                key=lambda item: (
                    str(item["destinationPath"]),
                    str(item["sourcePath"]),
                    str(item["indexStatus"]),
                    str(item["worktreeStatus"]),
                ),
            ),
        },
        "snapshots": {
            "index": index_snapshots,
            "working": working_snapshots,
        },
    }

    baseline_path = diff_baseline_file_for_repo_root(repo_root)
    try:
        write_json_object_atomic(baseline_path, baseline_payload)
    except JsonFileError as exc:
        raise FlowError(str(exc)) from exc


def handle_diff(command_name: str, arguments: list[str]) -> int:
    use_git, include_all, refresh = _parse_diff_options(command_name, arguments)

    repo_root, _, state = _resolve_repo_state_context()
    if _active_workflow_type(state) is None:
        raise FlowError("Cannot diff workflow: no active workflow is set.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    if refresh:
        _flow_diff_refresh_baseline(repo_root, state)
        print("Review baseline refreshed.")
        return 0

    current_uncommitted = _flow_diff_current_uncommitted_changes(repo_root)

    committed_diff = ""
    if include_all:
        base_revision = state.main_branch
        if state.stacked_handoff is not None:
            base_revision = state.stacked_handoff.inherited_base_commit
        committed_diff = _flow_diff_workflow_changes(
            repo_root,
            base_revision=base_revision,
            scratch_branch=state.scratch_branch,
        )

    scoped_uncommitted = current_uncommitted
    if not include_all and not use_git:
        baseline_relative = _flow_diff_relative_to_baseline(repo_root, state)
        if baseline_relative is not None:
            scoped_uncommitted = baseline_relative

    combined = "".join(
        part
        for part in (
            committed_diff,
            scoped_uncommitted,
        )
        if part
    )

    if not combined:
        print("No diff content for current scope.", file=sys.stderr)
        return 0

    print(combined, end="")
    return 0


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def handle_reset(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _reset_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot reset workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_differ(state)

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot reset workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)

    sync_local_excludes(repo_root)

    try:
        reset_revision = state.main_branch
        if state.stacked_handoff is not None:
            reset_revision = state.stacked_handoff.inherited_base_commit
        hard_reset_branch_to_revision(
            repo_root,
            branch_name=state.scratch_branch,
            revision=reset_revision,
        )
        clean_untracked_non_ignored(repo_root)
        if state.stacked_handoff is None:
            ensure_branches_point_to_same_commit(
                repo_root,
                left_branch=state.main_branch,
                right_branch=state.scratch_branch,
            )
    except RepositoryError as exc:
        raise FlowError(f"Git reset failed: {exc}") from exc

    _clear_promotion_review_record(repo_root)

    updated_state = replace(state, checkpoint=0)
    try:
        save_state(state_path, updated_state)
    except WorkflowStateError as exc:
        raise FlowError(
            "Scratch was reset but workflow state could not be saved. "
            f"{exc}"
        ) from exc
    try:
        clear_promotion_sync_record(repo_root)
    except PromotionSyncError as exc:
        raise FlowError(f"Scratch was reset but stale promotion synchronization state could not be cleared. {exc}") from exc
    _clear_diff_baseline_after_success(repo_root, operation="reset")

    reset_target = "inherited base" if state.stacked_handoff is not None else state.main_branch
    print(f"Reset {state.scratch_branch} to {reset_target}")
    print("checkpoint: 0")
    if state.patch_description is not None:
        print(f"patch: {state.patch_description}")
    else:
        assert state.active_issue_number is not None
        print(f"activeIssueNumber: {state.active_issue_number}")
    return 0


def handle_abandon(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _usage_error(command_name, "abandon", "")

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot abandon workflow: no active workflow is set.")

    _ensure_main_and_scratch_branches_differ(state)
    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot abandon workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)
    if git_status_short(repo_root):
        raise FlowError("Cannot abandon workflow: repository must be clean.")

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    assert comparison.scratch_ahead_of_main is not None
    assert comparison.scratch_behind_main is not None
    if comparison.scratch_ahead_of_main != 0 or comparison.scratch_behind_main != 0:
        if comparison.scratch_ahead_of_main != 0 and comparison.scratch_behind_main == 0:
            detail = f"{state.scratch_branch} is ahead of {state.main_branch}"
        elif comparison.scratch_ahead_of_main == 0:
            detail = f"{state.scratch_branch} is behind {state.main_branch}"
        else:
            detail = f"{state.scratch_branch} and {state.main_branch} have diverged"
        raise FlowError(
            f"Cannot abandon workflow: {detail}; resolve or preserve repository state explicitly first."
        )

    _clear_promotion_review_record(repo_root)
    try:
        clear_promotion_sync_record(repo_root)
    except PromotionSyncError as exc:
        raise FlowError(f"Cannot abandon workflow: {exc}") from exc
    _clear_diff_baseline_after_success(repo_root, operation="abandon")
    try:
        clear_state(state_path)
    except WorkflowStateError as exc:
        raise FlowError(f"Cannot abandon workflow: failed to clear local workflow state. {exc}") from exc

    print("Abandoned local workflow")
    print("Workflow: inactive")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")
    return 0


def handle_complete(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _complete_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()
    _validate_stacked_resume(repo_root, state)

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot complete workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_differ(state)

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot complete workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)
    if git_status_short(repo_root):
        raise FlowError("Cannot complete workflow: repository must be clean.")

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    assert comparison.scratch_ahead_of_main is not None
    assert comparison.scratch_behind_main is not None
    ahead = comparison.scratch_ahead_of_main
    behind = comparison.scratch_behind_main
    if not (ahead == 0 and behind == 0):
        if behind == 0:
            raise FlowError(
                f"Cannot complete workflow: {state.scratch_branch} is ahead of {state.main_branch}."
            )
        if ahead == 0:
            raise FlowError(
                f"Cannot complete workflow: {state.scratch_branch} is behind {state.main_branch}."
            )
        raise FlowError(
            f"Cannot complete workflow: {state.scratch_branch} and {state.main_branch} have diverged."
        )

    direct_empty_resumed_completion = (
        state.stacked_resume is not None
        and state.stacked_handoff is None
        and state.checkpoint == state.stacked_resume["checkpoint"]
        and resolve_commit_hash(repo_root, state.main_branch)
        == state.stacked_resume["promotedMainCommit"]
    )

    if state.checkpoint != 0 and not direct_empty_resumed_completion:
        raise FlowError(
            f"Cannot complete workflow: checkpoint must be 0 (current: {state.checkpoint})."
        )

    if direct_empty_resumed_completion:
        try:
            pending_sync = load_promotion_sync_record(repo_root)
        except PromotionSyncError as exc:
            raise FlowError(
                "Cannot complete workflow: promotion synchronization state is invalid. "
                f"{exc}"
            ) from exc
        if pending_sync is not None and pending_sync.status == "pending":
            raise FlowError(
                "Cannot complete workflow: promotion synchronization is pending. "
                "Rerun flow-promote to retry remote synchronization."
            )
    else:
        _require_synchronized_completion_state(repo_root, state)

    if state.active_issue_number is not None:
        if state.ticket_reference is None:
            # Compatibility path for legacy workflows created before ticket
            # references were persisted.
            try:
                completed = subprocess.run(
                    ["gh", "issue", "close", str(state.active_issue_number)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except FileNotFoundError as exc:
                raise FlowError("GitHub CLI (gh) is required for this command.") from exc

            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise FlowError(
                    "Cannot complete workflow: failed to close GitHub issue "
                    f"{state.active_issue_number}: {message}"
                )
        else:
            try:
                provider = resolve_ticket_provider_for_reference(
                    repo_root=repo_root,
                    reference=state.ticket_reference,
                )
            except TicketProviderError as exc:
                raise FlowError(str(exc)) from exc

            try:
                provider.complete(state.ticket_reference)
            except TicketProviderError as exc:
                raise FlowError(
                    "Cannot complete workflow: failed to complete bound ticket "
                    f"{state.active_issue_number}. {exc}"
                ) from exc

    if state.stacked_resume is not None and state.stacked_handoff is None:
        try:
            delete_managed_ref(
                repo_root,
                state.stacked_resume["suspendedRefName"],
                state.stacked_resume["suspendedCommit"],
            )
        except RepositoryError as exc:
            raise FlowError(
                "Cannot complete workflow: ticket completion succeeded but suspended ref cleanup failed. "
                f"Retry flow-complete after fixing the managed ref. {exc}"
            ) from exc

    inactive_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
    )
    try:
        save_state(state_path, inactive_state)
    except WorkflowStateError as exc:
        if state.active_issue_number is None:
            raise FlowError(
                "Cannot complete workflow: patch completed but workflow state could not be cleared. "
                f"{exc}"
            ) from exc

        raise FlowError(
            "Cannot complete workflow: ticket completion succeeded but workflow state could not be cleared. "
            "The active workflow state was kept so you can retry flow-complete after fixing local state persistence. "
            f"{exc}"
        ) from exc
    try:
        clear_promotion_sync_record(repo_root)
    except PromotionSyncError as exc:
        raise FlowError(
            "Cannot complete workflow: workflow completion succeeded but promotion synchronization state could not be cleared. "
            f"{exc}"
        ) from exc
    _clear_diff_baseline_after_success(repo_root, operation="complete")

    if state.patch_description is not None:
        print(f"Completed patch: {state.patch_description}")
    else:
        assert state.active_issue_number is not None
        print(f"Completed issue {state.active_issue_number}")
    print("Workflow: inactive")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")
    return 0


def handle_block(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1 or not arguments[0].strip():
        raise _block_usage(command_name)

    reason = arguments[0].strip()
    repo_root, state_path, state = _resolve_repo_state_context()

    workflow_type = _active_workflow_type(state)
    if workflow_type is None:
        raise FlowError("Cannot block workflow: no active issue is set.")
    if workflow_type == "patch":
        raise FlowError("Cannot block workflow: patch workflows are not supported.")

    assert state.active_issue_number is not None
    assert state.main_branch != state.scratch_branch

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    if git_status_short(repo_root):
        raise FlowError("Cannot block workflow: repository must be clean.")

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    if comparison.scratch_ahead_of_main != 0 or comparison.scratch_behind_main != 0:
        raise FlowError(
            f"Cannot block workflow: {state.scratch_branch} must equal {state.main_branch}."
        )

    if state.checkpoint != 0:
        raise FlowError(
            f"Cannot block workflow: checkpoint must be 0 (current: {state.checkpoint})."
        )

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_before = load_blocked_workflows(blocked_file)
    if state.ticket_reference is None:
        # Compatibility path for legacy workflows created before ticket references
        # were persisted in workflow state.
        try:
            issue_title, issue_url, issue_labels = _resolve_issue_details_with_labels(state.active_issue_number)
        except FileNotFoundError as exc:
            raise FlowError("GitHub CLI (gh) is required for this command.") from exc

        record = BlockedWorkflowRecord(
            issue_number=state.active_issue_number,
            issue_title=issue_title,
            issue_url=issue_url,
            reason=reason,
            blocked_at=_now_utc_iso_timestamp(),
            ticket_reference=None,
        )
        upsert_blocked_workflow(blocked_file, record)

        try:
            _reconcile_github_workflow_label(state.active_issue_number, "blocked", issue_labels)
        except FlowError as exc:
            save_blocked_workflows(blocked_file, blocked_before)
            print(
                f"{command_name}: GitHub label reconciliation failed for #{state.active_issue_number}.",
                file=sys.stderr,
            )
            raise FlowError(
                f"Cannot block workflow: failed to synchronize GitHub labels for issue {state.active_issue_number}."
            ) from exc
    else:
        try:
            provider = resolve_ticket_provider_for_reference(
                repo_root=repo_root,
                reference=state.ticket_reference,
            )
        except TicketProviderError as exc:
            raise FlowError(str(exc)) from exc

        try:
            blocked_ticket = provider.block(state.ticket_reference, reason)
        except TicketProviderError as exc:
            raise FlowError(
                f"Cannot block workflow: failed to transition ticket {state.active_issue_number} to blocked. {exc}"
            ) from exc

        def _rollback_provider_to_active() -> str | None:
            try:
                provider.resume(state.ticket_reference)
            except TicketProviderError as exc:
                return str(exc)
            return None

        issue_url = blocked_ticket.reference.url or state.active_issue_url

        record = BlockedWorkflowRecord(
            issue_number=state.active_issue_number,
            issue_title=blocked_ticket.title,
            issue_url=issue_url,
            reason=reason,
            blocked_at=_now_utc_iso_timestamp(),
            ticket_reference=blocked_ticket.reference,
        )
        try:
            upsert_blocked_workflow(blocked_file, record)
        except BlockedWorkflowsError as exc:
            rollback_failure = _rollback_provider_to_active()
            if rollback_failure is None:
                raise FlowError(
                    "Cannot block workflow: failed to update blocked workflow registry."
                ) from exc
            raise FlowError(
                "Cannot block workflow: failed to update blocked workflow registry. "
                "Additional failures: provider rollback failed after registry failure: "
                f"{rollback_failure}"
            ) from exc

    inactive_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
    )
    try:
        save_state(state_path, inactive_state)
    except WorkflowStateError as exc:
        if state.ticket_reference is None:
            raise FlowError(
                "Cannot block workflow: failed to transition local workflow to inactive state."
            ) from exc

        try:
            provider = resolve_ticket_provider_for_reference(
                repo_root=repo_root,
                reference=state.ticket_reference,
            )
        except TicketProviderError as exc:
            raise FlowError(str(exc)) from exc
        def _rollback_provider_to_active() -> str | None:
            try:
                provider.resume(state.ticket_reference)
            except TicketProviderError as provider_exc:
                return str(provider_exc)
            return None

        blocked_restore_failure: str | None = None
        try:
            save_blocked_workflows(blocked_file, blocked_before)
        except BlockedWorkflowsError as blocked_exc:
            blocked_restore_failure = str(blocked_exc)

        rollback_failure = _rollback_provider_to_active()
        details: list[str] = []
        if blocked_restore_failure is not None:
            details.append(
                "failed to restore blocked workflow metadata after state-save failure: "
                f"{blocked_restore_failure}"
            )
        if rollback_failure is not None:
            details.append(
                "provider rollback failed after state-save failure: "
                f"{rollback_failure}"
            )

        if not details:
            raise FlowError(
                "Cannot block workflow: failed to transition local workflow to inactive state."
            ) from exc

        raise FlowError(
            "Cannot block workflow: failed to transition local workflow to inactive state. "
            "Additional failures: "
            + " | ".join(details)
        ) from exc

    _clear_diff_baseline_after_success(repo_root, operation="block")

    print(f"Blocked issue {state.active_issue_number}")
    print(f"reason: {reason}")
    print("Workflow: inactive")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")
    return 0


def handle_resume(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise _resume_usage(command_name)

    issue_text = arguments[0].strip()
    if not issue_text.isdigit() or int(issue_text) <= 0:
        raise FlowError("ticket-number must be a positive integer.")
    issue_number = int(issue_text)

    repo_root, state_path, state = _resolve_repo_state_context()
    _validate_stacked_resume(repo_root, state)

    if _active_workflow_type(state) is not None:
        if state.active_issue_number is not None:
            raise FlowError(
                f"Cannot resume workflow: active issue {state.active_issue_number} is already set."
            )
        assert state.patch_description is not None
        raise FlowError(
            f"Cannot resume workflow: active patch {state.patch_description} is already set."
        )

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    if git_status_short(repo_root):
        raise FlowError("Cannot resume workflow: repository must be clean.")

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    if comparison.scratch_ahead_of_main != 0 or comparison.scratch_behind_main != 0:
        raise FlowError(
            f"Cannot resume workflow: {state.scratch_branch} must equal {state.main_branch}."
        )

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_before = load_blocked_workflows(blocked_file)
    blocked_record = get_blocked_workflow(blocked_file, issue_number)
    if blocked_record is None:
        raise FlowError(
            f"Cannot resume workflow: no blocked record exists for issue {issue_number}."
        )

    stacked_resume = blocked_record.resume_metadata
    if stacked_resume is not None:
        if resolve_commit_hash(repo_root, state.main_branch) != stacked_resume["promotedMainCommit"]:
            raise FlowError("Cannot resume workflow: canonical main is not the recorded promoted commit.")
        if resolve_commit_hash(repo_root, state.scratch_branch) != stacked_resume["promotedMainCommit"]:
            raise FlowError("Cannot resume workflow: scratch is not synchronized with the recorded promoted commit.")
        if resolve_managed_ref(repo_root, stacked_resume["suspendedRefName"]) != stacked_resume["suspendedCommit"]:
            raise FlowError("Cannot resume workflow: suspended ref does not match its recorded checkpoint.")

    if blocked_record.ticket_reference is None:
        # Compatibility path for blocked records created before ticket
        # references were persisted.
        try:
            _, _, issue_labels = _resolve_issue_details_with_labels(issue_number)
        except FileNotFoundError as exc:
            raise FlowError("GitHub CLI (gh) is required for this command.") from exc

        issue_labels_before_active = list(issue_labels)

        def _resume_label_rollback_error() -> str | None:
            rollback_labels = list(issue_labels_before_active)
            if "active" not in rollback_labels:
                rollback_labels.append("active")
            try:
                _reconcile_github_workflow_label(issue_number, "blocked", rollback_labels)
            except FlowError as exc:
                return str(exc)
            except OSError as exc:
                message = str(exc).strip() or exc.__class__.__name__
                return (
                    "GitHub invocation error during rollback "
                    f"({exc.__class__.__name__}): {message}"
                )

        def _resume_transition_failure_message(
            *,
            primary_message: str,
            blocked_restore_failure_message: str | None = None,
        ) -> str:
            detail_messages: list[str] = []
            if blocked_restore_failure_message is not None:
                detail_messages.append(blocked_restore_failure_message)

            label_rollback_failure = _resume_label_rollback_error()
            if label_rollback_failure is not None:
                detail_messages.append(
                    "GitHub label rollback failed after local resume failure: "
                    f"{label_rollback_failure}"
                )

            if not detail_messages:
                return primary_message

            return primary_message + " Additional failures: " + " | ".join(detail_messages)

        _reconcile_github_workflow_label(issue_number, "active", issue_labels)

        resumed_state = WorkflowState(
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            checkpoint=stacked_resume["checkpoint"] if stacked_resume is not None else 0,
            active_issue_number=issue_number,
            active_issue_title=blocked_record.issue_title,
            active_issue_url=blocked_record.issue_url,
            ticket_reference=blocked_record.ticket_reference,
            stacked_resume=stacked_resume,
        )

        try:
            remove_blocked_workflow(blocked_file, issue_number)
        except BlockedWorkflowsError as exc:
            raise FlowError(
                _resume_transition_failure_message(
                    primary_message="Cannot resume workflow: failed to update blocked workflow registry.",
                )
            ) from exc

        try:
            save_state(state_path, resumed_state)
        except WorkflowStateError as exc:
            blocked_restore_failure_message: str | None = None
            try:
                save_blocked_workflows(blocked_file, blocked_before)
            except BlockedWorkflowsError:
                blocked_restore_failure_message = (
                    f"failed to restore blocked workflow metadata for #{issue_number}"
                )
            raise FlowError(
                _resume_transition_failure_message(
                    primary_message=f"Cannot resume workflow: failed to activate issue {issue_number}.",
                    blocked_restore_failure_message=blocked_restore_failure_message,
                )
            ) from exc
    else:
        try:
            provider = resolve_ticket_provider_for_reference(
                repo_root=repo_root,
                reference=blocked_record.ticket_reference,
            )
        except TicketProviderError as exc:
            raise FlowError(str(exc)) from exc

        try:
            resumed_ticket = provider.resume(blocked_record.ticket_reference)
        except TicketProviderError as exc:
            raise FlowError(
                f"Cannot resume workflow: failed to transition ticket {issue_number} to active. {exc}"
            ) from exc

        def _resume_provider_rollback_error() -> str | None:
            try:
                provider.block(blocked_record.ticket_reference, blocked_record.reason)
            except TicketProviderError as exc:
                return str(exc)
            return None

        resumed_state = WorkflowState(
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            checkpoint=stacked_resume["checkpoint"] if stacked_resume is not None else 0,
            active_issue_number=issue_number,
            active_issue_title=resumed_ticket.title,
            active_issue_url=resumed_ticket.reference.url,
            ticket_reference=resumed_ticket.reference,
            stacked_resume=stacked_resume,
        )

        try:
            remove_blocked_workflow(blocked_file, issue_number)
        except BlockedWorkflowsError as exc:
            rollback_failure = _resume_provider_rollback_error()
            if rollback_failure is None:
                raise FlowError(
                    "Cannot resume workflow: failed to update blocked workflow registry."
                ) from exc
            raise FlowError(
                "Cannot resume workflow: failed to update blocked workflow registry. "
                "Additional failures: provider rollback failed after registry failure: "
                f"{rollback_failure}"
            ) from exc

        try:
            save_state(state_path, resumed_state)
        except WorkflowStateError as exc:
            blocked_restore_failure_message: str | None = None
            try:
                save_blocked_workflows(blocked_file, blocked_before)
            except BlockedWorkflowsError:
                blocked_restore_failure_message = (
                    f"failed to restore blocked workflow metadata for #{issue_number}"
                )

            rollback_failure = _resume_provider_rollback_error()
            detail_messages: list[str] = []
            if blocked_restore_failure_message is not None:
                detail_messages.append(blocked_restore_failure_message)
            if rollback_failure is not None:
                detail_messages.append(
                    "provider rollback failed after local resume failure: "
                    f"{rollback_failure}"
                )

            if not detail_messages:
                raise FlowError(
                    f"Cannot resume workflow: failed to activate issue {issue_number}."
                ) from exc

            raise FlowError(
                f"Cannot resume workflow: failed to activate issue {issue_number}. "
                "Additional failures: " + " | ".join(detail_messages)
            ) from exc

    _clear_diff_baseline_after_success(repo_root, operation="resume")

    print(f"Resumed issue {issue_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print(
        "checkpoint: "
        f"{stacked_resume['checkpoint'] if stacked_resume is not None else 0}"
    )
    return 0


def handle_status(command_name: str, arguments: list[str]) -> int:
    verbose = False
    if arguments:
        if len(arguments) != 1:
            raise _status_usage(command_name)

        if arguments[0] not in {"-v", "--verbose"}:
            raise _status_usage(command_name)

        verbose = True

    repo_root, state_path, state = _resolve_repo_state_context()

    current_branch = _display_branch_name(current_branch_name(repo_root))
    status_lines = git_status_short(repo_root)
    (
        staged_paths,
        modified_paths,
        untracked_paths,
        staged_count,
        modified_count,
        untracked_count,
        working_tree,
    ) = _working_tree_details(status_lines)

    comparison = compare_main_and_scratch(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )
    relation_state, main_only_count, scratch_only_count = _branch_relation_info(comparison)

    workflow_type = _workflow_type(state)
    stacked_scope_checkpoint: int | None = None
    if state.stacked_handoff is not None:
        stacked_scope_checkpoint = commit_count_between(
            repo_root,
            ancestor_revision=state.stacked_handoff.inherited_base_commit,
            descendant_revision=state.scratch_branch,
        )

    if not verbose:
        if workflow_type == "issue" and state.active_issue_number is not None:
            if state.active_issue_title:
                print(f"Issue {state.active_issue_number} — {state.active_issue_title}")
            else:
                print(f"Issue {state.active_issue_number}")
        elif workflow_type == "patch" and state.patch_description is not None:
            print(f"Patch: {state.patch_description}")
        else:
            print("No active workflow.")
        print(f"Branch: {current_branch}")

        relationship_line = _relationship_line_for_default_status(
            relation_state,
            state.main_branch,
            state.scratch_branch,
            main_only_count,
            scratch_only_count,
        )
        if (
            state.stacked_handoff is not None
            and relation_state == "ahead"
            and stacked_scope_checkpoint == state.checkpoint
        ):
            relationship_line = ""

        has_deviation = False
        if workflow_type != "none" and current_branch != state.scratch_branch:
            has_deviation = True
        if relationship_line:
            has_deviation = True
        if staged_count > 0 or modified_count > 0 or untracked_count > 0:
            has_deviation = True

        checkpoint_conveyed = relation_state == "ahead" and (
            stacked_scope_checkpoint == state.checkpoint
            if state.stacked_handoff is not None
            else scratch_only_count == state.checkpoint
        )
        if state.checkpoint != 0 and not checkpoint_conveyed:
            has_deviation = True

        if not has_deviation:
            return 0

        print()
        print("Working tree:")
        if workflow_type != "none" and current_branch != state.scratch_branch:
            print(f"  Expected branch: {state.scratch_branch}")
        if relationship_line:
            print(f"  {relationship_line}")
        if staged_count > 0:
            _print_status_count_line(staged_count, "staged")
        if modified_count > 0:
            _print_status_count_line(modified_count, "modified")
        if untracked_count > 0:
            _print_status_count_line(untracked_count, "untracked")
        if state.checkpoint != 0 and not checkpoint_conveyed:
            print(f"  Checkpoint: {state.checkpoint}")
        return 0

    print("Workflow:")
    if workflow_type != "none":
        print("  state: active")
        print(f"  type: {workflow_type}")
        if workflow_type == "issue" and state.active_issue_number is not None:
            print(f"  issue number: {state.active_issue_number}")
            if state.active_issue_title:
                print(f"  issue title: {state.active_issue_title}")
            if state.active_issue_url:
                print(f"  issue URL: {state.active_issue_url}")
        elif workflow_type == "patch" and state.patch_description is not None:
            print(f"  patch: {state.patch_description}")
        print(f"  checkpoint: {state.checkpoint}")
    else:
        print("  state: inactive")
        print(f"  checkpoint: {state.checkpoint}")

    print("Repository:")
    print(f"  current branch: {current_branch}")
    print(f"  main branch: {state.main_branch}")
    print(f"  scratch branch: {state.scratch_branch}")
    physical_relationship = _relationship_line_for_verbose_status(
        relation_state,
        main_only_count,
        scratch_only_count,
        state.main_branch,
        state.scratch_branch,
    )
    if state.stacked_handoff is not None:
        physical_relationship += "; includes inherited A tree"
    print("  relation: " + physical_relationship)

    if state.stacked_handoff is not None:
        handoff = state.stacked_handoff
        print("Stacked workflow:")
        print(f"  active prerequisite: issue {state.active_issue_number}")
        print(
            "  suspended original: issue "
            f"{handoff.suspended_issue_number}"
        )
        print(f"  suspended checkpoint: {handoff.suspended_checkpoint}")
        print(f"  inherited base commit: {handoff.inherited_base_commit}")
        print(f"  managed suspended ref: {handoff.suspended_ref_name}")
        print(
            "  active scope: "
            f"checkpoint {state.checkpoint}, {stacked_scope_checkpoint} commit(s) from inherited base"
        )

    print("Working tree:")
    if working_tree == "clean":
        print("  clean")
    else:
        _print_status_path_category("staged", staged_paths)
        _print_status_path_category("modified", modified_paths)
        _print_status_path_category("untracked", untracked_paths)

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    if workflow_type == "issue" and state.active_issue_number is not None:
        duplicate_record = get_blocked_workflow(blocked_file, state.active_issue_number)
        if duplicate_record is not None:
            print("Validation:")
            print(
                "  invalid state: active issue "
                f"{state.active_issue_number} is also present in blocked workflows"
            )

    print("Blocked workflows:")
    for line in format_blocked_summary_lines(blocked_file):
        print(line)

    return 0


def handle_report(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise FlowError(f"Usage: {command_name} report")
    print(render_latest_copilot_report(resolve_repo_root()))
    return 0


def handle_ticket_create(command_name: str, arguments: list[str]) -> int:
    if not arguments:
        raise _ticket_create_usage(command_name)

    title = arguments[0]
    body: str | None = None
    acceptance_criteria: list[str] = []
    labels: list[str] = []

    index = 1
    while index < len(arguments):
        token = arguments[index]
        if token == "--body":
            if index + 1 >= len(arguments):
                raise _ticket_create_usage(command_name)
            body = arguments[index + 1]
            index += 2
            continue
        if token == "--acceptance":
            if index + 1 >= len(arguments):
                raise _ticket_create_usage(command_name)
            acceptance_criteria.append(arguments[index + 1])
            index += 2
            continue
        if token == "--label":
            if index + 1 >= len(arguments):
                raise _ticket_create_usage(command_name)
            labels.append(arguments[index + 1])
            index += 2
            continue

        raise _ticket_create_usage(command_name)

    repo_root = resolve_repo_root()
    provider = _resolve_ticket_provider_for_repo_root(repo_root)

    try:
        reference = provider.create(
            title=title,
            body=body,
            acceptance_criteria=tuple(acceptance_criteria),
            labels=tuple(labels),
        )
        ticket = provider.get(reference.ticket_id)
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    print(json.dumps(ticket.to_dict(), indent=2))
    return 0


def handle_ticket_show(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise _ticket_show_usage(command_name)

    ticket_id = arguments[0].strip()
    if not ticket_id:
        raise _ticket_show_usage(command_name)

    repo_root = resolve_repo_root()
    provider = _resolve_ticket_provider_for_repo_root(repo_root)

    try:
        ticket = provider.get(ticket_id)
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    print(json.dumps(ticket.to_dict(), indent=2))
    return 0


def handle_ticket_query(command_name: str, arguments: list[str]) -> int:
    lifecycle_state: str | None = None
    workflow_state: str | None = None
    labels: list[str] = []
    query_text: str | None = None

    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--lifecycle":
            if index + 1 >= len(arguments):
                raise _ticket_query_usage(command_name)
            lifecycle_state = _normalize_ticket_lifecycle_state(raw_value=arguments[index + 1])
            index += 2
            continue
        if token == "--workflow":
            if index + 1 >= len(arguments):
                raise _ticket_query_usage(command_name)
            workflow_state = _normalize_ticket_workflow_state(raw_value=arguments[index + 1])
            index += 2
            continue
        if token == "--label":
            if index + 1 >= len(arguments):
                raise _ticket_query_usage(command_name)
            labels.append(arguments[index + 1])
            index += 2
            continue
        if token == "--query":
            if index + 1 >= len(arguments):
                raise _ticket_query_usage(command_name)
            query_text = arguments[index + 1]
            index += 2
            continue

        raise _ticket_query_usage(command_name)

    repo_root = resolve_repo_root()
    provider = _resolve_ticket_provider_for_repo_root(repo_root)

    try:
        tickets = provider.query(
            lifecycle_state=lifecycle_state,
            workflow_state=workflow_state,
            labels=tuple(labels),
            query_text=query_text,
        )
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    print(json.dumps({"tickets": [ticket.to_dict() for ticket in tickets]}, indent=2))
    return 0


def require_test_mode(command_name: str, command: str) -> None:
    if os.environ.get("FLOW_TEST_MODE", "0") != "1":
        print_unknown_command(command_name, command)
        raise SystemExit(1)


def handle_test_route_args(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-route-args")
    first = arguments[0] if len(arguments) >= 1 else ""
    second = arguments[1] if len(arguments) >= 2 else ""
    print(f"arg1={first}")
    print(f"arg2={second}")
    return 0


def handle_test_invalid_policy(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-invalid-policy")
    raise FlowError(
        "Unknown operational config policy: bogus. Supported policies: strict, ignore."
    )


def handle_test_state_get(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-state-get")

    if arguments:
        raise FlowError(f"Usage: {command_name} __test-state-get")

    _, _, state = _resolve_repo_state_context()

    print(json.dumps(state.to_dict(), indent=2))
    return 0


def handle_test_state_set(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-state-set")

    if len(arguments) != 1 or not arguments[0]:
        raise FlowError(f"Usage: {command_name} __test-state-set <json>")

    try:
        payload = json.loads(arguments[0])
    except json.JSONDecodeError as exc:
        raise FlowError(
            "Invalid JSON payload for workflow state: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise FlowError(
            "Invalid workflow state in state payload: expected a JSON object."
        )

    state = normalize_and_validate(payload, context="state payload")
    repo_root = resolve_repo_root()
    ensure_local_state_excluded(repo_root)
    state_path = workflow_state_file_for_repo_root(repo_root)
    save_state(state_path, state)

    print(json.dumps(state.to_dict(), indent=2))
    return 0


def handle_test_state_clear(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-state-clear")

    if arguments:
        raise FlowError(f"Usage: {command_name} __test-state-clear")

    repo_root = resolve_repo_root()
    state_path = workflow_state_file_for_repo_root(repo_root)
    state = clear_state(state_path)

    print(json.dumps(state.to_dict(), indent=2))
    return 0


def _resolve_command_handler(handler_key: str):
    handlers = {
        "start": handle_start,
        "patch": handle_patch,
        "status": handle_status,
        "report": handle_report,
        "diff": handle_diff,
        "commit": handle_commit,
        "reset": handle_reset,
        "abandon": handle_abandon,
        "promote": handle_promote,
        "complete": handle_complete,
        "block": handle_block,
        "resume": handle_resume,
        "ticket-create": handle_ticket_create,
        "ticket-show": handle_ticket_show,
        "ticket-query": handle_ticket_query,
    }
    return handlers.get(handler_key)


def _dispatch_command(
    invocation_name: str,
    spec: CommandSpec,
    arguments: list[str],
    *,
    direct_executable_mode: bool = False,
) -> int:
    if len(arguments) == 1 and arguments[0] in {"-h", "--help"}:
        with _with_invocation_usage_context(
            invocation_name,
            spec.name,
            direct_executable_mode=direct_executable_mode,
        ):
            print_command_help(
                invocation_name,
                spec.name,
                direct_executable_mode=direct_executable_mode,
            )
        return 0

    handler = _resolve_command_handler(spec.handler_key)
    if handler is None:
        raise FlowError(
            f"Python implementation for '{spec.name}' is not available yet."
        )

    with _with_invocation_usage_context(
        invocation_name,
        spec.name,
        direct_executable_mode=direct_executable_mode,
    ):
        return handler(invocation_name, arguments)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command_name = resolve_command_name()

    if arguments and arguments[0] == _DIRECT_FLOW_ROUTE_TOKEN:
        if len(arguments) < 2:
            raise FlowError("Invalid internal flow executable invocation: missing command key.")

        direct_flow_command = arguments[1]
        spec = COMMAND_SPEC_BY_NAME.get(direct_flow_command)
        if (
            spec is None
            or spec.canonical_namespace != "flow"
            or not spec.fixed_prefixed_executable
        ):
            raise FlowError(
                f"Invalid internal flow executable command: {direct_flow_command}"
            )
        return _dispatch_command(
            command_name,
            spec,
            arguments[2:],
            direct_executable_mode=True,
        )

    if arguments and arguments[0] == "__test-state-get":
        return handle_test_state_get(command_name, arguments[1:])

    if arguments and arguments[0] == "__test-state-set":
        return handle_test_state_set(command_name, arguments[1:])

    if arguments and arguments[0] == "__test-state-clear":
        return handle_test_state_clear(command_name, arguments[1:])

    if arguments and arguments[0] == "__test-route-args":
        return handle_test_route_args(command_name, arguments[1:])

    if arguments and arguments[0] == "__test-invalid-policy":
        return handle_test_invalid_policy(command_name, arguments[1:])

    if not arguments:
        raise FlowError(
            "Unsupported invocation. Use a direct executable such as flow-status or flow-commit."
        )

    command = arguments[0]

    print_unknown_command(command_name, command)
    return 1


def run() -> None:
    try:
        status = main()
    except (
        BlockedWorkflowsError,
        FlowError,
        RepositoryError,
        WorkflowStateError,
    ) as exc:
        print(f"{resolve_command_name()}: {exc}", file=sys.stderr)
        status = 1

    raise SystemExit(status)


if __name__ == "__main__":
    run()
