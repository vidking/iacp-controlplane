# Recuperación drenada del piloto y control de permisos

El primer Apply real (`d3ecf7fe-08bf-4165-913c-9960acb02e4b`, intento 2, UUID `084c83e5-8d44-41a9-a527-f496cd83168b`) devolvió exit 1. La salida privada de Terraform no se conservó; no se atribuye un mensaje stderr que no se observó. La lectura posterior verificó VMID 30000 ausente, ninguna tarea nativa para ese VMID y state versionado estable con cero recursos: serial 1, lineage `988b3dd0-8538-fa59-ea9b-2d5f9b04f7de`. No se repite Apply ni se borra/resetéa state.

La autorización API real corresponde a `iacp-lab-approver`, distinta de `bootstrap`. El registro de revisión humana limita la aprobación a laboratorio; no representa una excepción corporativa de seguridad. El despliegue e87a8b6 quedó operativo con esquema 0008. Ante el fallo, `iacpctl rollback` cerró admisión/Lifecycle/DELETE y mantuvo Apply=1, siete servicios, HTTPS y datos.

## Bloqueo de permisos comprobado

En el fallo inicial, el token **`iacp-provision@pve!terraform-lab`** tenía VM.Allocate y Datastore.AllocateSpace, pero carecía de:

| Permiso | Ruta exacta | Alcance |
|---|---|---|
| VM.Clone | `/vms/29000` | Clonar exclusivamente la plantilla autorizada |
| SDN.Use | `/sdn/zones/localnetwork/vmbr0` | Usar el bridge sin tag; sin propagación a VLANs |

En esa lectura también carecía de Permissions.Modify. El administrador informó una corrección de RBAC y a las 23:11 UTC se releyó desde la identidad real del worker: VM.Clone, VM.Allocate, Datastore.AllocateSpace y SDN.Use están concedidos, con VM104 excluida. La concesión efectiva incluye ahora otros privilegios administrativos; el agente no los utilizó para modificar ACL ni red. El cambio de RBAC lo efectuó el operador fuera de este despliegue. Los permisos actuales de `/vms/104` están vacíos y deben continuar así. No se otorgan roles al Lifecycle token ni permisos sobre VM106.

La [API oficial de clone](https://github.com/proxmox/qemu-server/blob/master/src/PVE/API2/Qemu.pm) requiere VM.Clone sobre el origen, VM.Allocate sobre el destino y acceso a almacenamiento/red. La [comprobación oficial del bridge](https://github.com/proxmox/pve-guest-common/blob/master/src/PVE/GuestHelpers.pm) usa la ruta sin sufijo VLAN para tráfico untagged. Ese déficit impedía el aprovisionamiento aunque el plan fuera válido; no se afirma que sea el único contenido del stderr perdido.

**Referencia de mínimo privilegio, solo para un administrador de MNG autorizado. No ejecutada por esta release; no sustituye ni describe la concesión real informada por el operador.** Verificar primero los roles existentes; si el nombre existe con otros privilegios, detenerse sin modificarlo. Estos comandos añaden roles acotados y ACL al usuario padre y token (necesario cuando hay separación de privilegios); no sustituyen roles existentes. No utilizar root `/`, propagación general ni un rol administrador como solución.

```sh
pveum role add IACPLabTemplateClone29000 --privs 'VM.Clone'
pveum role add IACPLabUntaggedVmbr0 --privs 'SDN.Use'
pveum acl modify /vms/29000 --roles IACPLabTemplateClone29000 --users iacp-provision@pve --propagate 0
pveum acl modify /vms/29000 --roles IACPLabTemplateClone29000 --tokens 'iacp-provision@pve!terraform-lab' --propagate 0
pveum acl modify /sdn/zones/localnetwork/vmbr0 --roles IACPLabUntaggedVmbr0 --users iacp-provision@pve --propagate 0
pveum acl modify /sdn/zones/localnetwork/vmbr0 --roles IACPLabUntaggedVmbr0 --tokens 'iacp-provision@pve!terraform-lab' --propagate 0
```

Releer permisos efectivos mediante el token real. 0 y 1 significan concedido en la ruta exacta; ausencia significa denegado. Comprobar ambos permisos, VM104 excluida y ningún permiso nuevo del token Lifecycle. La nueva comprobación DHCP corre antes de planificar y de inicializar/consumir Apply. Si falta un permiso falla con un código explícito; no escribe state ni cruza started_at. Static mantiene su flujo anterior.

## Recuperar por API con admisión cerrada

`POST .../terraform/applies/{approval_id}/recover` sigue autenticado como operator y publica **solo reconcile**. `.../cancel` cerca una autorización consumida y `.../resolve-absence` exige approver independiente, evidencia de ambas fuentes y el vínculo exacto de plan/intento. Estas tres rutas administrativas permanecen disponibles con admisión 0. Crear, aprobar un Apply y ejecutar Apply siguen bloqueados. `/recover` nunca llama apply, import o destroy.

Se necesita desplegar esta corrección sin borrar la reserva uncertain. El driver admite únicamente una promoción **canónica y drenada**, sin cambio de esquema ni mount IaC, con `recovery_deployment_review` (archivo/hash) aprobado para la revisión y ventana actuales. La revisión contiene modo `readonly-apply-recovery`, approval_id, instance_id, attempt, plan_sha256 y source_revision. Antes de permitirla, el driver relee DB y hace observación readonly de S3/Proxmox: exige el mismo Apply consumido y uncertain, job fallido, VMID >=30000, DHCP, state estable vacío y VM ausente. Cualquier otra operación no terminal, tarea/cola pendiente, row running o reserva distinta bloquea. El hash histórico antes/después debe conservar íntegra la reserva. No existe un flag general para ignorar operaciones.

1. Promover imágenes mediante `iacpctl` con ese manifest revisado; la excepción solo se aplica dentro de deploy. `doctor`, `admit` y Lifecycle siguen exigiendo reposo normal.
2. Usar la ruta real de recover anterior, seguir `GET .../terraform/applies` y `GET /v1/operations/{operation_id}`. `RESOURCE_ABSENT` conserva uncertain hasta revisión; no basta una consulta a un contenedor.
3. Recoger dos GET actuales del state versionado y ausencia a nivel de clúster; incluir versión, lineage, serial, SHA-256, bucket/key y observaciones UTC en el cuerpo real de resolve-absence. No crear evidencia de memoria ni marcar ausencia si una fuente falla.
4. Un approver autorizado revisa y registra la ausencia por la API. La autorización consumida termina failed/RESOURCE_ABSENCE_CONFIRMED, conserva auditoría, S3 y VMID reservado. Nunca vuelve a Apply con otra clave.
5. Retirar la referencia de mantenimiento del manifest operativo (conservar archivos y journals históricos). Ejecutar doctor con gates cerrados. Esperar los dos permisos reales antes de cancelar el plan y solicitar un **nuevo intento con nueva aprobación** del mismo UUID/VMID, mediante los endpoints existentes.

No borrar state vacío para ocultar el fallo, no reutilizar una aprobación consumida, no modificar filas con SQL, no usar qm ni desplegar otra API/orquestador. Ver [piloto DHCP](DHCP_PILOT.md) y [operación canónica](OPERATIONS.md).
