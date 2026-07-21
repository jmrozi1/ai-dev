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

line_number_for() {
	local haystack="$1"
	local needle="$2"
	printf '%s\n' "$haystack" | grep -nF "$needle" | head -n 1 | cut -d: -f1
}

assert_line_order() {
	local haystack="$1"
	local first="$2"
	local second="$3"

	local first_line
	first_line="$(line_number_for "$haystack" "$first")"
	local second_line
	second_line="$(line_number_for "$haystack" "$second")"

	if [[ -z "$first_line" || -z "$second_line" ]]; then
		printf 'line order assertion missing line(s): %s | %s\n' "$first" "$second" >&2
		exit 1
	fi

	if (( first_line >= second_line )); then
		printf 'expected line order: "%s" before "%s"\n' "$first" "$second" >&2
		printf 'actual output:\n%s\n' "$haystack" >&2
		exit 1
	fi
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
		git config user.name 'Flow Status Tests'
		git config user.email 'flow-status-tests@example.com'
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

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" rev-parse --abbrev-ref HEAD
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

assert_repo_clean() {
	local repo_root="$1"
	assert_equals "$(repo_status_porcelain "$repo_root")" ''
	assert_equals "$(cached_diff "$repo_root")" ''
	assert_equals "$(worktree_diff "$repo_root")" ''
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

# argument handling and help usage
repo_args="$TMP_DIR/repo-args"
init_repo "$repo_args"

no_arg_output="$(run_flow "$repo_args/subdir" status)"
assert_equals "$no_arg_output" $'No active workflow.\nBranch: main'

flag_v_output="$(run_flow "$repo_args/subdir" status -v)"
flag_verbose_output="$(run_flow "$repo_args/subdir" status --verbose)"
assert_equals "$flag_v_output" "$flag_verbose_output"

unknown_flag_output="$TMP_DIR/unknown-flag-output"
if run_flow_capture "$repo_args/subdir" "$unknown_flag_output" status -x; then
	unknown_flag_status=0
else
	unknown_flag_status=$?
fi
assert_equals "$unknown_flag_status" '1'
assert_contains "$(cat "$unknown_flag_output")" 'Usage: flow status [-v|--verbose]'

positional_output="$TMP_DIR/positional-output"
if run_flow_capture "$repo_args/subdir" "$positional_output" status detail; then
	positional_status=0
else
	positional_status=$?
fi
assert_equals "$positional_status" '1'
assert_contains "$(cat "$positional_output")" 'Usage: flow status [-v|--verbose]'

multi_output="$TMP_DIR/multi-output"
if run_flow_capture "$repo_args/subdir" "$multi_output" status -v --verbose; then
	multi_status=0
else
	multi_status=$?
fi
assert_equals "$multi_status" '1'
assert_contains "$(cat "$multi_output")" 'Usage: flow status [-v|--verbose]'

empty_arg_output="$TMP_DIR/empty-arg-output"
if run_flow_capture "$repo_args/subdir" "$empty_arg_output" status ''; then
	empty_arg_status=0
else
	empty_arg_status=$?
fi
assert_equals "$empty_arg_status" '1'
assert_contains "$(cat "$empty_arg_output")" 'Usage: flow status [-v|--verbose]'

help_output="$(run_flow "$repo_args/subdir" help)"
assert_contains "$help_output" 'status     Show the active issue and current repository state.'

# outside repository
outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" status; then
	outside_status=0
else
	outside_status=$?
fi
assert_equals "$outside_status" '1'
assert_contains "$(cat "$outside_output")" 'Not inside a Git repository'

# inactive clean default + verbose
repo_inactive="$TMP_DIR/repo-inactive"
init_repo "$repo_inactive"
inactive_default="$(run_flow "$repo_inactive/subdir" status)"
assert_equals "$inactive_default" $'No active workflow.\nBranch: main'
assert_not_contains "$inactive_default" 'checkpoint: 0'
assert_not_contains "$inactive_default" 'staged'
assert_not_contains "$inactive_default" 'untracked'

inactive_verbose="$(run_flow "$repo_inactive/subdir" status -v)"
assert_contains "$inactive_verbose" 'Workflow:'
assert_contains "$inactive_verbose" '  state: inactive'
assert_contains "$inactive_verbose" '  checkpoint: 0'
assert_contains "$inactive_verbose" 'Repository:'
assert_contains "$inactive_verbose" '  current branch: main'
assert_contains "$inactive_verbose" 'Working tree:'
assert_contains "$inactive_verbose" '  clean'

# active clean output with and without title
repo_active_title="$TMP_DIR/repo-active-title"
init_repo "$repo_active_title"
git -C "$repo_active_title" checkout -q -b scratch
state_set "$repo_active_title/subdir" '{"activeIssueNumber":42,"activeIssueTitle":"Improve flow status","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
active_title_default="$(run_flow "$repo_active_title/subdir" status)"
assert_equals "$active_title_default" $'Issue 42 — Improve flow status\nBranch: scratch'
assert_not_contains "$active_title_default" 'Checkpoint: 0'
assert_not_contains "$active_title_default" 'staged'
assert_not_contains "$active_title_default" 'modified'
assert_not_contains "$active_title_default" 'untracked'

repo_active_no_title="$TMP_DIR/repo-active-no-title"
init_repo "$repo_active_no_title"
git -C "$repo_active_no_title" checkout -q -b scratch
state_set "$repo_active_no_title/subdir" '{"activeIssueNumber":43,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
active_no_title_default="$(run_flow "$repo_active_no_title/subdir" status)"
assert_equals "$active_no_title_default" $'Issue 43\nBranch: scratch'

# unexpected current branch
repo_unexpected_branch="$TMP_DIR/repo-unexpected-branch"
init_repo "$repo_unexpected_branch"
git -C "$repo_unexpected_branch" checkout -q -b scratch
state_set "$repo_unexpected_branch/subdir" '{"activeIssueNumber":50,"activeIssueTitle":"Wrong branch","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
git -C "$repo_unexpected_branch" checkout -q main
unexpected_branch_output="$(run_flow "$repo_unexpected_branch/subdir" status)"
assert_contains "$unexpected_branch_output" 'Issue 50 — Wrong branch'
assert_contains "$unexpected_branch_output" 'Branch: main'
assert_contains "$unexpected_branch_output" 'Working tree:'
assert_contains "$unexpected_branch_output" 'Expected branch: scratch'

# branch relationship wording and singular/plural
repo_ahead_one="$TMP_DIR/repo-ahead-one"
init_repo "$repo_ahead_one"
git -C "$repo_ahead_one" checkout -q -b scratch
state_set "$repo_ahead_one/subdir" '{"activeIssueNumber":60,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
create_commit_on_current_branch "$repo_ahead_one" ahead.txt 'ahead one' 'ahead one'
ahead_one_output="$(run_flow "$repo_ahead_one/subdir" status)"
assert_contains "$ahead_one_output" '1 commit ahead of main'
assert_not_contains "$ahead_one_output" 'Checkpoint:'

repo_ahead_two="$TMP_DIR/repo-ahead-two"
init_repo "$repo_ahead_two"
git -C "$repo_ahead_two" checkout -q -b scratch
state_set "$repo_ahead_two/subdir" '{"activeIssueNumber":61,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_ahead_two" a1.txt 'a1' 'a1'
create_commit_on_current_branch "$repo_ahead_two" a2.txt 'a2' 'a2'
ahead_two_output="$(run_flow "$repo_ahead_two/subdir" status)"
assert_contains "$ahead_two_output" '2 commits ahead of main'
assert_not_contains "$ahead_two_output" 'Checkpoint:'

repo_behind_one="$TMP_DIR/repo-behind-one"
init_repo "$repo_behind_one"
git -C "$repo_behind_one" checkout -q -b scratch
state_set "$repo_behind_one/subdir" '{"activeIssueNumber":62,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
git -C "$repo_behind_one" checkout -q main
create_commit_on_current_branch "$repo_behind_one" b1.txt 'b1' 'b1'
git -C "$repo_behind_one" checkout -q scratch
behind_one_output="$(run_flow "$repo_behind_one/subdir" status)"
assert_contains "$behind_one_output" '1 commit behind main'

repo_behind_two="$TMP_DIR/repo-behind-two"
init_repo "$repo_behind_two"
git -C "$repo_behind_two" checkout -q -b scratch
state_set "$repo_behind_two/subdir" '{"activeIssueNumber":63,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
git -C "$repo_behind_two" checkout -q main
create_commit_on_current_branch "$repo_behind_two" b1.txt 'b1' 'b1'
create_commit_on_current_branch "$repo_behind_two" b2.txt 'b2' 'b2'
git -C "$repo_behind_two" checkout -q scratch
behind_two_output="$(run_flow "$repo_behind_two/subdir" status)"
assert_contains "$behind_two_output" '2 commits behind main'

repo_diverged="$TMP_DIR/repo-diverged"
init_repo "$repo_diverged"
git -C "$repo_diverged" checkout -q -b scratch
state_set "$repo_diverged/subdir" '{"activeIssueNumber":64,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
create_commit_on_current_branch "$repo_diverged" s1.txt 's1' 's1'
git -C "$repo_diverged" checkout -q main
create_commit_on_current_branch "$repo_diverged" m1.txt 'm1' 'm1'
git -C "$repo_diverged" checkout -q scratch
diverged_output="$(run_flow "$repo_diverged/subdir" status)"
assert_contains "$diverged_output" 'Branches have diverged: 1 on main, 1 on scratch'

# count categories and partial staged behavior
repo_counts="$TMP_DIR/repo-counts"
init_repo "$repo_counts"
git -C "$repo_counts" checkout -q -b scratch
state_set "$repo_counts/subdir" '{"activeIssueNumber":70,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'mod two\n' > "$repo_counts/modified-two.txt"
git -C "$repo_counts" add modified-two.txt
git -C "$repo_counts" commit -q -m 'track modified-two'
printf 'staged one\n' > "$repo_counts/staged-one.txt"
printf 'staged two\n' > "$repo_counts/staged-two.txt"
git -C "$repo_counts" add staged-one.txt staged-two.txt
printf 'mod one\n' >> "$repo_counts/tracked.txt"
printf 'mod two again\n' >> "$repo_counts/modified-two.txt"
printf 'u1\n' > "$repo_counts/untracked-one.txt"
printf 'u2\n' > "$repo_counts/untracked-two.txt"
counts_output="$(run_flow "$repo_counts/subdir" status)"
assert_contains "$counts_output" '2 staged'
assert_contains "$counts_output" '2 modified'
assert_contains "$counts_output" '2 untracked'

repo_partial="$TMP_DIR/repo-partial"
init_repo "$repo_partial"
git -C "$repo_partial" checkout -q -b scratch
state_set "$repo_partial/subdir" '{"activeIssueNumber":71,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'one\ntwo\n' > "$repo_partial/partial.txt"
git -C "$repo_partial" add partial.txt
git -C "$repo_partial" commit -q -m 'track partial'
printf 'three\n' >> "$repo_partial/partial.txt"
git -C "$repo_partial" add partial.txt
printf 'four\n' >> "$repo_partial/partial.txt"
partial_output="$(run_flow "$repo_partial/subdir" status)"
assert_contains "$partial_output" '1 staged'
assert_contains "$partial_output" '1 modified'

# combined deviations deterministic order
repo_order="$TMP_DIR/repo-order"
init_repo "$repo_order"
git -C "$repo_order" checkout -q -b scratch
state_set "$repo_order/subdir" '{"activeIssueNumber":72,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
create_commit_on_current_branch "$repo_order" ahead.txt 'ahead' 'ahead'
printf 'stage\n' > "$repo_order/stage.txt"
git -C "$repo_order" add stage.txt
printf 'mod\n' >> "$repo_order/tracked.txt"
printf 'new\n' > "$repo_order/untracked.txt"
order_output="$(run_flow "$repo_order/subdir" status)"
assert_line_order "$order_output" '1 commit ahead of main' '1 staged'
assert_line_order "$order_output" '1 staged' '1 modified'
assert_line_order "$order_output" '1 modified' '1 untracked'

# checkpoint shown only when useful
repo_checkpoint="$TMP_DIR/repo-checkpoint"
init_repo "$repo_checkpoint"
git -C "$repo_checkpoint" checkout -q -b scratch
state_set "$repo_checkpoint/subdir" '{"activeIssueNumber":73,"mainBranch":"main","scratchBranch":"scratch","checkpoint":5}' >/dev/null
create_commit_on_current_branch "$repo_checkpoint" only-one.txt 'one' 'one'
checkpoint_output="$(run_flow "$repo_checkpoint/subdir" status)"
assert_contains "$checkpoint_output" 'Checkpoint: 5'

# verbose active workflow details and clean tree
verbose_active_output="$(run_flow "$repo_active_title/subdir" status --verbose)"
assert_contains "$verbose_active_output" 'Workflow:'
assert_contains "$verbose_active_output" '  state: active'
assert_contains "$verbose_active_output" '  issue number: 42'
assert_contains "$verbose_active_output" '  issue title: Improve flow status'
assert_contains "$verbose_active_output" '  checkpoint: 0'
assert_contains "$verbose_active_output" 'Repository:'
assert_contains "$verbose_active_output" '  current branch: scratch'
assert_contains "$verbose_active_output" '  main branch: main'
assert_contains "$verbose_active_output" '  scratch branch: scratch'
assert_contains "$verbose_active_output" '  relation: scratch equals main'
assert_contains "$verbose_active_output" 'Working tree:'
assert_contains "$verbose_active_output" '  clean'

# verbose working-tree path listing, sorting, and rename representation
repo_verbose_paths="$TMP_DIR/repo-verbose-paths"
init_repo "$repo_verbose_paths"
git -C "$repo_verbose_paths" checkout -q -b scratch
state_set "$repo_verbose_paths/subdir" '{"activeIssueNumber":80,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'old\n' > "$repo_verbose_paths/old-name.txt"
printf 'm1\n' > "$repo_verbose_paths/m1.txt"
printf 'm2\n' > "$repo_verbose_paths/m2.txt"
git -C "$repo_verbose_paths" add old-name.txt m1.txt m2.txt
git -C "$repo_verbose_paths" commit -q -m 'track files'
printf 'a\n' > "$repo_verbose_paths/a.txt"
printf 'z\n' > "$repo_verbose_paths/z.txt"
git -C "$repo_verbose_paths" add a.txt z.txt
git -C "$repo_verbose_paths" mv old-name.txt new-name.txt
printf 'm2 changed\n' >> "$repo_verbose_paths/m2.txt"
printf 'm1 changed\n' >> "$repo_verbose_paths/m1.txt"
printf 'u2\n' > "$repo_verbose_paths/u2.txt"
printf 'u1\n' > "$repo_verbose_paths/u1.txt"
verbose_paths_output="$(run_flow "$repo_verbose_paths/subdir" status -v)"
assert_contains "$verbose_paths_output" 'Working tree:'
assert_contains "$verbose_paths_output" '  staged:'
assert_contains "$verbose_paths_output" '    old-name.txt -> new-name.txt'
assert_contains "$verbose_paths_output" '  modified:'
assert_contains "$verbose_paths_output" '    m1.txt'
assert_contains "$verbose_paths_output" '    m2.txt'
assert_contains "$verbose_paths_output" '  untracked:'
assert_contains "$verbose_paths_output" '    u1.txt'
assert_contains "$verbose_paths_output" '    u2.txt'
assert_line_order "$verbose_paths_output" '    a.txt' '    old-name.txt -> new-name.txt'
assert_line_order "$verbose_paths_output" '    old-name.txt -> new-name.txt' '    z.txt'
assert_line_order "$verbose_paths_output" '    m1.txt' '    m2.txt'
assert_line_order "$verbose_paths_output" '    u1.txt' '    u2.txt'

# detached HEAD handling
repo_detached="$TMP_DIR/repo-detached"
init_repo "$repo_detached"
git -C "$repo_detached" checkout -q --detach
detached_default="$(run_flow "$repo_detached/subdir" status)"
assert_contains "$detached_default" 'No active workflow.'
assert_contains "$detached_default" 'Branch: detached HEAD'

detached_verbose="$(run_flow "$repo_detached/subdir" status -v)"
assert_contains "$detached_verbose" 'current branch: detached HEAD'

# ignored workflow state excluded
repo_ignored="$TMP_DIR/repo-ignored"
init_repo "$repo_ignored"
git -C "$repo_ignored" checkout -q -b scratch
state_set "$repo_ignored/subdir" '{"activeIssueNumber":81,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
ignored_verbose="$(run_flow "$repo_ignored/subdir" status --verbose)"
assert_contains "$ignored_verbose" '  clean'
assert_not_contains "$ignored_verbose" '.ai-dev/workflow.json'
git -C "$repo_ignored" check-ignore -q .ai-dev/workflow.json

# repository subdirectory support
repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
mkdir -p "$repo_subdir/subdir/nested/deeper"
subdir_output="$(run_flow "$repo_subdir/subdir/nested/deeper" status)"
assert_equals "$subdir_output" $'No active workflow.\nBranch: main'

# output routing and malformed config strictness
repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
routed_output_path="$TMP_DIR/status-routed.txt"
write_config_file "$repo_routing" "$routed_output_path"
git -C "$repo_routing" add .ai-dev/config.json
git -C "$repo_routing" commit -q -m 'track config'
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" status; then
	routing_status=0
else
	routing_status=$?
fi
assert_equals "$routing_status" '0'
assert_equals "$(cat "$routing_terminal_output")" "Output written to $routed_output_path"
assert_contains "$(cat "$routed_output_path")" 'No active workflow.'
assert_contains "$(cat "$routed_output_path")" 'Branch: main'

repo_malformed="$TMP_DIR/repo-malformed"
init_repo "$repo_malformed"
mkdir -p "$repo_malformed/.ai-dev"
printf '{ invalid json\n' > "$repo_malformed/.ai-dev/config.json"
malformed_output="$TMP_DIR/malformed-output"
if run_flow_capture "$repo_malformed/subdir" "$malformed_output" status; then
	malformed_status=0
else
	malformed_status=$?
fi
assert_equals "$malformed_status" '1'
assert_contains "$(cat "$malformed_output")" 'Invalid JSON in'

# read-only guarantees: workflow state, refs, HEAD, index, and worktree unchanged
repo_read_only="$TMP_DIR/repo-read-only"
init_repo "$repo_read_only"
git -C "$repo_read_only" checkout -q -b scratch
state_set "$repo_read_only/subdir" '{"activeIssueNumber":90,"activeIssueTitle":"Read only","mainBranch":"main","scratchBranch":"scratch","checkpoint":3}' >/dev/null
printf 'staged\n' >> "$repo_read_only/tracked.txt"
git -C "$repo_read_only" add tracked.txt
printf 'modified\n' >> "$repo_read_only/tracked.txt"
printf 'untracked\n' > "$repo_read_only/untracked.txt"

state_before="$(cat "$repo_read_only/.ai-dev/workflow.json")"
branch_before="$(current_branch "$repo_read_only")"
head_before="$(current_head "$repo_read_only")"
main_before="$(branch_head "$repo_read_only" main)"
scratch_before="$(branch_head "$repo_read_only" scratch)"
index_before="$(cached_diff "$repo_read_only")"
worktree_before="$(worktree_diff "$repo_read_only")"
status_before="$(repo_status_porcelain "$repo_read_only")"
tracked_before="$(cat "$repo_read_only/tracked.txt")"
untracked_before="$(cat "$repo_read_only/untracked.txt")"

read_only_default_output="$(run_flow "$repo_read_only/subdir" status)"
read_only_verbose_output="$(run_flow "$repo_read_only/subdir" status -v)"
assert_contains "$read_only_default_output" 'Issue 90 — Read only'
assert_contains "$read_only_verbose_output" '  issue number: 90'
assert_contains "$read_only_verbose_output" '  state: active'

assert_equals "$(cat "$repo_read_only/.ai-dev/workflow.json")" "$state_before"
assert_equals "$(current_branch "$repo_read_only")" "$branch_before"
assert_equals "$(current_head "$repo_read_only")" "$head_before"
assert_equals "$(branch_head "$repo_read_only" main)" "$main_before"
assert_equals "$(branch_head "$repo_read_only" scratch)" "$scratch_before"
assert_equals "$(cached_diff "$repo_read_only")" "$index_before"
assert_equals "$(worktree_diff "$repo_read_only")" "$worktree_before"
assert_equals "$(repo_status_porcelain "$repo_read_only")" "$status_before"
assert_equals "$(cat "$repo_read_only/tracked.txt")" "$tracked_before"
assert_equals "$(cat "$repo_read_only/untracked.txt")" "$untracked_before"
assert_equals "$(state_get "$repo_read_only/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 3,
  "activeIssueNumber": 90,
  "activeIssueTitle": "Read only"
}'

printf 'flow status tests passed\n'
