# Certificación de las APIs dev e iac: seguridad, resiliencia, eficiencia y recuperación

Fecha: 2026-11-20 · Rama de trabajo: `devops/fix` → `devops/features`
Alcance: IACP Control Plane (VM 104), stacks `iacp-controlplane-dev-v1` y
`iacp-controlplane-prod-v1`. Producción no se modificó en esta etapa.

## 1. Rama archivada `mejoras/revision-20261115`

La rama ya no existe en el remoto; se conserva en el tag
`archive/branches/mejoras-revision-20261115` (`45687c9`). Parte de `1a2e965`
(10-nov, runtime previo a la migración del pool y de los subsistemas
actuales) y diverge en 97 commits de `main`. No se fusiona tal cual porque
eliminaría subsistemas actuales. Cada mejora se revisó contra `main`
(`54fce35`) y se reimplementó sobre el código vigente cuando faltaba.

| Mejora del 15-nov | Estado en `main` | Acción en esta certificación |
| --- | --- | --- |
| Readiness sin DDL | Faltaba: `/health/ready` ejecutaba `initialize()` | Integrada (`schema_ready`, 503 si no está lista) |
| Terraform sin bloqueo por pipe | Ya integrada (archivos temporales) | Sin cambios |
| Fencing de operaciones | Faltaba | Integrada (compare-and-set + `operation.fenced`) |
| Cierre atómico de operaciones | Parcial (`BEGIN IMMEDIATE` en claims y recuperación) | Cubierto por el fencing; sin cambios adicionales |
| Liberación de VMID solo por su dueño | Integrada por el equipo en `devops/fix` (`29466f2`) | Sin cambios |
| Backoff del worker ante PostgreSQL | Ya integrada (`worker/resilience.py`) | Se agrega apagado ordenado |
| Grupo de procesos de Terraform | Faltaba (`kill()` solo al principal) | Integrada (SIGTERM, gracia, SIGKILL) |
| Entorno de Terraform sin credenciales ajenas | Faltaba (`os.environ.copy()`) | Integrada |
| N+1 en listados | Faltaba | Integrada (prefetch con `DISTINCT ON` y `ANY`) |
| `idle_in_transaction_session_timeout` y keepalives | Faltaba | Integrada en el pool |
| Ping tras inactividad | Ya integrada (`check_connection` del pool) | Sin cambios |
| Healthcheck de workers, `stop_grace_period`, logs | Faltaba | Integrada en el Compose canónico |
| Bearer en tiempo constante | Ya integrada | Sin cambios |
| Heartbeats acotados | Faltaba (1 escritura/s por worker) | Integrada (cada 5 s, tolerante) |
| Drift IACP–Proxmox | Faltaba | **Pendiente** (ver §5) |
| CI Forgejo | Faltaba | **Pendiente** (ver §5) |
| Starlette con CVE conocidas | Faltaba | Integrada: FastAPI 0.121.3, Starlette 0.50.0 |

## 2. Brechas cerradas

| Severidad | Brecha | Corrección |
| --- | --- | --- |
| Crítica | Sin respaldos de los stacks v1: los únicos volcados eran manuales de octubre, del runtime anterior. Los states de Terraform solo existían en `workdata`. | `iacpctl backup` (pg_dump consistente más `workdata`, permisos 600, SHA-256 y manifiesto) e `iacpctl backup-verify` (restauración real en PostgreSQL efímero sin red) |
| Crítica | `/health/ready` ejecutaba unas 60 sentencias DDL bajo el advisory lock global cada 10–15 s, compitiendo con los claims y la asignación de VMID | Readiness de solo lectura |
| Alta | Un worker con heartbeat obsoleto podía sobrescribir la recuperación | Fencing |
| Alta | El timeout de Terraform dejaba providers huérfanos | Grupo de procesos propio |
| Alta | Terraform heredaba `PGPASSWORD`, el token del API, NetBox y Wasabi | Entorno filtrado |
| Alta | Starlette 0.46.2: DoS sin autenticación por la cabecera Range y por multipart | Starlette 0.50.0 |
| Alta | Listados N+1: unas 600 consultas por página de 200 | 4–5 consultas por página |
| Media | Un corte de PostgreSQL devolvía 500 | 503 con `Retry-After` y `DATABASE_UNAVAILABLE` |
| Media | Una transacción olvidada podía retener el lock global | `idle_in_transaction_session_timeout` de 60 s |
| Media | La API corría en un solo proceso | `IACP_API_WORKERS` (3) y pool de 10 por proceso |
| Media | Workers sin healthcheck ni apagado ordenado; logs sin rotar | Healthcheck por heartbeat, SIGTERM ordenado y json-file 20m×5. `verify` rechaza workers `unhealthy` |

## 3. Evidencia

### Pruebas automáticas

- 464 PASS con PostgreSQL 17.6 local (antes 443). Pruebas nuevas en
  `tests/test_certificacion_resiliencia.py` y `tests/test_go_live_backup.py`.

### Carga local, misma base para ambas imágenes

La base tenía 2001 instancias, 2001 operaciones y 6001 eventos.

| Endpoint | `main` 54fce35 | Certificación, 1 proceso | Certificación, 3 procesos |
| --- | --- | --- | --- |
| `/health/ready` (50 concurrentes) | 95 rps, p95 552 ms | 659 rps, p95 89 ms | 1512 rps, p95 43 ms |
| `/v1/instances?limit=200` (20 concurrentes) | 5,6 rps, p95 3888 ms | 27,5 rps, p95 888 ms | 69,8 rps, p95 444 ms |
| `/v1/operations?limit=200` (20 concurrentes) | 8,2 rps, p95 2589 ms | 34,6 rps, p95 734 ms | 89,4 rps, p95 414 ms |
| `/v1/instances?limit=50` (50 concurrentes) | 20,3 rps, p95 3671 ms | 79,4 rps, p95 695 ms | 230 rps, p95 286 ms |

Todas las corridas terminaron con 0 errores.

### Concurrencia y resiliencia (Compose canónico local)

| Prueba | Resultado |
| --- | --- |
| 100 altas concurrentes | 100×202, 133 rps, p95 230 ms, 100 operaciones únicas |
| Cola llena (`IACP_MAX_QUEUE_DEPTH=100`) | 429 de contrapresión |
| 20 solicitudes con la misma Idempotency-Key | 20×202, 1 operación |
| 20 solicitudes con el mismo nombre | 1×202 y 19×409; 1 instancia |
| Reinicio de PostgreSQL bajo carga | 628/628 respuestas 200; 0 reinicios de contenedores |
| SIGTERM a un worker idle | Sale con código 0 sin reclamar trabajo |
| Healthcheck de workers | 6/6 `healthy` y cada worker identificado en `pg_stat_activity` |

### Dev (`dev-iac.cyberfuel.com`)

| Paso | Resultado |
| --- | --- |
| Respaldo previo `v1-dev-antes-certif-20261120` | PASS |
| `backup-verify` sobre datos reales | PASS: 538 instancias, 2473 operaciones y 536 states restaurados, idénticos a lo respaldado |
| `preflight`, `deploy --preloaded-images` y `verify` de `a93fd5c` | PASS: 12 contenedores `healthy` |
| `preflight`, `deploy` y `verify` de `3c641bc` | PASS: 12 contenedores `healthy`; el borde responde `X-IACP-Build-Revision: 3c641bc…` |
| Carga interna con `3c641bc` | `/health/ready` 1287 rps (p95 49 ms); `/v1/instances?limit=200` 665 rps (p95 45 ms); `/v1/operations?limit=200` 107,5 rps (p95 367 ms; antes 44 rps y 815 ms); 0 errores |
| Carga externa a través de Traefik | 429 por diseño: el middleware `request-rate` limita a 10 rps promedio con ráfaga de 20 por IP, más `inflight` de 40 |

## 4. Procedimiento de recuperación ante desastres

Respaldo, con el stack en reposo de preferencia:

```
cd /opt/iacp/releases/controlplane-<SHA>
python3 scripts/iacpctl backup --manifest /etc/iacp/stacks/manifest-<dev|prod>.json \
  --evidence-dir /opt/iacp/evidence/<id>/backup --backup-dir /opt/iacp/backups/v1-<stack>-<fecha>
python3 scripts/iacpctl backup-verify --manifest /etc/iacp/stacks/manifest-<stack>.json \
  --evidence-dir /opt/iacp/evidence/<id>/verify --backup-dir /opt/iacp/backups/v1-<stack>-<fecha>
```

Restauración (manual y destructiva; requiere autorización y no se ensayó
sobre un stack real):

1. Detener `worker-*` y `service-worker-*` con `docker compose stop`. Nunca
   usar `down -v`.
2. Verificar el respaldo con `backup-verify`.
3. Restaurar PostgreSQL con
   `pg_restore --clean --if-exists --no-owner -d $POSTGRES_DB`, que lee
   `postgres.dump` dentro del contenedor `postgres`.
4. Restaurar `workdata`: extraer `workdata.tar.gz` en el volumen con un
   contenedor temporal, conservando permisos 600/700.
5. Ejecutar `iacpctl deploy --preloaded-images` y `verify`.
6. Reconciliar las operaciones `uncertain` antes de reabrir la cola.

Los respaldos contienen la contraseña root dentro de los states (marcada
`sensitive`, sin cifrar). Deben custodiarse como secretos.

## 5. Pendientes y plan

| Prioridad | Pendiente | Plan |
| --- | --- | --- |
| P0 | Programar los respaldos | Timer systemd en la VM 104 (dev y prod, diario, más uno antes de cada `deploy`) y copia cifrada fuera de la VM (Wasabi con SSE o equivalente). Requiere autorización. |
| P0 | Matriz física sobre la versión candidata | Matriz KVM 48 y de aplicaciones 16 en dev. Bloqueada hasta reconciliar las 2 operaciones `uncertain` ajenas (`qa-261120-*`). |
| P1 | Ensayo de fallo físico en dev | Reiniciar PostgreSQL y luego un worker durante un apply. Resultado esperado: reconciliación sin duplicados y `operation.fenced` si el worker revive. |
| P1 | Rate limit del borde | Decidir un límite para integradores autenticados (por ejemplo, otro `sourceCriterion` o allowlist del CRM) antes de pruebas de estrés externas. |
| P1 | Promoción a producción | Mismo SHA tras PASS en dev; respaldo y `backup-verify` de prod antes de `deploy`. |
| P2 | Drift IACP–Proxmox | Portar `GET /v1/observability/drift` (solo lectura) de la rama archivada. |
| P2 | CI | Portar `.forgejo/workflows/ci.yml` (lint, pruebas con PostgreSQL, `pip-audit`, `terraform validate`). |
| P2 | Actualización de paquetes al aprovisionar | Diseño pendiente compatible con qemu-guest-agent (decisión previa del equipo). |
| P2 | Contraseña root en el state | Preferir llaves; evaluar backend remoto cifrado. |
