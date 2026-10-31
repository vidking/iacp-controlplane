#!/usr/bin/env bash
set -euo pipefail

api_url="${IACP_API_URL:-http://localhost:8080}"
curl_args=(-fsS "${api_url}/v1/observability")
api_token="${IACP_API_TOKEN:-}"
if [[ -n "${IACP_API_TOKEN_FILE:-}" ]]; then api_token="$(<"$IACP_API_TOKEN_FILE")"; fi
if [[ -n "$api_token" ]]; then
  curl_args+=(-H "Authorization: Bearer ${api_token}")
fi
curl "${curl_args[@]}"
