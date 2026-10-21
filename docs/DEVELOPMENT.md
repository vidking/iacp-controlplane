# Desarrollo del Control Plane

## Arquitectura del incremento 1

| Módulo | Responsabilidad |
| --- | --- |
| `iacp/main.py` | App factory y registro de routers, sin acceso a secretos o DB al importar |
| `iacp/api/` | HTTP, códigos de respuesta e inyección de dependencias |
| `iacp/security.py` | Bearer de laboratorio estricto, comparación constante, fallo cerrado |
| `iacp/schemas.py` | Contratos Pydantic de respuestas y errores |
| `iacp/services/jobs.py` | Commit del job y publicación smoke; fallo persistente si sigue queued |
| `iacp/models.py`, `iacp/db.py` | Modelo existente, engine lazy y sesiones SQLAlchemy |
| `iacp/config.py` | Variables de entorno y lectura de archivos de secretos |
| `iacp/worker.py` | Entrypoint Celery y tarea `iacp.smoke`, sin Terraform |
| `alembic/`, `iacp/migrations.py` | Migraciones, baseline y consulta de versión del esquema |

Se conserva el modelo `Job` y la semántica del smoke. La aceptación HTTP sigue
devolviendo `202 {job_id, status: queued}`. Un fallo de publicación devuelve 503;
si el worker ya avanzó el estado, el servicio no lo sobrescribe como failed.
Un timeout sigue siendo ambiguo: no hay garantía de entrega exactamente una vez.

La separación usa [routers y dependencias FastAPI](https://fastapi.tiangolo.com/tutorial/bigger-applications/).
Las pruebas HTTP usan [TestClient](https://fastapi.tiangolo.com/tutorial/testing/).
Para integración se usa un proceso Celery real porque el modo eager no reproduce
el worker, como explica la [documentación de Celery](https://docs.celeryq.dev/en/v5.5.2/userguide/testing.html).

## Contrato HTTP

| Método y ruta | Respuesta | Autenticación |
| --- | --- | --- |
| `GET /health/live` | 200 live; sin dependencias externas | Pública |
| `GET /health/ready` | 200 ready o 503 por DB, esquema o Redis | Pública |
| `POST /v1/jobs/smoke` | 202 job aceptado o 503 por publicación/auth | Bearer |
| `GET /v1/jobs/{job_id}` | 200 job persistente, 404 ausente | Bearer |
| `POST /v1/instances` | 503 `IAC_EXECUTOR_NOT_CONFIGURED` | Bearer |

Todas las rutas protegidas devuelven 401 con `WWW-Authenticate: Bearer` para
credenciales ausentes/incorrectas. Un secreto ausente, ilegible o menor de 32
caracteres devuelve 503 genérico. El token sin prefijo Bearer deja de aceptarse;
`scripts/smoke.sh` ya usa el formato correcto.

`/health/ready` comprueba conexión SQL, revisión Alembic y ping de Redis, con
timeouts finitos. No comprueba capacidad de workers, outbox, backup ni drift
posterior a la migración. `/health/live` se mantiene independiente de DB/Redis.

El contrato está en `docs/openapi.json` y el runtime expone `/openapi.json` y
`/docs`. Para regenerarlo sin secretos ni conexión:

```bash
PYTHONPATH=app .venv/bin/python scripts/export-openapi.py
```

La prueba de snapshot exige revisar y regenerar cambios de contrato. Los
operation IDs son estables. No hay contrato de entrada para instancias hasta
definir tenants, permisos y operaciones autorizadas.

## Pruebas

```bash
.venv/bin/python -m pytest -m 'not integration'
bash scripts/test-integration.sh
docker compose -f compose.yaml config --quiet
docker compose -f compose.test.yaml config --quiet
bash -n scripts/*.sh
```

`compose.test.yaml` es independiente: no combinarlo con `compose.yaml`. No
publica puertos, no monta datos del host y usa tmpfs para PostgreSQL. Los secretos
del runtime no entran en el contexto de build. El script genera un nombre de
proyecto de prueba y retira sus contenedores al terminar, sin borrar volúmenes.
La red de prueba es interna; trust de PostgreSQL se limita a este stack efímero.

También se puede ejecutar la suite contra servicios locales **descartables**:

```bash
export IACP_INTEGRATION_DATABASE_URL=postgresql+psycopg://iacp@127.0.0.1:55432/iacp_test_suite
export IACP_INTEGRATION_REDIS_URL=redis://127.0.0.1:56379/15
.venv/bin/python -m pytest
```

El usuario PostgreSQL de prueba necesita CREATEDB. Cada prueba crea y elimina
solo su base con nombre aleatorio `iacp_test_*`; el proceso Celery hijo se detiene
antes de limpiarla. Solo se admiten loopback o los hosts del Compose de prueba.
Redis debe ser exclusivo de las pruebas, base 15. Sin estas variables, las
pruebas de integración se omiten expresamente. No usar túneles hacia entornos
persistentes ni el runtime existente.

SQLite permite las pruebas rápidas, pero no sustituye las verificaciones de
tipos, transacciones y migraciones sobre PostgreSQL 17. La integración comprueba
adopción de filas legadas, rollback ante drift y API → Redis 7 → Celery → DB,
incluida consulta tras recrear la app. No certifica concurrencia ni recuperación
de crashes, que corresponden al incremento 3.

## Configuración

Las variables y archivos de secretos de Compose se conservan. Para servicios de
prueba fuera de Compose se añade `DATABASE_PORT` (default 5432). La URL SQLAlchemy
se construye como objeto, sin concatenar ni registrar contraseñas. El engine
incluye timeout de conexión, de espera de pool y de sentencias SQL.

Las versiones directas del bootstrap se conservan y se añaden Alembic y
dependencias de prueba fijadas. La política de actualización, lock transitivo,
escaneo y digests de imágenes sigue pendiente.
