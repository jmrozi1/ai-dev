#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

printf 'DEPRECATED: tools/bootstrap/bootstrap-linux.sh is deprecated.\n' >&2
printf 'Use scripts/install.sh instead (or run ai-dev apply).\n' >&2

exec "$REPO_ROOT/scripts/install.sh" "$@"
