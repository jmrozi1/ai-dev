#!/usr/bin/env bash
set -euo pipefail

show_help() {
	cat <<'EOF'
Usage: scripts/install.sh [bootstrap-options]

Install or refresh prefixed flow launchers.

This wrapper calls:
  python -m ai_dev_flow.bootstrap --platform posix --repo-root <this-repo>

By default it adds:
	--prefix flow

So the installed launchers are:
	flow-start flow-patch flow-status flow-diff flow-commit
	flow-reset flow-promote flow-complete flow-block flow-resume
	flow-ticket-create flow-ticket-show flow-ticket-query

Additional installer shorthand:
	-f  Equivalent to --force.
	-v  Show detailed installation output.

Bootstrap options, including --force, --prefix, --home, and --install-dir,
are forwarded to ai_dev_flow.bootstrap.
	--verbose  Same as -v.

Installer options:
	-h, --help   Show this help.

Examples:
	./scripts/install.sh
	./scripts/install.sh -v
	./scripts/install.sh --force
	./scripts/install.sh --prefix ai-flow
	./scripts/install.sh --home "$HOME" --install-dir "$HOME/.local/bin"

Minimum Python version: 3.8

After installation, use your prefixed launchers (for example `flow-status`).
EOF
}

forwarded_args=()
installer_output_mode="concise"
for arg in "$@"; do
	case "$arg" in
		-f)
			forwarded_args+=(--force)
			;;
		-v|--verbose)
			installer_output_mode="detailed"
			;;
		-h|--help)
			show_help
			exit 0
			;;
		*)
			forwarded_args+=("$arg")
			;;
	esac
done

script_path=${BASH_SOURCE[0]}
while [[ -L "$script_path" ]]; do
	script_dir="$(cd "$(dirname "$script_path")" && pwd)"
	script_path="$(readlink "$script_path")"
	if [[ "$script_path" != /* ]]; then
		script_path="$script_dir/$script_path"
	fi
done

script_dir="$(cd "$(dirname "$script_path")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source "$repo_root/tools/bootstrap/python_select.sh"

python_executable="$(ai_dev_select_python "install.sh")" || exit 1

if [[ -n "${PYTHONPATH:-}" ]]; then
	PYTHONPATH="$repo_root:$PYTHONPATH"
else
	PYTHONPATH="$repo_root"
fi
export PYTHONPATH

bootstrap_args=(
	--platform posix
	--repo-root "$repo_root"
	--python "$python_executable"
	--prefix flow
	--installer-output "$installer_output_mode"
)
bootstrap_args+=("${forwarded_args[@]}")

exec "$python_executable" -m ai_dev_flow.bootstrap \
	"${bootstrap_args[@]}"
