# Auditoría de migración del runtime a API v1

- **Fecha:** 2026-11-01
- **Rama:** `develop`
- **Commit revisado:** `c8788faf6c1c39b569276d5b10caa9cd2f06d6c1`
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

## Dictamen de producción

**BLOCKED — no se modificó el runtime.**

El acceso SSH de solo lectura al IACP Control Plane (VM 104) no se pudo
autenticar: las conexiones intentadas recibieron `Permission denied (publickey)`
y el alias `iacp-controlplane-01` no resolvió desde este equipo.
Por ello no se verificaron la versión realmente ejecutada, imágenes, servicios,
esquema PostgreSQL, operaciones activas, colas ni locks.

El procedimiento `scripts/iacpctl`/`scripts/go_live.py` sellado en el tag
`archive/controlplane-legacy-main-20261101` valida el Compose y el esquema del
runtime anterior. Aún no está adaptado al manifiesto nuevo de API, workers y
PostgreSQL. La promoción necesita esos controles portados, una revisión del
estado real de VM 104 y el procedimiento aprobado de promoción y recuperación.

No se ejecutaron aprovisionamientos en el clúster MNG, no se consultaron ni
modificaron VMs, no se activó Portainer y no se tocaron la VM 104, la VM 106 ni
la VM 29000. La certificación física e integral sigue pendiente.

## Archivo y ramas

- `origin/develop` contiene el commit candidato revisado en esta auditoría.
- `origin/main` permanece en `a4c1107949249fa5ec48e0142b2e260021e4e095` como
  referencia del runtime legacy activo.
- El runtime legacy quedó sellado en
  `archive/controlplane-legacy-main-20261101`.
- Las puntas exclusivas de ramas antiguas se preservaron en 21 tags bajo
  `archive/legacy-branches/`; las ramas locales antiguas y las referencias
  remotas obsoletas se limpiaron.
- El repositorio de origen `controlplanev2` permanece sin cambios. No se
  eliminó durante esta fase de integración.
