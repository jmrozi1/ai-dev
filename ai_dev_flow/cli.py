from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    current_branch_name,
    ensure_branches_point_to_same_commit,
    ensure_local_state_excluded,
    ensure_no_active_git_operations,
    git_status_short,
    git_status_short_filtered,
    hard_reset_branch_to_revision,
    resolve_repo_root,
    resolve_commit_hash,
    resolve_short_commit_hash,
    resolve_tree_hash,
    restore_branch_to_revision,
    max_numbered_checkpoint_relative_to_main,
    squash_merge_branch_into_current,
    stage_all_changes,
    sync_local_excludes,
    workflow_state_file_for_repo_root,
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
    WorkflowState,
    WorkflowStateError,
    clear_state,
    load_state,
    normalize_and_validate,
    save_state,
)


_DIRECT_FLOW_ROUTE_TOKEN = "__ai_dev_flow_exec__"


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
)

COMMAND_SPEC_BY_NAME: dict[str, CommandSpec] = {
    spec.name: spec for spec in COMMAND_SPECS
}

FLOW_LIFECYCLE_COMMANDS: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
    if spec.canonical_namespace == "flow" and spec.help_visible
)

FIXED_FLOW_EXECUTABLE_COMMANDS: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
    if spec.canonical_namespace == "flow" and spec.fixed_prefixed_executable
)

COMMAND_HELP: dict[str, str] = {
    "start": """\
Usage: {command_name} start <issue-number>

Begin new work on an unblocked issue by resetting scratch to main,
checking out scratch, and recording the active issue.

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
        "diff": """\
Usage: {command_name} diff [--all] [--stdout]

Show read-only diff output for the active workflow without modifying index,
working tree, workflow state, or checkpoint state.

Options:
    --all      Include committed workflow changes since main plus current changes.
    --stdout   Explicitly select stdout delivery (the only implemented delivery mode).
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
    "promote": """\
Usage: {command_name} promote "<commit-message>"

Squash the complete scratch change into one permanent commit on main, then
reset scratch to the promoted main commit.

Options:
  -h, --help  Show this help.
""",
    "complete": """\
Usage: {command_name} complete

Clear the active local workflow after scratch and main are synchronized.

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

Reactivate a blocked issue workflow as the local active issue.

Options:
  -h, --help  Show this help.
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
        "Run one of the direct lifecycle executables (for example flow-status --help) for usage.",
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
    return repo_root, state_path, state


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


def _resolve_issue_metadata(issue_number: int) -> tuple[str, str]:
    completed = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "title,url"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return "", ""

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "", ""

    title = payload.get("title")
    url = payload.get("url")
    return (title.strip() if isinstance(title, str) else "", url.strip() if isinstance(url, str) else "")


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


def handle_start(command_name: str, arguments: list[str]) -> int:
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

    issue_title = ""
    issue_url = ""
    try:
        issue_title, issue_url = _resolve_issue_metadata(issue_number)
    except FileNotFoundError:
        issue_title, issue_url = "", ""

    # Validate prospective state before any git mutation.
    issue_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=issue_title or None,
        active_issue_url=issue_url or None,
    )
    issue_state = normalize_and_validate(
        issue_state.to_dict(),
        context="start command",
    )

    _ensure_main_and_scratch_branches_differ(state)

    if not branch_exists(repo_root, state.main_branch):
        raise FlowError(f"Main branch does not exist locally: {state.main_branch}")

    if git_status_short(repo_root):
        raise FlowError(
            "Working tree is not clean. Commit, stash, or remove changes before starting."
        )

    ensure_no_active_git_operations(repo_root)

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

    ensure_local_state_excluded(repo_root)
    save_state(state_path, issue_state)

    print(f"Started issue {issue_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")

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
    ensure_local_state_excluded(repo_root)
    save_state(state_path, patch_state)

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
    return _usage_error(command_name, "diff", "[--all] [--stdout]")


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
    if len(arguments) != 1:
        raise _promote_usage(command_name)

    commit_message = arguments[0].strip()
    if not commit_message:
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

        updated_state = replace(state, checkpoint=0)
        save_state(state_path, updated_state)
        workflow_state_updated = True

        if current_branch_name(repo_root) != state.scratch_branch:
            raise FlowError(
                "Cannot promote workflow: scratch branch was not checked out after promotion."
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


def _flow_diff_workflow_changes(repo_root: Path, *, main_branch: str, scratch_branch: str) -> str:
    diff_completed = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--binary", "--no-ext-diff", f"{main_branch}...{scratch_branch}"],
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


def _parse_diff_options(command_name: str, arguments: list[str]) -> tuple[bool, bool]:
    include_all = False
    use_stdout = False

    for option in arguments:
        if option == "--all":
            if include_all:
                raise FlowError("--all may be provided at most once.")
            include_all = True
            continue
        if option == "--stdout":
            if use_stdout:
                raise FlowError("--stdout may be provided at most once.")
            use_stdout = True
            continue
        raise _diff_usage(command_name)

    return include_all, use_stdout


def handle_diff(command_name: str, arguments: list[str]) -> int:
    include_all, _ = _parse_diff_options(command_name, arguments)

    repo_root, _, state = _resolve_repo_state_context()
    if _active_workflow_type(state) is None:
        raise FlowError("Cannot diff workflow: no active workflow is set.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)

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

    committed_diff = ""
    if include_all:
        committed_diff = _flow_diff_workflow_changes(
            repo_root,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
        )

    combined = "".join(
        part
        for part in (
            committed_diff,
            staged_diff,
            unstaged_diff,
            untracked_diff,
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
        hard_reset_branch_to_revision(
            repo_root,
            branch_name=state.scratch_branch,
            revision=state.main_branch,
        )
        clean_untracked_non_ignored(repo_root)
        ensure_branches_point_to_same_commit(
            repo_root,
            left_branch=state.main_branch,
            right_branch=state.scratch_branch,
        )
    except RepositoryError as exc:
        raise FlowError(f"Git reset failed: {exc}") from exc

    updated_state = replace(state, checkpoint=0)
    try:
        save_state(state_path, updated_state)
    except WorkflowStateError as exc:
        raise FlowError(
            "Scratch was reset but workflow state could not be saved. "
            f"{exc}"
        ) from exc

    print(f"Reset {state.scratch_branch} to {state.main_branch}")
    print("checkpoint: 0")
    if state.patch_description is not None:
        print(f"patch: {state.patch_description}")
    else:
        assert state.active_issue_number is not None
        print(f"activeIssueNumber: {state.active_issue_number}")
    return 0


def handle_complete(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _complete_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

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

    if state.checkpoint != 0:
        raise FlowError(
            f"Cannot complete workflow: checkpoint must be 0 (current: {state.checkpoint})."
        )

    if state.active_issue_number is not None:
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

    inactive_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
    )
    save_state(state_path, inactive_state)

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

    try:
        issue_title, issue_url, issue_labels = _resolve_issue_details_with_labels(state.active_issue_number)
    except FileNotFoundError as exc:
        raise FlowError("GitHub CLI (gh) is required for this command.") from exc

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_before = load_blocked_workflows(blocked_file)

    record = BlockedWorkflowRecord(
        issue_number=state.active_issue_number,
        issue_title=issue_title,
        issue_url=issue_url,
        reason=reason,
        blocked_at=_now_utc_iso_timestamp(),
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

    inactive_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
    )
    save_state(state_path, inactive_state)

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
        return None

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
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=blocked_record.issue_title,
        active_issue_url=blocked_record.issue_url,
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

    print(f"Resumed issue {issue_number}")
    print(f"mainBranch: {state.main_branch}")
    print(f"scratchBranch: {state.scratch_branch}")
    print("checkpoint: 0")
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

        has_deviation = False
        if workflow_type != "none" and current_branch != state.scratch_branch:
            has_deviation = True
        if relationship_line:
            has_deviation = True
        if staged_count > 0 or modified_count > 0 or untracked_count > 0:
            has_deviation = True

        checkpoint_conveyed = relation_state == "ahead" and scratch_only_count == state.checkpoint
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
    print(
        "  relation: "
        + _relationship_line_for_verbose_status(
            relation_state,
            main_only_count,
            scratch_only_count,
            state.main_branch,
            state.scratch_branch,
        )
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
        "diff": handle_diff,
        "commit": handle_commit,
        "reset": handle_reset,
        "promote": handle_promote,
        "complete": handle_complete,
        "block": handle_block,
        "resume": handle_resume,
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
