#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW="$ROOT/scripts/flow"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

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

cached_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --cached --binary --no-ext-diff
}

cached_diff_names() {
	local repo_root="$1"
	git -C "$repo_root" diff --cached --name-only --no-ext-diff
}

assert_all_changes_staged() {
	local repo_root="$1"
	local status_text
	status_text="$(repo_status_porcelain "$repo_root")"
	local line
	while IFS= read -r line; do
		if [[ -z "$line" ]]; then
			continue
		fi
		if [[ "${line:0:2}" == '??' ]]; then
			fail "expected no untracked entries after review: $line"
		fi
		if [[ "${line:1:1}" != ' ' ]]; then
			fail "expected no unstaged entries after review: $line"
		fi
	done <<< "$status_text"
}

repo_args="$TMP_DIR/repo-args"
init_repo "$repo_args"
extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_args/subdir" "$extra_output" review extra; then
	extra_status=0
else
	extra_status=$?
fi
extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" '1'
assert_contains "$extra_text" 'Usage: flow review'

empty_extra_output="$TMP_DIR/empty-extra-output"
if run_flow_capture "$repo_args/subdir" "$empty_extra_output" review ''; then
	empty_extra_status=0
else
	empty_extra_status=$?
fi
empty_extra_text="$(cat "$empty_extra_output")"
assert_equals "$empty_extra_status" '1'
assert_contains "$empty_extra_text" 'Usage: flow review'

outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" review; then
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
inactive_status_before="$(repo_status_porcelain "$repo_inactive")"
inactive_index_before="$(cached_diff "$repo_inactive")"
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" review; then
	inactive_status=0
else
	inactive_status=$?
fi
inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" '1'
assert_contains "$inactive_text" 'no active issue is set'
assert_equals "$(repo_status_porcelain "$repo_inactive")" "$inactive_status_before"
assert_equals "$(cached_diff "$repo_inactive")" "$inactive_index_before"

repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" branch scratch
state_set "$repo_wrong_branch/subdir" '{"activeIssueNumber":2,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
wrong_branch_status_before="$(repo_status_porcelain "$repo_wrong_branch")"
wrong_branch_output="$TMP_DIR/wrong-branch-output"
if run_flow_capture "$repo_wrong_branch/subdir" "$wrong_branch_output" review; then
	wrong_branch_status=0
else
	wrong_branch_status=$?
fi
wrong_branch_text="$(cat "$wrong_branch_output")"
assert_equals "$wrong_branch_status" '1'
assert_contains "$wrong_branch_text" 'current branch main does not match scratchBranch scratch'
assert_equals "$(repo_status_porcelain "$repo_wrong_branch")" "$wrong_branch_status_before"

repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
git -C "$repo_missing_main" checkout -q -b scratch
state_set "$repo_missing_main/subdir" '{"activeIssueNumber":3,"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" review; then
	missing_main_status=0
else
	missing_main_status=$?
fi
missing_main_text="$(cat "$missing_main_output")"
assert_equals "$missing_main_status" '1'
assert_contains "$missing_main_text" 'Main branch does not exist locally: trunk'

repo_missing_scratch="$TMP_DIR/repo-missing-scratch"
init_repo "$repo_missing_scratch"
state_set "$repo_missing_scratch/subdir" '{"activeIssueNumber":4,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_scratch_output="$TMP_DIR/missing-scratch-output"
if run_flow_capture "$repo_missing_scratch/subdir" "$missing_scratch_output" review; then
	missing_scratch_status=0
else
	missing_scratch_status=$?
fi
missing_scratch_text="$(cat "$missing_scratch_output")"
assert_equals "$missing_scratch_status" '1'
assert_contains "$missing_scratch_text" 'Scratch branch does not exist locally: scratch'

repo_no_changes="$TMP_DIR/repo-no-changes"
init_repo "$repo_no_changes"
git -C "$repo_no_changes" checkout -q -b scratch
no_changes_output_path="$TMP_DIR/no-changes-review.diff"
printf 'old review output\n' > "$no_changes_output_path"
track_config_file "$repo_no_changes" "$no_changes_output_path"
state_set "$repo_no_changes/subdir" '{"activeIssueNumber":5,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
no_changes_output="$TMP_DIR/no-changes-output"
if run_flow_capture "$repo_no_changes/subdir" "$no_changes_output" review; then
	no_changes_status=0
else
	no_changes_status=$?
fi
no_changes_text="$(cat "$no_changes_output")"
assert_equals "$no_changes_status" '1'
assert_contains "$no_changes_text" 'No proposed changes to review'
assert_equals "$(cat "$no_changes_output_path")" 'old review output'

repo_staged="$TMP_DIR/repo-staged"
init_repo "$repo_staged"
git -C "$repo_staged" checkout -q -b scratch
state_set "$repo_staged/subdir" '{"activeIssueNumber":10,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'staged\n' >> "$repo_staged/tracked.txt"
git -C "$repo_staged" add tracked.txt
staged_output="$TMP_DIR/staged-output"
if run_flow_capture "$repo_staged/subdir" "$staged_output" review; then
	staged_status=0
else
	staged_status=$?
fi
staged_text="$(cat "$staged_output")"
assert_equals "$staged_status" '0'
assert_equals "$staged_text" "$(cached_diff "$repo_staged")"
assert_all_changes_staged "$repo_staged"

repo_unstaged="$TMP_DIR/repo-unstaged"
init_repo "$repo_unstaged"
git -C "$repo_unstaged" checkout -q -b scratch
state_set "$repo_unstaged/subdir" '{"activeIssueNumber":11,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'unstaged\n' >> "$repo_unstaged/tracked.txt"
unstaged_output="$TMP_DIR/unstaged-output"
if run_flow_capture "$repo_unstaged/subdir" "$unstaged_output" review; then
	unstaged_status=0
else
	unstaged_status=$?
fi
unstaged_text="$(cat "$unstaged_output")"
assert_equals "$unstaged_status" '0'
assert_equals "$unstaged_text" "$(cached_diff "$repo_unstaged")"
assert_all_changes_staged "$repo_unstaged"

repo_untracked="$TMP_DIR/repo-untracked"
init_repo "$repo_untracked"
git -C "$repo_untracked" checkout -q -b scratch
state_set "$repo_untracked/subdir" '{"activeIssueNumber":12,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'hello\n' > "$repo_untracked/new-file.txt"
untracked_output="$TMP_DIR/untracked-output"
if run_flow_capture "$repo_untracked/subdir" "$untracked_output" review; then
	untracked_status=0
else
	untracked_status=$?
fi
untracked_text="$(cat "$untracked_output")"
assert_equals "$untracked_status" '0'
assert_equals "$untracked_text" "$(cached_diff "$repo_untracked")"
assert_contains "$untracked_text" 'diff --git a/new-file.txt b/new-file.txt'
assert_all_changes_staged "$repo_untracked"
git -C "$repo_untracked" check-ignore -q .ai-dev/workflow.json
assert_not_contains "$(cached_diff_names "$repo_untracked")" '.ai-dev/workflow.json'

repo_deleted="$TMP_DIR/repo-deleted"
init_repo "$repo_deleted"
git -C "$repo_deleted" checkout -q -b scratch
state_set "$repo_deleted/subdir" '{"activeIssueNumber":13,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
rm "$repo_deleted/tracked.txt"
deleted_output="$TMP_DIR/deleted-output"
if run_flow_capture "$repo_deleted/subdir" "$deleted_output" review; then
	deleted_status=0
else
	deleted_status=$?
fi
deleted_text="$(cat "$deleted_output")"
assert_equals "$deleted_status" '0'
assert_equals "$deleted_text" "$(cached_diff "$repo_deleted")"
assert_contains "$deleted_text" 'deleted file mode'
assert_all_changes_staged "$repo_deleted"

repo_renamed="$TMP_DIR/repo-renamed"
init_repo "$repo_renamed"
git -C "$repo_renamed" checkout -q -b scratch
state_set "$repo_renamed/subdir" '{"activeIssueNumber":14,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
git -C "$repo_renamed" mv tracked.txt renamed.txt
renamed_output="$TMP_DIR/renamed-output"
if run_flow_capture "$repo_renamed/subdir" "$renamed_output" review; then
	renamed_status=0
else
	renamed_status=$?
fi
renamed_text="$(cat "$renamed_output")"
assert_equals "$renamed_status" '0'
assert_equals "$renamed_text" "$(cached_diff "$repo_renamed")"
assert_contains "$renamed_text" 'rename from tracked.txt'
assert_contains "$renamed_text" 'rename to renamed.txt'
assert_all_changes_staged "$repo_renamed"

repo_combo="$TMP_DIR/repo-combo"
init_repo "$repo_combo"
git -C "$repo_combo" checkout -q -b scratch
state_set "$repo_combo/subdir" '{"activeIssueNumber":15,"activeIssueTitle":"Combo review","mainBranch":"main","scratchBranch":"scratch","checkpoint":3}' >/dev/null
printf 'staged\n' >> "$repo_combo/tracked.txt"
git -C "$repo_combo" add tracked.txt
printf 'unstaged\n' >> "$repo_combo/tracked.txt"
printf 'note\n' > "$repo_combo/notes.txt"
combo_branch_before="$(current_branch "$repo_combo")"
combo_head_before="$(current_head "$repo_combo")"
combo_workflow_before="$(cat "$repo_combo/.ai-dev/workflow.json")"
combo_tracked_before="$(cat "$repo_combo/tracked.txt")"
combo_untracked_before="$(cat "$repo_combo/notes.txt")"
combo_output="$TMP_DIR/combo-output"
if run_flow_capture "$repo_combo/subdir" "$combo_output" review; then
	combo_status=0
else
	combo_status=$?
fi
combo_text="$(cat "$combo_output")"
assert_equals "$combo_status" '0'
assert_equals "$combo_text" "$(cached_diff "$repo_combo")"
assert_all_changes_staged "$repo_combo"
assert_equals "$(current_branch "$repo_combo")" "$combo_branch_before"
assert_equals "$(current_head "$repo_combo")" "$combo_head_before"
assert_equals "$(cat "$repo_combo/.ai-dev/workflow.json")" "$combo_workflow_before"
assert_equals "$(cat "$repo_combo/tracked.txt")" "$combo_tracked_before"
assert_equals "$(cat "$repo_combo/notes.txt")" "$combo_untracked_before"

repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
git -C "$repo_routing" checkout -q -b scratch
routing_output_path="$TMP_DIR/review-output.diff"
track_config_file "$repo_routing" "$routing_output_path"
state_set "$repo_routing/subdir" '{"activeIssueNumber":16,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'routed\n' > "$repo_routing/routed.txt"
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" review; then
	routing_status=0
else
	routing_status=$?
fi
routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
assert_equals "$routing_terminal_text" "Output written to $routing_output_path"
assert_equals "$(cat "$routing_output_path")" "$(cached_diff "$repo_routing")"
assert_all_changes_staged "$repo_routing"

repo_write_fail="$TMP_DIR/repo-write-fail"
init_repo "$repo_write_fail"
git -C "$repo_write_fail" checkout -q -b scratch
write_fail_output_path="$TMP_DIR/missing-parent/review.diff"
track_config_file "$repo_write_fail" "$write_fail_output_path"
state_set "$repo_write_fail/subdir" '{"activeIssueNumber":17,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'failure\n' > "$repo_write_fail/failure.txt"
write_fail_terminal_output="$TMP_DIR/write-fail-terminal-output"
if run_flow_capture "$repo_write_fail/subdir" "$write_fail_terminal_output" review; then
	write_fail_status=0
else
	write_fail_status=$?
fi
write_fail_text="$(cat "$write_fail_terminal_output")"
assert_equals "$write_fail_status" '1'
assert_contains "$write_fail_text" 'Cannot write output to'
assert_contains "$write_fail_text" 'Generated output preserved at'
assert_not_exists "$write_fail_output_path"
assert_equals "$(cached_diff "$repo_write_fail")" "$(git -C "$repo_write_fail" diff --cached --binary --no-ext-diff)"
assert_all_changes_staged "$repo_write_fail"

printf 'flow review tests passed\n'
