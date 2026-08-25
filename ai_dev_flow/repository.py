from __future__ import annotations

from dataclasses import dataclass
import shutil
from pathlib import Path
from collections.abc import Sequence
import subprocess


_MANAGED_EXCLUDE_BEGIN = "# BEGIN ai-dev managed excludes"
_MANAGED_EXCLUDE_END = "# END ai-dev managed excludes"


class RepositoryError(Exception):
    """Raised when repository context cannot be resolved."""


@dataclass(frozen=True)
class BranchComparison:
    main_branch: str
    scratch_branch: str
    main_exists: bool
    scratch_exists: bool
    main_head: str | None
    scratch_head: str | None
    scratch_ahead_of_main: int | None
    scratch_behind_main: int | None

    @property
    def points_to_same_commit(self) -> bool | None:
        if self.main_head is None or self.scratch_head is None:
            return None

        return self.main_head == self.scratch_head


@dataclass(frozen=True)
class TrackedUpstream:
    local_branch: str
    remote_name: str
    merge_ref: str


@dataclass(frozen=True)
class MainUpstreamComparison:
    main_branch: str
    upstream: TrackedUpstream
    main_head: str
    upstream_head: str
    upstream_ref: str
    local_ahead_of_upstream: int
    local_behind_upstream: int

    @property
    def relationship(self) -> str:
        if self.local_ahead_of_upstream == 0 and self.local_behind_upstream == 0:
            return "equal"
        if self.local_ahead_of_upstream > 0 and self.local_behind_upstream == 0:
            return "upstream-behind"
        if self.local_ahead_of_upstream == 0 and self.local_behind_upstream > 0:
            return "upstream-ahead"
        return "diverged"


def _run_git(
    repo_root: Path,
    arguments: list[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
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
        stderr = completed.stderr.strip()
        if stderr:
            raise RepositoryError(stderr)

        raise RepositoryError(
            f"git {' '.join(arguments)} failed with exit code "
            f"{completed.returncode}."
        )

    return completed


def _git_path_for_repo_root(repo_root: Path, path_fragment: str) -> Path:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--git-path", path_fragment],
        check=True,
    )
    resolved = completed.stdout.strip()
    if not resolved:
        raise RepositoryError(
            f"Cannot resolve Git path for '{path_fragment}'."
        )

    candidate = Path(resolved)
    if candidate.is_absolute():
        return candidate

    return repo_root / candidate


def _resolve_branch_head(repo_root: Path, branch_name: str) -> str | None:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--verify", branch_name],
        check=False,
    )

    if completed.returncode != 0:
        return None

    return completed.stdout.strip() or None


def _git_operation_sentinel_exists(repo_root: Path, path_fragment: str) -> bool:
    sentinel_path = _git_path_for_repo_root(repo_root, path_fragment)
    return sentinel_path.exists()


def resolve_repo_root(cwd: Path | None = None) -> Path:
    working_directory = Path.cwd() if cwd is None else cwd

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(working_directory),
            "rev-parse",
            "--show-toplevel",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    if completed.returncode != 0:
        raise RepositoryError(
            "Not inside a Git repository. "
            "Run this command from within a repository."
        )

    return Path(completed.stdout.strip())


def workflow_state_file_for_repo_root(repo_root: Path) -> Path:
    return repo_root / ".ai-dev" / "workflow.json"


def blocked_workflows_file_for_repo_root(repo_root: Path) -> Path:
    return repo_root / ".ai-dev" / "blocked-workflows.json"


def config_file_for_repo_root(repo_root: Path) -> Path:
    return repo_root / ".ai-dev" / "config.json"


def diff_baseline_file_for_repo_root(repo_root: Path) -> Path:
    return repo_root / ".ai-dev" / "diff-baseline" / "baseline.json"


def clear_diff_baseline_for_repo_root(repo_root: Path) -> None:
    baseline_dir = repo_root / ".ai-dev" / "diff-baseline"
    if not baseline_dir.exists():
        return

    try:
        if baseline_dir.is_dir():
            shutil.rmtree(baseline_dir)
        else:
            baseline_dir.unlink()
    except OSError as exc:
        raise RepositoryError(
            f"Cannot clear review baseline at {baseline_dir}: {exc}"
        ) from exc


def ensure_local_state_excluded(repo_root: Path) -> None:
    sync_local_excludes(repo_root)


def sync_local_excludes(
    repo_root: Path,
) -> None:
    exclude_path = _git_path_for_repo_root(repo_root, "info/exclude")

    existing_text = ""
    if exclude_path.exists():
        try:
            existing_text = exclude_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RepositoryError(
                f"Cannot read local exclude file {exclude_path}: {exc}"
            ) from exc

    existing_lines = existing_text.splitlines()
    preserved_lines = _strip_managed_exclude_block(existing_lines)

    managed_entries = [".ai-dev/"]

    next_text = _compose_exclude_text(
        preserved_lines=preserved_lines,
        managed_entries=managed_entries,
    )

    if next_text == existing_text:
        return

    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(next_text, encoding="utf-8")
    except OSError as exc:
        raise RepositoryError(
            f"Cannot write local exclude file {exclude_path}: {exc}"
        ) from exc


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    return _resolve_branch_head(repo_root, branch_name) is not None


def ensure_branch_exists(repo_root: Path, branch_name: str) -> None:
    if not branch_exists(repo_root, branch_name):
        raise RepositoryError(f"Required branch does not exist: {branch_name}")


def resolve_tracked_upstream(
    repo_root: Path,
    *,
    branch_name: str,
) -> TrackedUpstream | None:
    remote = _run_git(
        repo_root,
        ["config", "--get", f"branch.{branch_name}.remote"],
        check=False,
    )
    if remote.returncode == 1:
        return None
    if remote.returncode != 0:
        raise RepositoryError(remote.stderr.strip() or "Cannot resolve branch upstream remote.")

    remote_name = remote.stdout.strip()
    if not remote_name or remote_name == ".":
        return None

    merge = _run_git(
        repo_root,
        ["config", "--get", f"branch.{branch_name}.merge"],
        check=False,
    )
    if merge.returncode != 0:
        raise RepositoryError(
            f"Tracked branch {branch_name} is missing its upstream merge ref."
        )

    merge_ref = merge.stdout.strip()
    if not merge_ref.startswith("refs/heads/") or merge_ref == "refs/heads/":
        raise RepositoryError(
            f"Tracked branch {branch_name} has invalid upstream merge ref: {merge_ref!r}."
        )

    return TrackedUpstream(
        local_branch=branch_name,
        remote_name=remote_name,
        merge_ref=merge_ref,
    )


def fetch_tracked_upstream(repo_root: Path, *, upstream: TrackedUpstream) -> None:
    _run_git(repo_root, ["fetch", "--no-tags", upstream.remote_name], check=True)


def compare_main_to_tracked_upstream(
    repo_root: Path,
    *,
    main_branch: str,
    upstream: TrackedUpstream,
) -> MainUpstreamComparison:
    if main_branch != upstream.local_branch:
        raise RepositoryError(
            "Tracked upstream branch does not match the requested main branch."
        )

    main_head = _resolve_branch_head(repo_root, main_branch)
    if main_head is None:
        raise RepositoryError(f"Required branch does not exist: {main_branch}")

    upstream_ref_result = _run_git(
        repo_root,
        ["rev-parse", "--symbolic-full-name", f"{main_branch}@{{upstream}}"],
        check=True,
    )
    upstream_ref = upstream_ref_result.stdout.strip()
    if not upstream_ref:
        raise RepositoryError(f"Cannot resolve tracked upstream for {main_branch}.")

    upstream_head_result = _run_git(
        repo_root,
        ["rev-parse", "--verify", upstream_ref],
        check=True,
    )
    upstream_head = upstream_head_result.stdout.strip()
    if not upstream_head:
        raise RepositoryError(f"Cannot resolve tracked upstream commit for {main_branch}.")

    counts_result = _run_git(
        repo_root,
        ["rev-list", "--left-right", "--count", f"{upstream_ref}...{main_branch}"],
        check=True,
    )
    counts = counts_result.stdout.strip().split()
    if len(counts) != 2:
        raise RepositoryError("Cannot determine local main and upstream relationship.")

    return MainUpstreamComparison(
        main_branch=main_branch,
        upstream=upstream,
        main_head=main_head,
        upstream_head=upstream_head,
        upstream_ref=upstream_ref,
        local_ahead_of_upstream=int(counts[1]),
        local_behind_upstream=int(counts[0]),
    )


def push_main_to_tracked_upstream(
    repo_root: Path,
    *,
    main_branch: str,
    upstream: TrackedUpstream,
) -> None:
    if main_branch != upstream.local_branch:
        raise RepositoryError(
            "Tracked upstream branch does not match the requested main branch."
        )

    _run_git(
        repo_root,
        ["push", "--porcelain", upstream.remote_name, f"{main_branch}:{upstream.merge_ref}"],
        check=True,
    )


def active_git_operations(repo_root: Path) -> list[str]:
    operations: list[tuple[str, str]] = [
        ("merge", "MERGE_HEAD"),
        ("rebase", "rebase-apply"),
        ("rebase", "rebase-merge"),
        ("cherry-pick", "CHERRY_PICK_HEAD"),
        ("revert", "REVERT_HEAD"),
        ("bisect", "BISECT_LOG"),
    ]

    active: list[str] = []
    for label, sentinel in operations:
        if _git_operation_sentinel_exists(repo_root, sentinel):
            if label not in active:
                active.append(label)

    return active


def ensure_no_active_git_operations(repo_root: Path) -> None:
    active = active_git_operations(repo_root)
    if not active:
        return

    joined = ", ".join(active)
    raise RepositoryError(
        "Cannot proceed while Git has active operation(s): "
        f"{joined}. Resolve or abort the operation first."
    )


def checkout_branch(repo_root: Path, branch_name: str) -> None:
    _run_git(repo_root, ["checkout", branch_name], check=True)


def create_or_reset_branch_from_source(
    repo_root: Path,
    *,
    branch_name: str,
    source_branch: str,
) -> None:
    if branch_exists(repo_root, branch_name):
        _run_git(repo_root, ["branch", "-f", branch_name, source_branch], check=True)
        return

    _run_git(repo_root, ["branch", branch_name, source_branch], check=True)


def delete_branch(repo_root: Path, branch_name: str) -> None:
    """Delete a local branch regardless of its merge state.

    Used to undo a branch this process created; callers must not invoke it on a
    branch they did not create in the same operation.
    """
    _run_git(repo_root, ["branch", "-D", branch_name], check=True)


def ensure_branches_point_to_same_commit(
    repo_root: Path,
    *,
    left_branch: str,
    right_branch: str,
) -> None:
    left_head = _resolve_branch_head(repo_root, left_branch)
    right_head = _resolve_branch_head(repo_root, right_branch)

    if left_head is None or right_head is None:
        raise RepositoryError(
            "Cannot compare branch commits because one or both branches are missing."
        )

    if left_head != right_head:
        raise RepositoryError(
            f"Branches are not aligned: {left_branch}={left_head}, "
            f"{right_branch}={right_head}."
        )


def current_branch_name(repo_root: Path) -> str:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
    )

    branch_name = completed.stdout.strip()
    if not branch_name:
        raise RepositoryError("Cannot determine current Git branch.")

    return branch_name


def git_status_short(
    repo_root: Path,
    *,
    excluded_paths: Sequence[str] | None = None,
) -> list[str]:
    active_excludes = () if excluded_paths is None else excluded_paths
    completed = _run_git(
        repo_root,
        [
            "status",
            "--short",
            "--untracked-files=all",
            *_pathspec_arguments(active_excludes),
        ],
        check=True,
    )

    return [line for line in completed.stdout.splitlines() if line]


def git_status_short_filtered(
    repo_root: Path,
    *,
    excluded_paths: Sequence[str],
) -> list[str]:
    return git_status_short(repo_root, excluded_paths=excluded_paths)


def ensure_clean_working_tree_and_index(repo_root: Path) -> None:
    if git_status_short(repo_root):
        raise RepositoryError("Repository must have a clean working tree and index.")


def has_committable_changes(repo_root: Path) -> bool:
    return bool(git_status_short(repo_root))


def stage_all_changes(repo_root: Path) -> None:
    _run_git(repo_root, ["add", "--all", "."], check=True)


def create_commit(repo_root: Path, *, message: str) -> str:
    _run_git(repo_root, ["commit", "-m", message], check=True)
    return resolve_commit_hash(repo_root, "HEAD")


def resolve_commit_hash(repo_root: Path, revision: str = "HEAD") -> str:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--verify", revision],
        check=True,
    )

    commit_hash = completed.stdout.strip()
    if not commit_hash:
        raise RepositoryError(f"Cannot resolve commit hash for {revision}.")

    return commit_hash


def resolve_revision_commit(repo_root: Path, revision: str) -> str:
    """Resolve a caller-supplied commit-ish to its full commit hash.

    Fails closed for empty, option-like, unresolvable, and non-commit
    revisions, plus the ambiguous abbreviated hashes ``--verify`` rejects,
    instead of letting Git interpret them loosely.
    """
    normalized = revision.strip()
    if not normalized:
        raise RepositoryError("Revision cannot be empty.")

    if normalized.startswith("-"):
        raise RepositoryError(f"Revision cannot start with '-': {normalized}")

    completed = _run_git(
        repo_root,
        ["rev-parse", "--verify", f"{normalized}^{{commit}}"],
        check=False,
    )

    commit_hash = completed.stdout.strip()
    if completed.returncode != 0 or not commit_hash:
        detail = completed.stderr.strip()
        message = f"Cannot resolve revision to a commit: {normalized}"
        if detail:
            message = f"{message}. {detail}"
        else:
            message = f"{message}."
        raise RepositoryError(message)

    return commit_hash


def resolve_short_commit_hash(repo_root: Path, revision: str = "HEAD") -> str:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--short", "--verify", revision],
        check=True,
    )

    commit_hash = completed.stdout.strip()
    if not commit_hash:
        raise RepositoryError(
            f"Cannot resolve short commit hash for {revision}."
        )

    return commit_hash


def resolve_tree_hash(repo_root: Path, revision: str = "HEAD") -> str:
    completed = _run_git(
        repo_root,
        ["rev-parse", "--verify", f"{revision}^{{tree}}"],
        check=True,
    )

    tree_hash = completed.stdout.strip()
    if not tree_hash:
        raise RepositoryError(f"Cannot resolve tree hash for {revision}.")

    return tree_hash


def resolve_managed_ref(repo_root: Path, ref_name: str) -> str | None:
    completed = _run_git(
        repo_root,
        ["show-ref", "--verify", "--hash", ref_name],
        check=False,
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if completed.returncode == 128 and "not a valid ref" in stderr:
            return None
        raise RepositoryError(stderr or f"Cannot resolve Git ref {ref_name}.")
    return completed.stdout.strip()


def create_managed_ref(repo_root: Path, ref_name: str, commit: str) -> bool:
    existing = resolve_managed_ref(repo_root, ref_name)
    if existing is not None:
        if existing != commit:
            raise RepositoryError(
                f"Managed ref {ref_name} already points to {existing}, not {commit}."
            )
        return False

    completed = _run_git(repo_root, ["update-ref", ref_name, commit], check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RepositoryError(stderr or f"Cannot create managed Git ref {ref_name}.")
    return True


def delete_managed_ref(repo_root: Path, ref_name: str, commit: str) -> None:
    existing = resolve_managed_ref(repo_root, ref_name)
    if existing is None:
        return
    if existing != commit:
        raise RepositoryError(
            f"Refusing to delete managed ref {ref_name}: it points to {existing}, not {commit}."
        )

    completed = _run_git(
        repo_root,
        ["update-ref", "-d", ref_name, commit],
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RepositoryError(stderr or f"Cannot delete managed Git ref {ref_name}.")


def hard_reset_branch_to_revision(
    repo_root: Path,
    *,
    branch_name: str,
    revision: str,
) -> None:
    _run_git(repo_root, ["checkout", branch_name], check=True)
    _run_git(repo_root, ["reset", "--hard", revision], check=True)


def restore_branch_to_revision(
    repo_root: Path,
    *,
    branch_name: str,
    revision: str,
) -> None:
    if current_branch_name(repo_root) == branch_name:
        _run_git(repo_root, ["reset", "--hard", revision], check=True)
        return

    _run_git(repo_root, ["branch", "-f", branch_name, revision], check=True)


def squash_merge_branch_into_current(repo_root: Path, source_branch: str) -> None:
    _run_git(repo_root, ["merge", "--squash", source_branch], check=True)


def clean_untracked_non_ignored(repo_root: Path) -> None:
    _run_git(repo_root, ["clean", "-fd"], check=True)


def diff_against_revision(
    repo_root: Path,
    *,
    revision: str,
    excluded_paths: Sequence[str] | None = None,
) -> str:
    active_excludes = () if excluded_paths is None else excluded_paths
    completed = _run_git(
        repo_root,
        [
            "diff",
            "--find-renames",
            "--no-ext-diff",
            revision,
            *_pathspec_arguments(active_excludes),
        ],
        check=True,
    )

    return completed.stdout


def diff_numstat_against_revision(
    repo_root: Path,
    *,
    revision: str,
    excluded_paths: Sequence[str] | None = None,
) -> list[str]:
    active_excludes = () if excluded_paths is None else excluded_paths
    completed = _run_git(
        repo_root,
        [
            "diff",
            "--numstat",
            "--find-renames",
            "--no-ext-diff",
            revision,
            *_pathspec_arguments(active_excludes),
        ],
        check=True,
    )

    return [line for line in completed.stdout.splitlines() if line]


def compare_main_and_scratch(
    repo_root: Path,
    *,
    main_branch: str,
    scratch_branch: str,
) -> BranchComparison:
    main_head = _resolve_branch_head(repo_root, main_branch)
    scratch_head = _resolve_branch_head(repo_root, scratch_branch)

    scratch_ahead_of_main: int | None = None
    scratch_behind_main: int | None = None

    if main_head is not None and scratch_head is not None:
        completed = _run_git(
            repo_root,
            ["rev-list", "--left-right", "--count", f"{main_branch}...{scratch_branch}"],
            check=True,
        )
        counts = completed.stdout.strip().split()
        if len(counts) != 2:
            raise RepositoryError(
                "Cannot determine branch relationship for main and scratch."
            )

        scratch_behind_main = int(counts[0])
        scratch_ahead_of_main = int(counts[1])

    return BranchComparison(
        main_branch=main_branch,
        scratch_branch=scratch_branch,
        main_exists=main_head is not None,
        scratch_exists=scratch_head is not None,
        main_head=main_head,
        scratch_head=scratch_head,
        scratch_ahead_of_main=scratch_ahead_of_main,
        scratch_behind_main=scratch_behind_main,
    )


def branch_is_ancestor(
    repo_root: Path,
    *,
    ancestor_revision: str,
    descendant_revision: str,
) -> bool:
    completed = _run_git(
        repo_root,
        ["merge-base", "--is-ancestor", ancestor_revision, descendant_revision],
        check=False,
    )

    if completed.returncode == 0:
        return True

    if completed.returncode == 1:
        return False

    stderr = completed.stderr.strip()
    if stderr:
        raise RepositoryError(stderr)

    raise RepositoryError(
        "Cannot determine whether one revision is an ancestor of another."
    )


def scratch_commits_relative_to_main(
    repo_root: Path,
    *,
    main_branch: str,
    scratch_branch: str,
) -> list[str]:
    if _resolve_branch_head(repo_root, main_branch) is None:
        return []

    if _resolve_branch_head(repo_root, scratch_branch) is None:
        return []

    completed = _run_git(
        repo_root,
        ["log", "--oneline", "--no-decorate", f"{main_branch}..{scratch_branch}"],
        check=True,
    )

    return [line for line in completed.stdout.splitlines() if line]


def scratch_commit_summaries_relative_to_main(
    repo_root: Path,
    *,
    main_branch: str,
    scratch_branch: str,
) -> list[str]:
    if _resolve_branch_head(repo_root, main_branch) is None:
        return []

    if _resolve_branch_head(repo_root, scratch_branch) is None:
        return []

    completed = _run_git(
        repo_root,
        [
            "log",
            "--reverse",
            "--format=%h %s",
            "--no-decorate",
            f"{main_branch}..{scratch_branch}",
        ],
        check=True,
    )

    return [line for line in completed.stdout.splitlines() if line]


def scratch_commit_count_relative_to_main(
    repo_root: Path,
    *,
    main_branch: str,
    scratch_branch: str,
) -> int:
    if _resolve_branch_head(repo_root, main_branch) is None:
        return 0

    if _resolve_branch_head(repo_root, scratch_branch) is None:
        return 0

    completed = _run_git(
        repo_root,
        ["rev-list", "--count", f"{main_branch}..{scratch_branch}"],
        check=True,
    )

    count_text = completed.stdout.strip()
    if not count_text:
        return 0

    return int(count_text)


def max_numbered_checkpoint_between(
    repo_root: Path,
    *,
    base_revision: str,
    head_revision: str,
) -> int:
    """Highest numbered checkpoint subject reachable from head but not base.

    Revision-based form of :func:`max_numbered_checkpoint_relative_to_main`, so
    callers can derive a checkpoint before moving any branch.
    """
    if _resolve_branch_head(repo_root, base_revision) is None:
        return 0

    if _resolve_branch_head(repo_root, head_revision) is None:
        return 0

    completed = _run_git(
        repo_root,
        ["log", "--format=%s", f"{base_revision}..{head_revision}"],
        check=True,
    )

    max_checkpoint = 0
    for line in completed.stdout.splitlines():
        text = line.strip()
        if text.isdigit() and not text.startswith("0"):
            value = int(text)
            if value > max_checkpoint:
                max_checkpoint = value

    return max_checkpoint


def max_numbered_checkpoint_relative_to_main(
    repo_root: Path,
    *,
    main_branch: str,
    scratch_branch: str,
) -> int:
    return max_numbered_checkpoint_between(
        repo_root,
        base_revision=main_branch,
        head_revision=scratch_branch,
    )


def commit_count_between(
    repo_root: Path,
    *,
    ancestor_revision: str,
    descendant_revision: str,
) -> int:
    completed = _run_git(
        repo_root,
        ["rev-list", "--count", f"{ancestor_revision}..{descendant_revision}"],
        check=True,
    )
    count_text = completed.stdout.strip()
    if not count_text:
        return 0
    return int(count_text)


def _pathspec_arguments(excluded_paths: Sequence[str]) -> list[str]:
    if not excluded_paths:
        return []

    arguments = ["--", "."]
    arguments.extend(f":(exclude){path}" for path in excluded_paths)
    return arguments


def _strip_managed_exclude_block(lines: list[str]) -> list[str]:
    preserved: list[str] = []
    inside_managed_block = False

    for line in lines:
        if line == _MANAGED_EXCLUDE_BEGIN:
            inside_managed_block = True
            continue

        if line == _MANAGED_EXCLUDE_END and inside_managed_block:
            inside_managed_block = False
            continue

        if not inside_managed_block:
            preserved.append(line)

    return preserved


def _compose_exclude_text(
    *,
    preserved_lines: list[str],
    managed_entries: list[str],
) -> str:
    lines = list(preserved_lines)
    if managed_entries:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(_MANAGED_EXCLUDE_BEGIN)
        lines.extend(managed_entries)
        lines.append(_MANAGED_EXCLUDE_END)

    if not lines:
        return ""

    return "\n".join(lines) + "\n"
