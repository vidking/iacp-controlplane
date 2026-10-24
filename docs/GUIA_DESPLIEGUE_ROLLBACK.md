# Despliegue y rollback — resumen para Desarrollo

Una release identifica el commit del Control Plane, imágenes por digest, esquema, configuración efectiva y evidencias. El runtime actual verificado usa `0011_crm_contract`; referencias históricas a 0004 no sustituyen ese dato.

El personal autorizado comprueba salud, versión, imágenes, colas, locks, operaciones no terminales y respaldos. La promoción se ejecuta únicamente mediante `iacpctl` y `go_live.py` con manifiesto revisado y evidencia privada nueva. Después se verifican readiness, workers, PostgreSQL, Redis, Traefik, OpenAPI y autenticación.

El rollback cierra admisión y conserva PostgreSQL, Redis, state, historial y trabajos activos. No usa `down -v`, no borra estados ni fuerza desbloqueos. Una operación incierta sigue su procedimiento de recuperación y no se resuelve desde la documentación CRM.

[Ver fuente versionada](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_DESPLIEGUE_ROLLBACK.md).
