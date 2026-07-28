#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose/jenkins-controller.compose.yaml"

cd "$ROOT_DIR"

source "${ROOT_DIR}/compose/compose-command.sh"

"${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" down --volumes --remove-orphans
