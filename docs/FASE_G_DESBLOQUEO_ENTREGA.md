# Fase G: desbloqueo del flujo comercial

Fecha: 25/10/2026, America/Costa_Rica. Estado: **NO-GO FASE G**.
Actualización posterior del responsable: Ceph queda fuera del alcance y VM 106
es descartable. El criterio operativo vigente y sus pruebas están en
[dependencias KVM y permisos](validation/phase-g/diagnostico-ceph-rbac-20261025/RESULTADOS.md).
Lo siguiente conserva el diagnóstico y la candidata anteriores a esa decisión.
Rama: `fix/fase-g-barrera-ejecucion`. Base local: `14966da`.
Último main comprobado: `d8ac23e`; Forgejo no permite actualizarlo en esta
ejecución: conexión SSH al puerto 2222 rechazada. No se presume main actualizado.

## Corrección puntual preparada

La API conserva identidad, tenant, catálogo, cuotas, capacidad física,
idempotencia y auditoría. Sus lecturas de admisión se limitan al nodo y datastore;
no necesita privilegios globales para consultar Ceph.

El Terraform Worker incorpora la barrera que le faltaba: verifica los datastores
de arranque, Cloud-Init y clonación; para RBD/CephFS exige exactamente
`HEALTH_OK`. Utiliza exclusivamente su identidad Terraform y TLS verificado.
Se comprueba antes de planificar y nuevamente después de validar el plan,
antes de consumir la autorización y ejecutar `apply`. No reutiliza una lectura
antigua. Un error de permisos, timeout, respuesta inválida o salud degradada
impide la mutación. Observación y reconciliación no requieren salud buena para
seguir diagnosticando. No se incorporan secretos ni cuerpos remotos al error.

Alcance: nuevas ejecuciones DHCP, incluida la vía comercial, sin cambiar
variables ni planes sellados, estados remotos, endpoints o contrato JSON.
Una solicitud puede recibir 202 y fallar después por salud: admisión no equivale
a ejecución completada. Un fallo anterior al consumo se registra como `failed`,
no como mutación incierta. Las garantías existentes de recuperación se conservan.

## Evidencia operativa que condiciona el E2E

Lecturas reales del 25/10/2026 a las 02:23:13–02:23:34, America/Costa_Rica:

| Condición | Resultado |
|---|---|
| Identidad Terraform existente | `iacp-provision@pve!terraform-lab` |
| Clonación de 29000, 29001, 29010 y 29011 | `VM.Clone` y `VM.Audit` concedidos; no falta de permisos demostrada |
| Pool `iacp-lab` | Contiene VM 104, VM 106 y las cuatro Golden Images |
| Inventario anterior `pool_inventario: null` | No prueba ausencia: pertenencia confirmada mediante miembros del pool |
| Permisos sobre VM 104 | Sin privilegios efectivos |
| Permisos sobre VM 106 | `VM.Allocate` y `VM.PowerMgmt` efectivos: aislamiento de backend insuficiente |
| ACL Terraform | Usuario y token con Administrator sobre `/` y `/vms`: exceso vigente |
| Ceph con identidad Terraform | HTTP 200, **HEALTH_ERR**; no autoriza ejecución |
| Servicios de VM 104 | Siete contenedores operativos; API, PostgreSQL y Redis saludables |

A las 02:25:57, la identidad `iacp-admission@pve!capacity-ro` confirmó HTTP 200
en nodo y `DS_CYBERFUEL`, con TLS verificado. No se repitieron escrituras.
PostgreSQL a las 02:25:19: esquema 0011, 264 operaciones completed, dos failed,
ninguna no terminal y cero locks esperando. Imágenes anteriores conservadas.

A las 02:26:41 se ejecutó únicamente la función candidata de barrera, en memoria,
contra las lecturas reales del worker: `EXECUTION_CEPH_NOT_HEALTHY`, sin `apply`.
Comprueba contención real; no equivale a desplegar o certificar el flujo completo.

No añadir permisos de administración al pool actual: alcanzarían plantillas y
recursos protegidos. No se creó otro pool, no se aplicaron ACL y no se clonaron VMs.
La delimitación administrativa de recursos queda a cargo de Infraestructura.
El proveedor fijado ya recibe `pool_id`; antes de activar RBAC limitado a pools
hay que adaptar y probar las comprobaciones actuales que exigen inventario global
y `VM.Allocate` por destino. No tratar un inventario filtrado como prueba de ausencia.

Certificación funcional independiente: Ubuntu 24.04 mantiene evidencia histórica;
Ubuntu 26.04 y AlmaLinux 9/10 siguen registradas, pendientes y no disponibles.
`VM.Clone` concedido no certifica Cloud-Init, DHCP, acceso o crecimiento del filesystem.

## Únicas acciones administrativas pendientes

Infraestructura puede comprobar lo anterior con estos comandos de **solo lectura**
en MNG. No incluyen cambios ni requieren entregar credenciales a Codex:

```bash
pvesh get /pools/iacp-lab --output-format json
pvesh get /access/acl --output-format json
pvesh get /access/permissions --userid 'iacp-provision@pve!terraform-lab' --path /vms/106 --output-format json
pvesh get /nodes/mng01/ceph/status --output-format json
```

Se requiere resolver la salud Ceph y aprobar el ámbito RBAC; no una nueva auditoría
integral de capacidad ni nuevos permisos para `capacity-ro`.

## Secuencia restante de cierre

1. Publicar y fusionar la candidata revisada cuando Forgejo vuelva a estar accesible.
2. Comprobar configuración G6, TTL/persistencia Redis, clave y SSH técnica;
   respaldos, migración aditiva 0012 y rollback compatible. Sin esta comprobación
   no promover. La validación G6 de laboratorio no equivale a bootstrap real.
3. Tras autorización operativa y barreras satisfechas, promover con
   `iacpctl env-promote`/`go_live.py`; certificar HTTPS y SHA realmente ejecutado.
4. Ejecutar kvm-1 y kvm-2 secuencialmente, verificando recursos, filesystem,
   DHCP, acceso, Lifecycle, DELETE y limpieza. Ambos E2E siguen **BLOCKED**.

Runtime conservado: `8a58db244ec1e9cd1a0d627b21e086548489e75d`, esquema
`0011_crm_contract`; sin promoción ni cambios a secretos, MNG, Compose o imágenes.
GO histórico para piloto supervisado y NO-GO de resiliencia integral se mantienen
separados del NO-GO de Fase G. Resultados de la candidata: ver evidencia adjunta.
