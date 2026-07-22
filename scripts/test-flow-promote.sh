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

assert_not_equals() {
	local left="$1"
	local right="$2"

	if [[ "$left" == "$right" ]]; then
		printf 'expected values to differ: %s\n' "$left" >&2
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

head_message() {
	local repo_root="$1"
	git -C "$repo_root" log -1 --format=%B
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

help_repo="$TMP_DIR/repo-help"
init_repo "$help_repo"
help_output="$TMP_DIR/help-output"
if run_flow_capture "$help_repo/subdir" "$help_output" help; then
	help_status=0
else
	help_status=$?
fi
help_text="$(cat "$help_output")"
assert_equals "$help_status" '0'
assert_contains "$help_text" 'flow promote "<commit-message>"'

repo_missing_arg="$TMP_DIR/repo-missing-arg"
init_repo "$repo_missing_arg"
missing_output="$TMP_DIR/missing-output"
if run_flow_capture "$repo_missing_arg/subdir" "$missing_output" promote; then
	missing_status=0
else
	missing_status=$?
fi
missing_text="$(cat "$missing_output")"
assert_equals "$missing_status" '1'
assert_contains "$missing_text" 'Usage: flow promote "<commit-message>"'

empty_output="$TMP_DIR/empty-output"
if run_flow_capture "$repo_missing_arg/subdir" "$empty_output" promote ''; then
	empty_status=0
else
	empty_status=$?
fi
empty_text="$(cat "$empty_output")"
assert_equals "$empty_status" '1'
assert_contains "$empty_text" 'Usage: flow promote "<commit-message>"'

extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_missing_arg/subdir" "$extra_output" promote 'msg' extra; then
	extra_status=0
else
	extra_status=$?
fi
extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" '1'
assert_contains "$extra_text" 'Usage: flow promote "<commit-message>"'

outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" promote 'msg'; then
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
create_commit_on_current_branch "$repo_inactive" inactive.txt 'inactive' 'inactive'
inactive_state_before="$(state_get "$repo_inactive/subdir")"
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" promote 'msg'; then
	inactive_status=0
else
	inactive_status=$?
fi
inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" '1'
assert_contains "$inactive_text" 'no active issue is set'
assert_equals "$(state_get "$repo_inactive/subdir")" "$inactive_state_before"

repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" branch scratch
state_set "$repo_wrong_branch/subdir" '{"activeIssueNumber":2,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
wrong_branch_output="$TMP_DIR/wrong-branch-output"
if run_flow_capture "$repo_wrong_branch/subdir" "$wrong_branch_output" promote 'msg'; then
	wrong_branch_status=0
else
	wrong_branch_status=$?
fi
wrong_branch_text="$(cat "$wrong_branch_output")"
assert_equals "$wrong_branch_status" '1'
assert_contains "$wrong_branch_text" 'current branch main does not match scratchBranch scratch'

repo_missing_main="$TMP_DIR/repo-missing-main"
init_repo "$repo_missing_main"
git -C "$repo_missing_main" checkout -q -b scratch
state_set "$repo_missing_main/subdir" '{"activeIssueNumber":3,"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":1}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" promote 'msg'; then
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
if run_flow_capture "$repo_missing_scratch/subdir" "$missing_scratch_output" promote 'msg'; then
	missing_scratch_status=0
else
	missing_scratch_status=$?
fi
missing_scratch_text="$(cat "$missing_scratch_output")"
assert_equals "$missing_scratch_status" '1'
assert_contains "$missing_scratch_text" 'Scratch branch does not exist locally: scratch'

repo_same_branch="$TMP_DIR/repo-same-branch"
init_repo "$repo_same_branch"
state_set "$repo_same_branch/subdir" '{"activeIssueNumber":5,"mainBranch":"main","scratchBranch":"main","checkpoint":1}' >/dev/null
same_branch_output="$TMP_DIR/same-branch-output"
if run_flow_capture "$repo_same_branch/subdir" "$same_branch_output" promote 'msg'; then
	same_branch_status=0
else
	same_branch_status=$?
fi
same_branch_text="$(cat "$same_branch_output")"
assert_equals "$same_branch_status" '1'
assert_contains "$same_branch_text" 'mainBranch and scratchBranch must be different'

repo_staged="$TMP_DIR/repo-staged"
init_repo "$repo_staged"
git -C "$repo_staged" checkout -q -b scratch
state_set "$repo_staged/subdir" '{"activeIssueNumber":6,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_staged" scratch.txt 'scratch' 'scratch'
printf 'staged\n' >> "$repo_staged/tracked.txt"
git -C "$repo_staged" add tracked.txt
staged_head_before="$(current_head "$repo_staged")"
staged_state_before="$(state_get "$repo_staged/subdir")"
staged_output="$TMP_DIR/staged-output"
if run_flow_capture "$repo_staged/subdir" "$staged_output" promote 'msg'; then
	staged_status=0
else
	staged_status=$?
fi
staged_text="$(cat "$staged_output")"
assert_equals "$staged_status" '1'
assert_contains "$staged_text" 'repository must be clean'
assert_equals "$(current_head "$repo_staged")" "$staged_head_before"
assert_equals "$(state_get "$repo_staged/subdir")" "$staged_state_before"
assert_equals "$(current_branch "$repo_staged")" 'scratch'

repo_unstaged="$TMP_DIR/repo-unstaged"
init_repo "$repo_unstaged"
git -C "$repo_unstaged" checkout -q -b scratch
state_set "$repo_unstaged/subdir" '{"activeIssueNumber":7,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_unstaged" scratch.txt 'scratch' 'scratch'
printf 'unstaged\n' >> "$repo_unstaged/tracked.txt"
unstaged_head_before="$(current_head "$repo_unstaged")"
unstaged_state_before="$(state_get "$repo_unstaged/subdir")"
unstaged_output="$TMP_DIR/unstaged-output"
if run_flow_capture "$repo_unstaged/subdir" "$unstaged_output" promote 'msg'; then
	unstaged_status=0
else
	unstaged_status=$?
fi
unstaged_text="$(cat "$unstaged_output")"
assert_equals "$unstaged_status" '1'
assert_contains "$unstaged_text" 'repository must be clean'
assert_equals "$(current_head "$repo_unstaged")" "$unstaged_head_before"
assert_equals "$(state_get "$repo_unstaged/subdir")" "$unstaged_state_before"
assert_equals "$(current_branch "$repo_unstaged")" 'scratch'

repo_untracked="$TMP_DIR/repo-untracked"
init_repo "$repo_untracked"
git -C "$repo_untracked" checkout -q -b scratch
state_set "$repo_untracked/subdir" '{"activeIssueNumber":8,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_untracked" scratch.txt 'scratch' 'scratch'
printf 'untracked\n' > "$repo_untracked/new.txt"
untracked_head_before="$(current_head "$repo_untracked")"
untracked_state_before="$(state_get "$repo_untracked/subdir")"
untracked_output="$TMP_DIR/untracked-output"
if run_flow_capture "$repo_untracked/subdir" "$untracked_output" promote 'msg'; then
	untracked_status=0
else
	untracked_status=$?
fi
untracked_text="$(cat "$untracked_output")"
assert_equals "$untracked_status" '1'
assert_contains "$untracked_text" 'repository must be clean'
assert_equals "$(current_head "$repo_untracked")" "$untracked_head_before"
assert_equals "$(state_get "$repo_untracked/subdir")" "$untracked_state_before"
assert_equals "$(current_branch "$repo_untracked")" 'scratch'

repo_equal="$TMP_DIR/repo-equal"
init_repo "$repo_equal"
git -C "$repo_equal" checkout -q -b scratch
state_set "$repo_equal/subdir" '{"activeIssueNumber":9,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
assert_equals "$(repo_status_porcelain "$repo_equal")" ''
equal_output="$TMP_DIR/equal-output"
if run_flow_capture "$repo_equal/subdir" "$equal_output" promote 'msg'; then
	equal_status=0
else
	equal_status=$?
fi
equal_text="$(cat "$equal_output")"
assert_equals "$equal_status" '1'
assert_contains "$equal_text" 'is equal to main'

repo_behind="$TMP_DIR/repo-behind"
init_repo "$repo_behind"
git -C "$repo_behind" checkout -q -b scratch
state_set "$repo_behind/subdir" '{"activeIssueNumber":10,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
git -C "$repo_behind" checkout -q main
create_commit_on_current_branch "$repo_behind" behind.txt 'behind' 'behind'
git -C "$repo_behind" checkout -q scratch
assert_equals "$(repo_status_porcelain "$repo_behind")" ''
behind_output="$TMP_DIR/behind-output"
if run_flow_capture "$repo_behind/subdir" "$behind_output" promote 'msg'; then
	behind_status=0
else
	behind_status=$?
fi
behind_text="$(cat "$behind_output")"
assert_equals "$behind_status" '1'
assert_contains "$behind_text" 'is behind main'

repo_diverged="$TMP_DIR/repo-diverged"
init_repo "$repo_diverged"
git -C "$repo_diverged" checkout -q -b scratch
state_set "$repo_diverged/subdir" '{"activeIssueNumber":11,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
create_commit_on_current_branch "$repo_diverged" scratch.txt 'scratch' 'scratch'
git -C "$repo_diverged" checkout -q main
create_commit_on_current_branch "$repo_diverged" main.txt 'main' 'main'
git -C "$repo_diverged" checkout -q scratch
assert_equals "$(repo_status_porcelain "$repo_diverged")" ''
diverged_output="$TMP_DIR/diverged-output"
if run_flow_capture "$repo_diverged/subdir" "$diverged_output" promote 'msg'; then
	diverged_status=0
else
	diverged_status=$?
fi
diverged_text="$(cat "$diverged_output")"
assert_equals "$diverged_status" '1'
assert_contains "$diverged_text" 'have diverged'

repo_single="$TMP_DIR/repo-single"
init_repo "$repo_single"
git -C "$repo_single" checkout -q -b scratch
state_set "$repo_single/subdir" '{"activeIssueNumber":21,"mainBranch":"main","scratchBranch":"scratch","checkpoint":1}' >/dev/null
create_commit_on_current_branch "$repo_single" one.txt 'one' '1'
main_before_single="$(branch_head "$repo_single" main)"
scratch_tree_before_single="$(branch_tree "$repo_single" scratch)"
single_output="$TMP_DIR/single-output"
if run_flow_capture "$repo_single/subdir" "$single_output" promote 'Promote one'; then
	single_status=0
else
	single_status=$?
fi
single_text="$(cat "$single_output")"
assert_equals "$single_status" '0'
new_main_single="$(branch_head "$repo_single" main)"
new_scratch_single="$(branch_head "$repo_single" scratch)"
assert_equals "$new_main_single" "$new_scratch_single"
assert_equals "$(git -C "$repo_single" rev-parse "$new_main_single^")" "$main_before_single"
assert_equals "$(head_message "$repo_single")" 'Promote one'
assert_equals "$(branch_tree "$repo_single" main)" "$scratch_tree_before_single"
assert_equals "$(current_branch "$repo_single")" 'scratch'
assert_repo_clean "$repo_single"
assert_equals "$(state_get "$repo_single")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "activeIssueNumber": 21
}'
assert_contains "$single_text" 'Promoted scratch to main'
assert_contains "$single_text" "commit: $new_main_single"
assert_contains "$single_text" 'checkpoint: 0'
assert_contains "$single_text" 'activeIssueNumber: 21'

repo_multi="$TMP_DIR/repo-multi"
init_repo "$repo_multi"
git -C "$repo_multi" checkout -q -b scratch
state_set "$repo_multi/subdir" '{"activeIssueNumber":22,"activeIssueTitle":"Promotion title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/22","mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
create_commit_on_current_branch "$repo_multi" a.txt 'a' '1'
create_commit_on_current_branch "$repo_multi" b.txt 'b' '2'
create_commit_on_current_branch "$repo_multi" c.txt 'c' '3'
main_before_multi="$(branch_head "$repo_multi" main)"
scratch_tree_before_multi="$(branch_tree "$repo_multi" scratch)"
multi_output="$TMP_DIR/multi-output"
if run_flow_capture "$repo_multi/subdir" "$multi_output" promote 'Ship feature'; then
	multi_status=0
else
	multi_status=$?
fi
multi_text="$(cat "$multi_output")"
assert_equals "$multi_status" '0'
new_main_multi="$(branch_head "$repo_multi" main)"
assert_equals "$(git -C "$repo_multi" rev-list --count "$main_before_multi..$new_main_multi")" '1'
assert_equals "$(head_message "$repo_multi")" 'Ship feature'
assert_equals "$(branch_tree "$repo_multi" main)" "$scratch_tree_before_multi"
assert_equals "$(branch_head "$repo_multi" main)" "$(branch_head "$repo_multi" scratch)"
assert_equals "$(state_get "$repo_multi")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "activeIssueNumber": 22,
  "activeIssueTitle": "Promotion title",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/22"
}'
assert_repo_clean "$repo_multi"
assert_contains "$multi_text" 'Promoted scratch to main'

repo_custom="$TMP_DIR/repo-custom"
init_repo "$repo_custom"
git -C "$repo_custom" branch -m main trunk
git -C "$repo_custom" checkout -q -b sandbox
state_set "$repo_custom/subdir" '{"activeIssueNumber":23,"activeIssueTitle":"Custom","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/23","mainBranch":"trunk","scratchBranch":"sandbox","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_custom" custom.txt 'custom' '1'
custom_output="$TMP_DIR/custom-output"
if run_flow_capture "$repo_custom/subdir" "$custom_output" promote 'Custom ship'; then
	custom_status=0
else
	custom_status=$?
fi
custom_text="$(cat "$custom_output")"
assert_equals "$custom_status" '0'
custom_commit="$(branch_head "$repo_custom" trunk)"
assert_equals "$(branch_head "$repo_custom" sandbox)" "$custom_commit"
assert_contains "$custom_text" 'Promoted sandbox to trunk'
assert_contains "$custom_text" "commit: $custom_commit"
assert_equals "$(state_get "$repo_custom")" $'{
  "mainBranch": "trunk",
  "scratchBranch": "sandbox",
  "checkpoint": 0,
  "activeIssueNumber": 23,
  "activeIssueTitle": "Custom",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/23"
}'

repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
git -C "$repo_subdir" checkout -q -b scratch
state_set "$repo_subdir/subdir" '{"activeIssueNumber":24,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_subdir" subdir.txt 'subdir' '1'
subdir_output="$TMP_DIR/subdir-output"
if run_flow_capture "$repo_subdir/subdir" "$subdir_output" promote 'Subdir ship'; then
	subdir_status=0
else
	subdir_status=$?
fi
subdir_text="$(cat "$subdir_output")"
assert_equals "$subdir_status" '0'
assert_contains "$subdir_text" 'Promoted scratch to main'
assert_equals "$(current_branch "$repo_subdir")" 'scratch'
assert_repo_clean "$repo_subdir"

repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
git -C "$repo_routing" checkout -q -b scratch
routing_output_path="$TMP_DIR/promote-output.txt"
track_config_file "$repo_routing" "$routing_output_path"
state_set "$repo_routing/subdir" '{"activeIssueNumber":25,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_routing" routed.txt 'routed' '1'
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" promote 'Route ship'; then
	routing_status=0
else
	routing_status=$?
fi
routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
assert_equals "$routing_terminal_text" "Output written to $routing_output_path"
routing_file_text="$(cat "$routing_output_path")"
assert_contains "$routing_file_text" 'Promoted scratch to main'
assert_contains "$routing_file_text" 'commit: '
assert_contains "$routing_file_text" 'checkpoint: 0'
assert_contains "$routing_file_text" 'activeIssueNumber: 25'

repo_switch_main_fail="$TMP_DIR/repo-switch-main-fail"
init_repo "$repo_switch_main_fail"
git -C "$repo_switch_main_fail" checkout -q -b scratch
state_set "$repo_switch_main_fail/subdir" '{"activeIssueNumber":26,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_switch_main_fail" switch-main.txt 'switch-main' '1'
touch "$repo_switch_main_fail/.git/index.lock"
switch_main_head_before="$(branch_head "$repo_switch_main_fail" main)"
switch_main_scratch_before="$(branch_head "$repo_switch_main_fail" scratch)"
switch_main_state_before="$(state_get "$repo_switch_main_fail/subdir")"
switch_main_output="$TMP_DIR/switch-main-output"
if run_flow_capture "$repo_switch_main_fail/subdir" "$switch_main_output" promote 'Switch main fail'; then
	switch_main_status=0
else
	switch_main_status=$?
fi
rm -f "$repo_switch_main_fail/.git/index.lock"
switch_main_text="$(cat "$switch_main_output")"
assert_equals "$switch_main_status" '1'
assert_contains "$switch_main_text" 'failed to switch to main'
assert_equals "$(branch_head "$repo_switch_main_fail" main)" "$switch_main_head_before"
assert_equals "$(branch_head "$repo_switch_main_fail" scratch)" "$switch_main_scratch_before"
assert_equals "$(state_get "$repo_switch_main_fail/subdir")" "$switch_main_state_before"

repo_squash_fail="$TMP_DIR/repo-squash-fail"
init_repo "$repo_squash_fail"
git -C "$repo_squash_fail" checkout -q -b scratch
state_set "$repo_squash_fail/subdir" '{"activeIssueNumber":27,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_squash_fail" squash.txt 'squash' '1'
cat >"$repo_squash_fail/.git/hooks/post-checkout" <<'EOF'
#!/usr/bin/env bash
repo_root="$(git rev-parse --show-toplevel)"
touch "$repo_root/.git/index.lock"
exit 0
EOF
chmod +x "$repo_squash_fail/.git/hooks/post-checkout"
squash_main_before="$(branch_head "$repo_squash_fail" main)"
squash_scratch_before="$(branch_head "$repo_squash_fail" scratch)"
squash_state_before="$(state_get "$repo_squash_fail/subdir")"
squash_output="$TMP_DIR/squash-output"
if run_flow_capture "$repo_squash_fail/subdir" "$squash_output" promote 'Squash fail'; then
	squash_status=0
else
	squash_status=$?
fi
rm -f "$repo_squash_fail/.git/index.lock"
rm -f "$repo_squash_fail/.git/hooks/post-checkout"
squash_text="$(cat "$squash_output")"
assert_equals "$squash_status" '1'
assert_contains "$squash_text" 'squash merge failed'
assert_equals "$(branch_head "$repo_squash_fail" main)" "$squash_main_before"
assert_equals "$(branch_head "$repo_squash_fail" scratch)" "$squash_scratch_before"
assert_equals "$(state_get "$repo_squash_fail")" "$squash_state_before"

repo_commit_fail="$TMP_DIR/repo-commit-fail"
init_repo "$repo_commit_fail"
git -C "$repo_commit_fail" checkout -q -b scratch
state_set "$repo_commit_fail/subdir" '{"activeIssueNumber":28,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_commit_fail" commit.txt 'commit' '1'
cat >"$repo_commit_fail/.git/hooks/pre-commit" <<'EOF'
#!/usr/bin/env bash
printf 'hook failure\n' >&2
exit 1
EOF
chmod +x "$repo_commit_fail/.git/hooks/pre-commit"
commit_main_before="$(branch_head "$repo_commit_fail" main)"
commit_scratch_before="$(branch_head "$repo_commit_fail" scratch)"
commit_state_before="$(state_get "$repo_commit_fail/subdir")"
commit_output="$TMP_DIR/commit-output"
if run_flow_capture "$repo_commit_fail/subdir" "$commit_output" promote 'Commit fail'; then
	commit_status=0
else
	commit_status=$?
fi
commit_text="$(cat "$commit_output")"
assert_equals "$commit_status" '1'
assert_contains "$commit_text" 'hook failure'
assert_contains "$commit_text" 'Git commit failed'
assert_equals "$(branch_head "$repo_commit_fail" main)" "$commit_main_before"
assert_equals "$(branch_head "$repo_commit_fail" scratch)" "$commit_scratch_before"
assert_equals "$(state_get "$repo_commit_fail")" "$commit_state_before"

repo_switch_back_fail="$TMP_DIR/repo-switch-back-fail"
init_repo "$repo_switch_back_fail"
git -C "$repo_switch_back_fail" checkout -q -b scratch
state_set "$repo_switch_back_fail/subdir" '{"activeIssueNumber":29,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_switch_back_fail" switch-back.txt 'switch-back' '1'
cat >"$repo_switch_back_fail/.git/hooks/post-commit" <<'EOF'
#!/usr/bin/env bash
repo_root="$(git rev-parse --show-toplevel)"
touch "$repo_root/.git/index.lock"
exit 0
EOF
chmod +x "$repo_switch_back_fail/.git/hooks/post-commit"
switch_back_main_before="$(branch_head "$repo_switch_back_fail" main)"
switch_back_scratch_before="$(branch_head "$repo_switch_back_fail" scratch)"
switch_back_state_before="$(state_get "$repo_switch_back_fail/subdir")"
switch_back_output="$TMP_DIR/switch-back-output"
if run_flow_capture "$repo_switch_back_fail/subdir" "$switch_back_output" promote 'Switch back fail'; then
	switch_back_status=0
else
	switch_back_status=$?
fi
rm -f "$repo_switch_back_fail/.git/index.lock"
rm -f "$repo_switch_back_fail/.git/hooks/post-commit"
switch_back_text="$(cat "$switch_back_output")"
assert_equals "$switch_back_status" '1'
assert_contains "$switch_back_text" 'failed to switch back to scratch'
assert_not_equals "$(branch_head "$repo_switch_back_fail" main)" "$switch_back_main_before"
assert_equals "$(branch_head "$repo_switch_back_fail" scratch)" "$switch_back_scratch_before"
assert_equals "$(state_get "$repo_switch_back_fail")" "$switch_back_state_before"

repo_reset_fail="$TMP_DIR/repo-reset-fail"
init_repo "$repo_reset_fail"
git -C "$repo_reset_fail" checkout -q -b scratch
state_set "$repo_reset_fail/subdir" '{"activeIssueNumber":30,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
create_commit_on_current_branch "$repo_reset_fail" reset.txt 'reset' '1'
cat >"$repo_reset_fail/.git/hooks/post-checkout" <<'EOF'
#!/usr/bin/env bash
old_sha="$1"
new_sha="$2"
flag="$3"
repo_root="$(git rev-parse --show-toplevel)"
if [[ "$flag" == '1' && "$(git branch --show-current)" == 'scratch' ]]; then
	touch "$repo_root/.git/index.lock"
fi
exit 0
EOF
chmod +x "$repo_reset_fail/.git/hooks/post-checkout"
reset_main_before="$(branch_head "$repo_reset_fail" main)"
reset_scratch_before="$(branch_head "$repo_reset_fail" scratch)"
reset_state_before="$(state_get "$repo_reset_fail/subdir")"
reset_output="$TMP_DIR/reset-output"
if run_flow_capture "$repo_reset_fail/subdir" "$reset_output" promote 'Reset fail'; then
	reset_status=0
else
	reset_status=$?
fi
rm -f "$repo_reset_fail/.git/index.lock"
rm -f "$repo_reset_fail/.git/hooks/post-checkout"
reset_text="$(cat "$reset_output")"
assert_equals "$reset_status" '1'
assert_contains "$reset_text" 'failed to reset scratch to promoted commit'
assert_not_equals "$(branch_head "$repo_reset_fail" main)" "$reset_main_before"
assert_equals "$(branch_head "$repo_reset_fail" scratch)" "$reset_scratch_before"
assert_equals "$(current_branch "$repo_reset_fail")" 'scratch'
assert_equals "$(state_get "$repo_reset_fail")" "$reset_state_before"

if [[ "$(id -u)" != '0' ]]; then
	repo_state_fail="$TMP_DIR/repo-state-fail"
	init_repo "$repo_state_fail"
	git -C "$repo_state_fail" checkout -q -b scratch
	state_set "$repo_state_fail/subdir" '{"activeIssueNumber":31,"activeIssueTitle":"Persist title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/31","mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
	create_commit_on_current_branch "$repo_state_fail" state.txt 'state' '1'
	state_fail_before="$(cat "$repo_state_fail/.ai-dev/workflow.json")"
	chmod 500 "$repo_state_fail/.ai-dev"
	state_fail_output="$TMP_DIR/state-fail-output"
	if run_flow_capture "$repo_state_fail/subdir" "$state_fail_output" promote 'State fail'; then
		state_fail_status=0
	else
		state_fail_status=$?
	fi
	chmod 700 "$repo_state_fail/.ai-dev"
	state_fail_text="$(cat "$state_fail_output")"
	assert_equals "$state_fail_status" '1'
	assert_contains "$state_fail_text" 'Cannot write workflow state to'
	assert_not_contains "$state_fail_text" 'Traceback'
	assert_equals "$(branch_head "$repo_state_fail" main)" "$(branch_head "$repo_state_fail" scratch)"
	assert_equals "$(current_branch "$repo_state_fail")" 'scratch'
	assert_repo_clean "$repo_state_fail"
	assert_equals "$(cat "$repo_state_fail/.ai-dev/workflow.json")" "$state_fail_before"
	assert_equals "$(state_get "$repo_state_fail")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 2,
  "activeIssueNumber": 31,
  "activeIssueTitle": "Persist title",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/31"
}'
fi

printf 'flow promote tests passed\n'
