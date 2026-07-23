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
		git config user.name 'Flow Help Tests'
		git config user.email 'flow-help-tests@example.com'
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

expected_top_help() {
	local command_name="$1"
	cat <<EOF
Usage: ${command_name} <command> [options]

Manage an issue-focused development workflow using permanent main history
and disposable scratch checkpoints.

Commands:
  start      Begin work on an issue and reset scratch from main.
	patch      Begin or adopt a local patch workflow on scratch.
  status     Show the active issue and current repository state.
  review     Generate the cumulative change package for review.
  commit     Create the next numbered checkpoint on scratch.
  reset      Discard scratch work and restore it from main.
  promote    Squash scratch into one permanent commit on main.
  complete   Clear the completed local workflow.
	block      Block the active issue workflow and release the active slot.
	resume     Resume a previously blocked issue workflow.
  get        Read a repository setting.
  set        Change a repository setting.
  unset      Remove a repository setting.
  help       Show this help.

Run \`${command_name} <command> --help\` for command-specific help.
EOF
}

expected_command_help() {
	local command_name="$1"
	local command="$2"

	case "$command" in
		start)
			cat <<EOF
Usage: ${command_name} start <issue-number>

Begin work on an issue by resetting scratch to main, checking out scratch,
and recording the active issue.

Options:
  -h, --help  Show this help.
EOF
			;;
		patch)
			cat <<EOF
Usage: ${command_name} patch "<description>"
       ${command_name} patch --adopt "<description>"

Start a local patch workflow for small, self-contained changes, or adopt
existing scratch work without changing commits, index, or working tree.

Options:
  --adopt      Adopt existing work on scratch and preserve repository state.
  -h, --help   Show this help.
EOF
			;;
		status)
			cat <<EOF
Usage: ${command_name} status [-v|--verbose]

Show the active issue, current branch, and repository deviations.

Options:
  -v, --verbose  Show complete workflow and Git details.
  -h, --help     Show this help.
EOF
			;;
		review)
			cat <<EOF
Usage: ${command_name} review

Generate a cumulative review package for all scratch and working-tree
changes relative to main.

Options:
  -h, --help  Show this help.
EOF
			;;
		commit)
			cat <<EOF
Usage: ${command_name} commit

Create the next numbered checkpoint commit on scratch.

Options:
  -h, --help  Show this help.
EOF
			;;
		reset)
			cat <<EOF
Usage: ${command_name} reset

Discard scratch commits and working-tree changes by resetting scratch to
main while preserving the active issue.

Options:
  -h, --help  Show this help.
EOF
			;;
		promote)
			cat <<EOF
Usage: ${command_name} promote "<commit-message>"

Squash the complete scratch change into one permanent commit on main, then
reset scratch to the promoted main commit.

Options:
  -h, --help  Show this help.
EOF
			;;
		complete)
			cat <<EOF
Usage: ${command_name} complete

Clear the active local workflow after scratch and main are synchronized.

Options:
  -h, --help  Show this help.
EOF
			;;
		block)
			cat <<EOF
Usage: ${command_name} block "<reason>"

Block an active issue workflow, keep the issue open, and release the
local active workflow slot.

Options:
  -h, --help  Show this help.
EOF
			;;
		resume)
			cat <<EOF
Usage: ${command_name} resume <ticket-number>

Resume a blocked issue workflow and restore it as the local active issue.

Options:
  -h, --help  Show this help.
EOF
			;;
		get)
			cat <<EOF
Usage: ${command_name} get out

Show the configured operational output destination.

Options:
  -h, --help  Show this help.
EOF
			;;
		set)
			cat <<EOF
Usage: ${command_name} set out=<path>

Configure operational command output to replace the specified file.

Options:
  -h, --help  Show this help.
EOF
			;;
		unset)
			cat <<EOF
Usage: ${command_name} unset out

Remove the configured operational output destination.

Options:
  -h, --help  Show this help.
EOF
			;;
		help)
			cat <<EOF
Usage: ${command_name} help

Show top-level command help.

Options:
  -h, --help  Show this help.
EOF
			;;
	esac
}
# top-level help is identical across all entrypoints
help_repo="$TMP_DIR/repo-help"
init_repo "$help_repo"
symlink_path="$TMP_DIR/ai-dev-flow"
ln -s "$FLOW" "$symlink_path"

top_help_flow="$(run_flow "$help_repo/subdir" help)"
assert_equals "$top_help_flow" "$(run_flow "$help_repo/subdir" )"
assert_equals "$top_help_flow" "$(run_flow "$help_repo/subdir" -h)"
assert_equals "$top_help_flow" "$(run_flow "$help_repo/subdir" --help)"
assert_equals "$top_help_flow" "$(expected_top_help flow)"
assert_contains "$top_help_flow" 'status     Show the active issue and current repository state.'
assert_contains "$top_help_flow" 'help       Show this help.'

top_help_symlink="$(cd "$help_repo/subdir" && "$symlink_path")"
assert_equals "$top_help_symlink" "$(expected_top_help ai-dev-flow)"
assert_contains "$top_help_symlink" 'Usage: ai-dev-flow <command> [options]'
assert_contains "$top_help_symlink" 'Run `ai-dev-flow <command> --help` for command-specific help.'

help_command_output="$(cd "$help_repo/subdir" && "$symlink_path" help -h)"
assert_equals "$help_command_output" "$(expected_command_help ai-dev-flow help)"
assert_contains "$help_command_output" 'Usage: ai-dev-flow help'
assert_contains "$help_command_output" 'Show top-level command help.'

help_command_verbose="$(cd "$help_repo/subdir" && "$symlink_path" help --help)"
assert_equals "$help_command_output" "$help_command_verbose"

# command-specific help for every public command
for command in start patch status review commit reset promote complete block resume get set unset help; do
	command_help_short="$TMP_DIR/${command}-short.txt"
	command_help_long="$TMP_DIR/${command}-long.txt"
	if run_flow_capture "$help_repo/subdir" "$command_help_short" "$command" -h; then
		short_status=0
	else
		short_status=$?
	fi
	if run_flow_capture "$help_repo/subdir" "$command_help_long" "$command" --help; then
		long_status=0
	else
		long_status=$?
	fi
	assert_equals "$short_status" '0'
	assert_equals "$long_status" '0'
	assert_equals "$(cat "$command_help_short")" "$(cat "$command_help_long")"
	assert_equals "$(cat "$command_help_short")" "$(expected_command_help flow "$command")"
	done

# help works outside Git repositories and with malformed config
outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
for command in start patch status review commit reset promote complete block resume get set unset help; do
	for help_flag in -h --help; do
		outside_output="$TMP_DIR/outside-${command}-${help_flag//-/}.txt"
		if run_flow_capture "$outside_repo" "$outside_output" "$command" "$help_flag"; then
			outside_status=0
		else
			outside_status=$?
		fi
		assert_equals "$outside_status" '0'
		assert_not_contains "$(cat "$outside_output")" 'Output written to'
		done
done

repo_malformed="$TMP_DIR/repo-malformed"
init_repo "$repo_malformed"
mkdir -p "$repo_malformed/.ai-dev"
printf '{ invalid json\n' > "$repo_malformed/.ai-dev/config.json"
for command in start patch status review commit reset promote complete block resume get set unset help; do
	malformed_output="$TMP_DIR/malformed-${command}.txt"
	if run_flow_capture "$repo_malformed/subdir" "$malformed_output" "$command" --help; then
		malformed_status=0
	else
		malformed_status=$?
	fi
	assert_equals "$malformed_status" '0'
	assert_not_contains "$(cat "$malformed_output")" 'Invalid JSON in'
	assert_not_contains "$(cat "$malformed_output")" 'Output written to'
done

# help bypasses workflow-state and branch validation
repo_bypass="$TMP_DIR/repo-bypass"
init_repo "$repo_bypass"
git -C "$repo_bypass" checkout -q -b scratch
state_set "$repo_bypass/subdir" '{"activeIssueNumber":99,"mainBranch":"main","scratchBranch":"scratch","checkpoint":4}' >/dev/null
git -C "$repo_bypass" checkout -q main
printf '{ invalid workflow json\n' > "$repo_bypass/.ai-dev/workflow.json"
for command in start patch status review commit reset promote complete block resume get set unset help; do
	bypass_output="$TMP_DIR/bypass-${command}.txt"
	if run_flow_capture "$repo_bypass/subdir" "$bypass_output" "$command" -h; then
		bypass_status=0
	else
		bypass_status=$?
	fi
	assert_equals "$bypass_status" '0'
	assert_not_contains "$(cat "$bypass_output")" 'Not inside a Git repository'
	assert_not_contains "$(cat "$bypass_output")" 'Invalid JSON in'
	done

# help never routes output
repo_routed="$TMP_DIR/repo-routed"
init_repo "$repo_routed"
set_repo_out="$TMP_DIR/help-output.txt"
write_config_file "$repo_routed" "$set_repo_out"
git -C "$repo_routed" add .ai-dev/config.json
git -C "$repo_routed" commit -q -m 'track config'
top_level_routed_output="$TMP_DIR/top-level-routed.txt"
if run_flow_capture "$repo_routed/subdir" "$top_level_routed_output" help; then
	routed_status=0
else
	routed_status=$?
fi
assert_equals "$routed_status" '0'
assert_equals "$(cat "$top_level_routed_output")" "$(expected_top_help flow)"
assert_not_exists "$set_repo_out"

command_help_routed_output="$TMP_DIR/command-help-routed.txt"
if run_flow_capture "$repo_routed/subdir" "$command_help_routed_output" status --help; then
	routed_command_status=0
else
	routed_command_status=$?
fi
assert_equals "$routed_command_status" '0'
assert_contains "$(cat "$command_help_routed_output")" 'Usage: flow status [-v|--verbose]'
assert_not_exists "$set_repo_out"

# extra arguments combined with help are rejected
for command in patch status review commit reset complete block resume get set unset help; do
	extra_output="$TMP_DIR/${command}-extra.txt"
	if run_flow_capture "$help_repo/subdir" "$extra_output" "$command" -h extra; then
		extra_status=0
	else
		extra_status=$?
	fi
	assert_equals "$extra_status" '1'
	assert_contains "$(cat "$extra_output")" 'Usage:'
	done

start_extra_output="$TMP_DIR/start-extra.txt"
if run_flow_capture "$help_repo/subdir" "$start_extra_output" start 2 -h; then
	start_extra_status=0
else
	start_extra_status=$?
fi
assert_equals "$start_extra_status" '1'
assert_contains "$(cat "$start_extra_output")" 'Usage: flow start <issue-number>'

promote_extra_output="$TMP_DIR/promote-extra.txt"
if run_flow_capture "$help_repo/subdir" "$promote_extra_output" promote -h message; then
	promote_extra_status=0
else
	promote_extra_status=$?
fi
assert_equals "$promote_extra_status" '1'
assert_contains "$(cat "$promote_extra_output")" 'Usage: flow promote "<commit-message>"'

# help does not modify workflow state, config, HEAD, refs, index, or working tree
repo_read_only="$TMP_DIR/repo-read-only"
init_repo "$repo_read_only"
git -C "$repo_read_only" checkout -q -b scratch
state_set "$repo_read_only/subdir" '{"activeIssueNumber":17,"activeIssueTitle":"Read only","mainBranch":"main","scratchBranch":"scratch","checkpoint":3}' >/dev/null
write_config_file "$repo_read_only" "$TMP_DIR/read-only-out.txt"
git -C "$repo_read_only" add .ai-dev/config.json
git -C "$repo_read_only" commit -q -m 'track config'
printf 'staged\n' >> "$repo_read_only/tracked.txt"
git -C "$repo_read_only" add tracked.txt
printf 'modified\n' >> "$repo_read_only/tracked.txt"
printf 'untracked\n' > "$repo_read_only/untracked.txt"

state_before="$(cat "$repo_read_only/.ai-dev/workflow.json")"
head_before="$(current_head "$repo_read_only")"
main_before="$(branch_head "$repo_read_only" main)"
scratch_before="$(branch_head "$repo_read_only" scratch)"
index_before="$(cached_diff "$repo_read_only")"
worktree_before="$(worktree_diff "$repo_read_only")"
status_before="$(repo_status_porcelain "$repo_read_only")"
tracked_before="$(cat "$repo_read_only/tracked.txt")"
untracked_before="$(cat "$repo_read_only/untracked.txt")"

run_flow "$repo_read_only/subdir" help >/dev/null
run_flow "$repo_read_only/subdir" status -h >/dev/null
run_flow "$repo_read_only/subdir" help --help >/dev/null

assert_equals "$(cat "$repo_read_only/.ai-dev/workflow.json")" "$state_before"
assert_equals "$(current_head "$repo_read_only")" "$head_before"
assert_equals "$(branch_head "$repo_read_only" main)" "$main_before"
assert_equals "$(branch_head "$repo_read_only" scratch)" "$scratch_before"
assert_equals "$(cached_diff "$repo_read_only")" "$index_before"
assert_equals "$(worktree_diff "$repo_read_only")" "$worktree_before"
assert_equals "$(repo_status_porcelain "$repo_read_only")" "$status_before"
assert_equals "$(cat "$repo_read_only/tracked.txt")" "$tracked_before"
assert_equals "$(cat "$repo_read_only/untracked.txt")" "$untracked_before"
assert_equals "$(cat "$repo_read_only/.ai-dev/config.json")" '{
  "out": "'$TMP_DIR'/read-only-out.txt"
}'

printf 'flow help tests passed\n'
