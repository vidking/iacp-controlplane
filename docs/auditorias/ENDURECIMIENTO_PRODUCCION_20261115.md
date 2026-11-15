# Endurecimiento para producción del IACP Control Plane

Fecha: 2026-11-15 · Rama: `mejoras/revision-20261115` (base `develop` = `main` en `1a2e965`)
Alcance: código, Compose, CI y pruebas. Sin despliegues, sin merge y sin acceso a Proxmox, NetBox, Wasabi ni producción.

## Dictamen

**No Production Ready.** Las mejoras están integradas en la rama y probadas en un PostgreSQL 17.6 local, pero quedan validaciones críticas pendientes:

1. Ejecutar la suite en Linux: el CI está definido pero nunca se ejecutó.
2. Validar en el clúster MNG la matriz de seis aprovisionamientos simultáneos.
3. Actualizar Starlette/FastAPI por 7 vulnerabilidades conocidas.
4. Revisar el riesgo residual de la contraseña root en el state de Terraform.

## Resultados comprobados

| Prueba | Resultado |
| --- | --- |
| Suite en Windows 11 + PostgreSQL 17.6 local | 182 PASS, 10 FAIL |
| Los 10 FAIL | Todos en `tests/test_go_live.py`. Dependen de permisos POSIX (`chmod 600`, `os.geteuid`) y fallan igual en `1a2e965`. |
| Tests nuevos en la rama | 51 (141 → 192) |
| Concurrencia (10/50/100 solicitudes) | PASS: misma Idempotency-Key, nombres duplicados, asignación de VMID |
| `terraform fmt -check` / `validate` (mng01, prx, fixtures/local) | PASS con Terraform 1.16.6 y bpg/proxmox 0.116.1 |
| `ruff` (E9, F63, F7, F82, F401, F841) | PASS |
| Bandit `-ll -s B608` | PASS |
| `pip-audit` | 7 vulnerabilidades en starlette 0.46.2 (pendiente) |
| Equivalencia de Compose (servicios, entorno, volúmenes, redes, imágenes) | PASS (comparación YAML antes y después) |

Medición de listados (PostgreSQL local, 5000 instancias, 5000 operaciones, 20000 eventos, `limit=200`, mejor de 5):

| Endpoint | Antes | Después |
| --- | --- | --- |
| `GET /v1/instances` | 601 consultas, 186 ms | 5 consultas, 15 ms |
| `GET /v1/operations` | 201 consultas, 83 ms | 3 consultas, 15 ms |

`EXPLAIN ANALYZE` confirma el uso de `idx_instances_created_at`, `idx_operations_created_at`, `idx_events_operation_id`, `idx_operations_instance_status` e `idx_instances_active_name`.

## Problemas, causa raíz y solución

| Severidad | Problema | Causa raíz | Solución |
| --- | --- | --- | --- |
| Critical | La readiness bloqueaba tablas cada 10 s | `/health/ready` ejecutaba `initialize()` (unas 50 sentencias DDL) | Readiness de solo lectura (`schema_ready`) |
| Critical | Terraform podía colgarse hasta el timeout | `stdout`/`stderr` en PIPE leídos solo al terminar | Archivos temporales sin nombre, lectura acotada |
| Critical | Un worker obsoleto podía sobrescribir una recuperación | Sin propiedad verificada al cerrar la operación | Fencing compare-and-set (`worker_id` + estado activo) y `OperationFencedError` |
| High | Cierre de operación no atómico | Cada escritura usaba su propia conexión | `transaction()` en los cierres; archivos borrados después del COMMIT |
| High | VMID liberado por una operación ajena | `release(vmid)` sin dueño | Liberación filtrada por instancia; upsert del lease con `WHERE status <> 'held'` |
| High | El worker moría si caía PostgreSQL | Excepción no controlada en el bucle | Backoff exponencial (máx. 30 s); heartbeat fallido no abandona Terraform |
| High | Timeout dejaba providers huérfanos | `kill()` solo al proceso principal | Grupo de procesos propio; SIGTERM, gracia y SIGKILL al grupo |
| High | Terraform heredaba credenciales ajenas | `os.environ.copy()` | Entorno sin `PG*`, `IACP_API_TOKEN`, NetBox, Wasabi ni `IACP_PROXMOX_*_API_TOKEN` |
| High | N+1 en listados | Consultas por fila y conexión por fila | Prefetch con `DISTINCT ON` / `ANY`; orden determinista |
| High | Contraseña root en `plan.tfplan` | El plan nunca se borraba | Se borra tras apply exitoso y en provision fallida o incierta |
| Medium | `POST /v1/operations` no atómico | Autocommit sin sección crítica | `BEGIN IMMEDIATE` con rollback |
| Medium | Sesiones "idle in transaction" sin límite | Sin timeout del lado servidor | `idle_in_transaction_session_timeout` de 60 s y keepalives TCP |
| Medium | Conexión muerta tras reiniciar PostgreSQL | La reutilización no verificaba la conexión | Ping tras 30 s inactiva y reconexión |
| Medium | Sin detección de drift | No existía el reporte | `GET /v1/observability/drift`, solo lectura |
| Medium | Workers sin healthcheck, logs sin rotar | Compose incompleto | Healthcheck por heartbeat, `stop_grace_period`, rotación json-file |
| Medium | Sin CI | No existía | `.forgejo/workflows/ci.yml` |
| Low | Token comparado con `!=` | Comparación no constante | `hmac.compare_digest` |
| Low | Imports y variable sin uso | Código muerto | Eliminados |

## Decisiones arquitectónicas

- **Conexiones:** se mantiene la reutilización por hilo en lugar de `psycopg_pool`. La API usa el threadpool acotado de Starlette y cada worker es de un solo hilo, así que el número de conexiones ya está limitado: como máximo unos 40 hilos de la API más 6 workers, contra los 100 que permite PostgreSQL por defecto. `psycopg_pool` sería una dependencia nueva sin beneficio medible. `IACP_DB_REUSE_CONNECTIONS=0` restaura el comportamiento original.
- **Transacciones y efectos externos:** `transaction()` agrupa solo escrituras en PostgreSQL. Ninguna transacción queda abierta durante Terraform, Proxmox o NetBox. La consistencia entre la base, Terraform y Proxmox se resuelve con reconciliación física, no con una transacción distribuida.
- **Fencing:** se usan `worker_id` y el estado activo como token de propiedad. No hizo falta agregar una columna de generación, así que no cambia el esquema.
- **Esquema:** solo índices `CREATE INDEX IF NOT EXISTS`. `schema_version` sigue en 2 porque `go_live.py` lo exige.
- **Redis:** el runtime actual no usa Redis; la cola es la tabla `operations` de PostgreSQL. La operación aceptada y su encolado son la misma fila en la misma transacción, así que no se requiere outbox. Las menciones a Redis en `docs/` corresponden al runtime anterior.
- **Drift:** solo se reporta. No hay acciones destructivas automáticas y los VMID presentes en Proxmox nunca se reasignan.

## Secretos: auditoría y riesgo residual

| Ubicación | Estado |
| --- | --- |
| `operation-secrets/*.root_password` | Archivo 600 de un uso; se borra tras provision o destroy |
| `plan.tfplan` | Contiene la contraseña en claro; ahora se borra tras apply exitoso y en provision fallida o incierta. El plan de un `plan` explícito se conserva hasta su apply. |
| `terraform.tfstate` / `.backup` | **Riesgo residual:** contiene la contraseña root, marcada como sensitive pero sin cifrar. |
| PostgreSQL, respuestas de la API, eventos | Sin la contraseña (verificado por `test_secret_exposure.py`) |
| Salida de Terraform | Redacción de la contraseña root y del token de Proxmox |
| Entorno de Terraform | Sin credenciales de PostgreSQL, IACP, NetBox ni Wasabi |
| CI | Solo valores ficticios; no publica imágenes |

Sobre el soporte real (verificado con `terraform providers schema -json`):

- El provider bpg/proxmox 0.116.1 **no** ofrece una variante write-only de `initialization.user_account.password`. Solo tiene write-only en recursos ACME, metrics server y realm OpenID.
- Terraform 1.16 soporta variables `ephemeral`, pero solo pueden usarse en argumentos write-only, así que no aplican a `password`.

La contraseña debe persistir en el state mientras se use `user_account.password`. Controles aplicados: `umask 077` en el worker (workspaces y states 600/700), volumen `workdata` exclusivo, contenedores `read_only`, `cap_drop: ALL` y `no-new-privileges`. Hay dos recomendaciones:

1. Preferir llaves SSH y contraseñas de un solo uso rotadas por cloud-init.
2. Evaluar el estado remoto cifrado (Wasabi con SSE) cuando se habilite.

Esas dos son recomendaciones; no se implementaron.

## Archivos modificados

- `api/db.py`, `api/main.py`, `api/service.py`, `api/drift.py` (nuevo)
- `worker/main.py`, `worker/allocation.py`, `worker/heartbeat.py`, `worker/netbox.py`, `worker/healthcheck.py` (nuevo)
- `compose.yaml`, `deploy/portainer/docker-compose.yml`
- `.forgejo/workflows/ci.yml` (nuevo), `.forgejo/pip-audit-ignore.txt` (nuevo)
- Tests: `tests/conftest.py`, `tests/test_api.py`, `tests/test_worker.py` y los nuevos `test_db.py`, `test_reliability.py`, `test_terraform_exec.py`, `test_concurrency.py`, `test_drift.py`, `test_listing_performance.py` y `test_secret_exposure.py`

## Variables de entorno nuevas (todas opcionales, con valores por defecto seguros)

`IACP_DB_REUSE_CONNECTIONS` (1), `IACP_DB_CONNECTION_MAX_AGE_SECONDS` (300), `IACP_DB_PING_IDLE_SECONDS` (30), `IACP_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` (60000), `IACP_DB_STATEMENT_TIMEOUT_MS` (0, desactivado), `IACP_DB_KEEPALIVES_*`, `IACP_DB_APPLICATION_NAME`, `IACP_COMMAND_HEARTBEAT_SECONDS` (5), `IACP_HEARTBEAT_MIN_INTERVAL_SECONDS` (5), `IACP_TERRAFORM_KILL_GRACE_SECONDS` (20), `IACP_WORKER_HEALTH_MAX_AGE_SECONDS` (120), `IACP_WORKER_STOP_GRACE_PERIOD` (120s), `IACP_LOG_MAX_SIZE` (20m), `IACP_LOG_MAX_FILE` (5).

## Compatibilidad

- No hay cambios en rutas, campos ni códigos de estado existentes.
- Lo nuevo es aditivo: `GET /v1/observability/drift` y los campos `queue_oldest_pending_seconds`, `retried_operations`, `held_vmid_leases` y `database` en `/v1/observability`.
- En los listados solo cambia el desempate del orden cuando dos filas tienen el mismo `created_at`.
- Compose conserva servicios, imágenes, entorno, volúmenes y redes.

## Rollback

1. Volver a desplegar la imagen anterior con `scripts/iacpctl` y el manifiesto vigente; las variables nuevas se ignoran.
2. Los índices nuevos son inocuos. Si se quiere retirarlos: `DROP INDEX IF EXISTS idx_operations_created_at, idx_instances_created_at, idx_instances_active_name, idx_operations_reconcile, idx_operations_worker_created;`
3. No se modificaron datos, states ni volúmenes, así que no hace falta revertir nada más.

## Riesgos residuales y pendientes

1. **High:** starlette 0.46.2 con 7 vulnerabilidades, incluida un DoS sin autenticación por la cabecera Range (GHSA-7f5h-v6xp-fcq8). Requiere actualizar FastAPI con una regresión completa.
2. **High:** contraseña root en el state de Terraform (ver la sección de secretos).
3. **Medium:** el CI no se ha ejecutado en un runner Forgejo. Necesita la etiqueta `docker` y un daemon Docker para el job `container`.
4. **Medium:** con `stop_grace_period` de 120 s, un apply más largo termina con SIGKILL y queda en reconciliación, que es el comportamiento seguro pero manual. Ajustar según la duración real que se mida en el LAB.
5. **Low:** no se definieron límites de CPU y memoria en Compose, porque faltan mediciones reales en la VM 104.
6. Workspaces y states se acumulan en `workdata` (por `AGENTS.md` no se borran). Conviene monitorear el disco.

## Propuesta de validación en el clúster LAB (MNG)

Siguiendo `AGENTS.md`, con credenciales y cola de prueba aisladas:

1. Desplegar la imagen de la rama en el stack `develop` del IACP Control Plane (VM 104) con `scripts/iacpctl` y verificar `preflight`, `verify` y los seis workers `healthy` e idle.
2. Matriz física de seis aprovisionamientos simultáneos en el clúster MNG (`mng01`): PASS si los seis terminan `ready` con VMID únicos.
3. Fault injection controlada: reiniciar el contenedor `postgres` durante un apply y luego un worker durante un apply. Esperado: reconciliación sin duplicados y `operation.fenced` si el worker revive.
4. Destroy de las seis VMs; `GET /v1/observability/drift` debe quedar `consistent`.
5. Registrar PASS, FAIL o BLOCKED con versión, fecha y evidencia en `docs/auditorias/`.
