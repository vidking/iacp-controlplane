# Cierre Fase F.1 — Navegación y documentación Swagger

Fecha: 24 de octubre de 2026, `America/Costa_Rica` (UTC−06:00).

## Release promovido

- SHA anterior: `e01c27a219471bf32cfac14d3b89df14d0bb0a34`.
- SHA F.1 ejecutado: `4afc5ba1e99a46b3a19e87641bd4dc01ae67b928`.
- PR #54 integrado en `main` mediante merge `9a235b7e1366ceac39a49369576c260d27563e97`.
- Imagen API/worker/Lifecycle: `sha256:b3dc8384cbbe4b2ad5df591459eebdc4f42cbb8d50096e8d6bae6064954bef45`.
- Imagen Terraform worker: `sha256:e31900c2b486ae80dce8c9941bc512d72b20c40486c3eb074c5bc0439673ba6e`.

La promoción utilizó `iacpctl env-promote` y `go_live.py inventory`, `preflight`, `promote` y `verify`, todos PASS. Se conservó un respaldo privado de la configuración antes de recrear los servicios. No se modificaron VMs protegidas, state, providers, credenciales, PostgreSQL, Redis ni Traefik.

## HTTPS real de VM 104

Playwright se ejecutó contra `https://iac.cyberfuel.com/docs` el 24/10/2026 a las 17:57 CR:

- Catálogo, creación, inventario, operaciones, Lifecycle y eliminación autorizada desplazaron al operation real bajo la cabecera fija.
- Deep link, recarga, Atrás y Adelante conservaron la referencia `#/tag/operationId`.
- El menú móvil se cerró tras seleccionar una operación.
- Las guías locales CRM, operaciones/diagnóstico y despliegue/rollback fueron visibles sin abrir Forgejo.
- El encabezado mostró `API privada · HTTPS/TLS`.
- Los seis recursos Swagger/CSS propios respondieron desde `/docs?asset=...`; no hubo CDN ni solicitudes externas.
- Errores JavaScript: cero.
- Capturas privadas: `SWAGGER_VM104_F1_DESKTOP.png` y `SWAGGER_VM104_F1_MOBILE.png` en `/home/despinoza/.local/state/iacp-fase-f/`.

## Regresión y salud

- OpenAPI `3.1.0`, versión `0.10.1`; la proyección contractual `/v1` permanece idéntica.
- Autenticación Bearer sobre `GET /v1/profiles`: HTTP 200. No se ejecutaron mutaciones.
- Readiness y API saludables; workers, PostgreSQL, Redis y Traefik operativos.
- Esquema `0011_crm_contract`; snapshots de inventory/preflight/promote/verify muestran cero operaciones no terminales, cero colas y cero `unacked`.
- OCI continúa BLOCKED de forma independiente por falta de autenticación autorizada del registro Forgejo.

La presentación Swagger queda congelada para el cierre del MVP KVM del 27/10/2026. El GO de MVP/piloto supervisado y el NO-GO de resiliencia integral permanecen separados.
