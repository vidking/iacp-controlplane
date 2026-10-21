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

## Gate previo y procedimiento único de servidor

**No se ejecutó Alembic en VM 104. Requiere autorización de migración.**
La [revisión del primer despliegue](RELEASE_READINESS.md) constató ausencia de
respaldo, confirmada por el propietario. No continuar con escrituras, clon,
migración ni despliegue hasta acreditar backup y restauración aislada.

[BOOTSTRAP_TO_GIT.md](BOOTSTRAP_TO_GIT.md) es el procedimiento principal. No usar
un resumen abreviado que omita `compose.server.yaml`, el proyecto real o los
volúmenes externos. En particular, construir sin el archivo del servidor puede
sobrescribir la etiqueta `:bootstrap` necesaria para rollback.

Después de resolver los gates y obtener las autorizaciones, el resumen coherente
con ese procedimiento es:

```bash
server() {
  docker compose --env-file "${IACP_SERVER_ENV:?}" -p "${IACP_SERVER_PROJECT:?}" \
    -f "${IACP_RELEASE_DIR:?}/compose.yaml" -f "$IACP_RELEASE_DIR/compose.server.yaml" "$@"
}
server config --quiet
server build api
# Aplicar la ventana aprobada: detener entrada/API, drenar, detener worker y verificar backups de cierre.
server run --rm --no-deps migrate
server up -d --no-deps --pull never api worker traefik
```

`IACP_SERVER_ENV` identifica el archivo fuera de Git; `IACP_SERVER_PROJECT` debe
ser el proyecto observado y `IACP_RELEASE_DIR` el clon fijado al SHA aprobado.
El env debe exigir volúmenes existentes, secretos originales y la etiqueta de
release que coincida con ese SHA. No iniciar ni recrear DB/Redis en este corte.

Si la migración falla, detener la actualización y revisar la discrepancia con
el backup disponible. No borrar tablas ni forzar stamp. El esquema y las filas
previas se conservan dentro de la transacción PostgreSQL.

Repetir `upgrade head` es seguro si la revisión ya está aplicada, pero no evita
la necesidad de aprobación. Tras el arranque autorizado, comprobar readiness,
contrato, worker y smoke según el acta del procedimiento principal.

Para este baseline aditivo, volver a la imagen anterior es compatible con la
tabla `jobs` preservada; no ejecutar downgrade de DB. Conservar el directorio
bootstrap y su imagen; una recuperación de datos requiere autorización propia.
Las revisiones futuras deben definir su procedimiento de recuperación.

Separar credenciales DDL de las credenciales API/worker será un gate de seguridad
posterior. Este incremento conserva los archivos de secretos y roles del Compose
existente para mantener compatibilidad operativa.
