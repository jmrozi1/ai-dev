#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BOOTSTRAP_SCRIPT="$ROOT/tools/bootstrap/bootstrap-linux.sh"
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
	local actual="$1"
	local expected="$2"

	if [[ "$actual" != "$expected" ]]; then
		printf 'expected: %s\nactual:   %s\n' "$expected" "$actual" >&2
		exit 1
	fi
}

assert_exists() {
	local path="$1"

	if [[ ! -e "$path" ]]; then
		printf 'expected path to exist: %s\n' "$path" >&2
		exit 1
	fi
}

run_bootstrap() {
	local home_root="$1"
	local path_value="$2"
	local output_file="$3"
	shift 3

	HOME="$home_root" PATH="$path_value" "$BOOTSTRAP_SCRIPT" "$@" >"$output_file" 2>&1
}

make_home() {
	local home_root="$1"
	mkdir -p "$home_root"
}

# default installation delegates to scripts/install.sh
home_root="$TMP_DIR/home default"
make_home "$home_root"
default_output="$TMP_DIR/default-output"
if run_bootstrap "$home_root" "/usr/bin:/bin" "$default_output"; then
	default_status=0
else
	default_status=$?
fi
default_text="$(cat "$default_output")"
assert_equals "$default_status" "0"
assert_contains "$default_text" 'DEPRECATED: tools/bootstrap/bootstrap-linux.sh is deprecated.'
assert_contains "$default_text" 'Use scripts/install.sh instead (or run ai-dev apply).'
assert_contains "$default_text" 'AI Dev installation completed with warnings.'
assert_contains "$default_text" 'Warning: Install directory is not currently on PATH.'
assert_exists "$home_root/.local/bin/flow-start"

# repeated invocation is safe/idempotent
home_root="$TMP_DIR/home-repeat"
make_home "$home_root"
repeat_output="$TMP_DIR/repeat-output"
if run_bootstrap "$home_root" "/usr/bin:/bin" "$repeat_output"; then
	repeat_status=0
else
	repeat_status=$?
fi
repeat_text="$(cat "$repeat_output")"
assert_equals "$repeat_status" "0"
assert_contains "$repeat_text" 'AI Dev installation completed with warnings.'
assert_contains "$repeat_text" 'Warning: Install directory is not currently on PATH.'
assert_exists "$home_root/.local/bin/flow-start"

printf 'bootstrap-linux tests passed\n'
