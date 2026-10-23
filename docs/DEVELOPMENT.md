# Desarrollo del Control Plane

## Entorno portable

Clonar el repositorio desde Forgejo con credenciales propias del equipo. Docker
Engine o Docker Desktop con contenedores Linux y Compose 2.24+ son el entorno de
ejecución. No se instala la aplicación ni sus servicios en el host. En Windows,
los comandos Compose se pueden ejecutar directamente; los scripts `.sh` requieren
un shell Bash. Git conserva sus finales de línea LF mediante `.gitattributes`.

Copiar `.env.dev.example` a `.env.dev`. Su contenido solo configura el proyecto,
puerto y nombres de DB/usuario de desarrollo. Usar siempre el archivo explícito:

```bash
docker compose --env-file .env.dev -f compose.dev.yaml up -d --build --wait
docker compose --env-file .env.dev -f compose.dev.yaml run --rm -T --no-deps dev-check
docker compose --env-file .env.dev -f compose.dev.yaml logs --tail=100 dev-api dev-worker
docker compose --env-file .env.dev -f compose.dev.yaml down
```

El proyecto de ejemplo es `iacp-development`; la API se publica únicamente en
`127.0.0.1:18080`. Configurar otro nombre de proyecto para mantener entornos
independientes en un mismo host. No usar el nombre del proyecto del servidor.
Las imágenes de desarrollo usan una etiqueta por proyecto distinta de bootstrap.

`dev-init-secrets` genera dos valores aleatorios dentro de `dev_secrets`, con
permisos de lectura para UID 10001. No usa Python/OpenSSL del host, no descarga
secretos del servidor y no los imprime. Al recrear contenedores valida y conserva
los archivos existentes; no los rota. `dev-check` lee el Bearer desde ese volumen
y realiza las peticiones sin exponer el token. Para otros clientes de desarrollo,
leer el archivo indicado por `IACP_API_TOKEN_FILE` dentro del contenedor.

Los volúmenes `dev_postgres_data`, `dev_redis_data` y `dev_secrets` quedan acotados
por el proyecto Compose. `down` retira contenedores y redes, conservando datos.
No usar `down -v`. Los secretos y datos locales no se transfieren al servidor.
`compose.yaml`, los archivos Traefik del servidor y sus secretos siguen separados.

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
| `POST /v1/instances` | 202 con IDs de instancia, operación y job simulado; 409 por conflicto | Bearer operator + Idempotency-Key |
| `GET /v1/instances`, `GET /v1/instances/{id}` | 200 inventario del tenant, 404 ausente/ajeno | Bearer |
| `GET /v1/operations/{id}` | 200 estado y eventos, 404 ausente/ajeno | Bearer |

Todas las rutas protegidas devuelven 401 con `WWW-Authenticate: Bearer` para
credenciales ausentes/incorrectas. Un secreto ausente, ilegible o menor de 32
caracteres devuelve 503 genérico. El token sin prefijo Bearer deja de aceptarse;
`scripts/smoke.sh` ya usa el formato correcto.

`/health/ready` comprueba conexión SQL, revisión Alembic y ping de Redis, con
timeouts finitos. No comprueba capacidad de workers, outbox, backup ni drift
posterior a la migración. `/health/live` se mantiene independiente de DB/Redis.

El contrato está en `docs/openapi.json` y el runtime expone `/openapi.json` y
`/docs`, ambos protegidos por identidad del tenant. En navegador usar el challenge
con usuario `iacp` y token como contraseña, y Authorize HTTPBearer para /v1.
Para regenerarlo sin secretos ni conexión:

```bash
PYTHONPATH=app .venv/bin/python scripts/export-openapi.py
```

Sin instalar Python en el host, construir la imagen de pruebas y exportar por
stdout; la redirección escribe el contrato como el usuario del host:

```bash
docker compose -p iacp-tests -f compose.test.yaml build tests
docker compose -p iacp-tests -f compose.test.yaml run --rm -T --no-deps tests python scripts/export-openapi.py --stdout > docs/openapi.json
```

La prueba de snapshot exige revisar y regenerar cambios de contrato. Los
operation IDs son estables. El [contrato de instancias de 2A](DELIVERY_2A.md)
incluye los límites, permisos mínimos e idempotencia; el ejecutor es simulado.

## Pruebas

La ruta principal usa únicamente Docker:

```bash
bash scripts/validate-docker.sh
```

Ejecuta la suite, construye la imagen del Dockerfile de aplicación y levanta el
runtime con Traefik y el worker con su pool habitual. Inicializa un esquema
bootstrap sintético y un marcador Redis, migra, prueba HTTP y recrea contenedores.
Verifica igualdad del job legado completo, secretos y nombres/fecha de creación
de los volúmenes. Los proyectos tienen nombres nuevos; al terminar se retiran
sus contenedores y redes y se conservan los volúmenes de desarrollo, cuyos nombres
se informan al final. No necesita sudo si el equipo ya tiene acceso a Docker.

Para comprobar únicamente los permisos de la imagen, sin iniciar PostgreSQL ni
Redis:

```bash
bash scripts/test-image-permissions.sh
```

Esta regresión construye el Dockerfile de aplicación con los archivos versionados
del árbol de trabajo en un contexto temporal: archivos `600` y directorios `700`.
Ejecuta `alembic -c /app/alembic.ini heads` e importa FastAPI y el worker como el
usuario predeterminado de la imagen (UID/GID 10001), sin red, secretos ni montajes.
Comprueba que `/app` y su contenido pertenecen a `root:root`, con directorios `755`
y archivos `644`, legibles sin permitir escritura a `iacp`. Al terminar retira
solo la imagen de prueba y el contexto temporal; no crea ni elimina volúmenes.
Requiere Git, Bash y acceso al Docker local. La validación completa también
incluye esta regresión.

Para ejecutar solo la suite, usar `bash scripts/test-integration.sh` o los
comandos Compose del README. Ambas rutas construyen el test runner en Docker.
La alternativa de pruebas rápidas con un Python 3.12 propio sigue disponible:

```bash
.venv/bin/python -m pytest -m 'not integration'
bash scripts/test-integration.sh
docker compose -f compose.yaml config --quiet
docker compose -f compose.test.yaml config --quiet
bash -n scripts/*.sh
```

El ensayo solo admite `DATABASE_HOST=dev-db`, DB `iacp_dev*`, Redis `dev-redis` y
`IACP_REHEARSAL=1`. Se niega a inicializar una tabla jobs existente. No debe
ejecutarse contra el servidor. `compose.server.yaml` es un archivo optativo
distinto, descrito en [BOOTSTRAP_TO_GIT.md](BOOTSTRAP_TO_GIT.md).

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
