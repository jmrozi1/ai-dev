#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose/jenkins-controller.compose.yaml"
CONTROLLER_CONTAINER_NAME="jenkins-controller-dev"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"

cd "$ROOT_DIR"

source "${ROOT_DIR}/compose/compose-command.sh"

print_diagnostics() {
  echo 'Compose service status:' >&2
  "${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" ps >&2 || true
  echo 'Controller logs:' >&2
  "${CONTAINER_CMD[@]}" logs "$CONTROLLER_CONTAINER_NAME" >&2 || true
}

if ! "${CONTAINER_CMD[@]}" inspect "$CONTROLLER_CONTAINER_NAME" >/dev/null 2>&1; then
  echo 'Controller container is not running. Start it first with compose/start-controller.sh.' >&2
  print_diagnostics
  exit 1
fi

deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  status="$("${CONTAINER_CMD[@]}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTROLLER_CONTAINER_NAME")"
  if [[ "$status" == "healthy" ]]; then
    echo 'Controller is healthy.'
    exit 0
  fi

  if [[ "$status" == "unhealthy" ]]; then
    echo 'Controller became unhealthy before reaching ready state.' >&2
    print_diagnostics
    exit 1
  fi

  sleep 2
done

echo "Timed out after ${TIMEOUT_SECONDS}s waiting for controller health status to become healthy." >&2
print_diagnostics
exit 1
