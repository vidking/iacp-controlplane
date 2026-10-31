#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'docker compose -f compose.technical.test.yaml down --remove-orphans' EXIT
docker compose -f compose.technical.test.yaml up --build --abort-on-container-exit --exit-code-from tests
