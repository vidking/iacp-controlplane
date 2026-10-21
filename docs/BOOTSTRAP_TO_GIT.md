# Transición del bootstrap a un despliegue basado en Git

## Objetivo y límites

VM 104 `iacp-controlplane-01` ejecutará un commit aprobado de Forgejo mediante
Docker Compose. El cambio sustituye la procedencia del código; conserva el
proyecto Compose, PostgreSQL, Redis, archivos de secretos y volúmenes actuales.
No incorpora Terraform, no cambia la versión mayor de servicios ni modifica
recursos Proxmox. Este procedimiento queda preparado; **no se ha ejecutado en
el servidor y requiere aprobación de despliegue, además de aprobación de merge**.

El ensayo automatizado usa datos sintéticos en Docker local. No sustituye el
inventario, el backup ni la restauración aislada del entorno real.

## Separación entre código y estado

| Elemento | Tratamiento |
| --- | --- |
| Código | Checkout de Forgejo en un directorio nuevo, fijado a SHA aprobado |
| Configuración local del servidor | Archivo fuera de Git, basado en `.env.server.example` |
| Secretos | Mismos archivos del bootstrap, mismas credenciales y permisos; fuera del checkout nuevo |
| PostgreSQL | Mismo volumen real montado en `/var/lib/postgresql/data` |
| Redis | Mismo volumen real montado en `/data`, conservando AOF/RDB y cola |
| Proyecto Compose | Exactamente el valor observado en las etiquetas de los contenedores actuales |
| Imágenes de la aplicación | Nueva etiqueta con SHA; conservar imagen bootstrap para rollback |
| Desarrollo | `.env.dev`, volúmenes `dev_*` y secretos generados localmente; no se transfieren al servidor |

El nombre lógico `postgres_data` no prueba el nombre real del volumen. Docker
Compose aísla recursos por proyecto; mover el checkout y depender del nombre del
directorio puede apuntar a recursos nuevos. El archivo optativo
`compose.server.yaml` utiliza [volúmenes externos con nombre explícito](https://docs.docker.com/reference/compose-file/volumes/):
si falta un volumen, Compose falla en lugar de crearlo. La configuración base
`compose.yaml` permanece sin cambios durante esta preparación.

## 1. Inventario previo, solo lectura

El operador identifica los contenedores del bootstrap y registra, fuera del
repositorio y sin imprimir secretos:

- Ruta del stack y archivos Compose/env originales; nombre del proyecto desde
  `com.docker.compose.project`, no por suposición.
- IDs y nombres de los contenedores; imágenes por ID/digest, usuario y base de
  datos efectivos, redes, puertos y bind mounts de Traefik.
- Nombres reales de los volúmenes montados por DB y Redis, driver, etiquetas y
  destino; existencia de todos los recursos referenciados.
- Rutas de origen de los archivos montados como `db_password` y `api_token`;
  propietario, permisos y comprobación privada de integridad. No registrar su
  contenido en logs, tickets, Git ni la línea de comandos.
- Estado PostgreSQL 17, Redis 7, AOF/RDB, jobs por estado y cola/unacked de Celery.
- Gestión actual del stack: un solo responsable de Compose; no habilitar otro
  reconciliador durante la transición.

Ejemplos de inspección, reemplazando los IDs por los observados:

```bash
docker ps -a --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}'
docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' CONTENEDOR_DB
docker inspect --format '{{range .Mounts}}{{println .Type .Name .Source .Destination}}{{end}}' CONTENEDOR_DB CONTENEDOR_REDIS CONTENEDOR_API
docker volume inspect VOLUMEN_POSTGRES_OBSERVADO VOLUMEN_REDIS_OBSERVADO
```

No usar un `docker inspect` completo ni imprimir `docker compose config` completo:
pueden contener valores de entorno. Usar formatos acotados y `config --quiet`.
Si el inventario difiere del bootstrap esperado, detener la transición y revisar.

## 2. Preparación y backup

Antes de la ventana de cambio:

1. Aprobar el PR y el SHA que se desplegará; clonar desde la URL oficial de
   Forgejo con credenciales autorizadas propias del servidor. No copiar el
   checkout, claves ni configuración de un equipo de desarrollo.
2. Conservar el directorio bootstrap completo y la imagen actual de API/worker
   con una etiqueta de rollback; no sobrescribir `:bootstrap` con la imagen nueva.
3. Crear fuera del checkout el archivo de configuración del servidor, basado en
   `.env.server.example`. Completar proyecto, usuario/base existentes, SHA,
   nombres reales de volúmenes y rutas absolutas de los secretos **existentes**.
   Una ubicación posible es `/etc/iacp/server.env`; la decide el operador.
4. Comprobar lectura de los secretos por el usuario de los contenedores sin
   modificar su contenido. No ejecutar `create-secrets.sh` durante la transición.
5. Obtener backup lógico PostgreSQL consistente en formato de restauración
   comprobado. Registrar recuento de jobs y estados, sin datos sensibles.
6. Respaldar Redis mediante snapshot consistente y conservar su configuración
   de persistencia. Una copia del directorio AOF durante escrituras no demuestra
   consistencia. Un `redis-cli --rdb` a un archivo temporal, luego custodiado fuera
   del contenedor, permite un snapshot; verificar su restauración aislada junto
   con la política de cola. No ejecutar `FLUSHDB` ni `FLUSHALL`.
7. Respaldar los archivos de secretos en el gestor apropiado, con custodia y
   permisos equivalentes; nunca dentro de Git o la imagen. Verificar backups
   lógicos y protección PBS/VM conforme a los gates operativos.
8. Ensayar restauración en un entorno aislado con credenciales y red propias.
   Un snapshot de VM por sí solo no prueba recuperación de aplicación.

Los backups de cierre se toman con los escritores detenidos en el siguiente paso.
No se elimina, renombra, mueve ni copia el volumen activo para cambiar de checkout.

## 3. Comprobación del checkout y configuración

En el directorio nuevo del código se verifica el SHA aprobado y un árbol limpio.
El archivo del servidor es **adicional** al base:

```bash
git status --short
git rev-parse HEAD
docker compose --env-file /etc/iacp/server.env -p PROYECTO_OBSERVADO -f compose.yaml -f compose.server.yaml config --quiet
```

Los marcadores `PROYECTO_OBSERVADO` y las rutas de ejemplo se sustituyen por el
inventario real. `IACP_RELEASE` debe coincidir con `git rev-parse HEAD`. Comprobar
que todos los volúmenes externos existen antes de iniciar nada; no ejecutar
`docker volume create` para resolver una discrepancia.

Construir la aplicación antes de detener escritores:

```bash
docker compose --env-file /etc/iacp/server.env -p PROYECTO_OBSERVADO -f compose.yaml -f compose.server.yaml build api
```

API, worker y migración comparten la misma etiqueta de release. Conservar las
imágenes actuales de PostgreSQL, Redis y Traefik; este cambio no incluye hacer
pull, actualizar esos servicios ni recrearlos. La configuración nueva no debe
competir con el bootstrap por el puerto 8080 o por los mismos datos.

## 4. Ventana aprobada y adopción

1. Bloquear nuevas solicitudes y detener Traefik/API con el Compose original.
   Mantener el worker mientras termina los jobs aceptados.
2. Verificar en DB que no queden jobs `queued` o `running`, y que cola/unacked
   estén vacíos. Si no drenan, detener el procedimiento y resolver el estado;
   este incremento no automatiza reconciliación ni permite perder mensajes.
3. Detener el worker original. Tomar y verificar los backups de cierre. Mantener
   DB y Redis en ejecución con sus contenedores, redes y volúmenes actuales.
4. Desde el checkout aprobado, ejecutar solo la migración sobre la red existente:

```bash
docker compose --env-file /etc/iacp/server.env -p PROYECTO_OBSERVADO -f compose.yaml -f compose.server.yaml run --rm --no-deps migrate
```

La revisión inicial adopta la tabla bootstrap sin cambiar filas. Si falla por
drift, detener la actualización; no forzar stamp, borrar tablas ni crear otra DB.
El detalle está en [MIGRATIONS.md](MIGRATIONS.md).

5. Tras migrar, iniciar únicamente los servicios de aplicación y proxy, sin
   iniciar ni recrear sus dependencias de datos:

```bash
docker compose --env-file /etc/iacp/server.env -p PROYECTO_OBSERVADO -f compose.yaml -f compose.server.yaml up -d --no-deps --pull never api worker traefik
```

El mismo proyecto Compose identifica los servicios existentes y actualiza sus
contenedores. No ejecutar ambos checkouts simultáneamente ni `down --remove-orphans`
durante la transición. No detener ni recrear DB/Redis para cambiar la fuente del código.

## 5. Aceptación

- IDs y mounts de DB/Redis, nombres y creación de volúmenes coinciden con el
  inventario inicial; no se creó almacenamiento sustituto.
- Los archivos de secretos originales mantienen contenido y permisos.
- Readiness y revisión Alembic correctos; recuento y jobs históricos conservados.
- Smoke autenticado a través de Traefik completa en PostgreSQL; el acceso sin
  token falla y `POST /v1/instances` conserva 503.
- Estado y persistencia Redis correctos; no se perdieron mensajes.
- Contenedores API/worker utilizan la imagen etiquetada por SHA y Traefik monta
  los archivos del checkout aprobado. La exposición sigue en loopback.

Registrar SHA, imágenes, resultados y responsable fuera del código de la
aplicación. Mantener el bootstrap y backups hasta cerrar la ventana de observación.

## 6. Rollback

Si falla antes de la migración, volver a iniciar la aplicación del bootstrap con
su Compose e imagen conservados. Si falla después de aplicar **solo este
baseline aditivo**, detener la nueva API/worker/Traefik y reactivar los anteriores
con el mismo proyecto y los mismos datos/secretos. No ejecutar downgrade de DB.
Las filas de jobs siguen siendo compatibles con el bootstrap.

No hacer rollback en paralelo: un solo API/worker propietario del stack. DB y
Redis siguen intactos. Una restauración de datos es una operación separada,
con aprobación específica y análisis de mensajes/jobs; no sobrescribir datos
actuales de forma automática. Este rollback no se presume válido para futuras
migraciones que cambien el esquema.
