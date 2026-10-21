#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $(id -u) -ne 0 ]]; then echo 'Ejecutar con sudo: sudo ./scripts/create-secrets.sh'; exit 1; fi
umask 077
install -d -m 0700 secrets
for key in db_password api_token; do
  if [[ -e "secrets/$key" ]]; then echo "Existing secrets/$key: unchanged"; continue; fi
  openssl rand -hex 32 > "secrets/$key"
  chown 0:10001 "secrets/$key"
  chmod 0640 "secrets/$key"
done
echo 'Secrets generated locally (never commit them).'
