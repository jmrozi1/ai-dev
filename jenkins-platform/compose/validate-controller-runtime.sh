#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/compose/jenkins-controller.compose.yaml"
CONTROLLER_CONTAINER_NAME="jenkins-controller-dev"
EXPECTED_PLUGINS_FILE="${ROOT_DIR}/image-definitions/jenkins-controller/plugins.txt"
EXPECTED_JENKINS_VERSION="2.516.3"
JENKINS_HTTP_PORT="${JENKINS_HTTP_PORT:-8080}"

cd "$ROOT_DIR"

source "${ROOT_DIR}/compose/compose-command.sh"

"${ROOT_DIR}/compose/wait-controller-healthy.sh"

reported_version="$({ curl -fsSI "http://127.0.0.1:${JENKINS_HTTP_PORT}/login" || true; } | tr -d '\r' | awk -F': ' 'tolower($1)=="x-jenkins" {print $2; exit}')"
if [[ -z "$reported_version" ]]; then
  echo 'Could not determine Jenkins version from HTTP response header X-Jenkins.' >&2
  exit 1
fi

if [[ "$reported_version" != "$EXPECTED_JENKINS_VERSION" ]]; then
  echo "Jenkins version mismatch: expected ${EXPECTED_JENKINS_VERSION}, got ${reported_version}." >&2
  exit 1
fi

echo "Jenkins core version verified: ${reported_version}"

if ! "${CONTAINER_CMD[@]}" inspect "$CONTROLLER_CONTAINER_NAME" >/dev/null 2>&1; then
  echo 'Controller container is not running.' >&2
  exit 1
fi

installed_plugins="$("${CONTAINER_CMD[@]}" exec "$CONTROLLER_CONTAINER_NAME" bash -lc '
  for plugin in /var/jenkins_home/plugins/*.jpi /var/jenkins_home/plugins/*.hpi; do
    [ -f "$plugin" ] || continue
    id=$(unzip -p "$plugin" META-INF/MANIFEST.MF | sed -n "s/^Short-Name: //p" | tr -d "\r")
    version=$(unzip -p "$plugin" META-INF/MANIFEST.MF | sed -n "s/^Plugin-Version: //p" | tr -d "\r")
    printf "%s:%s\n" "$id" "$version"
  done | sort
')"

missing=0
while IFS= read -r expected; do
  [[ -z "$expected" ]] && continue
  if ! grep -Fqx "$expected" <<< "$installed_plugins"; then
    echo "Missing or mismatched installed plugin: ${expected}" >&2
    missing=1
  fi
done < "$EXPECTED_PLUGINS_FILE"

if [[ $missing -ne 0 ]]; then
  echo 'Plugin verification failed against source-controlled expected versions.' >&2
  exit 1
fi

echo 'Pinned plugin versions verified against source-controlled expected versions.'
