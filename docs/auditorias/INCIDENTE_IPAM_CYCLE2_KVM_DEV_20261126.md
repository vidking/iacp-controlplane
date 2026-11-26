# Incidente de contención IPAM en matriz KVM de Dev

**Fecha:** 2026-11-26 (UTC)

**Entorno:** `dev-iac.cyberfuel.com`, clúster MNG, nodo `mng01`

**Revisión ejecutada:** `2424d45047e2382473ff298fa9c248ebeace2107`

**Imagen ejecutada:** `sha256:837ace28ce6da6c8faf24ce6c9e194456c26ac362db215dd475096122f568d53`

**Resultado del lote:** `BLOCKED`; no cuenta como ciclo certificado.

## Causa raíz

En el tercer lote de seis aprovisionamientos simultáneos del ciclo KVM 2, la
operación `37bdf328-0bb6-4638-abf5-61b5b069f51a` de `kvm-prueba-615`
(AlmaLinux 9, plan `kvm-1`) terminó con `IPAM_ALLOCATION_BUSY`. El registro
interno muestra que el intento comenzó a las `2026-11-26T19:22:12Z` y falló a
las `19:22:25Z`.

`NetBoxClient.allocate()` serializa las asignaciones del prefijo compartido
mediante un advisory lock de PostgreSQL mientras confirma la reserva remota.
El `lock_timeout` de 12 segundos no cubría la espera producida por seis
workers que solicitaban direcciones a la vez. La adquisición del lock falló
antes de llamar a NetBox, por lo que no se creó una reserva remota para ese
caso. La operación quedó `failed`, sin resultado incierto.

El control del runner detuvo nuevas inyecciones al detectar el fallo. Las otras
cinco operaciones terminaron correctamente. El mensaje `Bad Request` observado
por el usuario pertenecía a la interfaz de Codex y no fue una respuesta de esta
API.

## Reconciliación y limpieza

- El inventario de Proxmox del clúster MNG no contenía el VMID `41657`. La
  reconciliación canónica confirmó `RECONCILIATION_PHYSICAL_RESOURCE_ABSENT`.
- La instancia fallida terminó eliminada por el `DELETE` canónico. Su VMID quedó
  liberado y su registro IPAM terminó `released` sin dirección asignada.
- Las otras cinco KVM se apagaron y eliminaron por la API. Para cada una
  pasaron los controles aplicables de identidad, ausencia en Proxmox, liberación
  de IP en NetBox, lease VMID y state Terraform vacío.
- Estado posterior: 0 operaciones activas, 0 `uncertain`, 0 jobs de servicios,
  0 advisory locks. Los seis workers de infraestructura y los seis workers de
  servicios estaban idle y sanos.

Evidencia:

- Lote y bloqueo: `/opt/iacp/evidence/wan-dev-2424d45-kvm-cycle2/results.jsonl`
- Preflight posterior: `/opt/iacp/evidence/wan-dev-2424d45-kvm-cycle2-recovery-preflight-20261126201500/results.jsonl`
- Reconciliación y borrado lógico: API de Dev, operaciones
  `9dae4cfc-aabb-4214-b188-05a83b95393a` y
  `c0b67aba-7017-4d36-b5ef-fa8c07860bc2`.
- Limpieza de las cinco KVM listas:
  `/opt/iacp/evidence/manual-cleanup-cycle2-batch3-20261126T220500Z/results.jsonl`

## Corrección preparada

`worker/netbox.py` eleva a 120 segundos el límite de espera del advisory lock
para asignación y liberación. La exclusión mutua por prefijo se conserva; el
cambio permite que las seis solicitudes autorizadas esperen a que termine la
sección crítica remota. Si la espera excede el nuevo límite, el fallo sigue
siendo cerrado y ocurre antes de enviar una mutación a NetBox.

La prueba enfocada de `tests/test_audit_worker.py` verifica el límite en ambas
rutas y que una adquisición fallida no envíe solicitudes a NetBox. La suite
Docker aislada pasó con 704 pruebas y el runtime local aislado pasó. El nuevo
build, despliegue y matrices físicas en Dev siguen pendientes. La matriz del
nuevo digest comenzará desde el ciclo 1; los resultados de `2424d450` no
certifican una imagen distinta.
