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

assert_not_exists() {
	local path="$1"

	if [[ -e "$path" || -L "$path" ]]; then
		printf 'expected path to be absent: %s\n' "$path" >&2
		exit 1
	fi
}

assert_file_ends_with_newline() {
	local file_path="$1"

	python3 - "$file_path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.read_bytes().endswith(b'\n'):
    raise SystemExit(f'{path} does not end with a newline')
PY
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
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

repo_a="$TMP_DIR/repo-a"
repo_b="$TMP_DIR/repo-b"
repo_c="$TMP_DIR/repo-c"
init_repo "$repo_a"
init_repo "$repo_b"
init_repo "$repo_c"

# get when unset
unset_output="$TMP_DIR/unset-output"
if run_flow_capture "$repo_a/subdir" "$unset_output" get out; then
	unset_status=0
else
	unset_status=$?
fi
unset_text="$(cat "$unset_output")"
assert_equals "$unset_status" "0"
assert_equals "$unset_text" 'out: not configured'
assert_not_exists "$repo_a/.ai-dev"

# set and get
set_output="$TMP_DIR/set-output"
if run_flow_capture "$repo_a/subdir" "$set_output" set out='build/output.txt'; then
	set_status=0
else
	set_status=$?
fi
set_text="$(cat "$set_output")"
assert_equals "$set_status" "0"
assert_equals "$set_text" 'out: build/output.txt'

config_file_a="$repo_a/.ai-dev/config.json"
assert_equals "$(cat "$config_file_a")" $'{
  "out": "build/output.txt"
}'
assert_file_ends_with_newline "$config_file_a"

get_output="$TMP_DIR/get-output"
if run_flow_capture "$repo_a/subdir" "$get_output" get out; then
	get_status=0
else
	get_status=$?
fi
get_text="$(cat "$get_output")"
assert_equals "$get_status" "0"
assert_equals "$get_text" 'build/output.txt'

# replace existing out value
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/set-output-2" set out='dist/report.txt'; then
	replace_status=0
else
	replace_status=$?
fi
replace_text="$(cat "$TMP_DIR/set-output-2")"
assert_equals "$replace_status" "0"
assert_equals "$replace_text" 'out: dist/report.txt'
assert_equals "$(cat "$config_file_a")" $'{
  "out": "dist/report.txt"
}'

# unset
unset_set_output="$TMP_DIR/unset-set-output"
if run_flow_capture "$repo_a/subdir" "$unset_set_output" unset out; then
	unset_set_status=0
else
	unset_set_status=$?
fi
unset_set_text="$(cat "$unset_set_output")"
assert_equals "$unset_set_status" "0"
assert_equals "$unset_set_text" 'out: not configured'
assert_not_exists "$config_file_a"

# unset when already unset
if run_flow_capture "$repo_c/subdir" "$TMP_DIR/unset-again-output" unset out; then
	unset_again_status=0
else
	unset_again_status=$?
fi
unset_again_text="$(cat "$TMP_DIR/unset-again-output")"
assert_equals "$unset_again_status" "0"
assert_equals "$unset_again_text" 'out: not configured'
assert_not_exists "$repo_c/.ai-dev"

# malformed config JSON
mkdir -p "$repo_a/.ai-dev"
printf '{ invalid json\n' > "$config_file_a"
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/malformed-output" get out; then
	malformed_status=0
else
	malformed_status=$?
fi
malformed_text="$(cat "$TMP_DIR/malformed-output")"
assert_equals "$malformed_status" "1"
assert_contains "$malformed_text" 'Invalid JSON in'

# unknown config key in file
printf '{\n  "other": "value"\n}\n' > "$config_file_a"
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/unknown-config-output" get out; then
	unknown_config_status=0
else
	unknown_config_status=$?
fi
unknown_config_text="$(cat "$TMP_DIR/unknown-config-output")"
assert_equals "$unknown_config_status" "1"
assert_contains "$unknown_config_text" 'Unknown configuration key(s)'

# unknown get key
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/unknown-get-output" get nope; then
	unknown_get_status=0
else
	unknown_get_status=$?
fi
unknown_get_text="$(cat "$TMP_DIR/unknown-get-output")"
assert_equals "$unknown_get_status" "1"
assert_contains "$unknown_get_text" 'Unknown configuration key for get: nope'

# unknown set key
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/unknown-set-output" set nope=value; then
	unknown_set_status=0
else
	unknown_set_status=$?
fi
unknown_set_text="$(cat "$TMP_DIR/unknown-set-output")"
assert_equals "$unknown_set_status" "1"
assert_contains "$unknown_set_text" 'Unknown configuration key for set: nope'

# unknown unset key
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/unknown-unset-output" unset nope; then
	unknown_unset_status=0
else
	unknown_unset_status=$?
fi
unknown_unset_text="$(cat "$TMP_DIR/unknown-unset-output")"
assert_equals "$unknown_unset_status" "1"
assert_contains "$unknown_unset_text" 'Unknown configuration key for unset: nope'

# empty out value
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/empty-set-output" set out=; then
	empty_set_status=0
else
	empty_set_status=$?
fi
empty_set_text="$(cat "$TMP_DIR/empty-set-output")"
assert_equals "$empty_set_status" "1"
assert_contains "$empty_set_text" 'out value cannot be empty'

# usage errors include the invoked command name
alt_flow="$TMP_DIR/alt-flow"
ln -s "$FLOW" "$alt_flow"
if (
	cd "$repo_a/subdir"
	"$alt_flow" get >"$TMP_DIR/alt-flow-usage-output" 2>&1
); then
	alt_flow_usage_status=0
else
	alt_flow_usage_status=$?
fi
alt_flow_usage_text="$(cat "$TMP_DIR/alt-flow-usage-output")"
assert_equals "$alt_flow_usage_status" "1"
assert_contains "$alt_flow_usage_text" 'alt-flow: Usage: alt-flow get out'

# Restore repo_a to a valid config state after malformed/unknown-key checks.
rm -rf "$repo_a/.ai-dev"

# repository isolation
if run_flow_capture "$repo_a/subdir" "$TMP_DIR/repo-a-set-output" set out='repo-a.txt'; then
	repo_a_set_status=0
else
	repo_a_set_status=$?
fi
if run_flow_capture "$repo_b/subdir" "$TMP_DIR/repo-b-get-output" get out; then
	repo_b_get_status=0
else
	repo_b_get_status=$?
fi
repo_b_get_text="$(cat "$TMP_DIR/repo-b-get-output")"
assert_equals "$repo_a_set_status" "0"
assert_equals "$repo_b_get_status" "0"
assert_equals "$repo_b_get_text" 'out: not configured'
assert_equals "$(cat "$repo_a/.ai-dev/config.json")" $'{
  "out": "repo-a.txt"
}'
assert_not_exists "$repo_b/.ai-dev"

# commands run from a repository subdirectory
repo_subdir="$TMP_DIR/repo-subdir"
init_repo "$repo_subdir"
if run_flow_capture "$repo_subdir/subdir" "$TMP_DIR/subdir-output" set out='build/output.txt'; then
	subdir_set_status=0
else
	subdir_set_status=$?
fi
assert_equals "$subdir_set_status" "0"
if run_flow_capture "$repo_subdir/subdir" "$TMP_DIR/subdir-get-output" get out; then
	subdir_status=0
else
	subdir_status=$?
fi
subdir_text="$(cat "$TMP_DIR/subdir-get-output")"
assert_equals "$subdir_status" "0"
assert_equals "$subdir_text" 'build/output.txt'

# valid JSON persisted with expected formatting
if run_flow_capture "$repo_b/subdir" "$TMP_DIR/repo-b-set-output" set out='artifacts/report.json'; then
	repo_b_set_status=0
else
	repo_b_set_status=$?
fi
repo_b_config="$repo_b/.ai-dev/config.json"
assert_equals "$repo_b_set_status" "0"
assert_equals "$(cat "$repo_b_config")" $'{
  "out": "artifacts/report.json"
}'
assert_file_ends_with_newline "$repo_b_config"

printf 'flow config tests passed\n'
