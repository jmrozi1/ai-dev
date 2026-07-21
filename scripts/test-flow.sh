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

repo_unset="$TMP_DIR/repo-unset"
repo_routing="$TMP_DIR/repo-routing"
repo_malformed="$TMP_DIR/repo-malformed"
init_repo "$repo_unset"
init_repo "$repo_routing"
init_repo "$repo_malformed"

# help with out unset
help_output_file="$TMP_DIR/help-output"
if run_flow_capture "$repo_unset/subdir" "$help_output_file" help; then
	help_status=0
else
	help_status=$?
fi
help_output="$(cat "$help_output_file")"
assert_equals "$help_status" "0"
assert_contains "$help_output" 'flow help'
assert_contains "$help_output" 'flow start <issue-number>'
assert_contains "$help_output" 'flow status [-v|--verbose]'
assert_contains "$help_output" 'flow review'
assert_contains "$help_output" 'flow commit'
assert_contains "$help_output" 'flow reset'
assert_contains "$help_output" 'flow promote "<commit-message>"'
assert_contains "$help_output" 'flow complete'
assert_contains "$help_output" 'Read-only'
assert_contains "$help_output" 'Configuration'
assert_contains "$help_output" 'Workflow mutations'
assert_not_exists "$repo_unset/.ai-dev"

# no-argument help with out unset matches flow help
no_args_output_file="$TMP_DIR/no-args-output"
if run_flow_capture "$repo_unset/subdir" "$no_args_output_file"; then
	no_args_status=0
else
	no_args_status=$?
fi
no_args_output="$(cat "$no_args_output_file")"
assert_equals "$no_args_status" "0"
assert_equals "$no_args_output" "$help_output"

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
assert_contains "$malformed_help_output" 'flow help'
assert_contains "$malformed_help_output" 'flow review'
assert_contains "$malformed_help_output" 'flow commit'
assert_contains "$malformed_help_output" 'flow reset'
assert_contains "$malformed_help_output" 'flow complete'
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
assert_contains "$malformed_no_args_output" 'flow help'
assert_contains "$malformed_no_args_output" 'flow review'
assert_contains "$malformed_no_args_output" 'flow commit'
assert_contains "$malformed_no_args_output" 'flow reset'
assert_contains "$malformed_no_args_output" 'flow complete'
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

# help with relative out
mkdir -p "$repo_routing/reports"
set_repo_out "$repo_routing" 'reports/help.txt'
relative_terminal_output_file="$TMP_DIR/relative-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$relative_terminal_output_file" help; then
	relative_status=0
else
	relative_status=$?
fi
relative_terminal_output="$(cat "$relative_terminal_output_file")"
relative_output_path="$repo_routing/reports/help.txt"
assert_equals "$relative_status" "0"
assert_equals "$relative_terminal_output" "Output written to $relative_output_path"
assert_contains "$(cat "$relative_output_path")" 'flow help'

# no-argument help routing with relative out
relative_no_args_terminal_output_file="$TMP_DIR/relative-no-args-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$relative_no_args_terminal_output_file"; then
	relative_no_args_status=0
else
	relative_no_args_status=$?
fi
relative_no_args_terminal_output="$(cat "$relative_no_args_terminal_output_file")"
assert_equals "$relative_no_args_status" "0"
assert_equals "$relative_no_args_terminal_output" "Output written to $relative_output_path"
assert_contains "$(cat "$relative_output_path")" 'flow help'

# help with absolute out
absolute_output_path="$TMP_DIR/absolute-help.txt"
set_repo_out "$repo_routing" "$absolute_output_path"
absolute_terminal_output_file="$TMP_DIR/absolute-terminal-output"
if run_flow_capture "$repo_routing/subdir" "$absolute_terminal_output_file" help; then
	absolute_status=0
else
	absolute_status=$?
fi
absolute_terminal_output="$(cat "$absolute_terminal_output_file")"
assert_equals "$absolute_status" "0"
assert_equals "$absolute_terminal_output" "Output written to $absolute_output_path"
assert_contains "$(cat "$absolute_output_path")" 'flow help'

# help with tilde-expansion out
home_root="$TMP_DIR/home"
mkdir -p "$home_root/ai-dev-home"
tilde_output_path="$home_root/ai-dev-home/help.txt"
set_repo_out "$repo_routing" '~/ai-dev-home/help.txt'
tilde_terminal_output_file="$TMP_DIR/tilde-terminal-output"
if HOME="$home_root" run_flow_capture "$repo_routing/subdir" "$tilde_terminal_output_file" help; then
	tilde_status=0
else
	tilde_status=$?
fi
tilde_terminal_output="$(cat "$tilde_terminal_output_file")"
assert_equals "$tilde_status" "0"
assert_equals "$tilde_terminal_output" "Output written to $tilde_output_path"
assert_contains "$(cat "$tilde_output_path")" 'flow help'

# configured output replaces existing content
printf 'old content\n' > "$tilde_output_path"
if HOME="$home_root" run_flow_capture "$repo_routing/subdir" "$TMP_DIR/replace-terminal-output" help; then
	replace_status=0
else
	replace_status=$?
fi
replace_terminal_output="$(cat "$TMP_DIR/replace-terminal-output")"
replace_file_text="$(cat "$tilde_output_path")"
assert_equals "$replace_status" "0"
assert_equals "$replace_terminal_output" "Output written to $tilde_output_path"
assert_not_contains "$replace_file_text" 'old content'
assert_contains "$replace_file_text" 'flow help'

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

# restore configured out for subsequent routing checks
set_repo_out "$repo_routing" '~/ai-dev-home/help.txt'

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

# missing parent directory fails clearly
set_repo_out "$repo_routing" 'missing-parent/help.txt'
missing_parent_output_file="$TMP_DIR/missing-parent-output"
if run_flow_capture "$repo_routing/subdir" "$missing_parent_output_file" help; then
	missing_parent_status=0
else
	missing_parent_status=$?
fi
missing_parent_output="$(cat "$missing_parent_output_file")"
assert_equals "$missing_parent_status" "1"
assert_contains "$missing_parent_output" 'Cannot write output to'
assert_contains "$missing_parent_output" 'parent directory does not exist'
assert_contains "$missing_parent_output" 'Generated output preserved at'
assert_not_exists "$repo_routing/missing-parent/help.txt"

# unwritable destination fails clearly (portable where non-root)
if [[ "$(id -u)" != '0' ]]; then
	mkdir -p "$repo_routing/no-write"
	chmod 500 "$repo_routing/no-write"
	set_repo_out "$repo_routing" 'no-write/help.txt'
	unwritable_output_file="$TMP_DIR/unwritable-output"
	if run_flow_capture "$repo_routing/subdir" "$unwritable_output_file" help; then
		unwritable_status=0
	else
		unwritable_status=$?
	fi
	unwritable_output="$(cat "$unwritable_output_file")"
	assert_equals "$unwritable_status" "1"
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
assert_contains "$outside_output" 'flow help'
assert_not_contains "$outside_output" 'Output written to'

# alternate invoked command name appears inside redirected help
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
symlink_help_file="$repo_routing/reports/alt-help.txt"
assert_equals "$symlink_status" "0"
assert_equals "$symlink_terminal_output" "Output written to $symlink_help_file"
assert_contains "$(cat "$symlink_help_file")" 'ai-dev-flow help'
assert_contains "$(cat "$symlink_help_file")" 'ai-dev-flow review'
assert_contains "$(cat "$symlink_help_file")" 'ai-dev-flow commit'
assert_contains "$(cat "$symlink_help_file")" 'ai-dev-flow reset'
assert_contains "$(cat "$symlink_help_file")" 'ai-dev-flow complete'

printf 'flow CLI tests passed\n'
