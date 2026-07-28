#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
IMAGE_REF="${1:-${CONTROLLER_IMAGE}}"

cd "$ROOT_DIR"

source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"

"${CONTAINER_CMD[@]}" run --rm --entrypoint bash "$IMAGE_REF" -lc 'echo "$JENKINS_VERSION"'
