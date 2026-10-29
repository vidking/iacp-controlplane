#!/usr/bin/env bash
set -euo pipefail

repository="${IACP_IMAGE_REPOSITORY:-scm.cyberfuel.com/cyberfuel/iacp-controlplanev2}"
tag="${IACP_IMAGE_TAG:-develop}"
platform="${IACP_IMAGE_PLATFORM:-linux/amd64}"
registry="${repository%%/*}"

if [[ ! "$tag" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "IACP_IMAGE_TAG contiene caracteres no permitidos" >&2
  exit 2
fi

if [[ "${IACP_SKIP_COMPOSE_CHECK:-0}" != "1" ]]; then
  docker compose config >/dev/null
fi

echo "Publicando ${repository}:${tag} para ${platform}"
echo "Inicia sesión en ${registry} cuando Docker lo solicite."
docker login "$registry"
docker buildx build \
  --platform "$platform" \
  --tag "${repository}:${tag}" \
  --label "org.opencontainers.image.title=IACP Control Plane V2" \
  --label "org.opencontainers.image.version=${tag}" \
  --push \
  .
