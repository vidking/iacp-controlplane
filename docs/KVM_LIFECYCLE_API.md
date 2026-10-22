# KVM Lifecycle API

Este incremento agrega observación y operaciones asíncronas de energía mediante
IACP. El portal conserva la autenticación por tenant y los IDs de instancia y
operación; no recibe credenciales ni acceso a la API nativa de Proxmox.

**Las mutaciones quedan bloqueadas en este release hasta completar la promoción
del contrato de propiedad IaC.** `IACP_KVM_LIFECYCLE_ENABLED=1` no basta para
habilitarlas. GET de energía puede utilizarse con credenciales de lectura.
El aprovisionamiento de VMID 106, intento 4, ya fue confirmado completed como
hito previo; este incremento no repite provisioning ni recovery Terraform.

## Propiedad de started y dependencia de activación

El Control Plane fija la fuente IaC en
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4`. Su recurso
`module.kvm.proxmox_virtual_environment_vm.this` declara
`started = var.started`, `on_boot = false` y `reboot_after_update = false`, sin
`lifecycle.ignore_changes`. Por tanto, un futuro plan declarativo podría intentar
restaurar la energía elegida durante la creación. Lifecycle no puede actuar con
ese contrato.

La [propuesta independiente para iac-proxmox](proposals/iac-proxmox-started-ownership.patch)
agrega únicamente `lifecycle { ignore_changes = [started] }` al recurso KVM y
conserva `started = var.started`. No se aplicó al repositorio IaC. Se validó el
patch contra una copia temporal de la revisión fijada.

Terraform considera los atributos ignorados durante create y los omite al
planificar updates, según su [contrato oficial](https://developer.hashicorp.com/terraform/language/meta-arguments/lifecycle#ignore_changes).
La prueba de integración con Terraform 1.16.6 y el provider incorporado
`terraform_data` demuestra este comportamiento para un booleano: creación con
false, actualización con valor externo true conservado, y otro atributo sin
ignore que sigue generando update. No ejecuta apply ni crea infraestructura.
El test verifica Terraform core; la semántica del atributo Proxmox se revisó en
[bpg 0.116.1](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L7082):
`vmRead` observa la energía y `vmUpdate` utiliza el valor planificado y
`d.HasChange(started)` al finalizarla. Los cambios de CPU/discos pueden requerir
una ventana y reinicio independientemente de ignore; no se autoriza modificar
otros atributos por Lifecycle.

Activación requiere, en orden:

1. Revisar y fusionar la propuesta en iac-proxmox; conservar `started` en create
   y excluir únicamente ese atributo de los updates. Certificar la revisión
   exacta y los planes relevantes, también contra la versión desplegada de PVE.
2. Completar o resolver por los flujos autorizados todos los planes y Applies
   antiguos antes de la promoción; drenar workers antiguos. Un plan guardado
   conserva su configuración anterior y no incorpora ignore retroactivamente.
3. Mediante otro cambio revisado del Control Plane, fijar `SOURCE_REVISION` y
   `KVM_POWER_SOURCE_REVISION` al **mismo SHA inmutable** de la fuente aprobada.
   Este incremento deja el segundo en `None` y rechaza permanentemente el SHA
   anterior conocido como propietario Terraform de la energía. No hay un flag,
   parámetro cliente o edición SQL que sustituya esta promoción.
4. Desplegar las imágenes de esa promoción y su clon IaC autorizado conforme al
   procedimiento de releases, con el mismo código fijado para todos los workers
   Terraform. Solo entonces activar `IACP_KVM_LIFECYCLE_ENABLED=1` en API y worker.

Para recursos futuros, `started` mantiene el comportamiento inicial solicitado y
las postcondiciones de provisioning de PR #18 siguen comprobándolo. Para recursos
existentes, la regla entra en vigor en los **planes futuros** que usan la nueva
configuración; no hace falta escribir state para registrar ignore, pues Terraform
no guarda esa regla en state. No se ejecuta Apply para transferir energía. El
Apply de creación de VMID 106 debe seguir terminal y consumido. Una vez ready,
`Instance.result` sigue siendo evidencia histórica del aprovisionamiento;
GET `/power` es la observación vigente de energía.

Esta promoción es una dependencia pendiente, no una afirmación de que el recurso
actual ya cedió su propiedad. Escritores Terraform externos también deberán usar
la configuración aprobada; no se puede proteger contra un operador que ejecute
fuera de IACP una revisión antigua o cambie energía directamente.

## Contratos HTTP y OpenAPI

El [OpenAPI exportado](openapi.json) contiene los cuatro contratos:

| Método y ruta bajo `/v1/instances/{instance_id}` | Rol | Resultado |
|---|---|---|
| GET `/power` | reader, operator o approver del tenant | 200, observación actual de Proxmox |
| POST `/power/start` | operator | 202, operación asíncrona de arranque |
| POST `/power/shutdown` | operator | 202, apagado ordenado con timeout |
| POST `/power/reboot` | operator | 202, reinicio ordenado con tarea confirmada |

POST exige `Idempotency-Key` de 1–128 caracteres `A-Za-z0-9._:-`. Admite cuerpo
ausente o `{}`; rechaza VMID, nodo, timeout, force y demás parámetros de cuerpo.
Acción y destino proceden de la ruta y del registro administrado del servidor.
El fingerprint incluye instancia y acción y queda ligado al solicitante. Claves
compartidas con otra solicitud del tenant producen 409.

Ejemplo de respuesta 202, incluidos replays del mismo comando:

```json
{"instance_id":"<uuid>","operation_id":"<uuid>","job_id":"<uuid>","status":"queued"}
```

El replay devuelve los mismos IDs y el estado actual. Si sigue activo, puede
republicar el **mismo** identificador para reanudar observación, sin autorizar
otra mutación. Consultar GET `/v1/operations/{operation_id}`: incluye eventos,
actor, timestamps, error_code y `power` con acción, fase, estado, energía observada,
timestamps de inicio/envío/fin y `recovery_required`. No expone UPID, endpoint,
token ni configuración privada. Para operaciones de creación, `power=null`.

GET de energía informa `power_state`, `observed_at`, disponibilidad de mutaciones
y motivo de bloqueo. `os_boot`, `guest_agent`, `cloud_init` y `accessible` quedan
`unknown`; `guest_ready` queda null, incluso cuando Proxmox devuelve running.
No se consulta QEMU Guest Agent, SSH, Cloud-Init ni salud del SO para arrancar o
confirmar energía. Una imagen sin agente y con Cloud-Init sin certificar puede
arrancar, sin que eso demuestre disponibilidad del sistema invitado.

401 indica autenticación inválida; 403, rol no autorizado o tenant inactivo;
404 oculta instancias y operaciones ajenas; 409 indica contrato no disponible,
instancia no preparada, conflicto de clave o ejecución concurrente; 422 rechaza
parámetros no admitidos. 503 indica fallo de servicio, credenciales de Proxmox o
publicación no confirmada. Un error de publicación conserva la reserva durable:
repetir la misma clave, no crear otra para eludirla.

## Identidad, permisos y aislamiento

La instancia debe ser Terraform/KVM, ready, con VMID y nodo registrados y un
Apply completed de su intento y SHA actuales, con evidencia de presencia y
postcondiciones verificadas. VMID, nodo, nombre y aprobación se congelan en la
operación y se revalidan antes del envío y al finalizar. VMID 104 se excluye tanto
en el código de destino como en la restricción SQL de la ejecución de energía.
La autorización del tenant y el estado activo se vuelven a comprobar en el worker.

Usar una credencial Proxmox **dedicada** con `VM.Audit` y `VM.PowerMgmt` en los
VMIDs autorizados. Las consultas de configuración, energía y pending requieren
Audit; los tres POST requieren PowerMgmt. El cliente verifica los permisos en
`/vms/<vmid>` antes de ejecutar. No requiere permisos para editar configuración,
almacenamiento, usuarios o state. Debe poder observar sus propias tareas por UPID.
La asignación y certificación de permisos son pasos operativos posteriores;
este incremento no modifica permisos Proxmox.

El cliente solo utiliza HTTPS con CA verificada, sin proxies de entorno,
redirecciones ni reintentos de POST. Credenciales server-side:

```json
{"endpoint":"https://<proxmox>:8006/","api_token":"<credencial dedicada>"}
```

Montar ese archivo como secreto y señalar `IACP_KVM_CREDENTIALS_FILE`. Para CA
privada, montar su certificado público read-only y definir `IACP_KVM_CA_FILE` en
API y worker. No se copian credenciales al repositorio. La configuración solo
admite endpoint y token; nunca acepta desactivar TLS.

El override optativo [compose.lifecycle.yaml](../compose.lifecycle.yaml) agrega
la cola `lifecycle` y su worker sobre la imagen base, sin Terraform CLI, Git,
credenciales AWS, state, volumen de planes ni mounts de fuentes IaC. API y worker
usan el flag optativo apagado por defecto. No altera
`IACP_TERRAFORM_APPLY_ENABLED`; debe conservarse en 1 en los servicios Terraform.
El despliegue debe conservar también los overrides efectivos de tenant y TLS.

## Ejecución y recuperación

La migración `0005_kvm_lifecycle` conserva todas las filas existentes, elimina la
unicidad global de `Operation.instance_id`, mantiene una creación única mediante
índice parcial y agrega `power_executions`. La relación `Instance.creation`
selecciona explícitamente `instance.create`. El índice parcial de energía permite
como máximo una operación queued/running/uncertain por instancia, incluso tras
un fallo o liberación del lock de proceso.

Se reutiliza exactamente el advisory lock por instancia del ejecutor Terraform.
La API serializa admisión; el worker conserva el lock durante la tarea y
comprueba su conexión antes de mutar y confirmar. Una entrega duplicada ocupada
no reclama el trabajo del primer worker. No se crea un motor de locking paralelo.

Secuencia del worker:

1. Comprobar contrato, tenant, identidad, creación completada y permisos.
2. Consultar configuración actual y energía. Bloquear config lock, VM pausada,
   HA administrada y cambios pending/delete: un arranque o reboot nativo puede
   materializar configuración pendiente. No cambiarla ni limpiarla desde IACP.
3. Start sobre running y shutdown sobre stopped son no-op auditados. Reboot
   sobre stopped falla; no implica un arranque adicional.
4. Persistir `submitted_at` y fase dispatching **antes** de un único POST nativo.
   Guardar el UPID validado contra nodo, VMID y tipo de tarea esperados.
5. Consultar tarea hasta stopped/OK y volver a leer la energía real. Reboot
   necesita prueba de tarea; running por sí solo no demuestra reinicio. Si QEMU
   todavía está arrancando tras terminar qmreboot, observar hasta el mismo
   deadline de 90 s; no enviar otro comando para compensar esa latencia.
6. Revalidar vínculo y lock y confirmar estado, job, resultado y evento en la
   misma transacción condicional. La creación y el snapshot de provisioning se
   conservan. No se consulta ni escribe Wasabi.

Shutdown usa `/status/shutdown`, `timeout=60`, `forceStop=0`. Proxmox ejecuta
apagado ordenado mediante ACPI si no hay agente operativo, como en la plantilla
actual; puede usar Guest Agent si está configurado y disponible. No se invoca
`stop`, `reset`, ni se configura el agente. Reboot usa `/status/reboot` y timeout
60; no hay fallback forzado. Estas propiedades se contrastaron con la
[API Qemu oficial](https://git.proxmox.com/?p=qemu-server.git;a=blob;f=src/PVE/API2/Qemu.pm;hb=71ea0ac7192bb935f63cb64beb59e87d6cecd3bf)
y [_do_vm_stop / vm_reboot](https://git.proxmox.com/?p=qemu-server.git;a=blob;f=src/PVE/QemuServer.pm;hb=71ea0ac7192bb935f63cb64beb59e87d6cecd3bf).
La revisión consultada documenta el contrato de referencia; la versión concreta
desplegada debe certificarse durante activación. No se presupone QGA operativo.

| Situación | Estado / acción permitida |
|---|---|
| Error de preflight antes de consumir comando | failed; ninguna mutación enviada |
| Estado objetivo ya observado | completed, no-op auditado |
| UPID stopped/OK y energía esperada | completed |
| UPID todavía running al vencer observación (90 s) | uncertain / POWER_TASK_TIMEOUT; reanudar observación |
| Error de conexión después de envío | uncertain; nunca repetir POST |
| Worker interrumpido con UPID guardado | redelivery o misma clave reanuda GET de tarea y resultado |
| UPID perdido en ventana de envío/respuesta/persistencia | uncertain / POWER_TASK_UNKNOWN; auditoría requerida |
| UPID terminal con error o timeout ACPI | failed con diagnóstico específico, sin apagado forzado |
| Tarea OK pero energía discordante | uncertain / POWER_RESULT_MISMATCH |
| Pérdida de conexión del advisory lock | no confirmar; otro worker reevalúa evidencia persistida |

Operación uncertain permanece activa y bloquea acciones contradictorias de otra
clave. Su job puede aparecer failed; el contrato de seguimiento y recuperación
es la operación, no el job. La misma clave vuelve a observar una tarea conocida,
incluso si se pausa la admisión de comandos nuevos. Celery usa acks_late y
reject_on_worker_lost para esta tarea; un replay explícito puede acelerar su
observación tras reinicio.

No existe una transacción atómica entre PostgreSQL y el POST de Proxmox. Sin
UPID no se puede atribuir un reboot a una solicitud solo por la energía actual.
Se conserva identidad, actor y timestamps para investigación. Asociar una tarea
perdida o cerrar esa incertidumbre requiere un flujo auditado independiente,
fuera del MVP; no se implementa una adopción automática por similitud temporal
ni se recomienda editar SQL. Tampoco se expira la reserva para habilitar un
segundo comando mientras el primero puede seguir en vuelo.

## Prueba controlada futura: primer arranque de VMID 106

Este procedimiento es posterior a revisión, promoción IaC, despliegue autorizado
y certificación de permisos. No se ejecuta durante desarrollo ni al preparar PR.

1. Confirmar que el intento 4 de
   `a1b2d063-6240-4969-bc04-e7e0e6be9e73` sigue completed, aprobación
   `1750ee70-a6ca-4036-a764-c4b077a0b588`, VMID/ID 106, mng01, nombre
   `iacp-kvm-lab-01`, postcondiciones y state consistente. El serial 3 y lineage
   `d954af88-6ae6-2d04-9ead-55ed1f95d644` son referencias del hito, no valores
   que deban forzarse. No ejecutar otro apply, retry, recover, import o destroy.
2. Completar los cuatro pasos del contrato de propiedad anteriores. Inventariar
   y drenar workers viejos y planes no terminales. Mantener Apply global en 1.
   Verificar aislamiento del token Lifecycle a `/vms/106`, TLS y tareas propias,
   ausencia de escritores externos, HA y configuración pendiente.
3. Instalar imágenes revisadas y migración por el servicio `migrate`, conservando
   proyecto, volúmenes y overrides. Incluir el override Lifecycle de forma
   optativa y montar su secreto/CA. No ejecutar SQL manual, `down -v`, restaurar
   state ni recrear volúmenes. Health/readiness deben aprobar la revisión 0005.
4. Obtener GET `/v1/instances/<id>/power` con identidad del tenant. Exigir stopped,
   `mutations_available=true`, sin bloqueo. Si el contrato sigue no promovido,
   detener la prueba; activar flags no sustituye su revisión.
5. En una ventana acordada, enviar únicamente POST
   `/v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/power/start`, sin cuerpo,
   con Bearer operator del tenant y una clave nueva conservada para esa solicitud.
   Guardar operation_id/job_id. No usar la API nativa desde el portal.
6. Consultar GET `/v1/operations/<operation_id>` hasta completed y GET `/power`
   hasta running. Exigir un único evento power_completed y energía observada;
   guest_ready debe seguir null. SO, red, agente y Cloud-Init se certifican mediante
   procedimientos separados. El cambio de energía no es una nueva creación.
7. Contrastar mediante lectura autorizada Version ID, lineage, serial y hash de
   Wasabi antes/después, sin escritores externos. Lifecycle no tiene acceso a
   ese backend y los valores deben permanecer invariantes. No imprimir su JSON.
   Revisar auditoría de Proxmox, operación IACP y ausencia de nuevos intentos.

Ante uncertain, repetir solo la **misma clave y acción** para observar la tarea
conocida. No intentar shutdown/reboot con otra clave para resolver un envío
ambiguo. Un timeout de ACPI no autoriza stop forzado. No se inicia un apagado
automático para deshacer la prueba.

## Riesgos y rollback

- Deshabilitar admisión con `IACP_KVM_LIFECYCLE_ENABLED=0` en API y worker y drenar
  sin interrumpir tareas nativas activas. Continuar observación de UPID conocidos
  y conservar uncertain desconocidos para auditoría. No cambiar Apply global.
- No revertir la propiedad de started a Terraform como un efecto incidental del
  rollback: podría revertir la energía elegida. La devolución de propiedad exige
  otra decisión aprobada y contraste de valores deseados/reales.
- La migración amplía el modelo de operaciones y readiness exige la revisión
  actual. **El binario anterior 7b4e24e no es un rollback compatible con 0005**:
  presupone una sola operación por instancia. Se mantiene downgrade deshabilitado.
  Usar el flag para rollback funcional o un release correctivo que conserve 0005
  y la relación de creación filtrada. No restaurar/borrar PostgreSQL o Wasabi.
- Entre la consulta de pending y el POST no hay fencing de escritores externos
  en la API de energía de PVE. Coordinar cambios externos; el lock IACP serializa
  sus propios workers, no a operadores ajenos. La identidad se verifica de nuevo
  al finalizar y cualquier divergencia queda incierta.
- No se soporta HA, apagado forzado, reset, ni certificación del invitado. Los
  errores no exponen cuerpos del provider, tokens, UPID ni configuración privada.

Las pruebas utilizan fixtures sintéticas y servicios desechables. El informe
final registra la suite completa, integración, hashes y límites en
[KVM_LIFECYCLE_VALIDATION.json](validation/KVM_LIFECYCLE_VALIDATION.json).
El futuro IaCoC Operations Center puede consumir los mismos IDs, estados,
timestamps y eventos; no necesita acceso Proxmox ni reinterpretar jobs para
determinar el resultado de una operación.
