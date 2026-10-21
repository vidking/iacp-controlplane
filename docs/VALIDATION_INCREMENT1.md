# Evidencia de validación — incremento 1

Fecha: 2026-10-21. Rama: `feature/KVMHOST-91-controlplane-core`. PR #1.

## Resultado Docker desde Forgejo

**57 pruebas aprobadas: 51 rápidas y 6 de integración.** Además, el ensayo del
runtime completo y la conservación de datos y secretos tras recreación terminó
con salida 0.

Commit probado: `e320e674e278b5060473ee8d9a27a71646047f94`. Se publicó en Forgejo,
se clonó la rama en un directorio temporal nuevo y se ejecutó desde ese clon:

```bash
bash scripts/validate-docker.sh
```

El clon no contenía archivos de configuración/secretos del checkout de trabajo y
conservó su árbol limpio al finalizar. No se instalaron Python, PostgreSQL, Redis
ni dependencias de la aplicación en el host para esta validación.

Entorno de validación: Docker Engine 29.9.1, Compose 5.6.0, contenedores Linux;
Python 3.12.15 y Celery 5.5.2 dentro de las imágenes. Se utilizaron las imágenes
`postgres:17-alpine`, `redis:7-alpine`, `python:3.12-slim` y `traefik:v3.1`.

## Suite automatizada

El test runner se construyó con `tests/Dockerfile`; PostgreSQL y Redis se
levantaron con `compose.test.yaml`, sin puertos publicados, secretos del servidor
ni volúmenes persistentes. Las seis pruebas de integración se ejecutaron sin skips.

Resultado del runner:

```text
57 passed, 1 warning in 6.73s
```

La suite cubre autenticación, contratos Pydantic/OpenAPI, publicación y consulta
persistente de jobs, migración desde cero, adopción de filas heredadas sin cambios,
rechazo/rollback de drift, comparación contra metadata y comando CLI Alembic.
Incluye FastAPI ASGI → Redis real → proceso Celery real → PostgreSQL real.

Las pruebas añadidas comprueban aislamiento de los archivos Compose, exigencia de
recursos existentes para el servidor, secretos de desarrollo privados que no se
reemplazan, rechazo del ensayo con configuración de runtime y exportación OpenAPI
desde otro directorio mediante stdout. El aviso de deprecación de Starlette/AnyIO
no provocó fallos.

## Imagen y runtime completos

Se construyó el Dockerfile de aplicación, sin dependencias de pruebas dentro de
su imagen, y se levantó `compose.dev.yaml` con PostgreSQL, Redis AOF, Alembic,
FastAPI, el worker con su pool prefork y Traefik. El cliente HTTP corrió en otro
contenedor, leyendo el token desde el volumen de secretos de desarrollo.

Se verificaron readiness, rechazo de peticiones sin Bearer, aceptación 202,
finalización del job en DB, contrato OpenAPI y bloqueo 503 de aprovisionamiento.
El smoke se completó antes y después de recrear el runtime.

## Adopción y persistencia

Antes de ejecutar Alembic, el ensayo creó en su DB nueva el esquema y un job
sintético equivalentes al bootstrap. Guardó un marcador en Redis y una huella
privada de los secretos; no imprimió tokens ni contraseñas.

Después de migrar y después de parar/recrear contenedores, ambas verificaciones
produjeron:

```json
{"status": "passed", "bootstrap_job_preserved": true, "redis_marker_preserved": true, "secrets_preserved": true}
```

El job conservó todos sus campos, incluidas fechas; el marcador Redis persistió,
los secretos mantuvieron su contenido y los tres volúmenes conservaron nombre y
fecha de creación. El script terminó con:

```text
Validación Docker aprobada: suite, imagen de runtime, Traefik y persistencia tras recreación.
```

Se retiraron únicamente contenedores y redes de los proyectos nuevos de prueba.
Se conservaron todos sus volúmenes de desarrollo. No se ejecutó limpieza global,
prune ni borrado de volúmenes.

## Verificaciones adicionales y límites

Aprobaron la sintaxis de Compose de pruebas y desarrollo, el render combinado de
`compose.yaml`/`compose.server.yaml` con parámetros ficticios explícitos,
`bash -n scripts/*.sh` y `git diff --check`. Se comprobó que esta continuación no
modifica `compose.yaml`, `.env.example`, el Dockerfile/código de aplicación ni los
archivos Traefik del servidor. La aplicación no depende de rutas personales ni
de la configuración del equipo de desarrollo.

La evidencia anterior del incremento 1 incluía 48 pruebas con procesos nativos.
La repetición Docker descrita aquí cierra la validación de imágenes y Traefik que
entonces estaba pendiente. Se validó un host Docker local; no se afirma una
matriz de arquitecturas o sistemas operativos ensayada.

No se accedió a VM 104 ni a Proxmox, no se ejecutó Terraform y no se fusionó el PR.
Los datos del ensayo son sintéticos. El inventario y la restauración de backups
reales del servidor siguen pendientes de la ventana aprobada de
[transición a Git](BOOTSTRAP_TO_GIT.md). Tenants/RBAC, outbox, concurrencia y
recuperación de crashes permanecen en los incrementos posteriores de la
[auditoría](AUDIT_PHASE1.md).
