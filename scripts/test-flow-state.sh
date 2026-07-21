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

assert_file_ends_with_newline() {
	local file_path="$1"

	python3 - "$file_path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.read_bytes().endswith(b'\n'):
    raise SystemExit(f'{path} does not end with a newline')
PY
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
	)
}

run_flow() {
	local cwd="$1"
	shift
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" "$@"
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

repo_a="$TMP_DIR/repo-a"
repo_b="$TMP_DIR/repo-b"
init_repo "$repo_a"
init_repo "$repo_b"

# defaults when missing and read-only get does not create .ai-dev
get_missing_output="$TMP_DIR/get-missing-output"
if run_flow_capture "$repo_a/subdir" "$get_missing_output" __test-state-get; then
	get_missing_status=0
else
	get_missing_status=$?
fi
get_missing_text="$(cat "$get_missing_output")"
assert_equals "$get_missing_status" "0"
assert_equals "$get_missing_text" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'
assert_not_exists "$repo_a/.ai-dev"

# save and load with trimming normalization
set_full_output="$TMP_DIR/set-full-output"
set_full_payload='{"activeIssueNumber":2,"activeIssueTitle":"  Issue title  ","mainBranch":"  main-dev  ","scratchBranch":"  scratch-dev  ","checkpoint":3}'
if run_flow_capture "$repo_a/subdir" "$set_full_output" __test-state-set "$set_full_payload"; then
	set_full_status=0
else
	set_full_status=$?
fi
set_full_text="$(cat "$set_full_output")"
assert_equals "$set_full_status" "0"
assert_equals "$set_full_text" $'{
  "mainBranch": "main-dev",
  "scratchBranch": "scratch-dev",
  "checkpoint": 3,
  "activeIssueNumber": 2,
  "activeIssueTitle": "Issue title"
}'

workflow_file_a="$repo_a/.ai-dev/workflow.json"
assert_equals "$(cat "$workflow_file_a")" $'{
  "mainBranch": "main-dev",
  "scratchBranch": "scratch-dev",
  "checkpoint": 3,
  "activeIssueNumber": 2,
  "activeIssueTitle": "Issue title"
}'
assert_file_ends_with_newline "$workflow_file_a"

load_saved_output="$TMP_DIR/load-saved-output"
if run_flow_capture "$repo_a/subdir" "$load_saved_output" __test-state-get; then
	load_saved_status=0
else
	load_saved_status=$?
fi
load_saved_text="$(cat "$load_saved_output")"
assert_equals "$load_saved_status" "0"
assert_equals "$load_saved_text" "$set_full_text"

# replacing prior state
replace_output="$TMP_DIR/replace-output"
replace_payload='{"mainBranch":" release ","scratchBranch":" scratch-2 ","checkpoint":1}'
if run_flow_capture "$repo_a/subdir" "$replace_output" __test-state-set "$replace_payload"; then
	replace_status=0
else
	replace_status=$?
fi
replace_text="$(cat "$replace_output")"
assert_equals "$replace_status" "0"
assert_equals "$replace_text" $'{
  "mainBranch": "release",
  "scratchBranch": "scratch-2",
  "checkpoint": 1
}'
assert_equals "$(cat "$workflow_file_a")" "$replace_text"

# clear restores defaults and preserves config.json
mkdir -p "$repo_a/.ai-dev"
printf '{\n  "out": "logs/out.txt"\n}\n' > "$repo_a/.ai-dev/config.json"
clear_output="$TMP_DIR/clear-output"
if run_flow_capture "$repo_a/subdir" "$clear_output" __test-state-clear; then
	clear_status=0
else
	clear_status=$?
fi
clear_text="$(cat "$clear_output")"
assert_equals "$clear_status" "0"
assert_equals "$clear_text" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'
assert_not_exists "$workflow_file_a"
assert_equals "$(cat "$repo_a/.ai-dev/config.json")" $'{
  "out": "logs/out.txt"
}'

# repository isolation
set_repo_a_output="$TMP_DIR/set-repo-a-output"
set_repo_a_payload='{"activeIssueNumber":7,"activeIssueTitle":"Title","mainBranch":"main","scratchBranch":"scratch","checkpoint":2}'
if run_flow_capture "$repo_a/subdir" "$set_repo_a_output" __test-state-set "$set_repo_a_payload"; then
	set_repo_a_status=0
else
	set_repo_a_status=$?
fi
assert_equals "$set_repo_a_status" "0"

get_repo_b_output="$TMP_DIR/get-repo-b-output"
if run_flow_capture "$repo_b/subdir" "$get_repo_b_output" __test-state-get; then
	get_repo_b_status=0
else
	get_repo_b_status=$?
fi
get_repo_b_text="$(cat "$get_repo_b_output")"
assert_equals "$get_repo_b_status" "0"
assert_equals "$get_repo_b_text" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'
assert_not_exists "$repo_b/.ai-dev"

# malformed JSON
mkdir -p "$repo_a/.ai-dev"
printf '{ invalid json\n' > "$repo_a/.ai-dev/workflow.json"
malformed_output="$TMP_DIR/malformed-output"
if run_flow_capture "$repo_a/subdir" "$malformed_output" __test-state-get; then
	malformed_status=0
else
	malformed_status=$?
fi
malformed_text="$(cat "$malformed_output")"
assert_equals "$malformed_status" "1"
assert_contains "$malformed_text" 'Invalid JSON in'

# non-object JSON
printf '[]\n' > "$repo_a/.ai-dev/workflow.json"
non_object_output="$TMP_DIR/non-object-output"
if run_flow_capture "$repo_a/subdir" "$non_object_output" __test-state-get; then
	non_object_status=0
else
	non_object_status=$?
fi
non_object_text="$(cat "$non_object_output")"
assert_equals "$non_object_status" "1"
assert_contains "$non_object_text" 'expected a JSON object'

# unknown keys
printf '{\n  "mainBranch": "main",\n  "scratchBranch": "scratch",\n  "checkpoint": 0,\n  "other": true\n}\n' > "$repo_a/.ai-dev/workflow.json"
unknown_key_output="$TMP_DIR/unknown-key-output"
if run_flow_capture "$repo_a/subdir" "$unknown_key_output" __test-state-get; then
	unknown_key_status=0
else
	unknown_key_status=$?
fi
unknown_key_text="$(cat "$unknown_key_output")"
assert_equals "$unknown_key_status" "1"
assert_contains "$unknown_key_text" 'Unknown workflow state key(s)'

# invalid issue number
invalid_issue_output="$TMP_DIR/invalid-issue-output"
invalid_issue_payload='{"activeIssueNumber":0,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}'
if run_flow_capture "$repo_a/subdir" "$invalid_issue_output" __test-state-set "$invalid_issue_payload"; then
	invalid_issue_status=0
else
	invalid_issue_status=$?
fi
invalid_issue_text="$(cat "$invalid_issue_output")"
assert_equals "$invalid_issue_status" "1"
assert_contains "$invalid_issue_text" 'activeIssueNumber must be a positive integer'

# empty issue title
empty_title_output="$TMP_DIR/empty-title-output"
empty_title_payload='{"activeIssueNumber":2,"activeIssueTitle":"   ","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}'
if run_flow_capture "$repo_a/subdir" "$empty_title_output" __test-state-set "$empty_title_payload"; then
	empty_title_status=0
else
	empty_title_status=$?
fi
empty_title_text="$(cat "$empty_title_output")"
assert_equals "$empty_title_status" "1"
assert_contains "$empty_title_text" 'activeIssueTitle cannot be empty'

# title without issue number
orphan_title_output="$TMP_DIR/orphan-title-output"
orphan_title_payload='{"activeIssueTitle":"Title","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}'
if run_flow_capture "$repo_a/subdir" "$orphan_title_output" __test-state-set "$orphan_title_payload"; then
	orphan_title_status=0
else
	orphan_title_status=$?
fi
orphan_title_text="$(cat "$orphan_title_output")"
assert_equals "$orphan_title_status" "1"
assert_contains "$orphan_title_text" 'activeIssueTitle requires activeIssueNumber'

# empty branch names
empty_main_branch_output="$TMP_DIR/empty-main-branch-output"
empty_main_branch_payload='{"mainBranch":"   ","scratchBranch":"scratch","checkpoint":0}'
if run_flow_capture "$repo_a/subdir" "$empty_main_branch_output" __test-state-set "$empty_main_branch_payload"; then
	empty_main_branch_status=0
else
	empty_main_branch_status=$?
fi
empty_main_branch_text="$(cat "$empty_main_branch_output")"
assert_equals "$empty_main_branch_status" "1"
assert_contains "$empty_main_branch_text" 'mainBranch cannot be empty'

empty_scratch_branch_output="$TMP_DIR/empty-scratch-branch-output"
empty_scratch_branch_payload='{"mainBranch":"main","scratchBranch":"   ","checkpoint":0}'
if run_flow_capture "$repo_a/subdir" "$empty_scratch_branch_output" __test-state-set "$empty_scratch_branch_payload"; then
	empty_scratch_branch_status=0
else
	empty_scratch_branch_status=$?
fi
empty_scratch_branch_text="$(cat "$empty_scratch_branch_output")"
assert_equals "$empty_scratch_branch_status" "1"
assert_contains "$empty_scratch_branch_text" 'scratchBranch cannot be empty'

# invalid checkpoint
invalid_checkpoint_output="$TMP_DIR/invalid-checkpoint-output"
invalid_checkpoint_payload='{"mainBranch":"main","scratchBranch":"scratch","checkpoint":-1}'
if run_flow_capture "$repo_a/subdir" "$invalid_checkpoint_output" __test-state-set "$invalid_checkpoint_payload"; then
	invalid_checkpoint_status=0
else
	invalid_checkpoint_status=$?
fi
invalid_checkpoint_text="$(cat "$invalid_checkpoint_output")"
assert_equals "$invalid_checkpoint_status" "1"
assert_contains "$invalid_checkpoint_text" 'checkpoint must be a non-negative integer'

# commands run from a subdirectory
subdir_repo="$TMP_DIR/repo-subdir"
init_repo "$subdir_repo"
subdir_set_output="$TMP_DIR/subdir-set-output"
subdir_payload='{"mainBranch":" main ","scratchBranch":" scratch ","checkpoint":4}'
if run_flow_capture "$subdir_repo/subdir" "$subdir_set_output" __test-state-set "$subdir_payload"; then
	subdir_set_status=0
else
	subdir_set_status=$?
fi
assert_equals "$subdir_set_status" "0"
subdir_get_output="$TMP_DIR/subdir-get-output"
if run_flow_capture "$subdir_repo/subdir" "$subdir_get_output" __test-state-get; then
	subdir_get_status=0
else
	subdir_get_status=$?
fi
subdir_get_text="$(cat "$subdir_get_output")"
assert_equals "$subdir_get_status" "0"
assert_equals "$subdir_get_text" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 4
}'

printf 'flow state tests passed\n'
