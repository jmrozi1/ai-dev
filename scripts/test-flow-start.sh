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
		exit 1
	fi
}

assert_not_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" == *"$needle"* ]]; then
		printf 'expected output not to contain: %s\n' "$needle" >&2
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

assert_not_exists() {
	local path="$1"

	if [[ -e "$path" || -L "$path" ]]; then
		printf 'expected path to be absent: %s\n' "$path" >&2
		exit 1
	fi
}

assert_file_exists() {
	local path="$1"

	if [[ ! -f "$path" ]]; then
		printf 'expected file to exist: %s\n' "$path" >&2
		exit 1
	fi
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
		git config user.name 'Flow Tests'
		git config user.email 'flow-tests@example.com'
		printf '.ai-dev/workflow.json\n' > .gitignore
		printf 'base\n' > tracked.txt
		git add .gitignore tracked.txt
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

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" branch --show-current
}

current_head() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse HEAD
}

repo_status_porcelain() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
}

assert_repo_clean() {
	local repo_root="$1"
	assert_equals "$(repo_status_porcelain "$repo_root")" ''
}

# missing argument
repo_missing_arg="$TMP_DIR/repo-missing-arg"
init_repo "$repo_missing_arg"
missing_arg_output="$TMP_DIR/missing-arg-output"
if run_flow_capture "$repo_missing_arg/subdir" "$missing_arg_output" start; then
	missing_arg_status=0
else
	missing_arg_status=$?
fi
missing_arg_text="$(cat "$missing_arg_output")"
assert_equals "$missing_arg_status" "1"
assert_contains "$missing_arg_text" 'Usage: flow start <issue-number>'

# extra argument
extra_arg_output="$TMP_DIR/extra-arg-output"
if run_flow_capture "$repo_missing_arg/subdir" "$extra_arg_output" start 1 extra; then
	extra_arg_status=0
else
	extra_arg_status=$?
fi
extra_arg_text="$(cat "$extra_arg_output")"
assert_equals "$extra_arg_status" "1"
assert_contains "$extra_arg_text" 'Usage: flow start <issue-number>'

# explicitly empty extra argument
empty_extra_output="$TMP_DIR/empty-extra-output"
if run_flow_capture "$repo_missing_arg/subdir" "$empty_extra_output" start 1 ''; then
	empty_extra_status=0
else
	empty_extra_status=$?
fi
empty_extra_text="$(cat "$empty_extra_output")"
assert_equals "$empty_extra_status" "1"
assert_contains "$empty_extra_text" 'Usage: flow start <issue-number>'

# too many extra arguments
many_extra_output="$TMP_DIR/many-extra-output"
if run_flow_capture "$repo_missing_arg/subdir" "$many_extra_output" start 1 extra more; then
	many_extra_status=0
else
	many_extra_status=$?
fi
many_extra_text="$(cat "$many_extra_output")"
assert_equals "$many_extra_status" "1"
assert_contains "$many_extra_text" 'Usage: flow start <issue-number>'

too_many_extra_output="$TMP_DIR/too-many-extra-output"
if run_flow_capture "$repo_missing_arg/subdir" "$too_many_extra_output" start 1 extra more even-more; then
	too_many_extra_status=0
else
	too_many_extra_status=$?
fi
too_many_extra_text="$(cat "$too_many_extra_output")"
assert_equals "$too_many_extra_status" "1"
assert_contains "$too_many_extra_text" 'Usage: flow start <issue-number>'

# invalid issue numbers
for invalid_issue in 0 -1 nope 1.5; do
	invalid_output="$TMP_DIR/invalid-issue-${invalid_issue//[^a-zA-Z0-9]/_}.out"
	if run_flow_capture "$repo_missing_arg/subdir" "$invalid_output" start "$invalid_issue"; then
		invalid_status=0
	else
		invalid_status=$?
	fi
	invalid_text="$(cat "$invalid_output")"
	assert_equals "$invalid_status" "1"
	assert_contains "$invalid_text" 'issue-number must be a positive integer'
done

# outside git repository
outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" start 1; then
	outside_status=0
else
	outside_status=$?
fi
outside_text="$(cat "$outside_output")"
assert_equals "$outside_status" "1"
assert_contains "$outside_text" 'Not inside a Git repository'

# dirty unstaged file and unchanged branch/head/state on failed validation
repo_dirty_unstaged="$TMP_DIR/repo-dirty-unstaged"
init_repo "$repo_dirty_unstaged"
branch_before="$(current_branch "$repo_dirty_unstaged")"
head_before="$(current_head "$repo_dirty_unstaged")"
state_before="$(state_get "$repo_dirty_unstaged/subdir")"
printf 'dirty\n' >> "$repo_dirty_unstaged/tracked.txt"
dirty_unstaged_output="$TMP_DIR/dirty-unstaged-output"
if run_flow_capture "$repo_dirty_unstaged/subdir" "$dirty_unstaged_output" start 2; then
	dirty_unstaged_status=0
else
	dirty_unstaged_status=$?
fi
dirty_unstaged_text="$(cat "$dirty_unstaged_output")"
assert_equals "$dirty_unstaged_status" "1"
assert_contains "$dirty_unstaged_text" 'Working tree is not clean'
assert_equals "$(current_branch "$repo_dirty_unstaged")" "$branch_before"
assert_equals "$(current_head "$repo_dirty_unstaged")" "$head_before"
assert_equals "$(state_get "$repo_dirty_unstaged/subdir")" "$state_before"

# staged changes
repo_dirty_staged="$TMP_DIR/repo-dirty-staged"
init_repo "$repo_dirty_staged"
printf 'staged\n' >> "$repo_dirty_staged/tracked.txt"
git -C "$repo_dirty_staged" add tracked.txt
staged_output="$TMP_DIR/staged-output"
if run_flow_capture "$repo_dirty_staged/subdir" "$staged_output" start 3; then
	staged_status=0
else
	staged_status=$?
fi
staged_text="$(cat "$staged_output")"
assert_equals "$staged_status" "1"
assert_contains "$staged_text" 'Working tree is not clean'

# untracked file
repo_dirty_untracked="$TMP_DIR/repo-dirty-untracked"
init_repo "$repo_dirty_untracked"
printf 'new\n' > "$repo_dirty_untracked/untracked.txt"
untracked_output="$TMP_DIR/untracked-output"
if run_flow_capture "$repo_dirty_untracked/subdir" "$untracked_output" start 4; then
	untracked_status=0
else
	untracked_status=$?
fi
untracked_text="$(cat "$untracked_output")"
assert_equals "$untracked_status" "1"
assert_contains "$untracked_text" 'Working tree is not clean'

# already-active workflow
repo_active="$TMP_DIR/repo-active"
init_repo "$repo_active"
state_set "$repo_active/subdir" '{"activeIssueNumber":9,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
active_output="$TMP_DIR/active-output"
if run_flow_capture "$repo_active/subdir" "$active_output" start 5; then
	active_status=0
else
	active_status=$?
fi
active_text="$(cat "$active_output")"
assert_equals "$active_status" "1"
assert_contains "$active_text" 'active issue 9 is already set'

# missing local main branch
repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
state_set "$repo_missing_main/subdir" '{"mainBranch":"does-not-exist","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" start 6; then
	missing_main_status=0
else
	missing_main_status=$?
fi
missing_main_text="$(cat "$missing_main_output")"
assert_equals "$missing_main_status" "1"
assert_contains "$missing_main_text" 'Main branch does not exist locally: does-not-exist'

# identical main and scratch branch names
repo_identical="$TMP_DIR/repo-identical"
init_repo "$repo_identical"
state_set "$repo_identical/subdir" '{"mainBranch":"main","scratchBranch":"main","checkpoint":0}' >/dev/null
identical_output="$TMP_DIR/identical-output"
if run_flow_capture "$repo_identical/subdir" "$identical_output" start 7; then
	identical_status=0
else
	identical_status=$?
fi
identical_text="$(cat "$identical_output")"
assert_equals "$identical_status" "1"
assert_contains "$identical_text" 'mainBranch and scratchBranch must be different'

# creation of missing scratch branch and run from subdirectory
repo_create_scratch="$TMP_DIR/repo-create-scratch"
init_repo "$repo_create_scratch"
main_commit_before_create="$(current_head "$repo_create_scratch")"
create_output="$TMP_DIR/create-output"
if run_flow_capture "$repo_create_scratch/subdir" "$create_output" start 21; then
	create_status=0
else
	create_status=$?
fi
create_text="$(cat "$create_output")"
assert_equals "$create_status" "0"
assert_contains "$create_text" 'Started issue 21'
assert_contains "$create_text" 'mainBranch: main'
assert_contains "$create_text" 'scratchBranch: scratch'
assert_contains "$create_text" 'checkpoint: 0'
assert_equals "$(current_branch "$repo_create_scratch")" 'scratch'
assert_equals "$(current_head "$repo_create_scratch")" "$main_commit_before_create"
assert_repo_clean "$repo_create_scratch"
workflow_create_file="$repo_create_scratch/.ai-dev/workflow.json"
assert_file_exists "$workflow_create_file"
git -C "$repo_create_scratch" check-ignore -q .ai-dev/workflow.json
state_create="$(state_get "$repo_create_scratch/subdir")"
assert_contains "$state_create" '"activeIssueNumber": 21'
assert_contains "$state_create" '"mainBranch": "main"'
assert_contains "$state_create" '"scratchBranch": "scratch"'
assert_contains "$state_create" '"checkpoint": 0'
assert_not_contains "$state_create" 'activeIssueTitle'

# resetting existing scratch branch to main and ending on scratch
repo_reset_scratch="$TMP_DIR/repo-reset-scratch"
init_repo "$repo_reset_scratch"
git -C "$repo_reset_scratch" checkout -q -b scratch
printf 'scratch-change\n' > "$repo_reset_scratch/scratch.txt"
git -C "$repo_reset_scratch" add scratch.txt
git -C "$repo_reset_scratch" commit -q -m 'scratch commit'
old_scratch_head="$(current_head "$repo_reset_scratch")"
git -C "$repo_reset_scratch" checkout -q main
main_commit_before_reset="$(current_head "$repo_reset_scratch")"
reset_output="$TMP_DIR/reset-output"
if run_flow_capture "$repo_reset_scratch/subdir" "$reset_output" start 22; then
	reset_status=0
else
	reset_status=$?
fi
reset_text="$(cat "$reset_output")"
assert_equals "$reset_status" "0"
assert_contains "$reset_text" 'Started issue 22'
assert_equals "$(current_branch "$repo_reset_scratch")" 'scratch'
assert_equals "$(current_head "$repo_reset_scratch")" "$main_commit_before_reset"
assert_repo_clean "$repo_reset_scratch"
assert_file_exists "$repo_reset_scratch/.ai-dev/workflow.json"
git -C "$repo_reset_scratch" check-ignore -q .ai-dev/workflow.json
if [[ "$old_scratch_head" == "$(current_head "$repo_reset_scratch")" ]]; then
	printf 'expected scratch branch to be reset to main commit\n' >&2
	exit 1
fi

# custom mainBranch and scratchBranch from existing inactive state
repo_custom_branches="$TMP_DIR/repo-custom-branches"
init_repo "$repo_custom_branches"
git -C "$repo_custom_branches" branch trunk
git -C "$repo_custom_branches" branch work
trunk_head_before="$(git -C "$repo_custom_branches" rev-parse trunk)"
state_set "$repo_custom_branches/subdir" '{"mainBranch":" trunk ","scratchBranch":" work ","checkpoint":5}' >/dev/null
custom_output="$TMP_DIR/custom-output"
if run_flow_capture "$repo_custom_branches/subdir" "$custom_output" start 23; then
	custom_status=0
else
	custom_status=$?
fi
custom_text="$(cat "$custom_output")"
assert_equals "$custom_status" "0"
assert_contains "$custom_text" 'Started issue 23'
assert_contains "$custom_text" 'mainBranch: trunk'
assert_contains "$custom_text" 'scratchBranch: work'
assert_equals "$(current_branch "$repo_custom_branches")" 'work'
assert_equals "$(current_head "$repo_custom_branches")" "$trunk_head_before"
assert_repo_clean "$repo_custom_branches"
assert_file_exists "$repo_custom_branches/.ai-dev/workflow.json"
git -C "$repo_custom_branches" check-ignore -q .ai-dev/workflow.json
custom_state="$(state_get "$repo_custom_branches/subdir")"
assert_contains "$custom_state" '"activeIssueNumber": 23'
assert_contains "$custom_state" '"mainBranch": "trunk"'
assert_contains "$custom_state" '"scratchBranch": "work"'
assert_contains "$custom_state" '"checkpoint": 0'
assert_not_contains "$custom_state" 'activeIssueTitle'

# config file remains visible to Git while workflow file is ignored
repo_config_visibility="$TMP_DIR/repo-config-visibility"
init_repo "$repo_config_visibility"
run_flow "$repo_config_visibility/subdir" set out='reports/a.txt' >/dev/null
config_status_untracked="$(repo_status_porcelain "$repo_config_visibility")"
assert_contains "$config_status_untracked" '?? .ai-dev/config.json'
assert_not_contains "$config_status_untracked" '.ai-dev/workflow.json'
git -C "$repo_config_visibility" add .ai-dev/config.json
git -C "$repo_config_visibility" commit -q -m 'track config for visibility check'
run_flow "$repo_config_visibility/subdir" set out='reports/b.txt' >/dev/null
config_status_modified="$(repo_status_porcelain "$repo_config_visibility")"
assert_contains "$config_status_modified" ' M .ai-dev/config.json'
assert_not_contains "$config_status_modified" '.ai-dev/workflow.json'

# output routing
repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
mkdir -p "$repo_routing/reports"
run_flow "$repo_routing/subdir" set out='reports/start-output.txt' >/dev/null
git -C "$repo_routing" add .ai-dev/config.json
git -C "$repo_routing" commit -q -m 'seed output routing config'
routing_output="$TMP_DIR/routing-output"
if run_flow_capture "$repo_routing/subdir" "$routing_output" start 24; then
	routing_status=0
else
	routing_status=$?
fi
routing_text="$(cat "$routing_output")"
routed_file="$repo_routing/reports/start-output.txt"
assert_equals "$routing_status" "0"
assert_equals "$routing_text" "Output written to $routed_file"
routed_text="$(cat "$routed_file")"
assert_contains "$routed_text" 'Started issue 24'
assert_contains "$routed_text" 'mainBranch: main'
assert_contains "$routed_text" 'scratchBranch: scratch'
assert_contains "$routed_text" 'checkpoint: 0'
assert_file_exists "$repo_routing/.ai-dev/workflow.json"
git -C "$repo_routing" check-ignore -q .ai-dev/workflow.json
routing_status_after_start="$(repo_status_porcelain "$repo_routing")"
assert_equals "$routing_status_after_start" '?? reports/start-output.txt'

printf 'flow start tests passed\n'
