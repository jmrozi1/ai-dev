#!/usr/bin/env bash
set -euo pipefail

show_help() {
	cat <<'EOF'
Usage: scripts/install.sh [bootstrap-options]

Safely remove AI Dev-managed legacy Flow launchers.

This wrapper calls:
  python -m ai_dev_flow.bootstrap --platform posix

Installer options:
	-h, --help          Show this help.
	--home <path>       Clean the ownership record under a different home.

Examples:
	./scripts/install.sh
	./scripts/install.sh --home "$HOME"

Minimum Python version: 3.8

Normal Flow execution uses the installed Copilot skill package, not PATH commands.
EOF
}

forwarded_args=()
for arg in "$@"; do
	case "$arg" in
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
)
bootstrap_args+=("${forwarded_args[@]}")

exec "$python_executable" -m ai_dev_flow.bootstrap \
	"${bootstrap_args[@]}"
