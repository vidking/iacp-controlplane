# Incremento 2B: planificación Terraform

> Alcance histórico de 2B: planificación. La capacidad posterior de aplicación
> autorizada y sus controles están descritos en [AUTHORIZED_APPLY.md](AUTHORIZED_APPLY.md).

Esta entrega prepara planes verificables desde un worker dedicado. La API y el
worker de 2A conservan la simulación por defecto. La planificación real también
está deshabilitada por defecto; `apply` está bloqueado en el código y no existe
un endpoint para habilitarlo. No se ha aprovisionado ninguna KVM con esta entrega.

## Auditoría y reutilización

Se revisó `CyberFuel/iac-proxmox`, baseline
`89f524036eaeff93cffb80c2b113f70c32b14e22`: módulo `modules/compute/kvm`, raíz
`stacks/kvm-instance`, provider `bpg/proxmox = 0.116.1`, lockfile y el procedimiento
`docs/operations/terraform-state-wasabi.md` (KVMHOST-87). La raíz exige Terraform
`>= 1.16.6, < 1.17.0`; este worker fija y verifica exactamente CLI `1.16.6`.

Se corrigió el disco en el [PR #6 del repositorio original](https://scm.cyberfuel.com/CyberFuel/iac-proxmox/pulls/6).
La dependencia utilizada aquí es el commit
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4`. Integrar esa dependencia antes de
habilitar un plan real. El worker archiva ese commit de un clon montado de solo
lectura; nunca usa la rama mutable, archivos ignorados, credenciales Git ni rutas
del equipo de desarrollo. No se copió la lógica HCL al Control Plane.

La limitación era la ausencia de un bloque de disco configurable. El nuevo
`instance.boot_disk`, nulo por defecto, transmite al módulo tamaño, interfaz,
datastore, mínimo verificado y opciones del disco. Los contratos anteriores
conservan la herencia. Activarlo sobre una VM existente requiere conocer el
tamaño real y revisar el plan; no constituye autorización para redimensionar.
La partición/filesystem invitada debe admitir el crecimiento por separado.

Se reutiliza el backend S3 Wasabi con `use_lockfile`, cifrado y path style
activados, además de las opciones del baseline para S3 compatible. El bucket,
región y endpoint deben suministrarse externamente. No se reutilizan las claves
de los experimentos anteriores ni se cae a state local. La clave persistente es:

```text
iacp/lab/tenants/{tenant_uuid}/instances/{instance_uuid}/terraform.tfstate
```

La clave no cambia al renombrar hosts, cambiar de equipo o reintentar. Se utiliza
siempre el workspace Terraform `default`. El baseline documenta pruebas previas
de locking/versionado/restauración; este PR no repitió accesos reales a Wasabi.

## Flujo y estados

Un servidor configurado con `IACP_INSTANCE_EXECUTOR=terraform` sigue aceptando
`POST /v1/instances` con identidad e idempotencia de 2A. Crea en la misma
transacción instancia, operación, job, ejecución Terraform e intento inicial.
Solo publica `(job_id, intento)` a la cola `terraform`. El worker habitual no
consume esa cola ni interpreta el job de planificación como simulación terminada.

El worker dedicado ejecuta `version`, `init`, `validate`, `plan -out` y `show -json`.
No acepta argumentos del cliente, no hereda `TF_CLI_ARGS`, no desactiva locking y
usa timeouts y límites de salida. La cancelación termina el grupo de procesos.
No almacena excepciones o stderr potencialmente sensibles en PostgreSQL/logs.

Antes de aceptar el plan valida dirección del recurso, VMID/nodo/nombre,
CPU/RAM/disco y plantilla contra la asignación autorizada. Solo acepta creación
o no-op de la KVM propia. Rechaza actualizaciones, reemplazos, eliminaciones y
states con recursos ajenos. VM 104 está excluida como destino y como plantilla,
tanto en validación como mediante una restricción de reserva en la base.

| Situación | Ejecución | Job | Operación | Instancia |
| --- | --- | --- | --- | --- |
| En cola | queued | queued | queued | pending |
| Planificando | running | running | running | pending |
| Plan guardado y verificado | planned | completed | running | pending |
| Fallo | failed | failed | failed | failed |
| Cancelación completada | cancelled | failed | failed | pending |

Un job completado de planificación **no** significa VM creada. La operación
permanece abierta a la espera de aprobación. VMID/nodo de la instancia permanecen
nulos; el VMID reservado figura solo en la ejecución interna y el resumen del
plan. El contrato público de jobs conserva sus estados de 2A; por eso la
cancelación del plan se representa como job failed y ejecución cancelled.

El plan binario y su manifiesto se conservan privados en el volumen de planes,
por instancia/intento, con directorio 700 y archivos sensibles 600. PostgreSQL
guarda SHA-256, revisión del código, state key, errores y el historial de intentos;
los eventos de operación registran actor y transiciones. `prepare_apply` verifica
integridad del binario, entradas, backend, clave y código contra el intento.
Devuelve aprobación pendiente y ejecución deshabilitada. `apply()` siempre falla
con `APPLY_DISABLED`, incluso si alguien define variables de entorno de apply.
No hay aprobación automática ni ejecución de un plan regenerado implícitamente.

## Consulta, cancelación y recuperación

- `GET /v1/instances/{id}/terraform`: estado, intento actual, SHA y trazabilidad.
- `POST /v1/instances/{id}/terraform/cancel`: intención de cancelación; idempotente.
- `POST /v1/instances/{id}/terraform/retry`: cuerpo `{"expected_attempt": N}` e
  `Idempotency-Key` nueva para esa recuperación. Una repetición devuelve el mismo
  intento y no vuelve a publicar. Reutilizar la clave con otro N produce 409.

Se aplican los permisos operator/reader y el aislamiento de 2A. Un reader consulta
pero no cancela/reintenta. Los recursos de otro tenant devuelven 404.

Un advisory lock PostgreSQL de sesión serializa cada instancia entre procesos.
Se mantiene durante toda la planificación y se libera antes de devolver la
conexión al pool. El state remoto agrega su propio `.tflock`; no se utiliza
`-lock=false`, force-unlock ni push de state. Las reservas VMID son únicas en DB.

Los reintentos conservan instancia, operación, job, asignación congelada y state
key. Cada intento tiene su propio directorio y registro. El número de intento
descarta entregas antiguas, aunque lleguen tras una recuperación. La asignación
no se vuelve a obtener del archivo mutable después de reservarla.

Si se pierde la publicación o muere un worker, un operador puede reintentar con
el número esperado cuando no exista lock activo. Los intentos queued/running
anteriores quedan marcados como interrupted. Una ejecución activa devuelve 409;
un plan listo exige cancelación explícita antes de replanificar. Un lock remoto
huérfano produce fallo/timeout: su liberación requiere revisión operativa.

La recuperación cubre **solo planes**, que no crean recursos. No se hace retry
automático de una mutación incierta. Antes de implementar apply se necesitan
aprobación verificable del SHA exacto, permisos separados, caducidad del plan,
conciliación de VM/state tras interrupciones y pruebas reales autorizadas.

## Configuración autorizada requerida para la primera KVM

No hay valores de red, storage o Golden Image predeterminados. El operador debe
proporcionar un archivo JSON externo `IACP_TERRAFORM_ALLOCATIONS_FILE`, indexado
por `tenant_uuid/nombre_de_instancia`, con estos campos:

| Campo | Evidencia/autorización requerida |
| --- | --- |
| environment | lab |
| image | Referencia lógica de la solicitud, vinculada por el operador a la Golden Image |
| allowed_nodes, node_name, template_node_name opcional | Nodos MNG aprobados; baseline mng01/mng02 |
| vm_id | VMID reservado y disponible, distinto de 104 y de la plantilla |
| template_vm_id | Golden Image validada, excluyendo VM 104 |
| backend.bucket, backend.region, backend.endpoint | Configuración Wasabi aprobada, versionado/cifrado y permiso sobre el prefijo nuevo y `.tflock` |
| pool_id, clone_datastore_id opcionales | Pool y destino de clonación aprobados, si corresponden |
| network_bridge, vlan_id | Segmento de laboratorio aprobado |
| ipv4_cidr, ipv4_gateway | Reserva efectiva de IPAM, sin reutilizar ejemplos |
| cloudinit_datastore_id | Storage cloud-init autorizado |
| cloudinit_username, ssh_public_keys | Usuario y claves públicas autorizadas |
| guest_agent_enabled, started | Política de la Golden Image y del primer arranque; por defecto false |
| boot_disk.interface, datastore_id, minimum_size_gb | Interfaz/storage y tamaño mínimo verificado de origen |
| boot_disk.file_format, aio, cache, discard, backup, replicate, iothread, ssd | Opciones verificadas de la plantilla; conservar toda diferencia respecto de los defaults del provider |

El tamaño solicitado procede de `disk_gb` de la instancia. Se rechaza si es menor
que el mínimo autorizado. CPU/RAM/nombre proceden de la solicitud persistida.
El contrato actual cubre un disco de arranque; plantillas con otros discos,
dispositivos o atributos fuera del contrato requieren revisión previa.

Otro archivo externo, `IACP_TERRAFORM_CREDENTIALS_FILE`, contiene únicamente las
claves `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `PROXMOX_VE_ENDPOINT` (HTTPS),
`PROXMOX_VE_API_TOKEN` y opcionalmente `AWS_SESSION_TOKEN`. Nunca se persiste en DB
ni se incorpora a backend.json, tfvars o imagen. El token Proxmox debe tener los
permisos mínimos de planificación sobre el laboratorio y la plantilla.
Si Proxmox usa una CA interna, montar el certificado autorizado y configurar
`IACP_TERRAFORM_CA_FILE`; se transmite como `SSL_CERT_FILE`. No deshabilitar TLS.

## Docker portable y validación

`compose.yaml`, `compose.server.yaml` y sus volúmenes/secretos existentes no se
modifican. El override optativo `compose.terraform.yaml` agrega el worker con
target `terraform-worker`, filesystem de solo lectura, sin capabilities, sin
Docker socket, y un volumen de planes externo nuevo para el entorno. No crea
automáticamente un volumen sustituto. Las rutas del host se suministran mediante
variables; dentro del contenedor las rutas son estables.

Antes de usar ese override deben existir el clon de solo lectura con el commit
fijo, el volumen privado y los archivos externos legibles por UID/GID 10001.
Exige `IACP_TF_WORKER_IMAGE`, `IACP_TF_SOURCE_PATH`, `IACP_TF_ALLOCATIONS_PATH`,
`IACP_TF_CREDENTIALS_PATH` e `IACP_TF_PLANS_VOLUME`. Para una CA interna agregar
un montaje de solo lectura y la variable indicada mediante configuración externa.
`IACP_TERRAFORM_PLAN_ENABLED` sigue en 0 hasta autorizar el plan real.

La migración `0003` solo añade las tablas de ejecuciones/intentos. `0001`/`0002`
y todas las filas previas se conservan; no hay downgrade destructivo. Ejecutar
migraciones/despliegue requiere el procedimiento operativo, respaldo y aprobación.

```bash
bash scripts/validate-docker.sh
IACP_IMAGE_TARGET=terraform-worker bash scripts/test-image-permissions.sh
# En el clon independiente de iac-proxmox:
bash scripts/test-docker.sh
```

Las pruebas usan Docker local y Terraform simulado; no requieren credenciales
reales. Cubren permisos, migraciones, cola Celery, state estable, exclusión mutua,
errores, cancelaciones, reintentos, SHA y rechazo de comandos mutantes. Ver
[VALIDATION_2B.md](VALIDATION_2B.md). No fusionar ni desplegar como parte de estas
validaciones. `apply` y `destroy` sobre Proxmox quedan fuera de esta entrega.
