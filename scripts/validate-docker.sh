#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# Solo Docker local; no utiliza contextos SSH/TCP ni el stack del servidor.
docker_endpoint="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
case "$docker_endpoint" in
  unix://*|npipe://*) ;;
  *) echo 'La validación requiere un endpoint Docker local.' >&2; exit 1 ;;
esac

validation_project="iacp-dev-check-$(date +%s)-$$"
dev() {
  IACP_DEV_HTTP_PORT=0 docker compose --ansi never --progress plain \
    --env-file .env.dev.example -p "$validation_project" -f compose.dev.yaml "$@"
}
cleanup() {
  dev down --remove-orphans
  echo "Conservé los volúmenes de desarrollo del proyecto $validation_project."
}
trap cleanup EXIT

bash scripts/test-integration.sh
dev config --quiet
dev build dev-api
dev up -d --wait --wait-timeout 120 dev-db dev-redis
dev run --rm -T --no-deps -e IACP_REHEARSAL=1 dev-check python /checks/rehearse_transition.py seed
dev up -d --wait --wait-timeout 120
dev run --rm -T --no-deps dev-check
dev run --rm -T --no-deps -e IACP_REHEARSAL=1 dev-check python /checks/rehearse_transition.py verify

volume_names=("${validation_project}_dev_postgres_data" "${validation_project}_dev_redis_data" "${validation_project}_dev_secrets")
before_volumes="$(docker volume inspect --format '{{.Name}} {{.CreatedAt}}' "${volume_names[@]}")"
dev stop dev-traefik dev-api dev-worker dev-db dev-redis
dev up -d --force-recreate --wait --wait-timeout 120
dev run --rm -T --no-deps -e IACP_REHEARSAL=1 dev-check python /checks/rehearse_transition.py verify
dev run --rm -T --no-deps dev-check
after_volumes="$(docker volume inspect --format '{{.Name}} {{.CreatedAt}}' "${volume_names[@]}")"
[[ "$before_volumes" == "$after_volumes" ]]
echo 'Validación Docker aprobada: suite, imagen de runtime, Traefik y persistencia tras recreación.'
