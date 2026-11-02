#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${IACP_PLAN_OPERATION_ID:-}" && -z "${IACP_INSTANCE_ID:-}" ]]; then
  echo "Uso: IACP_INSTANCE_ID=<id> ./scripts/destroy.sh" >&2
  exit 2
fi
api_url="${IACP_API_URL:-http://localhost:8080}"
if [[ -n "${IACP_INSTANCE_ID:-}" ]]; then
  idempotency_key="${IACP_IDEMPOTENCY_KEY:-iacp-destroy-${IACP_INSTANCE_ID}}"
  curl_args=(-fsS -X DELETE "${api_url}/v1/instances/${IACP_INSTANCE_ID}" -H "Idempotency-Key: ${idempotency_key}")
  if [[ -n "${IACP_API_TOKEN:-}" ]]; then
    curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
  fi
  curl "${curl_args[@]}"
  exit 0
fi
payload="$(IACP_PLAN_OPERATION_ID="${IACP_PLAN_OPERATION_ID:-}" IACP_INSTANCE_ID="${IACP_INSTANCE_ID:-}" python3 -c 'import json, os
body = {"command": "destroy", "action": "destroy"}
if os.environ["IACP_PLAN_OPERATION_ID"]: body["plan_operation_id"] = os.environ["IACP_PLAN_OPERATION_ID"]
if os.environ["IACP_INSTANCE_ID"]: body["instance_id"] = os.environ["IACP_INSTANCE_ID"]
print(json.dumps(body))')"
curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
