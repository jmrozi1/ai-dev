#!/usr/bin/env bash

if command -v docker >/dev/null 2>&1; then
  CONTAINER_CMD=(docker)
elif command -v podman >/dev/null 2>&1; then
  CONTAINER_CMD=(podman)
else
  echo 'No container runtime found. Install Docker or Podman.' >&2
  exit 1
fi
