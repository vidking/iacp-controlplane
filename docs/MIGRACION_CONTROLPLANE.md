# Plan de migración a IACP Control Plane

Este documento define cómo llevar el código candidato de `controlplanev2` al
repositorio único `iacp-controlplane` sin cambiar el contrato público `/v1` ni
alterar el runtime actual durante la preparación. El nombre `controlplanev2`
identifica el repositorio de origen; la API continúa siendo v1 y conserva su
versión visual 1.0.0.

La migración comienza con bases PostgreSQL nuevas y volúmenes propios. No
importa operaciones ni VMs del runtime anterior. La separación inicial deja
`dev.iac.cyberfuel.com` en MNG con state local persistente, y configura
`iac.cyberfuel.com` para MNG con state en Wasabi bajo `iacp/prod-runtime-v1`. PRX se
mantiene deshabilitado hasta que su configuración y recuperación estén
certificadas.

## Estado del candidato

| Área | Estado preparado en este checkout |
| --- | --- |
| Código base | Aislado en la rama local `migration/controlplane-v2-baseline`, derivada de `controlplanev2/develop` |
| API | Contrato público `/v1`; tags descriptivos en `tags: string[]`; Swagger y el portal muestran un ejemplo completo |
| Persistencia | PostgreSQL 17; esquema nuevo y vacío al inicializar |
| Desarrollo | MNG, state local persistente, volumen independiente |
| Producción | MNG habilitado, state Wasabi bajo `iacp/prod-runtime-v1`, volumen independiente |
| Platinum/PRX | Catálogo vacío y entorno deshabilitado |
| Catálogo MNG | Solo `mng01` está configurado en este candidato; `mng02` requiere validación de storage e imágenes |
| Promoción | No ejecutada; no hubo despliegue ni cambios en DNS, base de datos o Wasabi |

## Secuencia de migración

### 1. Congelar y etiquetar el repositorio anterior

Antes de promover el candidato, registrar el commit y los artefactos realmente
ejecutados del Control Plane actual. Crear una etiqueta de archivo después de
revisar el contenido y conservar el repositorio y sus ramas hasta verificar que
la etiqueta permite recuperar el código. No reescribir el historial fusionado
ni borrar estados, operaciones, locks o volúmenes históricos.

### 2. Preparar el repositorio único

Revisar el diff local de `migration/controlplane-v2-baseline` frente a
`controlplanev2/develop`. Integrar el código en `iacp-controlplane` mediante el
proceso de revisión acordado, conservando la API `/v1`, la versión visible
1.0.0 y el contrato documentado en `docs/API_MANUAL_DESARROLLO.md`. Esta fase
no publica imágenes ni cambia ramas remotas.

### 3. Crear entornos aislados

- Para desarrollo, usar PostgreSQL 17 y volúmenes nuevos exclusivos. Activar
  solo `lab`, dirigido al clúster MNG, con state local persistido en el volumen
  de trabajo. No entregar credenciales Wasabi a desarrollo.
- Para el futuro stack productivo, usar su propio PostgreSQL y volumen de
  trabajo. Activar inicialmente solo `lab` hacia MNG; utilizar Wasabi con el
  prefijo exacto `iacp/prod-runtime-v1` y credenciales entregadas únicamente a los workers.
- No montar volúmenes del runtime anterior ni importar filas de SQLite,
  operaciones, secretos o datos de instancias.
- No borrar el prefijo histórico `iac/lab` ni otros objetos de Wasabi. El
  prefijo nuevo evita mezclar el state que cree este candidato con el histórico.

### 4. Validar configuración sin aprovisionar VMs

Revisar en cada stack la imagen y su digest, manifiesto Compose, nombres de
volumen, secretos, conexión PostgreSQL, rutas HTTPS, token de API, catálogos,
  backend y permisos de red. Confirmar que producción apunta a `iacp/prod-runtime-v1`, que
desarrollo usa backend local y que ambos publican únicamente el entorno
`lab`. Confirmar también que el health check no informa listo cuando la
configuración requerida está incompleta.

El catálogo actual del candidato solo autoriza `mng01`. Antes de ofrecer
`mng02`, comprobar de forma independiente conectividad, permisos, storage,
acceso a las Golden Images y capacidad de clonado. No exponer el nodo mientras
alguna de estas comprobaciones siga pendiente.

### 5. Resolver las operaciones y el lock históricos

Antes de retirar el runtime anterior, inventariar y revisar individualmente
las cinco operaciones antiguas cuyo resultado quedó incierto. Consultar
operación, cola, proceso, VMID y estado físico en Proxmox; conservar la
intención original y reconciliar de forma controlada. No crear operaciones
duplicadas ni marcar como fallidas por suposición.

Investigar por qué Wasabi respondió `403` al state lock y comprobar permisos,
identidad, bucket, endpoint, región, ruta y lock file con credenciales de
lectura limitada. No eliminar el lock ni forzar su liberación para completar
la prueba. Si el estado físico no se puede demostrar, detener el corte y
mantener la operación en estado incierto hasta contar con evidencia.

### 6. Ensayar recuperación en aislamiento

Usar credenciales, colas, base, prefijo y workers dedicados al ensayo. No
interrumpir workers ni servicios compartidos del IACP Control Plane (VM 104).
Mantener como máximo dos aprovisionamientos simultáneos. Cubrir pérdida de
worker, timeout de Terraform, respuesta ambigua de Proxmox, duplicidad de
solicitud, reconciliación y recuperación de state. Registrar para cada caso
alcance, versión, fecha, resultado PASS/FAIL/BLOCKED y evidencia, distinguiendo
recuperación automática, intervención manual y contención fail-closed.

La solicitud de migración autoriza ensayos físicos de aprovisionamiento, pero
no el envío de operaciones hasta demostrar aislamiento por cola, proceso,
instancia y credenciales de prueba. La primera ejecución física será en el
clúster MNG, nodo `mng01`, con una sola operación simultánea por stack y límites
de cuota revisados. Ante visibilidad incompleta, estados inciertos o workers
compartidos, se detienen nuevas inyecciones y se conserva la intención para
recuperación controlada.

### 7. Decidir promoción por etapas

Primero emitir un dictamen de integración piloto con el alcance efectivamente
ensayado. Después completar el dictamen de certificación integral; no declarar
GO integral mientras queden escenarios críticos pendientes. La promoción
futura requiere verificar el commit integrado, la imagen ejecutada, los
servicios, el esquema, las colas, los locks y la versión realmente activa.

Solo después de esos gates se podrá planificar el cambio de tráfico de
`iac.cyberfuel.com`. El primer destino seguirá siendo MNG. Platinum/PRX se
habilitará por separado tras validar endpoints, credenciales, nodos, storage,
Golden Images, catálogo, state y reconciliación. El endpoint de desarrollo
continuará limitado a MNG.

## Criterios para cerrar la migración

- El repositorio único es recuperable desde su etiqueta y el runtime anterior
  permanece preservado como legacy hasta cerrar el corte.
- `/v1`, OpenAPI, Swagger, portal, manual Markdown y ejemplos usan el mismo
  contrato JSON, incluidos los tags como lista flexible.
- Cada runtime usa su PostgreSQL y volúmenes exclusivos; producción escribe
  únicamente en `iacp/prod-runtime-v1` y desarrollo mantiene state local.
- El entorno `lab` funciona contra el clúster MNG con nodos previamente
  certificados; PRX aparece solo cuando se habilita explícitamente.
- Las cinco operaciones inciertas y el `403` de Wasabi tienen resolución
  documentada y evidencia suficiente, sin borrar state ni forzar locks.
- La reconciliación y los escenarios críticos están certificados con
  aislamiento demostrado y un máximo de dos aprovisionamientos simultáneos.
- El dictamen integral es GO antes de cambiar tráfico o retirar componentes del
  runtime actual.

## Estado de ejecución — 2 de noviembre de 2026

El candidato se trabaja en la rama de preparación `migration/controlplane-v2-baseline`,
con base en `controlplanev2/develop` (`4903395face37aff78caa3ec4046dfaef7ed1bb0`).
El código anterior continúa disponible en `origin/main`
(`a4c1107949249fa5ec48e0142b2e260021e4e095`) y en la etiqueta
`archive/controlplane-legacy-main-20261101`. No se reescribió historial.

Antes del corte se creó el respaldo privado
`/opt/iacp/evidence/controlplane-v2-precut-20261102T1400CST/` en el IACP
Control Plane (VM 104). Incluye dumps de las bases legacy, configuración
privada/certificados y el volumen de trabajo de Terraform (2.9 GB). Los cuatro
artefactos pasan la verificación SHA-256. La operación solo leyó el runtime;
los contenedores, la ruta pública, las bases y las VMs permanecen sin cambios.

La suite aislada de esta revisión da `65 PASS` con tres advertencias de
deprecación de FastAPI/Starlette. Las configuraciones Compose local,
producción y desarrollo pasan el render de Docker Compose. La imagen local del
candidato se construyó correctamente y el smoke test devolvió readiness,
revisión de imagen y OpenAPI 1.0.0 con `/v1/instances`. También pasó el
`preflight` estructural del stack dev con credenciales ficticias; no valida
conectividad real con MNG. El `preflight` estructural de producción también
pasó con credenciales ficticias; no comprueba permisos de Wasabi ni de Proxmox.

**No se certificó el aprovisionamiento físico ni se desplegó el runtime nuevo.**
El token de servicio MNG disponible responde HTTP 401; el token de solo lectura
no puede listar VMs ni comprobar los VMID protegidos. Wasabi no reconoce el
bucket con las credenciales instaladas (`NoSuchBucket`) y la enumeración de
buckets devuelve 403. Sin esas dos capacidades no se puede demostrar la
asignación segura de VMID, escribir el state nuevo ni ensayar reconciliación.
No se usaron esos tokens para enviar operaciones y no se modificó Wasabi.
El inventario previo también registró cinco operaciones inciertas en la base
técnica legacy, con leases liberados y workers idle. Deben reconciliarse contra
el estado físico antes de cerrar o retirar ese runtime; sus filas se conservan
en el respaldo y no se importarán a la base nueva.

### Dictamen de gates

Registro al 2 de noviembre de 2026, 13:38 CST. La revisión candidata de runtime
es `8f9ee58352efe5fc5c04bf0168603b1b466ca39f`; la revisión que sigue ejecutando
el IACP Control Plane (VM 104) es `a4c1107949249fa5ec48e0142b2e260021e4e095`.

| Gate | Resultado | Alcance y evidencia |
| --- | --- | --- |
| Suite con PostgreSQL 17.6 aislado | PASS | 65 pruebas, 3 avisos de deprecación; la base temporal fue retirada al terminar. |
| Compose local, dev y producción | PASS | Los tres manifiestos renderizan sin error. |
| Build y smoke de imagen candidata | PASS | API `ready`, revisión coincidente, OpenAPI `1.0.0`, `/v1/instances`, `/docs` y `/swagger` responden. Imagen local; no publicada. |
| Preflight estructural dev/prod | PASS limitado | Configuración con valores ficticios; no verifica credenciales, red ni servicios externos. |
| Respaldo del runtime legacy | PASS | Los cuatro archivos de `/opt/iacp/evidence/controlplane-v2-precut-20261102T1400CST/SHA256SUMS` verifican. |
| Inventario del clúster MNG y VMID protegidos | BLOCKED | Token de servicio HTTP 401; token de lectura no muestra inventario ni VMID 104, 106 y 29000. |
| Aprovisionamiento, eliminación y reconciliación física en MNG | BLOCKED | No se inyectaron operaciones; falta credencial de prueba válida y aislamiento comprobable. |
| State y lock nuevos en Wasabi | BLOCKED | El acceso actual devuelve `NoSuchBucket` y `403`; no se escribió ni alteró ningún objeto. |
| Reconciliación de cinco operaciones inciertas legacy | BLOCKED | Requiere contrastar cada intención con el estado físico; no importar ni descartar filas. |
| Cambio de upstream productivo | BLOCKED / NO EJECUTADO | La ruta sigue en `http://api:8000`; no se desplegó ni publicó la imagen candidata. |

El resultado es **PASS de integración local**, **BLOCKED de piloto físico** y
**sin GO de certificación integral**. La versión ejecutada y la ruta legacy
siguen disponibles para continuidad operativa.

Para reanudar sin compartir secretos por chat, el administrador debe actualizar
en el IACP Control Plane (VM 104) las credenciales de servicio MNG y Wasabi en
el almacén seguro del host. La configuración de runtime nueva se guardará en
`/etc/iacp/stacks/dev.env` y `/etc/iacp/stacks/prod.env`, ambos `root:root` con
modo `600`; los valores de secretos no se guardan en Git ni en evidencias. El
token MNG debe permitir leer el inventario completo del clúster MNG, incluidos
VMID 104, 106 y 29000, y operar únicamente en el pool aislado autorizado. La
credencial Wasabi debe acceder al bucket `iacp-cyberfuel` y al
prefijo nuevo `iacp/prod-runtime-v1`, sin alterar los objetos históricos.

## Procedimiento del candidato

Los manifiestos de ejemplo son
[`manifest-dev.example.json`](../deploy/portainer/manifest-dev.example.json) y
[`manifest-prod.example.json`](../deploy/portainer/manifest-prod.example.json).
Antes de operar, crear copias privadas fuera del checkout, cambiar el SHA de
imagen por el commit revisado y mantener modo `600` en los archivos `.env`.
Los siguientes comandos ilustran la secuencia en un release desplegado bajo
`/opt/iacp/releases/<release>`; cada ruta de evidencia debe ser nueva y quedar
fuera del checkout:

```bash
sudo /opt/iacp/releases/<release>/scripts/iacpctl preflight \
  --manifest /etc/iacp/stacks/manifest-dev.json \
  --evidence-dir /opt/iacp/evidence/preflight-dev-<fecha>
sudo /opt/iacp/releases/<release>/scripts/iacpctl inventory \
  --manifest /etc/iacp/stacks/manifest-dev.json \
  --evidence-dir /opt/iacp/evidence/inventory-dev-<fecha>
sudo /opt/iacp/releases/<release>/scripts/iacpctl deploy \
  --manifest /etc/iacp/stacks/manifest-dev.json \
  --evidence-dir /opt/iacp/evidence/deploy-dev-<fecha>
sudo /opt/iacp/releases/<release>/scripts/iacpctl verify \
  --manifest /etc/iacp/stacks/manifest-dev.json \
  --evidence-dir /opt/iacp/evidence/verify-dev-<fecha>
```

Si después de la activación se decide restaurar el servicio legacy, ejecutar la
acción manual desde el release como root y guardar evidencia nueva:

```bash
sudo /opt/iacp/releases/<release>/scripts/iacpctl restore-legacy \
  --manifest /etc/iacp/stacks/manifest-prod.json \
  --evidence-dir /opt/iacp/evidence/restore-legacy-<fecha>
```

El stack nuevo no crea routers Traefik mediante labels. La activación productiva
solo cambia el upstream `iacp-api` si existe una certificación `PASS` ligada al
mismo SHA de imagen, y verifica readiness y la cabecera
`X-IACP-Build-Revision` en el dominio. No hay rollback automático. Si se decide
volver temporalmente al servicio legacy, el operador dispone de la acción
manual `restore-legacy`: exige que la API legacy esté única, `running` y
`healthy`, limita el cambio a `/etc/iacp/tls/dynamic/iacp.yml`, conserva la ruta
candidata en la evidencia y valida readiness del dominio tras el cambio. El
runtime legacy queda preservado durante el piloto.

Los archivos de ejemplo usan exclusivamente MNG. No activar PRX, no reutilizar
los volúmenes legacy y no arrancar el despliegue hasta que `preflight` compruebe
credenciales y configuración. Después de cargar las credenciales correctas se
repite inventario read-only, se verifica la visibilidad de los VMID protegidos,
se prueba primero el stack de desarrollo y se ejecutan pruebas físicas aisladas
en el clúster MNG, preferiblemente en `mng01`. Solo un resultado integral PASS
autoriza cambiar el upstream de `iac.cyberfuel.com`.

## Revalidación para promoción

Registro del 2 de noviembre de 2026, 14:11 CST. Dictamen: **BLOCKED; no
desplegar ni cambiar el upstream**.

### Versión y estado antes del corte

- `origin/main` del repositorio canónico apunta a
  `a4c1107949249fa5ec48e0142b2e260021e4e095`; es también la revisión de la API
  que sigue ejecutándose en el IACP Control Plane (VM 104). El contenedor API
  está `healthy` y el upstream de Traefik continúa en `http://api:8000`.
- El candidato `8f9ee58352efe5fc5c04bf0168603b1b466ca39f` está construido solo en
  el entorno local; la imagen no está publicada y ningún contenedor candidato
  está activo en el IACP Control Plane (VM 104).
- `origin/develop` canónico apunta a `451b9e7f6ae4105808bfe02f00d69922bf2cbe4a`.
  El checkout canónico conserva cambios locales y no se modificó durante esta
  revalidación.

### Configuración de credenciales

**PASS — copia privada preparada.** `/etc/iacp/stacks/dev.env` y
`/etc/iacp/stacks/prod.env` coinciden con los archivos de origen legacy; ambos
son `root:root` modo `600` bajo un directorio `root:root` modo `700`. Dev y
producción tienen token API, contraseña PostgreSQL y token MNG; Wasabi solo se
incluye en producción. No se imprimieron valores ni se guardaron secretos en
Git. Esta comprobación confirma la copia, no la autorización de las credenciales.

### Operaciones, colas y locks

- La base principal conserva el esquema `0015_technical_access`, no presenta
  operaciones activas y no tiene locks PostgreSQL pendientes ni advisory locks
  activos.
- La base técnica conserva el esquema `0001_technical_schema`: hay 15
  operaciones `succeeded`, **5 `uncertain`**, cinco leases `released`, seis
  workers `idle` y cero workers asociados a una operación actual.
- Redis tiene cero elementos en listas de cola y cero locks pendientes con
  nombre de lock. No se enviaron operaciones nuevas.
- Las cinco operaciones inciertas siguen sin poder reconciliarse porque falta
  un inventario físico autorizado del clúster MNG. Sus registros y leases se
  conservan.

### Gates externos

| Gate | Resultado | Evidencia sin secretos |
| --- | --- | --- |
| Inventario MNG con credenciales de worker, Terraform y Lifecycle | BLOCKED | Los tres tokens responden HTTP 401 a la consulta de recursos VM. |
| Inventario MNG con credencial de solo lectura | BLOCKED | HTTP 200, pero `cluster/resources?type=vm` devuelve cero VMs y no muestra los VMID protegidos 104, 106 y 29000. |
| State nuevo en Wasabi | BLOCKED | `ListObjectsV2` al bucket/prefijo configurado responde HTTP 404 `NoSuchBucket`; no se escribió ningún objeto. |
| Reconciliación de las cinco operaciones inciertas | BLOCKED | No existe una fuente autorizada que permita contrastar las intenciones con el estado físico. |
| Prueba física aislada y promoción | BLOCKED / NO EJECUTADA | No se inició el candidato, no se publicó su imagen y no se cambió Traefik. |

### Acción requerida para reanudar

Actualizar los permisos o credenciales MNG y Wasabi en el almacén seguro del
IACP Control Plane (VM 104), sin enviar secretos por chat. Después, repetir el
inventario read-only y verificar la visibilidad de los VMID protegidos; usar
ese inventario para reconciliar las cinco operaciones inciertas; validar el
bucket y el prefijo nuevo sin tocar objetos históricos; y solo entonces correr
el piloto aislado en `mng01`. La promoción de `iac.cyberfuel.com` sigue
condicionada a certificación integral PASS. La ruta legacy permanece activa.

## Actualización: bucket y preparación de rutas

Registro del 2 de noviembre de 2026. Dictamen: **BLOCKED; no desplegar ni
activar rutas**.

- **PASS — suite local automatizada:** 75 pruebas pasaron contra PostgreSQL
  17.6 temporal en un contenedor con almacenamiento `tmpfs`; se retiró al
  terminar. Hubo tres advertencias deprecadas de Starlette/FastAPI. No fueron
  pruebas físicas del clúster MNG ni escribieron en Wasabi.
- El 404 registrado arriba correspondía al nombre anterior
  `cyberfuel-iacp-tfstate-lab`. El usuario confirmó que Wasabi renombró el
  bucket a `iacp-cyberfuel` y autorizó purgar permanentemente todos sus objetos,
  versiones y delete markers, sin respaldo. La autorización queda registrada;
  no equivale a ejecución.
- La llave Wasabi de runtime no tiene acceso visible al bucket nuevo: la
  consulta `HeadBucket` responde HTTP 403. No se escribió, borró ni versionó
  ningún objeto. La purga y el ajuste de política esperan acceso administrativo;
  luego la llave de runtime deberá quedar limitada a `iacp/prod-runtime-v1`.
- La purga autorizada sin respaldo elimina también la posibilidad de recuperar
  versiones antiguas de Terraform state. Si se revierte el upstream al runtime
  legacy, se podrá restaurar su código y API, pero los aprovisionamientos que
  dependían del state anterior requerirán reconstrucción o reconciliación; el
  rollback de runtime no equivale a recuperar datos.
- El archivo candidato `/etc/iacp/stacks/prod.env` ya apunta a
  `iacp-cyberfuel`. En `dev.env` y `prod.env`, el token MNG copiado del runtime
  legacy fue reemplazado por un marcador fail-closed porque las credenciales de
  worker, Terraform y Lifecycle reciben HTTP 401. La fuente legacy no se tocó.
  Los archivos siguen `root:root`, modo `600`; la llave SSH pública de bootstrap
  se copió desde el archivo público legacy. No se inició ningún stack candidato.
- Desde el IACP Control Plane (VM 104), `mng01.cyberfuel.com` resuelve a
  `192.168.1.113` y `mng02.cyberfuel.com` a `192.168.1.114`; SSH administrativo
  al clúster MNG no está disponible. La credencial read-only anterior tampoco
  muestra VMs ni los VMID protegidos. No se puede emitir el token dedicado de
  candidato ni reconciliar las cinco operaciones `uncertain` con la evidencia
  disponible.
- Se añadió al candidato una plantilla Traefik aislada para
  `dev.iac.cyberfuel.com`, con activación y retirada guardadas bajo evidencia.
  Es un cambio local al worktree: no se instaló en el IACP Control Plane
  (VM 104), no cambió el upstream de `iac.cyberfuel.com` y no afecta PRX.
- El certificado TLS actual de `/etc/iacp/tls/certs/fullchain.pem` declara
  `*.cyberfuel.com` y `cyberfuel.com`, pero no cubre `dev.iac.cyberfuel.com`.
  `activate-develop` ahora valida SAN antes de instalar la ruta; se requiere un
  certificado que incluya el hostname exacto o `*.iac.cyberfuel.com`.

La ruta legacy de producción sigue activa. El corte requiere token MNG nuevo,
inventario que muestre VM 104/106/29000, reconciliación física de las cinco
operaciones inciertas, acceso administrativo a Wasabi para la purga autorizada,
política de runtime limitada al prefijo, pruebas de state-lock y certificación
integral PASS. Ninguno de esos gates se considera completado por este registro.

## Revalidación de credenciales cargadas

Registro del 2 de noviembre de 2026. Dictamen: **BLOCKED; no desplegar ni
purgear**.

- **PASS — almacenamiento seguro:** `dev.env`, `prod.env` y
  `wasabi-admin.env` son `root:root`, modo `600`. Los dos stacks tienen el
  mismo token MNG candidato; el archivo Wasabi contiene ambas claves y no se
  monta en los contenedores.
- **BLOCKED — token MNG:** `GET /cluster/resources?type=vm` autentica con HTTP
  200 y devuelve 59 VMs. Hace visibles VM 106 y VM 29000, pero no VM 104. No se
  ejecutó ninguna operación de escritura. La ACL del token debe permitir
  `VM.Audit` sobre `/vms/104`, manteniendo las capacidades de modificación
  limitadas al pool `iacp-lab`.
- **BLOCKED — Wasabi:** una solicitud firmada `HEAD` al bucket nuevo y una
  lectura `GetBucketVersioning` respondieron `403 AccessDenied` para la
  credencial cargada como administrativa. La firma se contrastó con la llave
  de runtime y el nombre anterior del bucket, que respondió `404 NoSuchBucket`.
  No se listaron objetos ni versiones, no se modificaron políticas y no se
  borró contenido.
- **Sin cambios de runtime:** el API legacy del IACP Control Plane (VM 104)
  continúa `healthy`, el upstream productivo sigue `http://api:8000`, no existe
  la ruta dinámica de desarrollo y no hay contenedores candidatos activos.

Para continuar, ajustar la ACL de auditoría de VM 104 para el token candidato o
cargar una credencial Wasabi con permisos administrativos efectivos en
`wasabi-admin.env`. La llave de runtime sigue separada y necesita permiso sobre
el prefijo `iacp/prod-runtime-v1`. La purga autorizada permanece pendiente.

## Aclaración posterior sobre la política Wasabi

El usuario compartió la política anterior: autorizaba el bucket
`cyberfuel-iacp-tfstate-lab` y solo el prefijo `iacp/lab/*`. Por ello, los
403 obtenidos al consultar el bucket renombrado o el prefijo de producción son
compatibles con esa política; esas respuestas no prueban que la llave sea
inválida. Las comprobaciones `HEAD` y `GetBucketVersioning` tampoco prueban el
acceso de Terraform porque esa política no les concede sus respectivas
acciones. La verificación del runtime debe ser `ListObjectsV2` acotada a
`iacp/prod-runtime-v1/`, que es la consulta implementada en el preflight.

El backend candidato guarda cada state en
`iacp/prod-runtime-v1/instances/{instance_id}/terraform.tfstate` y habilita
`use_lockfile`; la llave necesita lectura/escritura del state y lectura,
escritura y borrado del objeto `.tflock`. HashiCorp documenta estos permisos
para el backend S3 en [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3).
La política propuesta con `iacp/*` abarca otros prefijos; se recomienda limitar
las condiciones y recursos a `iacp/prod-runtime-v1/*` y retirar la regla de
limpieza `iacp/lab/_preflight`. La llave de runtime no debe tener
`s3:DeleteObjectVersion`: la purga de versiones históricas es una tarea
administrativa separada. En buckets versionados, `DeleteObject` crea un delete
marker; eliminar una versión concreta requiere una solicitud con `versionId`,
según [Amazon S3: deleting object versions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html).

## Revalidación operativa posterior — 2 de noviembre de 2026

Dictamen: **preparación parcial; corte y purga siguen bloqueados**. Estas
comprobaciones son de solo lectura y se realizaron desde el IACP Control Plane
(VM 104); no se desplegaron imágenes, no se cambió el runtime y no se escribió
ni eliminó contenido Wasabi.

- **PASS — prefijo de producción:** la credencial configurada para runtime
  obtiene HTTP 200 de `ListObjectsV2` para el bucket `iacp-cyberfuel` y el
  prefijo exacto `iacp/prod-runtime-v1/`; devuelve cero objetos. La lectura de
  versiones para ese mismo prefijo también devuelve HTTP 200 y cero versiones.
- **BLOCKED — inventario/purga del bucket completo:**
  `wasabi-admin.env` contiene el mismo ID de llave que `prod.env`, por lo que
  no es una identidad administrativa separada. Las consultas de inventario de
  todo `iacp/` y `ListBucketVersions` con ese alcance responden HTTP 403
  `AccessDenied`. No se puede afirmar que el bucket completo esté vacío ni
  ejecutar la purga autorizada. Para purgarlo se necesita temporalmente una
  credencial administrativa con `s3:ListBucket`, `s3:ListBucketVersions` y
  `s3:DeleteObjectVersion` limitada al bucket `iacp-cyberfuel`; retirar el
  permiso de borrado de versiones al terminar. La llave de runtime no requiere
  ese permiso.
- **BLOCKED — visibilidad protegida MNG:** el token obtiene HTTP 200 y enumera
  55 VMs; VM 106 y VM 29000 son visibles, pero el IACP Control Plane (VM 104)
  no aparece. La ACL todavía requiere `VM.Audit` sobre `/vms/104`, sin otorgar
  Lifecycle ni permisos de modificación a ese VMID.
- **PASS — no hay operación activa en la base legacy consultada:** la tabla
  `operations` solo contiene `completed` y `failed`; el conteo de estados no
  terminales usados por el API es cero. `jobs` solo contiene `completed` y
  `failed`; el host informa cero procesos `terraform`. Se conservan los
  registros históricos (`terraform_executions`: 126 `planned`, 7 `failed`; no
  se borraron filas ni estados).
- **Runtime observado:** `origin/main` en el remoto es
  `a4c1107949249fa5ec48e0142b2e260021e4e095`, el mismo commit de las imágenes
  activas `iacp/controlplane-api` y `iacp/terraform-worker`. El API legacy está
  healthy y la base y Redis están healthy. El runtime candidato sigue sin
  desplegarse.

El preflight local anterior registró **75 pruebas aprobadas y 3 avisos de
deprecación**. Esto valida el conjunto local, pero no certifica todavía un
aprovisionamiento físico. Para continuar con el corte faltan la ACL de
auditoría para VM 104, el acceso administrativo efectivo requerido para la
purga completa del bucket y la reconciliación documentada de las cinco
incertidumbres previas; el conteo de la base legacy consultada no demuestra
por sí solo que aquellas incertidumbres estén resueltas.

## ACL de auditoría para VM 104 — 2 de noviembre de 2026

El usuario indicó conservar el token MNG actual y ampliar sus políticas; no se
debe crear otro token. La inspección de solo lectura del clúster MNG confirma:

- La identidad es `iacp-orchestrator@pve!runtime-lab` y tiene `privsep=1`.
- El rol actual `PVEPoolAdmin` no incluye `VM.Audit` ni
  `Permissions.Modify`.
- Existen entradas explícitas `NoAccess` en `/vms/104` para el usuario
  `iacp-orchestrator@pve` y para el token `runtime-lab`. Las entradas de
  `iacp-provision@pve` y `iacp-lifecycle@pve` también permanecen en `NoAccess`.
- El rol integrado `PVEAuditor` del clúster incluye privilegios de auditoría,
  pero no `VM.PowerMgmt`, permisos de configuración de VM, `VM.Allocate` ni
  `Permissions.Modify`. En el path exacto `/vms/104` sirve para hacer visible
  ese recurso sin otorgar Lifecycle ni aprovisionamiento.

Como el token tiene separación de privilegios, el administrador del clúster
debe cambiar las dos entradas existentes —usuario y token— a `PVEAuditor` en
`/vms/104`, con propagación desactivada. El token actual no puede realizar el
cambio: carece de `Permissions.Modify`. El acceso SSH administrativo probado
al nodo `mng01` falla en la validación de host key; no se omitió esa validación.
No se alteró ninguna ACL. Proxmox documenta que los permisos efectivos de un
token separado son la intersección de los permisos del usuario y del token, y
documenta `pveum acl modify` para asignar ambos [en su guía oficial de
`pveum`](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc).

Cambios que debe aplicar un administrador del clúster MNG sobre el ACL actual:

```sh
pveum acl modify /vms/104 --users iacp-orchestrator@pve --roles PVEAuditor --propagate 0
pveum acl modify /vms/104 --tokens 'iacp-orchestrator@pve!runtime-lab' --roles PVEAuditor --propagate 0
```

Esto sustituye el `NoAccess` del servicio de orquestación solo en VM 104 y deja
sin cambios las protecciones de VM 106, VM 29000 y las ACL de Lifecycle y
aprovisionamiento. Después de aplicar el cambio, se debe volver a comprobar
`GET /cluster/resources?type=vm` desde el IACP Control Plane (VM 104) y
confirmar que el VMID 104 aparece antes de promover el runtime.
