#!/usr/bin/env bash
set -euo pipefail

show_help() {
	cat <<'EOF'
Usage: scripts/install.sh [bootstrap-options]

Install or refresh the canonical ai-dev launcher.

This wrapper calls:
  python -m ai_dev_flow.bootstrap --platform posix --repo-root <this-repo>

Options:
  -h, --help  Show this help and exit.

Any additional options are forwarded to ai_dev_flow.bootstrap.
Common examples:
  scripts/install.sh
  scripts/install.sh --home "$HOME" --install-dir "$HOME/.local/bin"

Minimum Python version: 3.8

After installation, use `ai-dev` as the normal interface.
EOF
}

for arg in "$@"; do
	case "$arg" in
		-h|--help)
			show_help
			exit 0
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

exec "$python_executable" -m ai_dev_flow.bootstrap \
	--platform posix \
	--repo-root "$repo_root" \
	--python "$python_executable" \
	--command-name ai-dev \
	"$@"
