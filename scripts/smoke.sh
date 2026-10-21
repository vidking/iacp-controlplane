#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
BASE_URL="${IACP_BASE_URL:-http://127.0.0.1:8080}"
TOKEN="$(sudo cat secrets/api_token)"
JOB="$(curl -fsS -X POST "$BASE_URL/v1/jobs/smoke" -H "Authorization: Bearer $TOKEN")"
printf '%s\n' "$JOB"
ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"$JOB")"
for i in $(seq 1 15); do
  RESP="$(curl -fsS "$BASE_URL/v1/jobs/$ID" -H "Authorization: Bearer $TOKEN")"
  echo "$RESP"
  grep -q '"status":"completed"' <<<"$RESP" && exit 0
  sleep 1
done
exit 1
