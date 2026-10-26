IACP_BASE_URL='https://iac.cyberfuel.com'
if [ -z "${IACP_API_TOKEN:-}" ]; then
  read -r -s -p 'API token autorizado del tenant: ' IACP_API_TOKEN; printf '\n'
fi
read -r -p 'Nombre único de VM (minúsculas, números, guiones): ' IACP_NAME
read -r -p 'Ruta de tu llave SSH pública para este recorrido: ' IACP_SSH_PUBLIC_KEY_FILE
export IACP_BASE_URL IACP_NAME IACP_SSH_PUBLIC_KEY_FILE
umask 077
IACP_WORK="$(mktemp -d)"; export IACP_WORK
trap 'rm -f "$IACP_WORK/request.json"; rmdir "$IACP_WORK" 2>/dev/null || true' EXIT
python3 - <<'PY'
import getpass, json, os, re
from pathlib import Path
body = {{pilot_request_python}}
name = os.environ['IACP_NAME']
assert 1 <= len(name) <= 63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', name), 'Nombre inválido'
password = getpass.getpass('Contraseña inicial root (16–128 caracteres): ')
assert 16 <= len(password) <= 128 and all(32 <= ord(c) <= 126 for c in password), 'Contraseña inválida'
public_key = Path(os.environ['IACP_SSH_PUBLIC_KEY_FILE']).expanduser().read_text().strip()
assert public_key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')), 'Se requiere una llave pública SSH'
body['name'] = name
body['access'] = {'root_password': password, 'ssh_public_keys': [public_key]}
Path(os.environ['IACP_WORK'], 'request.json').write_text(json.dumps(body))
PY
# Si Python informa un error, corregirlo antes de enviar. No cambiar clave/cuerpo tras un timeout.
if [ ! -f "$IACP_WORK/request.json" ]; then echo 'No se generó la solicitud'; exit 1; fi
IACP_KEY="${IACP_KEY:-piloto-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
curl --fail-with-body -sS -X POST "$IACP_BASE_URL/v1/instances" \
  -H "Authorization: Bearer $IACP_API_TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $IACP_KEY" --data-binary "@$IACP_WORK/request.json" \
  --dump-header respuesta.headers --output respuesta.json
rm -f "$IACP_WORK/request.json"
cat respuesta.json
