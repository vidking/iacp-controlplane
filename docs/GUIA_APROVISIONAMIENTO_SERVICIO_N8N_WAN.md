# Guía de aprovisionamiento puntual de n8n por WAN en Dev

Revisión: 18 de noviembre de 2026  
Destino: `dev-iac.cyberfuel.com` → entorno lógico `lab` → clúster MNG  
Tipo de prueba: canaria puntual; no equivale a certificación física integral.

## 1. Propósito y causa del resultado LAN anterior

Esta guía crea un servicio n8n de prueba con una dirección WAN estática asignada por NetBox y acceso web por IP. El perfil de red se fija en `instance.network_profile`; `configuration.access_mode: "ip"` elige el modo de acceso web, pero no elige WAN.

La solicitud anterior envió `network_profile: "lab_dhcp"`, por eso el servicio recibió una dirección LAN DHCP (`192.168.3.163`). Para WAN, el request debe incluir `network_profile: "prod_static"` y el catálogo de Dev debe seguir mostrando ese perfil habilitado.

La creación aprovisiona recursos reales en el clúster MNG y reserva una dirección IP. Esta guía no elimina ni cambia servicios existentes; la limpieza es independiente y requiere autorización explícita.

## 2. Perfil WAN y marcadores

La consulta de solo lectura a `GET /v1/network-profiles` en Dev, compartida por el equipo de operaciones el 18 de noviembre de 2026, mostró `prod_static` habilitado, bridge `vmbr3`, IPAM `netbox`, prefijo `190.0.231.0/24` y gateway `190.0.231.1`. Repite el preflight antes de cada ensayo.

NetBox selecciona la dirección disponible. No incluyas IP, gateway, VLAN, bridge o VMID en el JSON. No uses `190.0.231.2`; está excluida de los ensayos por una discrepancia ARP documentada. `lab_dhcp` permanece en la LAN de pruebas y no valida WAN.

Marcadores del ejemplo:

- `RUN_ID`: sello UTC único generado por `date`.
- `PLAN_ID`: identificador disponible en `GET /v1/plans`; el ejemplo usa `kvm-2`.
- `IMAGE_ID`: imagen admitida por n8n y publicada en `GET /v1/images`; el ejemplo usa `ubuntu-2404`.
- Token API y contraseña root temporal: se leen sin eco. No los guardes en Git ni en evidencias.

## 3. Preflight de solo lectura

Ejecuta en Bash desde una estación autorizada. Estos comandos no crean el servicio:

```bash
set -euo pipefail
export IACP_BASE_URL='https://dev-iac.cyberfuel.com'
read -r -s -p 'Token API de Dev: ' IACP_API_TOKEN
printf '\n'
AUTH=(-H "Authorization: Bearer ${IACP_API_TOKEN}")

PROFILE_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/network-profiles")
printf '%s\n' "$PROFILE_JSON" | jq '.network_profiles[] | select(.profile_id == "prod_static")'
if ! printf '%s\n' "$PROFILE_JSON" | jq -e '
  any(.network_profiles[];
    .profile_id == "prod_static" and .enabled == true and
    .proxmox_bridge == "vmbr3" and .ipam == "netbox" and
    .prefix == "190.0.231.0/24" and .gateway == "190.0.231.1")
' >/dev/null; then
  printf '%s\n' 'BLOCKED: el perfil WAN no coincide con la configuración autorizada.' >&2
  exit 1
fi

curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/plans" | jq '.plans'
curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/images" | jq '.images'
curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/applications" |
  jq '.[] | select(.application_id == "n8n")'

OBS_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/observability")
if ! printf '%s\n' "$OBS_JSON" | jq -e '
  .max_concurrent == 6 and .active_operations == 0 and
  .queue_depth == 0 and .uncertain_operations == 0 and
  (.workers | length) == 6 and
  all(.workers[];
    .status == "idle" and .liveness == "healthy" and
    (.current_operation_id == null or .current_operation_id == ""))
' >/dev/null; then
  printf '%s\n' 'BLOCKED: no hay seis workers de infraestructura sanos e idle.' >&2
  exit 1
fi

SERVICE_WORKERS_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/service-workers")
if ! printf '%s\n' "$SERVICE_WORKERS_JSON" | python3 -c '
import datetime, json, sys
state = json.load(sys.stdin)
now = datetime.datetime.now(datetime.timezone.utc)
workers = state.get("workers", [])
def idle(worker):
    try:
        beat = datetime.datetime.fromisoformat(
            worker["last_heartbeat"].replace("Z", "+00:00")
        )
        age = (now - beat).total_seconds()
        return worker.get("status") == "idle" and not worker.get("job_id") and 0 <= age < 60
    except (KeyError, TypeError, ValueError):
        return False
ready = state.get("max_concurrent") == 6 and len(workers) == 6 and all(idle(w) for w in workers)
print("service_workers=PASS" if ready else "service_workers=BLOCKED")
sys.exit(0 if ready else 1)
'; then
  printf '%s\n' 'BLOCKED: se requieren seis workers de servicios idle con heartbeat reciente.' >&2
  exit 1
fi
printf '%s\n' 'Preflight WAN, API y workers: PASS'
```

Si falla cualquiera de las verificaciones, no envíes el `POST`. No reduzcas capacidad ni interrumpas workers compartidos. Para una prueba física se necesita aislamiento por cola, proceso, instancia y credenciales de prueba; una consulta del catálogo no demuestra conectividad WAN.

## 4. Preparar la solicitud de servicio

El ejemplo crea n8n con Ubuntu 24.04, `kvm-2`, acceso web por IP y perfil WAN estático. La contraseña root es temporal y sirve para el acceso del sistema operativo; n8n crea su propia cuenta propietaria en la interfaz. El proxy no entrega una contraseña de propietario para n8n.

```bash
APP_ID='n8n'
PLAN_ID='kvm-2'
IMAGE_ID='ubuntu-2404'
RUN_ID=$(date -u +%Y%m%d%H%M%S)
SERVICE_NAME="prueba-service-n8n-wan-${RUN_ID}"
IDEMPOTENCY_KEY="crear-n8n-wan-${RUN_ID}"
read -r -s -p 'Contraseña root temporal (mínimo 12 caracteres): ' IACP_ROOT_PASSWORD
printf '\n'

umask 077
REQUEST_FILE=$(mktemp "${TMPDIR:-/tmp}/iacp-n8n-wan.XXXXXX")
RESPONSE_FILE=$(mktemp "${TMPDIR:-/tmp}/iacp-n8n-wan-response.XXXXXX")
trap 'rm -f -- "$REQUEST_FILE" "$RESPONSE_FILE"' EXIT
printf '%s' "$IACP_ROOT_PASSWORD" | jq -n \
  --arg application_id "$APP_ID" \
  --arg name "$SERVICE_NAME" \
  --arg plan_id "$PLAN_ID" \
  --arg image_id "$IMAGE_ID" \
  --arg tag "wan-smoke-${RUN_ID}" \
  --rawfile root_password /dev/stdin \
  '{
    application_id: $application_id,
    instance: {
      name: $name,
      plan_id: $plan_id,
      image_id: $image_id,
      environment: "lab",
      network_profile: "prod_static",
      access: {
        root_password: $root_password,
        ssh_public_keys: []
      },
      tags: [$tag],
      start_on_create: true
    },
    configuration: {access_mode: "ip"}
  }' > "$REQUEST_FILE"
chmod 600 "$REQUEST_FILE"
unset IACP_ROOT_PASSWORD
```

Verifica antes de enviar que `instance.network_profile` sea `prod_static` y que `configuration.access_mode` sea `ip`. No agregues `domain` a este modo. No reutilices el nombre o la llave de idempotencia de la prueba LAN anterior.

## 5. Crear y seguir el trabajo

Este `POST` crea el servicio. Una respuesta HTTP `202` solo significa que la intención fue aceptada; hay que esperar a que el trabajo termine.

```bash
HTTP_CODE=$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
  -X POST "${IACP_BASE_URL}/v1/service-instances" \
  "${AUTH[@]}" \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary "@${REQUEST_FILE}")
if [ "$HTTP_CODE" != '202' ]; then
  printf 'POST no aceptado: HTTP %s. No crees otro servicio con una clave nueva.\n' "$HTTP_CODE" >&2
  exit 1
fi
RESPONSE=$(<"$RESPONSE_FILE")
printf '%s\n' "$RESPONSE" | jq '{service_id, job_id, instance_id, operation_id, status, idempotency_key}'
SERVICE_ID=$(jq -er '.service_id' <<<"$RESPONSE")
JOB_ID=$(jq -er '.job_id' <<<"$RESPONSE")
INSTANCE_ID=$(jq -er '.instance_id' <<<"$RESPONSE")
```

Consulta el trabajo cada 10 segundos, hasta 45 minutos:

```bash
for attempt in $(seq 1 270); do
  JOB=$(curl --fail-with-body -sS "${AUTH[@]}" \
    "${IACP_BASE_URL}/v1/service-jobs/${JOB_ID}")
  JOB_STATUS=$(jq -r '.status' <<<"$JOB")
  printf 'Trabajo %s: %s\n' "$JOB_ID" "$JOB_STATUS"
  case "$JOB_STATUS" in
    queued|running) sleep 10 ;;
    succeeded) break ;;
    failed|blocked|uncertain|cancelled)
      printf '%s\n' "$JOB" | jq '{job_id, status, error}'
      exit 1
      ;;
    *) printf 'BLOCKED: estado no reconocido: %s\n' "$JOB_STATUS" >&2; exit 1 ;;
  esac
done
if [ "$JOB_STATUS" != 'succeeded' ]; then
  printf '%s\n' 'BLOCKED: venció el plazo; no reintentes ni crees otro servicio.' >&2
  exit 1
fi
```

Si el `POST` o el job quedan en estado desconocido, detén nuevas inyecciones y conserva los IDs. No repitas el aprovisionamiento con otra llave, no canceles un job en ejecución y no liberes la IP manualmente. Una operación `uncertain` necesita recuperación controlada.

## 6. Verificar red, servicio y TLS

Consulta el detalle del servicio y luego el endpoint de red de su instancia. Deben aparecer el perfil `prod_static`, una asignación NetBox `allocated`, el gateway autorizado y una dirección del prefijo que no sea `.1` ni `.2`:

```bash
SERVICE_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}")
printf '%s\n' "$SERVICE_JSON" |
  jq '{service_id, application_id, status, infrastructure_status,
       access_url, vmid, configuration}'

NETWORK_JSON=$(curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/instances/${INSTANCE_ID}/network")
printf '%s\n' "$NETWORK_JSON" |
  jq '{network_profile, configured_ipv4_cidr, configured_ipv4_gateway,
       observed_ipv4, status, network_allocation}'
printf '%s\n' "$NETWORK_JSON" | jq -e '
  (.network_allocation.address | split("/")[0]) as $ip |
  (.network_profile == "prod_static" and
   .network_allocation.provider == "netbox" and
   .network_allocation.profile == "prod_static" and
   .network_allocation.status == "allocated" and
   .network_allocation.gateway == "190.0.231.1" and
   ($ip | startswith("190.0.231.")) and $ip != "190.0.231.1" and
   $ip != "190.0.231.2")
' >/dev/null
```

El resultado esperado del servicio es `status: "awaiting_customer"`, `infrastructure_status: "ready"` y `access_url` con la dirección WAN asignada. `access_mode: "ip"` debe conservarse en `configuration`; no confundas una URL por IP con la selección de la red.

Para validar HTTPS del piloto, descarga únicamente el certificado público de la CA a un archivo local protegido y úsalo para comprobar el `access_url`:

```bash
CA_FILE="$PWD/iacp-pilot-ca-${RUN_ID}.crt"
test ! -e "$CA_FILE" || {
  printf 'El archivo ya existe; no se sobrescribe: %s\n' "$CA_FILE" >&2
  exit 1
}
umask 077
curl --fail-with-body -sS "${AUTH[@]}" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}/tls-ca" \
  -o "$CA_FILE"
ACCESS_URL=$(jq -er '.access_url' <<<"$SERVICE_JSON")
curl --cacert "$CA_FILE" --fail -sS -o /dev/null \
  -w 'HTTPS=%{http_code}\n' "$ACCESS_URL"
```

En n8n, abre el `access_url` desde un punto de prueba autorizado y crea la cuenta propietaria en la pantalla inicial; valida después el inicio de sesión. No es la contraseña root ni el token API. El endpoint `POST /v1/service-instances/{service_id}/credentials` devuelve instrucciones de alta y marca la consulta como consumida; úsalo como máximo una vez y no esperes una contraseña de propietario para n8n.

La asignación de IP y un TLS válido no prueban que el gateway WAN sea alcanzable. Comprueba la salida WAN y acceso desde el punto autorizado. Si falla gateway, DNS, HTTPS o login, conserva el servicio y la reserva, registra `FAIL` o `BLOCKED` y detén nuevas inyecciones; no cambies a `lab_dhcp` para convertir la prueba en PASS.

## 7. Resultado y límites

Registra fecha UTC, revisión realmente servida, `RUN_ID`, `service_id`, `job_id`, `instance_id`, perfil de red, VMID, dirección asignada y evidencias de WAN, TLS y autenticación funcional. No registres tokens, contraseña root, contraseñas del propietario ni secretos de aplicación.

Esta solicitud individual es una prueba de humo, no la matriz de certificación. La matriz física vigente requiere aislamiento, workers sanos e idle, seis aprovisionamientos simultáneos y verificación de reinicio, persistencia, entrega y liberación de IP. El runner de matriz limpia sus propias canarias; esa eliminación requiere autorización explícita y no forma parte de esta guía. Producción permanece excluida.
