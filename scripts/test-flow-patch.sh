#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW="$ROOT/scripts/flow"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

assert_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" != *"$needle"* ]]; then
		printf 'expected output to contain: %s\n' "$needle" >&2
		printf 'actual output:\n%s\n' "$haystack" >&2
		exit 1
	fi
}

assert_not_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" == *"$needle"* ]]; then
		printf 'expected output not to contain: %s\n' "$needle" >&2
		printf 'actual output:\n%s\n' "$haystack" >&2
		exit 1
	fi
}

assert_equals() {
	local left="$1"
	local right="$2"

	if [[ "$left" != "$right" ]]; then
		printf 'expected: %s\nactual:   %s\n' "$right" "$left" >&2
		exit 1
	fi
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
		git config user.name 'Flow Patch Tests'
		git config user.email 'flow-patch-tests@example.com'
		printf '.ai-dev/workflow.json\n' > .gitignore
		printf 'base\n' > tracked.txt
		printf 'keep\n' > subdir/.keep
		git add .gitignore tracked.txt subdir/.keep
		git commit -q -m 'initial commit'
		git branch -M main
	)
}

run_flow() {
	local cwd="$1"
	shift
	(
		cd "$cwd"
		"$FLOW" "$@"
	)
}

run_flow_capture() {
	local cwd="$1"
	local output_file="$2"
	shift 2

	if run_flow "$cwd" "$@" >"$output_file" 2>&1; then
		return 0
	else
		local status=$?
		return "$status"
	fi
}

state_get() {
	local cwd="$1"
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" __test-state-get
	)
}

state_set() {
	local cwd="$1"
	local payload="$2"
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" __test-state-set "$payload"
	)
}

current_head() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse HEAD
}

branch_head() {
	local repo_root="$1"
	local branch_name="$2"
	git -C "$repo_root" rev-parse "$branch_name"
}

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse --abbrev-ref HEAD
}

repo_status_porcelain() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
}

cached_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --cached --binary --no-ext-diff
}

worktree_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --binary --no-ext-diff
}

create_commit_on_current_branch() {
	local repo_root="$1"
	local file_name="$2"
	local content="$3"
	local message="$4"
	printf '%s\n' "$content" > "$repo_root/$file_name"
	git -C "$repo_root" add "$file_name"
	git -C "$repo_root" commit -q -m "$message"
}

# help
repo_help="$TMP_DIR/repo-help"
init_repo "$repo_help"
help_text="$(run_flow "$repo_help/subdir" help)"
assert_contains "$help_text" 'patch      Begin or adopt a local patch workflow on scratch.'

patch_help_short="$(run_flow "$repo_help/subdir" patch -h)"
patch_help_long="$(run_flow "$repo_help/subdir" patch --help)"
assert_equals "$patch_help_short" "$patch_help_long"
assert_contains "$patch_help_short" 'Usage: flow patch "<description>"'
assert_contains "$patch_help_short" 'flow patch --adopt "<description>"'
assert_contains "$patch_help_short" 'without changing commits, index, or working tree'

# clean patch start resets scratch and records patch state
repo_clean="$TMP_DIR/repo-clean"
init_repo "$repo_clean"
git -C "$repo_clean" checkout -q -b scratch
create_commit_on_current_branch "$repo_clean" scratch-old.txt 'old' 'old'
git -C "$repo_clean" checkout -q main
clean_main_before="$(branch_head "$repo_clean" main)"
if run_flow_capture "$repo_clean/subdir" "$TMP_DIR/clean-output" patch 'Local tidy'; then
	clean_status=0
else
	clean_status=$?
fi
clean_text="$(cat "$TMP_DIR/clean-output")"
assert_equals "$clean_status" '0'
assert_equals "$clean_text" $'Started patch: Local tidy\nmainBranch: main\nscratchBranch: scratch\ncheckpoint: 0'
assert_equals "$(current_branch "$repo_clean")" 'scratch'
assert_equals "$(branch_head "$repo_clean" scratch)" "$clean_main_before"
assert_equals "$(state_get "$repo_clean/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "patchDescription": "Local tidy"
}'

# clean patch start refuses dirty worktree with existing warning
repo_dirty="$TMP_DIR/repo-dirty"
init_repo "$repo_dirty"
printf 'dirty\n' >> "$repo_dirty/tracked.txt"
if run_flow_capture "$repo_dirty/subdir" "$TMP_DIR/dirty-output" patch 'Dirty'; then
	dirty_status=0
else
	dirty_status=$?
fi
dirty_text="$(cat "$TMP_DIR/dirty-output")"
assert_equals "$dirty_status" '1'
assert_contains "$dirty_text" 'Working tree is not clean. Commit, stash, or remove changes before starting.'

# patch argument validation
repo_args="$TMP_DIR/repo-args"
init_repo "$repo_args"

if run_flow_capture "$repo_args/subdir" "$TMP_DIR/empty-output" patch ''; then
	empty_status=0
else
	empty_status=$?
fi
assert_equals "$empty_status" '1'
assert_contains "$(cat "$TMP_DIR/empty-output")" 'patch description cannot be empty.'

if run_flow_capture "$repo_args/subdir" "$TMP_DIR/adopt-empty-output" patch --adopt ''; then
	adopt_empty_status=0
else
	adopt_empty_status=$?
fi
assert_equals "$adopt_empty_status" '1'
assert_contains "$(cat "$TMP_DIR/adopt-empty-output")" 'patch description cannot be empty.'

if run_flow_capture "$repo_args/subdir" "$TMP_DIR/adopt-missing-output" patch --adopt; then
	adopt_missing_status=0
else
	adopt_missing_status=$?
fi
assert_equals "$adopt_missing_status" '1'
assert_contains "$(cat "$TMP_DIR/adopt-missing-output")" 'Usage: flow patch [--adopt] "<description>"'

if run_flow_capture "$repo_args/subdir" "$TMP_DIR/ordering-output" patch 'desc' --adopt; then
	ordering_status=0
else
	ordering_status=$?
fi
assert_equals "$ordering_status" '1'
assert_contains "$(cat "$TMP_DIR/ordering-output")" 'Usage: flow patch [--adopt] "<description>"'

if run_flow_capture "$repo_args/subdir" "$TMP_DIR/extra-output" patch --adopt 'desc' extra; then
	extra_status=0
else
	extra_status=$?
fi
assert_equals "$extra_status" '1'
assert_contains "$(cat "$TMP_DIR/extra-output")" 'Usage: flow patch [--adopt] "<description>"'

# active workflow conflict
repo_conflict_issue="$TMP_DIR/repo-conflict-issue"
init_repo "$repo_conflict_issue"
state_set "$repo_conflict_issue/subdir" '{"activeIssueNumber":9,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
if run_flow_capture "$repo_conflict_issue/subdir" "$TMP_DIR/conflict-issue-output" patch 'Conflict'; then
	conflict_issue_status=0
else
	conflict_issue_status=$?
fi
assert_equals "$conflict_issue_status" '1'
assert_contains "$(cat "$TMP_DIR/conflict-issue-output")" 'active issue 9 is already set'

repo_conflict_patch="$TMP_DIR/repo-conflict-patch"
init_repo "$repo_conflict_patch"
state_set "$repo_conflict_patch/subdir" '{"patchDescription":"Existing patch","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
if run_flow_capture "$repo_conflict_patch/subdir" "$TMP_DIR/conflict-patch-output" patch 'Conflict'; then
	conflict_patch_status=0
else
	conflict_patch_status=$?
fi
assert_equals "$conflict_patch_status" '1'
assert_contains "$(cat "$TMP_DIR/conflict-patch-output")" 'active patch Existing patch is already set'

# adopt requires current branch scratch
repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" checkout -q -b scratch
git -C "$repo_wrong_branch" checkout -q main
if run_flow_capture "$repo_wrong_branch/subdir" "$TMP_DIR/wrong-branch-output" patch --adopt 'Adopt'; then
	wrong_branch_status=0
else
	wrong_branch_status=$?
fi
wrong_branch_text="$(cat "$TMP_DIR/wrong-branch-output")"
assert_equals "$wrong_branch_status" '1'
assert_contains "$wrong_branch_text" 'current branch main does not match scratchBranch scratch'

# adopt preserves existing commits/index/worktree/untracked files and infers checkpoint
repo_adopt="$TMP_DIR/repo-adopt"
init_repo "$repo_adopt"
git -C "$repo_adopt" checkout -q -b scratch
create_commit_on_current_branch "$repo_adopt" c1.txt 'c1' '1'
create_commit_on_current_branch "$repo_adopt" c2.txt 'c2' '2'
printf 'staged\n' >> "$repo_adopt/tracked.txt"
git -C "$repo_adopt" add tracked.txt
printf 'unstaged\n' >> "$repo_adopt/tracked.txt"
printf 'note\n' > "$repo_adopt/untracked.txt"
adopt_branch_before="$(current_branch "$repo_adopt")"
adopt_head_before="$(current_head "$repo_adopt")"
adopt_main_before="$(branch_head "$repo_adopt" main)"
adopt_scratch_before="$(branch_head "$repo_adopt" scratch)"
adopt_status_before="$(repo_status_porcelain "$repo_adopt")"
adopt_index_before="$(cached_diff "$repo_adopt")"
adopt_worktree_before="$(worktree_diff "$repo_adopt")"
adopt_tracked_before="$(cat "$repo_adopt/tracked.txt")"
adopt_untracked_before="$(cat "$repo_adopt/untracked.txt")"
if run_flow_capture "$repo_adopt/subdir" "$TMP_DIR/adopt-output" patch --adopt 'Adopt existing scratch'; then
	adopt_status=0
else
	adopt_status=$?
fi
adopt_text="$(cat "$TMP_DIR/adopt-output")"
assert_equals "$adopt_status" '0'
assert_contains "$adopt_text" 'Adopted patch: Adopt existing scratch'
assert_contains "$adopt_text" 'checkpoint: 2'
assert_equals "$(current_branch "$repo_adopt")" "$adopt_branch_before"
assert_equals "$(current_head "$repo_adopt")" "$adopt_head_before"
assert_equals "$(branch_head "$repo_adopt" main)" "$adopt_main_before"
assert_equals "$(branch_head "$repo_adopt" scratch)" "$adopt_scratch_before"
assert_equals "$(repo_status_porcelain "$repo_adopt")" "$adopt_status_before"
assert_equals "$(cached_diff "$repo_adopt")" "$adopt_index_before"
assert_equals "$(worktree_diff "$repo_adopt")" "$adopt_worktree_before"
assert_equals "$(cat "$repo_adopt/tracked.txt")" "$adopt_tracked_before"
assert_equals "$(cat "$repo_adopt/untracked.txt")" "$adopt_untracked_before"
assert_equals "$(state_get "$repo_adopt/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 2,
  "patchDescription": "Adopt existing scratch"
}'

# adopt with no numbered checkpoint commits initializes checkpoint to zero
repo_adopt_zero="$TMP_DIR/repo-adopt-zero"
init_repo "$repo_adopt_zero"
git -C "$repo_adopt_zero" checkout -q -b scratch
create_commit_on_current_branch "$repo_adopt_zero" note.txt 'n' 'feature work'
if run_flow_capture "$repo_adopt_zero/subdir" "$TMP_DIR/adopt-zero-output" patch --adopt 'Adopt zero'; then
	adopt_zero_status=0
else
	adopt_zero_status=$?
fi
adopt_zero_text="$(cat "$TMP_DIR/adopt-zero-output")"
assert_equals "$adopt_zero_status" '0'
assert_contains "$adopt_zero_text" 'checkpoint: 0'

# status and verbose status for patches
repo_status="$TMP_DIR/repo-status"
init_repo "$repo_status"
git -C "$repo_status" checkout -q -b scratch
state_set "$repo_status/subdir" '{"patchDescription":"Patch status","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
status_default="$(run_flow "$repo_status/subdir" status)"
status_verbose="$(run_flow "$repo_status/subdir" status --verbose)"
assert_equals "$status_default" $'Patch: Patch status\nBranch: scratch'
assert_contains "$status_verbose" '  type: patch'
assert_contains "$status_verbose" '  patch: Patch status'
assert_not_contains "$status_verbose" 'issue URL'
assert_not_contains "$status_verbose" 'github.com'

# review identifies patch
repo_review="$TMP_DIR/repo-review"
init_repo "$repo_review"
git -C "$repo_review" checkout -q -b scratch
state_set "$repo_review/subdir" '{"patchDescription":"Patch review","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'review\n' >> "$repo_review/tracked.txt"
git -C "$repo_review" add tracked.txt
if run_flow_capture "$repo_review/subdir" "$TMP_DIR/review-output" review; then
	review_status=0
else
	review_status=$?
fi
review_text="$(cat "$TMP_DIR/review-output")"
assert_equals "$review_status" '0'
assert_contains "$review_text" 'Patch: Patch review'
assert_contains "$review_text" 'diff --git a/tracked.txt b/tracked.txt'

# commit/promote/reset/complete support patch workflows
repo_lifecycle="$TMP_DIR/repo-lifecycle"
init_repo "$repo_lifecycle"
git -C "$repo_lifecycle" checkout -q -b scratch
if run_flow_capture "$repo_lifecycle/subdir" "$TMP_DIR/lifecycle-patch-output" patch 'Lifecycle patch'; then
	lifecycle_patch_status=0
else
	lifecycle_patch_status=$?
fi
assert_equals "$lifecycle_patch_status" '0'

printf 'checkpoint one\n' >> "$repo_lifecycle/tracked.txt"
git -C "$repo_lifecycle" add tracked.txt
if run_flow_capture "$repo_lifecycle/subdir" "$TMP_DIR/lifecycle-commit-output" commit; then
	lifecycle_commit_status=0
else
	lifecycle_commit_status=$?
fi
lifecycle_commit_text="$(cat "$TMP_DIR/lifecycle-commit-output")"
assert_equals "$lifecycle_commit_status" '0'
assert_contains "$lifecycle_commit_text" 'Created checkpoint 1'
assert_contains "$lifecycle_commit_text" 'patch: Lifecycle patch'
assert_contains "$(state_get "$repo_lifecycle/subdir")" '"checkpoint": 1'

if run_flow_capture "$repo_lifecycle/subdir" "$TMP_DIR/lifecycle-promote-output" promote 'Promote patch'; then
	lifecycle_promote_status=0
else
	lifecycle_promote_status=$?
fi
lifecycle_promote_text="$(cat "$TMP_DIR/lifecycle-promote-output")"
assert_equals "$lifecycle_promote_status" '0'
assert_contains "$lifecycle_promote_text" 'patch: Lifecycle patch'
assert_contains "$lifecycle_promote_text" 'checkpoint: 0'

if run_flow_capture "$repo_lifecycle/subdir" "$TMP_DIR/lifecycle-complete-output" complete; then
	lifecycle_complete_status=0
else
	lifecycle_complete_status=$?
fi
lifecycle_complete_text="$(cat "$TMP_DIR/lifecycle-complete-output")"
assert_equals "$lifecycle_complete_status" '0'
assert_contains "$lifecycle_complete_text" 'Completed patch: Lifecycle patch'
assert_not_contains "$(state_get "$repo_lifecycle/subdir")" 'patchDescription'

# reset support for patch workflows
repo_reset="$TMP_DIR/repo-reset"
init_repo "$repo_reset"
git -C "$repo_reset" checkout -q -b scratch
state_set "$repo_reset/subdir" '{"patchDescription":"Patch reset","mainBranch":"main","scratchBranch":"scratch","checkpoint":3}' >/dev/null
printf 'staged\n' >> "$repo_reset/tracked.txt"
git -C "$repo_reset" add tracked.txt
printf 'unstaged\n' >> "$repo_reset/tracked.txt"
printf 'untracked\n' > "$repo_reset/new.txt"
if run_flow_capture "$repo_reset/subdir" "$TMP_DIR/reset-output" reset; then
	reset_status=0
else
	reset_status=$?
fi
reset_text="$(cat "$TMP_DIR/reset-output")"
assert_equals "$reset_status" '0'
assert_contains "$reset_text" 'Reset scratch to main'
assert_contains "$reset_text" 'patch: Patch reset'
assert_equals "$(repo_status_porcelain "$repo_reset")" ''
assert_equals "$(state_get "$repo_reset/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "patchDescription": "Patch reset"
}'

# existing issue-backed behavior still works
repo_issue="$TMP_DIR/repo-issue"
init_repo "$repo_issue"
if run_flow_capture "$repo_issue/subdir" "$TMP_DIR/issue-start-output" start 44; then
	issue_start_status=0
else
	issue_start_status=$?
fi
issue_start_text="$(cat "$TMP_DIR/issue-start-output")"
assert_equals "$issue_start_status" '0'
assert_contains "$issue_start_text" 'Started issue 44'
assert_contains "$(state_get "$repo_issue/subdir")" '"activeIssueNumber": 44'
assert_not_contains "$(state_get "$repo_issue/subdir")" 'patchDescription'

printf 'flow patch tests passed\n'
