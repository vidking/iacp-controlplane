# Despliegue, actualización y rollback

Procedimiento principal para la transición inicial:
[BOOTSTRAP_TO_GIT.md](../BOOTSTRAP_TO_GIT.md). Este complemento prepara el
incremento posterior; no autoriza ejecutarlo ni sustituye el inventario del host.
La VM 104 queda fuera de todos los states de clientes y no debe modificarse al
preparar un PR. El host definitivo sigue siendo `iacp-controlplane-01`.

## Condiciones para autorizar la ventana

1. Integrar los PRs aprobados en Forgejo y fijar el SHA exacto de main. Revisar
   la dependencia de 2B y el módulo de disco. Registrar SHA del Control Plane,
   revisión de iac-proxmox, digests de imágenes y configuración externa revisada.
2. Inventariar proyecto Compose, contenedores, imágenes, redes y los nombres/IDs
   reales de volúmenes; registrar `docker volume inspect` y mounts de db/redis.
   No deducir nombres a partir de un checkout nuevo.
3. Verificar dump PostgreSQL íntegro, checksum, fecha, propietario y restauración
   probada en una base aislada con PostgreSQL 17. Sin evidencia de restauración,
   detener la transición. Guardar el respaldo fuera de los volúmenes operativos.
4. Verificar AOF de Redis y copia consistente según su configuración vigente;
   conservar trabajos pendientes y medir colas. Respaldar secretos, política,
   asignaciones, planes privados y certificados en almacenamiento autorizado.
5. Tener disponible la imagen/checkout y configuración anteriores, sin borrar el
   bootstrap. Definir quién detiene la cola, quién migra y quién valida rollback.
6. Aprobar migración y despliegue como operaciones separadas. Revisar SQL de 0004:
   agrega `terraform_applies`; no altera las tablas previas ni 0001–0003.

## Preparación reproducible

Usar credencial Forgejo de solo lectura para el servidor, separada de credenciales
API, Wasabi y Proxmox. No incluir credenciales en URL, argumentos de Git ni imagen.
Cada host configura su autenticación Git fuera del repositorio.

```sh
: "${IACP_GIT_READ_URL:?URL de clon autorizada sin credenciales embebidas}"
: "${IACP_CHECKOUT:?Directorio de release fuera del bootstrap}"
: "${IACP_RELEASE_COMMIT:?SHA aprobado de 40 caracteres}"
git clone "$IACP_GIT_READ_URL" "$IACP_CHECKOUT"
git -C "$IACP_CHECKOUT" checkout --detach "$IACP_RELEASE_COMMIT"
test "$(git -C "$IACP_CHECKOUT" rev-parse HEAD)" = "$IACP_RELEASE_COMMIT"
```

El archivo externo de entorno fija proyecto, volúmenes existentes, rutas de
secretos, release y overrides. `compose.server.yaml` exige nombres explícitos y
marca los volúmenes como externos. El checkout no contiene datos de PostgreSQL,
Redis, credenciales ni certificados. La imagen API se construye desde `app` y el
worker Terraform desde su target; no montar el código como volumen del runtime.

```sh
cd "$IACP_CHECKOUT"
# IACP_ENV_FILE y COMPOSE_PROJECT_NAME se obtienen del inventario autorizado.
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml config --quiet
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml build
# Worker Terraform, solo si se autorizó su configuración externa:
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml \
  -f compose.terraform.yaml config --quiet
```

Añadir `compose.tenants.yaml` si se configuraron identidades de tenant. TLS usa
su propio override y directorios, según [guía TLS versionada](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/commit/8355f4bc260dee8959cec052727cb3cb4a6d1e73/docs/TLS_NETWORK.md).
Reutilizar en todos los comandos la misma lista de overrides aprobada. No generar
archivos efectivos con secretos en un directorio versionado.

## Secuencia efectiva, únicamente después de aprobación

Los siguientes comandos son el procedimiento para una ventana futura. Antes de
cada paso comprobar host, proyecto, release, backup y configuración.

```sh
# Pausar aceptación de nuevas mutaciones en CRM y drenar/revisar colas antes de detener workers.
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml stop worker
# Si existe terraform-worker, detenerlo con la lista completa de overrides.
# Migración aprobada; el respaldo restaurable es condición previa obligatoria.
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml \
  run --rm --no-deps migrate alembic upgrade head
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml -f compose.server.yaml \
  up -d --no-build
```

Los workers Terraform deben terminar sin procesos hijos activos; un task marcado
running tras una caída se recupera mediante observación, no volviendo a aplicar.
No ejecutar apply al levantar contenedores: `IACP_TERRAFORM_PLAN_ENABLED=0` e
`IACP_TERRAFORM_APPLY_ENABLED=0` permanecen explícitos hasta la ventana de laboratorio.
No usar `down -v`, ni borrar volúmenes, bootstrap, planes o state para resolver errores.

## Comprobaciones posteriores

- `/health/live` y `/health/ready`; `/v1/instances` sin token devuelve 401.
- Token reader del tenant autorizado solo consulta; otro tenant no ve objetos.
- Job smoke completo y un flujo simulado, sin usar una identidad de producción
  como sustituto de un laboratorio; revisar eventos y autenticación.
- Alembic head esperado, conteo de jobs históricos e IDs previos intactos.
- db/redis montan exactamente los volúmenes inventariados; Redis conserva su AOF.
- API, PostgreSQL, Redis y workers no publican puertos. TLS permite únicamente
  fuentes aprobadas y el listener diagnóstico continúa en loopback.
- Ningún run uncertain se publica como ready sin conciliación. Revisar colas
  plan/apply separadamente y documentar cualquier publicación ambigua.

## Rollback

Detener nuevas mutaciones y workers; no interrumpir a ciegas un apply activo.
Conservar evidencia y conciliar primero si hay incertidumbre. La reversión inmediata
consiste en deshabilitar apply y las nuevas mutaciones conservando una imagen
compatible con 0004, el mismo proyecto y los volúmenes. 0004 es aditiva; la base
ampliada se conserva sin downgrade.

`schema_is_current` compara el head exacto: una imagen 2A/2B anterior espera
0002/0003 y fallará readiness sobre 0004. No anunciarla como rollback binario
válido. Antes de la ventana debe probarse una imagen de compatibilidad con 0004
o aprobarse una restauración aislada y posterior conmutación de PostgreSQL.
Una reversión de configuración o imagen no revierte una KVM ni un state remoto.

Si es imprescindible restaurar PostgreSQL, hacerlo primero en un volumen/base
nuevos y validar jobs, inventario y compatibilidad del esquema. La conmutación
requiere una aprobación distinta y un plan que preserve el volumen anterior.
No restaurar state Wasabi antiguo para intentar deshacer un apply: consultar
[STATE_RECOVERY.md](STATE_RECOVERY.md).
