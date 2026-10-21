# Evidencia de validación — incremento 1

Fecha: 2026-10-21. Rama: `feature/KVMHOST-91-controlplane-core`.

## Resultado

**48 pruebas aprobadas: 42 rápidas y 6 de integración.**

Entorno aislado: Python 3.12.15, PostgreSQL 17.11, Redis 7.0.15 y Celery 5.5.2.
PostgreSQL y Redis se iniciaron como procesos temporales en loopback, con datos
bajo `/tmp` y puertos libres asignados para la prueba. Se detuvieron al finalizar.
Cada prueba PostgreSQL creó y retiró exclusivamente su base `iacp_test_*`.

La suite completa se ejecutó con `python -m pytest` y las variables
`IACP_INTEGRATION_DATABASE_URL` e `IACP_INTEGRATION_REDIS_URL` apuntando a esos
servicios. Las instrucciones reproducibles están en [DEVELOPMENT.md](DEVELOPMENT.md).

Verificaciones cubiertas:

- Rutas existentes, autenticación estricta, errores sin datos sensibles y
  aprovisionamiento bloqueado.
- Commit anterior a publicación, fallo de cola persistente y protección de un
  job ya completado frente a un timeout ambiguo del publicador.
- Migración desde cero y adopción de filas legadas sin alteración, incluidas
  fechas; revisión repetida sin cambios y rechazo de drift con rollback.
- Comparación de metadata ORM contra PostgreSQL y SQLite sin diferencias.
- Comando CLI Alembic con configuración y secretos de runtime.
- Flujo ASGI FastAPI → Redis real → proceso Celery real → PostgreSQL real,
  consulta del resultado tras recrear la app y no-op sobre job ya completado.
- Snapshot OpenAPI, estados y configuración de conservación de volúmenes y
  dependencias de migración en Compose.

También aprobaron `docker compose -f compose.yaml config --quiet`,
`docker compose -f compose.test.yaml config --quiet`, `bash -n scripts/*.sh`,
compilación Python y `git diff --check`.

Hay un aviso de deprecación del TestClient de la versión conservada de Starlette
al usar el alias BlockingPortal de AnyIO; no hubo fallos de prueba.

## Límites de la evidencia

No se pudo usar el daemon Docker local por permisos; **no se construyeron ni
arrancaron las imágenes Docker**, ni se probó Traefik en ejecución. El stack
`compose.test.yaml` quedó validado estáticamente. La integración descrita usa
procesos nativos temporales y no certifica un despliegue Compose completo.

No se modificó VM 104, no se ejecutó Terraform ni se accedió a Proxmox; no se
eliminaron volúmenes persistentes. No se verificaron producción, backup/restore,
tenants/RBAC, outbox, concurrencia ni recuperación de crashes. Esos gates siguen
pendientes según la [auditoría](AUDIT_PHASE1.md).
