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

Para reanudar sin compartir secretos por chat, el administrador debe actualizar
en el IACP Control Plane (VM 104) las credenciales de servicio MNG y Wasabi en
el almacén seguro del host. La configuración de runtime nueva se guardará en
`/etc/iacp/stacks/dev.env` y `/etc/iacp/stacks/prod.env`, ambos `root:root` con
modo `600`; los valores de secretos no se guardan en Git ni en evidencias. El
token MNG debe permitir leer el inventario completo del clúster MNG, incluidos
VMID 104, 106 y 29000, y operar únicamente en el pool aislado autorizado. La
credencial Wasabi debe acceder al bucket `cyberfuel-iacp-tfstate-lab` y al
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
