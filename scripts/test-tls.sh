#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker_endpoint="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
case "$docker_endpoint" in unix://*|npipe://*) ;; *) echo 'La prueba TLS requiere Docker local.' >&2; exit 1 ;; esac
project="iacp-tls-test-$(date +%s)-$$"
cleanup() {
  # Solo contenedores/red del proyecto aleatorio; nunca eliminar volúmenes.
  docker compose -p "$project" -f compose.tls.test.yaml down --remove-orphans
}
trap cleanup EXIT
docker compose --ansi never --progress plain -p "$project" -f compose.tls.test.yaml up --build -d traefik
docker compose --ansi never -p "$project" -f compose.tls.test.yaml run --build --rm -T --no-deps tls-client
