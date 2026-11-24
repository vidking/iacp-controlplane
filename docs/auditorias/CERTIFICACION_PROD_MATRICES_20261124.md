# Matrices físicas en producción — 2026-11-24

**Alcance:** stack `iacp-controlplane-prod-v1` del IACP Control Plane (VM 104),
API `iac.cyberfuel.com`, clúster MNG (nodo `mng01`), rango VMID 30000–39999,
perfil de red `prod_static` (`vmbr3`, prefijo NetBox `190.0.231.0/24`).
Producción PRX no participó. VM 104, VM 106 y VM 29000 no se tocaron.

**Revisión ejecutada:** `126ab4aa2de94b8780dd2e25c75bb073f2b834d8` (imagen
publicada con ese SHA, 6 workers de infraestructura y 6 de servicios).
Scripts de matriz de `main` en `2c70568`.

## Dictamen

| Matriz | Casos | Resultado | Checks | Evidencia (VM 104) |
| --- | --- | --- | --- | --- |
| KVM, ciclo 1 (6 imágenes × 4 planes × llave/contraseña) | 48 | **PASS** | 1152 PASS, 0 FAIL | `/opt/iacp/evidence-prod-kvm-c1-20261124111312.log` |
| Aplicaciones, ciclo 1 (n8n 12, Nextcloud 12; Ubuntu 24.04) | 24 | **PASS** | 924 PASS, 0 FAIL | `/opt/iacp/evidence-prod-apps-c1-20261124113357.log` |

Seis aprovisionamientos simultáneos con los seis workers sanos e idle antes de
cada inyección. Estado posterior: 0 instancias, 0 operaciones, 0 leases VMID,
6 + 6 workers idle.

**Qué no certifica este dictamen.** Los ciclos 1 y 2 de ambas matrices están
en PASS (96 KVM y 48 aplicaciones; ver «Ciclo 2» más abajo). No se generó el archivo
`/etc/iacp/certification/controlplane-v1.json` que exige `iacpctl activate`.
Es un dictamen de integración en producción, no una certificación integral.

## Incidentes de la sesión y su resolución

Cada intento anterior quedó como **BLOCKED** y se corrigió antes de repetir
toda la matriz.

| # | Síntoma | Causa raíz | Corrección |
| --- | --- | --- | --- |
| 1 | `VMID_PHYSICAL_RESOURCE_ALREADY_PRESENT` en el 100 % de los aprovisionamientos | La verificación de ausencia exigía un 404 de `/nodes/<n>/qemu/<id>/config`; con el token de MNG Proxmox responde **403** (evalúa el permiso antes que la existencia) | Se confirma con `/cluster/nextid?vmid=N` (200 libre / 400 existe), independiente de ACL; sigue fail-closed |
| 2 | Operaciones `uncertain` tras 180 s, VM creada y detenida | `ccd2f0e` exigía `VM.PowerMgmt=1` en `/vms/<id>`; la ACL se concede en `/pool/iacp-lab` (propagate=1) y esa consulta devuelve 0 | `permissions()` acepta el privilegio heredado del pool si la VM es miembro; VM ajena sigue denegada |
| 3 | Respaldo y despliegue esperaban sin fin y retenían la compuerta de escritura | `active_operations` contaba las operaciones `uncertain` (sin worker) | `iacpctl backup/deploy --allow-uncertain` |
| 4 | `SERVICE_QUEUE_NOT_IDLE` entre lotes | La matriz contaba cualquier advisory lock; la liberación de IPs en NetBox toma uno un instante | `idle()` reintenta en una ventana acotada (90 s) |
| 5 | Un caso AlmaLinux 10 `kvm-4` (093) sin red estática en el guest | **Causa raíz sin determinar.** Intermitente: tres VMs idénticas aprovisionadas aparte y 8 de AlmaLinux 10 en la matriz final pasaron | Cerrada como `failed` definitivo por reconcile y eliminada. No se reprodujo en el intento que dio PASS |

El hallazgo 5 queda **abierto** como riesgo: una VM puede quedar sin red en el
primer arranque (cloud-init/NetworkManager de la imagen AlmaLinux 10) y la
plataforma la declara fallida, pero no se reintenta automáticamente.

## Promoción y reversión

- Promoción con `/opt/iacp/promote-prod.sh <sha>` (respaldo `backup` +
  `backup-verify`, `deploy --replace-active --allow-uncertain`, verificación
  externa de `X-IACP-Build-Revision`).
- Reversión: `prod.env.before-<sha7>-<marca>` en `/etc/iacp/stacks/` conserva el
  tag previo (`5cd54b1b9310523edcc1cfa6fa73f940120d192c`); respaldos en
  `/etc/iacp/backups/prod-*`. Los volúmenes no se tocaron.
- `devops/features` (rama de Codex) **no** contiene estas correcciones; `main` sí.

## Ciclo 2 (2026-11-24, revisión `c6b1a9c`)

Tras integrar `origin/devops/features` en `main` (`1c6cb4a`, 673 pruebas) y
promover al IACP Control Plane (VM 104), se ejecutó el ciclo 2 sobre el clúster
MNG (nodo `mng01`), con seis workers sanos e idle antes de cada lote.

| Matriz | Casos | Resultado | Checks | Evidencia |
| --- | --- | --- | --- | --- |
| KVM, ciclo 2 | 48 | **PASS** | 1152 PASS, 0 FAIL | `/opt/iacp/evidence-prod-kvm-c2-20261124160430.log` |
| Aplicaciones, ciclo 2 | 24 | **PASS** | 924 PASS, 0 FAIL | `/opt/iacp/evidence-prod-apps-c2-20261124163440.log` |

Acumulado: **96 aprovisionamientos KVM y 48 de aplicaciones en PASS**. Estado
posterior: 0 instancias de prueba, 0 operaciones activas, 0 leases VMID.

### Incidentes del ciclo 2

Cada intento previo quedó BLOCKED; se corrigió o limpió y se repitió la matriz
completa.

| # | Síntoma | Causa | Resolución |
| --- | --- | --- | --- |
| 6 | `kvm-prueba-179` (AlmaLinux 10, `kvm-1`, VMID 30003): `SERVICE_BOOTSTRAP_GUEST_FAILED`; sin IP y «NetworkManager is not running», también tras reiniciar | **Sin determinar** (el guest no permite leer logs: SELinux y Guest Agent restringido). Mismo síntoma que el incidente 5; ambos fallos son AlmaLinux 10 con plan `kvm-1` (correlación, no causa demostrada) | Reconcile → `failed`, eliminación por API y repetición de la matriz. Sigue **abierto** |
| 7 | `kvm-prueba-192` (Ubuntu 24.04, `kvm-4`): `STATIC_GUEST_VALIDATION_UNCERTAIN` con la VM sana y la IP correcta | Un error transitorio de Proxmox en `agent/exec` o `exec-status` se convertía en incertidumbre | `worker/service_bootstrap.py`: reenvío acotado (4) del exec solo para comandos idempotentes (sondeo y modo estático) y lectura de estado tolerante hasta 180 s; DHCP no se reenvía. Commit `c6b1a9c` |

Riesgo abierto: dos fallos en 16+ aprovisionamientos AlmaLinux 10 `kvm-1`
exigen un reintento automático con VM nueva o reforzar el helper de la Golden
Image; ambos cambios tocan runtime y requieren pruebas y promoción controlada.

Nota de aislamiento: durante el ciclo 2 Codex ejecutó en el stack dev una
matriz de servicios sobre la misma red `190.0.231.0/24`; no se tocó el stack dev.
