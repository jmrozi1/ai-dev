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

assert_not_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" == *"$needle"* ]]; then
		printf 'expected output not to contain: %s\n' "$needle" >&2
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
	)
}

set_repo_out() {
	local repo_root="$1"
	local out_value="$2"
	mkdir -p "$repo_root/.ai-dev"
	cat >"$repo_root/.ai-dev/config.json" <<EOF
{
  "out": "${out_value}"
}
EOF
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

expected_top_help() {
	local command_name="$1"
	cat <<EOF
Usage: ${command_name} <command> [options]

Manage an issue-focused development workflow using permanent main history
and disposable scratch checkpoints.

Commands:
  start      Begin work on an issue and reset scratch from main.
  status     Show the active issue and current repository state.
  review     Generate the cumulative change package for review.
  commit     Create the next numbered checkpoint on scratch.
  reset      Discard scratch work and restore it from main.
  promote    Squash scratch into one permanent commit on main.
  complete   Clear the completed local workflow.
  get        Read a repository setting.
  set        Change a repository setting.
  unset      Remove a repository setting.
  help       Show this help.

Run \`${command_name} <command> --help\` for command-specific help.
EOF
}

repo_unset="$TMP_DIR/repo-unset"
repo_routing="$TMP_DIR/repo-routing"
repo_malformed="$TMP_DIR/repo-malformed"
init_repo "$repo_unset"
init_repo "$repo_routing"
init_repo "$repo_malformed"
(
	cd "$repo_routing"
	git config user.name 'Flow Routing Tests'
	git config user.email 'flow-routing-tests@example.com'
	printf '.ai-dev/workflow.json\n' > .gitignore
	printf 'base\n' > tracked.txt
	git add .gitignore tracked.txt
	git commit -q -m 'initial commit'
	git branch -M main
)
repo_routing_branch="$(git -C "$repo_routing" symbolic-ref --quiet --short HEAD)"

# top-level help bypasses routing and works without repo/config context
help_output_file="$TMP_DIR/help-output"
if run_flow_capture "$repo_unset/subdir" "$help_output_file" help; then
	help_status=0
else
	help_status=$?
fi
help_output="$(cat "$help_output_file")"
assert_equals "$help_status" "0"
assert_contains "$help_output" 'Usage: flow <command> [options]'
assert_contains "$help_output" 'Commands:'
assert_contains "$help_output" '  help       Show this help.'
assert_not_exists "$repo_unset/.ai-dev"

# no-argument invocation matches help and top-level flags
no_args_output_file="$TMP_DIR/no-args-output"
if run_flow_capture "$repo_unset/subdir" "$no_args_output_file"; then
	no_args_status=0
else
	no_args_status=$?
fi
no_args_output="$(cat "$no_args_output_file")"
assert_equals "$no_args_status" "0"
assert_equals "$no_args_output" "$help_output"

minus_h_output_file="$TMP_DIR/minus-h-output"
if run_flow_capture "$repo_unset/subdir" "$minus_h_output_file" -h; then
	minus_h_status=0
else
	minus_h_status=$?
fi
minus_h_output="$(cat "$minus_h_output_file")"
assert_equals "$minus_h_status" "0"
assert_equals "$minus_h_output" "$help_output"

long_help_output_file="$TMP_DIR/long-help-output"
if run_flow_capture "$repo_unset/subdir" "$long_help_output_file" --help; then
	long_help_status=0
else
	long_help_status=$?
fi
long_help_output="$(cat "$long_help_output_file")"
assert_equals "$long_help_status" "0"
assert_equals "$long_help_output" "$help_output"

# malformed config + help succeeds and prints to stdout
mkdir -p "$repo_malformed/.ai-dev"
printf '{ invalid json\n' > "$repo_malformed/.ai-dev/config.json"
malformed_help_output_file="$TMP_DIR/malformed-help-output"
if run_flow_capture "$repo_malformed/subdir" "$malformed_help_output_file" help; then
	malformed_help_status=0
else
	malformed_help_status=$?
fi
malformed_help_output="$(cat "$malformed_help_output_file")"
assert_equals "$malformed_help_status" "0"
assert_equals "$malformed_help_output" "$help_output"
assert_not_contains "$malformed_help_output" 'Output written to'
assert_not_contains "$malformed_help_output" 'Invalid JSON in'

# malformed config + no args succeeds and prints to stdout
malformed_no_args_output_file="$TMP_DIR/malformed-no-args-output"
if run_flow_capture "$repo_malformed/subdir" "$malformed_no_args_output_file"; then
	malformed_no_args_status=0
else
	malformed_no_args_status=$?
fi
malformed_no_args_output="$(cat "$malformed_no_args_output_file")"
assert_equals "$malformed_no_args_status" "0"
assert_equals "$malformed_no_args_output" "$help_output"
assert_not_contains "$malformed_no_args_output" 'Output written to'
assert_not_contains "$malformed_no_args_output" 'Invalid JSON in'

# malformed config + get out stays strict
malformed_get_output_file="$TMP_DIR/malformed-get-output"
if run_flow_capture "$repo_malformed/subdir" "$malformed_get_output_file" get out; then
	malformed_get_status=0
else
	malformed_get_status=$?
fi
malformed_get_output="$(cat "$malformed_get_output_file")"
assert_equals "$malformed_get_status" "1"
assert_contains "$malformed_get_output" 'Invalid JSON in'

# common routing policy validation only accepts strict/ignore
invalid_policy_output_file="$TMP_DIR/invalid-policy-output"
if (
	cd "$repo_unset/subdir"
	FLOW_TEST_MODE=1 "$FLOW" __test-invalid-policy >"$invalid_policy_output_file" 2>&1
); then
	invalid_policy_status=0
else
	invalid_policy_status=$?
fi
invalid_policy_output="$(cat "$invalid_policy_output_file")"
assert_equals "$invalid_policy_status" "1"
assert_contains "$invalid_policy_output" 'Unknown operational config policy: bogus. Supported policies: strict, ignore.'

# arguments after function are passed through unchanged
passthrough_output_file="$TMP_DIR/passthrough-output"
if (
	cd "$repo_unset/subdir"
	FLOW_TEST_MODE=1 "$FLOW" __test-route-args '123' 'value with spaces' >"$passthrough_output_file" 2>&1
); then
	passthrough_status=0
else
	passthrough_status=$?
fi
passthrough_output="$(cat "$passthrough_output_file")"
assert_equals "$passthrough_status" "0"
assert_contains "$passthrough_output" 'arg1=123'
assert_contains "$passthrough_output" 'arg2=value with spaces'

# tee-style routing prints command output to stdout and writes the same output to the file
mkdir -p "$repo_routing/reports"
status_routed_path="$TMP_DIR/status-routed.txt"
set_repo_out "$repo_routing" "$status_routed_path"
status_routed_output_file="$TMP_DIR/status-routed-output"
if run_flow_capture "$repo_routing/subdir" "$status_routed_output_file" status; then
	status_routed_status=0
else
	status_routed_status=$?
fi
status_routed_output="$(cat "$status_routed_output_file")"
status_routed_file_output="$(cat "$status_routed_path")"
status_routed_output_without_confirmation="$(printf '%s\n' "$status_routed_output" | sed '$d')"
status_routed_output_confirmation="$(printf '%s\n' "$status_routed_output" | tail -n 1)"
assert_equals "$status_routed_status" "0"
assert_contains "$status_routed_output" 'No active workflow.'
assert_contains "$status_routed_output" "Branch: ${repo_routing_branch}"
assert_contains "$status_routed_output" 'Output written to '
assert_equals "$status_routed_output_without_confirmation" "$status_routed_file_output"
assert_equals "$status_routed_output_confirmation" "Output written to $status_routed_path"
assert_not_contains "$status_routed_file_output" 'Output written to'

if run_flow_capture "$repo_routing/subdir" "$TMP_DIR/status-routed-output-2" status; then
	status_routed_status_2=0
else
	status_routed_status_2=$?
fi
status_routed_output_2="$(cat "$TMP_DIR/status-routed-output-2")"
status_routed_file_output_2="$(cat "$status_routed_path")"
status_routed_output_2_without_confirmation="$(printf '%s\n' "$status_routed_output_2" | sed '$d')"
assert_equals "$status_routed_status_2" "0"
assert_equals "$status_routed_output_2_without_confirmation" "$status_routed_file_output_2"
assert_equals "$status_routed_file_output_2" "$status_routed_file_output"

# help never routes output even when configured
set_repo_out "$repo_routing" 'reports/help.txt'
help_routed_output_file="$TMP_DIR/help-routed-output"
if run_flow_capture "$repo_routing/subdir" "$help_routed_output_file" help; then
	help_routed_status=0
else
	help_routed_status=$?
fi
help_routed_output="$(cat "$help_routed_output_file")"
assert_equals "$help_routed_status" "0"
assert_equals "$help_routed_output" "$help_output"
assert_not_exists "$repo_routing/reports/help.txt"

help_routed_no_args_file="$TMP_DIR/help-routed-no-args-output"
if run_flow_capture "$repo_routing/subdir" "$help_routed_no_args_file"; then
	help_routed_no_args_status=0
else
	help_routed_no_args_status=$?
fi
help_routed_no_args_output="$(cat "$help_routed_no_args_file")"
assert_equals "$help_routed_no_args_status" "0"
assert_equals "$help_routed_no_args_output" "$help_output"
assert_not_exists "$repo_routing/reports/help.txt"

help_routed_status_flag_file="$TMP_DIR/help-routed-status-help-output"
if run_flow_capture "$repo_routing/subdir" "$help_routed_status_flag_file" status --help; then
	help_routed_status_flag_status=0
else
	help_routed_status_flag_status=$?
fi
help_routed_status_flag_output="$(cat "$help_routed_status_flag_file")"
assert_equals "$help_routed_status_flag_status" "0"
assert_contains "$help_routed_status_flag_output" 'Usage: flow status [-v|--verbose]'
assert_not_exists "$repo_routing/reports/help.txt"

# restore configured out for subsequent routing checks
set_repo_out "$repo_routing" '~/ai-dev-home/help.txt'

# configuration commands remain in the terminal
if run_flow_capture "$repo_routing/subdir" "$TMP_DIR/get-out-output" get out; then
	get_out_status=0
else
	get_out_status=$?
fi
get_out_text="$(cat "$TMP_DIR/get-out-output")"
assert_equals "$get_out_status" "0"
assert_equals "$get_out_text" '~/ai-dev-home/help.txt'
assert_not_contains "$get_out_text" 'Output written to'

if run_flow_capture "$repo_routing/subdir" "$TMP_DIR/set-out-output" set out='reports/config-help.txt'; then
	set_out_status=0
else
	set_out_status=$?
fi
set_out_text="$(cat "$TMP_DIR/set-out-output")"
assert_equals "$set_out_status" "0"
assert_equals "$set_out_text" 'out: reports/config-help.txt'
assert_not_contains "$set_out_text" 'Output written to'

if run_flow_capture "$repo_routing/subdir" "$TMP_DIR/unset-out-output" unset out; then
	unset_out_status=0
else
	unset_out_status=$?
fi
unset_out_text="$(cat "$TMP_DIR/unset-out-output")"
assert_equals "$unset_out_status" "0"
assert_equals "$unset_out_text" 'out: not configured'
assert_not_contains "$unset_out_text" 'Output written to'

# unknown-command errors remain in the terminal
unknown_output_file="$TMP_DIR/unknown-output"
if run_flow_capture "$repo_routing/subdir" "$unknown_output_file" gibberish; then
	unknown_status=0
else
	unknown_status=$?
fi
unknown_output="$(cat "$unknown_output_file")"
assert_equals "$unknown_status" "1"
assert_contains "$unknown_output" 'flow: unknown command: gibberish'
assert_contains "$unknown_output" 'Run flow help for usage.'
assert_not_contains "$unknown_output" 'Output written to'

# routed commands preserve invalid-path behavior and generated-output recovery
set_repo_out "$repo_routing" 'missing-parent/help.txt'
missing_parent_output_file="$TMP_DIR/missing-parent-output"
if run_flow_capture "$repo_routing/subdir" "$missing_parent_output_file" status; then
	missing_parent_status=0
else
	missing_parent_status=$?
fi
missing_parent_output="$(cat "$missing_parent_output_file")"
assert_equals "$missing_parent_status" "1"
assert_contains "$missing_parent_output" 'No active workflow.'
assert_contains "$missing_parent_output" "Branch: ${repo_routing_branch}"
assert_contains "$missing_parent_output" 'Cannot write output to'
assert_contains "$missing_parent_output" 'Generated output preserved at'
assert_not_exists "$repo_routing/missing-parent/help.txt"

if [[ "$(id -u)" != '0' ]]; then
	mkdir -p "$repo_routing/no-write"
	chmod 500 "$repo_routing/no-write"
	set_repo_out "$repo_routing" 'no-write/help.txt'
	unwritable_output_file="$TMP_DIR/unwritable-output"
	if run_flow_capture "$repo_routing/subdir" "$unwritable_output_file" status; then
		unwritable_status=0
	else
		unwritable_status=$?
	fi
	unwritable_output="$(cat "$unwritable_output_file")"
	assert_equals "$unwritable_status" "1"
	assert_contains "$unwritable_output" 'No active workflow.'
	assert_contains "$unwritable_output" "Branch: ${repo_routing_branch}"
	assert_contains "$unwritable_output" 'Cannot write output to'
	assert_contains "$unwritable_output" 'Generated output preserved at'
	assert_not_exists "$repo_routing/no-write/help.txt"
	chmod 700 "$repo_routing/no-write"
fi

# help outside a git repository still prints normally
outside_repo="$TMP_DIR/outside-repo"
mkdir -p "$outside_repo"
outside_output_file="$TMP_DIR/outside-output"
if run_flow_capture "$outside_repo" "$outside_output_file" help; then
	outside_status=0
else
	outside_status=$?
fi
outside_output="$(cat "$outside_output_file")"
assert_equals "$outside_status" "0"
assert_equals "$outside_output" "$help_output"

# alternate invoked command name appears inside top-level help
mkdir -p "$repo_routing/reports"
set_repo_out "$repo_routing" 'reports/alt-help.txt'
symlink_path="$TMP_DIR/ai-dev-flow"
ln -s "$FLOW" "$symlink_path"
symlink_terminal_output_file="$TMP_DIR/symlink-terminal-output"
if (
	cd "$repo_routing/subdir"
	"$symlink_path" help >"$symlink_terminal_output_file" 2>&1
); then
	symlink_status=0
else
	symlink_status=$?
fi
symlink_terminal_output="$(cat "$symlink_terminal_output_file")"
assert_equals "$symlink_status" "0"
assert_equals "$symlink_terminal_output" "$(expected_top_help ai-dev-flow)"
assert_not_exists "$repo_routing/reports/alt-help.txt"

printf 'flow CLI tests passed\n'
