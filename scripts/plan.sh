#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
if [[ -n "${IACP_PLAN_JSON:-}" ]]; then
  payload="$IACP_PLAN_JSON"
else
  payload="$(IACP_STACK="${IACP_STACK:-mng01}" \
    IACP_NAME="${IACP_NAME:-iacp-v2-demo}" \
    IACP_IMAGE="${IACP_IMAGE:-ubuntu-2404}" \
    IACP_VCPUS="${IACP_VCPUS:-2}" \
    IACP_MEMORY_MB="${IACP_MEMORY_MB:-2048}" \
    IACP_DISK_GB="${IACP_DISK_GB:-20}" \
    IACP_NETWORK_MODE="${IACP_NETWORK_MODE:-dhcp}" \
    IACP_SSH_PUBLIC_KEY="${IACP_SSH_PUBLIC_KEY:-}" \
    python3 -c 'import json, os
spec = {
    "name": os.environ["IACP_NAME"],
    "image": os.environ["IACP_IMAGE"],
    "vcpus": int(os.environ["IACP_VCPUS"]),
    "memory_mb": int(os.environ["IACP_MEMORY_MB"]),
    "disk_gb": int(os.environ["IACP_DISK_GB"]),
    "network_mode": os.environ["IACP_NETWORK_MODE"],
    "ssh_public_keys": [os.environ["IACP_SSH_PUBLIC_KEY"]] if os.environ["IACP_SSH_PUBLIC_KEY"] else [],
}
print(json.dumps({"command": "plan", "action": "provision", "stack": os.environ["IACP_STACK"], "spec": spec}))')"
fi

curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
if [[ -n "${IACP_API_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${IACP_API_TOKEN}")
fi
curl "${curl_args[@]}"
