#!/usr/bin/env bash
set -euo pipefail

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

resolve_python() {
  if [[ -n "${AI_DEV_PYTHON:-}" ]]; then
    printf '%s' "$AI_DEV_PYTHON"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  return 1
}

python_executable="$(resolve_python || true)"
if [[ -z "$python_executable" ]]; then
  printf 'bootstrap-ai-dev.sh: Python 3 was not found. Set AI_DEV_PYTHON or install python3.\n' >&2
  exit 1
fi

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
  --command-name ai-dev
