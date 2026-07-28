#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
IMAGE_REF="${1:-${CONTROLLER_IMAGE}}"
LOCK_FILE="${ROOT_DIR}/image-definitions/jenkins-controller/plugins.lock"
EXPECTED_JENKINS_CORE="${JENKINS_VERSION}"
ACTUAL_INVENTORY_FILE="$(mktemp)"
trap 'rm -f "$ACTUAL_INVENTORY_FILE"' EXIT

cd "$ROOT_DIR"

if [[ ! -f "$LOCK_FILE" ]]; then
  echo "Missing lock file: ${LOCK_FILE}" >&2
  exit 1
fi

actual_core="$("${ROOT_DIR}/image-definitions/jenkins-controller/extract-jenkins-core-version.sh" "$IMAGE_REF")"
if [[ "$actual_core" != "$EXPECTED_JENKINS_CORE" ]]; then
  echo "Jenkins core version mismatch for ${IMAGE_REF}: expected ${EXPECTED_JENKINS_CORE}, got ${actual_core}." >&2
  exit 1
fi

"${ROOT_DIR}/image-definitions/jenkins-controller/extract-plugin-inventory.sh" "$IMAGE_REF" > "$ACTUAL_INVENTORY_FILE"

if ! diff -u "$LOCK_FILE" "$ACTUAL_INVENTORY_FILE"; then
  echo
  echo "Plugin lock mismatch for ${IMAGE_REF}." >&2
  echo "Regenerate lock intentionally with: ./image-definitions/jenkins-controller/regenerate-plugin-lock.sh" >&2
  exit 1
fi

echo "Jenkins core version verified: ${actual_core}"
echo "Plugin inventory matches lock: ${LOCK_FILE}"
