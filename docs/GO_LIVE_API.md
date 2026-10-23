# Consumo autenticado desde desarrollo

La [normalización operativa](OPERATIONS.md) agrega identidades Basic de rol docs,
sin acceso a /v1. Si se configura ese rol, el password de documentación y el token
HTTPBearer usado en Authorize son distintos. El modo legacy se conserva hasta
la rotación aprobada; no hay JWT ni htpasswd de Traefik en este mecanismo.

Base oficial: `https://iac.cyberfuel.com`. Requiere publicación, IP de salida CRM
o gateway corporativo y credencial del tenant. Ver [roles y exposición](GO_LIVE_SECURITY.md)
y [OpenAPI](openapi.json). No enviar tokens al frontend del CRM ni incluirlos en
URLs, Git o logs. Los ejemplos no crean instancias ni ejecutan energía por sí solos.

Preparar un archivo privado de cabeceras (0600) con `Authorization: Bearer <token>`.
Para el acceso administrativo fuera de la ruta CRM, agregar `Cookie: <sesión del
gateway>` obtenida por el flujo corporativo. Nunca usar -k. Una CA privada requiere
`CURL_CA_BUNDLE` y el trust store de los clientes, no desactivar validación.

```sh
export IACP_URL=https://iac.cyberfuel.com
export IACP_AUTH_HEADER_FILE=/ruta/privada/tenant.header
export INSTANCE_ID=a1b2d063-6240-4969-bc04-e7e0e6be9e73
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" "$IACP_URL/openapi.json"
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" "$IACP_URL/v1/instances"
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" "$IACP_URL/v1/instances/$INSTANCE_ID"
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" "$IACP_URL/v1/instances/$INSTANCE_ID/power"
```

GET lista/detalle/energía devuelve 200. guest_ready es null aunque power_state sea
running. La API no certifica SO, QGA, Cloud-Init ni conectividad del invitado.
Las consultas a PVE pueden devolver 503 sin mostrar credenciales o configuración.

Crear desde el CRM, únicamente con una asignación y proceso de provisión aprobados:

```sh
export CREATE_KEY=crm-order-IDENTIFICADOR-ESTABLE
curl --fail-with-body --silent --show-error -X POST \
  -H @"$IACP_AUTH_HEADER_FILE" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $CREATE_KEY" \
  --data '{"name":"nombre-asignado","image":"imagen-logica-aprobada","vcpus":2,"memory_mb":2048,"disk_gb":20}' \
  "$IACP_URL/v1/instances"
```

202 devuelve instance_id, operation_id y job_id. En runtime Terraform se prepara
el plan: **no equivale a Apply autorizado ni a una VM disponible**. No pasar
tenant_id, VMID, nodo, IP, token PVE o backend en el cuerpo. La asignación se
resuelve del lado servidor. Revisar el contrato existente de aprobación/Apply
con un operador y un approver independientes; esos endpoints requieren gateway.

Para energía, solo después de la certificación y activación descritas en
[el runbook](GO_LIVE_MVP.md), con operator y clave estable de la solicitud:

```sh
export POWER_KEY=crm-power-IDENTIFICADOR-ESTABLE
# Elegir UNA acción aprobada: start, shutdown o reboot. No encadenarlas como smoke.
export ACTION=start
curl --fail-with-body --silent --show-error -X POST \
  -H @"$IACP_AUTH_HEADER_FILE" -H "Idempotency-Key: $POWER_KEY" \
  "$IACP_URL/v1/instances/$INSTANCE_ID/power/$ACTION"
export OPERATION_ID=UUID_RECIBIDO_EN_EL_202
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" \
  "$IACP_URL/v1/operations/$OPERATION_ID"
```

Conservar clave, acción, cuerpo e IDs en el registro transaccional del CRM. Un
timeout o 503 ambiguo se sigue con GET operation y/o la **misma clave**, nunca
una nueva. El POST 202 solo acredita aceptación. Polling GET con espera acotada
consulta status/eventos/power hasta completed, failed o uncertain. Un uncertain
requiere investigación y conserva la reserva; no asumir fracaso y mandar otro
comando. Shutdown usa ACPI ordenado sin fuerza; reboot exige evidencia de tarea.

| HTTP | Interpretación |
|---|---|
| 200 | Lectura autorizada |
| 202 | Operación aceptada/replay; seguir operation_id |
| 401 | Token ausente/inválido o login del gateway requerido |
| 403 | Rol/tenant/grupo o fuente no autorizado |
| 404 | Inexistente/otro tenant/Origin sin router |
| 409 | Clave incompatible, reserva activa o contrato de ownership inválido |
| 413 / 429 | Límite de cuerpo / solicitudes; no cambiar la clave |
| 422 | Cuerpo/cabecera fuera del contrato |
| 503 | Admisión cerrada, dependencia no disponible o publicación incierta; conservar identidad de solicitud |

Un gateway puede responder redirect al login para un navegador sin sesión. Un
cliente HTTP debe detenerse antes de reenviar secretos a otro host. Ejemplo de
cliente estándar de lectura (token y Cookie en archivos privados):

```python
import json
import ssl
from pathlib import Path
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

token = Path('/ruta/privada/tenant.token').read_text().strip()
cookie = Path('/ruta/privada/gateway.cookie').read_text().strip()
opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
request = Request('https://iac.cyberfuel.com/v1/instances',
                  headers={'Authorization': 'Bearer ' + token, 'Cookie': cookie})
with opener.open(request, timeout=10) as response:
    instances = json.load(response)
```

Swagger: login/MFA del gateway → /docs → challenge usuario iacp/contraseña token
→ Authorize HTTPBearer. Una identidad reader puede explorar/consultar y recibe
403 al mutar. No usar Try it out sobre VM 106 antes de la ventana aprobada.
