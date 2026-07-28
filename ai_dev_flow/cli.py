from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO

from .config import (
    ConfigError,
    get_out,
    set_out,
    unset_out,
    validate_config_key,
)
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
    config_file_for_repo_root,
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
from .review import ReviewError, resolve_review_output_path
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


TOP_LEVEL_HELP = """\
Usage: {command_name} <command> [options]

Manage an issue-focused development workflow using permanent main history
and disposable scratch checkpoints.

Commands:
  start      Begin work on an issue and reset scratch from main.
	patch      Begin or adopt a local patch workflow on scratch.
  status     Show the active issue and current repository state.
  review     Generate the cumulative change package for review.
  commit     Create the next numbered checkpoint on scratch.
  reset      Discard scratch work and restore it from main.
  promote    Squash scratch into one permanent commit on main.
  complete   Clear the completed local workflow.
	block      Block the active issue workflow and release the active slot.
	resume     Resume a previously blocked issue workflow.
  get        Read a repository setting.
  set        Change a repository setting.
  unset      Remove a repository setting.
  help       Show this help.

Run `{command_name} <command> --help` for command-specific help.
"""


COMMAND_HELP: dict[str, str] = {
    "start": """\
Usage: {command_name} start <issue-number>

Begin work on an issue by resetting scratch to main, checking out scratch,
and recording the active issue.

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
    "review": """\
Usage: {command_name} review

Generate a cumulative review package for all scratch and working-tree
changes relative to main.

Options:
  -h, --help  Show this help.
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

Resume a blocked issue workflow and restore it as the local active issue.

Options:
    -h, --help  Show this help.
""",
    "get": """\
Usage: {command_name} get out

Show the configured operational output destination.

Options:
  -h, --help  Show this help.
""",
    "set": """\
Usage: {command_name} set out=<path>

Configure operational command output to replace the specified file.

Options:
  -h, --help  Show this help.
""",
    "unset": """\
Usage: {command_name} unset out

Remove the configured operational output destination.

Options:
  -h, --help  Show this help.
""",
    "help": """\
Usage: {command_name} help

Show top-level command help.

Options:
  -h, --help  Show this help.
""",
}


KNOWN_COMMANDS = frozenset(
    {
        "start",
        "patch",
        "status",
        "review",
        "commit",
        "reset",
        "promote",
        "complete",
        "block",
        "resume",
        "get",
        "set",
        "unset",
    }
)


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


def print_top_level_help(command_name: str) -> None:
    print(TOP_LEVEL_HELP.format(command_name=command_name), end="")


def print_command_help(command_name: str, command: str) -> None:
    template = COMMAND_HELP.get(command)
    if template is None:
        raise FlowError(f"Unknown command help target: {command}")

    if command in {"block", "resume"}:
        template = template.replace("\n    -h, --help", "\n  -h, --help")

    print(template.format(command_name=command_name), end="")


def print_unknown_command(command_name: str, command: str) -> None:
    print(f"{command_name}: unknown command: {command}", file=sys.stderr)
    print(f"Run {command_name} help for usage.", file=sys.stderr)


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


def expand_home_prefix(path_value: str) -> str:
    if path_value == "~":
        return str(Path.home())

    if path_value.startswith("~/"):
        return str(Path.home() / path_value[2:])

    return path_value


def resolve_output_destination(repo_root: Path, configured_path: str) -> Path:
    expanded = expand_home_prefix(configured_path)
    candidate = Path(expanded)
    if candidate.is_absolute():
        return candidate
    return repo_root / candidate


def write_routed_output(
    command_name: str,
    temporary_output: Path,
    destination: Path,
) -> bool:
    parent = destination.parent
    if not parent.is_dir():
        print(
            f"{command_name}: Cannot write output to {destination}: parent directory does not exist: {parent}",
            file=sys.stderr,
        )
        print(
            f"{command_name}: Generated output preserved at {temporary_output}",
            file=sys.stderr,
        )
        return False

    try:
        destination.write_text(temporary_output.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        print(f"{command_name}: Cannot write output to {destination}", file=sys.stderr)
        print(
            f"{command_name}: Generated output preserved at {temporary_output}",
            file=sys.stderr,
        )
        return False

    try:
        temporary_output.unlink()
    except OSError:
        pass

    return True


def run_operational_command(
    command_name: str,
    config_error_policy: str,
    handler,
    arguments: list[str],
    *,
    echo_routed_output: bool = False,
) -> int:
    if config_error_policy not in {"strict", "ignore"}:
        raise FlowError(
            f"Unknown operational config policy: {config_error_policy}. Supported policies: strict, ignore."
        )

    repo_root = resolve_repo_root_if_available()
    if repo_root is None:
        return handler(command_name, arguments)

    configured_out: str | None = None
    config_path = config_file_for_repo_root(repo_root)
    try:
        configured_out = get_out(config_path)
    except ConfigError:
        if config_error_policy == "ignore":
            configured_out = None
        else:
            raise

    if not configured_out:
        return handler(command_name, arguments)

    stream = StringIO()
    status = 0
    with redirect_stdout(stream):
        try:
            status = handler(command_name, arguments)
        except Exception:
            print(stream.getvalue(), end="")
            raise

    captured_output = stream.getvalue()

    if status != 0:
        if captured_output:
            print(captured_output, end="")
        return status

    if echo_routed_output and captured_output:
        print(captured_output, end="")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"{command_name}.output.",
        delete=False,
    ) as handle:
        handle.write(captured_output)
        temp_output = Path(handle.name)

    destination = resolve_output_destination(repo_root, configured_out)
    if write_routed_output(command_name, temp_output, destination):
        print(f"Output written to {destination}")
        return 0

    if not echo_routed_output and captured_output:
        print(captured_output, end="")

    return 1


def handle_get(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise FlowError(f"Usage: {command_name} get out")

    key = arguments[0]
    validate_config_key("get", key)

    repo_root = resolve_repo_root()
    config_path = config_file_for_repo_root(repo_root)
    value = get_out(config_path)

    if value is None:
        print("out: not configured")
    else:
        print(value)

    return 0


def handle_set(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1 or "=" not in arguments[0]:
        raise FlowError(f"Usage: {command_name} set out=<path>")

    key, value = arguments[0].split("=", 1)
    validate_config_key("set", key)

    repo_root = resolve_repo_root()
    config_path = config_file_for_repo_root(repo_root)
    configured_value = set_out(config_path, value)
    sync_local_excludes(repo_root, configured_output=configured_value)

    print(f"out: {configured_value}")
    return 0


def handle_unset(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise FlowError(f"Usage: {command_name} unset out")

    key = arguments[0]
    validate_config_key("unset", key)

    repo_root = resolve_repo_root()
    config_path = config_file_for_repo_root(repo_root)
    unset_out(config_path)
    sync_local_excludes(repo_root)

    print("out: not configured")
    return 0


def _patch_usage(command_name: str) -> FlowError:
    return FlowError(
        f"Usage: {command_name} patch [--adopt] \"<description>\""
    )


def _start_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} start <issue-number>")


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

    issue_title = ""
    issue_url = ""
    try:
        issue_title, issue_url = _resolve_issue_metadata(issue_number)
    except FileNotFoundError:
        issue_title, issue_url = "", ""

    issue_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=issue_title or None,
        active_issue_url=issue_url or None,
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
    return FlowError(f"Usage: {command_name} status [-v|--verbose]")


def _review_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} review")


def _commit_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} commit")


def _reset_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} reset")


def _complete_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} complete")


def _promote_usage(command_name: str) -> FlowError:
    return FlowError(f'Usage: {command_name} promote "<commit-message>"')


def _block_usage(command_name: str) -> FlowError:
    return FlowError(f'Usage: {command_name} block "<reason>"')


def _resume_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} resume <ticket-number>")


def _promote_excluded_paths(
    repo_root: Path,
    configured_output: str | None,
) -> list[str]:
    excluded_paths = [".ai-dev/"]
    output_path = resolve_review_output_path(repo_root, configured_output)
    if output_path is None:
        return excluded_paths

    try:
        relative_output = output_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return excluded_paths

    excluded_paths.append(relative_output.as_posix())
    return excluded_paths


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

    config_path = config_file_for_repo_root(repo_root)
    configured_output = get_out(config_path)
    excluded_paths = _promote_excluded_paths(repo_root, configured_output)
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

    sync_local_excludes(repo_root, configured_output=configured_output)

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


def handle_review(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _review_usage(command_name)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot review workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot review workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    if not git_status_short(repo_root):
        raise FlowError("No proposed changes to review.")

    stage_all_changes(repo_root)
    quiet_completed = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--binary", "--no-ext-diff", "--quiet"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if quiet_completed.returncode == 0:
        raise FlowError("No staged changes available for review.")
    if quiet_completed.returncode not in {0, 1}:
        message = quiet_completed.stderr.strip() or quiet_completed.stdout.strip()
        raise FlowError(message)

    print(_review_workflow_label(state))
    print(f"Review summary: {_review_summary(repo_root)}")
    print("Diff legend: + added, - removed, unprefixed lines are unchanged context")
    print()

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

    print(diff_completed.stdout, end="")
    return 0


def _review_workflow_label(state: WorkflowState) -> str:
    if state.active_issue_number is not None:
        if state.active_issue_title is not None:
            return f"Issue: {state.active_issue_number} — {state.active_issue_title}"

        return f"Issue: {state.active_issue_number}"

    assert state.patch_description is not None
    return f"Patch: {state.patch_description}"


def _review_summary(repo_root: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--shortstat",
            "--binary",
            "--no-ext-diff",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise FlowError(message)

    summary = completed.stdout.strip()
    if not summary:
        raise FlowError("No staged changes available for review.")

    return summary


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

    config_path = config_file_for_repo_root(repo_root)
    configured_output = get_out(config_path)
    sync_local_excludes(repo_root, configured_output=configured_output)

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
    blocked_record = get_blocked_workflow(blocked_file, issue_number)
    if blocked_record is None:
        raise FlowError(
            f"Cannot resume workflow: no blocked record exists for issue {issue_number}."
        )

    try:
        _, _, issue_labels = _resolve_issue_details_with_labels(issue_number)
    except FileNotFoundError as exc:
        raise FlowError("GitHub CLI (gh) is required for this command.") from exc

    _reconcile_github_workflow_label(issue_number, "active", issue_labels)

    resumed_state = WorkflowState(
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        checkpoint=0,
        active_issue_number=issue_number,
        active_issue_title=blocked_record.issue_title,
        active_issue_url=blocked_record.issue_url,
    )
    save_state(state_path, resumed_state)
    remove_blocked_workflow(blocked_file, issue_number)

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

    print("Blocked workflows:")
    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
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


def print_top_level_help_handler(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise FlowError(f"Usage: {command_name} help")
    print_top_level_help(command_name)
    return 0


def handle_test_invalid_policy(command_name: str, arguments: list[str]) -> int:
    require_test_mode(command_name, "__test-invalid-policy")
    return run_operational_command(command_name, "bogus", print_top_level_help_handler, arguments)


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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command_name = resolve_command_name()

    if not arguments:
        print_top_level_help(command_name)
        return 0

    command = arguments[0]
    command_arguments = arguments[1:]

    if command in {"-h", "--help"}:
        if command_arguments:
            raise FlowError(f"Usage: {command_name} <command> [options]")

        print_top_level_help(command_name)
        return 0

    if command == "help":
        if not command_arguments:
            print_top_level_help(command_name)
            return 0

        if len(command_arguments) == 1 and command_arguments[0] in {"-h", "--help"}:
            print_command_help(command_name, "help")
            return 0

        raise FlowError(f"Usage: {command_name} help")

    if command in KNOWN_COMMANDS:
        if len(command_arguments) == 1 and command_arguments[0] in {"-h", "--help"}:
            print_command_help(command_name, command)
            return 0

        if command == "get":
            return handle_get(command_name, command_arguments)

        if command == "set":
            return handle_set(command_name, command_arguments)

        if command == "unset":
            return handle_unset(command_name, command_arguments)

        if command == "patch":
            return run_operational_command(command_name, "strict", handle_patch, command_arguments)

        if command == "start":
            return run_operational_command(command_name, "strict", handle_start, command_arguments)

        if command == "status":
            return run_operational_command(
                command_name,
                "strict",
                handle_status,
                command_arguments,
                echo_routed_output=True,
            )

        if command == "review":
            return run_operational_command(command_name, "strict", handle_review, command_arguments)

        if command == "commit":
            return run_operational_command(command_name, "strict", handle_commit, command_arguments)

        if command == "promote":
            return run_operational_command(command_name, "strict", handle_promote, command_arguments)

        if command == "reset":
            return run_operational_command(command_name, "strict", handle_reset, command_arguments)

        if command == "complete":
            return run_operational_command(command_name, "strict", handle_complete, command_arguments)

        if command == "block":
            return run_operational_command(command_name, "strict", handle_block, command_arguments)

        if command == "resume":
            return run_operational_command(command_name, "strict", handle_resume, command_arguments)

        raise FlowError(
            f"Python implementation for '{command}' "
            "is not available yet."
        )

    if command == "__test-state-get":
        return handle_test_state_get(command_name, command_arguments)

    if command == "__test-state-set":
        return handle_test_state_set(command_name, command_arguments)

    if command == "__test-state-clear":
        return handle_test_state_clear(command_name, command_arguments)

    if command == "__test-route-args":
        return run_operational_command(command_name, "strict", handle_test_route_args, command_arguments)

    if command == "__test-invalid-policy":
        return handle_test_invalid_policy(command_name, command_arguments)

    print_unknown_command(command_name, command)
    return 1


def run() -> None:
    try:
        status = main()
    except (
        BlockedWorkflowsError,
        ConfigError,
        FlowError,
        RepositoryError,
        ReviewError,
        WorkflowStateError,
    ) as exc:
        print(f"{resolve_command_name()}: {exc}", file=sys.stderr)
        status = 1

    raise SystemExit(status)


if __name__ == "__main__":
    run()
