# IACP Orchestrator: autoservicio de Desarrollo

Contrato **0.9.1**, migración **0010_guest_identity**. Consultar
[la evidencia](validation/SELF_SERVICE.json) para distinguir la revisión probada,
la release ejecutada y las pruebas reales de los adaptadores simulados.
Entrada operativa única: `scripts/iacpctl`, Compose
`deploy/mng/compose.yaml` y driver `scripts/go_live.py`.

## Consumo desde Swagger, sin Proxmox

Entrar a `https://iac.cyberfuel.com/docs` desde LAN/VPN. HTTP Basic docs-only
abre documentación. **Authorize** recibe el token Bearer del tenant; la
contraseña de documentación no permite operar la API. `reader` consulta,
`operator` solicita comandos, `approver` revisa políticas y planes. Desarrollo
no necesita credenciales, VMIDs, shell ni acceso directo a Proxmox.

1. Consultar `GET /v1/profiles`, `GET /v1/lab/policies` y `GET /v1/lab/usage`.
   Elegir imagen/perfil admitidos y una política `approved` vigente de su tenant.
2. Ejecutar `POST /v1/instances` con nombre nuevo y una clave por intención.
   Reemplazar `self_service_policy_id` por el UUID **real devuelto por la API**.
   La aprobación de una política no representa creación ni autorización comercial.
3. Conservar `instance_id`, `operation_id`, `job_id` e `Idempotency-Key`. Seguir
   `GET /v1/operations/{operation_id}` hasta estado normalizado terminal. HTTP 202
   significa admisión. `ready` y `infrastructure_kind=kvm` acreditan la observación
   de infraestructura; una IP o `running` no certifican una aplicación lista.
4. Usar `/power/start`, `/power/reboot` y `/power/shutdown` con `{}` y claves
   distintas por intención. Consultar `/power`. El apagado es ordenado; no existe
   un `force`, `/stop`, pausa de memoria ni suspensión de hipervisor pública.
5. Consultar `ip_addresses` y `network_observation`; `/network/discover` repite
   observación Guest Agent con MAC verificada. Una lista vacía es IP pendiente.
   El perfil Ubuntu del laboratorio inicializa automáticamente la identidad tras
   el primer start: consultar el historial de la instancia para la operación
   `instance.network.initialize`. Esperar `completed` antes de utilizar la IP.
6. Apagar la VM antes de `DELETE /v1/instances/{instance_id}`, cuerpo `{}` y clave
   nueva. En una VM creada bajo la política, el IaC Worker prepara el plan,
   verifica su único recurso, registra delegación y ejecuta el plan guardado.
   Seguir la operación hasta `completed`/`deletion.phase=deleted`.

Solicitud de creación, con tipos y límites válidos del perfil de ejemplo:

```json
{
  "name": "desarrollo-kvm-01",
  "image": "ubuntu-2404",
  "vcpus": 2,
  "memory_mb": 2048,
  "disk_gb": 20,
  "network_profile": "lab-dhcp",
  "self_service_policy_id": "00000000-0000-4000-8000-000000000001"
}
```

El UUID del ejemplo solo ilustra formato. No es una política operativa. Omitir
`node_preference` distribuye reservas entre los destinos permitidos según la
cantidad de instancias reservadas y desempate estable. Incluir por ejemplo
`"node_preference":"mng01"` exige que el perfil autorice ese destino. El nodo de
la plantilla permanece fijo cuando el destino cambia. El catálogo no expone
claves, credenciales, backend ni rutas privadas.

## Tabla del contrato

Las rutas existentes conservan paths, métodos y operation IDs. Los campos nuevos
son aditivos. Los endpoints administrativos históricos `/terraform/...` conservan
su semántica y el nombre real del motor para operación/auditoría/licencias.
El flujo de Desarrollo usa **IACP** e **IaC Worker** y no invoca Terraform/OpenTofu.
El motor implementado continúa siendo Terraform 1.16.6 ([inventario y licencias](ENGINE_INVENTORY.md)), con fuente IaC fijada en
`iacp.terraform.config.SOURCE_REVISION`; no se afirma compatibilidad OpenTofu.

| Método / ruta | Cuerpo | Resultado / permisos | Disponible en 0.8 |
|---|---|---|---|
| GET `/v1/profiles` | — | 200 catálogo del tenant, reader | No |
| GET `/v1/lab/usage` | — | 200 reservas y operaciones, reader | No |
| POST `/v1/instances` | creación anterior | 202 IDs, operator + clave | Sí; delegación nueva en 0.9 |
| GET `/v1/instances` | — | 200 array; `limit=1..100`, `offset`, filtros `status`, `name` exacto, `network_profile`, `node`; `X-Total-Count` | Sí; filtros/header nuevos |
| GET `/v1/instances/{id}` | — | UUID, VMID, nodo, recursos, estado, IP conocida, política; reader | Sí |
| GET `/v1/operations` | — | 200 historial, `instance_id`, `status`, limit/offset | No |
| GET `/v1/operations/{id}` | — | eventos, error, `normalized_status`, `recovery_required`, progreso IaC/Lifecycle/DELETE | Sí; progreso IaC nuevo |
| POST `/v1/instances/{id}/provision` | `{}` | 202 continúa el mismo aprovisionamiento; operator | No |
| POST `/v1/instances/{id}/retry-provision` | `{"expected_attempt":1}` | 202 nuevo intento de planificación, mismo UUID/VMID/state; clave | No |
| POST `/v1/instances/{id}/reconcile` | `{}` | 202 observación de mutación consumida; operator | No |
| GET `/v1/instances/{id}/power` | — | 200 estado de energía y bloqueo; reader | Sí |
| POST `/v1/instances/{id}/power/start` | `{}` | 202, operator + clave | Sí |
| POST `/v1/instances/{id}/power/reboot` | `{}` | 202, operator + clave | Sí |
| POST `/v1/instances/{id}/power/shutdown` | `{}` | 202, operator + clave | Sí |
| POST `/v1/instances/{id}/network/discover` | `{}` | 202 observación DHCP; operator + clave | Sí |
| POST `/v1/instances/{id}/network/initialize` | `{}` | 202 inicialización fija aprobada; entrega automática tras start, misma clave para recuperar entrega; operator | No |
| DELETE `/v1/instances/{id}` | `{}` bajo política; contrato antiguo conserva operation_id/plan_sha256 | 202; operator + clave; ausencia verificada antes de liberar cuota | Sí; cuerpo vacío/delegación nuevos |
| POST `/v1/instances/{id}/terraform/deletion-plan` | `{}` | Solo plan; **no activa eliminación automática** | Sí |
| POST `/v1/instances/{id}/terraform/deletion-approval` | operation_id, plan_sha256, valid_for_seconds | 201 aprobación individual independiente | Sí |
| POST `/v1/instances/{id}/terraform/deletions/{op}/recover` | `{}` | 202 lectura tras consumo; operator original | Sí |
| POST `/v1/instances/{id}/terraform/deletions/{op}/cancel` | `{}` | 200 cancela únicamente antes de started_at | Sí |
| POST `/v1/lab/policies` | propuesta siguiente | 201 propuesta; operator + clave | No |
| GET `/v1/lab/policies`, `/{id}` | — | lista paginada / política, digest e historial; reader | No |
| POST `/v1/lab/policies/{id}/approve` | `{"policy_sha256":"digest real"}` | 200; approver distinto del proponente | No |
| POST `/v1/lab/policies/{id}/renew` | mismo digest real | 200 nueva revisión independiente; misma membresía e historial | No |
| POST `/v1/lab/policies/{id}/revoke` | `{}` | 200 irreversible; approver; también con admisión cerrada | No |

Los nombres `{id}` representan UUID IACP, nunca VMID. Una clave se conserva por
intención, no se genera otra por timeout. UUID de instancia identifica el recurso;
operation_id identifica una intención; `X-Request-ID` identifica cada petición HTTP.
No borrar los registros de claves para reutilizarlas. Nombre único por tenant
incluye tombstones: usar otro nombre para una nueva instancia.

## Delegación y cuotas

El administrador configura techos en el perfil del servidor. Un operador propone:

```json
{
  "environment": "lab",
  "profiles": ["lab-dhcp"],
  "actions": ["create", "start", "shutdown", "reboot", "delete"],
  "max_instances": 3,
  "max_vcpus": 8,
  "max_memory_mb": 8192,
  "max_disk_gb": 80,
  "max_concurrent_operations": 2,
  "valid_for_seconds": 86400,
  "change_reference": "CAMBIO-LAB-REVISADO"
}
```

Un approver independiente revisa el digest y usa `/approve`. El digest incluye
tenant, todas las cuotas, acciones, perfiles completos y revisión IaC fijada.
La vigencia máxima es siete días; `/renew` requiere otra revisión del mismo digest
y perfiles intactos, conserva el instante de aprobación original y añade historial.
La política revocada no se renueva. Una política expirada/revocada impide comandos
nuevos; reconciliar mutaciones ya consumidas permanece disponible.

VM, CPU, RAM, disco y concurrencia se calculan sobre **todo el tenant**, incluyendo
otros perfiles y reservas fallidas/inciertas. La política no aumenta el techo del
servidor. Las reservas se serializan en PostgreSQL con un advisory lock
transaccional; reserva VMID, instancia, operación y job se confirman juntos. Los
VMIDs se contrastan con el inventario de clúster y nunca se reciclan dentro del
historial IACP. Si hay recursos históricos del mismo tenant, consumen capacidad:
`max_instances=3` no implica tres VMs adicionales.

El vínculo de una instancia nueva sella UUID, tenant, nombre, recursos, imagen,
perfil, red, asignación, state key, revisión IaC y política. No existe API de
adopción ni edición del vínculo. Las autorizaciones por plan registran
`approved_by=policy:<UUID>`; ese actor representa delegación y enlaza la política
con el approver humano original. No se presenta como aprobación humana por VM.
Los registros de políticas conservan proponente, approver, digest, vigencia y
`review_history`, incluidas renovación y revocación.

Apply, Lifecycle, DELETE y admisión mantienen sus flags. La mutación revalida
tenant, política, propiedad, vigencia, plan y contexto antes de consumir su fence.
La VM debe estar apagada, sin locks/pending/HA para DELETE. El plan debe tener
exactamente una acción delete del recurso propio; nunca se aplica un plan global.
VM 104 y VM 106 no pueden entrar en esta política. Al activar laboratorio, una
allowlist Lifecycle vacía solo permite miembros de políticas válidas; no abre
las instancias históricas.

## Reintentos y errores

| Situación | Acción segura |
|---|---|
| 401 / 403 | Corregir credencial o rol, sin cambiar la intención comercial |
| 404 | UUID ausente o ajeno; no revela otro tenant |
| 409 Idempotency conflict | Conservar el cuerpo original o abrir una intención distinta explícita |
| 409 LAB_QUOTA_EXCEEDED:* | Consultar usage; esperar operaciones o limpiar recursos propios verificados; no cambiar de perfil para eludirlo |
| 409 política no autorizada / perfil cambiado | Revisar vigencia, revocación, permisos y digest; requiere revisión independiente para renovar |
| 422 | Corregir tipos/campos/límites; nunca enviar tenant_id, VMID, IP o force |
| 503 admisión cerrada | Respetar Retry-After; consultar operaciones; `/reconcile` sigue disponible |
| entrega de creación no confirmada | Repetir la misma clave para recuperar IDs; `/retry-provision` solo si no hubo Apply consumido |
| plan listo / entrega Apply pendiente | `/provision` continúa el mismo UUID y autorización; redelivery cercada |
| `normalized_status=uncertain` | POST `/reconcile`, mismo UUID; solo lectura de state/Proxmox o UPID existente |
| DELETE rechazado porque está encendida | Apagar ordenadamente y generar otra intención de DELETE después del fallo terminal |

`status` histórico conserva compatibilidad. `normalized_status` eleva un Apply
incierto a `uncertain` aunque el status histórico sea failed. `provisioning.phase`
y `recovery_required` evitan que el CRM consulte endpoints del motor interno.
La observación no importa, borra, desbloquea ni reescribe state; no repite Apply.
Una ausencia no confirmada conserva la reserva. La resolución independiente de
ausencia y los controles históricos no se sustituyen por fixtures o SQL.

## Archivos y parámetros exactos

| Cambio | Fuente |
|---|---|
| Perfil disponible por tenant | Host activo `/etc/iacp/terraform/profiles-guest-identity-370ce98.json` (ruta seleccionada por env; no editar en caliente), `profiles.<nombre>.allowed_tenants`; env host `IACP_TF_PROFILES_PATH`; mount API/IaC Worker `/run/config/profiles.json`; env del contenedor `IACP_TERRAFORM_PROFILES_FILE` |
| Plantilla / imagen | `profiles.<nombre>.settings.template_vm_id`, `.template_node_name`, `.image`; API `image` debe coincidir; otra imagen usa otro perfil |
| CPU solicitada / techo por VM | API `vcpus`; perfil `.max_vcpus` |
| RAM solicitada / techo por VM | API `memory_mb` (MiB, múltiplo de 256); `.max_memory_mb` |
| Disco / mínimo / techo por VM | API `disk_gb` (GiB); `.settings.boot_disk.minimum_size_gb`, `.max_disk_gb`; storage `.settings.boot_disk.datastore_id`, `.clone_datastore_id`, `.cloudinit_datastore_id` |
| Cuotas agregadas | Perfil `.max_live_instances`, `.max_total_vcpus`, `.max_total_memory_mb`, `.max_total_disk_gb`, `.max_concurrent_operations`; propuesta de política limita aún más |
| Destinos | `.target_nodes`, `.settings.allowed_nodes`, `.settings.node_name`; API `node_preference` opcional |
| Red de laboratorio | `.settings.network_bridge="vmbr0"`, `.network_mode="dhcp"`, `.vlan_id=null`; omite gateway/IP estáticos y VLAN tag; nunca usa IPAM |
| IPAM estático existente | Host `IACP_TF_ALLOCATIONS_PATH`, contenedor `IACP_TERRAFORM_ALLOCATIONS_FILE`, asignación por tenant/nombre con `network_mode=static`, ipv4_cidr/gateway y VLAN explícitos; no modificar para laboratorio |
| Cloud-Init / Guest Agent | `.settings.cloudinit_username`, `.ssh_public_keys`, `.guest_agent_enabled`; plantilla debe soportarlos; `guest_ready` sigue desconocido |
| Identidad de la clon Ubuntu DHCP | Perfil `.guest_initialization="ubuntu-2404-lab-identity-v1"`; programa fijo y digest en `app/iacp/services/guest_identity.py`, incluidos en la revisión de política; no es una variable Terraform ni un comando del consumidor |
| Backend y propiedad | `.settings.backend.bucket/region/endpoint`; state key la genera IACP por tenant/UUID, nunca el cliente |
| Habilitar propuesta autoservicio | Perfil `.self_service_enabled=true`; no equivale a abrir admisión |
| Activación | `/etc/iacp/server.env`: `IACP_LAB_SELF_SERVICE_ENABLED`, `IACP_ADMISSION_ENABLED`, `IACP_TERRAFORM_APPLY_ENABLED`, `IACP_TERRAFORM_DELETE_ENABLED`, `IACP_KVM_LIFECYCLE_ENABLED`; driver revisado, nunca editar flags para evadirlo |
| Scope individual histórico | `IACP_DISPOSABLE_INSTANCE_IDS`, `IACP_LIFECYCLE_INSTANCE_IDS`; no convierten VMs existentes en miembros de una política |
| Credenciales y autenticación | `IACP_TENANT_CREDENTIALS_PATH`, secrets API, `IACP_TF_CREDENTIALS_PATH` y `IACP_KVM_CREDENTIALS_PATH`; nunca enviados a Desarrollo ni Git |

Ejemplo completo de formato en [lab-profiles.example.json](deployment/lab-profiles.example.json).
No es autorización ni configuración operativa. `deploy/profiles.json` continúa
vacío por defecto. Cambiar un perfil invalida el digest vigente para admisión y
mutaciones IaC: preparar otra política y revisión; no editar el snapshot en DB.

## Release, migración y piloto real

Verificar SHA del checkout, labels de imágenes **realmente ejecutadas**, esquema
y OpenAPI antes de cada promoción. Una integración Git no despliega contenedores.
Preparar release fuera de la activa, imágenes con label de commit, respaldo y
restore acreditados, manifest con IDs y ventana reales, y config canónica.
Ejecutar `iacpctl ... doctor`, luego `iacpctl ... deploy`. La migración 0009 crea
políticas y campos nulos para todas las instancias previas, y marca
`deletion_executions.autonomous=false`; no atribuye propiedad histórica. El driver
compara hashes de filas proyectados a 0008. La migración 0010 añade únicamente
`guest_initializations`, sin adoptar ni inicializar instancias previas. Rollback cierra flags y conserva el
esquema y la historia; nunca ejecuta downgrade.

Una operación DELETE awaiting_approval impide el drenado normal. Resolverla por
su flujo aprobado o cancelar **antes de started_at**, conservando su evidencia,
mediante la API si el cambio operativo lo autoriza. No agregar una excepción
general a `check_idle`, modificar SQL ni declarar completado un plan pendiente.

Tras desplegar con flags OFF: preparar archivo de perfiles dentro de recursos
verificados, proponer política por API y obtener aprobación independiente real.
El cambio `activate-lab` exige revisión externa, perfil/hash, políticas/digests
aprobados y capacidad/red verificadas. Template de revisión en
[lab-self-service-review.example.json](deployment/lab-self-service-review.example.json).
Ejecutar únicamente:

```sh
sudo /opt/iacp/releases/RELEASE/scripts/iacpctl \
  --manifest /etc/iacp/changes/manifest-lab.json \
  --evidence-dir /opt/iacp/evidence/activate-lab-VENTANA activate-lab
```

El driver activa admisión/Lifecycle/DELETE y delegación en API e IaC/Lifecycle
workers, manteniendo las allowlists históricas vacías. No añade un despliegue
paralelo ni modifica DB/Redis/Traefik. El rollback cierra delegación y comandos;
las observaciones consumidas continúan. No habilitar VM 106 como sustituto.

Piloto real posterior: crear una instancia y seguir el resultado; enviar dos
POST concurrentes adicionales si cuota/capacidad lo permiten; verificar UUIDs y
VMIDs distintos y repetir cada clave; iniciar/reiniciar/apagar, descubrir IP,
simular una falla recuperable autorizada, reconciliar el mismo UUID, comprobar
tenant ajeno y cuota, y eliminar solo las instancias expresamente autorizadas.
Guardar solicitudes/respuestas sin tokens, eventos, UPIDs, planes, versión/lineage/
serial/hash de state, dos lecturas coherentes, ausencia nativa e idempotencia.
Usar exclusivamente la API para mutaciones. Los adaptadores de pruebas nunca
constituyen evidencia de varias KVM reales.

## Identidad DHCP de clones Ubuntu

El piloto de tres clones detectó MAC y SMBIOS diferentes con el mismo machine-id
heredado y el mismo CLIENTID DHCP: las tres recibieron 192.168.3.59. Esa ejecución
no acredita red multiinstancia. Se eliminaron por API antes de repetir el piloto.

El perfil opt-in `ubuntu-2404-lab-identity-v1` agrega una inicialización fija en el
IaC Worker después de start. Su código y SHA forman parte del snapshot/digest
revisado de la política. Requiere una VM nueva ligada a esa política, Apply real
completado, VMID >=30000, nombre/nodo/MAC/Cloud-Init coherentes, Ubuntu 24.04,
netplan/networkd y Guest Agent con el permiso preexistente
`VM.GuestAgent.Unrestricted`. No se conceden permisos por petición.

El programa espera Cloud-Init, conserva `50-cloud-init.yaml`, crea un override
`/etc/netplan/99-iacp-identity.yaml` con `dhcp-identifier: mac`, genera machine-id
aleatorio y conserva el vínculo en `/var/lib/iacp/guest-identity.json`. El nuevo
machine-id persistente se usa por todos los servicios después del siguiente
reboot. No modifica la plantilla 29000, usuarios, claves, paquetes, IPAM ni state.
El contrato HTTP solo admite `{}`: no permite ejecutar programas ni escribir
archivos elegidos por el cliente.

El registro DB conserva binding de propiedad, modo, SHA, MAC, instante de consumo,
PID y resultado. El consumo se confirma antes del único POST al Guest Agent.
Redelivery, misma clave y `/reconcile` después de ese instante solo leen el PID
existente y la evidencia persistente, incluso si se perdió la respuesta con el PID.
Una incertidumbre retiene cuota y bloquea energía/DELETE hasta conciliarla.
Un fallo previo al consumo admite repetir **la misma clave**, tras corregir su
causa, sin crear otro registro ni otra VM. La clave automática es
`identity-<UUID de instancia>`.

La verificación exige hashes de los archivos, MAC propia, concesión con CLIENTID
`01<MAC sin separadores>` e IP reportada por el agente. No publica IPs hasta ese
momento y rechaza una dirección ya conocida de otra KVM DHCP viva. Después de
reboot se repite descubrimiento readonly. `guest_ready` sigue null: esta validación
no certifica una aplicación. Para otras distribuciones utilizar una plantilla
correctamente preparada o un adaptador versionado y revisado; no habilitar este
modo en perfiles estáticos/producción.
