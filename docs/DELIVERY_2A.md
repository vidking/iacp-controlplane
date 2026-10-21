# Entregable 2A: inventario y operaciones simuladas

La API acepta instancias y conserva su ciclo de ejecución en PostgreSQL. Celery
usa exclusivamente `SimulatedExecutor`, detrás del contrato `IaCExecutor`.
`ready` significa simulación completada: no existe una VM real. `vmid` y `node`
permanecen nulos; no se asignan direcciones IP ni se contacta Proxmox o Wasabi.
Seleccionar otro valor de `IACP_EXECUTOR` provoca `EXECUTOR_UNAVAILABLE`; esta
versión no contiene una implementación Terraform.

## Contrato de creación

`POST /v1/instances` requiere Bearer e `Idempotency-Key` (1–128 caracteres
alfanuméricos o `._:-`). Ejemplo de cuerpo, exclusivamente para simulación:

```json
{"name":"prueba-kvm","image":"imagen-de-prueba","vcpus":2,"memory_mb":2048,"disk_gb":20}
```

`name` es una etiqueta DNS de hasta 63 caracteres y debe ser único por tenant.
`image` es una referencia lógica; el catálogo aprobado y su asociación a plantillas
Proxmox corresponden a 2B. Los recursos son enteros estrictos: 1–64 vCPU,
512–262144 MiB de RAM en múltiplos de 256 y 8–4096 GiB de disco. Si se omiten, los recursos toman los
valores del ejemplo. Estos límites no sustituyen cuotas ni políticas comerciales.
Se rechazan campos adicionales como `tenant_id`, `node`, `vmid` o `executor`.

HTTP 202 devuelve `instance_id`, `operation_id`, `job_id`, `status: queued` y
`executor: simulated`. Es la aceptación original, incluso al repetir una petición
después de que termine. Consultar los estados actuales mediante:

- `GET /v1/instances/{id}`: recursos, estado, resultado y enlaces de operación/job.
- `GET /v1/instances?limit=50&offset=0`: inventario del tenant, máximo 100 por página.
- `GET /v1/operations/{id}`: solicitante, estado, código de error y eventos ordenados.
- `GET /v1/jobs/{id}`: conserva los campos del contrato de incremento 1.

Instancia, operación, job y evento `accepted` se insertan en una sola transacción.
Después del commit se publica únicamente el ID del job. La restricción única
`(tenant_id, idempotency_key)` arbitra solicitudes concurrentes entre procesos:
misma clave y cuerpo normalizado devuelve los mismos IDs sin volver a publicar;
misma clave y otro cuerpo devuelve 409. La clave se conserva sin caducidad mientras
exista la operación. El nombre repetido con otra clave también devuelve 409.

## Identidad y autorización

La revisión `0002_instances_operations` crea el tenant `bootstrap`, UUID
`00000000-0000-0000-0000-000000000001`. El secreto existente `IACP_API_TOKEN_FILE`
conserva su contrato Bearer, ligado únicamente a ese tenant y al rol `operator`.
No concede acceso global al inventario ni a los jobs de otros tenants.
Los jobs históricos conservan `tenant_id = NULL`; solo bootstrap puede leerlos.
Los nuevos jobs smoke también se vinculan al tenant autenticado.

Para otros tenants, un operador de la plataforma realiza el alta explícita,
después de integrar y migrar esta versión con autorización:

```bash
# Usar la función server y el SHA aprobado del procedimiento BOOTSTRAP_TO_GIT.md.
server run --rm --no-deps api python -m iacp.admin create-tenant --slug "${TENANT_SLUG:?}" --name "${TENANT_NAME:?}"
```

El slug debe ir en minúsculas. La orden devuelve el UUID y no genera credenciales.
Se reserva al operador con acceso al entorno; no se expone un endpoint de alta
de tenants ni selección de tenant por cabecera/cuerpo de la petición.

El archivo externo indicado por `IACP_TENANT_CREDENTIALS_FILE` contiene un array
JSON de objetos con estos campos obligatorios:

| Campo | Valor |
| --- | --- |
| `token_sha256` | SHA-256 hexadecimal minúsculo de un token aleatorio de al menos 32 caracteres |
| `tenant_id` | UUID obtenido del alta del tenant |
| `subject` | Identidad auditable de 1–128 caracteres, usando letras, dígitos o `._:@-` |
| `role` | `operator` para crear/consultar o `reader` para consultar |

Generar, custodiar y distribuir los tokens por el canal de secretos de cada
entorno. Guardar el registro fuera de Git, sin tokens en claro, legible por UID/GID
10001 (por ejemplo, propietario `root:10001`, modo `0440`). No incluirlo en el
contexto de construcción ni en la imagen. El override optativo
`compose.tenants.yaml` lo monta solo en la API; requiere la ruta externa mediante
`IACP_TENANT_CREDENTIALS_PATH`. Se agrega después de `compose.yaml` y, cuando
corresponda, `compose.server.yaml`. No combinarlo con el Compose de desarrollo.

El registro no puede remapear el token bootstrap ni repetir un digest. Un registro
inválido o ilegible cierra la autenticación con 503. Token ausente/incorrecto
devuelve 401; rol insuficiente o tenant inexistente/inactivo devuelve 403.
Recursos ajenos devuelven 404 y quedan fuera del listado. El rol `reader` tampoco
puede crear jobs smoke. Se permite un registro sin el token bootstrap cuando
`IACP_API_TOKEN_FILE` no está configurado. No es un sistema de usuarios/OIDC;
la revocación/rotación se realiza en el registro externo, leído por petición.

## Estados, fallos y trazabilidad

| Entidad | Transiciones |
| --- | --- |
| Job y operación | `queued → running → completed` o `failed` |
| Instancia | `pending → provisioning → ready` o `failed` |
| Evento | `accepted`, `started`, `completed`, `dispatch_failed`, `execution_failed` |

Los eventos registran estado, actor y fecha dentro de la misma transacción que
el cambio correspondiente. Los errores persistidos usan códigos estables y
mensajes genéricos, sin copiar excepciones que pudieran contener credenciales.
No hay endpoint para alterar estados o borrar eventos.

El worker reclama el job con un `UPDATE ... WHERE status = 'queued'` atómico antes
de ejecutar. Entregas simultáneas/repetidas no vuelven a ejecutar trabajos en
curso o terminales. La tarea smoke rechaza jobs de aprovisionamiento. Un tenant
desactivado antes de ejecutar produce `TENANT_INACTIVE`.

Si no se confirma la publicación, la API devuelve 503 y marca las filas todavía
en cola como fallidas con `DISPATCH_FAILED`. Un worker que ya avanzó no es
sobrescrito. Repetir **la misma clave y cuerpo** permite recuperar los IDs y
consultar el resultado; no reintenta ni crea otra instancia. No usar otra clave
para recuperar una operación fallida.

Todavía existe una ventana entre commit y publicación: una caída del proceso
puede dejar un job `queued`. Una interrupción después de reclamarlo puede dejarlo
`running`. No hay recuperación automática, outbox ni garantía de ejecución
exactamente una vez ante crashes. La conciliación, leases, bloqueo de state y
aprobación del plan son requisitos de 2B antes de habilitar efectos reales.

## Migración y recuperación

`0001` permanece intacta. `0002` añade cuatro tablas, dos columnas nullable en
`jobs`, restricciones e índices; inserta únicamente el tenant bootstrap. No
modifica estados, mensajes ni fechas existentes. PostgreSQL usa DDL aditivo y
transaccional; SQLite usa batch solo en las pruebas. Repetir `upgrade head` no
repite cambios. El downgrade está deshabilitado para conservar todos los datos.

La ejecución operativa requiere respaldo restaurable, ventana aprobada y el
procedimiento [BOOTSTRAP_TO_GIT.md](BOOTSTRAP_TO_GIT.md). Los índices y FK pueden
tomar locks mientras migra: no se ha medido carga de producción. No borrar
volúmenes, regenerar secretos ni ejecutar `stamp` para omitir validaciones.

Un rollback de imagen no revierte la base. Drenar los jobs y cerrar la entrada
antes de volver a una versión anterior: sus endpoints no conocen el aislamiento
de 2A y no deben exponerse a tenants nuevos. No reactivar el API anterior como
servicio multiempresa. No vaciar Redis ni eliminar el inventario para recuperar.

## Validación reproducible

```bash
bash scripts/validate-docker.sh
```

Construye desde fuentes versionadas, comprueba permisos de la imagen y ejecuta
pruebas sobre PostgreSQL 17/Redis 7 aislados. El runtime de desarrollo prueba
Traefik → API → Redis → worker Celery → PostgreSQL con creación simulada,
idempotencia y eventos; recrea los contenedores y verifica inventario, job legado,
marcador Redis, secretos e identidad de volúmenes. Conserva los volúmenes de
desarrollo e informa su nombre; no usa datos del servidor. Ver el resultado
registrado en [VALIDATION_2A.md](VALIDATION_2A.md).
