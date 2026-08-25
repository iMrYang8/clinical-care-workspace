#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
expected_project="$("$root/scripts/demo-project-name.sh")"
fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
project="${1:-$expected_project}"
mode="${2:-local}"

if [[ "$project" != "$expected_project" ]]; then
  if [[ "$mode" != "--temporary" || ! "$project" =~ ^nightingale-(verify|release|test)-${fingerprint}-[0-9a-f]{16}$ ]]; then
    echo "Refusing ownership check for '$project'; expected '$expected_project' or a cryptographically scoped temporary project for this checkout." >&2
    exit 2
  fi
fi

containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
for container in $containers; do
  working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")"
  config_files="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$container")"
  if [[ "$working_dir" != "$root" ]]; then
    echo "Refusing local demo operation: container $container belongs to working_dir '$working_dir'." >&2
    exit 3
  fi
  if [[ -z "$config_files" || "$config_files" == *"compose.deploy.yml"* ]]; then
    echo "Refusing local demo operation: container $container has unsafe config_files '$config_files'." >&2
    exit 3
  fi
  IFS=',' read -r -a compose_files <<<"$config_files"
  for config_file in "${compose_files[@]}"; do
    case "$config_file" in
      "$root/compose.yml"|"$root/compose.override.yml") ;;
      "$root/compose.dev-tools.yml")
        if [[ "$mode" != "--temporary" ]]; then
          echo "Refusing local demo operation: container $container unexpectedly uses dev-tools." >&2
          exit 3
        fi
        ;;
      *)
        echo "Refusing local demo operation: container $container references '$config_file'." >&2
        exit 3
        ;;
    esac
  done
done

for resource_type in volume network; do
  resources="$(docker "$resource_type" ls -q --filter "label=com.docker.compose.project=$project")"
  for resource in $resources; do
    resource_fingerprint="$(docker "$resource_type" inspect --format '{{ index .Labels "com.nightingale.checkout_fingerprint" }}' "$resource")"
    if [[ "$resource_fingerprint" != "$fingerprint" ]]; then
      echo "Refusing local demo operation: $resource_type $resource lacks this checkout's fingerprint." >&2
      exit 3
    fi
  done
done
