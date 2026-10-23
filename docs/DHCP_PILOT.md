# Piloto DHCP y pruebas desde Swagger (0.8)

El perfil del servidor decide el direccionamiento, sin condiciones por nombre de clúster. El perfil de laboratorio `lab-dhcp` autoriza un tenant, rango VMID >=30000 y capacidad máxima de una instancia viva. Usa vmbr0, VirtIO, VLAN `null` (atributo omitido en Proxmox), Cloud-Init `ip=dhcp`, sin gateway estático. Nunca abre, reserva ni libera IPAM. El modo estático existente conserva IPAM, gateway y VLAN 204. Un OFFER diagnóstico demuestra servicio DHCP, no asignación de una dirección a una VM.

## Swagger como cliente del panel CRM

Abrir https://iac.cyberfuel.com/docs desde LAN/VPN con TLS válido. El login HTTP Basic de documentación usa el rol `docs`; obtenerlo por el handoff privado descrito en [OPERATIONS](OPERATIONS.md). **Authorize** requiere el token Bearer del tenant (solo el token, sin prefijo). Basic y Bearer son identidades distintas. Swagger no guarda la autorización entre sesiones. Anónimo recibe 401; reader consulta, operator solicita comandos, approver revisa planes con sujeto diferente del solicitante. No compartir tokens administrativos con el CRM.

Usar un nombre nuevo y una Idempotency-Key UUID por intención; conservar la clave al reintentar. No introducir VMID, IP, VLAN ni credenciales en cuerpos HTTP. Payload real de `POST /v1/instances` (202):

```json
{"name":"iacp-mvp-dhcp-01","image":"ubuntu-2404","vcpus":2,"memory_mb":2048,"disk_gb":20,"network_profile":"lab-dhcp"}
```

1. `GET /v1/instances` y `GET /v1/instances/{instance_id}` consultan inventario del tenant. `GET /v1/operations/{operation_id}` sigue eventos. HTTP 202 no acredita creación de infraestructura.
2. `GET /v1/instances/{instance_id}/terraform` devuelve intento y SHA del plan. Un approver distinto revisa el plan y usa `POST .../terraform/approvals` con `{"attempt":1,"plan_sha256":"<sha real>","valid_for_seconds":900}`. El operator usa `POST .../terraform/apply` con `{"approval_id":"<uuid aprobado>"}` y otra clave. Seguir el job/Apply hasta completed; no repetir Apply ante uncertain.
3. Con propiedad, identidad y controles del piloto acreditados, `iacpctl activate-pilot` habilita Lifecycle **solo para los UUID revisados**. VM 104 y VM 106 no se autorizan por esta activación.
4. `POST .../power/start`, `POST .../power/reboot`, `POST .../power/shutdown` aceptan cuerpo vacío `{}` y devuelven operación 202. No existe `/power/stop`. Consultar operación y `GET .../power`. Repetir la misma clave confirma idempotencia, sin segundo comando.
5. Después de start/reboot se programa descubrimiento readonly en el Terraform Worker. `POST .../network/discover`, cuerpo `{}` y clave nueva, permite repetir la observación. Consulta QEMU Guest Agent y liga IPv4 a la MAC de la KVM. `ip_addresses: []` y `network_observation.status: pending` son válidos; no fallan creación. `guest_ready` continúa null: una IP no certifica Cloud-Init ni disponibilidad de la aplicación.
6. DELETE requiere habilitación administrativa separada, allowlist UUID y VMID >=30000. `POST .../terraform/deletion-plan` prepara plan destroy; revisar `operation.deletion.plan_sha256`. Un approver independiente llama `POST .../terraform/deletion-approval` con `{"operation_id":"<uuid>","plan_sha256":"<sha real>"}`. `DELETE /v1/instances/{instance_id}` usa el mismo cuerpo y **la misma clave del deletion-plan**. No sustituir por DELETE nativo de Proxmox. Ver [SAFE_DELETE](SAFE_DELETE.md).

CRM puede consultar con reader y solicitar creación/energía con operator del tenant autorizado. Aprobar Apply/DELETE y habilitar admisión/Lifecycle son funciones privilegiadas. 403 = rol insuficiente; 404 oculta otro tenant; 409 = conflicto/idempotencia/propiedad/gate; 422 = payload inválido; 503 = admisión cerrada o dependencia indisponible. No convertir estos rechazos esperados en pruebas fallidas del sistema.

## Archivos exactos para cambios autorizados

| Parámetro | Configuración del servidor (no Git) | Validación/código | Terraform fijado por release |
|---|---|---|---|
| DHCP/static, bridge, VLAN | `/etc/iacp/terraform/profiles.json`, `profiles.<perfil>.settings.{network_mode,network_bridge,vlan_id}`; static legacy `/etc/iacp/terraform/allocations.json` | [profiles.py](../app/iacp/terraform/profiles.py), [config.py](../app/iacp/terraform/config.py) | `iac-proxmox/stacks/kvm-instance/{variables.tf,main.tf}`, `modules/compute/kvm/{variables.tf,main.tf}` |
| Plantilla, nodos, almacenamiento | mismo perfil, `settings.{template_vm_id,template_node_name,node_name,allowed_nodes,clone_datastore_id,cloudinit_datastore_id,boot_disk}` | mismas rutas; nunca VM104 como plantilla | `modules/compute/kvm/main.tf`: clone, initialization, disk |
| CPU/RAM/disco solicitados | payload `{vcpus,memory_mb,disk_gb}`; máximos del perfil `{max_vcpus,max_memory_mb,max_disk_gb}`; mínimo `settings.boot_disk.minimum_size_gb` | [schemas.py](../app/iacp/schemas.py), [executor.py](../app/iacp/terraform/executor.py) | `modules/compute/kvm/main.tf`: cpu, memory, disk |
| VMID/rango y capacidad | `vmid_min`, `vmid_max`, `max_live_instances`, `allowed_tenants` | reserva persistida en TerraformExecution, lock global de identidad y unicidad DB | `stacks/kvm-instance/variables.tf`: vm_id |
| Guest Agent | `settings.guest_agent_enabled`; requiere agente operativo en el invitado | [network.py](../app/iacp/services/network.py), [worker.py](../app/iacp/terraform/worker.py) | `modules/compute/kvm/main.tf`: agent.wait_for_ip.disabled=true |
| Fuente Terraform | `/etc/iacp/server.env`: `IACP_TF_SOURCE_PATH` | pin `SOURCE_REVISION` en config.py y go_live.py | release fuente `2c0f319ba5d7a45987cd9d0395673828c28f678b`, archive SHA `05ed1d0d6f2b886df8072eb24cd764dc881f63e9f536baa9af3bafa0804d156b` |

En VM104 el checkout de fuente previsto es `/opt/iacp/sources/iac-proxmox-dhcp-2c0f319`; módulos en esa ruta. Verificar el mount `/iac-source` realmente ejecutado. Cambiar una fuente sellada requiere nueva revisión, checksum, imágenes y promoción; no editarla en caliente para sortear postcondiciones. Un cambio de perfil afecta nuevas solicitudes; no reescribe allocations, UUID ni state existentes. Los perfiles deben ser root:10001, 0440; env activo 0600; no lectura mundial. El perfil contiene solo claves SSH públicas, nunca privadas.

## Promoción y recuperación

Usar exclusivamente `scripts/iacpctl`, proyecto iacp-controlplane y Compose canónico. Manifest revisado incluye `source_promotion`, `profile_configuration` (ruta/hash), backup/restore reciente con schema real, imágenes exactas y registro de cambio del operador. La comparación solo admite las seis imágenes, el bind readonly del perfil, la fuente fijada y flags nuevos inicialmente OFF/vacíos; rechaza todo otro drift.

Migraciones ordenadas 0007_safe_deletion y 0008_dhcp_profiles, conservando 0006 y registros/certificados históricos. La comparación de historia proyecta los campos anteriores y exige defaults estáticos en las columnas nuevas; no ignora filas reescritas. No hay downgrade. Rollback funcional cierra admisión/Lifecycle/DELETE, conserva Apply=1, DB, volúmenes y state; no implica volver un binario antiguo incompatible.

```sh
sudo /opt/iacp/releases/iacp-dhcp-pilot/scripts/iacpctl --manifest /etc/iacp/changes/manifest-dhcp-pilot.json --evidence-dir /opt/iacp/evidence/dhcp-deploy-$(date -u +%Y%m%dT%H%M%SZ) deploy
```

Antes: config-check, comparación semántica, preflight y env-promote del candidato revisado. Después: doctor, siete roles operativos, TLS/docs y rechazo anónimo. `admit` exige propósito dhcp-provisioning, perfil/hash/tenant revisados y ausencia VMID; no habilita Lifecycle. `activate-pilot` exige identidad UUID/VMID/nodo/nombre, state owned estable, Proxmox detenido con DHCP sin tag, prueba DHCP y HTTPS verificadas. Esto no aprueba VM106 ni reemplaza los controles WAN históricos.

Pruebas automatizadas unitarias/mock e integración PostgreSQL/Redis no son prueba de un clúster real. La evidencia del piloto real debe incluir UUID, claves/operation_id sin tokens, tiempos, VMID/nodo, valores CPU/RAM/disco, net0/ipconfig0, concesión observada, UPID, versiones/lineage/serial/hash Wasabi. Energía no modifica state. DELETE debe dejar tombstone y auditoría, Proxmox ausente y state vacío con lineage conservado. Una respuesta uncertain solo admite reconciliación readonly; jamás reenviar una mutación consumida.

La recuperación drenada y los permisos efectivos de clonación se detallan en [DHCP_RECOVERY](DHCP_RECOVERY.md). No repetir Apply si el resultado es incierto.
