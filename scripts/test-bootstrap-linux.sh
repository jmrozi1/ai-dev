#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP_SCRIPT_SOURCE="$ROOT/scripts/bootstrap-linux.sh"
CONFIG_SOURCE="$ROOT/scripts/bootstrap-config.yaml"
FLOW_SOURCE="$ROOT/scripts/flow"
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

assert_not_exists() {
	local path="$1"

	if [[ -e "$path" || -L "$path" ]]; then
		printf 'expected path to be absent: %s\n' "$path" >&2
		exit 1
	fi
}

assert_symlink_target() {
	local link_path="$1"
	local expected_target="$2"

	if [[ ! -L "$link_path" ]]; then
		printf 'expected symlink: %s\n' "$link_path" >&2
		exit 1
	fi

	local resolved_target
	resolved_target="$(readlink -f "$link_path")"
	assert_equals "$resolved_target" "$expected_target"
}

prepare_workspace() {
	local workspace_root="$1"
	mkdir -p "$workspace_root/scripts"
	cp "$BOOTSTRAP_SCRIPT_SOURCE" "$workspace_root/scripts/bootstrap-linux.sh"
	cp "$CONFIG_SOURCE" "$workspace_root/scripts/bootstrap-config.yaml"
	cp "$FLOW_SOURCE" "$workspace_root/scripts/flow"
	chmod +x "$workspace_root/scripts/bootstrap-linux.sh" "$workspace_root/scripts/flow"
}

run_bootstrap() {
	local workspace_root="$1"
	local home_root="$2"
	local path_value="$3"
	local output_file="$4"
	shift 4

	HOME="$home_root" PATH="$path_value" "$workspace_root/scripts/bootstrap-linux.sh" "$@" >"$output_file" 2>&1
}

make_home() {
	local home_root="$1"
	mkdir -p "$home_root"
}

workspace_root="$TMP_DIR/workspace"
prepare_workspace "$workspace_root"

# default installation
home_root="$TMP_DIR/home-default"
make_home "$home_root"
default_output="$TMP_DIR/default-output"
if run_bootstrap "$workspace_root" "$home_root" "/usr/bin:/bin" "$default_output"; then
	default_status=0
else
	default_status=$?
fi
default_text="$(cat "$default_output")"
assert_equals "$default_status" "0"
assert_contains "$default_text" 'Installed'
assert_contains "$default_text" 'Installed command: flow'
assert_contains "$default_text" 'Symlink target:'
assert_contains "$default_text" 'Add to PATH with:'
assert_contains "$default_text" 'export PATH="'
assert_symlink_target "$home_root/.local/bin/flow" "$workspace_root/scripts/flow"

# overridden command name
override_workspace="$TMP_DIR/workspace-override"
cp -R "$workspace_root" "$override_workspace"
cat > "$override_workspace/scripts/bootstrap-config.yaml" <<'EOF'
# AI Dev Linux bootstrap configuration.
# Uncomment the setting below to install the launcher under a different name.
command_name: ai-dev-flow
EOF
home_root="$TMP_DIR/home-override"
make_home "$home_root"
override_output="$TMP_DIR/override-output"
if run_bootstrap "$override_workspace" "$home_root" "/usr/bin:/bin" "$override_output"; then
	override_status=0
else
	override_status=$?
fi
override_text="$(cat "$override_output")"
assert_equals "$override_status" "0"
assert_contains "$override_text" 'Installed command: ai-dev-flow'
assert_contains "$override_text" 'Symlink target:'
assert_symlink_target "$home_root/.local/bin/ai-dev-flow" "$override_workspace/scripts/flow"

# invalid command name
invalid_workspace="$TMP_DIR/workspace-invalid"
cp -R "$workspace_root" "$invalid_workspace"
cat > "$invalid_workspace/scripts/bootstrap-config.yaml" <<'EOF'
# AI Dev Linux bootstrap configuration.
# Uncomment the setting below to install the launcher under a different name.
command_name: bad/name
EOF
home_root="$TMP_DIR/home-invalid"
make_home "$home_root"
invalid_output="$TMP_DIR/invalid-output"
if run_bootstrap "$invalid_workspace" "$home_root" "/usr/bin:/bin" "$invalid_output"; then
	invalid_status=0
else
	invalid_status=$?
fi
invalid_text="$(cat "$invalid_output")"
assert_equals "$invalid_status" "1"
assert_contains "$invalid_text" 'Invalid command name: bad/name'

# empty command name fails without creating a symlink
empty_workspace="$TMP_DIR/workspace-empty"
cp -R "$workspace_root" "$empty_workspace"
cat > "$empty_workspace/scripts/bootstrap-config.yaml" <<'EOF'
# AI Dev Linux bootstrap configuration.
# Uncomment the setting below to install the launcher under a different name.
command_name:
EOF
home_root="$TMP_DIR/home-empty"
make_home "$home_root"
empty_output="$TMP_DIR/empty-output"
if run_bootstrap "$empty_workspace" "$home_root" "/usr/bin:/bin" "$empty_output"; then
	empty_status=0
else
	empty_status=$?
fi
empty_text="$(cat "$empty_output")"
assert_equals "$empty_status" "1"
assert_contains "$empty_text" 'command_name is empty or invalid'
assert_not_exists "$home_root/.local/bin/flow"

# existing correct symlink
correct_workspace="$TMP_DIR/workspace-correct"
cp -R "$workspace_root" "$correct_workspace"
home_root="$TMP_DIR/home-correct"
make_home "$home_root"
mkdir -p "$home_root/.local/bin"
ln -s "$correct_workspace/scripts/flow" "$home_root/.local/bin/flow"
correct_output="$TMP_DIR/correct-output"
if run_bootstrap "$correct_workspace" "$home_root" "/usr/bin:/bin" "$correct_output"; then
	correct_status=0
else
	correct_status=$?
fi
correct_text="$(cat "$correct_output")"
assert_equals "$correct_status" "0"
assert_contains "$correct_text" 'Already up to date'
assert_contains "$correct_text" 'Installed command: flow'
assert_symlink_target "$home_root/.local/bin/flow" "$correct_workspace/scripts/flow"

# existing incorrect symlink
incorrect_workspace="$TMP_DIR/workspace-incorrect"
cp -R "$workspace_root" "$incorrect_workspace"
home_root="$TMP_DIR/home-incorrect"
make_home "$home_root"
mkdir -p "$home_root/.local/bin"
ln -s /bin/true "$home_root/.local/bin/flow"
incorrect_output="$TMP_DIR/incorrect-output"
if run_bootstrap "$incorrect_workspace" "$home_root" "/usr/bin:/bin" "$incorrect_output"; then
	incorrect_status=0
else
	incorrect_status=$?
fi
incorrect_text="$(cat "$incorrect_output")"
assert_equals "$incorrect_status" "0"
assert_contains "$incorrect_text" 'Updated'
assert_contains "$incorrect_text" 'Installed command: flow'
assert_symlink_target "$home_root/.local/bin/flow" "$incorrect_workspace/scripts/flow"

# existing regular file fails safely
file_workspace="$TMP_DIR/workspace-file"
cp -R "$workspace_root" "$file_workspace"
home_root="$TMP_DIR/home-file"
make_home "$home_root"
mkdir -p "$home_root/.local/bin"
regular_file="$home_root/.local/bin/flow"
printf 'preserve me\n' > "$regular_file"
file_output="$TMP_DIR/file-output"
if run_bootstrap "$file_workspace" "$home_root" "/usr/bin:/bin" "$file_output"; then
	file_status=0
else
	file_status=$?
fi
file_text="$(cat "$file_output")"
assert_equals "$file_status" "1"
assert_contains "$file_text" 'Cannot install flow:'
assert_contains "$file_text" 'already exists and is not a symlink'
assert_equals "$(cat "$regular_file")" 'preserve me'

# PATH detection
path_workspace="$TMP_DIR/workspace-path"
cp -R "$workspace_root" "$path_workspace"
home_root="$TMP_DIR/home-path"
make_home "$home_root"
mkdir -p "$home_root/.local/bin"
path_output="$TMP_DIR/path-output"
if run_bootstrap "$path_workspace" "$home_root" "/usr/bin:/bin:$home_root/.local/bin" "$path_output"; then
	path_status=0
else
	path_status=$?
fi
path_text="$(cat "$path_output")"
assert_equals "$path_status" "0"
assert_contains "$path_text" "$home_root/.local/bin is on PATH"
assert_contains "$path_text" 'Installed'

printf 'bootstrap-linux tests passed\n'
