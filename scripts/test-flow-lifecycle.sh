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
		git config user.name 'Flow Lifecycle Tests'
		git config user.email 'flow-lifecycle-tests@example.com'

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

state_get() {
	local cwd="$1"

	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" __test-state-get
	)
}

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" branch --show-current
}

branch_head() {
	local repo_root="$1"
	local branch_name="$2"

	git -C "$repo_root" rev-parse "$branch_name"
}

branch_tree() {
	local repo_root="$1"
	local branch_name="$2"

	git -C "$repo_root" rev-parse "$branch_name^{tree}"
}

head_message() {
	local repo_root="$1"
	git -C "$repo_root" log -1 --format=%B
}

repo_status() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
}

assert_repo_clean() {
	local repo_root="$1"
	assert_equals "$(repo_status "$repo_root")" ''
}

lifecycle_repo="$TMP_DIR/lifecycle"
init_repo "$lifecycle_repo"

start_output="$(run_flow "$lifecycle_repo/subdir" start 123)"
assert_contains "$start_output" 'Started issue 123'
assert_contains "$start_output" 'mainBranch: main'
assert_contains "$start_output" 'scratchBranch: scratch'
assert_contains "$start_output" 'checkpoint: 0'

assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_equals "$(branch_head "$lifecycle_repo" main)" "$(branch_head "$lifecycle_repo" scratch)"
assert_repo_clean "$lifecycle_repo"

status_after_start="$(run_flow "$lifecycle_repo/subdir" status)"
assert_contains "$status_after_start" 'Workflow: active'
assert_contains "$status_after_start" 'activeIssueNumber: 123'
assert_contains "$status_after_start" 'checkpoint: 0'
assert_contains "$status_after_start" 'currentBranch: scratch'
assert_contains "$status_after_start" 'workingTree: clean'
assert_contains "$status_after_start" 'branchState: scratch equal to main'

printf 'checkpoint one\n' >> "$lifecycle_repo/tracked.txt"
printf 'new file one\n' > "$lifecycle_repo/one.txt"

review_one_output="$(run_flow "$lifecycle_repo/subdir" review)"
assert_contains "$review_one_output" 'diff --git a/one.txt b/one.txt'
assert_contains "$review_one_output" 'diff --git a/tracked.txt b/tracked.txt'

commit_one_output="$(run_flow "$lifecycle_repo/subdir" commit)"
assert_contains "$commit_one_output" 'Created checkpoint 1'
assert_contains "$commit_one_output" 'activeIssueNumber: 123'
assert_equals "$(head_message "$lifecycle_repo")" '1'
assert_repo_clean "$lifecycle_repo"

status_after_commit_one="$(run_flow "$lifecycle_repo/subdir" status)"
assert_contains "$status_after_commit_one" 'checkpoint: 1'
assert_contains "$status_after_commit_one" 'branchState: scratch ahead of main by 1 commit(s)'

printf 'checkpoint two\n' >> "$lifecycle_repo/tracked.txt"
printf 'new file two\n' > "$lifecycle_repo/two.txt"

review_two_output="$(run_flow "$lifecycle_repo/subdir" review)"
assert_contains "$review_two_output" 'diff --git a/two.txt b/two.txt'

commit_two_output="$(run_flow "$lifecycle_repo/subdir" commit)"
assert_contains "$commit_two_output" 'Created checkpoint 2'
assert_equals "$(head_message "$lifecycle_repo")" '2'
assert_repo_clean "$lifecycle_repo"

main_before_promote="$(branch_head "$lifecycle_repo" main)"
scratch_tree_before_promote="$(branch_tree "$lifecycle_repo" scratch)"

promote_output="$(run_flow "$lifecycle_repo/subdir" promote 'Test complete workflow')"
assert_contains "$promote_output" 'Promoted scratch to main'
assert_contains "$promote_output" 'checkpoint: 0'
assert_contains "$promote_output" 'activeIssueNumber: 123'

main_after_promote="$(branch_head "$lifecycle_repo" main)"
scratch_after_promote="$(branch_head "$lifecycle_repo" scratch)"

assert_equals "$main_after_promote" "$scratch_after_promote"
assert_equals "$(git -C "$lifecycle_repo" rev-parse "$main_after_promote^")" "$main_before_promote"
assert_equals "$(branch_tree "$lifecycle_repo" main)" "$scratch_tree_before_promote"
assert_equals "$(head_message "$lifecycle_repo")" 'Test complete workflow'
assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_repo_clean "$lifecycle_repo"

status_after_promote="$(run_flow "$lifecycle_repo/subdir" status)"
assert_contains "$status_after_promote" 'Workflow: active'
assert_contains "$status_after_promote" 'activeIssueNumber: 123'
assert_contains "$status_after_promote" 'checkpoint: 0'
assert_contains "$status_after_promote" 'branchState: scratch equal to main'

complete_output="$(run_flow "$lifecycle_repo/subdir" complete)"
assert_contains "$complete_output" 'Completed issue 123'
assert_contains "$complete_output" 'Workflow: inactive'
assert_contains "$complete_output" 'checkpoint: 0'

status_after_complete="$(run_flow "$lifecycle_repo/subdir" status)"
assert_contains "$status_after_complete" 'Workflow: inactive'
assert_not_contains "$status_after_complete" 'activeIssueNumber:'
assert_contains "$status_after_complete" 'checkpoint: 0'
assert_contains "$status_after_complete" 'branchState: scratch equal to main'

assert_equals "$(state_get "$lifecycle_repo/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'

assert_equals "$(branch_head "$lifecycle_repo" main)" "$main_after_promote"
assert_equals "$(branch_head "$lifecycle_repo" scratch)" "$main_after_promote"
assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_repo_clean "$lifecycle_repo"

reset_repo="$TMP_DIR/reset-lifecycle"
init_repo "$reset_repo"

run_flow "$reset_repo/subdir" start 124 >/dev/null

printf 'checkpoint work\n' >> "$reset_repo/tracked.txt"
git -C "$reset_repo" add tracked.txt
run_flow "$reset_repo/subdir" commit >/dev/null

printf 'staged change\n' >> "$reset_repo/tracked.txt"
git -C "$reset_repo" add tracked.txt
printf 'unstaged change\n' >> "$reset_repo/tracked.txt"
printf 'untracked file\n' > "$reset_repo/untracked.txt"
mkdir -p "$reset_repo/untracked-dir"
printf 'nested\n' > "$reset_repo/untracked-dir/nested.txt"

reset_output="$(run_flow "$reset_repo/subdir" reset)"
assert_contains "$reset_output" 'Reset scratch to main'
assert_contains "$reset_output" 'checkpoint: 0'
assert_contains "$reset_output" 'activeIssueNumber: 124'

assert_equals "$(branch_head "$reset_repo" main)" "$(branch_head "$reset_repo" scratch)"
assert_equals "$(current_branch "$reset_repo")" 'scratch'
assert_repo_clean "$reset_repo"

if [[ -e "$reset_repo/untracked.txt" ]]; then
	fail 'reset did not remove untracked.txt'
fi

if [[ -e "$reset_repo/untracked-dir" ]]; then
	fail 'reset did not remove untracked-dir'
fi

reset_status="$(run_flow "$reset_repo/subdir" status)"
assert_contains "$reset_status" 'Workflow: active'
assert_contains "$reset_status" 'activeIssueNumber: 124'
assert_contains "$reset_status" 'checkpoint: 0'
assert_contains "$reset_status" 'workingTree: clean'
assert_contains "$reset_status" 'branchState: scratch equal to main'

printf 'flow lifecycle tests passed\n'
