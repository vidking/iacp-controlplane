#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test_project="iacp-test-$(date +%s)-$$"
cleanup() {
  docker compose -p "$test_project" -f compose.test.yaml down --remove-orphans
}
trap cleanup EXIT
docker compose -p "$test_project" -f compose.test.yaml run --build --rm tests
