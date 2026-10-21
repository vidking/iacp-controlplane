#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
test_project="iacp-test-$(date +%s)-$$"
cleanup() {
  docker compose --ansi never -p "$test_project" -f compose.test.yaml down --remove-orphans
}
trap cleanup EXIT
docker compose --ansi never --progress plain -p "$test_project" -f compose.test.yaml run --build --rm -T tests
