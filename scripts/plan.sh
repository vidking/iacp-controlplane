#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
name="${IACP_NAME:-iacp-demo}"
if [[ -n "${IACP_REQUEST_JSON:-}" ]]; then
  payload="$IACP_REQUEST_JSON"
else
  payload="$(IACP_NAME="$name" \
    IACP_ENVIRONMENT="${IACP_ENVIRONMENT:-lab}" \
    IACP_PLAN_ID="${IACP_PLAN_ID:-kvm-2}" \
    IACP_IMAGE_ID="${IACP_IMAGE_ID:-ubuntu-2404}" \
    IACP_NETWORK_PROFILE="${IACP_NETWORK_PROFILE:-lab_dhcp}" \
    IACP_TAGS="${IACP_TAGS:-}" \
    IACP_SSH_PUBLIC_KEY="${IACP_SSH_PUBLIC_KEY:-}" \
    IACP_ROOT_PASSWORD="${IACP_ROOT_PASSWORD:-}" \
    IACP_START_ON_CREATE="${IACP_START_ON_CREATE:-false}" \
    python3 -c 'import json, os
access = {
    "ssh_public_keys": [os.environ["IACP_SSH_PUBLIC_KEY"]] if os.environ["IACP_SSH_PUBLIC_KEY"] else [],
}
if os.environ["IACP_ROOT_PASSWORD"]:
    access["root_password"] = os.environ["IACP_ROOT_PASSWORD"]
body = {
    "name": os.environ["IACP_NAME"],
    "environment": os.environ["IACP_ENVIRONMENT"],
    "plan_id": os.environ["IACP_PLAN_ID"],
    "image_id": os.environ["IACP_IMAGE_ID"],
    "network_profile": os.environ["IACP_NETWORK_PROFILE"],
    "tags": [item.strip() for item in os.environ["IACP_TAGS"].split(",") if item.strip()],
    "access": access,
    "start_on_create": os.environ["IACP_START_ON_CREATE"].lower() == "true",
}
print(json.dumps(body))')"
fi

idempotency_key="${IACP_IDEMPOTENCY_KEY:-iacp-create-${name}}"
curl_args=(-fsS -X POST "${api_url}/v1/instances" -H 'Content-Type: application/json' -H "Idempotency-Key: ${idempotency_key}" -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
