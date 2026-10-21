# State remoto y recuperación

Se reutiliza el baseline Wasabi documentado en
`CyberFuel/iac-proxmox/docs/operations/terraform-state-wasabi.md` (KVMHOST-87).
No se crea un bucket nuevo ni se migra el backend al arrancar el Control Plane.
Bucket, región y endpoint proceden de la asignación externa aprobada; los valores
históricos del baseline no autorizan una KVM nueva.

Cada instancia utiliza una clave estable:

```text
iacp/lab/tenants/{tenant_uuid}/instances/{instance_uuid}/terraform.tfstate
```

La clave no incluye el ID del job o intento. Reintentar planificación conserva
instancia, VMID, asignación y state. El workspace Terraform es siempre default;
no se utilizan workspaces del desarrollador. La VM 104 y las claves del bootstrap
no se importan, migran ni administran mediante estos states.

El backend exige cifrado, TLS, `use_lockfile=true` y acceso path-style. El provider
verifica certificados; una CA corporativa se monta explícitamente y se configura
mediante `IACP_TERRAFORM_CA_FILE`. Las credenciales están en un archivo externo,
se pasan por entorno solo al subproceso y no se escriben en tfvars/backend.

La identidad Wasabi necesita listar el prefijo autorizado y GetObject/PutObject
sobre el state. El objeto `.tflock` necesita GetObject/PutObject/DeleteObject.
Versionado y retención del bucket son requisitos operativos que deben verificarse
en el entorno; el código no los habilita automáticamente. Separar identidades y
prefijos de laboratorio y producción. El worker actual acepta únicamente lab.

Controles de concurrencia:

| Capa | Garantía | Límite |
|---|---|---|
| PostgreSQL advisory lock | Un flujo activo por instancia, compartido entre workers | Requiere misma base; heartbeat detecta pérdida de sesión |
| Restricciones DB | VMID único, intento y autorización únicos, claim de run | No detectan operaciones manuales externas a IACP |
| Saved plan + SHA-256 | Se aplica la decisión aprobada, sin volver a planificar creación | Si el state cambió, Terraform puede rechazar un plan obsoleto |
| Lock S3 `.tflock` | Exclusión entre clientes Terraform sobre la misma clave | Se debe validar la semántica condicional de Wasabi en laboratorio |
| Fencing del run | Una entrega duplicada no reaplica un run iniciado | Tras caída se exige conciliación explícita |

La espera de lock Terraform es 30 s; no se usa `-lock=false` ni force-unlock
automático. No eliminar `.tflock` por antigüedad. Identificar proceso, run y dueño,
verificar que no siga mutando y obtener autorización antes de cualquier desbloqueo.

## Decisiones de recuperación

| Situación | Acción segura |
|---|---|
| Plan fallido/cancelado sin autorización activa | Inspeccionar error; cancelar/reintentar plan con expected_attempt y nueva clave de retry |
| Aprobación caducada antes de mutar | Cancelar autorización; cancelar plan y generar un intento nuevo; obtener aprobación nueva |
| Publicación apply no confirmada, run queued | Consultar IDs, repetir la misma petición; `/recover` republica el mismo run |
| Worker cayó, run running | Confirmar que no haya proceso activo; `/recover` observa provider y mantiene exclusión |
| Timeout/error/cancelación después de started_at | Estado uncertain; nunca reaplicar automáticamente |
| Recurso exacto observado | Conciliación completed, inventario ready con fecha y proyección de recursos |
| State vacío, recurso parcial/ajeno o provider inaccesible | Mantener uncertain; inspección manual de state, Proxmox y versiones; no crear otra instancia para reemplazarla |
| Plan/volumen perdido | Restaurar artefactos privados desde respaldo; no sustituirlos por un plan distinto bajo la aprobación anterior |
| PostgreSQL perdido | Restaurar respaldo validado; conciliar cambios ocurridos después del respaldo antes de reanudar colas |

`GET .../terraform/applies` expone status, error_code, started_at, finished_at y
result. `POST .../applies/{approval_id}/recover` no concede permisos de apply.
Durante conciliación se permite observar aunque apply esté deshabilitado; la
planificación debe estar habilitada y las credenciales de lectura disponibles.

## Respaldo y restauración de state

Infraestructura debe registrar bucket/key, versión, lineage, serial, checksum,
VMID, nodo, revisión del módulo y run relacionado. Respaldar el objeto y sus
versiones antes de cambios autorizados; tratar plan y state como información
sensible. El volumen de planes también requiere respaldo privado y restauración
probada con UID 10001 y rutas de contenedor estables.

Ante corrupción, congelar escritores y preservar todas las versiones. Comparar
estado remoto, recursos reales y eventos de PostgreSQL. Probar cualquier versión
restaurada en un entorno aislado; validar lineage/serial y pertenencia exclusiva
antes de una conmutación autorizada. El Control Plane no expone state push,
import, force-unlock, target, replace ni destroy.

La observación usa `plan -refresh-only` y lee planned_values; no aplica ese plan ni
actualiza por sí misma el state remoto. El dato observado tiene fecha y no
constituye monitoreo continuo. Si el provider no logra completar la lectura,
IACP conserva la incertidumbre en vez de inventar estado de infraestructura.

Referencias:
[Backend S3 y permisos](https://developer.hashicorp.com/terraform/language/backend/s3),
[apply de un plan guardado](https://developer.hashicorp.com/terraform/cli/commands/apply),
[show JSON](https://developer.hashicorp.com/terraform/cli/commands/show).
