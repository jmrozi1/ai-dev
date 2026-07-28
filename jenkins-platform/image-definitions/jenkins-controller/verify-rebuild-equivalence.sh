#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
EXPECTED_JENKINS_CORE="${JENKINS_VERSION}"
LOCK_FILE="${ROOT_DIR}/image-definitions/jenkins-controller/plugins.lock"
IMAGE_PREFIX="${CONTROLLER_IMAGE%:*}"
UNIQUE_SUFFIX="$(date +%Y%m%d%H%M%S)-$RANDOM"
IMAGE_A="${IMAGE_PREFIX}:equiv-a-${UNIQUE_SUFFIX}"
IMAGE_B="${IMAGE_PREFIX}:equiv-b-${UNIQUE_SUFFIX}"
BUILD_LOG_A="$(mktemp)"
BUILD_LOG_B="$(mktemp)"
CORE_A_FILE="$(mktemp)"
CORE_B_FILE="$(mktemp)"
INV_A_FILE="$(mktemp)"
INV_B_FILE="$(mktemp)"

cleanup() {
  source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"
  "${CONTAINER_CMD[@]}" image rm -f "$IMAGE_A" "$IMAGE_B" >/dev/null 2>&1 || true
  rm -f "$BUILD_LOG_A" "$BUILD_LOG_B" "$CORE_A_FILE" "$CORE_B_FILE" "$INV_A_FILE" "$INV_B_FILE"
}
trap cleanup EXIT

cd "$ROOT_DIR"

if [[ ! -f "$LOCK_FILE" ]]; then
  echo "Missing lock file: ${LOCK_FILE}" >&2
  exit 1
fi

source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"

build_image() {
  local image_ref="$1"
  local log_file="$2"

  echo "Building ${image_ref}"
  if ! "${CONTAINER_CMD[@]}" build --no-cache \
    --build-arg "JENKINS_BASE_IMAGE=${JENKINS_BASE_IMAGE}" \
    -f image-definitions/jenkins-controller/Dockerfile \
    -t "$image_ref" \
    image-definitions/jenkins-controller 2>&1 | tee "$log_file"; then
    return 1
  fi

  # Heuristic: fail on security warning text while preserving full build output.
  if grep -Eiq 'security warning|security warnings' "$log_file"; then
    echo "Detected potential jenkins-plugin-cli security warning in build output for ${image_ref}." >&2
    return 1
  fi
}

build_image "$IMAGE_A" "$BUILD_LOG_A"
build_image "$IMAGE_B" "$BUILD_LOG_B"

"${ROOT_DIR}/image-definitions/jenkins-controller/extract-jenkins-core-version.sh" "$IMAGE_A" > "$CORE_A_FILE"
"${ROOT_DIR}/image-definitions/jenkins-controller/extract-jenkins-core-version.sh" "$IMAGE_B" > "$CORE_B_FILE"
"${ROOT_DIR}/image-definitions/jenkins-controller/extract-plugin-inventory.sh" "$IMAGE_A" > "$INV_A_FILE"
"${ROOT_DIR}/image-definitions/jenkins-controller/extract-plugin-inventory.sh" "$IMAGE_B" > "$INV_B_FILE"

core_a="$(cat "$CORE_A_FILE")"
core_b="$(cat "$CORE_B_FILE")"

if [[ "$core_a" != "$EXPECTED_JENKINS_CORE" || "$core_b" != "$EXPECTED_JENKINS_CORE" ]]; then
  echo "Jenkins core version mismatch. Expected ${EXPECTED_JENKINS_CORE}, got A=${core_a}, B=${core_b}." >&2
  exit 1
fi

if ! diff -u "$CORE_A_FILE" "$CORE_B_FILE"; then
  echo 'Rebuild mismatch: Jenkins core versions differ between build A and build B.' >&2
  exit 1
fi

if ! diff -u "$INV_A_FILE" "$INV_B_FILE"; then
  echo 'Rebuild mismatch: plugin inventories differ between build A and build B.' >&2
  exit 1
fi

if ! diff -u "$LOCK_FILE" "$INV_A_FILE"; then
  echo 'Build A inventory mismatch versus plugins.lock.' >&2
  exit 1
fi

if ! diff -u "$LOCK_FILE" "$INV_B_FILE"; then
  echo 'Build B inventory mismatch versus plugins.lock.' >&2
  exit 1
fi

echo "Equivalent installation verified for ${IMAGE_A} and ${IMAGE_B}."
echo "Jenkins core: ${EXPECTED_JENKINS_CORE}"
echo 'Complete plugin inventory matches plugins.lock.'
