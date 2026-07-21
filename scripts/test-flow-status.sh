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

capture_index_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --cached --no-ext-diff
}

capture_worktree_diff() {
	local repo_root="$1"
	git -C "$repo_root" diff --no-ext-diff
}

# no arguments accepted, inactive defaults, clean tree, scratch missing, and subdirectory support
repo_inactive="$TMP_DIR/repo-inactive"
init_repo "$repo_inactive"
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" status; then
	inactive_status=0
else
	inactive_status=$?
fi
inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" "0"
assert_equals "$inactive_text" $'Workflow: inactive\nmainBranch: main\nscratchBranch: scratch\ncheckpoint: 0\ncurrentBranch: main\nworkingTree: clean\nstaged: no\nunstaged: no\nuntracked: no\nbranchState: scratch branch missing'
assert_equals "$(current_branch "$repo_inactive")" 'main'
assert_equals "$(current_head "$repo_inactive")" "$(git -C "$repo_inactive" rev-parse main)"
assert_equals "$(repo_status_porcelain "$repo_inactive")" ''

# extra arguments rejected
extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_inactive/subdir" "$extra_output" status extra; then
	extra_status=0
else
	extra_status=$?
fi
extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" "1"
assert_contains "$extra_text" 'Usage: flow status'

extra_many_output="$TMP_DIR/extra-many-output"
if run_flow_capture "$repo_inactive/subdir" "$extra_many_output" status extra more; then
	extra_many_status=0
else
	extra_many_status=$?
fi
extra_many_text="$(cat "$extra_many_output")"
assert_equals "$extra_many_status" "1"
assert_contains "$extra_many_text" 'Usage: flow status'

# outside git repository
outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" status; then
	outside_status=0
else
	outside_status=$?
fi
outside_text="$(cat "$outside_output")"
assert_equals "$outside_status" "1"
assert_contains "$outside_text" 'Not inside a Git repository'

# active workflow without title remains clean because workflow file is ignored
repo_active="$TMP_DIR/repo-active"
init_repo "$repo_active"
git -C "$repo_active" checkout -q -b scratch
state_set "$repo_active/subdir" '{"activeIssueNumber":21,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
active_output="$TMP_DIR/active-output"
if run_flow_capture "$repo_active/subdir" "$active_output" status; then
	active_status=0
else
	active_status=$?
fi
active_text="$(cat "$active_output")"
assert_equals "$active_status" "0"
assert_contains "$active_text" 'Workflow: active'
assert_contains "$active_text" 'activeIssueNumber: 21'
assert_not_contains "$active_text" 'activeIssueTitle:'
assert_contains "$active_text" 'mainBranch: main'
assert_contains "$active_text" 'scratchBranch: scratch'
assert_contains "$active_text" 'checkpoint: 2'
assert_contains "$active_text" 'currentBranch: scratch'
assert_contains "$active_text" 'workingTree: clean'
assert_contains "$active_text" 'staged: no'
assert_contains "$active_text" 'unstaged: no'
assert_contains "$active_text" 'untracked: no'
assert_contains "$active_text" 'branchState: scratch equal to main'
assert_file_exists "$repo_active/.ai-dev/workflow.json"
git -C "$repo_active" check-ignore -q .ai-dev/workflow.json
assert_equals "$(repo_status_porcelain "$repo_active")" ''

# optional active issue title displayed only when present
repo_title="$TMP_DIR/repo-title"
init_repo "$repo_title"
state_set "$repo_title/subdir" '{"activeIssueNumber":22,"activeIssueTitle":"Investigate bug","mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
title_output="$TMP_DIR/title-output"
if run_flow_capture "$repo_title/subdir" "$title_output" status; then
	title_status=0
else
	title_status=$?
fi
title_text="$(cat "$title_output")"
assert_equals "$title_status" "0"
assert_contains "$title_text" 'activeIssueNumber: 22'
assert_contains "$title_text" 'activeIssueTitle: Investigate bug'

# staged-only changes
repo_staged="$TMP_DIR/repo-staged"
init_repo "$repo_staged"
printf 'staged\n' >> "$repo_staged/tracked.txt"
git -C "$repo_staged" add tracked.txt
staged_output="$TMP_DIR/staged-output"
run_flow_capture "$repo_staged/subdir" "$staged_output" status
staged_text="$(cat "$staged_output")"
assert_contains "$staged_text" 'workingTree: dirty'
assert_contains "$staged_text" 'staged: yes'
assert_contains "$staged_text" 'unstaged: no'
assert_contains "$staged_text" 'untracked: no'

# unstaged-only tracked changes
repo_unstaged="$TMP_DIR/repo-unstaged"
init_repo "$repo_unstaged"
printf 'unstaged\n' >> "$repo_unstaged/tracked.txt"
unstaged_output="$TMP_DIR/unstaged-output"
run_flow_capture "$repo_unstaged/subdir" "$unstaged_output" status
unstaged_text="$(cat "$unstaged_output")"
assert_contains "$unstaged_text" 'workingTree: dirty'
assert_contains "$unstaged_text" 'staged: no'
assert_contains "$unstaged_text" 'unstaged: yes'
assert_contains "$unstaged_text" 'untracked: no'

# untracked-only changes
repo_untracked="$TMP_DIR/repo-untracked"
init_repo "$repo_untracked"
printf 'new\n' > "$repo_untracked/untracked.txt"
untracked_output="$TMP_DIR/untracked-output"
run_flow_capture "$repo_untracked/subdir" "$untracked_output" status
untracked_text="$(cat "$untracked_output")"
assert_contains "$untracked_text" 'workingTree: dirty'
assert_contains "$untracked_text" 'staged: no'
assert_contains "$untracked_text" 'unstaged: no'
assert_contains "$untracked_text" 'untracked: yes'

# renamed tracked file remains a staged-only change
repo_renamed="$TMP_DIR/repo-renamed"
init_repo "$repo_renamed"
git -C "$repo_renamed" mv tracked.txt renamed.txt
renamed_output="$TMP_DIR/renamed-output"
run_flow_capture "$repo_renamed/subdir" "$renamed_output" status
renamed_text="$(cat "$renamed_output")"
assert_contains "$renamed_text" 'workingTree: dirty'
assert_contains "$renamed_text" 'staged: yes'
assert_contains "$renamed_text" 'unstaged: no'
assert_contains "$renamed_text" 'untracked: no'

# combination of dirty states
repo_combo="$TMP_DIR/repo-combo"
init_repo "$repo_combo"
printf 'staged\n' >> "$repo_combo/tracked.txt"
git -C "$repo_combo" add tracked.txt
printf 'unstaged\n' >> "$repo_combo/tracked.txt"
printf 'new\n' > "$repo_combo/untracked.txt"
combo_output="$TMP_DIR/combo-output"
run_flow_capture "$repo_combo/subdir" "$combo_output" status
combo_text="$(cat "$combo_output")"
assert_contains "$combo_text" 'workingTree: dirty'
assert_contains "$combo_text" 'staged: yes'
assert_contains "$combo_text" 'unstaged: yes'
assert_contains "$combo_text" 'untracked: yes'

# main branch missing
repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
state_set "$repo_missing_main/subdir" '{"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" status
missing_main_text="$(cat "$missing_main_output")"
assert_contains "$missing_main_text" 'mainBranch: trunk'
assert_contains "$missing_main_text" 'branchState: main branch missing'

# scratch ahead of main
repo_ahead="$TMP_DIR/repo-ahead"
init_repo "$repo_ahead"
git -C "$repo_ahead" checkout -q -b scratch
printf 'ahead\n' > "$repo_ahead/ahead.txt"
git -C "$repo_ahead" add ahead.txt
git -C "$repo_ahead" commit -q -m 'scratch ahead'
ahead_output="$TMP_DIR/ahead-output"
run_flow_capture "$repo_ahead/subdir" "$ahead_output" status
ahead_text="$(cat "$ahead_output")"
assert_contains "$ahead_text" 'branchState: scratch ahead of main by 1 commit(s)'

# scratch behind main
repo_behind="$TMP_DIR/repo-behind"
init_repo "$repo_behind"
git -C "$repo_behind" checkout -q -b scratch
git -C "$repo_behind" checkout -q main
printf 'behind\n' > "$repo_behind/behind.txt"
git -C "$repo_behind" add behind.txt
git -C "$repo_behind" commit -q -m 'main ahead'
behind_output="$TMP_DIR/behind-output"
run_flow_capture "$repo_behind/subdir" "$behind_output" status
behind_text="$(cat "$behind_output")"
assert_contains "$behind_text" 'branchState: scratch behind main by 1 commit(s)'

# branches diverged
repo_diverged="$TMP_DIR/repo-diverged"
init_repo "$repo_diverged"
git -C "$repo_diverged" checkout -q -b scratch
printf 'scratch\n' > "$repo_diverged/scratch.txt"
git -C "$repo_diverged" add scratch.txt
git -C "$repo_diverged" commit -q -m 'scratch change'
git -C "$repo_diverged" checkout -q main
printf 'main\n' > "$repo_diverged/main.txt"
git -C "$repo_diverged" add main.txt
git -C "$repo_diverged" commit -q -m 'main change'
diverged_output="$TMP_DIR/diverged-output"
run_flow_capture "$repo_diverged/subdir" "$diverged_output" status
diverged_text="$(cat "$diverged_output")"
assert_contains "$diverged_text" 'branchState: scratch and main have diverged: scratch ahead by 1 commit(s), behind by 1 commit(s)'

# custom mainBranch and scratchBranch values
repo_custom="$TMP_DIR/repo-custom"
init_repo "$repo_custom"
git -C "$repo_custom" branch trunk
git -C "$repo_custom" branch work
state_set "$repo_custom/subdir" '{"mainBranch":" trunk ","scratchBranch":" work ","checkpoint":5}' >/dev/null
custom_output="$TMP_DIR/custom-output"
run_flow_capture "$repo_custom/subdir" "$custom_output" status
custom_text="$(cat "$custom_output")"
assert_contains "$custom_text" 'mainBranch: trunk'
assert_contains "$custom_text" 'scratchBranch: work'
assert_contains "$custom_text" 'checkpoint: 5'
assert_contains "$custom_text" 'branchState: scratch equal to main'

# output routing with strict config handling
repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
routed_output_path="$TMP_DIR/status-report.txt"
write_config_file "$repo_routing" "$routed_output_path"
git -C "$repo_routing" add .ai-dev/config.json
git -C "$repo_routing" commit -q -m 'track config'
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" status; then
	routing_status=0
else
	routing_status=$?
fi
routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
assert_equals "$routing_terminal_text" "Output written to $routed_output_path"
routed_text="$(cat "$routed_output_path")"
assert_contains "$routed_text" 'Workflow: inactive'
assert_contains "$routed_text" 'currentBranch: main'

# status does not modify branch, head, index, workflow state, or working tree contents
repo_read_only="$TMP_DIR/repo-read-only"
init_repo "$repo_read_only"
git -C "$repo_read_only" checkout -q -b scratch
state_set "$repo_read_only/subdir" '{"activeIssueNumber":30,"activeIssueTitle":"Read only","mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
printf 'staged\n' >> "$repo_read_only/tracked.txt"
git -C "$repo_read_only" add tracked.txt
printf 'unstaged\n' >> "$repo_read_only/tracked.txt"
printf 'untracked\n' > "$repo_read_only/untracked.txt"
branch_before="$(current_branch "$repo_read_only")"
head_before="$(current_head "$repo_read_only")"
index_diff_before="$(capture_index_diff "$repo_read_only")"
worktree_diff_before="$(capture_worktree_diff "$repo_read_only")"
status_before="$(repo_status_porcelain "$repo_read_only")"
workflow_before="$(cat "$repo_read_only/.ai-dev/workflow.json")"
tracked_before="$(cat "$repo_read_only/tracked.txt")"
untracked_before="$(cat "$repo_read_only/untracked.txt")"
read_only_output="$TMP_DIR/read-only-output"
run_flow_capture "$repo_read_only/subdir" "$read_only_output" status
read_only_text="$(cat "$read_only_output")"
assert_contains "$read_only_text" 'Workflow: active'
assert_contains "$read_only_text" 'activeIssueTitle: Read only'
assert_contains "$read_only_text" 'currentBranch: scratch'
assert_contains "$read_only_text" 'workingTree: dirty'
assert_contains "$read_only_text" 'staged: yes'
assert_contains "$read_only_text" 'unstaged: yes'
assert_contains "$read_only_text" 'untracked: yes'
assert_equals "$(current_branch "$repo_read_only")" "$branch_before"
assert_equals "$(current_head "$repo_read_only")" "$head_before"
assert_equals "$(capture_index_diff "$repo_read_only")" "$index_diff_before"
assert_equals "$(capture_worktree_diff "$repo_read_only")" "$worktree_diff_before"
assert_equals "$(repo_status_porcelain "$repo_read_only")" "$status_before"
assert_equals "$(cat "$repo_read_only/.ai-dev/workflow.json")" "$workflow_before"
assert_equals "$(cat "$repo_read_only/tracked.txt")" "$tracked_before"
assert_equals "$(cat "$repo_read_only/untracked.txt")" "$untracked_before"

printf 'flow status tests passed\n'
