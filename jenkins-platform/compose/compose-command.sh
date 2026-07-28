#!/usr/bin/env bash

# Detect an available compose provider once and expose it as a bash array.
# Consumers should use: "${COMPOSE_CMD[@]}" <args>
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
  CONTAINER_CMD=(docker)
elif podman compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(podman compose)
  CONTAINER_CMD=(podman)
else
  echo 'No Compose provider found. Install Docker Compose (docker compose) or Podman Compose (podman compose).' >&2
  exit 1
fi
