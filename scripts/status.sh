#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
curl_args=(-fsS "${api_url}/v1/observability")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
