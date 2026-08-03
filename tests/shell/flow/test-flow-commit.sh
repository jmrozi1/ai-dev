#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AI_DEV_REAL="${AI_DEV_BIN:-$(command -v ai-dev || true)}"
if [[ -z "$AI_DEV_REAL" ]]; then
	printf 'ai-dev command not found on PATH.\n' >&2
	exit 1
fi
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
		"$AI_DEV_REAL" flow "$@"
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
		FLOW_TEST_MODE=1 "$AI_DEV_REAL" __test-state-get
	)
}

state_set() {
	local cwd="$1"
	local payload="$2"
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$AI_DEV_REAL" __test-state-set "$payload"
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

current_message() {
	local repo_root="$1"
	git -C "$repo_root" log -1 --format=%B
}

current_parent() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse HEAD^
}

repo_status_porcelain() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
}

cached_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --cached --binary --no-ext-diff
}

head_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff HEAD --binary --no-ext-diff
}

assert_repo_clean() {
	local repo_root="$1"
	assert_equals "$(repo_status_porcelain "$repo_root")" ''
	assert_equals "$(cached_diff "$repo_root")" ''
	assert_equals "$(head_diff "$repo_root")" ''
}

repo_args="$TMP_DIR/repo-args"
init_repo "$repo_args"
extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_args/subdir" "$extra_output" commit extra; then
	extra_status=0
else
	extra_status=$?
fi
	extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" '1'
assert_contains "$extra_text" 'Usage: ai-dev flow commit'

empty_extra_output="$TMP_DIR/empty-extra-output"
if run_flow_capture "$repo_args/subdir" "$empty_extra_output" commit ''; then
	empty_extra_status=0
else
	empty_extra_status=$?
fi
	empty_extra_text="$(cat "$empty_extra_output")"
assert_equals "$empty_extra_status" '1'
assert_contains "$empty_extra_text" 'Usage: ai-dev flow commit'

outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" commit; then
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
printf 'untracked\n' > "$repo_inactive/new-file.txt"
inactive_head_before="$(current_head "$repo_inactive")"
inactive_state_before="$(state_get "$repo_inactive/subdir")"
inactive_status_before="$(repo_status_porcelain "$repo_inactive")"
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" commit; then
	inactive_status=0
else
	inactive_status=$?
fi
	inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" '1'
assert_contains "$inactive_text" 'no active issue is set'
assert_equals "$(current_head "$repo_inactive")" "$inactive_head_before"
assert_equals "$(state_get "$repo_inactive/subdir")" "$inactive_state_before"
assert_equals "$(repo_status_porcelain "$repo_inactive")" "$inactive_status_before"

repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" branch scratch
state_set "$repo_wrong_branch/subdir" '{"activeIssueNumber":2,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'change\n' >> "$repo_wrong_branch/tracked.txt"
printf 'untracked\n' > "$repo_wrong_branch/new-file.txt"
wrong_branch_head_before="$(current_head "$repo_wrong_branch")"
wrong_branch_status_before="$(repo_status_porcelain "$repo_wrong_branch")"
wrong_branch_output="$TMP_DIR/wrong-branch-output"
if run_flow_capture "$repo_wrong_branch/subdir" "$wrong_branch_output" commit; then
	wrong_branch_status=0
else
	wrong_branch_status=$?
fi
	wrong_branch_text="$(cat "$wrong_branch_output")"
assert_equals "$wrong_branch_status" '1'
assert_contains "$wrong_branch_text" 'current branch main does not match scratchBranch scratch'
assert_equals "$(current_head "$repo_wrong_branch")" "$wrong_branch_head_before"
assert_equals "$(repo_status_porcelain "$repo_wrong_branch")" "$wrong_branch_status_before"

repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
git -C "$repo_missing_main" checkout -q -b scratch
state_set "$repo_missing_main/subdir" '{"activeIssueNumber":3,"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'change\n' >> "$repo_missing_main/tracked.txt"
git -C "$repo_missing_main" add tracked.txt
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" commit; then
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
printf 'change\n' >> "$repo_missing_scratch/tracked.txt"
git -C "$repo_missing_scratch" add tracked.txt
missing_scratch_output="$TMP_DIR/missing-scratch-output"
if run_flow_capture "$repo_missing_scratch/subdir" "$missing_scratch_output" commit; then
	missing_scratch_status=0
else
	missing_scratch_status=$?
fi
	missing_scratch_text="$(cat "$missing_scratch_output")"
assert_equals "$missing_scratch_status" '1'
assert_contains "$missing_scratch_text" 'Scratch branch does not exist locally: scratch'

repo_no_staged="$TMP_DIR/repo-no-staged"
init_repo "$repo_no_staged"
git -C "$repo_no_staged" checkout -q -b scratch
state_set "$repo_no_staged/subdir" '{"activeIssueNumber":5,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
no_staged_head_before="$(current_head "$repo_no_staged")"
no_staged_state_before="$(state_get "$repo_no_staged/subdir")"
no_staged_output="$TMP_DIR/no-staged-output"
if run_flow_capture "$repo_no_staged/subdir" "$no_staged_output" commit; then
	no_staged_status=0
else
	no_staged_status=$?
fi
	no_staged_text="$(cat "$no_staged_output")"
assert_equals "$no_staged_status" '1'
assert_contains "$no_staged_text" 'no staged changes are available'
assert_equals "$(current_head "$repo_no_staged")" "$no_staged_head_before"
assert_equals "$(state_get "$repo_no_staged/subdir")" "$no_staged_state_before"

repo_modified_tracked="$TMP_DIR/repo-modified-tracked"
init_repo "$repo_modified_tracked"
git -C "$repo_modified_tracked" checkout -q -b scratch
state_set "$repo_modified_tracked/subdir" '{"activeIssueNumber":6,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'modified tracked\n' >> "$repo_modified_tracked/tracked.txt"
modified_tracked_head_before="$(current_head "$repo_modified_tracked")"
modified_tracked_working_before="$(git -C "$repo_modified_tracked" diff --binary --no-ext-diff)"
modified_tracked_output="$TMP_DIR/modified-tracked-output"
if run_flow_capture "$repo_modified_tracked/subdir" "$modified_tracked_output" commit; then
	modified_tracked_status=0
else
	modified_tracked_status=$?
fi
	modified_tracked_text="$(cat "$modified_tracked_output")"
assert_equals "$modified_tracked_status" '0'
modified_tracked_head_after="$(current_head "$repo_modified_tracked")"
assert_contains "$modified_tracked_text" 'Created checkpoint 1'
assert_contains "$modified_tracked_text" "commit: $modified_tracked_head_after"
assert_equals "$(current_message "$repo_modified_tracked")" '1'
assert_equals "$(current_parent "$repo_modified_tracked")" "$modified_tracked_head_before"
assert_equals "$(git -C "$repo_modified_tracked" diff "$modified_tracked_head_before" "$modified_tracked_head_after" --binary --no-ext-diff)" "$modified_tracked_working_before"
assert_repo_clean "$repo_modified_tracked"

repo_untracked_only="$TMP_DIR/repo-untracked-only"
init_repo "$repo_untracked_only"
git -C "$repo_untracked_only" checkout -q -b scratch
state_set "$repo_untracked_only/subdir" '{"activeIssueNumber":7,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'untracked\n' > "$repo_untracked_only/new-file.txt"
untracked_only_head_before="$(current_head "$repo_untracked_only")"
untracked_only_output="$TMP_DIR/untracked-only-output"
if run_flow_capture "$repo_untracked_only/subdir" "$untracked_only_output" commit; then
	untracked_only_status=0
else
	untracked_only_status=$?
fi
	untracked_only_text="$(cat "$untracked_only_output")"
assert_equals "$untracked_only_status" '0'
assert_contains "$untracked_only_text" 'Created checkpoint 1'
assert_equals "$(current_parent "$repo_untracked_only")" "$untracked_only_head_before"
assert_equals "$(current_message "$repo_untracked_only")" '1'
assert_equals "$(cat "$repo_untracked_only/new-file.txt")" 'untracked'
git -C "$repo_untracked_only" ls-files --error-unmatch new-file.txt >/dev/null
assert_repo_clean "$repo_untracked_only"

repo_deleted_tracked="$TMP_DIR/repo-deleted-tracked"
init_repo "$repo_deleted_tracked"
git -C "$repo_deleted_tracked" checkout -q -b scratch
state_set "$repo_deleted_tracked/subdir" '{"activeIssueNumber":8,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
rm "$repo_deleted_tracked/tracked.txt"
deleted_tracked_head_before="$(current_head "$repo_deleted_tracked")"
deleted_tracked_output="$TMP_DIR/deleted-tracked-output"
if run_flow_capture "$repo_deleted_tracked/subdir" "$deleted_tracked_output" commit; then
	deleted_tracked_status=0
else
	deleted_tracked_status=$?
fi
	deleted_tracked_text="$(cat "$deleted_tracked_output")"
assert_equals "$deleted_tracked_status" '0'
assert_contains "$deleted_tracked_text" 'Created checkpoint 1'
assert_equals "$(current_parent "$repo_deleted_tracked")" "$deleted_tracked_head_before"
assert_equals "$(current_message "$repo_deleted_tracked")" '1'
assert_not_exists "$repo_deleted_tracked/tracked.txt"
assert_contains "$(git -C "$repo_deleted_tracked" show --name-status --format= HEAD)" $'D\ttracked.txt'
assert_repo_clean "$repo_deleted_tracked"

repo_mixed_changes="$TMP_DIR/repo-mixed-changes"
init_repo "$repo_mixed_changes"
git -C "$repo_mixed_changes" checkout -q -b scratch
state_set "$repo_mixed_changes/subdir" '{"activeIssueNumber":9,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'staged\n' >> "$repo_mixed_changes/tracked.txt"
git -C "$repo_mixed_changes" add tracked.txt
printf 'unstaged\n' >> "$repo_mixed_changes/tracked.txt"
printf 'untracked\n' > "$repo_mixed_changes/new-file.txt"
mixed_changes_head_before="$(current_head "$repo_mixed_changes")"
mixed_changes_output="$TMP_DIR/mixed-changes-output"
if run_flow_capture "$repo_mixed_changes/subdir" "$mixed_changes_output" commit; then
	mixed_changes_status=0
else
	mixed_changes_status=$?
fi
	mixed_changes_text="$(cat "$mixed_changes_output")"
assert_equals "$mixed_changes_status" '0'
assert_contains "$mixed_changes_text" 'Created checkpoint 1'
assert_equals "$(current_parent "$repo_mixed_changes")" "$mixed_changes_head_before"
assert_equals "$(current_message "$repo_mixed_changes")" '1'
assert_contains "$(cat "$repo_mixed_changes/tracked.txt")" 'staged'
assert_contains "$(cat "$repo_mixed_changes/tracked.txt")" 'unstaged'
assert_equals "$(cat "$repo_mixed_changes/new-file.txt")" 'untracked'
git -C "$repo_mixed_changes" ls-files --error-unmatch new-file.txt >/dev/null
assert_repo_clean "$repo_mixed_changes"

repo_success_zero="$TMP_DIR/repo-success-zero"
init_repo "$repo_success_zero"
git -C "$repo_success_zero" checkout -q -b scratch
state_set "$repo_success_zero/subdir" '{"activeIssueNumber":21,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'checkpoint one\n' >> "$repo_success_zero/tracked.txt"
git -C "$repo_success_zero" add tracked.txt
success_zero_head_before="$(current_head "$repo_success_zero")"
success_zero_diff_before="$(cached_diff "$repo_success_zero")"
success_zero_output="$TMP_DIR/success-zero-output"
if run_flow_capture "$repo_success_zero/subdir" "$success_zero_output" commit; then
	success_zero_status=0
else
	success_zero_status=$?
fi
	success_zero_text="$(cat "$success_zero_output")"
assert_equals "$success_zero_status" '0'
success_zero_head_after="$(current_head "$repo_success_zero")"
assert_contains "$success_zero_text" 'Created checkpoint 1'
assert_contains "$success_zero_text" "commit: $success_zero_head_after"
assert_contains "$success_zero_text" 'activeIssueNumber: 21'
assert_equals "$(current_message "$repo_success_zero")" '1'
assert_equals "$(current_parent "$repo_success_zero")" "$success_zero_head_before"
assert_equals "$(git -C "$repo_success_zero" diff "$success_zero_head_before" "$success_zero_head_after" --binary --no-ext-diff)" "$success_zero_diff_before"
assert_equals "$(state_get "$repo_success_zero/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 1,
  "activeIssueNumber": 21
}'
assert_repo_clean "$repo_success_zero"
assert_equals "$(current_branch "$repo_success_zero")" 'scratch'
git -C "$repo_success_zero" check-ignore -q .ai-dev/workflow.json

repo_success_nonzero="$TMP_DIR/repo-success-nonzero"
init_repo "$repo_success_nonzero"
git -C "$repo_success_nonzero" checkout -q -b scratch
state_set "$repo_success_nonzero/subdir" '{"activeIssueNumber":22,"activeIssueTitle":"Checkpoint title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/22","mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
printf 'next checkpoint\n' >> "$repo_success_nonzero/tracked.txt"
git -C "$repo_success_nonzero" add tracked.txt
success_nonzero_output="$TMP_DIR/success-nonzero-output"
if run_flow_capture "$repo_success_nonzero/subdir" "$success_nonzero_output" commit; then
	success_nonzero_status=0
else
	success_nonzero_status=$?
fi
	success_nonzero_text="$(cat "$success_nonzero_output")"
assert_equals "$success_nonzero_status" '0'
success_nonzero_head_after="$(current_head "$repo_success_nonzero")"
assert_contains "$success_nonzero_text" 'Created checkpoint 5'
assert_contains "$success_nonzero_text" "commit: $success_nonzero_head_after"
assert_contains "$success_nonzero_text" 'activeIssueNumber: 22'
assert_equals "$(current_message "$repo_success_nonzero")" '5'
assert_equals "$(state_get "$repo_success_nonzero/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 5,
  "activeIssueNumber": 22,
  "activeIssueTitle": "Checkpoint title",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/22"
}'
assert_repo_clean "$repo_success_nonzero"

repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
git -C "$repo_subdir" checkout -q -b scratch
state_set "$repo_subdir/subdir" '{"activeIssueNumber":23,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
printf 'subdir run\n' >> "$repo_subdir/tracked.txt"
git -C "$repo_subdir" add tracked.txt
subdir_output="$TMP_DIR/subdir-output"
if run_flow_capture "$repo_subdir/subdir" "$subdir_output" commit; then
	subdir_status=0
else
	subdir_status=$?
fi
	subdir_text="$(cat "$subdir_output")"
assert_equals "$subdir_status" '0'
assert_contains "$subdir_text" 'Created checkpoint 2'
assert_equals "$(current_branch "$repo_subdir")" 'scratch'
assert_repo_clean "$repo_subdir"

repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
git -C "$repo_routing" checkout -q -b scratch
routing_output_path="$TMP_DIR/commit-output.txt"
track_config_file "$repo_routing" "$routing_output_path"
state_set "$repo_routing/subdir" '{"activeIssueNumber":24,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
printf 'routed\n' >> "$repo_routing/tracked.txt"
git -C "$repo_routing" add tracked.txt
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" commit; then
	routing_status=0
else
	routing_status=$?
fi
	routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
assert_equals "$routing_terminal_text" "Output written to $routing_output_path"
routing_file_text="$(cat "$routing_output_path")"
assert_contains "$routing_file_text" 'Created checkpoint 3'
assert_contains "$routing_file_text" 'commit: '
assert_contains "$routing_file_text" 'activeIssueNumber: 24'
assert_repo_clean "$repo_routing"

repo_commit_fail="$TMP_DIR/repo-commit-fail"
init_repo "$repo_commit_fail"
git -C "$repo_commit_fail" checkout -q -b scratch
state_set "$repo_commit_fail/subdir" '{"activeIssueNumber":25,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
mkdir -p "$repo_commit_fail/.git/hooks"
cat >"$repo_commit_fail/.git/hooks/pre-commit" <<'EOF'
#!/usr/bin/env bash
printf 'hook failure\n' >&2
exit 1
EOF
chmod +x "$repo_commit_fail/.git/hooks/pre-commit"
printf 'blocked\n' >> "$repo_commit_fail/tracked.txt"
git -C "$repo_commit_fail" add tracked.txt
commit_fail_head_before="$(current_head "$repo_commit_fail")"
commit_fail_state_before="$(state_get "$repo_commit_fail/subdir")"
commit_fail_output="$TMP_DIR/commit-fail-output"
if run_flow_capture "$repo_commit_fail/subdir" "$commit_fail_output" commit; then
	commit_fail_status=0
else
	commit_fail_status=$?
fi
	commit_fail_text="$(cat "$commit_fail_output")"
assert_equals "$commit_fail_status" '1'
assert_contains "$commit_fail_text" 'hook failure'
assert_contains "$commit_fail_text" 'Git commit failed'
assert_equals "$(current_head "$repo_commit_fail")" "$commit_fail_head_before"
assert_equals "$(state_get "$repo_commit_fail/subdir")" "$commit_fail_state_before"
assert_not_contains "$commit_fail_text" 'Created checkpoint'

if [[ "$(id -u)" != '0' ]]; then
	repo_state_fail="$TMP_DIR/repo-state-fail"
	init_repo "$repo_state_fail"
	git -C "$repo_state_fail" checkout -q -b scratch
	state_set "$repo_state_fail/subdir" '{"activeIssueNumber":26,"activeIssueTitle":"Persist title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/26","mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
	printf 'persist failure\n' >> "$repo_state_fail/tracked.txt"
	git -C "$repo_state_fail" add tracked.txt
	state_fail_head_before="$(current_head "$repo_state_fail")"
	state_fail_state_before="$(cat "$repo_state_fail/.ai-dev/workflow.json")"
	chmod 500 "$repo_state_fail/.ai-dev"
	state_fail_output="$TMP_DIR/state-fail-output"
	if run_flow_capture "$repo_state_fail/subdir" "$state_fail_output" commit; then
		state_fail_status=0
	else
		state_fail_status=$?
	fi
	chmod 700 "$repo_state_fail/.ai-dev"
	state_fail_text="$(cat "$state_fail_output")"
	assert_equals "$state_fail_status" '1'
	assert_contains "$state_fail_text" 'Cannot write workflow state to'
	assert_not_contains "$state_fail_text" 'Traceback'
	assert_equals "$(current_parent "$repo_state_fail")" "$state_fail_head_before"
	assert_equals "$(current_message "$repo_state_fail")" '3'
	assert_equals "$(cat "$repo_state_fail/.ai-dev/workflow.json")" "$state_fail_state_before"
	assert_equals "$(state_get "$repo_state_fail/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 2,
  "activeIssueNumber": 26,
  "activeIssueTitle": "Persist title",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/26"
}'
	assert_equals "$(cached_diff "$repo_state_fail")" ''
	assert_equals "$(head_diff "$repo_state_fail")" ''
	assert_equals "$(current_branch "$repo_state_fail")" 'scratch'
fi

printf 'flow commit tests passed\n'
