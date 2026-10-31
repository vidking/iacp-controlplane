#!/usr/bin/env bash
set -euo pipefail

repository="${IACP_IMAGE_REPOSITORY:-scm.cyberfuel.com/cyberfuel/iacp-controlplane}"
tag="${IACP_IMAGE_TAG:-develop}"
platform="${IACP_IMAGE_PLATFORM:-linux/amd64}"

if [[ ! "$tag" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "IACP_IMAGE_TAG contiene caracteres no permitidos" >&2
  exit 2
fi

echo "Publicando ${repository}:${tag} para ${platform}"
docker buildx build \
  --file Dockerfile.technical \
  --platform "$platform" \
  --tag "${repository}:${tag}" \
  --label "org.opencontainers.image.title=IACP Control Plane Técnico" \
  --label "org.opencontainers.image.version=${tag}" \
  --provenance=false \
  --load \
  .
docker push "${repository}:${tag}"
