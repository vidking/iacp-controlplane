# Recuperación controlada del MVP KVM

Aplicable a IACP 0.10.1 en MNG. [Estado de certificación y límites](RESILIENCE_CERTIFICATION.md). Nunca ejecutar este ensayo en producción ni sobre VM104, VM106 o Golden Image 29000.

## Reconocer y contener

1. Conservar la misma intención: tenant, UUID, idempotency key, operación, job, intento, approval_id, VMID/nodo y state key. Registrar release/digests, tiempos UTC y último evento. Guardar planes/manifests/state únicamente en el almacenamiento privado aprobado; en Git solo referencias y hashes no secretos.
2. Si una operación queda uncertain, detener la secuencia de ensayos. Consultar `GET /v1/operations/{operation_id}` y `GET /v1/instances/{UUID}`; para administración, consultar el plan y ejecuciones bajo `/v1/instances/{UUID}/provisioning`. El estado normalizado puede ser uncertain aunque la operación histórica figure failed.
3. Comprobar workers activos/reservados, las tres colas y sus prioridades, `unacked`, procesos Terraform/provider y sesiones/advisory locks. Un LLEN=0 no demuestra inactividad. No matar un proceso, retirar un lock o reiniciar servicios sin relacionarlo con el UUID y el cambio autorizado.
4. Confirmar que no haya un escritor de Terraform todavía vivo. En el runtime baseline `4f153e8`, SIGKILL puede dejarlo huérfano. El candidato con supervisor requiere despliegue y ensayo antes de confiar en él. Terminar el cliente local no cancela una clonación Proxmox ya enviada.
5. Verificar DB, inventario/tareas Proxmox con alcance de clúster y Wasabi actual por GET/HEAD. Comparar VersionId, lineage, serial y hashes. Un 403, timeout, listado parcial o objeto no legible no prueba ausencia.

`scripts/resilience_snapshot.py` exporta únicamente metadata allowlist del runtime existente. Es diagnóstico readonly; no drena ni repara. Sus JSON no contienen valores de secretos, planes ni state.

## Elegir la recuperación según el fence

| Situación | Acción soportada | Resultado aceptable |
|---|---|---|
| Creación/plan queued o running sin Apply autorizado/consumido, worker ausente y lock libre | `POST /v1/instances/{UUID}/retry-provision` con `expected_attempt` y clave estable, cuando la instancia pertenezca a política de autoservicio; administración usa `/provisioning/plan/retry` | Nuevo intento de planificación sobre mismo UUID, VMID/asignación/state; intento viejo interrupted y entregas tardías cercadas |
| Plan planned y delegación vigente, sin mutación iniciada | `POST /v1/instances/{UUID}/provision` para continuar la intención existente | Usa el plan y aprobación existentes; no crea otra instancia |
| Apply running/uncertain o entrega ambigua | `POST /v1/instances/{UUID}/reconcile`; administración puede usar `/provisioning/executions/{approval_id}/recover` | Solo observación del mismo plan/recurso. 202 acredita encolado; seguir la misma operación |
| Apply queued y entrega no confirmada | Continuación del mismo UUID según política; si se decide abandonar la mutación, `/recover` cerca el Apply pendiente antes de publicar observación | Nunca nueva aprobación, key de creación o UUID para compensar |
| Power con submitted_at y UPID conocido | Replay de la misma acción con misma key; entrega al worker reanuda monitorización | Cero POST nativos adicionales; tarea y estado final concordantes |
| Power consumido sin UPID | Conservar uncertain; investigar tarea Proxmox y evidencias en ticket privado | Running no demuestra reboot; no existe una API revisada para asociar un UPID arbitrario o cerrar automáticamente este caso |
| Guest initialization consumida | Reconcile del mismo UUID/operación; solo observar PID y marcadores validados | Nunca repetir guest-exec; sin evidencia suficiente permanece uncertain |
| Cloud-Init/SSH sin confirmar | `POST /v1/instances/{UUID}/access/verify` con misma key cuando proceda; validar identidad/host key | La contraseña no se devuelve; no recrear VM ni desactivar controles SSH/TLS |
| DELETE consumido | `/reconcile` o recuperación administrativa de la misma operación de deletion | Solo lectura de state/inventario; no segundo destroy |
| DELETE preparado, sin fence consumido y ya no autorizado | Cancelación administrativa de la misma operación | Historial conservado; nunca resetear started_at |

No mezclar las claves: repetir una intención conserva su clave; una nueva acción autorizada tiene su propia clave. Guardar el cuerpo exacto/hash original. Cuerpo distinto bajo la misma key debe devolver 409.

## Interpretar la observación

| Wasabi actual | Proxmox | Decisión |
|---|---|---|
| Recurso propio, versión estable, postcondiciones válidas | Recurso propio y consistente | Puede completar reconciliación y recuperar el estado administrado |
| State vacío versionado y legible | Ausencia en inventario completo, sin tarea en vuelo | Conservar uncertain/RESOURCE_ABSENT hasta revisión independiente; resolve-absence cierra failed, nunca completed |
| Recurso ausente | VM presente | UNMANAGED_RESOURCE: detener, preservar evidencia; no import ni clonación automática |
| Recurso presente | VM ausente | STATE_RESOURCE_MISSING: investigar drift, no declarar éxito |
| Backend no legible, sin VersionId o cambia entre lecturas | Cualquiera | Fail-closed; investigar y realizar observación nueva cuando se estabilice |
| Identidad, plan, binding, hash o postcondición incompatible | Cualquiera | Detener; restaurar evidencia original desde respaldo verificado si corresponde, sin recalcular una autorización para aceptar el cambio |

Reconcile de Apply puede adquirir/retirar su `.tflock` nativo durante refresh-only. No modifica el objeto state ni repara drift. DELETE recovery no ejecuta Terraform mutante. Las observaciones corresponden a un intervalo; no son una transacción distribuida que impida cambios externos posteriores.

## Lock residual

Preservar metadata del lock y su ID en almacenamiento privado. Relacionar propietario, UUID, PID/host/worker y tarea Proxmox. Verificar independientemente que ningún escritor esté activo, incluyendo procesos huérfanos y tareas remotas. Un bloqueo por tiempo no autoriza eliminarlo.

No hay desbloqueo automático certificado. Si el propietario no puede liberar su lock, preparar un cambio específico con evidencia de ausencia de escritores y revisión independiente del ID exacto. Este runbook no autoriza `force-unlock`, borrar `.tflock`, editar state ni eliminar versiones históricas.

## Caídas de dependencias o de VM104

Redis: conservar la reserva DB y el error de entrega; una publicación sin ACK puede haber llegado. Recuperar la misma intención según su fase. No purgar colas/unacked. El timeout de visibilidad de 3.600 s y `acks_late` no garantizan redelivery tras cualquier muerte de child prefork.

PostgreSQL: restablecer el servicio por el procedimiento operativo; una conexión de lock perdida no autoriza éxito desde el worker antiguo. Verificar esquema, historial y reservas antes de reabrir mutaciones. No restaurar un backup sobre la DB viva como compensación de una operación individual.

Wasabi/Proxmox: restablecer conectividad con cambios de alcance explícito. No relajar TLS, permisos o validación de versiones. Desconocido permanece desconocido.

VM104: recuperar host/almacenamiento por el procedimiento de desastre, conservar proyecto/volúmenes/secretos/artefactos y revisar DB/Redis/planes como conjunto. No existe failover independiente certificado. El arranque sano observado el 24/10 no sustituye una restauración de backup ni un ensayo con trabajos en vuelo.

## Cierre obligatorio

Exigir una intención y un recurso como máximo; todas las operaciones correlacionadas, sin mutaciones consumidas repetidas; postcondiciones DB/PVE/state concordantes o uncertain explícito. Para una VM descartable que ya no se necesita, shutdown ordenado y DELETE normal por IACP bajo la política vigente. Confirmar deleted, ausencia en todo MNG, state vacío versionado, ausencia de `.tflock`, colas/unacked y advisory locks sin anomalías. Conservar tombstones y eventos.

Marcar si la recuperación fue automática, solicitada por cliente o manual. Una limpieza o atestación humana no convierte una corrida fallida en automática aprobada.
