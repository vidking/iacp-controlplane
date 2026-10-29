#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${IACP_PLAN_OPERATION_ID:-}" ]]; then
  echo "Uso: IACP_PLAN_OPERATION_ID=<id-del-plan> ./scripts/apply.sh" >&2
  exit 2
fi
api_url="${IACP_API_URL:-http://localhost:8080}"
payload="{\"command\":\"apply\",\"plan_operation_id\":\"${IACP_PLAN_OPERATION_ID}\"}"
curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"

