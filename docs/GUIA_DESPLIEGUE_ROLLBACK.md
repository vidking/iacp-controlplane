# Despliegue y rollback — resumen para Desarrollo

Una release identifica el commit del Control Plane, imágenes por digest, esquema, configuración efectiva y evidencias. El runtime actual verificado usa `0011_crm_contract`; referencias históricas a 0004 no sustituyen ese dato.

El personal autorizado comprueba salud, versión, imágenes, colas, locks, operaciones no terminales y respaldos. La promoción se ejecuta únicamente mediante `iacpctl` y `go_live.py` con manifiesto revisado y evidencia privada nueva. Después se verifican readiness, workers, PostgreSQL, Redis, Traefik, OpenAPI y autenticación.

El rollback cierra admisión y conserva PostgreSQL, Redis, state, historial y trabajos activos. No usa `down -v`, no borra estados ni fuerza desbloqueos. Una operación incierta sigue su procedimiento de recuperación y no se resuelve desde la documentación CRM.

## Secuencia resumida

1. `inventory`: confirma salud, workers, colas y ausencia de operaciones no terminales.
2. `preflight`: compara checkout, imágenes, mounts, secretos y persistencia contra el manifiesto.
3. `promote`: drena admisión, ejecuta migraciones idempotentes y recrea solo los servicios de aplicación necesarios.
4. `verify`: valida readiness, workers, esquema, autenticación, contrato y configuración posterior.

La guía accesible a Desarrollo no contiene rutas privadas, tokens ni comandos privilegiados. El runbook administrativo detallado permanece restringido.

[Ver fuente versionada](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_DESPLIEGUE_ROLLBACK.md).
