# Auditoría de migración del runtime a API v1

- **Fecha:** 2026-11-01
- **Rama:** `develop`
- **Commit del runtime candidato:** `c8788faf6c1c39b569276d5b10caa9cd2f06d6c1`
- **Última actualización de evidencia:** 2026-11-01 22:18 CST
- **Dominio previsto:** `iac.cyberfuel.com`
- **API pública:** `/v1`

## Dictamen de integración local

**PASS — validación del código candidato, sin ensayos físicos.**

- Suite API/worker con Python 3.12.16 y PostgreSQL 16 aislado: `53 passed`,
  `1 warning`; la advertencia restante proviene de una dependencia de
  `starlette`/`anyio`.
- `docker compose config --quiet` pasó para `compose.yaml` y
  `deploy/portainer/docker-compose.yml` usando los ejemplos de configuración.
- Construcción de `iacp-controlplane` pasó. Terraform 1.16.6 inicializó el
  provider `bpg/proxmox` 0.116.1 desde el lockfile.
- Prueba aislada de la imagen: readiness PostgreSQL HTTP 200, catálogo
  protegido por Bearer y OpenAPI con versión `v1` y el ejemplo JSON que incluye
  `tags` adicionales.
- Los tags del consumidor se validan, combinan con
  `commercial_context.customer_tag`, se deduplican y se propagan al recurso
  KVM. La concurrencia física queda limitada a dos workers.

## Dictamen inicial de producción — evidencia del primer intento

**BLOCKED — no se modificó el runtime.**

En el primer intento, el acceso SSH al IACP Control Plane (VM 104) no se pudo
autenticar: las conexiones recibieron `Permission denied (publickey)` y el alias
`iacp-controlplane-01` no resolvió. Ese dictamen corresponde a esa evidencia
inicial; después se recibió el alias funcional `iacp-controlplane`.

## Inventario de producción — 2026-11-01, hora de Costa Rica

**PASS — inspección de solo lectura del runtime vigente.**

- SSH confirmó el host `iacp-controlplane-01`. El runtime público sigue en
  `a4c1107949249fa5ec48e0142b2e260021e4e095`; API, workers, Traefik,
  PostgreSQL y Redis están ejecutándose. El doctor de
  `scripts/iacpctl` devolvió `passed: true`, sin diferencias de configuración y
  con API, DB y Redis saludables. Evidencia privada en
  `/opt/iacp/evidence/doctor-a4c1107-migration-20261102T041800Z`.
- El esquema PostgreSQL es `0015_technical_access`. El snapshot contó 133
  instancias, 1.220 operaciones y 1.355 jobs. No hay operaciones, applies,
  ejecuciones de Lifecycle/eliminación ni jobs no terminales; Redis no tiene
  mensajes pendientes ni unacked. Los tres workers reportaron cero tareas
  activas, reservadas o programadas.
- El estado histórico usa 133 referencias Terraform. Una comprobación de solo
  lectura confirmó 132 objetos `.tflock` ausentes y ninguno presente. Para una
  referencia, Wasabi devolvió `403 AccessDenied` tanto en `HEAD` como en `LIST`;
  su lock queda **sin confirmar**. No se leyó ni modificó contenido de state y
  no se forzó ningún lock.
- Hay otro proyecto Docker activo,
  `iacp-controlplane-technical`, con PostgreSQL propio, API y seis workers. Su
  DB registra cinco operaciones de aprovisionamiento `uncertain`; las cinco
  instancias figuran `deleted`, sus leases están `released` y los seis workers
  están `idle`. No se consultaron sus campos de error ni se intentó
  reconciliarlas. También hay proyectos de prueba aislados; ninguno reportó
  contenedores `unhealthy`.
- `https://iac.cyberfuel.com/health/ready` respondió HTTP 200. Traefik carga
  rutas desde archivos y tiene deshabilitado el proveedor Docker. La ruta
  dinámica activa sigue usando `http://api:8000` y conserva sus middlewares.
  Las etiquetas del manifiesto Portainer candidato no cambiarían esa ruta.
- El runtime vigente usa PostgreSQL 17 y conserva el volumen
  `iacp-controlplane_postgres_data`. El Compose candidato declara PostgreSQL 16
  y almacenamiento nuevo; no se montó ni alteró el volumen vigente.

El primer `go_live.py inventory` se rechazó porque el manifiesto indicado
pertenecía a otro checkout. No examinó DB/colas ni cambió servicios. El segundo
intento, desde el checkout y manifiesto exactos de `a4c1107`, terminó `passed:
true`.

## Dictamen vigente de producción

**BLOCKED — no se promovió el runtime candidato ni se modificó el tráfico.**

El bloqueo de locks persiste: un objeto de lock no se puede confirmar con las
credenciales de lectura actuales por `403 AccessDenied`. Además, el proyecto
técnico activo conserva cinco operaciones de aprovisionamiento `uncertain` que
requieren resolución controlada antes de retirar ese runtime. No se deben
reintentar ni cerrar mediante cambios directos de DB.

El candidato aún no incluye un flujo `scripts/iacpctl`/`scripts/go_live.py`
adaptado para su API, workers y PostgreSQL. El manifiesto de Portainer también
necesita una identidad de proyecto que permita coexistir temporalmente con el
stack actual, y una ruta de Traefik revisada para el servicio nuevo. No se
ejecutó ningún despliegue, cambio de ruta ni mutación de API; tampoco se accedió
a la API de Proxmox.

La certificación física e integral sigue pendiente. La VM 104, la VM 106, la
VM 29000, el clúster MNG y el clúster Platinum/PRX no recibieron cambios.

## Archivo y ramas

- `origin/develop` apuntaba a `66e16c16637712736bde66e9af0be81e3de07204`
  durante este inventario.
- `origin/main` permanece en `a4c1107949249fa5ec48e0142b2e260021e4e095` como
  referencia del runtime legacy activo.
- El runtime legacy quedó sellado en
  `archive/controlplane-legacy-main-20261101`.
- Las puntas exclusivas de ramas antiguas se preservaron en 21 tags bajo
  `archive/legacy-branches/`; las ramas locales antiguas y las referencias
  remotas obsoletas se limpiaron.
- El repositorio de origen `controlplanev2` permanece sin cambios. No se
  eliminó durante esta fase de integración.
