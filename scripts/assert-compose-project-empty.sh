#!/usr/bin/env bash

set -euo pipefail

project="${1:-}"
if [[ -z "$project" || ! "$project" =~ ^[a-z0-9][a-z0-9_-]+$ ]]; then
  echo "A valid explicit Compose project name is required." >&2
  exit 2
fi

containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
networks="$(docker network ls -q --filter "label=com.docker.compose.project=$project")"
volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
if [[ -n "$containers" || -n "$networks" || -n "$volumes" ]]; then
  echo "Refusing to reuse occupied Compose project '$project'." >&2
  exit 3
fi
