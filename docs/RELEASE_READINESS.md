# Cierre del incremento 1 y preparación del primer despliegue Git

Fecha: 2026-10-21, zona America/Costa_Rica. PR #1.

**Revisión técnica terminada; transición detenida por ausencia de respaldo.**
El propietario confirmó que no hay backup. No se autorizan implícitamente merge,
migración, despliegue, creación de credenciales ni modificaciones del bootstrap.
El procedimiento principal sigue siendo [BOOTSTRAP_TO_GIT.md](BOOTSTRAP_TO_GIT.md).

## A. Revisión del PR

Base revisada: `a964850bd3981a5b1cc573e2a9afdbf1a2025511`.
Head al iniciar la revisión: `49f7c914dfcccd9afe8100041b3d71f3a0824e06`.

- Revisé el diff, rutas, servicio de jobs, configuración, worker, contratos,
  migración, Dockerfiles y separación entre entornos. No identifiqué un cambio
  destructivo del esquema ni un bloqueo nuevo de código dentro del alcance del
  laboratorio. No incorporé funciones del incremento 2.
- Revisé OpenAPI 3.1.0, API 0.2.0: health público; smoke 202; lectura de jobs
  200/404; Bearer en todas las operaciones `/v1`; aprovisionamiento bloqueado 503.
  El snapshot tiene pruebas contra la aplicación y operation IDs estables.
- La única revisión Alembic es `0001_bootstrap_jobs`. Valida la tabla existente
  antes de adoptarla; no reescribe filas. El downgrade destructivo está bloqueado.
  No ejecuté Alembic contra la base activa.
- La evidencia Docker existente registra 57 pruebas y el ensayo completo desde
  un clon limpio de Forgejo del código `e320e67`. Hasta el head revisado solo
  cambió documentación. Este cierre también modifica únicamente documentación;
  no requiere reconstruir la aplicación ni repetir pruebas de código sin cambios.
- Revalidé sintaxis Python/Bash, los tres Compose independientes y el diff.
  No hay rutas personales, instrucciones internas de trabajo ni dependencias
  del host en la aplicación. Los secretos siguen fuera de Git y del build.
- Corregí el resumen operativo de `MIGRATIONS.md` para que use siempre el archivo
  del servidor y una etiqueta de release; no debe sobrescribir `:bootstrap`
  ni iniciar/recrear los servicios de datos por usar comandos abreviados.

El PR puede someterse a aprobación técnica. Esto no equivale a aprobar su despliegue.
Las limitaciones conocidas (token compartido, outbox/idempotencia/concurrencia y
RBAC pendientes) siguen siendo gates para exponerlo a clientes.

## B. Inspección de VM 104, exclusivamente de lectura

Se comprobó por `ssh iacp-controlplane` el hostname `iacp-controlplane-01`.
Proyecto Docker real: `iacp-controlplane`. Bootstrap:
`/opt/iacp/stacks/controlplane/compose.yaml`. Compose 5.6.0.

| Servicio | ID de contenedor observado | Estado |
| --- | --- | --- |
| PostgreSQL | `a7cc37508fa4` | Healthy, PostgreSQL 17.11 |
| Redis | `8fa3f587bb6c` | Healthy, Redis 7.4.11 |
| API | `9620a272864a` | Healthy |
| Worker | `5f733e80bc29` | Running; ready y un job exitoso en logs, sin ERROR/CRITICAL observados |
| Traefik | `b7ae8fb141d0` | Running; solo `127.0.0.1:8080` publicado |

Los cinco contenedores registraban cero reinicios. Los IDs son una fotografía:
repetir el inventario antes de una ventana aprobada.

| Volumen Docker real | Identidad y montaje observados |
| --- | --- |
| `iacp-controlplane_postgres_data` | Driver local; creado 2026-10-21 01:07:32 −06:00; montado en `/var/lib/postgresql/data` |
| `iacp-controlplane_redis_data` | Driver local; misma fecha de creación; montado en `/data` |

En este driver, el nombre identifica al volumen; no hay un ID de contenedor que
lo sustituya. Etiquetas, nombres y creación quedaron comprobados con inspect.
Los datos residen en volúmenes Docker, no en los directorios vacíos de `/srv/iacp`.

PostgreSQL tiene `fsync=on` y `full_page_writes=on`. La inspección se ejecutó en
una transacción READ ONLY: tabla `jobs` con las seis columnas, tipos, nulabilidad,
PK e índice esperados; un job completed, ninguno queued/running, sin tabla
`alembic_version`. La compatibilidad estructural observada no sustituye el ensayo
contra una restauración ni autoriza ejecutar la migración activa.

Redis tiene AOF habilitado en `/data/appendonlydir`, `appendfsync=everysec`, último
write/rewrite OK y RDB con estado OK. Cola `celery`, hash `unacked` e índice
`unacked_index` estaban vacíos. La persistencia está configurada; no es un backup.

Los dos secretos del bootstrap están montados como archivos de solo lectura,
modo 0640, root:10001. No leí ni publiqué sus valores. Las rutas de secretos
existentes no deben moverse, regenerarse ni copiarse a Git. Los healthchecks live
y ready respondieron 200; no envié POST ni creé jobs.

Los hashes de Compose, Dockerfile, requirements y los módulos bootstrap main,
db, models y worker coinciden con `a964850`. El Compose activo pasó `config --quiet`.

Imagen bootstrap de API/worker para conservar antes de construir una release:
`sha256:6de426d5ed9cb426afdea396f3d74ecc82d6d718e4018228f65e98b3ac8e267a`.
No la etiqueté, reemplacé ni eliminé durante la inspección.

## C. Bloqueos y tareas no ejecutadas

No se localizaron dumps ni evidencias de restore en las rutas de backup y del
stack inspeccionadas. Tampoco referencias de backup PostgreSQL en cron/timers
revisados. La ausencia fue confirmada por el propietario. No se acreditó PBS ni
restauración externa. **No continuar con escrituras en VM 104.**

El usuario de despliegue no tiene un acceso saliente de solo lectura a Forgejo
acreditado. No creé una clave, configuración, directorio de release, clon ni
archivo del servidor. Quedan pendientes después de resolver el backup:

1. Autorizar un dump PostgreSQL y restauración aislada según la siguiente sección.
2. Acreditar permisos de solo lectura de una deploy key exclusiva para este
   repositorio, almacenada fuera del checkout. No reutilizar credenciales del
   equipo de desarrollo, copiar claves privadas ni habilitar agent forwarding.
   Verificar la huella SSH del servidor por un canal autorizado.
3. Clonar desde la URL oficial de Forgejo con esa identidad y fijar el SHA de
   cierre del PR. No usar `main` como referencia flotante de despliegue.
4. Crear la configuración del servidor fuera de Git. `IACP_RELEASE` será el SHA
   completo aprobado. Los volúmenes externos deben ser exactamente los dos
   nombres de la tabla anterior y los secretos deben ser los archivos originales.
5. Validar el Compose combinado, mounts, redes y permisos, y ensayar Alembic
   sobre la restauración aislada. No ejecutar `upgrade` contra la base activa.

El candidato exacto será el SHA completo del commit que publica este cierre,
informado junto al reporte y verificable en el head de PR #1. Su aplicación es
idéntica a la probada en `e320e67`. Para conservarlo tras la integración, usar un
merge que mantenga el commit candidato (fast-forward o merge convencional).
Si se cambia su contenido o se usa squash/rebase, fijar y validar el nuevo SHA
antes de autorizarlo. No creé un tag ni una rama del incremento 2.

## D. Respaldo y recuperación propuestos, aún no autorizados

La siguiente operación es independiente del merge/migración/despliegue. Requiere
aprobación expresa para crear archivos de backup y un contenedor de restauración
sin red ni puertos, con almacenamiento tmpfs y sin montar volúmenes del runtime.
No detiene ni modifica la base activa. Los comandos se ejecutarían como root:

```bash
set -euo pipefail
umask 077
IACP_BACKUP_DIR="/srv/iacp/backups/pre-git-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 0700 "$IACP_BACKUP_DIR"
docker exec iacp-controlplane-db-1 pg_dump -U iacp -d iacp --format=custom > "$IACP_BACKUP_DIR/iacp.dump.partial"
test -s "$IACP_BACKUP_DIR/iacp.dump.partial"
mv "$IACP_BACKUP_DIR/iacp.dump.partial" "$IACP_BACKUP_DIR/iacp.dump"
sha256sum "$IACP_BACKUP_DIR/iacp.dump" > "$IACP_BACKUP_DIR/iacp.dump.sha256"
docker exec -i iacp-controlplane-db-1 pg_restore --list < "$IACP_BACKUP_DIR/iacp.dump" > "$IACP_BACKUP_DIR/iacp.dump.toc"
```

La restauración debe utilizar la imagen PostgreSQL actual, ya presente, por ID:
`sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`.
No descargar otra versión. Generar un nombre nuevo para el contenedor de prueba:

```bash
IACP_RESTORE_CONTAINER="iacp-restorecheck-$(date -u +%Y%m%dT%H%M%SZ)"
docker run -d --name "$IACP_RESTORE_CONTAINER" --network none \
  --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_USER=iacp -e POSTGRES_DB=iacp_restorecheck \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
restore_ready=0
for attempt in $(seq 1 30); do
  if docker exec "$IACP_RESTORE_CONTAINER" pg_isready -U iacp -d iacp_restorecheck > /dev/null; then
    restore_ready=1
    break
  fi
  sleep 1
done
test "$restore_ready" -eq 1
docker exec -i "$IACP_RESTORE_CONTAINER" pg_restore -U iacp -d iacp_restorecheck \
  --no-owner --no-privileges --exit-on-error --single-transaction < "$IACP_BACKUP_DIR/iacp.dump"
docker exec "$IACP_RESTORE_CONTAINER" psql -X -U iacp -d iacp_restorecheck \
  -c 'SELECT status, count(*) FROM jobs GROUP BY status;'
```

No basta listar el TOC: verificar restore exitoso, esquema, filas/fechas e
integridad, y registrar fecha, SHA256 y resultado fuera de Git junto al backup.
El archivo custom y la restauración transaccional están descritos en
[pg_dump 17](https://www.postgresql.org/docs/17/app-pgdump.html) y
[pg_restore 17](https://www.postgresql.org/docs/17/app-pgrestore.html).
Retirar solo el contenedor aislado al finalizar; no tocar contenedores ni
volúmenes existentes. Custodiar además una copia cifrada fuera de la VM y
acreditar su recuperación antes de considerar protegido el servicio. Un dump
local no protege frente a pérdida de la VM. El backup de cierre de la ventana
aprobada también debe incluir Redis y custodia de los secretos, según el
procedimiento principal. Nunca subir dumps, hashes de secretos ni credenciales a Git.

## E. Comandos de cambio, solo después de todos los gates y aprobación

Con el clon ya fijado al SHA aprobado y la configuración fuera de Git:

```bash
server() {
  docker compose --env-file "${IACP_SERVER_ENV:?}" -p iacp-controlplane \
    -f "${IACP_RELEASE_DIR:?}/compose.yaml" -f "$IACP_RELEASE_DIR/compose.server.yaml" "$@"
}
server config --quiet
server build api
# Detener entrada/API, drenar cola, detener worker y verificar backups de cierre.
server run --rm --no-deps migrate
server up -d --no-deps --pull never api worker traefik
```

**Estos comandos no se ejecutaron.** Requieren aprobación separada de merge,
migración y despliegue. El proyecto, los dos volúmenes y los contenedores de datos
se conservan. Guardar antes una etiqueta de rollback de la imagen bootstrap por
ID; no reemplazar `:bootstrap`. Recuperar API/worker/Traefik desde el directorio
anterior ante fallo; no ejecutar downgrade, restauración de datos automática ni
mantener dos propietarios del stack.

## F. Comprobaciones posteriores y siguiente incremento

- Verificar SHA e imagen aprobados en API/worker; revisión Alembic única correcta;
  health live/ready 200 y OpenAPI igual al contrato aprobado.
- Verificar que DB/Redis conserven IDs de contenedor, volúmenes, mounts y fecha de
  creación; no recrearlos durante el corte. Conservar los secretos originales.
- Ejecutar un smoke autorizado por Traefik y consultar el mismo job hasta
  completed; conservar el job histórico sin cambios y explicar el incremento
  esperado del recuento. Revisar worker y ausencia de errores, cola/unacked vacíos.
- Verificar 401 sin Bearer y 503 en instancias; mantener Terraform deshabilitado.
- Registrar acta operativa de aceptación: aprobación, responsable, SHA, imágenes,
  revisión, resultados, timestamps y rollback disponible, sin secretos. Traefik,
  logs de API/worker y timestamps de jobs dan evidencia operativa básica; **no hay
  auditoría persistente de acciones/actor/tenant en el incremento 1**. No darla
  por validada: pertenece al siguiente incremento.

Solo tras integrar, desplegar y validar el incremento 1 se abrirá una rama nueva
desde el `main` integrado para tenants, aislamiento, KVM, RBAC, idempotencia,
estados/eventos y auditoría. No iniciar ese trabajo dentro de PR #1 ni activar
Terraform durante ese incremento.
