#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${IACP_TEST_DATABASE_URL:-}" ]]; then
  echo "Define IACP_TEST_DATABASE_URL con una base PostgreSQL cuyo nombre incluya _test" >&2
  exit 2
fi
python -m pytest -q

