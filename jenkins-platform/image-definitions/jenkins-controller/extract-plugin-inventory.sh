#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/image-definitions/jenkins-controller/versions.env"
IMAGE_REF="${1:-${CONTROLLER_IMAGE}}"

cd "$ROOT_DIR"

source "${ROOT_DIR}/image-definitions/jenkins-controller/container-command.sh"

"${CONTAINER_CMD[@]}" run --rm --entrypoint bash "$IMAGE_REF" -lc '
  for plugin in /usr/share/jenkins/ref/plugins/*.jpi /usr/share/jenkins/ref/plugins/*.hpi; do
    [ -f "$plugin" ] || continue
    id=$(unzip -p "$plugin" META-INF/MANIFEST.MF | sed -n "s/^Short-Name: //p" | tr -d "\r")
    version=$(unzip -p "$plugin" META-INF/MANIFEST.MF | sed -n "s/^Plugin-Version: //p" | tr -d "\r")
    printf "%s:%s\n" "$id" "$version"
  done | sort -u
'
