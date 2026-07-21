#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_SCRIPT="$SCRIPT_DIR/flow"
CONFIG_FILE="$SCRIPT_DIR/bootstrap-config.yaml"
LOCAL_BIN_DIR="$HOME/.local/bin"
DEFAULT_COMMAND_NAME="flow"

error() {
	printf 'bootstrap-linux.sh: %s\n' "$1" >&2
	exit 1
}

read_configured_command_name() {
	if [[ ! -f "$CONFIG_FILE" ]]; then
		return 1
	fi

	awk '
		BEGIN { found = 0 }
		/^[[:space:]]*#/ { next }
		/^[[:space:]]*command_name[[:space:]]*:/ {
			found = 1
			sub(/^[[:space:]]*command_name[[:space:]]*:[[:space:]]*/, "", $0)
			sub(/[[:space:]]*#.*$/, "", $0)
			gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
			print
			exit
		}
		END {
			if (!found) {
				exit 1
			}
		}
	' "$CONFIG_FILE"
}

validate_command_name() {
	local command_name="$1"

	if [[ ! "$command_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
		error "Invalid command name: $command_name"
	fi
}

path_contains_local_bin() {
	local local_bin_path="$1"
	local entry

	IFS=':' read -ra path_entries <<< "$PATH"
	for entry in "${path_entries[@]}"; do
		if [[ "$entry" == "$local_bin_path" ]]; then
			return 0
		fi
	done

	return 1
}

main() {
	local command_name
	local configured_command_name=""
	if configured_command_name="$(read_configured_command_name)"; then
		if [[ -z "$configured_command_name" ]]; then
			error "command_name is empty or invalid in bootstrap-config.yaml."
		fi

		command_name="$configured_command_name"
	else
		command_name="$DEFAULT_COMMAND_NAME"
	fi
	validate_command_name "$command_name"

	if [[ ! -e "$FLOW_SCRIPT" ]]; then
		error "Missing flow script: $FLOW_SCRIPT"
	fi

	chmod +x "$FLOW_SCRIPT"
	mkdir -p "$LOCAL_BIN_DIR"

	local installed_link="$LOCAL_BIN_DIR/$command_name"
	local desired_target
	desired_target="$(cd "$SCRIPT_DIR" && pwd)/flow"
	local status=""
	local link_exists=false
	local current_target=""

	if [[ -e "$installed_link" || -L "$installed_link" ]]; then
		link_exists=true
	fi

	if [[ -e "$installed_link" && ! -L "$installed_link" ]]; then
		error "Cannot install $command_name: $installed_link already exists and is not a symlink. Remove it first."
	fi

	if [[ -L "$installed_link" ]]; then
		current_target="$(readlink -f "$installed_link")"
	fi

	if [[ "$current_target" == "$desired_target" ]]; then
		status='Already up to date'
	else
		ln -sfnT "$desired_target" "$installed_link"
		if [[ "$link_exists" == true ]]; then
			status='Updated'
		else
			status='Installed'
		fi
	fi

	printf '%s\n' "$status"
	printf 'Installed command: %s\n' "$command_name"
	printf 'Symlink target: %s\n' "$desired_target"

	if path_contains_local_bin "$LOCAL_BIN_DIR"; then
		printf '%s is on PATH\n' "$LOCAL_BIN_DIR"
	else
		printf 'Add to PATH with:\n'
		printf 'export PATH="%s:$PATH"\n' "$LOCAL_BIN_DIR"
	fi
}

main "$@"
