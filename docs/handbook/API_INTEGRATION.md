# Integración del backend CRM

Contrato versionado: [OpenAPI](../openapi.json), generado desde FastAPI y comparado
por las pruebas. El servidor oficial del contrato es **https://iac.cyberfuel.com**.
El CRM no debe usar IP del host, puerto diagnóstico, Redis, PostgreSQL ni workers.

Cada identidad se configura en el servidor con digest SHA-256 del token, sujeto,
tenant y rol. El backend conserva el token en su gestor de secretos y lo envía
como `Authorization: Bearer`. No reenviar el token al navegador. La aplicación
rechaza un `tenant_id` en el cuerpo de creación; el tenant proviene del token.

Estos ejemplos requieren curl y Python 3. Las variables se cargan fuera de Git;
los archivos de petición deben contener valores de laboratorio aprobados. No usar
`set -x`, no guardar Authorization en logs y no desactivar la verificación TLS.
Configurar `CURL_CA_BUNDLE` si el certificado usa una CA corporativa.

```sh
export IACP_API_BASE=https://iac.cyberfuel.com
# IACP_SERVICE_TOKEN y IACP_APPROVER_TOKEN provienen del gestor de secretos.
# LAB_REQUEST_FILE contiene name, image, vcpus, memory_mb y disk_gb autorizados.
: "${IACP_SERVICE_TOKEN:?Falta la identidad de servicio}"
: "${LAB_REQUEST_FILE:?Falta la petición aprobada}"
: "${CREATE_KEY:?Define y conserva la clave idempotente de creación}"
curl --fail-with-body --silent --show-error -D response.headers \
  -H "Authorization: Bearer $IACP_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $CREATE_KEY" \
  --data-binary "@$LAB_REQUEST_FILE" "$IACP_API_BASE/v1/instances" > accepted.json
```

HTTP 202 devuelve `instance_id`, `operation_id`, `job_id`, `executor` y `status`.
Conservar esos IDs y `X-Request-ID`. La misma clave y cuerpo devuelve los mismos
IDs sin republicar; otro cuerpo con la misma clave devuelve 409. El nombre es
único por tenant. Después de un timeout repetir exactamente la solicitud; generar
una clave nueva puede crear otra intención.

Validación de recursos: nombre DNS de 1–63 caracteres; imagen lógica de 1–63;
CPU 1–64 (2 por defecto); memoria 512–262144 MiB, múltiplo de 256 (2048 por
defecto); disco 8–4096 GiB (20 por defecto). En provisioning se exige además el mínimo
del disco de la plantilla autorizado. Estos defaults de recursos no seleccionan
red, almacenamiento, plantilla, IP ni credenciales.

```sh
INSTANCE_ID=$(python3 -c 'import json; print(json.load(open("accepted.json"))["instance_id"])')
OPERATION_ID=$(python3 -c 'import json; print(json.load(open("accepted.json"))["operation_id"])')
JOB_ID=$(python3 -c 'import json; print(json.load(open("accepted.json"))["job_id"])')
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" "$IACP_API_BASE/v1/jobs/$JOB_ID"
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" "$IACP_API_BASE/v1/operations/$OPERATION_ID"
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" "$IACP_API_BASE/v1/instances/$INSTANCE_ID"
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" "$IACP_API_BASE/v1/instances?limit=50&offset=0"
```

`GET /v1/jobs/{id}` conserva el contrato de seis campos: job_id, operation,
status, message, created_at, updated_at. En provisioning, el job de plan puede estar
completed mientras la operación permanece running y la instancia pending.
No anunciar una KVM creada al CRM hasta observar `infrastructure_kind=kvm`,
`status=ready` y la evidencia `result.simulated=false`.

| Campo | Interpretación para el CRM |
|---|---|
| `executor=simulated`, `infrastructure_kind=simulated` | Flujo de prueba; no existe KVM creada por esta operación |
| `executor=provisioning`, `infrastructure_kind=unprovisioned` | Solicitud/plan pendientes; VMID y nodo reales aún no confirmados |
| `infrastructure_kind=kvm` | Identidad de KVM observada; revisar status y fecha de result, no asumir salud actual indefinida |
| `result.power_state` | Snapshot de provisioning, derivado del started observado entonces; no es la energía vigente |
| `result.observed_at` | Fecha de la observación; no es monitoreo continuo |

Para energía actual usar GET `/v1/instances/{instance_id}/power`. Los POST
`/power/start`, `/power/shutdown` y `/power/reboot` reutilizan operation_id e
Idempotency-Key; el resultado se consulta en `/v1/operations/{operation_id}`.
Lifecycle agrega estado `uncertain` y el bloque tipado `power`; un job failed
puede acompañar una operación incierta que necesita observación, no otro comando.
Running no implica SO, agente, Cloud-Init o conectividad: guest_ready permanece
null. El contrato del PR #7 ya está fijado; las instancias históricas siguen
bloqueadas hasta su certificación explícita y la activación operativa.
Consultar [contratos y activación de Lifecycle](../KVM_LIFECYCLE_API.md).
La [certificación histórica](../KVM_POWER_PROMOTION.md) no cambia los IDs ni la
revisión original de aprovisionamiento.

La revisión independiente de planes y su recuperación pertenecen al
[contrato administrativo](../PROVISIONING_CONTRACT.md). El CRM conserva las
rutas neutrales de negocio y no recibe permisos administrativos. DELETE bajo
política sigue disponible; véase [DELETE seguro](../SAFE_DELETE.md).
