#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${IACP_PLAN_OPERATION_ID:-}" ]]; then
  echo "Uso: IACP_PLAN_OPERATION_ID=<id-del-plan> ./scripts/apply.sh" >&2
  exit 2
fi
api_url="${IACP_API_URL:-http://localhost:8080}"
payload="$(IACP_PLAN_OPERATION_ID="$IACP_PLAN_OPERATION_ID" python3 -c 'import json, os; print(json.dumps({"command":"apply", "action":"provision", "plan_operation_id":os.environ["IACP_PLAN_OPERATION_ID"]}))')"
curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
