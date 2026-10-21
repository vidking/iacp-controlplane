# Migraciones y conservación del bootstrap

## Comportamiento

La revisión `0001_bootstrap_jobs` crea `jobs` e índice `ix_jobs_status` cuando la
tabla no existe. Si ya existe, inspecciona columnas, tipos, nulabilidad, defaults,
índices, relaciones, checks y primary key contra una definición congelada del bootstrap.
No actualiza ni elimina filas. Diferencias provocan un error y no se registra
la revisión. No se usa `stamp head` para omitir esta validación.

Alembic administra su tabla `alembic_version`. El baseline no tiene downgrade
destructivo: intentar volver a base falla explícitamente para proteger jobs.
Las migraciones offline están deshabilitadas porque la adopción requiere
inspeccionar el esquema real. Ver [Alembic](https://alembic.sqlalchemy.org/en/latest/tutorial.html).

La inspección estructural no valida todos los objetos externos (por ejemplo,
triggers o privilegios) ni la semántica de cada fila. Un entorno personalizado
requiere revisión antes de adoptarlo.

## Actualización futura del laboratorio

**No se ejecutó este procedimiento en VM 104. Requiere aprobación de despliegue.**

Antes de actualizar: backup lógico consistente y restore comprobado; revisión
del diff, secreto disponible para UID/GID 10001 y ventana sin escritores. No
cambiar el nombre del proyecto Compose ni los volúmenes `postgres_data` y
`redis_data`. No usar `down -v`.

Una vez aprobado, desde el checkout del stack correcto:

```bash
docker compose config --quiet
docker compose stop api worker
docker compose build api worker migrate
docker compose run --rm migrate
```

Si la migración falla, detener la actualización y revisar la discrepancia con
el backup disponible. No borrar tablas ni forzar stamp. El esquema y las filas
previas se conservan dentro de la transacción PostgreSQL.

Después de una migración exitosa:

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8080/health/ready
bash scripts/smoke.sh
```

El servicio `migrate` también corre durante `up`; repetir `upgrade head` es
seguro si la revisión ya está aplicada. API y worker esperan su salida exitosa.
Para este baseline aditivo, volver a la imagen anterior es compatible con la
tabla `jobs` preservada; no ejecutar downgrade de DB. Las revisiones futuras
deben definir su propio procedimiento de recuperación.

Separar credenciales DDL de las credenciales API/worker será un gate de seguridad
posterior. Este incremento conserva los archivos de secretos y roles del Compose
existente para mantener compatibilidad operativa.
