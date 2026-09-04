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
    read_commit_boundary,
    compare_main_and_scratch,
    commit_count_between,
    create_managed_ref,
    compare_main_to_tracked_upstream,
    current_branch_name,
    delete_branch,
    ensure_branches_point_to_same_commit,
    ensure_local_state_excluded,
    ensure_no_active_git_operations,
    git_status_short,
    git_status_short_filtered,
    hard_reset_branch_to_revision,
    resolve_repo_root,
    resolve_commit_hash,
    resolve_revision_commit,
    resolve_short_commit_hash,
    resolve_tree_hash,
    restore_branch_to_revision,
    clear_diff_baseline_for_repo_root,
    max_numbered_checkpoint_between,
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
    add_worktree,
    merge_revision_fast_forward_only,
    set_pending_merge_message,
    merge_revision_no_fast_forward,
    unmerged_paths,
    merge_in_progress,
    remove_worktree,
    prune_worktrees,
    delete_branch_if_at_revision,
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
    Ticket,
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
    promotion_sync_record_path,
    clear_promotion_sync_record,
    save_promotion_sync_record,
)
from .copilot_report import render_latest_copilot_report
from .tickets import TicketReference
from .workspaces import (
    ClaimOccupiedError,
    MalformedClaim,
    WorkspaceError,
    canonical_ticket_key,
    LastPromotion,
    MalformedLock,
    PromotionLockError,
    PromotionLockHeldError,
    acquire_active_claim,
    acquire_promotion_lock,
    claim_is_live,
    claim_path,
    claims_directory,
    create_active_claim,
    default_workspace_path,
    describe_occupancy,
    verify_workspace_ticket_identity,
    effective_worktree_id,
    evaluate_claim,
    find_foreign_out_path,
    force_remove_claim,
    list_claim_files,
    list_worktrees,
    promote_claim,
    read_claim,
    read_claim_file,
    reclaim_stale_active_claim,
    registry_directory,
    relocate_config_for_workspace,
    release_claim,
    reserve_claim,
    force_release_promotion_lock,
    promotion_lock_owner_state,
    promotion_lock_path,
    read_last_promotion,
    read_promotion_lock,
    release_promotion_lock,
    validate_branch_name,
    workspace_branch_name,
    write_last_promotion,
    worktree_id_for_repo_root,
)


_DIRECT_FLOW_ROUTE_TOKEN = "__ai_dev_flow_exec__"

# The installed launchers live outside this checkout and route through the same
# token, so it is part of their contract rather than an internal detail.
DIRECT_FLOW_ROUTE_TOKEN = _DIRECT_FLOW_ROUTE_TOKEN
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
        name="workspace",
        description="Manage concurrent ticket workspaces backed by Git worktrees.",
        canonical_namespace="flow",
        order=115,
        handler_key="workspace",
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
    {command_name} start <issue-number> --adopt <commit-ish>

Begin independent work on an unblocked issue from main. The prerequisite form
is only for active issue A handing off to prerequisite B; it keeps A's current
scratch tree, starts B at checkpoint 0, and preserves A's checkpoint ownership.
The adopt form binds an existing commit that already contains main to the issue
from an idle workflow; it publishes nothing and does not reconcile the target.

Options:
  --prerequisite-for <active-issue>  Hand off from the named active issue.
  --adopt <commit-ish>               Adopt an existing commit as this issue's work.
  -h, --help                         Show this help.
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
    "workspace": """\
Usage: {command_name} add <ticket-id> [path]
       {command_name} adopt <ticket-id>
       {command_name} list
       {command_name} refresh
       {command_name} remove [path]
       {command_name} prune [--claim <ticket-id>]
       {command_name} unlock [--force] <holder-path>

Manage concurrent ticket workspaces. Each workspace is a linked Git worktree
with its own scratch branch and its own .ai-dev state, so ordinary status,
diff, checkpoint, reset, abandon, and completion act only on the workspace they
are run from. Ordinary single-workspace repositories never need these commands.

Subcommands:
  add <ticket-id> [path]  Reserve the ticket, create its worktree and branch,
                          then activate it. Defaults to a sibling directory.
  adopt <ticket-id>       Claim and activate the current worktree without
                          resetting its branch or working tree.
  list                    Show every claim and its live workspace path.
  refresh                 Merge the current main into this workspace's scratch
                          branch after main advanced. Never rebases, force-
                          updates, or changes main. Leaves a conflicted merge
                          in place for explicit resolution.
  remove [path]           Remove an inactive, clean workspace and its claim.
  prune [--claim <id>]    Remove claims whose worktree is gone or prunable.
                          --claim recovers one unreadable claim record.
  unlock <holder-path>    Release an abandoned promotion lock. Removal is
                          automatic only for a same-host process proven gone;
                          --force is required when liveness is undetermined.

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


def _resolve_repo_state_context(
    *, validate_identity: bool = True
) -> tuple[Path, Path, WorkflowState]:
    repo_root = resolve_repo_root()
    state_path = workflow_state_file_for_repo_root(repo_root)
    state = load_state(state_path)
    if state.stacked_handoff is not None:
        _validate_stacked_scope(repo_root, state)
    if validate_identity:
        _validate_workspace_ticket_identity(repo_root, state)
    return repo_root, state_path, state


def _validate_workspace_ticket_identity(repo_root: Path, state: WorkflowState) -> None:
    """Stop before acting on a ticket this worktree cannot prove it owns.

    Two workspaces acting on one ticket is the failure this ticket exists to
    prevent, and a session that reads contradictory identity must stop rather
    than pick a side. The registry-level workspace commands do not resolve
    state through here, so listing, pruning, unlocking, and removal remain
    available to repair whatever produced the contradiction.
    """
    permitted_keys = []
    handoff = state.stacked_handoff
    if handoff is not None and handoff.suspended_ticket_reference is not None:
        # A prerequisite handoff legitimately owns its suspended issue's claim.
        permitted_keys.append(canonical_ticket_key(handoff.suspended_ticket_reference))

    try:
        problem = verify_workspace_ticket_identity(
            repo_root,
            reference=state.ticket_reference,
            permitted_keys=permitted_keys,
        )
    except WorkspaceError as exc:
        raise FlowError(f"Cannot resolve workspace ticket identity: {exc}") from exc

    if problem is None:
        return

    workspace_command = _workspace_command_name(resolve_command_name())
    raise FlowError(
        f"Ambiguous workspace ticket identity: {problem.detail} "
        f"Nothing was changed. Inspect the workspaces with '{workspace_command} list' "
        "and repair the workspace-to-ticket association before running any ticket work."
    )


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


def _adopt_usage(command_name: str) -> FlowError:
    return _usage_error(command_name, "start", "<issue-number> --adopt <commit-ish>")


def _parse_adopt_start(command_name: str, arguments: list[str]) -> tuple[int, str] | None:
    if "--adopt" not in arguments:
        return None

    if "--prerequisite-for" in arguments:
        raise FlowError(
            "Cannot adopt workflow: --adopt and --prerequisite-for cannot be combined."
        )

    if len(arguments) != 3 or arguments[1] != "--adopt":
        raise _adopt_usage(command_name)

    issue_number = _parse_issue_number(command_name, [arguments[0]])

    revision = arguments[2].strip()
    if not revision:
        raise FlowError("Cannot adopt workflow: adopted revision cannot be empty.")

    return issue_number, revision


@dataclass(frozen=True)
class _AdoptionTarget:
    revision: str
    commit: str


def _resolve_adoption_target(
    repo_root: Path,
    *,
    main_branch: str,
    revision: str,
) -> _AdoptionTarget:
    """Resolve and validate an adoption target without mutating anything.

    Refuses anything Flow would otherwise have to reconcile. Adoption never
    fetches, merges, rebases, or cherry-picks to make a target usable.
    """
    try:
        adopted_commit = resolve_revision_commit(repo_root, revision)
    except RepositoryError as exc:
        raise FlowError(f"Cannot adopt workflow: {exc}") from exc

    try:
        main_commit = resolve_commit_hash(repo_root, main_branch)
    except RepositoryError as exc:
        raise FlowError(
            f"Cannot adopt workflow: cannot resolve {main_branch}. {exc}"
        ) from exc

    if adopted_commit == main_commit:
        raise FlowError(
            f"Cannot adopt workflow: adopted revision {revision} already equals "
            f"{main_branch}; there is no work to adopt."
        )

    if not branch_is_ancestor(
        repo_root,
        ancestor_revision=main_commit,
        descendant_revision=adopted_commit,
    ):
        raise FlowError(
            f"Cannot adopt workflow: {main_branch} is not an ancestor of adopted "
            f"revision {revision}. Adoption does not fetch, merge, rebase, or "
            f"otherwise reconcile the target."
        )

    return _AdoptionTarget(revision=revision, commit=adopted_commit)


def _validate_adoption_prerequisites(
    command_name: str,
    repo_root: Path,
    state: WorkflowState,
    issue_number: int,
) -> None:
    active_workflow_type = _active_workflow_type(state)
    if active_workflow_type is not None:
        if state.active_issue_number is not None:
            raise FlowError(
                f"Cannot adopt workflow: active issue {state.active_issue_number} is already set."
            )
        assert state.patch_description is not None
        raise FlowError(
            f"Cannot adopt workflow: active patch {state.patch_description} is already set."
        )

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    if get_blocked_workflow(blocked_file, issue_number) is not None:
        if "-" in command_name:
            prefix, _, _ = command_name.rpartition("-")
            resume_command = f"{prefix}-resume"
        else:
            resume_command = "flow-resume"
        raise FlowError(
            f"Cannot adopt workflow: issue {issue_number} is blocked. "
            f"Use {resume_command} {issue_number}."
        )

    _ensure_main_and_scratch_branches_differ(state)

    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")

    if git_status_short(repo_root):
        raise FlowError(
            "Cannot adopt workflow: working tree is not clean. Commit, stash, or "
            "remove changes before adopting."
        )

    ensure_no_active_git_operations(repo_root)


@dataclass(frozen=True)
class _AdoptionSnapshot:
    """Externally visible state captured before any adoption mutation."""

    branch: str
    scratch_commit: str | None
    files: tuple[tuple[Path, bytes | None], ...]


def _adoption_snapshot_paths(repo_root: Path) -> tuple[Path, ...]:
    return (
        workflow_state_file_for_repo_root(repo_root),
        _promotion_review_record_path(repo_root),
        promotion_sync_record_path(repo_root),
        diff_baseline_file_for_repo_root(repo_root),
    )


def _capture_adoption_snapshot(repo_root: Path, *, scratch_branch: str) -> _AdoptionSnapshot:
    branch = current_branch_name(repo_root)

    scratch_commit: str | None = None
    if branch_exists(repo_root, scratch_branch):
        scratch_commit = resolve_commit_hash(repo_root, scratch_branch)

    files = tuple(
        (path, path.read_bytes() if path.is_file() else None)
        for path in _adoption_snapshot_paths(repo_root)
    )

    return _AdoptionSnapshot(branch=branch, scratch_commit=scratch_commit, files=files)


def _restore_adoption_snapshot(
    repo_root: Path,
    snapshot: _AdoptionSnapshot,
    *,
    main_branch: str,
    scratch_branch: str,
) -> None:
    """Return the repository to its exact pre-adoption externally visible state."""
    # A checked-out branch cannot be force-updated or deleted, so step off it
    # first. Adoption supports running while scratch is checked out.
    if current_branch_name(repo_root) == scratch_branch:
        checkout_branch(repo_root, main_branch)

    if snapshot.scratch_commit is None:
        if branch_exists(repo_root, scratch_branch):
            delete_branch(repo_root, scratch_branch)
    else:
        create_or_reset_branch_from_source(
            repo_root,
            branch_name=scratch_branch,
            source_branch=snapshot.scratch_commit,
        )

    if current_branch_name(repo_root) != snapshot.branch:
        checkout_branch(repo_root, snapshot.branch)

    for path, payload in snapshot.files:
        if payload is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _roll_back_adoption(
    repo_root: Path,
    snapshot: _AdoptionSnapshot,
    *,
    main_branch: str,
    scratch_branch: str,
    provider: TicketProvider | None = None,
    pre_activation_ticket: Ticket | None = None,
    activation_attempted: bool = False,
) -> list[str]:
    """Best-effort restoration. Never raises, so the original error survives."""
    failures: list[str] = []

    # Compensation must use the PRE-activation reference and labels. The
    # post-activation ticket already carries "active", so restoring from it
    # would preserve activation instead of undoing it. Activation is also
    # compensated when mark_active raised: providers that edit labels before a
    # final read can fail after the remote side effect has landed.
    if provider is not None and activation_attempted and pre_activation_ticket is not None:
        try:
            provider.deactivate(
                pre_activation_ticket.reference,
                previous_labels=pre_activation_ticket.labels,
            )
        except (TicketProviderError, OSError) as exc:
            failures.append(f"provider activation rollback failed: {exc}")

    try:
        _restore_adoption_snapshot(
            repo_root,
            snapshot,
            main_branch=main_branch,
            scratch_branch=scratch_branch,
        )
    except (RepositoryError, OSError) as exc:
        failures.append(f"repository state rollback failed: {exc}")

    return failures


def _adoption_failure(message: str, rollback_failures: list[str]) -> FlowError:
    if not rollback_failures:
        return FlowError(message)
    return FlowError(message + " Rollback failures: " + " | ".join(rollback_failures))


def _handle_adopt_start(
    command_name: str,
    issue_number: int,
    revision: str,
) -> int:
    repo_root, state_path, state = _resolve_repo_state_context()

    _validate_adoption_prerequisites(command_name, repo_root, state, issue_number)

    target = _resolve_adoption_target(
        repo_root,
        main_branch=state.main_branch,
        revision=revision,
    )

    provider = _resolve_ticket_provider_for_repo_root(repo_root)
    try:
        ticket = provider.get(str(issue_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    if ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot adopt workflow: ticket {issue_number} is {ticket.lifecycle_state}."
        )

    # Derived before any mutation so an invalid prospective state cannot leave
    # the repository half-adopted.
    adopted_checkpoint = max_numbered_checkpoint_between(
        repo_root,
        base_revision=state.main_branch,
        head_revision=target.commit,
    )

    adopted_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=adopted_checkpoint,
        active_issue_number=issue_number,
        active_issue_title=ticket.title,
        active_issue_url=ticket.reference.url,
        ticket_reference=ticket.reference,
    )
    normalize_and_validate(
        adopted_state.to_dict(),
        context="start --adopt command",
    )

    # Everything below mutates. Adoption is all-or-nothing from here, so the
    # pre-adoption state is captured first and restored on any failure.
    snapshot = _capture_adoption_snapshot(repo_root, scratch_branch=state.scratch_branch)
    activation_attempted = False

    try:
        checkout_branch(repo_root, state.main_branch)
        create_or_reset_branch_from_source(
            repo_root,
            branch_name=state.scratch_branch,
            source_branch=target.commit,
        )
        checkout_branch(repo_root, state.scratch_branch)

        placed_commit = resolve_commit_hash(repo_root, state.scratch_branch)
        if placed_commit != target.commit:
            raise FlowError(
                f"Cannot adopt workflow: {state.scratch_branch} resolved to {placed_commit} "
                f"instead of the adopted commit {target.commit}."
            )

        # Set before the call: a provider may mutate remote labels and then
        # fail its read-back, so the attempt itself obliges compensation.
        activation_attempted = True
        try:
            activated_ticket = provider.mark_active(ticket.reference)
        except TicketProviderError as exc:
            raise FlowError(
                f"Cannot adopt workflow: failed to mark ticket {issue_number} active. {exc}"
            ) from exc

        _clear_promotion_review_record(repo_root)

        adopted_state = WorkflowState(
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            checkpoint=adopted_checkpoint,
            active_issue_number=issue_number,
            active_issue_title=activated_ticket.title,
            active_issue_url=activated_ticket.reference.url,
            ticket_reference=activated_ticket.reference,
        )
        adopted_state = normalize_and_validate(
            adopted_state.to_dict(),
            context="start --adopt command",
        )

        ensure_local_state_excluded(repo_root)
        save_state(state_path, adopted_state)
        try:
            clear_promotion_sync_record(repo_root)
        except PromotionSyncError as exc:
            raise FlowError(
                f"Cannot adopt workflow: could not clear stale promotion synchronization "
                f"state. {exc}"
            ) from exc
        clear_diff_baseline_for_repo_root(repo_root)
    except (
        FlowError,
        RepositoryError,
        WorkflowStateError,
        PromotionSyncError,
        JsonFileError,
        OSError,
    ) as exc:
        rollback_failures = _roll_back_adoption(
            repo_root,
            snapshot,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
            provider=provider,
            pre_activation_ticket=ticket,
            activation_attempted=activation_attempted,
        )
        raise _adoption_failure(str(exc), rollback_failures) from exc

    print(f"Adopted issue {issue_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print(f"checkpoint: {adopted_checkpoint}")
    print(f"adoptedCommit: {target.commit}")

    return 0


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


def _release_workspace_claim_for_state(repo_root: Path, state: WorkflowState) -> None:
    """Release this workspace's ticket claim when its workflow ends.

    Authorization is the worktree id: a workspace only ever gives back a claim
    it owns, so ending one workflow can never free another workspace's ticket.
    """
    if state.ticket_reference is None:
        return

    key = canonical_ticket_key(state.ticket_reference)
    try:
        record = read_claim(repo_root, key)
    except WorkspaceError as exc:
        print(f"Warning: could not read the workspace claim for {key}: {exc}", file=sys.stderr)
        return

    if record is None:
        return
    if isinstance(record, MalformedClaim):
        print(
            f"Warning: the workspace claim at {record.path} is unreadable and was kept: "
            f"{record.detail}",
            file=sys.stderr,
        )
        return

    try:
        owner = effective_worktree_id(repo_root)
    except WorkspaceError as exc:
        print(f"Warning: could not resolve this workspace identity: {exc}", file=sys.stderr)
        return

    if record.worktree_id != owner:
        print(
            f"Warning: the claim for {key} belongs to another workspace and was kept.",
            file=sys.stderr,
        )
        return

    try:
        release_claim(repo_root, key=key, token=record.token, worktree_id=owner)
    except WorkspaceError as exc:
        print(f"Warning: could not release the workspace claim for {key}: {exc}", file=sys.stderr)


def _release_claim_quietly(repo_root: Path, record) -> None:
    """Give back a claim this invocation created; never mask the real failure."""
    try:
        release_claim(
            repo_root,
            key=record.key,
            token=record.token,
            worktree_id=record.worktree_id,
        )
    except WorkspaceError as exc:
        print(
            f"Warning: could not release the workspace claim for {record.key}: {exc}",
            file=sys.stderr,
        )


def handle_start(command_name: str, arguments: list[str]) -> int:
    adopt_arguments = _parse_adopt_start(command_name, arguments)
    if adopt_arguments is not None:
        return _handle_adopt_start(command_name, *adopt_arguments)

    prerequisite_arguments = _parse_prerequisite_start(command_name, arguments)
    if prerequisite_arguments is not None:
        return _handle_prerequisite_start(command_name, *prerequisite_arguments)

    issue_number = _parse_issue_number(command_name, arguments)

    repo_root, state_path, state = _resolve_repo_state_context()
    active_workflow_type = _active_workflow_type(state)
    if active_workflow_type is not None:
        if state.active_issue_number is not None:
            raise FlowError(
                f"Cannot start workflow: active issue {state.active_issue_number} is already set."
            )
        assert state.patch_description is not None
        raise FlowError(
            f"Cannot start workflow: active patch {state.patch_description} is already set."
        )

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_record = get_blocked_workflow(blocked_file, issue_number)
    if blocked_record is not None:
        if "-" in command_name:
            prefix, _, _ = command_name.rpartition("-")
            resume_command = f"{prefix}-resume"
        else:
            resume_command = "flow-resume"
        raise FlowError(
            f"Cannot start workflow: issue {issue_number} is blocked. "
            f"Use {resume_command} {issue_number}."
        )

    _ensure_main_and_scratch_branches_differ(state)

    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")

    if git_status_short(repo_root):
        raise FlowError(
            "Working tree is not clean. Commit, stash, or remove changes before starting."
        )

    ensure_no_active_git_operations(repo_root)

    provider = _resolve_ticket_provider_for_repo_root(repo_root)
    try:
        ticket = provider.get(str(issue_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    if ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot start workflow: ticket {issue_number} is {ticket.lifecycle_state}."
        )

    # Claim the ticket before any repository or provider mutation so the same
    # ticket can never become two independent writable workspaces.
    claim_key = canonical_ticket_key(ticket.reference)
    try:
        requesting_worktree = effective_worktree_id(repo_root)
        occupancy = describe_occupancy(
            repo_root,
            claim_key,
            requesting_worktree_id=requesting_worktree,
        )
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc
    if occupancy is not None:
        raise FlowError(f"Cannot start workflow for ticket {issue_number}: {occupancy}")

    try:
        claim_record = acquire_active_claim(
            repo_root,
            reference=ticket.reference,
            worktree_id=worktree_id_for_repo_root(repo_root),
            workspace_path=repo_root,
            branch=state.scratch_branch,
        )
    except ClaimOccupiedError as exc:
        raise FlowError(f"Cannot start workflow for ticket {issue_number}: {exc}") from exc
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

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

    try:
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
    except (RepositoryError, FlowError):
        _release_claim_quietly(repo_root, claim_record)
        raise

    try:
        active_ticket = provider.mark_active(ticket.reference)
    except TicketProviderError as exc:
        _release_claim_quietly(repo_root, claim_record)
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
    try:
        save_state(state_path, issue_state)
    except WorkflowStateError as exc:
        # The ticket is already active with the provider, so the claim stays with
        # this workspace rather than being released into an inconsistent state.
        raise FlowError(
            f"Ticket {issue_number} was marked active but workflow state could not be "
            f"saved. This workspace keeps the ticket claim; rerun start from this "
            f"workspace after fixing local state persistence. {exc}"
        ) from exc
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

    # A prerequisite becomes the active ticket of this workspace, so it takes a
    # claim of its own. Without one it would look unclaimed to every other
    # workspace, which could then activate the same ticket a second time. The
    # suspended issue keeps its own claim: this workspace still owns it.
    prerequisite_key = canonical_ticket_key(prerequisite_ticket.reference)
    try:
        requesting_worktree = effective_worktree_id(repo_root)
        occupancy = describe_occupancy(
            repo_root,
            prerequisite_key,
            requesting_worktree_id=requesting_worktree,
        )
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc
    if occupancy is not None:
        raise FlowError(
            f"Cannot start prerequisite workflow for ticket {prerequisite_number}: {occupancy}"
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

    try:
        prerequisite_claim = acquire_active_claim(
            repo_root,
            reference=prerequisite_ticket.reference,
            worktree_id=worktree_id_for_repo_root(repo_root),
            workspace_path=repo_root,
            branch=state.scratch_branch,
        )
    except ClaimOccupiedError as exc:
        raise FlowError(
            f"Cannot start prerequisite workflow for ticket {prerequisite_number}: {exc}"
        ) from exc
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

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
        _release_claim_quietly(repo_root, prerequisite_claim)
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

    try:
        boundary = read_commit_boundary(repo_root, commit=commit_hash)
    except RepositoryError as exc:
        if "-" in command_name:
            prefix, _, _ = command_name.rpartition("-")
            status_command = f"{prefix}-status"
        else:
            status_command = "flow-status"
        raise FlowError(
            f"Checkpoint {next_checkpoint} commit was created but its committed "
            f"changed-path boundary could not be derived. Commit: {commit_hash}. "
            "No changed-path boundary is claimed. History was not changed. "
            f"Run {status_command} to inspect the created checkpoint. {exc}"
        ) from exc

    print(f"Created checkpoint {next_checkpoint}")
    print(f"commit: {commit_hash}")
    print(f"parent: {boundary.parent}")
    print(f"changed-paths: {len(boundary.changed_paths)}")
    for changed_path in boundary.changed_paths:
        print(f"  {changed_path}")
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


def _workspace_command_name(command_name: str) -> str:
    """The sibling workspace executable for whatever prefix is in use."""
    if "-" in command_name:
        prefix, _, _ = command_name.rpartition("-")
        return f"{prefix}-workspace"
    return "flow-workspace"


def _workspace_refresh_is_available(repo_root: Path, state: WorkflowState) -> bool:
    """Whether the workspace refresh recovery could actually run here.

    Refresh recovers a stale base only for a workflow that owns an active claim
    in this worktree. A claim-less workflow -- a patch, or workflow state seeded
    without a bound ticket -- must not be pointed at a command that would refuse.
    """
    try:
        _owned_active_claim_for_state(repo_root, state)
    except FlowError:
        return False
    return True


def _promote_stale_base_message(
    repo_root: Path,
    state: WorkflowState,
    comparison: BranchComparison,
    *,
    command_name: str,
    observed_main_commit: str,
    preflight_main_commit: str | None = None,
) -> str:
    """Explain a stale base without guessing at who caused it.

    The breadcrumb is advisory, so it is quoted only when it describes the exact
    transition actually observed. Otherwise the message still carries the branch
    facts, which are enough to act on.
    """
    ahead = comparison.scratch_ahead_of_main
    behind = comparison.scratch_behind_main
    lines = [
        f"Cannot promote workflow: {state.main_branch} advanced and this workspace's "
        f"base is stale.",
        f"  {state.scratch_branch} is {ahead if ahead is not None else 'an unknown number of'} "
        f"commit(s) ahead of and {behind if behind is not None else 'an unknown number of'} "
        f"commit(s) behind {state.main_branch}.",
        f"  {state.main_branch} is now at {observed_main_commit}.",
    ]
    if preflight_main_commit and preflight_main_commit != observed_main_commit:
        lines.append(
            f"  {state.main_branch} moved from {preflight_main_commit} to "
            f"{observed_main_commit} while this promotion was being prepared."
        )

    breadcrumb = read_last_promotion(repo_root)
    attributed = False
    if (
        breadcrumb is not None
        and breadcrumb.main_branch == state.main_branch
        and breadcrumb.commit_after == observed_main_commit
    ):
        # When main moved during this invocation the breadcrumb must also explain
        # that exact step; otherwise matching the observed head is enough.
        observed_a_move = (
            preflight_main_commit is not None
            and preflight_main_commit != observed_main_commit
        )
        if not observed_a_move or breadcrumb.commit_before == preflight_main_commit:
            attributed = True
            ticket = breadcrumb.ticket_key or "an unrecorded ticket"
            lines.append(
                f"  Promoted by workspace {breadcrumb.workspace_path} for {ticket} "
                f"at {breadcrumb.at}."
            )
    if not attributed:
        lines.append(
            "  The promoting workspace could not be identified from the promotion "
            "breadcrumb; inspect the workspaces with the workspace list command."
        )

    workspace_command = _workspace_command_name(command_name)
    lines.append(
        f"  Nothing was changed. Run {workspace_command} refresh to merge the current "
        f"{state.main_branch} into this workspace, then promote again."
    )
    return "\n".join(lines)


def _promote_stale_base_error(
    repo_root: Path,
    state: WorkflowState,
    comparison: BranchComparison,
    *,
    command_name: str,
    observed_main_commit: str,
    preflight_main_commit: str | None = None,
) -> str:
    """The refusal for a base that is no longer current.

    Concurrent workspaces get the stale-base explanation and the refresh
    recovery. A repository without a claim for this workflow keeps the ordinary
    branch-relationship refusal, so ordinary single-workflow promotion behaves
    exactly as it did before concurrent workspaces existed.
    """
    if not _workspace_refresh_is_available(repo_root, state):
        relationship_error = _promote_branch_relationship_error(comparison)
        if relationship_error:
            return relationship_error
    return _promote_stale_base_message(
        repo_root,
        state,
        comparison,
        command_name=command_name,
        observed_main_commit=observed_main_commit,
        preflight_main_commit=preflight_main_commit,
    )


def _promote_locked_revalidation(
    repo_root: Path,
    state: WorkflowState,
    *,
    command_name: str,
    preflight_main_commit: str | None,
):
    """Re-prove every promotion precondition while the lock is held.

    A preflight observation is only a hint: another workspace may advance main
    between that read and this one. Nothing here mutates anything, so a refusal
    from this point still leaves the workspace exactly as it was found.
    """
    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            "Cannot promote workflow: current branch "
            f"{current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)

    if git_status_short_filtered(repo_root, excluded_paths=[".ai-dev/"]):
        raise FlowError("Cannot promote workflow: repository must be clean.")

    observed_main_commit = resolve_commit_hash(repo_root, state.main_branch)

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
        raise FlowError(
            _promote_stale_base_error(
                repo_root,
                state,
                comparison,
                command_name=command_name,
                observed_main_commit=observed_main_commit,
                preflight_main_commit=preflight_main_commit,
            )
        )

    if comparison.scratch_behind_main:
        raise FlowError(
            _promote_stale_base_error(
                repo_root,
                state,
                comparison,
                command_name=command_name,
                observed_main_commit=observed_main_commit,
                preflight_main_commit=preflight_main_commit,
            )
        )

    relationship_error = _promote_branch_relationship_error(comparison)
    if relationship_error:
        raise FlowError(relationship_error)

    upstream = _require_tracked_upstream_preflight(repo_root, state)
    _require_valid_promotion_review_gate(repo_root, state)
    return comparison, upstream, observed_main_commit


# The lock is repository-wide, so more than promotion takes it. Contention has
# to name the operation the caller actually asked for; telling someone running
# workspace refresh that the workflow cannot be promoted sends them to the wrong
# command.
_PROMOTION_LOCK_FAILURE_SUMMARIES = {
    "promote": "Cannot promote workflow",
    "promote-sync-retry": "Cannot promote workflow",
    "refresh": "Cannot refresh workspace",
}


def _promotion_lock_failure_summary(operation: str) -> str:
    return _PROMOTION_LOCK_FAILURE_SUMMARIES.get(
        operation, f"Cannot complete {operation}"
    )


@contextmanager
def _held_promotion_lock(repo_root: Path, state: WorkflowState, *, operation: str):
    """Hold the repository-wide promotion lock for one canonical-main mutation.

    Contention fails closed and names the holder rather than waiting: two agents
    silently queueing behind one another is exactly the ambiguity this ticket
    exists to remove.
    """
    summary = _promotion_lock_failure_summary(operation)
    ticket_key = None
    if state.ticket_reference is not None:
        ticket_key = canonical_ticket_key(state.ticket_reference)

    try:
        record = acquire_promotion_lock(
            repo_root,
            worktree_id=effective_worktree_id(repo_root),
            workspace_path=repo_root,
            ticket_key=ticket_key,
            operation=operation,
        )
    except PromotionLockHeldError as exc:
        raise FlowError(
            f"{summary}: {exc} The promotion lock is serialized across workspaces; "
            "retry once that workspace finishes, or recover an abandoned lock with the "
            "workspace unlock command."
        ) from exc
    except PromotionLockError as exc:
        raise FlowError(f"{summary}: {exc}") from exc

    try:
        yield record
    finally:
        try:
            release_promotion_lock(repo_root, token=record.token)
        except PromotionLockError as exc:
            print(
                f"Warning: the promotion lock could not be released: {exc}",
                file=sys.stderr,
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

    # Read-only preflight. It is an early, cheap refusal and never authorizes a
    # mutation on its own; the locked revalidation below is what decides.
    preflight_main_commit: str | None = None
    if branch_exists(repo_root, state.main_branch):
        preflight_main_commit = resolve_commit_hash(repo_root, state.main_branch)

    retry_context = _pending_sync_retry_context(repo_root, state)
    if retry_context is not None:
        record, upstream = retry_context
        with _held_promotion_lock(repo_root, state, operation="promote-sync-retry"):
            retry_context = _pending_sync_retry_context(repo_root, state)
            if retry_context is None:
                raise FlowError(
                    "Cannot retry remote synchronization: the pending synchronization "
                    "state changed while the promotion lock was being acquired."
                )
            record, upstream = retry_context
            return _retry_pending_remote_synchronization(
                repo_root,
                state=state,
                record=record,
                upstream=upstream,
            )
    if not commit_message:
        raise _promote_usage(command_name)

    with _held_promotion_lock(repo_root, state, operation="promote") as _lock_record:
        return _promote_under_lock(
            command_name,
            repo_root=repo_root,
            state_path=state_path,
            state=state,
            commit_message=commit_message,
            current_branch=current_branch,
            preflight_main_commit=preflight_main_commit,
        )


def _promote_under_lock(
    command_name: str,
    *,
    repo_root: Path,
    state_path: Path,
    state: WorkflowState,
    commit_message: str,
    current_branch: str,
    preflight_main_commit: str | None,
) -> int:
    comparison, upstream, observed_main_commit = _promote_locked_revalidation(
        repo_root,
        state,
        command_name=command_name,
        preflight_main_commit=preflight_main_commit,
    )

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

        # Written only here, on the success path, while the lock is still held.
        try:
            write_last_promotion(
                repo_root,
                commit_before=original_main_commit,
                commit_after=commit_hash,
                workspace_path=repo_root,
                ticket_key=(
                    canonical_ticket_key(state.ticket_reference)
                    if state.ticket_reference is not None
                    else None
                ),
                main_branch=state.main_branch,
            )
        except WorkspaceError as exc:
            print(
                f"Warning: promotion succeeded but its breadcrumb was not recorded: {exc}",
                file=sys.stderr,
            )

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
    _release_workspace_claim_for_state(repo_root, state)
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

    _release_workspace_claim_for_state(repo_root, state)

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


def _workspace_usage(command_name: str) -> FlowError:
    return _usage_error(
        command_name,
        "workspace",
        "<add|adopt|list|refresh|remove|prune|unlock> ...",
    )


def _workspace_reference_for_ticket_id(repo_root: Path, ticket_id: str) -> TicketReference:
    """Build a canonical reference from local configuration, without network I/O."""
    try:
        configuration = load_ticket_configuration_for_repo_root(repo_root)
    except TicketConfigError as exc:
        raise FlowError(str(exc)) from exc

    provider = getattr(configuration, "provider", "")
    if provider == "github":
        return TicketReference(
            provider="github",
            ticket_id=ticket_id,
            repository=getattr(configuration, "repository"),
        )
    if provider == "local":
        return TicketReference(provider="local", ticket_id=ticket_id)
    raise FlowError(
        "Cannot derive a workspace identity for provider "
        f"'{provider}'. Pass the claim filename instead."
    )


def _workspace_state_for_root(workspace_root: Path) -> WorkflowState:
    return load_state(workflow_state_file_for_repo_root(workspace_root))


def _print_workspace_residue(residue: list) -> None:
    print("Preserved artifacts that must be resolved manually:", file=sys.stderr)
    for item in residue:
        print(f"  {item}", file=sys.stderr)


def _rollback_workspace_creation(
    repo_root: Path,
    record,
    *,
    workspace_path: Path,
    branch_name: str,
    branch_commit: str,
    worktree_created: bool,
) -> list:
    """Undo only what this invocation created; stop at the first failure."""
    residue: list = []
    claim_note = (
        f"claim {claim_path(repo_root, record.key)} (release token {record.token})"
    )

    if worktree_created:
        try:
            remove_worktree(repo_root, workspace_path=workspace_path)
        except RepositoryError as exc:
            residue.append(f"worktree {workspace_path}: {exc}")
            residue.append(f"branch {branch_name}")
            residue.append(claim_note)
            return residue

    if branch_commit:
        try:
            delete_branch_if_at_revision(
                repo_root,
                branch_name=branch_name,
                expected_commit=branch_commit,
            )
        except RepositoryError as exc:
            residue.append(f"branch {branch_name}: {exc}")
            residue.append(claim_note)
            return residue

    try:
        release_claim(
            repo_root,
            key=record.key,
            token=record.token,
            worktree_id=record.worktree_id,
        )
    except WorkspaceError as exc:
        residue.append(f"{claim_note}: {exc}")

    return residue


def _seed_workspace_ai_dev(
    *,
    source_root: Path,
    workspace_root: Path,
    main_branch: str,
    scratch_branch: str,
) -> str:
    """Copy configuration into the new workspace and write inactive state."""
    source_config_path = config_file_for_repo_root(source_root)
    target_config_path = config_file_for_repo_root(workspace_root)
    detail = "no configuration to seed"

    if source_config_path.exists():
        try:
            payload = load_json_object(source_config_path, missing_default={})
        except JsonFileError as exc:
            raise FlowError(f"Cannot read source configuration: {exc}") from exc

        relocation = relocate_config_for_workspace(
            payload,
            source_root=source_root,
            target_root=workspace_root,
        )
        detail = relocation.detail
        try:
            write_json_object_atomic(target_config_path, relocation.config)
        except JsonFileError as exc:
            raise FlowError(f"Cannot seed workspace configuration: {exc}") from exc

    inactive_state = WorkflowState(
        main_branch=main_branch,
        scratch_branch=scratch_branch,
        checkpoint=0,
    )
    save_state(workflow_state_file_for_repo_root(workspace_root), inactive_state)
    return detail


def _handle_workspace_add(command_name: str, arguments: list[str]) -> int:
    if not arguments or len(arguments) > 2:
        raise _workspace_usage(command_name)

    issue_number = _parse_issue_number(command_name, arguments[:1])
    explicit_path = arguments[1].strip() if len(arguments) == 2 else ""
    if len(arguments) == 2 and not explicit_path:
        raise _workspace_usage(command_name)

    repo_root, _, state = _resolve_repo_state_context()
    ensure_no_active_git_operations(repo_root)

    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")

    provider = _resolve_ticket_provider_for_repo_root(repo_root)
    try:
        ticket = provider.get(str(issue_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    if ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot add workspace: ticket {issue_number} is {ticket.lifecycle_state}."
        )

    reference = ticket.reference
    key = canonical_ticket_key(reference)
    branch_name = workspace_branch_name(reference)
    try:
        validate_branch_name(repo_root, branch_name)
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

    # Ticket-level occupancy is the most informative refusal, so it is checked
    # before any path or branch precondition.
    try:
        occupancy = describe_occupancy(repo_root, key)
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc
    if occupancy is not None:
        raise FlowError(f"Cannot add workspace for ticket {issue_number}: {occupancy}")

    if explicit_path:
        workspace_path = Path(os.path.abspath(os.path.expanduser(explicit_path)))
    else:
        try:
            workspace_path = default_workspace_path(repo_root, reference)
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc

    # Every precondition is checked before the reservation so an ordinary
    # refusal never leaves a claim behind.
    if workspace_path.exists():
        if not workspace_path.is_dir() or any(workspace_path.iterdir()):
            raise FlowError(
                f"Cannot add workspace: {workspace_path} already exists and is not empty."
            )

    try:
        worktrees = list_worktrees(repo_root)
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc
    for entry in worktrees:
        if _workspace_path_conflicts(workspace_path, entry.path):
            raise FlowError(
                f"Cannot add workspace: {workspace_path} is inside the existing "
                f"worktree at {entry.path}."
            )

    if branch_exists(repo_root, branch_name):
        raise FlowError(
            f"Cannot add workspace: branch {branch_name} already exists."
        )

    # Phase A: reserve before creating anything.
    try:
        record = reserve_claim(
            repo_root,
            reference=reference,
            intended_path=workspace_path,
            intended_branch=branch_name,
        )
    except ClaimOccupiedError as exc:
        raise FlowError(f"Cannot add workspace for ticket {issue_number}: {exc}") from exc
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

    # Phase B: create the branch and worktree.
    worktree_created = False
    branch_commit = ""
    try:
        add_worktree(
            repo_root,
            workspace_path=workspace_path,
            branch_name=branch_name,
            source_branch=state.main_branch,
        )
        worktree_created = True
        branch_commit = resolve_commit_hash(repo_root, branch_name)
    except RepositoryError as exc:
        residue = _rollback_workspace_creation(
            repo_root,
            record,
            workspace_path=workspace_path,
            branch_name=branch_name,
            branch_commit=branch_commit,
            worktree_created=worktree_created,
        )
        if residue:
            print(f"Cannot add workspace: {exc}", file=sys.stderr)
            _print_workspace_residue(residue)
            return 1
        raise FlowError(f"Cannot add workspace: {exc}") from exc

    # Phase C: promote the reservation to an active claim.
    promoted = None
    promotion_error = None
    for _ in range(2):
        try:
            worktree_id = effective_worktree_id(workspace_path)
            promoted = promote_claim(
                repo_root,
                record=record,
                worktree_id=worktree_id,
                workspace_path=workspace_path,
            )
            break
        except WorkspaceError as exc:
            promotion_error = exc
    if promoted is None:
        print(
            f"Cannot add workspace: claim promotion failed. {promotion_error}",
            file=sys.stderr,
        )
        _print_workspace_residue(
            [
                f"worktree {workspace_path}",
                f"branch {branch_name}",
                f"reservation {claim_path(repo_root, record.key)} (release token {record.token})",
            ]
        )
        return 1

    # Phase D: seed and activate. Artifacts are preserved on failure.
    try:
        seed_detail = _seed_workspace_ai_dev(
            source_root=repo_root,
            workspace_root=workspace_path,
            main_branch=state.main_branch,
            scratch_branch=branch_name,
        )

        # Activation belongs to the new workspace, so it is written through that
        # workspace's provider. A repository-local ticket store would otherwise
        # be modified inside this worktree's working tree, leaking one ticket's
        # activation into the other ticket's uncommitted work.
        workspace_provider = _resolve_ticket_provider_for_repo_root(workspace_path)
        try:
            active_ticket = workspace_provider.mark_active(reference)
        except TicketProviderError as exc:
            raise FlowError(
                f"failed to mark ticket {issue_number} active. {exc}"
            ) from exc

        active_state = WorkflowState(
            main_branch=state.main_branch,
            scratch_branch=branch_name,
            checkpoint=0,
            active_issue_number=issue_number,
            active_issue_title=active_ticket.title,
            active_issue_url=active_ticket.reference.url,
            ticket_reference=active_ticket.reference,
        )
        active_state = normalize_and_validate(
            active_state.to_dict(),
            context="workspace add command",
        )
        save_state(workflow_state_file_for_repo_root(workspace_path), active_state)
    except (FlowError, WorkflowStateError, RepositoryError) as exc:
        print(f"Cannot add workspace: {exc}", file=sys.stderr)
        _print_workspace_residue(
            [
                f"worktree {workspace_path}",
                f"branch {branch_name}",
                f"active claim {claim_path(repo_root, promoted.key)}",
                f"resolve with: {command_name} remove {workspace_path}",
            ]
        )
        return 1

    print(f"Added workspace for issue {issue_number}")
    print(f"path: {workspace_path}")
    print(f"branch: {branch_name}")
    print(f"mainBranch: {state.main_branch}")
    print("checkpoint: 0")
    print(f"claim: {claim_path(repo_root, promoted.key)}")
    print(f"config: {seed_detail}")
    return 0


def _workspace_path_conflicts(candidate: Path, existing: Path) -> bool:
    candidate_text = os.path.normcase(os.path.abspath(str(candidate)))
    existing_text = os.path.normcase(os.path.abspath(str(existing)))
    if candidate_text == existing_text:
        return True
    return candidate_text.startswith(existing_text.rstrip(os.sep) + os.sep)


def _register_claims_for_active_workflow(
    *,
    repo_root: Path,
    state: WorkflowState,
    issue_number: int,
) -> int:
    """Record the claim a workspace's existing active workflow already implies.

    A workflow started before this repository had a claim registry owns its
    ticket in every way except the record, which leaves that ticket free for a
    second workspace to activate. Registering it changes no branch, index,
    working tree, workflow state, or ticket provider state.
    """
    if state.active_issue_number != issue_number or state.ticket_reference is None:
        raise FlowError(
            "Cannot adopt workspace: this workspace already has an active workflow."
        )

    references = [state.ticket_reference]
    handoff = state.stacked_handoff
    if handoff is not None and handoff.suspended_ticket_reference is not None:
        # The suspended issue is owned here too, and is just as exposed.
        references.append(handoff.suspended_ticket_reference)

    registered: list[str] = []
    already_owned: list[str] = []
    for reference in references:
        key = canonical_ticket_key(reference)
        try:
            requesting_worktree = effective_worktree_id(repo_root)
            occupancy = describe_occupancy(
                repo_root,
                key,
                requesting_worktree_id=requesting_worktree,
            )
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc
        if occupancy is not None:
            raise FlowError(f"Cannot adopt workspace for ticket {key}: {occupancy}")

        existing = read_claim(repo_root, key)
        if (
            existing is not None
            and not isinstance(existing, MalformedClaim)
            and existing.status == "active"
            and existing.worktree_id == requesting_worktree
        ):
            already_owned.append(key)
            continue

        # A claim naming a worktree that is gone or prunable is stale, and
        # leaving it in place keeps this workspace's ownership unprovable.
        # Adopt is the documented repair, so it re-registers that record here.
        try:
            reclaim_stale_active_claim(
                repo_root,
                reference=reference,
                worktree_id=worktree_id_for_repo_root(repo_root),
                workspace_path=repo_root,
                branch=state.scratch_branch,
            )
        except ClaimOccupiedError as exc:
            raise FlowError(f"Cannot adopt workspace for ticket {key}: {exc}") from exc
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc
        registered.append(key)

    if not registered:
        print(f"Workspace already owns the claim for issue {issue_number}")
        for key in already_owned:
            print(f"claim: {key}")
        print("No changes were made.")
        return 0

    print(f"Registered workspace ownership for issue {issue_number}")
    for key in registered:
        print(f"claim: {key}")
    for key in already_owned:
        print(f"already claimed: {key}")
    print(f"branch: {state.scratch_branch}")
    print(f"checkpoint: {state.checkpoint}")
    return 0


def _handle_workspace_adopt(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise _workspace_usage(command_name)

    issue_number = _parse_issue_number(command_name, arguments)
    # Adoption is how an unproven association is repaired, so it reads state
    # without demanding the identity it is about to establish.
    repo_root, state_path, state = _resolve_repo_state_context(validate_identity=False)

    if _active_workflow_type(state) is not None:
        return _register_claims_for_active_workflow(
            repo_root=repo_root, state=state, issue_number=issue_number
        )

    _ensure_main_and_scratch_branches_differ(state)
    _ensure_main_and_scratch_branches_exist(repo_root, state)
    ensure_no_active_git_operations(repo_root)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot adopt workspace: current branch {current_branch} does not match "
            f"scratchBranch {state.scratch_branch}."
        )

    config_path = config_file_for_repo_root(repo_root)
    if config_path.exists():
        try:
            config_payload = load_json_object(config_path, missing_default={})
        except JsonFileError as exc:
            raise FlowError(f"Cannot adopt workspace: {exc}") from exc
        try:
            foreign = find_foreign_out_path(
                repo_root,
                config_payload,
                workspace_root=repo_root,
            )
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc
        if foreign is not None:
            raise FlowError(
                "Cannot adopt workspace: configured out path "
                f"{foreign.out_path} is inside another worktree at "
                f"{foreign.owning_worktree.path}. Point out at a path inside this "
                "workspace so the two workspaces do not share one artifact."
            )

    provider = _resolve_ticket_provider_for_repo_root(repo_root)
    try:
        ticket = provider.get(str(issue_number))
    except TicketProviderError as exc:
        raise FlowError(str(exc)) from exc

    if ticket.lifecycle_state != "open":
        raise FlowError(
            f"Cannot adopt workspace: ticket {issue_number} is {ticket.lifecycle_state}."
        )

    reference = ticket.reference
    key = canonical_ticket_key(reference)
    try:
        requesting_worktree = effective_worktree_id(repo_root)
        occupancy = describe_occupancy(
            repo_root,
            key,
            requesting_worktree_id=requesting_worktree,
        )
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc
    if occupancy is not None:
        raise FlowError(f"Cannot adopt workspace for ticket {issue_number}: {occupancy}")

    try:
        record = reclaim_stale_active_claim(
            repo_root,
            reference=reference,
            worktree_id=worktree_id_for_repo_root(repo_root),
            workspace_path=repo_root,
            branch=state.scratch_branch,
        )
    except ClaimOccupiedError as exc:
        raise FlowError(f"Cannot adopt workspace for ticket {issue_number}: {exc}") from exc
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

    checkpoint = max_numbered_checkpoint_relative_to_main(
        repo_root,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
    )

    try:
        active_ticket = provider.mark_active(reference)
    except TicketProviderError as exc:
        # Nothing outside the registry changed yet, so give the claim back.
        try:
            release_claim(
                repo_root,
                key=key,
                token=record.token,
                worktree_id=record.worktree_id,
            )
        except WorkspaceError as release_exc:
            print(
                f"Warning: could not release the workspace claim for {key}: {release_exc}",
                file=sys.stderr,
            )
        raise FlowError(
            f"Cannot adopt workspace: failed to mark ticket {issue_number} active. {exc}"
        ) from exc

    adopted_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=checkpoint,
        active_issue_number=issue_number,
        active_issue_title=active_ticket.title,
        active_issue_url=active_ticket.reference.url,
        ticket_reference=active_ticket.reference,
    )
    adopted_state = normalize_and_validate(
        adopted_state.to_dict(),
        context="workspace adopt command",
    )
    try:
        save_state(state_path, adopted_state)
    except WorkflowStateError as exc:
        # The provider already marks the ticket active, so the claim stays with
        # this workspace; retrying adopt here reuses it rather than colliding.
        raise FlowError(
            f"Ticket {issue_number} was marked active but workflow state could not be "
            f"saved. This workspace keeps the ticket claim; rerun adopt from this "
            f"workspace after fixing local state persistence. {exc}"
        ) from exc
    ensure_local_state_excluded(repo_root)

    print(f"Adopted issue {issue_number} into this workspace")
    print(f"path: {repo_root}")
    print(f"branch: {state.scratch_branch}")
    print(f"mainBranch: {state.main_branch}")
    print(f"checkpoint: {checkpoint}")
    print(f"claim: {claim_path(repo_root, key)}")
    return 0


def _handle_workspace_list(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _workspace_usage(command_name)

    repo_root = resolve_repo_root()
    current_id = effective_worktree_id(repo_root)

    try:
        claim_files = list_claim_files(repo_root)
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

    print(f"Registry: {registry_directory(repo_root)}")
    if not claim_files:
        print("No workspace claims.")
        return 0

    for path in claim_files:
        entry = read_claim_file(path)
        if entry is None:
            continue
        if isinstance(entry, MalformedClaim):
            print(f"- {path.name}")
            print(f"    state: occupied (unreadable: {entry.detail})")
            continue

        status = evaluate_claim(repo_root, entry, path)
        marker = " (current)" if entry.worktree_id == current_id else ""
        print(f"- {entry.key}{marker}")
        print(f"    state: {status.state}")
        print(f"    branch: {entry.intended_branch or 'unknown'}")
        print(f"    path: {status.live_path or entry.intended_path or 'unknown'}")
        print(f"    worktreeId: {entry.worktree_id or 'pending'}")
        if status.detail:
            print(f"    detail: {status.detail}")
    return 0


def _handle_workspace_remove(command_name: str, arguments: list[str]) -> int:
    if len(arguments) > 1:
        raise _workspace_usage(command_name)

    repo_root = resolve_repo_root()
    if arguments:
        target_argument = arguments[0].strip()
        if not target_argument:
            raise _workspace_usage(command_name)
        target_root = Path(os.path.abspath(os.path.expanduser(target_argument)))
    else:
        target_root = repo_root

    try:
        worktrees = list_worktrees(repo_root)
    except WorkspaceError as exc:
        raise FlowError(str(exc)) from exc

    target_entry = None
    for entry in worktrees:
        if os.path.normcase(str(entry.path)) == os.path.normcase(str(target_root)):
            target_entry = entry
            break

    if target_entry is None:
        raise FlowError(f"Cannot remove workspace: {target_root} is not a registered worktree.")
    if target_entry.is_primary:
        raise FlowError("Cannot remove workspace: the primary worktree is not removable.")

    target_state = _workspace_state_for_root(target_entry.path)
    if _active_workflow_type(target_state) is not None:
        raise FlowError(
            "Cannot remove workspace: it still has an active workflow. "
            "Complete or abandon it first."
        )

    if git_status_short(target_entry.path):
        raise FlowError("Cannot remove workspace: its working tree is not clean.")

    branch_name = target_entry.branch
    if branch_name:
        comparison = compare_main_and_scratch(
            repo_root,
            main_branch=target_state.main_branch,
            scratch_branch=branch_name,
        )
        if comparison.scratch_ahead_of_main:
            raise FlowError(
                f"Cannot remove workspace: {branch_name} is ahead of "
                f"{target_state.main_branch}; preserve or promote that work first."
            )

    matching_claim = None
    for path in list_claim_files(repo_root):
        entry = read_claim_file(path)
        if isinstance(entry, MalformedClaim) or entry is None:
            continue
        if entry.worktree_id and entry.worktree_id == target_entry.worktree_id:
            matching_claim = entry
            break

    branch_commit = ""
    if branch_name and branch_exists(repo_root, branch_name):
        branch_commit = resolve_commit_hash(repo_root, branch_name)

    try:
        remove_worktree(repo_root, workspace_path=target_entry.path)
    except RepositoryError as exc:
        raise FlowError(f"Cannot remove workspace: {exc}") from exc

    removed_branch = False
    if branch_name and branch_commit:
        try:
            removed_branch = delete_branch_if_at_revision(
                repo_root,
                branch_name=branch_name,
                expected_commit=branch_commit,
            )
        except RepositoryError as exc:
            print(f"Removed the worktree, but the branch was preserved: {exc}", file=sys.stderr)

    released = False
    if matching_claim is not None:
        try:
            released = release_claim(
                repo_root,
                key=matching_claim.key,
                token=matching_claim.token,
                worktree_id=target_entry.worktree_id,
            )
        except WorkspaceError as exc:
            print(f"Removed the worktree, but the claim was preserved: {exc}", file=sys.stderr)

    print(f"Removed workspace {target_entry.path}")
    print(f"branch removed: {'yes' if removed_branch else 'no'}")
    print(f"claim released: {'yes' if released else 'no'}")
    return 0


def _ensure_bare_claim_selector(selector: str) -> None:
    """Refuse anything that could name a path outside the claims directory.

    The selector is joined onto the claims directory, where an absolute path
    replaces that directory outright and a traversal component escapes it. Prune
    then deletes whatever the join produced, so the shape is proved here, before
    any join, and the selector is required to be a bare filename or identifier.
    """
    separators = [character for character in (os.sep, os.altsep, "/") if character]
    if any(separator in selector for separator in separators):
        raise FlowError(
            "Cannot prune claim: the claim selector must be a bare claim filename or "
            f"ticket identifier, not a path. Got {selector!r}."
        )
    if selector in {os.curdir, os.pardir} or selector.startswith("~"):
        raise FlowError(
            "Cannot prune claim: the claim selector must be a bare claim filename or "
            f"ticket identifier. Got {selector!r}."
        )
    if Path(selector).is_absolute() or len(Path(selector).parts) != 1:
        raise FlowError(
            "Cannot prune claim: the claim selector must be a bare claim filename or "
            f"ticket identifier, not a path. Got {selector!r}."
        )


def _handle_workspace_prune(command_name: str, arguments: list[str]) -> int:
    claim_selector = ""
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--claim":
            raise _workspace_usage(command_name)
        claim_selector = arguments[1].strip()
        if not claim_selector:
            raise _workspace_usage(command_name)

    repo_root = resolve_repo_root()

    if claim_selector:
        _ensure_bare_claim_selector(claim_selector)
        if claim_selector.endswith(".json") or (
            len(claim_selector) == 64
            and all(character in "0123456789abcdef" for character in claim_selector)
        ):
            filename = claim_selector if claim_selector.endswith(".json") else f"{claim_selector}.json"
            target_path = claims_directory(repo_root) / filename
        else:
            reference = _workspace_reference_for_ticket_id(repo_root, claim_selector)
            target_path = claim_path(repo_root, canonical_ticket_key(reference))

        if not target_path.exists():
            raise FlowError(f"Cannot prune claim: no claim record at {target_path}.")

        existing = read_claim_file(target_path)
        if existing is not None and not isinstance(existing, MalformedClaim):
            if claim_is_live(repo_root, existing, target_path):
                status = evaluate_claim(repo_root, existing, target_path)
                location = status.live_path or existing.intended_path or "its workspace"
                raise FlowError(
                    "Cannot prune claim: the claim for "
                    f"{existing.key} is live and owned by workspace {location}. "
                    "A blocked workflow keeps its claim on purpose. Recover it from "
                    f"that workspace with resume {existing.ticket_id}, then end it with "
                    "the appropriate lifecycle command such as abandon."
                )

        try:
            force_remove_claim(target_path)
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc
        print(f"Removed claim record {target_path}")
        return 0

    removed = 0
    for path in list_claim_files(repo_root):
        entry = read_claim_file(path)
        if entry is None:
            continue
        if isinstance(entry, MalformedClaim):
            print(f"Kept unreadable claim {path.name}: {entry.detail}")
            print(f"  recover with: {command_name} prune --claim {path.stem}")
            continue
        status = evaluate_claim(repo_root, entry, path)
        if status.state != "stale":
            continue
        try:
            force_remove_claim(path)
        except WorkspaceError as exc:
            raise FlowError(str(exc)) from exc
        removed += 1
        print(f"Removed stale claim {entry.key}: {status.detail}")

    if removed == 0:
        print("No stale claims.")
    return 0


REFRESH_MERGE_SUBJECT_PREFIX = "Refresh workspace base from"


def _owned_active_claim_for_state(repo_root: Path, state: WorkflowState):
    """The claim this workspace owns for its active ticket, or a refusal."""
    if state.ticket_reference is None:
        raise FlowError(
            "Cannot refresh workspace: the active workflow has no bound ticket, so it "
            "owns no workspace claim."
        )

    key = canonical_ticket_key(state.ticket_reference)
    try:
        record = read_claim(repo_root, key)
    except WorkspaceError as exc:
        raise FlowError(f"Cannot refresh workspace: {exc}") from exc

    if record is None:
        raise FlowError(
            f"Cannot refresh workspace: no workspace claim exists for {key}."
        )
    if isinstance(record, MalformedClaim):
        raise FlowError(
            f"Cannot refresh workspace: the claim at {record.path} is unreadable "
            f"({record.detail})."
        )
    if record.status != "active":
        raise FlowError(
            f"Cannot refresh workspace: the claim for {key} is not active."
        )

    try:
        owner = effective_worktree_id(repo_root)
    except WorkspaceError as exc:
        raise FlowError(f"Cannot refresh workspace: {exc}") from exc

    if record.worktree_id != owner:
        raise FlowError(
            f"Cannot refresh workspace: the claim for {key} is owned by workspace "
            f"{record.intended_path or record.worktree_id}, not this one."
        )
    return record


def _handle_workspace_refresh(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _workspace_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot refresh workspace: no active workflow is set.")

    _owned_active_claim_for_state(repo_root, state)

    _ensure_main_and_scratch_branches_differ(state)
    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot refresh workspace: current branch {current_branch} does not match "
            f"scratchBranch {state.scratch_branch}."
        )

    ensure_no_active_git_operations(repo_root)

    excluded_paths = [".ai-dev/"]
    if git_status_short_filtered(repo_root, excluded_paths=excluded_paths):
        raise FlowError(
            "Cannot refresh workspace: the index and working tree must be clean."
        )

    # Read-only preflight. Only the locked reread below decides anything.
    preflight_main_commit = resolve_commit_hash(repo_root, state.main_branch)

    with _held_promotion_lock(repo_root, state, operation="refresh"):
        return _refresh_under_lock(
            command_name,
            repo_root=repo_root,
            state=state,
            preflight_main_commit=preflight_main_commit,
        )


def _refresh_under_lock(
    command_name: str,
    *,
    repo_root: Path,
    state: WorkflowState,
    preflight_main_commit: str,
) -> int:
    # Everything is re-proved under the lock; main may have moved since preflight.
    _ensure_main_and_scratch_branches_exist(repo_root, state)
    ensure_no_active_git_operations(repo_root)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot refresh workspace: current branch {current_branch} does not match "
            f"scratchBranch {state.scratch_branch}."
        )

    if git_status_short_filtered(repo_root, excluded_paths=[".ai-dev/"]):
        raise FlowError(
            "Cannot refresh workspace: the index and working tree must be clean."
        )

    observed_main_commit = resolve_commit_hash(repo_root, state.main_branch)

    # Staleness is measured only against main. Another workspace's scratch branch
    # advancing has no bearing on whether this workspace needs a refresh.
    if branch_is_ancestor(
        repo_root,
        ancestor_revision=observed_main_commit,
        descendant_revision=state.scratch_branch,
    ):
        print(f"Workspace is current with {state.main_branch}")
        print(f"{state.main_branch}: {observed_main_commit}")
        print("No changes were made.")
        return 0

    if observed_main_commit != preflight_main_commit:
        print(
            f"Note: {state.main_branch} moved from {preflight_main_commit} to "
            f"{observed_main_commit} while the promotion lock was being acquired; "
            "refreshing onto the newer commit.",
            file=sys.stderr,
        )

    # A workspace that is only behind has nothing of its own to reconcile.
    # Recording a merge there would leave it ahead of main with an empty commit,
    # which no lifecycle command can then resolve, so it is moved forward
    # instead. Nothing is rewritten: the branch follows its own ancestry.
    scratch_is_contained_in_main = branch_is_ancestor(
        repo_root,
        ancestor_revision=state.scratch_branch,
        descendant_revision=observed_main_commit,
    )

    short_main = resolve_short_commit_hash(repo_root, observed_main_commit)
    # A non-numeric subject keeps numbered-checkpoint inference unchanged.
    merge_subject = (
        f"{REFRESH_MERGE_SUBJECT_PREFIX} {state.main_branch} {short_main}"
    )

    # Evidence bound to the old base cannot survive a merge that moves the base.
    _clear_promotion_review_record(repo_root)
    try:
        clear_diff_baseline_for_repo_root(repo_root)
    except RepositoryError as exc:
        raise FlowError(
            f"Cannot refresh workspace: review baseline could not be cleared. {exc}"
        ) from exc

    try:
        if scratch_is_contained_in_main:
            merge_revision_fast_forward_only(repo_root, revision=observed_main_commit)
        else:
            merge_revision_no_fast_forward(
                repo_root,
                revision=observed_main_commit,
                message=merge_subject,
            )
    except RepositoryError as exc:
        conflicted = unmerged_paths(repo_root)
        if conflicted or merge_in_progress(repo_root):
            # The merge itself is left exactly as Git produced it. Only the
            # prepared message is replaced, so finishing the merge records the
            # intended non-numeric subject however the user runs git commit.
            try:
                set_pending_merge_message(repo_root, message=merge_subject)
            except RepositoryError as message_exc:
                print(
                    f"Warning: could not prepare the merge message: {message_exc}",
                    file=sys.stderr,
                )
            print(
                f"Cannot refresh workspace: merging {state.main_branch} "
                f"({observed_main_commit}) into {state.scratch_branch} conflicted.",
                file=sys.stderr,
            )
            if conflicted:
                print("Conflicted paths:", file=sys.stderr)
                for path_text in conflicted:
                    print(f"  {path_text}", file=sys.stderr)
            workspace_command = _workspace_command_name(command_name)
            print(
                "The merge was left in progress for explicit resolution. Resolve the "
                "conflicts and run 'git commit' to finish it, or run 'git merge --abort' "
                f"to return to the previous base and rerun {workspace_command} refresh.",
                file=sys.stderr,
            )
            print(
                "This workspace keeps its ticket claim, workflow state, and checkpoint. "
                "Review evidence tied to the previous base was cleared and must be "
                "earned again.",
                file=sys.stderr,
            )
            return 1
        raise FlowError(f"Cannot refresh workspace: {exc}") from exc

    if not branch_is_ancestor(
        repo_root,
        ancestor_revision=observed_main_commit,
        descendant_revision=state.scratch_branch,
    ):
        raise FlowError(
            "Cannot refresh workspace: the merge completed but "
            f"{state.main_branch} is still not an ancestor of {state.scratch_branch}."
        )

    print(f"Refreshed {state.scratch_branch} from {state.main_branch}")
    print(f"{state.main_branch}: {observed_main_commit}")
    if scratch_is_contained_in_main:
        print("fast-forwarded: no merge commit was needed")
    else:
        print(f"merge commit: {resolve_commit_hash(repo_root, 'HEAD')}")
    print(f"checkpoint: {state.checkpoint}")
    print(
        "Review evidence tied to the previous base was cleared; re-run review before "
        "promoting."
    )
    return 0


def _handle_workspace_unlock(command_name: str, arguments: list[str]) -> int:
    force = False
    remaining = list(arguments)
    if remaining and remaining[0] == "--force":
        force = True
        remaining = remaining[1:]
    if len(remaining) != 1 or not remaining[0].strip():
        raise _workspace_usage(command_name)

    holder_path = remaining[0].strip()
    repo_root = resolve_repo_root()

    current = read_promotion_lock(repo_root)
    if current is None:
        raise FlowError(
            f"There is no promotion lock to release at {promotion_lock_path(repo_root)}."
        )
    if not isinstance(current, MalformedLock):
        owner_state = promotion_lock_owner_state(current)
        print(f"Promotion lock holder: {current.describe()}")
        print(f"owner state: {owner_state}")

    try:
        released = force_release_promotion_lock(
            repo_root,
            holder_path=holder_path,
            force=force,
        )
    except PromotionLockError as exc:
        raise FlowError(str(exc)) from exc

    if released is None:
        print("Removed an unreadable promotion lock record.")
    else:
        print(f"Released the promotion lock held by {released.workspace_path}")
    return 0


def handle_workspace(command_name: str, arguments: list[str]) -> int:
    if not arguments:
        raise _workspace_usage(command_name)

    subcommand = arguments[0]
    rest = arguments[1:]

    if subcommand == "add":
        return _handle_workspace_add(command_name, rest)
    if subcommand == "adopt":
        return _handle_workspace_adopt(command_name, rest)
    if subcommand == "list":
        return _handle_workspace_list(command_name, rest)
    if subcommand == "remove":
        return _handle_workspace_remove(command_name, rest)
    if subcommand == "refresh":
        return _handle_workspace_refresh(command_name, rest)
    if subcommand == "prune":
        return _handle_workspace_prune(command_name, rest)
    if subcommand == "unlock":
        return _handle_workspace_unlock(command_name, rest)

    raise _workspace_usage(command_name)


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
        "workspace": handle_workspace,
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
