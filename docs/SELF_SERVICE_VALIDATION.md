# IACP: cierre del MVP de autoservicio de laboratorio

La API está desplegada en **0.9.1**, revisión ejecutada
`724fa107212b20dfd28bbd334af6c683005c293e`, esquema **0010_guest_identity**.
La revisión final aprobó **749 pruebas: 700 unitarias/CLI y 49 integraciones,
sin fallos ni omisiones**. Las integraciones de PostgreSQL/Redis/Celery usan
infraestructura Proxmox simulada; la tabla siguiente corresponde a KVM reales.
El [registro de evidencia](validation/SELF_SERVICE.json) conserva versiones,
operaciones, planes, UPIDs, hashes/seriales/lineage de state y límites de validación.

El autoservicio queda **abierto bajo política**, con admisión, Apply, Lifecycle,
DELETE y delegación habilitados. Las allowlists históricas están vacías. Se
conservan revisión independiente de política, permisos del tenant, cuotas,
propiedad sellada y fences antes de mutaciones; VM 104 y VM 106 no se habilitan.

## KVM reales y limpieza

Todas las creaciones, cancelaciones, reintentos, reconciliaciones, operaciones de
energía y eliminaciones se solicitaron por HTTPS a IACP. No se usó `qm` para
sustituir ningún flujo. Cada VM tuvo un único Apply consumido y una única
inicialización de invitado consumida. Las IP se verificaron nuevamente después
de reboot; el state permaneció idéntico durante energía e inicialización.

| UUID IACP | VMID / nodo | vCPU / MiB / GiB | IPv4 verificada tras reboot | Resultado final |
|---|---|---|---|---|
| `aaea8360-faf9-4093-aa8b-19b06580b0ae` | 30004 / mng01 | 2 / 2048 / 20 | 192.168.3.227 | DELETE completo, Proxmox ausente, state vacío |
| `dce49c55-b87e-4e3e-8fd8-39409f83a08b` | 30006 / mng01 | 1 / 1536 / 20 | 192.168.3.105 | DELETE completo, Proxmox ausente, state vacío |
| `e024cd8d-0bc3-4bc6-b990-47af1d38b1f3` | 30005 / mng01 | 3 / 2560 / 20 | 192.168.3.196 | DELETE completo, Proxmox ausente, state vacío |
| `7e017bd4-b1c1-4daa-98cd-ffb2f70b1408` | 30007 / mng01 | 1 / 1024 / 24 | 192.168.3.193 | DELETE completo, Proxmox ausente, state vacío |

Se creó la primera instancia secuencialmente y se enviaron las otras dos
solicitudes concurrentemente. La tercera conservó UUID/VMID durante una
cancelación controlada de planificación y su reintento de intento 1 a 2. Su Apply
incierto se reconcilió solo por observación. También se reconciliaron observaciones
de inicialización y de la cuarta creación, conservando sus fences e identidades.
Se comprobaron replays de creación, retry-provision, energía, inicialización y
DELETE con las mismas claves, filtros/listado, cuotas y aislamiento de lectura
con una identidad real de otro tenant.

El grupo de tres completó inicialización, Lifecycle y DELETE sobre la revisión
`4259483`; las creaciones segunda y tercera también corresponden a esa revisión.
La primera había sido creada en `370ce98` y se conservó al corregir el despacho
entre workers. La cuarta ronda se ejecutó sobre la revisión final **724fa10**,
después de limpiar el grupo y liberar cuota. Solicitó disco de 24 GiB y preferencia
`mng01`; recibió VMID nuevo. Su inicialización automática terminó sin solicitar
reconciliación. Estos límites evitan atribuir pruebas de una revisión a otra.

El DELETE del piloto original **30000**, UUID
`084c83e5-8d44-41a9-a527-f496cd83168b`, también terminó y se verificó idempotencia,
Proxmox ausente y state vacío, con lineage conservado y serial 2 → 3. Operación:
`6e415918-919b-47e7-ba28-5eeb5505d693`. Se registró la corrección explícita del
usuario para ejecutar el laboratorio y la identidad API separada
`iacp-lab-approver`; no se inventó un revisor humano.

El primer intento multiinstancia, VMs 30001–30003, detectó un fallo real: MACs y
SMBIOS distintos heredaban el mismo machine-id/CLIENTID y recibían 192.168.3.59.
Se limpiaron por API. Se resolvió con el inicializador fijo, versionado y
preaprobado de clones propias; **la plantilla compartida 29000 no se modificó**.
La espera final de concesión DHCP tolera su reemplazo temporal sin reenviar el
programa. Las cuatro VMs de la tabla acreditan identidades y concesiones distintas.

## Política y capacidad disponible

Tenant autorizado: `00000000-0000-0000-0000-000000000001`.
Política: `ac558dfa-4c2b-4e2f-8886-67648e880dc8`.
Digest: `6feea8186e364418c4019c40d1556125d44f458b4118d26f42faf21b8d8d1317`.
Proponente `bootstrap`, aprobador API independiente `iacp-lab-approver`.
Vence **2026-10-31 01:23:03 UTC / 2026-10-30 19:23:03 Costa Rica**. Renovarla
requiere revisión independiente del mismo digest y perfiles intactos.

| Límite | Configurado | Uso final del tenant | Disponible tras limpieza |
|---|---:|---:|---:|
| Cantidad de VMs | 4 | 1 histórica, protegida | Hasta 3 nuevas |
| vCPU | 8 | 2 | 6 |
| RAM, MiB | 8192 | 2048 | 6144 |
| Disco, GiB | 80 | 20 | 60 |
| Operaciones concurrentes | 2 | 0 | 2 |

Por instancia: hasta 4 vCPU, 4096 MiB y 40 GiB, disco mínimo 20 GiB. CPU/RAM/disco
se consumen conjuntamente: tres slots no garantizan tres VMs con máximos por VM.
La instancia histórica VM106 consume cuota pero no recibe permisos de operación.
Se comprobaron rechazos reales de cantidad, CPU, RAM, disco, nodo no admitido y
omisión de política; las reservas de intentos fallidos/inciertos no se eluden.
La concurrencia también se valida en pruebas con locks PostgreSQL reales.

Perfil activo: `/etc/iacp/terraform/profiles-guest-identity-370ce98.json`, seleccionado
por `IACP_TF_PROFILES_PATH`. El nombre de archivo no determina la revisión ejecutada:
se verifica por SHA en manifest y por labels/IDs de imágenes. El perfil usa
`ubuntu-2404`, plantilla 29000, `mng01`, `vmbr0`, DHCP y VLAN ausente. Conserva el
modo estático/IPAM existente para los perfiles correspondientes.

## Release y evidencias

API y workers se promovieron exclusivamente mediante `scripts/iacpctl`,
`deploy/mng/compose.yaml` y `scripts/go_live.py`. Se restauró un respaldo reciente
en PostgreSQL aislado antes de promover. Las migraciones y promociones compararon
la historia persistida; no hubo downgrade ni reparación de registros por SQL.
DB, Redis y Traefik se conservaron. El doctor final aprobó las siete funciones,
la configuración canónica sin diferencias y la ausencia de trabajo pendiente.

- API: `sha256:e070027a259870418d4a3f60616979dd49a34867c190f04767fd08b1ae067661`.
- IaC Worker: `sha256:b01480ad0f034bc4539797cb558b43c0a6221a8b799e54fd7775a3f7cfebee8e`.
- OpenAPI real y snapshot coinciden: SHA canónico `93953845491f493f28ff846df386320a1e9c3861df8d6661892a1f18b25921da`.
- JUnit: SHA `df7e328095df84b6a3eb6b271b48affe2c1fa0717d0e6b35da8970523f6c8795`.
- Evidencia privada final: `/opt/iacp/evidence/guest-identity-20261024T015344Z/`.
- Solicitudes y pruebas del piloto: `/opt/iacp/evidence/guest-identity-20261024T012104Z/pilot/`.

Se verificó HTTPS con TLS válido; anónimo no accede a docs/OpenAPI; Basic docs sí
accede, pero ni Basic ni su contraseña como Bearer autorizan la API. El estado de
VM106 conserva SHA de configuración
`c2b19bc1dc35eabb3cb0d22bf9a993625a5bf9e981b32e82af6f139a17fef1a1`;
el token IaC mantiene permisos efectivos vacíos sobre VM104. Las pruebas no
mutaron Proxmox de VM104/VM106, otros tenants ni la plantilla compartida.

PRs de implementación: [28](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/28),
[29](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/29),
[30](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/30),
[31](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/31),
[32](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/32),
[33](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/33) y
[34](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/34).
La revisión desplegada y probada se identifica por commit completo, no por el
estado de integración de un PR ni por un tag de imagen reutilizable.

No quedan bloqueos técnicos o de autorización verificados en este alcance de
laboratorio. `guest_ready=null` conserva su semántica: una IP no certifica una
aplicación. No se integraron sistemas comerciales reales.

Para Desarrollo: [tabla de endpoints, JSON para Swagger y archivos/variables](SELF_SERVICE.md),
[contrato CRM](CRM_INTEGRATION.md), [OpenAPI](openapi.json) e
[inventario de motores y licencias](ENGINE_INVENTORY.md).
