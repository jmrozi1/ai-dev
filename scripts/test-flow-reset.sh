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

assert_exists() {
	local path="$1"

	if [[ ! -e "$path" && ! -L "$path" ]]; then
		printf 'expected path to exist: %s\n' "$path" >&2
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

write_config_file() {
	local repo_root="$1"
	local out_value="$2"
	mkdir -p "$repo_root/.ai-dev"
	cat >"$repo_root/.ai-dev/config.json" <<EOF
{
  "out": "$out_value"
}
EOF
}

track_config_file() {
	local repo_root="$1"
	local out_value="$2"
	write_config_file "$repo_root" "$out_value"
	git -C "$repo_root" add .ai-dev/config.json
	git -C "$repo_root" commit -q -m 'track config'
}

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse --abbrev-ref HEAD
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

create_commit_on_current_branch() {
	local repo_root="$1"
	local file_name="$2"
	local content="$3"
	local message="$4"
	printf '%s\n' "$content" > "$repo_root/$file_name"
	git -C "$repo_root" add "$file_name"
	git -C "$repo_root" commit -q -m "$message"
}

repo_args="$TMP_DIR/repo-args"
init_repo "$repo_args"
extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_args/subdir" "$extra_output" reset extra; then
	extra_status=0
else
	extra_status=$?
fi
extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" '1'
assert_contains "$extra_text" 'Usage: flow reset'

empty_extra_output="$TMP_DIR/empty-extra-output"
if run_flow_capture "$repo_args/subdir" "$empty_extra_output" reset ''; then
	empty_extra_status=0
else
	empty_extra_status=$?
fi
empty_extra_text="$(cat "$empty_extra_output")"
assert_equals "$empty_extra_status" '1'
assert_contains "$empty_extra_text" 'Usage: flow reset'

outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" reset; then
	outside_status=0
else
	outside_status=$?
fi
outside_text="$(cat "$outside_output")"
assert_equals "$outside_status" '1'
assert_contains "$outside_text" 'Not inside a Git repository'

repo_inactive="$TMP_DIR/repo-inactive"
init_repo "$repo_inactive"
git -C "$repo_inactive" checkout -q -b scratch
printf 'change\n' >> "$repo_inactive/tracked.txt"
git -C "$repo_inactive" add tracked.txt
inactive_head_before="$(current_head "$repo_inactive")"
inactive_state_before="$(state_get "$repo_inactive/subdir")"
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" reset; then
	inactive_status=0
else
	inactive_status=$?
fi
inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" '1'
assert_contains "$inactive_text" 'no active issue is set'
assert_equals "$(current_head "$repo_inactive")" "$inactive_head_before"
assert_equals "$(state_get "$repo_inactive/subdir")" "$inactive_state_before"

repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" branch scratch
state_set "$repo_wrong_branch/subdir" '{"activeIssueNumber":2,"mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
wrong_branch_head_before="$(current_head "$repo_wrong_branch")"
wrong_branch_output="$TMP_DIR/wrong-branch-output"
if run_flow_capture "$repo_wrong_branch/subdir" "$wrong_branch_output" reset; then
	wrong_branch_status=0
else
	wrong_branch_status=$?
fi
wrong_branch_text="$(cat "$wrong_branch_output")"
assert_equals "$wrong_branch_status" '1'
assert_contains "$wrong_branch_text" 'current branch main does not match scratchBranch scratch'
assert_equals "$(current_head "$repo_wrong_branch")" "$wrong_branch_head_before"

repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
git -C "$repo_missing_main" checkout -q -b scratch
state_set "$repo_missing_main/subdir" '{"activeIssueNumber":3,"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":1}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" reset; then
	missing_main_status=0
else
	missing_main_status=$?
fi
missing_main_text="$(cat "$missing_main_output")"
assert_equals "$missing_main_status" '1'
assert_contains "$missing_main_text" 'Main branch does not exist locally: trunk'

repo_missing_scratch="$TMP_DIR/repo-missing-scratch"
init_repo "$repo_missing_scratch"
state_set "$repo_missing_scratch/subdir" '{"activeIssueNumber":4,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
missing_scratch_output="$TMP_DIR/missing-scratch-output"
if run_flow_capture "$repo_missing_scratch/subdir" "$missing_scratch_output" reset; then
	missing_scratch_status=0
else
	missing_scratch_status=$?
fi
missing_scratch_text="$(cat "$missing_scratch_output")"
assert_equals "$missing_scratch_status" '1'
assert_contains "$missing_scratch_text" 'Scratch branch does not exist locally: scratch'

repo_same_branch="$TMP_DIR/repo-same-branch"
init_repo "$repo_same_branch"
state_set "$repo_same_branch/subdir" '{"activeIssueNumber":5,"mainBranch":"main","scratchBranch":"main","checkpoint":2}' >/dev/null
same_branch_output="$TMP_DIR/same-branch-output"
if run_flow_capture "$repo_same_branch/subdir" "$same_branch_output" reset; then
	same_branch_status=0
else
	same_branch_status=$?
fi
same_branch_text="$(cat "$same_branch_output")"
assert_equals "$same_branch_status" '1'
assert_contains "$same_branch_text" 'mainBranch and scratchBranch must be different'

repo_ahead="$TMP_DIR/repo-ahead"
init_repo "$repo_ahead"
git -C "$repo_ahead" checkout -q -b scratch
state_set "$repo_ahead/subdir" '{"activeIssueNumber":6,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_ahead" ahead.txt 'scratch ahead' 'scratch ahead'
ahead_main_head="$(git -C "$repo_ahead" rev-parse main)"
ahead_output="$TMP_DIR/ahead-output"
if run_flow_capture "$repo_ahead/subdir" "$ahead_output" reset; then
	ahead_status=0
else
	ahead_status=$?
fi
ahead_text="$(cat "$ahead_output")"
assert_equals "$ahead_status" '0'
assert_equals "$(current_head "$repo_ahead")" "$ahead_main_head"
assert_equals "$ahead_text" $'Reset scratch to main\ncheckpoint: 0\nactiveIssueNumber: 6'
assert_repo_clean "$repo_ahead"
assert_equals "$(current_branch "$repo_ahead")" 'scratch'

repo_behind="$TMP_DIR/repo-behind"
init_repo "$repo_behind"
git -C "$repo_behind" checkout -q -b scratch
state_set "$repo_behind/subdir" '{"activeIssueNumber":7,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
git -C "$repo_behind" checkout -q main
create_commit_on_current_branch "$repo_behind" main-ahead.txt 'main ahead' 'main ahead'
behind_main_head="$(git -C "$repo_behind" rev-parse main)"
git -C "$repo_behind" checkout -q scratch
behind_output="$TMP_DIR/behind-output"
if run_flow_capture "$repo_behind/subdir" "$behind_output" reset; then
	behind_status=0
else
	behind_status=$?
fi
behind_text="$(cat "$behind_output")"
assert_equals "$behind_status" '0'
assert_contains "$behind_text" 'Reset scratch to main'
assert_equals "$(current_head "$repo_behind")" "$behind_main_head"
assert_repo_clean "$repo_behind"

repo_diverged="$TMP_DIR/repo-diverged"
init_repo "$repo_diverged"
git -C "$repo_diverged" checkout -q -b scratch
state_set "$repo_diverged/subdir" '{"activeIssueNumber":8,"mainBranch":"main","scratchBranch":"scratch","checkpoint":3}' >/dev/null
create_commit_on_current_branch "$repo_diverged" scratch-only.txt 'scratch only' 'scratch only'
git -C "$repo_diverged" checkout -q main
create_commit_on_current_branch "$repo_diverged" main-only.txt 'main only' 'main only'
diverged_main_head="$(git -C "$repo_diverged" rev-parse main)"
git -C "$repo_diverged" checkout -q scratch
diverged_output="$TMP_DIR/diverged-output"
if run_flow_capture "$repo_diverged/subdir" "$diverged_output" reset; then
	diverged_status=0
else
	diverged_status=$?
fi
diverged_text="$(cat "$diverged_output")"
assert_equals "$diverged_status" '0'
assert_contains "$diverged_text" 'Reset scratch to main'
assert_equals "$(current_head "$repo_diverged")" "$diverged_main_head"
assert_repo_clean "$repo_diverged"

repo_cleanup="$TMP_DIR/repo-cleanup"
init_repo "$repo_cleanup"
git -C "$repo_cleanup" checkout -q -b scratch
state_set "$repo_cleanup/subdir" '{"activeIssueNumber":21,"activeIssueTitle":"Reset title","mainBranch":"main","scratchBranch":"scratch","checkpoint":5}' >/dev/null
printf 'staged\n' >> "$repo_cleanup/tracked.txt"
git -C "$repo_cleanup" add tracked.txt
printf 'unstaged\n' >> "$repo_cleanup/tracked.txt"
mkdir -p "$repo_cleanup/tmp-dir/nested"
printf 'junk\n' > "$repo_cleanup/tmp-dir/nested/file.txt"
printf 'junk\n' > "$repo_cleanup/untracked.txt"
printf 'local-cache/\nlocal-note.txt\n' >> "$repo_cleanup/.git/info/exclude"
mkdir -p "$repo_cleanup/local-cache"
printf 'keep\n' > "$repo_cleanup/local-cache/cache.txt"
printf 'keep\n' > "$repo_cleanup/local-note.txt"
cleanup_main_head="$(git -C "$repo_cleanup" rev-parse main)"
cleanup_output="$TMP_DIR/cleanup-output"
if run_flow_capture "$repo_cleanup/subdir" "$cleanup_output" reset; then
	cleanup_status=0
else
	cleanup_status=$?
fi
cleanup_text="$(cat "$cleanup_output")"
assert_equals "$cleanup_status" '0'
assert_equals "$cleanup_text" $'Reset scratch to main\ncheckpoint: 0\nactiveIssueNumber: 21'
assert_equals "$(current_head "$repo_cleanup")" "$cleanup_main_head"
assert_equals "$(current_branch "$repo_cleanup")" 'scratch'
assert_repo_clean "$repo_cleanup"
assert_not_exists "$repo_cleanup/untracked.txt"
assert_not_exists "$repo_cleanup/tmp-dir"
assert_exists "$repo_cleanup/.ai-dev/workflow.json"
assert_exists "$repo_cleanup/local-cache/cache.txt"
assert_exists "$repo_cleanup/local-note.txt"
assert_not_contains "$(cat "$repo_cleanup/.ai-dev/workflow.json")" '"checkpoint": 5'
assert_equals "$(state_get "$repo_cleanup")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "activeIssueNumber": 21,
  "activeIssueTitle": "Reset title"
}'

repo_custom="$TMP_DIR/repo-custom"
init_repo "$repo_custom"
git -C "$repo_custom" branch -m main trunk
git -C "$repo_custom" checkout -q -b sandbox
state_set "$repo_custom/subdir" '{"activeIssueNumber":22,"activeIssueTitle":"Custom branches","mainBranch":"trunk","scratchBranch":"sandbox","checkpoint":7}' >/dev/null
printf 'custom\n' >> "$repo_custom/tracked.txt"
git -C "$repo_custom" add tracked.txt
custom_output="$TMP_DIR/custom-output"
if run_flow_capture "$repo_custom/subdir" "$custom_output" reset; then
	custom_status=0
else
	custom_status=$?
fi
custom_text="$(cat "$custom_output")"
assert_equals "$custom_status" '0'
assert_equals "$custom_text" $'Reset sandbox to trunk\ncheckpoint: 0\nactiveIssueNumber: 22'
assert_equals "$(state_get "$repo_custom")" $'{
  "mainBranch": "trunk",
  "scratchBranch": "sandbox",
  "checkpoint": 0,
  "activeIssueNumber": 22,
  "activeIssueTitle": "Custom branches"
}'
assert_repo_clean "$repo_custom"

repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
git -C "$repo_subdir" checkout -q -b scratch
state_set "$repo_subdir/subdir" '{"activeIssueNumber":23,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'subdir\n' >> "$repo_subdir/tracked.txt"
git -C "$repo_subdir" add tracked.txt
subdir_output="$TMP_DIR/subdir-output"
if run_flow_capture "$repo_subdir/subdir" "$subdir_output" reset; then
	subdir_status=0
else
	subdir_status=$?
fi
subdir_text="$(cat "$subdir_output")"
assert_equals "$subdir_status" '0'
assert_contains "$subdir_text" 'Reset scratch to main'
assert_repo_clean "$repo_subdir"

repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
git -C "$repo_routing" checkout -q -b scratch
routing_output_path="$TMP_DIR/reset-output.txt"
track_config_file "$repo_routing" "$routing_output_path"
state_set "$repo_routing/subdir" '{"activeIssueNumber":24,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'routed\n' >> "$repo_routing/tracked.txt"
git -C "$repo_routing" add tracked.txt
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" reset; then
	routing_status=0
else
	routing_status=$?
fi
routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
assert_equals "$routing_terminal_text" "Output written to $routing_output_path"
routing_file_text="$(cat "$routing_output_path")"
assert_equals "$routing_file_text" $'Reset scratch to main\ncheckpoint: 0\nactiveIssueNumber: 24'
assert_repo_clean "$repo_routing"

repo_reset_fail="$TMP_DIR/repo-reset-fail"
init_repo "$repo_reset_fail"
git -C "$repo_reset_fail" checkout -q -b scratch
state_set "$repo_reset_fail/subdir" '{"activeIssueNumber":25,"mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
printf 'blocked\n' >> "$repo_reset_fail/tracked.txt"
git -C "$repo_reset_fail" add tracked.txt
touch "$repo_reset_fail/.git/index.lock"
reset_fail_head_before="$(current_head "$repo_reset_fail")"
reset_fail_state_before="$(state_get "$repo_reset_fail/subdir")"
reset_fail_output="$TMP_DIR/reset-fail-output"
if run_flow_capture "$repo_reset_fail/subdir" "$reset_fail_output" reset; then
	reset_fail_status=0
else
	reset_fail_status=$?
fi
rm -f "$repo_reset_fail/.git/index.lock"
reset_fail_text="$(cat "$reset_fail_output")"
assert_equals "$reset_fail_status" '1'
assert_contains "$reset_fail_text" 'Git reset failed'
assert_equals "$(current_head "$repo_reset_fail")" "$reset_fail_head_before"
assert_equals "$(state_get "$repo_reset_fail/subdir")" "$reset_fail_state_before"

if [[ "$(id -u)" != '0' ]]; then
	repo_state_fail="$TMP_DIR/repo-state-fail"
	init_repo "$repo_state_fail"
	git -C "$repo_state_fail" checkout -q -b scratch
	state_set "$repo_state_fail/subdir" '{"activeIssueNumber":26,"activeIssueTitle":"Persist title","mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
	printf 'staged\n' >> "$repo_state_fail/tracked.txt"
	git -C "$repo_state_fail" add tracked.txt
	printf 'untracked\n' > "$repo_state_fail/remove.txt"
	state_fail_main_head="$(git -C "$repo_state_fail" rev-parse main)"
	state_fail_state_before="$(cat "$repo_state_fail/.ai-dev/workflow.json")"
	chmod 500 "$repo_state_fail/.ai-dev"
	state_fail_output="$TMP_DIR/state-fail-output"
	if run_flow_capture "$repo_state_fail/subdir" "$state_fail_output" reset; then
		state_fail_status=0
	else
		state_fail_status=$?
	fi
	chmod 700 "$repo_state_fail/.ai-dev"
	state_fail_text="$(cat "$state_fail_output")"
	assert_equals "$state_fail_status" '1'
	assert_contains "$state_fail_text" 'Cannot write workflow state to'
	assert_not_contains "$state_fail_text" 'Traceback'
	assert_equals "$(current_head "$repo_state_fail")" "$state_fail_main_head"
	assert_equals "$(current_branch "$repo_state_fail")" 'scratch'
	assert_not_exists "$repo_state_fail/remove.txt"
	assert_equals "$(cat "$repo_state_fail/.ai-dev/workflow.json")" "$state_fail_state_before"
	assert_equals "$(state_get "$repo_state_fail")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 2,
  "activeIssueNumber": 26,
  "activeIssueTitle": "Persist title"
}'
	assert_repo_clean "$repo_state_fail"
fi

printf 'flow reset tests passed\n'
