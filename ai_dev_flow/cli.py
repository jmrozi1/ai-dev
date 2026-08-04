from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import hashlib
import uuid
from pathlib import Path
import sys
from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
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
from .review_context import (
    AcceptanceCriteriaSection,
    ReviewContext,
    ReviewContextError,
    build_review_context,
    build_review_id,
    extract_acceptance_criteria_section,
    read_local_issue_markdown,
)
from .review_package import ReviewPackageError, create_review_package, render_changes_diff
from .review_paths import ReviewArtifactPaths, ReviewPathError, build_review_artifact_paths
from .review_task_generation import (
    PlannedReviewTask,
    ReviewTaskGenerationError,
    create_review_task_file,
    plan_review_task,
    render_review_task_markdown,
    write_current_task_pointer,
)
from .review_manifest import ReviewManifestError, resolve_current_review_id, validate_review_id
from .review_verification import (
    OVERALL_STATUS_COMPLETE as REVIEW_VERIFY_COMPLETE,
    ReviewVerificationError,
    run_review_verification,
)
from .editor_opening import build_editor_opener
from .managed_installation import (
    InstallationConfigError,
    ManagedInstallationError,
    apply_installation_reconciliation,
    load_desired_installation_state,
)
from .editable_config import (
    EditableConfigError,
    ensure_editable_user_config,
    resolve_configured_editor_command,
)
from .editor_selection import launch_selected_editor, select_editor_candidate
from .report_presentation import ReportPresentationError, build_report_presenter
from .summarize_batching import SummarizeBatchingError, build_summarize_batches
from .summarize_config import SummarizeConfigError, load_repository_summarize_config
from .summarize_discovery import SummarizeDiscoveryError
from .summarize_manifest import SummarizeManifestError
from .summarize_planning import SummarizePlanningError, build_summarize_plan
from .summarize_task_generation import (
    SummarizeTaskGenerationError,
    plan_summarize_task_artifacts,
    prepare_summarize_task_artifacts,
)
from .summarize_verification import (
    OVERALL_STATUS_COMPLETE,
    SummarizeVerificationError,
    resolve_current_summarize_plan_id,
    run_summarize_verification,
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
from .json_files import JsonFileError, write_text_atomic
from .task_artifacts import TaskArtifactError, create_generated_task, plan_generated_task
from .task_config import (
    TaskConfigError,
    load_task_config,
)
from .task_delivery import ClipboardDeliveryError, build_delivery_adapter
from .task_invocation import render_invocation
from .update_installation import (
    UpdateInstallationError,
    run_update_from_record,
    resolve_installation_source_path,
)


FLOW_NAMESPACE_DESCRIPTION = "Manage issue-focused development workflows."
CANONICAL_COMMAND_NAME = "ai-dev"
CANONICAL_FLOW_PREFIX = f"{CANONICAL_COMMAND_NAME} flow"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    canonical_namespace: str
    order: int
    handler_key: str
    operational_config_policy: str | None = None
    echo_routed_output: bool = False
    compatibility_top_level: bool = False
    help_visible: bool = True
    alias_eligible: bool = True


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="start",
        description="Begin new work on an unblocked issue and reset scratch from main.",
        canonical_namespace="flow",
        order=10,
        handler_key="start",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="patch",
        description="Begin or adopt a local patch workflow on scratch.",
        canonical_namespace="flow",
        order=20,
        handler_key="patch",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="task-prepare",
        description="Prepare an immutable generated task artifact.",
        canonical_namespace="flow",
        order=30,
        handler_key="task-prepare",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="status",
        description="Show the active issue and current repository state.",
        canonical_namespace="flow",
        order=40,
        handler_key="status",
        operational_config_policy="strict",
        echo_routed_output=True,
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="review",
        description="Prepare a review package and generated review task for proposed changes.",
        canonical_namespace="flow",
        order=50,
        handler_key="review",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="commit",
        description="Create the next numbered checkpoint on scratch.",
        canonical_namespace="flow",
        order=60,
        handler_key="commit",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="reset",
        description="Discard scratch work and restore it from main.",
        canonical_namespace="flow",
        order=70,
        handler_key="reset",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="promote",
        description="Squash scratch into one permanent commit on main.",
        canonical_namespace="flow",
        order=80,
        handler_key="promote",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="complete",
        description="Clear the completed local workflow.",
        canonical_namespace="flow",
        order=90,
        handler_key="complete",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="block",
        description="Block the active issue workflow and release the active slot.",
        canonical_namespace="flow",
        order=100,
        handler_key="block",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="resume",
        description="Reactivate a previously blocked issue workflow.",
        canonical_namespace="flow",
        order=110,
        handler_key="resume",
        operational_config_policy="strict",
        compatibility_top_level=True,
    ),
    CommandSpec(
        name="summarize",
        description="Prepare deterministic summarize task artifacts for source files.",
        canonical_namespace="top",
        order=120,
        handler_key="summarize",
    ),
    CommandSpec(
        name="summarize-verify",
        description="Verify summarize outputs for a prepared plan.",
        canonical_namespace="top",
        order=130,
        handler_key="summarize-verify",
    ),
    CommandSpec(
        name="review-verify",
        description="Verify deterministic review report and package integrity.",
        canonical_namespace="top",
        order=140,
        handler_key="review-verify",
    ),
    CommandSpec(
        name="config",
        description="Open or create editable user configuration.",
        canonical_namespace="top",
        order=150,
        handler_key="config",
    ),
    CommandSpec(
        name="apply",
        description="Reconcile managed launchers, PATH state, and installation ownership.",
        canonical_namespace="top",
        order=155,
        handler_key="apply",
    ),
    CommandSpec(
        name="update",
        description="Refresh source checkout, launcher bootstrap, and managed installation state.",
        canonical_namespace="top",
        order=157,
        handler_key="update",
    ),
    CommandSpec(
        name="get",
        description="Read a repository setting.",
        canonical_namespace="top",
        order=160,
        handler_key="get",
    ),
    CommandSpec(
        name="set",
        description="Change a repository setting.",
        canonical_namespace="top",
        order=170,
        handler_key="set",
    ),
    CommandSpec(
        name="unset",
        description="Remove a repository setting.",
        canonical_namespace="top",
        order=180,
        handler_key="unset",
    ),
    CommandSpec(
        name="showreport",
        description="Show the generated report from disk.",
        canonical_namespace="top",
        order=190,
        handler_key="showreport",
    ),
    CommandSpec(
        name="help",
        description="Show this help.",
        canonical_namespace="top",
        order=200,
        handler_key="help",
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

TOP_LEVEL_CANONICAL_COMMANDS: tuple[str, ...] = (
    "flow",
    *(
        spec.name
        for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
        if spec.canonical_namespace == "top" and spec.help_visible
    ),
)

TOP_LEVEL_COMPATIBILITY_COMMANDS: tuple[str, ...] = tuple(
    spec.name
    for spec in sorted(COMMAND_SPECS, key=lambda item: item.order)
    if spec.compatibility_top_level and spec.help_visible
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
    "task-prepare": """\
Usage: {command_name} task-prepare <task-id> <task-type> <requested-command> (--body <text> | --body-file <path>) [--constraints <text>] [--expected-output <text>]

Prepare an immutable task file under .ai-dev/tasks/, update
.ai-dev/current-task.md atomically, and deliver invocation text per ai.delivery.

Options:
  --body <text>             Inline task body markdown.
  --body-file <path>        Path to a markdown file used as task body.
  --constraints <text>      Constraints block text.
  --expected-output <text>  Expected-output block text.
  -h, --help                Show this help.
""",
    "summarize": """\
Usage: {command_name} summarize <glob>

Prepare deterministic summarize task artifacts from matching source files,
update current-task pointer, and deliver invocation via ai.delivery.

Options:
  -h, --help  Show this help.
""",
    "summarize-verify": """\
Usage: {command_name} summarize-verify [<plan-id>]

Verify summarize outputs against the immutable summarize manifest, write
deterministic verification artifacts, and present verification.md using
reports.presentation mode.

Options:
  -h, --help  Show this help.
""",
    "review-verify": """\
Usage: {command_name} review-verify [<review-id>]

Verify review package/task/report integrity for a deterministic review,
write verification artifacts, and present the canonical review report using
reports.presentation mode.

Options:
  -h, --help  Show this help.
""",
    "status": """\
Usage: {command_name} status [-v|--verbose]

Show the active issue, current branch, and repository deviations.

Options:
  -v, --verbose  Show complete workflow and Git details.
  -h, --help     Show this help.
""",
    "review": """\
Usage: {command_name} review [-a|--all]

Prepare deterministic review package and generated review task for proposed changes.

Options:
  -a, --all   Include all changes in the active workflow since main.
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

Reactivate a blocked issue workflow as the local active issue.

Options:
  -h, --help  Show this help.
""",
    "get": """\
Usage: {command_name} get out

Show the configured operational output destination.

Options:
  -h, --help  Show this help.
""",
    "config": """\
Usage: {command_name} config [apply]

Create the user AI Dev YAML configuration file if missing, then open it
using editor.command, VISUAL, EDITOR, or platform defaults.
If no editor can be launched, print the absolute path for manual editing.

Run `ai-dev apply` to reconcile managed launchers, PATH state,
and installation ownership from user config.

Options:
  -h, --help  Show this help.
""",
    "apply": """\
Usage: {command_name} apply

Reconcile managed installation resources from user configuration:
launcher files, Linux ~/.bashrc PATH marker block, and ownership manifest.

This command is idempotent for unchanged configuration.

Options:
  -h, --help  Show this help.
""",
    "update": """\
Usage: {command_name} update

Refresh AI Dev from recorded installation source metadata:
validate source checkout safety, fetch and fast-forward configured remote branch,
refresh launcher bootstrap, then run managed installation apply.

This command refuses dirty checkouts and will not stash, reset, clean, merge,
rebase, or force updates automatically.

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
    "showreport": """\
Usage: {command_name} showreport

Present the report file from configured out path or default out.txt using
reports.presentation mode.

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


DEFAULT_SHOWREPORT_PATH = "out.txt"


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


def _format_help_rows(
    command_names: Sequence[str],
    descriptions: dict[str, str],
) -> str:
    if not command_names:
        return ""

    width = max(len(command) for command in command_names)
    return "\n".join(
        f"  {command.ljust(width)}  {descriptions[command]}"
        for command in command_names
    )


def render_top_level_help(command_name: str) -> str:
    top_level_descriptions: dict[str, str] = {
        "flow": FLOW_NAMESPACE_DESCRIPTION,
    }
    top_level_descriptions.update(
        {
            spec.name: spec.description
            for spec in COMMAND_SPECS
            if spec.canonical_namespace == "top" and spec.help_visible
        }
    )

    compatibility_descriptions = {
        command: f"Compatibility route to `{CANONICAL_FLOW_PREFIX} {command}`."
        for command in TOP_LEVEL_COMPATIBILITY_COMMANDS
    }

    top_rows = _format_help_rows(TOP_LEVEL_CANONICAL_COMMANDS, top_level_descriptions)
    compatibility_rows = _format_help_rows(
        TOP_LEVEL_COMPATIBILITY_COMMANDS,
        compatibility_descriptions,
    )

    return (
        f"Usage: {command_name} <command> [options]\n\n"
        "Manage an issue-focused development workflow using permanent main history\n"
        "and disposable scratch checkpoints.\n\n"
        "Commands:\n"
        f"{top_rows}\n\n"
        "Compatibility routes (temporary during Issue #19 migration):\n"
        f"{compatibility_rows}\n\n"
        f"Run `{command_name} <command> --help` for command-specific help.\n"
        f"Run `{CANONICAL_FLOW_PREFIX} --help` for workflow lifecycle commands.\n"
    )


def render_flow_help(command_name: str) -> str:
    flow_descriptions = {
        spec.name: spec.description
        for spec in COMMAND_SPECS
        if spec.canonical_namespace == "flow" and spec.help_visible
    }
    flow_rows = _format_help_rows(FLOW_LIFECYCLE_COMMANDS, flow_descriptions)

    return (
        f"Usage: {CANONICAL_FLOW_PREFIX} <command> [options]\n\n"
        "Manage issue-focused workflow lifecycle operations.\n\n"
        "Commands:\n"
        f"{flow_rows}\n\n"
        f"Run `{CANONICAL_FLOW_PREFIX} <command> --help` for command-specific help.\n"
    )


def print_top_level_help(command_name: str) -> None:
    print(render_top_level_help(command_name), end="")


def print_flow_help(command_name: str) -> None:
    print(render_flow_help(command_name), end="")


def print_command_help(command_name: str, command: str) -> None:
    template = COMMAND_HELP.get(command)
    if template is None:
        raise FlowError(f"Unknown command help target: {command}")

    print(template.format(command_name=command_name), end="")


def print_unknown_command(command_name: str, command: str) -> None:
    print(f"{command_name}: unknown command: {command}", file=sys.stderr)
    print(f"Run {command_name} help for usage.", file=sys.stderr)


def print_unknown_flow_subcommand(command_name: str, command: str) -> None:
    print(f"{CANONICAL_FLOW_PREFIX}: unknown command: {command}", file=sys.stderr)
    print(f"Run {CANONICAL_FLOW_PREFIX} --help for usage.", file=sys.stderr)


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


def _config_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} config [apply]")


def _apply_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} apply")


def _update_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} update")


def _showreport_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} showreport")


def _run_apply_command() -> int:
    try:
        config_state = ensure_editable_user_config()
        desired = load_desired_installation_state(
            config_state.config_path,
            case_insensitive_names=(os.name == "nt"),
        )
        summary = apply_installation_reconciliation(desired)
    except (
        InstallationConfigError,
        ManagedInstallationError,
        EditableConfigError,
    ) as exc:
        raise FlowError(str(exc)) from exc

    print("Managed launchers:")
    print(f"  created: {summary.launchers_created}")
    print(f"  updated: {summary.launchers_updated}")
    print(f"  removed: {summary.launchers_removed}")
    print(f"  unchanged: {summary.launchers_unchanged}")
    print(f"  directory: {summary.launcher_directory}")
    print("Expansion:")
    print(f"  expanded roots: {len(summary.expanded_root_aliases)}")
    if summary.expanded_root_aliases:
        print(f"  expanded root aliases: {', '.join(summary.expanded_root_aliases)}")
    print(f"  generated descendants: {len(summary.generated_descendant_aliases)}")
    if summary.generated_descendant_aliases:
        print(f"  descendants: {', '.join(summary.generated_descendant_aliases)}")
    print(f"  suppressed descendants: {len(summary.suppressed_descendant_aliases)}")
    if summary.suppressed_descendant_aliases:
        print(f"  suppressed: {', '.join(summary.suppressed_descendant_aliases)}")
    print(f"  no authoritative expansion source: {len(summary.expansion_unavailable_root_aliases)}")
    if summary.expansion_unavailable_root_aliases:
        print(f"  roots without source: {', '.join(summary.expansion_unavailable_root_aliases)}")
    print("Managed PATH:")
    print(f"  {summary.path_status}")
    if summary.bashrc_path is not None:
        print(f"  file: {summary.bashrc_path}")
    print("Manifest:")
    print(f"  {summary.manifest_status}")
    print(f"  file: {summary.manifest_path}")
    return 0


def handle_apply(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _apply_usage(command_name)

    return _run_apply_command()


def handle_update(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _update_usage(command_name)

    metadata_path = resolve_installation_source_path()
    try:
        result = run_update_from_record(metadata_path)
    except UpdateInstallationError as exc:
        raise FlowError(
            "Update source:\n"
            "  failed\n"
            f"  metadata: {metadata_path}\n"
            f"  detail: {exc}"
        ) from exc

    print("Update source:")
    if result.source.source_status == "already up to date":
        print("  already up to date")
    elif result.source.source_status == "fast-forwarded":
        print(
            "  "
            f"fast-forwarded {result.source.source_from} -> {result.source.source_to}"
        )
    else:
        print(f"  {result.source.source_status}")
    print(f"  repository: {result.source.source_repo}")
    print(f"  branch: {result.source.branch}")
    print(f"  remote: {result.source.remote}")

    print("Launcher refresh:")
    print(f"  {result.launcher.status}")
    if result.launcher.detail:
        print(f"  detail: {result.launcher.detail}")

    print("Apply:")
    print(f"  {result.apply.status}")
    if result.apply.detail:
        print(f"  detail: {result.apply.detail}")

    if result.launcher.status == "failed" or result.apply.status == "failed":
        return 1
    return 0


def handle_config(command_name: str, arguments: list[str]) -> int:
    if arguments:
        if len(arguments) == 1 and arguments[0] == "apply":
            return _run_apply_command()

        raise _config_usage(command_name)

    try:
        config_state = ensure_editable_user_config()
    except EditableConfigError as exc:
        raise FlowError(str(exc)) from exc

    configured_editor_command, parse_warning = resolve_configured_editor_command(
        config_state.config_path
    )
    if parse_warning is not None:
        print(f"Warning: {parse_warning}", file=sys.stderr)

    selection = select_editor_candidate(configured_editor_command)
    launch_result = launch_selected_editor(config_state.config_path, selection)

    if config_state.created:
        print(f"Created AI Dev config: {config_state.config_path}")
    else:
        print(f"AI Dev config: {config_state.config_path}")

    if launch_result.warning:
        print(f"Warning: {launch_result.warning}", file=sys.stderr)

    if launch_result.opened:
        command_display = launch_result.command_display or "(unknown)"
        print(f"Opened config with: {command_display}")
        return 0

    print("No editor could be launched. Edit this file manually.")
    return 0


def _resolve_showreport_path(repo_root: Path) -> Path:
    configured_output = get_out(config_file_for_repo_root(repo_root))
    if configured_output:
        return resolve_output_destination(repo_root, configured_output)

    return repo_root / DEFAULT_SHOWREPORT_PATH


def handle_showreport(command_name: str, arguments: list[str]) -> int:
    if arguments:
        raise _showreport_usage(command_name)

    repo_root = resolve_repo_root()
    task_config = load_task_config(repo_root)
    report_path = _resolve_showreport_path(repo_root)

    presenter = build_report_presenter(
        task_config.report_presentation,
        editor_opener=build_editor_opener(task_config.editor_command),
    )

    try:
        presenter.present(report_path)
    except ReportPresentationError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print(f"Report path: {report_path}")
        return 1

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

    blocked_file = blocked_workflows_file_for_repo_root(repo_root)
    blocked_record = get_blocked_workflow(blocked_file, issue_number)
    if blocked_record is not None:
        raise FlowError(
            f"Cannot start workflow: issue {issue_number} is blocked. "
            f"Use {command_name} resume {issue_number}."
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
    return FlowError(f"Usage: {command_name} status [-v|--verbose]")


def _task_prepare_usage(command_name: str) -> FlowError:
    return FlowError(
        "Usage: "
        f"{command_name} task-prepare <task-id> <task-type> <requested-command> "
        "(--body <text> | --body-file <path>) "
        "[--constraints <text>] [--expected-output <text>]"
    )


def _review_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} review [-a|--all]")


def _summarize_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} summarize <glob>")


def _summarize_verify_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} summarize-verify [<plan-id>]")


def _review_verify_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} review-verify [<review-id>]")


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


def _parse_task_prepare_options(
    command_name: str,
    option_tokens: list[str],
) -> tuple[str, str, str]:
    body_text: str | None = None
    body_supplied_by: str | None = None
    constraints = "(none)"
    expected_output = "(none)"
    constraints_supplied = False
    expected_output_supplied = False

    index = 0
    while index < len(option_tokens):
        option = option_tokens[index]
        index += 1

        if option == "--body":
            if index >= len(option_tokens):
                raise _task_prepare_usage(command_name)

            if body_supplied_by is not None:
                raise FlowError(
                    "Specify exactly one of --body or --body-file, and provide it only once."
                )

            body_text = option_tokens[index]
            body_supplied_by = "--body"
            index += 1
            continue

        if option == "--body-file":
            if index >= len(option_tokens):
                raise _task_prepare_usage(command_name)

            if body_supplied_by is not None:
                raise FlowError(
                    "Specify exactly one of --body or --body-file, and provide it only once."
                )

            body_file_path = Path(option_tokens[index])
            index += 1
            try:
                body_text = body_file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise FlowError(
                    f"Cannot read task body file {body_file_path}: {exc}"
                ) from exc
            body_supplied_by = "--body-file"
            continue

        if option == "--constraints":
            if index >= len(option_tokens):
                raise _task_prepare_usage(command_name)
            if constraints_supplied:
                raise FlowError("--constraints may be provided at most once.")
            constraints = option_tokens[index]
            constraints_supplied = True
            index += 1
            continue

        if option == "--expected-output":
            if index >= len(option_tokens):
                raise _task_prepare_usage(command_name)
            if expected_output_supplied:
                raise FlowError("--expected-output may be provided at most once.")
            expected_output = option_tokens[index]
            expected_output_supplied = True
            index += 1
            continue

        raise _task_prepare_usage(command_name)

    if body_supplied_by is None or body_text is None:
        raise FlowError("Specify exactly one of --body or --body-file.")

    return body_text, constraints, expected_output


def handle_task_prepare(command_name: str, arguments: list[str]) -> int:
    if len(arguments) < 3:
        raise _task_prepare_usage(command_name)

    task_id = arguments[0]
    task_type = arguments[1]
    requested_command = arguments[2]
    body_text, constraints, expected_output = _parse_task_prepare_options(
        command_name,
        arguments[3:],
    )

    repo_root = resolve_repo_root()
    task_config = load_task_config(repo_root)
    planned_task = plan_generated_task(
        repo_root=repo_root,
        task_id=task_id,
        task_type=task_type,
        requested_command=requested_command,
    )

    invocation = render_invocation(
        task_config.invocation,
        task_file=planned_task.repository_relative_path,
        task_id=planned_task.task_id,
        task_type=planned_task.task_type,
        config_path=task_config.invocation_source_path,
        config_field_path=task_config.invocation_source_field or "ai.invocation",
    )

    generated_task = create_generated_task(
        repo_root=repo_root,
        task_id=planned_task.task_id,
        task_type=planned_task.task_type,
        requested_command=planned_task.requested_command,
        task_body=body_text,
        constraints=constraints,
        expected_output=expected_output,
    )
    adapter = build_delivery_adapter(task_config.delivery)
    adapter.deliver(invocation)

    print(f"Task file: {generated_task.repository_relative_path}")
    return 0


def handle_summarize(command_name: str, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise _summarize_usage(command_name)

    repo_root = resolve_repo_root()
    summarize_config = load_repository_summarize_config(repo_root)
    plan = build_summarize_plan(repo_root, arguments[0])
    batches = build_summarize_batches(plan, max_files=summarize_config.batch_max_files)
    planned_artifacts = plan_summarize_task_artifacts(
        repo_root=repo_root,
        plan=plan,
        batches=batches,
    )

    task_config = load_task_config(repo_root)
    invocation = render_invocation(
        task_config.invocation,
        task_file=planned_artifacts.coordinator_planned.repository_relative_path,
        task_id=planned_artifacts.coordinator_planned.task_id,
        task_type="summarize",
        config_path=task_config.invocation_source_path,
        config_field_path=task_config.invocation_source_field or "ai.invocation",
    )
    adapter = build_delivery_adapter(task_config.delivery)

    prepared = prepare_summarize_task_artifacts(
        repo_root=repo_root,
        plan=plan,
        batches=batches,
        planned_artifacts=planned_artifacts,
    )
    adapter.deliver(invocation)

    print(
        f"Prepared summarize tasks for plan {prepared.plan_id}: "
        f"{prepared.batch_count} batch(es), {prepared.source_count} source file(s)."
    )
    print(f"Coordinator task: {prepared.coordinator_task_path}")
    print(f"Manifest: {prepared.manifest_path}")
    print(f"Task file: {prepared.coordinator_task_path}")
    return 0


def handle_summarize_verify(command_name: str, arguments: list[str]) -> int:
    if len(arguments) > 1:
        raise _summarize_verify_usage(command_name)

    repo_root = resolve_repo_root()
    plan_id = arguments[0].strip() if arguments else ""
    if arguments and not plan_id:
        raise _summarize_verify_usage(command_name)

    if not plan_id:
        plan_id = resolve_current_summarize_plan_id(repo_root)

    result, markdown_relative_path, json_relative_path = run_summarize_verification(
        repo_root=repo_root,
        plan_id=plan_id,
    )

    task_config = load_task_config(repo_root)
    presenter = build_report_presenter(
        task_config.report_presentation,
        editor_opener=build_editor_opener(task_config.editor_command),
    )

    report_path = repo_root / markdown_relative_path
    try:
        presenter.present(report_path)
    except ReportPresentationError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print(f"Report path: {report_path}")

    print(
        f"Summarize verification status for plan {result.plan_id}: {result.overall_status}"
    )
    print(f"Verification report: {markdown_relative_path}")
    print(f"Verification JSON: {json_relative_path}")

    if result.overall_status == OVERALL_STATUS_COMPLETE:
        return 0
    return 1


def handle_review_verify(command_name: str, arguments: list[str]) -> int:
    if len(arguments) > 1:
        raise _review_verify_usage(command_name)

    repo_root = resolve_repo_root()
    requested_review_id = arguments[0].strip() if arguments else ""
    if arguments and not requested_review_id:
        raise _review_verify_usage(command_name)

    current_review_id = resolve_current_review_id(repo_root)
    if requested_review_id and requested_review_id != current_review_id:
        raise FlowError(
            "Requested review ID does not match the current rolling review. "
            f"Requested: {requested_review_id}. Current: {current_review_id}."
        )

    review_id = current_review_id

    result, markdown_relative_path, json_relative_path = run_review_verification(
        repo_root=repo_root,
        review_id=review_id,
    )

    review_report_path = repo_root / result.report_path
    if result.overall_status == REVIEW_VERIFY_COMPLETE and result.report_state.status == "valid":
        task_config = load_task_config(repo_root)
        presenter = build_report_presenter(
            task_config.report_presentation,
            editor_opener=build_editor_opener(task_config.editor_command),
        )
        try:
            presenter.present(review_report_path)
        except ReportPresentationError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            print(f"Review report path: {review_report_path}")
    else:
        print(f"Review report path: {review_report_path}")

    print(f"Review verification status for {result.review_id}: {result.overall_status}")
    print(f"Review decision: {result.review_decision or '(unavailable)'}")
    print(f"Review report: {result.report_path}")
    print(f"Verification report: {markdown_relative_path}")
    print(f"Verification JSON: {json_relative_path}")

    if result.overall_status == REVIEW_VERIFY_COMPLETE:
        return 0
    return 1


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

    try:
        _cleanup_rolling_review_workspace(repo_root)
    except OSError as exc:
        raise FlowError(
            "Checkpoint commit created but review cleanup failed. "
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

        try:
            _cleanup_rolling_review_workspace(repo_root)
        except OSError as cleanup_exc:
            raise FlowError(
                f"Promoted workflow but review cleanup failed: {cleanup_exc}"
            ) from cleanup_exc
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


REVIEW_SCOPE_CHECKPOINT = "checkpoint"
REVIEW_SCOPE_WORKFLOW = "workflow"
ROLLING_REVIEW_RELATIVE_PATH = ".ai-dev/review"
LEGACY_REVIEWS_RELATIVE_PATH = ".ai-dev/reviews"


def _parse_review_scope(command_name: str, arguments: list[str]) -> str:
    if not arguments:
        return REVIEW_SCOPE_CHECKPOINT

    if len(arguments) != 1:
        raise _review_usage(command_name)

    option = arguments[0]
    if option in {"-a", "--all"}:
        return REVIEW_SCOPE_WORKFLOW

    raise _review_usage(command_name)


def _review_cached_diff(repo_root: Path) -> str:
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


def _review_workflow_diff(repo_root: Path, *, main_branch: str, scratch_branch: str) -> str:
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


def _review_workflow_summary(repo_root: Path, *, main_branch: str, scratch_branch: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--shortstat",
            "--no-ext-diff",
            f"{main_branch}...{scratch_branch}",
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

    return completed.stdout.strip()


def _review_combined_summary(*, workflow_summary: str, working_tree_summary: str) -> str:
    if workflow_summary and working_tree_summary:
        return (
            f"{workflow_summary}; "
            f"plus staged working-tree changes: {working_tree_summary}"
        )

    if workflow_summary:
        return workflow_summary

    return working_tree_summary


def _review_diff_sha256(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def _decode_nul_paths(raw_output: bytes) -> list[str]:
    decoded = raw_output.decode("utf-8", errors="surrogateescape")
    if not decoded:
        return []

    parts = decoded.split("\x00")
    if parts and parts[-1] == "":
        parts = parts[:-1]

    return [item for item in parts if item != ""]


def _review_cached_paths(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-ext-diff",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FlowError(message)
    return _decode_nul_paths(completed.stdout)


def _review_workflow_paths(repo_root: Path, *, main_branch: str, scratch_branch: str) -> list[str]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--name-only",
            "-z",
            "--no-ext-diff",
            f"{main_branch}...{scratch_branch}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise FlowError(message)
    return _decode_nul_paths(completed.stdout)


def _review_instruction_reference_paths(repo_root: Path) -> list[str]:
    candidates = [
        repo_root / "ai-dev-core" / "workflows" / "review" / "review-documentation.md",
        repo_root / "ai-dev-core" / "workflows" / "review" / "finding-template.md",
        repo_root / "vendor" / "ai-dev-core" / "workflows" / "review" / "review-documentation.md",
        repo_root / "vendor" / "ai-dev-core" / "workflows" / "review" / "finding-template.md",
    ]

    relative_paths: list[str] = []
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            relative_paths.append(candidate.relative_to(repo_root).as_posix())

    return sorted(set(relative_paths))


@dataclass(frozen=True)
class PreparedReviewPackage:
    review_id: str
    review_paths: ReviewArtifactPaths
    context: ReviewContext
    changes_diff_text: str


def _plan_review_package(
    *,
    repo_root: Path,
    command_name: str,
    review_scope: str,
    state: WorkflowState,
    current_branch: str,
    committed_diff_text: str,
    overlay_diff_text: str,
    committed_paths: list[str],
    overlay_paths: list[str],
) -> PreparedReviewPackage:
    workflow_type = _active_workflow_type(state) or "none"
    diagnostics: list[str] = []

    issue_markdown: str | None = None
    issue_source: str | None = None
    issue_description_status = "not_applicable"
    acceptance_criteria_status = "not_applicable"

    if state.active_issue_number is not None:
        issue_description_status = "unavailable_local"
        acceptance_criteria_status = "unavailable_local"

        issue_markdown, issue_source = read_local_issue_markdown(
            repo_root,
            state.active_issue_number,
        )
        if issue_markdown is not None:
            issue_description_status = "available_local"
            acceptance_criteria_status = "available_local"
        else:
            diagnostics.append(
                "Issue body unavailable locally; acceptance criteria extraction skipped."
            )

    acceptance_criteria: AcceptanceCriteriaSection = extract_acceptance_criteria_section(
        issue_markdown or ""
    )
    if acceptance_criteria_status == "available_local" and not acceptance_criteria.found:
        acceptance_criteria_status = "unavailable_local"
        diagnostics.append("Acceptance criteria heading not found in local issue metadata.")

    committed_reference = f"{state.main_branch}...{state.scratch_branch}"
    overlay_reference = "HEAD -> index"

    all_paths = sorted(set(committed_paths + overlay_paths))

    committed_diff_sha256 = (
        _review_diff_sha256(committed_diff_text) if committed_diff_text else None
    )
    overlay_diff_sha256 = _review_diff_sha256(overlay_diff_text) if overlay_diff_text else None
    instruction_reference_paths = _review_instruction_reference_paths(repo_root)

    placeholder_id = "review-pending"
    placeholder_paths = build_review_artifact_paths(repo_root, placeholder_id)

    temporary_context = build_review_context(
        scope=review_scope,
        command=command_name,
        workflow_type=workflow_type,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        current_branch=current_branch,
        checkpoint=state.checkpoint,
        active_issue_number=state.active_issue_number,
        active_issue_title=state.active_issue_title,
        active_issue_url=state.active_issue_url,
        patch_description=state.patch_description,
        issue_description_status=issue_description_status,
        issue_description_source=issue_source,
        acceptance_criteria_status=acceptance_criteria_status,
        acceptance_criteria_heading=acceptance_criteria.heading,
        acceptance_criteria_lines=acceptance_criteria.lines,
        committed_reference=committed_reference,
        committed_paths=committed_paths,
        committed_diff_text=committed_diff_text,
        committed_diff_sha256=committed_diff_sha256,
        overlay_reference=overlay_reference,
        overlay_paths=overlay_paths,
        overlay_diff_text=overlay_diff_text,
        overlay_diff_sha256=overlay_diff_sha256,
        all_paths=all_paths,
        changes_diff_sha256="0" * 64,
        instruction_reference_paths=instruction_reference_paths,
        diagnostics=diagnostics,
        review_root_path=placeholder_paths.review_root_relative_path,
        package_markdown_path=placeholder_paths.package_markdown_relative_path,
        package_json_path=placeholder_paths.package_json_relative_path,
        changes_diff_path=placeholder_paths.changes_diff_relative_path,
        canonical_report_path=placeholder_paths.canonical_report_relative_path,
    )

    changes_diff_text = render_changes_diff(temporary_context)
    changes_diff_sha256 = _review_diff_sha256(changes_diff_text)

    id_input_context = build_review_context(
        scope=review_scope,
        command=command_name,
        workflow_type=workflow_type,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        current_branch=current_branch,
        checkpoint=state.checkpoint,
        active_issue_number=state.active_issue_number,
        active_issue_title=state.active_issue_title,
        active_issue_url=state.active_issue_url,
        patch_description=state.patch_description,
        issue_description_status=issue_description_status,
        issue_description_source=issue_source,
        acceptance_criteria_status=acceptance_criteria_status,
        acceptance_criteria_heading=acceptance_criteria.heading,
        acceptance_criteria_lines=acceptance_criteria.lines,
        committed_reference=committed_reference,
        committed_paths=committed_paths,
        committed_diff_text=committed_diff_text,
        committed_diff_sha256=committed_diff_sha256,
        overlay_reference=overlay_reference,
        overlay_paths=overlay_paths,
        overlay_diff_text=overlay_diff_text,
        overlay_diff_sha256=overlay_diff_sha256,
        all_paths=all_paths,
        changes_diff_sha256=changes_diff_sha256,
        instruction_reference_paths=instruction_reference_paths,
        diagnostics=diagnostics,
        review_root_path=placeholder_paths.review_root_relative_path,
        package_markdown_path=placeholder_paths.package_markdown_relative_path,
        package_json_path=placeholder_paths.package_json_relative_path,
        changes_diff_path=placeholder_paths.changes_diff_relative_path,
        canonical_report_path=placeholder_paths.canonical_report_relative_path,
    )

    review_id = build_review_id(id_input_context)
    review_paths = build_review_artifact_paths(repo_root, review_id)

    context = build_review_context(
        scope=review_scope,
        command=command_name,
        workflow_type=workflow_type,
        main_branch=state.main_branch,
        scratch_branch=state.scratch_branch,
        current_branch=current_branch,
        checkpoint=state.checkpoint,
        active_issue_number=state.active_issue_number,
        active_issue_title=state.active_issue_title,
        active_issue_url=state.active_issue_url,
        patch_description=state.patch_description,
        issue_description_status=issue_description_status,
        issue_description_source=issue_source,
        acceptance_criteria_status=acceptance_criteria_status,
        acceptance_criteria_heading=acceptance_criteria.heading,
        acceptance_criteria_lines=acceptance_criteria.lines,
        committed_reference=committed_reference,
        committed_paths=committed_paths,
        committed_diff_text=committed_diff_text,
        committed_diff_sha256=committed_diff_sha256,
        overlay_reference=overlay_reference,
        overlay_paths=overlay_paths,
        overlay_diff_text=overlay_diff_text,
        overlay_diff_sha256=overlay_diff_sha256,
        all_paths=all_paths,
        changes_diff_sha256=changes_diff_sha256,
        instruction_reference_paths=instruction_reference_paths,
        diagnostics=diagnostics,
        review_root_path=review_paths.review_root_relative_path,
        package_markdown_path=review_paths.package_markdown_relative_path,
        package_json_path=review_paths.package_json_relative_path,
        changes_diff_path=review_paths.changes_diff_relative_path,
        canonical_report_path=review_paths.canonical_report_relative_path,
    )

    if build_review_id(context) != review_id:
        raise ReviewPackageError(
            "Deterministic review package ID mismatch between final context and artifact directory."
        )

    return PreparedReviewPackage(
        review_id=review_id,
        review_paths=review_paths,
        context=context,
        changes_diff_text=changes_diff_text,
    )


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_tree_if_exists(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)


def _cleanup_legacy_review_storage(repo_root: Path) -> None:
    legacy_reviews_root = repo_root / LEGACY_REVIEWS_RELATIVE_PATH
    if legacy_reviews_root.exists():
        _remove_tree_if_exists(legacy_reviews_root)

    tasks_root = repo_root / ".ai-dev" / "tasks"
    if not tasks_root.exists() or not tasks_root.is_dir():
        return

    for path in tasks_root.iterdir():
        if not path.is_file() or path.suffix != ".md":
            continue
        stem = path.stem
        if not stem.endswith("-task"):
            continue
        review_id_candidate = stem[: -len("-task")]
        try:
            validate_review_id(review_id_candidate)
        except ReviewManifestError:
            continue
        _remove_file_if_exists(path)


def _cleanup_rolling_review_workspace(repo_root: Path) -> None:
    rolling_root = repo_root / ROLLING_REVIEW_RELATIVE_PATH
    if rolling_root.exists():
        _remove_tree_if_exists(rolling_root)

    _cleanup_legacy_review_storage(repo_root)


def _restore_current_task_pointer(
    *,
    pointer_path: Path,
    previous_pointer_text: str | None,
) -> list[str]:
    failures: list[str] = []

    if previous_pointer_text is None:
        try:
            _remove_file_if_exists(pointer_path)
        except OSError as exc:
            failures.append(f"{pointer_path}: {exc}")
        return failures

    try:
        write_text_atomic(pointer_path, previous_pointer_text)
    except JsonFileError as exc:
        failures.append(f"{pointer_path}: {exc}")

    return failures


def _replace_rolling_review_workspace(
    *,
    canonical_root: Path,
    temp_root: Path,
) -> Path | None:
    backup_root = canonical_root.parent / f"{canonical_root.name}.bak-{uuid.uuid4().hex}"
    moved_existing = False

    try:
        if canonical_root.exists():
            canonical_root.rename(backup_root)
            moved_existing = True

        temp_root.rename(canonical_root)
    except OSError as exc:
        if moved_existing and backup_root.exists() and not canonical_root.exists():
            try:
                backup_root.rename(canonical_root)
            except OSError:
                pass
        raise FlowError(f"Failed to replace rolling review workspace: {exc}") from exc

    return backup_root if moved_existing else None


def _prepare_review_task_and_deliver(
    *,
    repo_root: Path,
    package: PreparedReviewPackage,
    planned_task: PlannedReviewTask,
    invocation: str,
    adapter,
) -> None:
    pointer_path = repo_root / ".ai-dev" / "current-task.md"
    previous_pointer_text: str | None = None
    if pointer_path.exists():
        try:
            previous_pointer_text = pointer_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FlowError(
                f"Cannot read current task pointer for rollback: {pointer_path}. {exc}"
            ) from exc

    canonical_root = package.review_paths.review_root_absolute_path
    temp_root = canonical_root.parent / f"{canonical_root.name}.tmp-{uuid.uuid4().hex}"
    temp_review_paths = replace(
        package.review_paths,
        review_root_absolute_path=temp_root,
        task_markdown_absolute_path=temp_root / "task.md",
        package_markdown_absolute_path=temp_root / "package.md",
        package_json_absolute_path=temp_root / "package.json",
        changes_diff_absolute_path=temp_root / "changes.diff",
        canonical_report_absolute_path=temp_root / "report.md",
        verification_markdown_absolute_path=temp_root / "verification.md",
        verification_json_absolute_path=temp_root / "verification.json",
    )
    temp_planned_task = replace(planned_task, absolute_path=temp_review_paths.task_markdown_absolute_path)
    backup_root: Path | None = None
    published = False
    published_and_delivered = False

    try:
        create_review_package(
            repo_root=repo_root,
            review_paths=temp_review_paths,
            review_id=package.review_id,
            context=package.context,
            changes_diff_text=package.changes_diff_text,
        )

        task_markdown = render_review_task_markdown(
            planned_task=temp_planned_task,
            review_paths=package.review_paths,
            context=package.context,
        )
        create_review_task_file(
            planned_task=temp_planned_task,
            markdown_text=task_markdown,
        )

        backup_root = _replace_rolling_review_workspace(canonical_root=canonical_root, temp_root=temp_root)
        published = True

        write_current_task_pointer(repo_root=repo_root, planned_task=planned_task)

        adapter.deliver(invocation)
        published_and_delivered = True
    except (ReviewPackageError, ReviewTaskGenerationError, ClipboardDeliveryError, FlowError, OSError) as exc:
        cleanup_failures: list[str] = []
        retained_backup_path: Path | None = None
        backup_restore_succeeded = False

        if published:
            canonical_removed = False
            if canonical_root.exists():
                try:
                    shutil.rmtree(canonical_root)
                    canonical_removed = True
                except OSError as cleanup_exc:
                    cleanup_failures.append(f"{canonical_root}: {cleanup_exc}")
            else:
                canonical_removed = True

            if backup_root is not None and backup_root.exists():
                if canonical_removed and not canonical_root.exists():
                    try:
                        backup_root.rename(canonical_root)
                        backup_restore_succeeded = True
                    except OSError as cleanup_exc:
                        retained_backup_path = backup_root
                        cleanup_failures.append(f"{backup_root} -> {canonical_root}: {cleanup_exc}")
                else:
                    retained_backup_path = backup_root
                    cleanup_failures.append(
                        "Cannot restore previous rolling review because newly published "
                        f"workspace could not be removed: {canonical_root}"
                    )

        cleanup_failures.extend(
            _restore_current_task_pointer(
                pointer_path=pointer_path,
                previous_pointer_text=previous_pointer_text,
            )
        )

        if temp_root.exists():
            try:
                shutil.rmtree(temp_root)
            except OSError as cleanup_exc:
                cleanup_failures.append(f"{temp_root}: {cleanup_exc}")

        if backup_root is not None and backup_root.exists():
            if backup_restore_succeeded:
                try:
                    shutil.rmtree(backup_root)
                except OSError as cleanup_exc:
                    cleanup_failures.append(f"{backup_root}: {cleanup_exc}")
            else:
                retained_backup_path = backup_root

        if retained_backup_path is not None:
            cleanup_failures.append(f"Retained backup workspace: {retained_backup_path}")

        if cleanup_failures:
            raise FlowError(
                f"Review task preparation failed. {exc} Cleanup failures: "
                + "; ".join(cleanup_failures)
            ) from exc

        raise FlowError(f"Review task preparation failed. {exc}") from exc

    if published_and_delivered:
        cleanup_failures: list[str] = []
        retained_backup_path: Path | None = None

        if backup_root is not None and backup_root.exists():
            try:
                shutil.rmtree(backup_root)
            except OSError as exc:
                retained_backup_path = backup_root
                cleanup_failures.append(
                    f"Failed to delete previous rolling review backup {backup_root}: {exc}"
                )

        try:
            _cleanup_legacy_review_storage(repo_root)
        except OSError as exc:
            cleanup_failures.append(f"Legacy cleanup failed: {exc}")

        if retained_backup_path is not None:
            cleanup_failures.append(f"Retained backup workspace: {retained_backup_path}")

        if cleanup_failures:
            raise FlowError(
                "Review task preparation succeeded, but post-commit cleanup failed. "
                f"Published review remains available at {ROLLING_REVIEW_RELATIVE_PATH}. "
                + "; ".join(cleanup_failures)
            )


def _print_review_preparation_metadata(
    *,
    package: PreparedReviewPackage,
    planned_task: PlannedReviewTask,
) -> None:
    print(f"Prepared review task for {package.review_id}.")
    print(f"Review task: {planned_task.repository_relative_path}")
    print(f"Review package: {package.review_paths.package_markdown_relative_path}")
    print(f"Changes: {package.review_paths.changes_diff_relative_path}")
    print(f"Expected report: {package.review_paths.canonical_report_relative_path}")


def handle_review(command_name: str, arguments: list[str]) -> int:
    review_scope = _parse_review_scope(command_name, arguments)

    repo_root, state_path, state = _resolve_repo_state_context()

    if _active_workflow_type(state) is None:
        raise FlowError("Cannot review workflow: no active issue is set.")

    _ensure_main_and_scratch_branches_exist(repo_root, state)

    current_branch = current_branch_name(repo_root)
    if current_branch != state.scratch_branch:
        raise FlowError(
            f"Cannot review workflow: current branch {current_branch} does not match scratchBranch {state.scratch_branch}."
        )

    if review_scope == REVIEW_SCOPE_CHECKPOINT and not git_status_short(repo_root):
        raise FlowError("No proposed changes to review.")

    configured_output = get_out(config_file_for_repo_root(repo_root))
    sync_local_excludes(repo_root, configured_output=configured_output)
    stage_all_changes(repo_root)

    if review_scope == REVIEW_SCOPE_WORKFLOW:
        workflow_diff = _review_workflow_diff(
            repo_root,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
        )
        working_tree_diff = _review_cached_diff(repo_root)

        if not workflow_diff and not working_tree_diff:
            raise FlowError("No proposed changes to review.")

        workflow_summary = _review_workflow_summary(
            repo_root,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
        )
        working_tree_summary = _review_summary(repo_root, allow_empty=True)
        summary = _review_combined_summary(
            workflow_summary=workflow_summary,
            working_tree_summary=working_tree_summary,
        )
        if not summary:
            raise FlowError("No proposed changes to review.")

        committed_paths = _review_workflow_paths(
            repo_root,
            main_branch=state.main_branch,
            scratch_branch=state.scratch_branch,
        )
        overlay_paths = _review_cached_paths(repo_root)

        try:
            package = _plan_review_package(
                repo_root=repo_root,
                command_name=f"{command_name} --all",
                review_scope=review_scope,
                state=state,
                current_branch=current_branch,
                committed_diff_text=workflow_diff,
                overlay_diff_text=working_tree_diff,
                committed_paths=committed_paths,
                overlay_paths=overlay_paths,
            )
        except (ReviewContextError, ReviewPathError, ReviewPackageError) as exc:
            raise FlowError(f"Cannot prepare deterministic review package. {exc}") from exc

        planned_task = plan_review_task(
            repo_root=repo_root,
            review_id=package.review_id,
            requested_command=package.context.command,
        )

        task_config = load_task_config(repo_root)
        invocation = render_invocation(
            task_config.invocation,
            task_file=planned_task.repository_relative_path,
            task_id=planned_task.task_id,
            task_type=planned_task.task_type,
            config_path=task_config.invocation_source_path,
            config_field_path=task_config.invocation_source_field or "ai.invocation",
        )
        try:
            adapter = build_delivery_adapter(task_config.delivery)
        except ValueError as exc:
            raise FlowError(f"Invalid delivery mode. {exc}") from exc

        _prepare_review_task_and_deliver(
            repo_root=repo_root,
            package=package,
            planned_task=planned_task,
            invocation=invocation,
            adapter=adapter,
        )

        print(_review_workflow_label(state))
        print(f"Review summary: {summary}")
        print("Diff legend: + added, - removed, unprefixed lines are unchanged context")
        _print_review_preparation_metadata(package=package, planned_task=planned_task)
        print()

        if workflow_diff:
            print(workflow_diff, end="")

        if workflow_diff and working_tree_diff and not workflow_diff.endswith("\n"):
            print()

        if workflow_diff and working_tree_diff:
            print()

        if working_tree_diff:
            print(working_tree_diff, end="")
        return 0

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

    checkpoint_diff = _review_cached_diff(repo_root)
    overlay_paths = _review_cached_paths(repo_root)
    try:
        package = _plan_review_package(
            repo_root=repo_root,
            command_name=command_name,
            review_scope=review_scope,
            state=state,
            current_branch=current_branch,
            committed_diff_text="",
            overlay_diff_text=checkpoint_diff,
            committed_paths=[],
            overlay_paths=overlay_paths,
        )
    except (ReviewContextError, ReviewPathError, ReviewPackageError) as exc:
        raise FlowError(f"Cannot prepare deterministic review package. {exc}") from exc

    planned_task = plan_review_task(
        repo_root=repo_root,
        review_id=package.review_id,
        requested_command=package.context.command,
    )

    task_config = load_task_config(repo_root)
    invocation = render_invocation(
        task_config.invocation,
        task_file=planned_task.repository_relative_path,
        task_id=planned_task.task_id,
        task_type=planned_task.task_type,
        config_path=task_config.invocation_source_path,
        config_field_path=task_config.invocation_source_field or "ai.invocation",
    )
    try:
        adapter = build_delivery_adapter(task_config.delivery)
    except ValueError as exc:
        raise FlowError(f"Invalid delivery mode. {exc}") from exc

    _prepare_review_task_and_deliver(
        repo_root=repo_root,
        package=package,
        planned_task=planned_task,
        invocation=invocation,
        adapter=adapter,
    )

    print(_review_workflow_label(state))
    print(f"Review summary: {_review_summary(repo_root)}")
    print("Diff legend: + added, - removed, unprefixed lines are unchanged context")
    _print_review_preparation_metadata(package=package, planned_task=planned_task)
    print()

    print(checkpoint_diff, end="")
    return 0


def _review_workflow_label(state: WorkflowState) -> str:
    if state.active_issue_number is not None:
        if state.active_issue_title is not None:
            return f"Issue: {state.active_issue_number} — {state.active_issue_title}"

        return f"Issue: {state.active_issue_number}"

    assert state.patch_description is not None
    return f"Patch: {state.patch_description}"


def _review_summary(repo_root: Path, *, allow_empty: bool = False) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--cached",
            "--shortstat",
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
    if not summary and not allow_empty:
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

    try:
        _cleanup_rolling_review_workspace(repo_root)
    except OSError as exc:
        raise FlowError(f"Scratch was reset but review cleanup failed. {exc}") from exc

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

    try:
        _cleanup_rolling_review_workspace(repo_root)
    except OSError as exc:
        raise FlowError(f"Workflow completed but review cleanup failed: {exc}") from exc

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


def _resolve_command_handler(handler_key: str):
    handlers = {
        "start": handle_start,
        "patch": handle_patch,
        "task-prepare": handle_task_prepare,
        "status": handle_status,
        "review": handle_review,
        "commit": handle_commit,
        "reset": handle_reset,
        "promote": handle_promote,
        "complete": handle_complete,
        "block": handle_block,
    "resume": handle_resume,
        "summarize": handle_summarize,
    "summarize-verify": handle_summarize_verify,
        "review-verify": handle_review_verify,
        "config": handle_config,
        "apply": handle_apply,
        "update": handle_update,
        "get": handle_get,
        "set": handle_set,
        "unset": handle_unset,
        "showreport": handle_showreport,
    }
    return handlers.get(handler_key)


def _dispatch_command(
    invocation_name: str,
    spec: CommandSpec,
    arguments: list[str],
) -> int:
    if len(arguments) == 1 and arguments[0] in {"-h", "--help"}:
        print_command_help(invocation_name, spec.name)
        return 0

    handler = _resolve_command_handler(spec.handler_key)
    if handler is None:
        raise FlowError(
            f"Python implementation for '{spec.name}' is not available yet."
        )

    if spec.operational_config_policy is None:
        return handler(invocation_name, arguments)

    return run_operational_command(
        invocation_name,
        spec.operational_config_policy,
        handler,
        arguments,
        echo_routed_output=spec.echo_routed_output,
    )


def _flow_usage(command_name: str) -> FlowError:
    return FlowError(f"Usage: {command_name} flow <command> [options]")


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

    if command == "flow":
        if not command_arguments:
            print_flow_help(command_name)
            return 0

        if command_arguments[0] in {"-h", "--help"}:
            if len(command_arguments) > 1:
                raise _flow_usage(command_name)
            print_flow_help(command_name)
            return 0

        flow_command = command_arguments[0]
        flow_arguments = command_arguments[1:]
        flow_spec = COMMAND_SPEC_BY_NAME.get(flow_command)
        if flow_spec is None or flow_spec.canonical_namespace != "flow":
            print_unknown_flow_subcommand(command_name, flow_command)
            return 1

        return _dispatch_command(CANONICAL_FLOW_PREFIX, flow_spec, flow_arguments)

    command_spec = COMMAND_SPEC_BY_NAME.get(command)
    if command_spec is not None:
        if command_spec.canonical_namespace == "flow" and not command_spec.compatibility_top_level:
            print_unknown_command(command_name, command)
            return 1
        return _dispatch_command(command_name, command_spec, command_arguments)

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
        ReviewManifestError,
        ReportPresentationError,
        ReviewTaskGenerationError,
        ReviewVerificationError,
        SummarizeConfigError,
        SummarizeBatchingError,
        SummarizeDiscoveryError,
        SummarizeManifestError,
        SummarizePlanningError,
        SummarizeTaskGenerationError,
        SummarizeVerificationError,
        UpdateInstallationError,
        TaskArtifactError,
        TaskConfigError,
        WorkflowStateError,
    ) as exc:
        print(f"{resolve_command_name()}: {exc}", file=sys.stderr)
        status = 1

    raise SystemExit(status)


if __name__ == "__main__":
    run()
