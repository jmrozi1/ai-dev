#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
FLOW_SOURCE="${ROOT}/scripts/flow"
FLOW_TARGET="${BIN_DIR}/flow"

if [[ ! -f "$FLOW_SOURCE" ]]; then
	printf 'Flow script not found: %s\n' "$FLOW_SOURCE" >&2
	exit 1
fi

if [[ ! -x "$FLOW_SOURCE" ]]; then
	chmod +x "$FLOW_SOURCE"
fi

mkdir -p "$BIN_DIR"

if [[ -L "$FLOW_TARGET" ]]; then
	current_target="$(readlink "$FLOW_TARGET")"

	if [[ "$current_target" != "$FLOW_SOURCE" ]]; then
		ln -sfn "$FLOW_SOURCE" "$FLOW_TARGET"
		printf 'Updated flow command:\n'
		printf '  %s -> %s\n' "$FLOW_TARGET" "$FLOW_SOURCE"
	fi
elif [[ -e "$FLOW_TARGET" ]]; then
	printf 'Cannot install flow: target already exists and is not a symlink:\n' >&2
	printf '  %s\n' "$FLOW_TARGET" >&2
	exit 1
else
	ln -s "$FLOW_SOURCE" "$FLOW_TARGET"
	printf 'Installed flow command:\n'
	printf '  %s -> %s\n' "$FLOW_TARGET" "$FLOW_SOURCE"
fi

case ":${PATH}:" in
	*":${BIN_DIR}:"*)
		;;
	*)
		printf '%s is not currently on PATH.\n' "$BIN_DIR"
		printf 'Add this line to your shell profile:\n\n'
		printf '  export PATH="$HOME/.local/bin:$PATH"\n\n'
		printf 'For bash, that is usually ~/.bashrc.\n'
		;;
esac

printf 'Bootstrap complete.\n'
