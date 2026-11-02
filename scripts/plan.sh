#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
if [[ -n "${IACP_PLAN_JSON:-}" ]]; then
  payload="$IACP_PLAN_JSON"
else
  payload="$(IACP_NAME="${IACP_NAME:-iacp-v2-demo}" \
    IACP_PLAN_ID="${IACP_PLAN_ID:-kvm-2}" \
    IACP_IMAGE_ID="${IACP_IMAGE_ID:-ubuntu-2404}" \
    IACP_SSH_PUBLIC_KEY="${IACP_SSH_PUBLIC_KEY:-}" \
    IACP_ROOT_PASSWORD="${IACP_ROOT_PASSWORD:-}" \
    IACP_START_ON_CREATE="${IACP_START_ON_CREATE:-false}" \
    IACP_CUSTOMER_ID="${IACP_CUSTOMER_ID:-}" \
    IACP_CUSTOMER_TAG="${IACP_CUSTOMER_TAG:-}" \
    IACP_ORDER_ID="${IACP_ORDER_ID:-}" \
    IACP_PRODUCT_ID="${IACP_PRODUCT_ID:-}" \
    python3 -c 'import json, os
access = {
    "ssh_public_keys": [os.environ["IACP_SSH_PUBLIC_KEY"]] if os.environ["IACP_SSH_PUBLIC_KEY"] else [],
}
if os.environ["IACP_ROOT_PASSWORD"]:
    access["root_password"] = os.environ["IACP_ROOT_PASSWORD"]
body = {
    "name": os.environ["IACP_NAME"],
    "plan_id": os.environ["IACP_PLAN_ID"],
    "image_id": os.environ["IACP_IMAGE_ID"],
    "access": access,
    "start_on_create": os.environ["IACP_START_ON_CREATE"].lower() == "true",
}
commercial = {
    "customer_id": os.environ["IACP_CUSTOMER_ID"],
    "customer_tag": os.environ["IACP_CUSTOMER_TAG"],
    "order_id": os.environ["IACP_ORDER_ID"],
    "product_id": os.environ["IACP_PRODUCT_ID"],
}
if any(commercial.values()):
    if not all(commercial.values()):
        raise SystemExit("IACP_CUSTOMER_ID, IACP_CUSTOMER_TAG, IACP_ORDER_ID e IACP_PRODUCT_ID deben enviarse juntos")
    body["commercial_context"] = commercial
print(json.dumps(body))')"
fi

idempotency_key="${IACP_IDEMPOTENCY_KEY:-iacp-create-${IACP_NAME:-iacp-v2-demo}}"
curl_args=(-fsS -X POST "${api_url}/v1/instances" -H 'Content-Type: application/json' -H "Idempotency-Key: ${idempotency_key}" -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
