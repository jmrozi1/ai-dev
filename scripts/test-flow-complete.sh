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
		PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_LOG="$gh_log_file" "$FLOW" "$@"
	)
}

make_gh_mock() {
	local mock_bin_dir="$1"
	mkdir -p "$mock_bin_dir"
	cat >"$mock_bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

python3 - "$@" <<'PY'
import json
import os
import sys
from pathlib import Path


def fail(message):
	print(message, file=sys.stderr)
	sys.exit(1)


def load_state(path):
	if not path.exists():
		return {'issues': {}}
	return json.loads(path.read_text(encoding='utf-8'))


def save_state(path, data):
	path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


state_path_value = os.environ.get('GH_MOCK_STATE')
if not state_path_value:
	fail('GH_MOCK_STATE is required')
state_path = Path(state_path_value)
state_path.parent.mkdir(parents=True, exist_ok=True)

state = load_state(state_path)
if 'issues' not in state or not isinstance(state['issues'], dict):
	state = {'issues': {}}

log_path_value = os.environ.get('GH_MOCK_LOG', '')

args = sys.argv[1:]
if len(args) < 3 or args[0] != 'issue':
	fail('unsupported command')

command = args[1]
issue_number = args[2]

if command == 'close':
	if os.environ.get('GH_MOCK_FAIL_CLOSE') == '1':
		fail('mock close failure')
	if issue_number not in state['issues']:
		fail('issue not found')
	state['issues'][issue_number]['state'] = 'closed'
	save_state(state_path, state)
	print(f'closed {issue_number}')
	if log_path_value:
		with open(log_path_value, 'a', encoding='utf-8') as log_handle:
			log_handle.write(f"close:{issue_number}\\n")
	sys.exit(0)

fail('unsupported issue subcommand')
PY
EOF
	chmod +x "$mock_bin_dir/gh"
}

write_gh_state() {
	local state_path="$1"
	local payload="$2"
	cat >"$state_path" <<EOF
$payload
EOF
}

read_gh_issue_state() {
	local state_path="$1"
	local issue_number="$2"
	python3 - "$state_path" "$issue_number" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], 'r', encoding='utf-8').read())
issue = state.get('issues', {}).get(sys.argv[2], {})
issue_state = issue.get('state', '')
if not isinstance(issue_state, str):
	issue_state = ''
print(issue_state)
PY
}

make_no_gh_bin() {
	local no_gh_bin="$1"
	mkdir -p "$no_gh_bin"
	ln -sf "$(command -v bash)" "$no_gh_bin/bash"
	ln -sf "$(command -v basename)" "$no_gh_bin/basename"
	ln -sf "$(command -v git)" "$no_gh_bin/git"
	ln -sf "$(command -v python3)" "$no_gh_bin/python3"
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

mock_bin_dir="$TMP_DIR/mock-bin"
make_gh_mock "$mock_bin_dir"
gh_state_file="$TMP_DIR/gh-state.json"
gh_log_file="$TMP_DIR/gh.log"
no_gh_bin="$TMP_DIR/no-gh-bin"
make_no_gh_bin "$no_gh_bin"

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
missing_output="$TMP_DIR/missing-output"
if run_flow_capture "$repo_args/subdir" "$missing_output" complete; then
	missing_status=0
else
	missing_status=$?
fi
missing_text="$(cat "$missing_output")"
assert_equals "$missing_status" '1'
assert_contains "$missing_text" 'Cannot complete workflow: no active issue is set.'

extra_output="$TMP_DIR/extra-output"
if run_flow_capture "$repo_args/subdir" "$extra_output" complete extra; then
	extra_status=0
else
	extra_status=$?
fi
extra_text="$(cat "$extra_output")"
assert_equals "$extra_status" '1'
assert_contains "$extra_text" 'Usage: flow complete'

empty_extra_output="$TMP_DIR/empty-extra-output"
if run_flow_capture "$repo_args/subdir" "$empty_extra_output" complete ''; then
	empty_extra_status=0
else
	empty_extra_status=$?
fi
empty_extra_text="$(cat "$empty_extra_output")"
assert_equals "$empty_extra_status" '1'
assert_contains "$empty_extra_text" 'Usage: flow complete'

outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output" complete; then
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
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" complete; then
	inactive_status=0
else
	inactive_status=$?
fi
inactive_text="$(cat "$inactive_output")"
assert_equals "$inactive_status" '1'
assert_contains "$inactive_text" 'no active issue is set'

repo_wrong_branch="$TMP_DIR/repo-wrong-branch"
init_repo "$repo_wrong_branch"
git -C "$repo_wrong_branch" branch scratch
state_set "$repo_wrong_branch/subdir" '{"activeIssueNumber":2,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
wrong_branch_output="$TMP_DIR/wrong-branch-output"
if run_flow_capture "$repo_wrong_branch/subdir" "$wrong_branch_output" complete; then
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
state_set "$repo_missing_main/subdir" '{"activeIssueNumber":3,"mainBranch":"trunk","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_main_output="$TMP_DIR/missing-main-output"
if run_flow_capture "$repo_missing_main/subdir" "$missing_main_output" complete; then
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
if run_flow_capture "$repo_missing_scratch/subdir" "$missing_scratch_output" complete; then
	missing_scratch_status=0
else
	missing_scratch_status=$?
fi
missing_scratch_text="$(cat "$missing_scratch_output")"
assert_equals "$missing_scratch_status" '1'
assert_contains "$missing_scratch_text" 'Scratch branch does not exist locally: scratch'

repo_same_branch="$TMP_DIR/repo-same-branch"
init_repo "$repo_same_branch"
state_set "$repo_same_branch/subdir" '{"activeIssueNumber":5,"mainBranch":"main","scratchBranch":"main","checkpoint":0}' >/dev/null
same_branch_output="$TMP_DIR/same-branch-output"
if run_flow_capture "$repo_same_branch/subdir" "$same_branch_output" complete; then
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
state_set "$repo_staged/subdir" '{"activeIssueNumber":6,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'staged\n' >> "$repo_staged/tracked.txt"
git -C "$repo_staged" add tracked.txt
staged_output="$TMP_DIR/staged-output"
if run_flow_capture "$repo_staged/subdir" "$staged_output" complete; then
	staged_status=0
else
	staged_status=$?
fi
staged_text="$(cat "$staged_output")"
assert_equals "$staged_status" '1'
assert_contains "$staged_text" 'repository must be clean'

repo_unstaged="$TMP_DIR/repo-unstaged"
init_repo "$repo_unstaged"
git -C "$repo_unstaged" checkout -q -b scratch
state_set "$repo_unstaged/subdir" '{"activeIssueNumber":7,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'unstaged\n' >> "$repo_unstaged/tracked.txt"
unstaged_output="$TMP_DIR/unstaged-output"
if run_flow_capture "$repo_unstaged/subdir" "$unstaged_output" complete; then
	unstaged_status=0
else
	unstaged_status=$?
fi
unstaged_text="$(cat "$unstaged_output")"
assert_equals "$unstaged_status" '1'
assert_contains "$unstaged_text" 'repository must be clean'

repo_untracked="$TMP_DIR/repo-untracked"
init_repo "$repo_untracked"
git -C "$repo_untracked" checkout -q -b scratch
state_set "$repo_untracked/subdir" '{"activeIssueNumber":8,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'untracked\n' > "$repo_untracked/new.txt"
untracked_output="$TMP_DIR/untracked-output"
if run_flow_capture "$repo_untracked/subdir" "$untracked_output" complete; then
	untracked_status=0
else
	untracked_status=$?
fi
untracked_text="$(cat "$untracked_output")"
assert_equals "$untracked_status" '1'
assert_contains "$untracked_text" 'repository must be clean'

repo_ignored="$TMP_DIR/repo-ignored"
init_repo "$repo_ignored"
git -C "$repo_ignored" checkout -q -b scratch
state_set "$repo_ignored/subdir" '{"activeIssueNumber":9,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
	"issues": {
		"9": {
			"state": "open"
		}
	}
}'
ignored_output="$TMP_DIR/ignored-output"
if run_flow_capture "$repo_ignored/subdir" "$ignored_output" complete; then
	ignored_status=0
else
	ignored_status=$?
fi
ignored_text="$(cat "$ignored_output")"
assert_equals "$ignored_status" '0'
assert_contains "$ignored_text" 'Completed issue 9'
assert_equals "$(read_gh_issue_state "$gh_state_file" '9')" 'closed'

repo_ahead="$TMP_DIR/repo-ahead"
init_repo "$repo_ahead"
git -C "$repo_ahead" checkout -q -b scratch
state_set "$repo_ahead/subdir" '{"activeIssueNumber":10,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
create_commit_on_current_branch "$repo_ahead" ahead.txt 'ahead' 'ahead'
ahead_output="$TMP_DIR/ahead-output"
if run_flow_capture "$repo_ahead/subdir" "$ahead_output" complete; then
	ahead_status=0
else
	ahead_status=$?
fi
ahead_text="$(cat "$ahead_output")"
assert_equals "$ahead_status" '1'
assert_contains "$ahead_text" 'is ahead of main'

repo_behind="$TMP_DIR/repo-behind"
init_repo "$repo_behind"
git -C "$repo_behind" checkout -q -b scratch
state_set "$repo_behind/subdir" '{"activeIssueNumber":11,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
git -C "$repo_behind" checkout -q main
create_commit_on_current_branch "$repo_behind" behind.txt 'behind' 'behind'
git -C "$repo_behind" checkout -q scratch
behind_output="$TMP_DIR/behind-output"
if run_flow_capture "$repo_behind/subdir" "$behind_output" complete; then
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
state_set "$repo_diverged/subdir" '{"activeIssueNumber":12,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
create_commit_on_current_branch "$repo_diverged" scratch.txt 'scratch' 'scratch'
git -C "$repo_diverged" checkout -q main
create_commit_on_current_branch "$repo_diverged" main.txt 'main' 'main'
git -C "$repo_diverged" checkout -q scratch
diverged_output="$TMP_DIR/diverged-output"
if run_flow_capture "$repo_diverged/subdir" "$diverged_output" complete; then
	diverged_status=0
else
	diverged_status=$?
fi
diverged_text="$(cat "$diverged_output")"
assert_equals "$diverged_status" '1'
assert_contains "$diverged_text" 'have diverged'

repo_checkpoint="$TMP_DIR/repo-checkpoint"
init_repo "$repo_checkpoint"
git -C "$repo_checkpoint" checkout -q -b scratch
state_set "$repo_checkpoint/subdir" '{"activeIssueNumber":13,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
checkpoint_output="$TMP_DIR/checkpoint-output"
if run_flow_capture "$repo_checkpoint/subdir" "$checkpoint_output" complete; then
	checkpoint_status=0
else
	checkpoint_status=$?
fi
checkpoint_text="$(cat "$checkpoint_output")"
assert_equals "$checkpoint_status" '1'
assert_contains "$checkpoint_text" 'checkpoint must be 0'

repo_success="$TMP_DIR/repo-success"
init_repo "$repo_success"
success_routed_output_path="$TMP_DIR/success-out.txt"
track_config_file "$repo_success" "$success_routed_output_path"
git -C "$repo_success" checkout -q -b scratch
state_set "$repo_success/subdir" '{"activeIssueNumber":21,"activeIssueTitle":"Done title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/21","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
	"issues": {
		"21": {
			"state": "open"
		}
	}
}'
success_branch_before="$(current_branch "$repo_success")"
success_head_before="$(current_head "$repo_success")"
success_main_before="$(branch_head "$repo_success" main)"
success_scratch_before="$(branch_head "$repo_success" scratch)"
success_index_before="$(cached_diff "$repo_success")"
success_worktree_before="$(worktree_diff "$repo_success")"
success_output="$TMP_DIR/success-output"
if run_flow_capture "$repo_success/subdir" "$success_output" complete; then
	success_status=0
else
	success_status=$?
fi
success_text="$(cat "$success_output")"
assert_equals "$success_status" '0'
assert_equals "$success_text" "Output written to $success_routed_output_path"
assert_equals "$(cat "$success_routed_output_path")" $'Completed issue 21\nWorkflow: inactive\nmainBranch: main\nscratchBranch: scratch\ncheckpoint: 0'
assert_equals "$(state_get "$repo_success")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'
expected_success_config="$(printf '{\n  "out": "%s"\n}' "$success_routed_output_path")"
assert_equals "$(cat "$repo_success/.ai-dev/config.json")" "$expected_success_config"
assert_equals "$(current_branch "$repo_success")" "$success_branch_before"
assert_equals "$(current_head "$repo_success")" "$success_head_before"
assert_equals "$(branch_head "$repo_success" main)" "$success_main_before"
assert_equals "$(branch_head "$repo_success" scratch)" "$success_scratch_before"
assert_equals "$(cached_diff "$repo_success")" "$success_index_before"
assert_equals "$(worktree_diff "$repo_success")" "$success_worktree_before"
assert_repo_clean "$repo_success"
assert_equals "$(read_gh_issue_state "$gh_state_file" '21')" 'closed'

repo_close_fail="$TMP_DIR/repo-close-fail"
init_repo "$repo_close_fail"
git -C "$repo_close_fail" checkout -q -b scratch
state_set "$repo_close_fail/subdir" '{"activeIssueNumber":26,"activeIssueTitle":"Fail close","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/26","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
  "issues": {
    "26": {
      "state": "open"
    }
  }
}'
close_fail_state_before="$(state_get "$repo_close_fail")"
close_fail_output="$TMP_DIR/close-fail-output"
if (
	cd "$repo_close_fail/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_LOG="$gh_log_file" GH_MOCK_FAIL_CLOSE=1 "$FLOW" complete
) >"$close_fail_output" 2>&1; then
	close_fail_status=0
else
	close_fail_status=$?
fi
close_fail_text="$(cat "$close_fail_output")"
assert_equals "$close_fail_status" '1'
assert_contains "$close_fail_text" 'failed to close GitHub issue 26'
assert_equals "$(state_get "$repo_close_fail")" "$close_fail_state_before"
assert_equals "$(read_gh_issue_state "$gh_state_file" '26')" 'open'

repo_missing_gh="$TMP_DIR/repo-missing-gh"
init_repo "$repo_missing_gh"
git -C "$repo_missing_gh" checkout -q -b scratch
state_set "$repo_missing_gh/subdir" '{"activeIssueNumber":27,"activeIssueTitle":"No gh","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/27","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
missing_gh_state_before="$(state_get "$repo_missing_gh")"
missing_gh_output="$TMP_DIR/missing-gh-output"
if (
	cd "$repo_missing_gh/subdir"
	PATH="$no_gh_bin" "$FLOW" complete
) >"$missing_gh_output" 2>&1; then
	missing_gh_status=0
else
	missing_gh_status=$?
fi
missing_gh_text="$(cat "$missing_gh_output")"
assert_equals "$missing_gh_status" '1'
assert_contains "$missing_gh_text" 'GitHub CLI (gh) is required for this command.'
assert_equals "$(state_get "$repo_missing_gh")" "$missing_gh_state_before"

repo_patch_complete="$TMP_DIR/repo-patch-complete"
init_repo "$repo_patch_complete"
git -C "$repo_patch_complete" checkout -q -b scratch
state_set "$repo_patch_complete/subdir" '{"patchDescription":"Local patch","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
patch_complete_output="$TMP_DIR/patch-complete-output"
if (
	cd "$repo_patch_complete/subdir"
	PATH="$no_gh_bin" "$FLOW" complete
) >"$patch_complete_output" 2>&1; then
	patch_complete_status=0
else
	patch_complete_status=$?
fi
patch_complete_text="$(cat "$patch_complete_output")"
assert_equals "$patch_complete_status" '0'
assert_contains "$patch_complete_text" 'Completed patch: Local patch'
assert_contains "$patch_complete_text" 'Workflow: inactive'
assert_equals "$(state_get "$repo_patch_complete")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'

repo_custom="$TMP_DIR/repo-custom"
init_repo "$repo_custom"
git -C "$repo_custom" branch -m main trunk
git -C "$repo_custom" checkout -q -b sandbox
state_set "$repo_custom/subdir" '{"activeIssueNumber":22,"activeIssueTitle":"Custom","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/22","mainBranch":"trunk","scratchBranch":"sandbox","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
  "issues": {
    "22": {
      "state": "open"
    }
  }
}'
custom_output="$TMP_DIR/custom-output"
if run_flow_capture "$repo_custom/subdir" "$custom_output" complete; then
	custom_status=0
else
	custom_status=$?
fi
custom_text="$(cat "$custom_output")"
assert_equals "$custom_status" '0'
assert_equals "$custom_text" $'Completed issue 22\nWorkflow: inactive\nmainBranch: trunk\nscratchBranch: sandbox\ncheckpoint: 0'
assert_equals "$(state_get "$repo_custom")" $'{
  "mainBranch": "trunk",
  "scratchBranch": "sandbox",
  "checkpoint": 0
}'
assert_equals "$(read_gh_issue_state "$gh_state_file" '22')" 'closed'

repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
git -C "$repo_subdir" checkout -q -b scratch
state_set "$repo_subdir/subdir" '{"activeIssueNumber":23,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
	"issues": {
		"23": {
			"state": "open"
		}
	}
}'
subdir_output="$TMP_DIR/subdir-output"
if run_flow_capture "$repo_subdir/subdir" "$subdir_output" complete; then
	subdir_status=0
else
	subdir_status=$?
fi
subdir_text="$(cat "$subdir_output")"
assert_equals "$subdir_status" '0'
assert_contains "$subdir_text" 'Completed issue 23'
assert_equals "$(read_gh_issue_state "$gh_state_file" '23')" 'closed'

repo_routing="$TMP_DIR/repo-routing"
init_repo "$repo_routing"
track_config_file "$repo_routing" "$TMP_DIR/complete-output.txt"
git -C "$repo_routing" checkout -q -b scratch
state_set "$repo_routing/subdir" '{"activeIssueNumber":24,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
	"issues": {
		"24": {
			"state": "open"
		}
	}
}'
routing_terminal_output="$TMP_DIR/routing-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$routing_terminal_output" complete; then
	routing_status=0
else
	routing_status=$?
fi
routing_terminal_text="$(cat "$routing_terminal_output")"
assert_equals "$routing_status" '0'
routing_file_text="$(cat "$TMP_DIR/complete-output.txt")"
assert_equals "$routing_terminal_text" "Output written to $TMP_DIR/complete-output.txt"
assert_contains "$routing_file_text" 'Completed issue 24'
assert_contains "$routing_file_text" 'Workflow: inactive'
assert_contains "$routing_file_text" 'checkpoint: 0'
assert_equals "$(read_gh_issue_state "$gh_state_file" '24')" 'closed'

if [[ "$(id -u)" != '0' ]]; then
	repo_state_fail="$TMP_DIR/repo-state-fail"
	init_repo "$repo_state_fail"
	git -C "$repo_state_fail" checkout -q -b scratch
	state_set "$repo_state_fail/subdir" '{"activeIssueNumber":25,"activeIssueTitle":"Persist title","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/25","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
	write_gh_state "$gh_state_file" '{
	  "issues": {
	    "25": {
	      "state": "open"
	    }
	  }
	}'
	state_before="$(cat "$repo_state_fail/.ai-dev/workflow.json")"
	head_before="$(current_head "$repo_state_fail")"
	main_before="$(branch_head "$repo_state_fail" main)"
	scratch_before="$(branch_head "$repo_state_fail" scratch)"
	branch_before="$(current_branch "$repo_state_fail")"
	index_before="$(cached_diff "$repo_state_fail")"
	worktree_before="$(worktree_diff "$repo_state_fail")"
	chmod 500 "$repo_state_fail/.ai-dev"
	state_fail_output="$TMP_DIR/state-fail-output"
	if run_flow_capture "$repo_state_fail/subdir" "$state_fail_output" complete; then
		state_fail_status=0
	else
		state_fail_status=$?
	fi
	chmod 700 "$repo_state_fail/.ai-dev"
	state_fail_text="$(cat "$state_fail_output")"
	assert_equals "$state_fail_status" '1'
	assert_contains "$state_fail_text" 'Cannot write workflow state to'
	assert_not_contains "$state_fail_text" 'Traceback'
	assert_equals "$(cat "$repo_state_fail/.ai-dev/workflow.json")" "$state_before"
	assert_equals "$(current_head "$repo_state_fail")" "$head_before"
	assert_equals "$(branch_head "$repo_state_fail" main)" "$main_before"
	assert_equals "$(branch_head "$repo_state_fail" scratch)" "$scratch_before"
	assert_equals "$(current_branch "$repo_state_fail")" "$branch_before"
	assert_equals "$(cached_diff "$repo_state_fail")" "$index_before"
	assert_equals "$(worktree_diff "$repo_state_fail")" "$worktree_before"
	assert_equals "$(state_get "$repo_state_fail")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0,
  "activeIssueNumber": 25,
  "activeIssueTitle": "Persist title",
  "activeIssueUrl": "https://github.com/jmrozi1/ai-dev/issues/25"
}'
fi

printf 'flow complete tests passed\n'
