# Validación Docker del entregable 2A

Fecha: 2026-10-21. Rama: `codex/entregable-2a-instancias`.
Base integrada: `0933765c2891feb37132d2cefc3f279642d2fdbb`.
Solo se usó Docker local, con proyectos nuevos y servicios descartables de
pruebas/desarrollo. No se accedió a VM 104, Proxmox ni Wasabi.

## Resultados

| Comprobación | Resultado |
| --- | --- |
| `bash scripts/validate-docker.sh` | Aprobada: 95 pruebas, imagen de runtime, HTTP por Traefik, worker y recreación |
| `bash scripts/test-integration.sh` después de ajustar el contrato de RAM al módulo KVM | 96 aprobadas: 86 unitarias y 10 de integración, 11.13 s |
| `bash -n scripts/*.sh` | Aprobada |
| `git diff --check` | Aprobada |
| Regresión de imagen desde archivos 600/directorios 700 | Aprobada: UID/GID 10001, código root:root legible sin escritura |
| Alembic en imagen de aplicación | `0002_instances_operations (head)` |
| Snapshot OpenAPI | Aprobado, con el contrato actualizado |

La suite final añadió el rechazo de RAM que no sea múltiplo de 256. Se conserva
una advertencia de deprecación de Starlette/AnyIO; no hubo pruebas omitidas ni
fallos en Docker. Python, pytest y los servicios se ejecutaron en contenedores.

## Evidencia funcional

- Creación transaccional y publicación después del commit; rollback de todas las
  filas si falla la inserción de un evento.
- Idempotencia normalizada, rechazo de otra solicitud con la misma clave,
  exclusión mutua PostgreSQL ante solicitudes simultáneas y nombres únicos por tenant.
- Dos entregas concurrentes reclaman una sola ejecución sobre PostgreSQL.
- Aislamiento de inventario/jobs/eventos; intento de suplantación por cabecera o
  cuerpo; roles reader/operator; tenant inactivo/desconocido; registro inválido,
  duplicado o que intenta remapear el token bootstrap.
- Error de cola, publicación con confirmación ambigua y fallos del ejecutor,
  persistidos sin credenciales ni excepciones crudas.
- Proceso Celery real, PostgreSQL 17 y Redis 7: smoke e instancia simulada terminan;
  las consultas conservan sus resultados al recrear la aplicación.
- Migración desde bootstrap sin versionar y desde `0001` versionada, conservación
  de filas legadas, detección de drift, upgrade repetido y `alembic check` sin cambios.

SHA-256 de `0001_bootstrap_jobs.py`, idéntico a `main`:

```text
92820b3a330bd99bfbd92f1824010182087b2b11e9507866b95b934c51812863
```

## Recreación del runtime

Proyecto independiente: `iacp-dev-check-1792575922-97708`.

El check HTTP completó el smoke y una instancia simulada, repitió su solicitud
sin crear otro job y comprobó los eventos `accepted → started → completed`.
Después de detener y recrear los contenedores, el ensayo confirmó:

```json
{"status":"passed","bootstrap_job_preserved":true,"redis_marker_preserved":true,"secrets_preserved":true,"simulated_inventory_preserved":true}
```

Se compararon nombres y fechas de creación antes/después de estos volúmenes:

- `iacp-dev-check-1792575922-97708_dev_postgres_data`
- `iacp-dev-check-1792575922-97708_dev_redis_data`
- `iacp-dev-check-1792575922-97708_dev_secrets`

El flujo volvió a completar otro smoke y otra simulación tras la recreación.
La limpieza retiró contenedores/redes y conservó esos tres volúmenes de desarrollo.
No se ejecutó `down -v` ni se utilizaron volúmenes o secretos del servidor.

## Límites de esta validación

No certifica aprovisionamiento KVM, locking de state remoto ni recuperación de
un worker interrumpido. Estas capacidades pertenecen a 2B. No se ejecutó
Terraform, ni migración operativa, merge o despliegue. El procedimiento y las
limitaciones de cola/rollback están en [DELIVERY_2A.md](DELIVERY_2A.md).
