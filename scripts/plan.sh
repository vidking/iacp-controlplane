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
    IACP_SSH_PUBLIC_KEY_FILE="${IACP_SSH_PUBLIC_KEY_FILE:-}" \
    IACP_ROOT_PASSWORD_FILE="${IACP_ROOT_PASSWORD_FILE:-}" \
    python3 -c 'import json, os
from pathlib import Path
key = os.environ["IACP_SSH_PUBLIC_KEY"]
if not key and os.environ["IACP_SSH_PUBLIC_KEY_FILE"]:
    key = Path(os.environ["IACP_SSH_PUBLIC_KEY_FILE"]).read_text().strip()
password = None
if os.environ["IACP_ROOT_PASSWORD_FILE"]:
    password = Path(os.environ["IACP_ROOT_PASSWORD_FILE"]).read_text().strip()
spec = {
    "name": os.environ["IACP_NAME"],
    "image": os.environ["IACP_IMAGE"],
    "vcpus": int(os.environ["IACP_VCPUS"]),
    "memory_mb": int(os.environ["IACP_MEMORY_MB"]),
    "disk_gb": int(os.environ["IACP_DISK_GB"]),
    "network_mode": os.environ["IACP_NETWORK_MODE"],
    "ssh_public_keys": [key] if key else [],
}
if password:
    spec["root_password"] = password
print(json.dumps({"command": "plan", "action": "provision", "stack": os.environ["IACP_STACK"], "spec": spec}))')"
fi

curl_args=(-fsS -X POST "${api_url}/v1/operations" -H 'Content-Type: application/json' -d "${payload}")
api_token="${IACP_API_TOKEN:-}"
if [[ -n "${IACP_API_TOKEN_FILE:-}" ]]; then api_token="$(<"$IACP_API_TOKEN_FILE")"; fi
if [[ -n "$api_token" ]]; then
  curl_args+=(-H "Authorization: Bearer ${api_token}")
fi
curl "${curl_args[@]}"
