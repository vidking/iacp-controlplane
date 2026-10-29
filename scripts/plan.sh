#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
payload="${IACP_PLAN_JSON:-{\"command\":\"plan\",\"variables\":{}}}"
curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"

