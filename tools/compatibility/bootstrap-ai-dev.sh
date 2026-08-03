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
repo_root="$(cd "$script_dir/../.." && pwd)"

printf 'DEPRECATED: tools/compatibility/bootstrap-ai-dev.sh is deprecated.\n' >&2
printf 'Use scripts/install.sh instead (or run ai-dev apply).\n' >&2

exec "$repo_root/scripts/install.sh" "$@"
