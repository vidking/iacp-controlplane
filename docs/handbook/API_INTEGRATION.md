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
defecto); disco 8–4096 GiB (20 por defecto). En Terraform se exige además el mínimo
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
status, message, created_at, updated_at. En Terraform, el job de plan puede estar
completed mientras la operación permanece running y la instancia pending.
No anunciar una KVM creada al CRM hasta observar `infrastructure_kind=kvm`,
`status=ready` y la evidencia `result.simulated=false`.

| Campo | Interpretación para el CRM |
|---|---|
| `executor=simulated`, `infrastructure_kind=simulated` | Flujo de prueba; no existe KVM creada por esta operación |
| `executor=terraform`, `infrastructure_kind=unprovisioned` | Solicitud/plan pendientes; VMID y nodo reales aún no confirmados |
| `infrastructure_kind=kvm` | Identidad de KVM observada; revisar status y fecha de result, no asumir salud actual indefinida |
| `result.power_state` | running/stopped derivado del booleano started observado del provider |
| `result.observed_at` | Fecha de la observación; no es monitoreo continuo |

Flujo Terraform, solo después de revisión y autorización operativa:

```sh
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" \
  "$IACP_API_BASE/v1/instances/$INSTANCE_ID/terraform" > plan.json
python3 - <<'PY'
import json
plan = json.load(open('plan.json'))
assert plan['status'] == 'planned'
json.dump({'attempt': plan['attempt'], 'plan_sha256': plan['plan_sha256'],
           'valid_for_seconds': 900}, open('approval-request.json', 'w'))
PY
: "${IACP_APPROVER_TOKEN:?Falta la identidad revisora independiente}"
: "${APPROVAL_KEY:?Define y conserva la clave de aprobación}"
# El aprobador revisa el plan binario y la asignación antes de esta petición.
curl --fail-with-body -H "Authorization: Bearer $IACP_APPROVER_TOKEN" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $APPROVAL_KEY" \
  --data-binary @approval-request.json \
  "$IACP_API_BASE/v1/instances/$INSTANCE_ID/terraform/approvals" > approval.json
```

`apply_enabled` refleja el gate de la API; no concede autorización por operación ni confirma la configuración del worker.

La identidad approver debe tener el mismo tenant y un sujeto distinto del creador
y del operador que solicita apply. La API no permite al CRM asignarse permisos.
El siguiente paso es una operación efectiva de infraestructura y requiere
habilitación en API/worker y aprobación explícita del entorno de laboratorio:

```sh
python3 - <<'PY'
import json
row = json.load(open('approval.json'))
json.dump({'approval_id': row['approval_id']}, open('apply-request.json', 'w'))
PY
: "${APPLY_KEY:?Define y conserva la clave de ejecución}"
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $APPLY_KEY" \
  --data-binary @apply-request.json \
  "$IACP_API_BASE/v1/instances/$INSTANCE_ID/terraform/apply"
curl --fail-with-body -H "Authorization: Bearer $IACP_SERVICE_TOKEN" \
  "$IACP_API_BASE/v1/instances/$INSTANCE_ID/terraform/applies"
```

La respuesta de apply incluye el nuevo job de aplicación; el job inicial sigue
siendo el de planificación. Polling con backoff y jitter (por ejemplo, 1–10 s)
es una decisión del consumidor; aún no existen webhooks ni SSE. Ante 503 de cola,
repetir la misma solicitud para recuperar IDs y consultar antes de usar `/recover`.
Nunca automatizar una aprobación nueva para resolver un resultado incierto.

Cancelación y recuperación usan POST en
`.../terraform/applies/{approval_id}/cancel` y `.../recover`. La recuperación de un
run queued republica el mismo identificador; running/uncertain observa al provider
sin aplicar. Lecturas de otro tenant devuelven 404. Un reader no puede escribir;
un operator no puede aprobar; un approver no puede solicitar apply.

Los ejemplos de eliminación están en [la demostración](LAB_DEMO.md): todavía no
existe DELETE de instancias en la API. No simularlo borrando registros del inventario.
