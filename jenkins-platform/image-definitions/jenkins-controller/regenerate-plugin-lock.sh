#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
LOCK_FILE="${ROOT_DIR}/image-definitions/jenkins-controller/plugins.lock"
PLUGINS_TXT="${ROOT_DIR}/image-definitions/jenkins-controller/plugins.txt"
RESOLVER_IMAGE="jenkins-platform/jenkins-controller:resolver-$(date +%Y%m%d%H%M%S)-$RANDOM"
RESOLVER_CONTEXT_DIR="$(mktemp -d)"
TMP_LOCK="$(mktemp)"
RESOLVER_LOG="$(mktemp)"

cleanup() {
  rm -rf "$RESOLVER_CONTEXT_DIR"
  rm -f "$TMP_LOCK" "$RESOLVER_LOG"
  if [[ -n "${CONTAINER_CMD[*]:-}" ]]; then
    "${CONTAINER_CMD[@]}" image rm -f "$RESOLVER_IMAGE" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$ROOT_DIR"

if [[ ! -f "$PLUGINS_TXT" ]]; then
  echo "Missing direct plugin manifest: ${PLUGINS_TXT}" >&2
  exit 1
fi

source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"

cp "$PLUGINS_TXT" "${RESOLVER_CONTEXT_DIR}/plugins.txt"
cat > "${RESOLVER_CONTEXT_DIR}/Dockerfile" <<'EOF'
ARG JENKINS_BASE_IMAGE
FROM ${JENKINS_BASE_IMAGE}
COPY --chown=jenkins:jenkins plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
EOF

if ! "${CONTAINER_CMD[@]}" build --no-cache --build-arg "JENKINS_BASE_IMAGE=${JENKINS_BASE_IMAGE}" -t "$RESOLVER_IMAGE" "$RESOLVER_CONTEXT_DIR" 2>&1 | tee "$RESOLVER_LOG"; then
  echo 'Failed to resolve plugin lock from plugins.txt.' >&2
  exit 1
fi

# Heuristic: fail if resolver build output reports security warning text.
if grep -Eiq 'security warning|security warnings' "$RESOLVER_LOG"; then
  echo 'Detected potential jenkins-plugin-cli security warning while resolving plugins.lock.' >&2
  exit 1
fi

"${ROOT_DIR}/image-definitions/jenkins-controller/extract-plugin-inventory.sh" "$RESOLVER_IMAGE" > "$TMP_LOCK"

if [[ ! -s "$TMP_LOCK" ]]; then
  echo 'Refusing to write empty lock file from resolver image.' >&2
  exit 1
fi

mv "$TMP_LOCK" "$LOCK_FILE"

echo "Updated ${LOCK_FILE} from plugins.txt resolver build."
echo 'Review lock changes before commit.'
