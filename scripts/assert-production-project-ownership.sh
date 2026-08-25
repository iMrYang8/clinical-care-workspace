#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fingerprint="$("$root/scripts/demo-project-name.sh" --fingerprint)"
project="${1:-}"

if [[ -z "$project" || ! "$project" =~ ^nightingale-release-${fingerprint}-[0-9a-f]{16}$ ]]; then
  echo "Refusing production-topology ownership check for '$project'; expected a cryptographically scoped release project for this checkout." >&2
  exit 2
fi

containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
for container in $containers; do
  working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")"
  config_files="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$container")"
  if [[ "$working_dir" != "$root" ]]; then
    echo "Refusing production-topology operation: container $container belongs to working_dir '$working_dir'." >&2
    exit 3
  fi
  expected_files="$root/compose.yml,$root/compose.deploy.yml"
  if [[ "$config_files" != "$expected_files" ]]; then
    echo "Refusing production-topology operation: container $container has config_files '$config_files', expected '$expected_files'." >&2
    exit 3
  fi
done

for resource_type in volume network; do
  resources="$(docker "$resource_type" ls -q --filter "label=com.docker.compose.project=$project")"
  for resource in $resources; do
    resource_fingerprint="$(docker "$resource_type" inspect --format '{{ index .Labels "com.nightingale.checkout_fingerprint" }}' "$resource")"
    if [[ "$resource_fingerprint" != "$fingerprint" ]]; then
      echo "Refusing production-topology operation: $resource_type $resource lacks this checkout's fingerprint." >&2
      exit 3
    fi
  done
done
