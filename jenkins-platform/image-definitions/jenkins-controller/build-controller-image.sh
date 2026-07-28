#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"

DEFAULT_IMAGE_NAME="${CONTROLLER_IMAGE%:*}"
DEFAULT_IMAGE_TAG="${CONTROLLER_IMAGE##*:}"
IMAGE_NAME="${IMAGE_NAME:-${DEFAULT_IMAGE_NAME}}"
IMAGE_TAG="${IMAGE_TAG:-${DEFAULT_IMAGE_TAG}}"

cd "$ROOT_DIR"

"${CONTAINER_CMD[@]}" build \
  --build-arg "JENKINS_BASE_IMAGE=${JENKINS_BASE_IMAGE}" \
  -f image-definitions/jenkins-controller/Dockerfile \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  image-definitions/jenkins-controller
