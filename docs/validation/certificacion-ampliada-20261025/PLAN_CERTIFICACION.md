# Plan de ampliación de certificación del MVP KVM

Fecha: **25/10/2026, America/Costa_Rica**. Inspección inicial de solo lectura a las **13:07–13:09**. Las nuevas evidencias se conservan separadas del cierre de Fase G.

## Estado comprobado

- `origin/main`: **5916a6df21fcec09e21215dafd24605dd8fb474c**, merge PR #73. Contiene `5aa7daf`; no hay diferencias en aplicación, Compose, `go_live.py` o `iacpctl` respecto de dedb344. **No corresponde redesplegar.**
- IACP Control Plane (VM 104): las imágenes efectivas continúan siendo las de Fase G. API y readiness responden HTTP 200 por HTTPS; `iacpctl doctor` pasa sin deriva y con los siete servicios en ejecución. El release operativo certificado sigue siendo **dedb344b49d401c4da00d0271882881a0ee34dae**; el merge documental no es una promoción.
- `GET /v1/plans`, `/v1/images`, `/v1/profiles` y `/v1/lab/usage`: HTTP 200. Snapshot: [inventario-runtime.json](inventario-runtime.json).
- KVM 1/2 disponibles; KVM 3/4 responden `COMMERCIAL_PLAN_NOT_AUTHORIZED` en disponibilidad. No se intentó una mutación para demostrar un rechazo que ya informa el catálogo.
- Ubuntu 24.04 habilitada. Ubuntu 26.04, AlmaLinux 9 y AlmaLinux 10 pendientes con `IMAGE_CERTIFICATION_PENDING`.
- Proxmox confirma las cuatro plantillas en mng01 y DS_CYBERFUEL, `template=1`, sin locks. Existencia y `agent=enabled` en configuración **no certifican** que Cloud-Init, QEMU Guest Agent o acceso funcionen en un clon.

## Bloqueos concretos y ajustes mínimos

### Presupuesto de una VM de ensayo

Uso actual: una instancia histórica, 2 vCPU, 2048 MiB y 20 GiB; cero operaciones concurrentes. Límites vigentes: planes 1/2, 2 instancias, 4 vCPU, 6144 MiB, 120 GiB y concurrencia 1.

La [propuesta no aplicada](presupuesto-secuencial.propuesto.json) suma al uso existente una sola KVM 4: **2 instancias totales, 10 vCPU, 18432 MiB, 420 GiB y concurrencia 1**, autorizando los cuatro planes únicamente al tenant LAB existente. No suma KVM 3 y 4 simultáneas, no concede recursos ilimitados ni modifica tamaños comerciales. Recalcular el uso inmediatamente antes de aplicar y restaurar el presupuesto previo al terminar; gestionar el archivo revisado mediante el procedimiento vigente.

Observación puntual del nodo/datastore: HTTP 200, 48 CPU, aproximadamente 173 GiB de RAM libre y 10,57 TiB libres en DS_CYBERFUEL. Es una instantánea; no reserva capacidad. No se consultó salud global Ceph ni se propone intervenirlo. La API y el worker deben repetir sus barreras antes de cada operación.

### Habilitación por imagen

El bloqueo no es solo documental: `resolve_image()` admite exclusivamente Ubuntu 24.04. La admisión exige el modo `ubuntu-2404-lab-identity-v1`; el perfil autorizado está vinculado a Ubuntu 24.04 y VMID 29000. El programa de identidad ejecuta `/usr/sbin/netplan` y usa `/etc/netplan`. **No basta con cambiar `status` para certificar AlmaLinux.**

Trabajo mínimo en candidata: resolución coherente de plantilla por `image_id`, configuración específica de inicialización por imagen y validación del bootstrap existente, manteniendo contrato CRM y transporte G6. Verificar el sistema de red y filesystem real en clones; no asumir Netplan, ext4, XFS o LVM por el nombre de la distribución. No instalar herramientas ni cambiar las Golden Images originales para conseguir un PASS.

Antes de publicar una imagen como disponible, completar su validación técnica mediante el procedimiento autorizado de clones de certificación, con aislamiento de datos/colas respecto del piloto. La certificación técnica del clon y el E2E comercial por API son dos evidencias diferentes. No eliminar el bloqueo `IMAGE_CERTIFICATION_PENDING` del runtime estable para probar a ciegas.

### Cliente de certificación

`scripts/e2e-commercial-sequential.py` actual admite solo kvm-1/2, fija `ubuntu-2404` y mide ext4 con `dumpe2fs`. Conservar sus controles de confidencialidad, host key, readiness y limpieza. Parametrizar plan/imagen y utilizar mediciones correspondientes al filesystem efectivamente observado; cubrir disco, partición, LVM si existe y filesystem, sin comparar espacio utilizable contra tamaño nominal. Agregar casos positivos/negativos offline antes de cualquier clon.

No se ha modificado todavía el cliente ni la aplicación en esta rama de preparación.

## Secuencia de ejecución propuesta

1. **Preparación offline:** pruebas del catálogo/resolución, bootstrap por imagen y cliente de geometría; snapshots OpenAPI y regresión de confidencialidad, tenant, cuota, idempotencia y recursos protegidos. PR mínimo antes de una promoción que cambie código.
2. **Presupuesto LAB revisado:** aplicar solo después del preflight y de comprobar consumo/reservas actuales, con concurrencia 1. No cambiar RBAC: Administrator LAB continúa autorizado.
3. **Certificación técnica de imágenes pendientes:** clones descartables de 29001, 29010 y 29011 mediante procedimiento controlado; verificar identidad real del SO, Cloud-Init, DHCP, Agent, bootstrap PAM/SSH y expansión. Si un clon falla, recuperar/eliminar y conservar evidencia; la imagen permanece no disponible.
4. **E2E comercial secuencial:** casos descritos en [casos-e2e-secuenciales.json](casos-e2e-secuenciales.json). Primero Ubuntu 24.04 KVM 3 y KVM 4 como control del tamaño; después Ubuntu 26.04, AlmaLinux 9 y AlmaLinux 10, cada una con KVM 3 y KVM 4 cuando su validación técnica permita habilitarla. Son **ocho casos**, nunca ocho VMs simultáneas.
5. **Cierre de cada caso:** HTTP 202 y replay idéntico; terminal normalizado; VMID autorizado; CPU/RAM/disco exactos; filesystem; DHCP; SSH host verificado; root PAM; llave opcional; start/shutdown/reboot tras guest listo; DELETE terminal; ausencia de VM/discos, state sin recurso administrado, `.tflock` ausente, cuota liberada y operación conciliada. Conservar versiones de state y auditoría; no publicar state ni secretos.
6. **Promoción y certificación del runtime:** únicamente con cambios fusionados, backup restaurable, manifiesto revisado, colas sin actividad y preflight aprobado. Usar `iacpctl`/`go_live.py`; comprobar SHA real y repetir por HTTPS. No promover esta rama documental.

Si aparece `uncertain`, secreto perdido, un fallo de bootstrap o almacenamiento crítico, detener los nuevos casos, conservar la misma intención y recuperar por observación/procedimiento autorizado. No inyectar fallos físicos en workers compartidos ni forzar desbloqueos. Un fallo de una imagen no revoca automáticamente el GO existente de Ubuntu 24.04 KVM 1/2.

## WAN VLAN 204: diagnóstico inicial y condiciones de prueba

Consulta de lectura Proxmox `/nodes/mng01/network`: HTTP 200. `vmbr0` está activo, VLAN-aware y anuncia `bridge_vids=2-4094`, que incluye 204; su uplink es `bond1`. **Esto no demuestra tránsito VLAN 204 hasta el switch, gateway, reserva IPv4 ni conectividad WAN.** No se deduce un fallo porque no exista una subinterfaz host llamada vlan204: un bridge VLAN-aware puede transportar el tag sin ella.

El contrato `wan-static` existe, pero API/preflight lo rechazan con `WAN_STATIC_EXECUTION_NOT_CERTIFIED`. No se verificó una reserva IPv4 utilizable en este levantamiento. Se necesitan dirección/prefijo/gateway reservados y el camino de red autorizado; no habilitar selección libre de VLAN o datastore.

Infraestructura puede completar la lectura del camino de red en MNG, sin realizar cambios:

```bash
ip -d link show dev vmbr0
ip -d link show dev bond1
bridge vlan show
bridge link show
```

La comprobación posterior debe usar una única VM desechable y la reserva autorizada: configuración efectiva tag=204, dirección/prefijo, ruta, vecindad del gateway y HTTPS hacia un destino autorizado. Diferenciar fallo ICMP de fallo real de ruta/TCP. No asignar IP, cambiar trunk, bridge, VLAN, firewall o gateway sin autorización específica. No usar las Golden Images ni VM 104 como máquinas de prueba WAN.

## Estados y entrega

[MATRIZ_CERTIFICACION.md](MATRIZ_CERTIFICACION.md) separa validado, pendiente y fallido. Una ausencia de ejecución se registra **PENDIENTE**, nunca FALLIDO ni VALIDADO por inferencia. [INTEGRACION_DESARROLLO.md](INTEGRACION_DESARROLLO.md) extrae el contrato vigente; [DEMO_20261027.md](DEMO_20261027.md) prepara una demostración que distingue evidencia previa y ejecución en vivo.

**Esta entrega es la inspección y el plan previos a operaciones de infraestructura solicitados por el responsable.** No se han creado VMs, modificado cuotas, habilitado imágenes, cambiado red ni desplegado código. El GO previo para el piloto supervisado KVM 1/2 se conserva; la certificación ampliada está PENDIENTE y la resiliencia integral permanece NO-GO.
