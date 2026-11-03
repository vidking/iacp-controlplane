# Configuración y operación de stacks

Esta guía documenta el Compose canónico, los manifiestos de ejemplo y los
controles para operar los stacks de API v1. El estado del runtime productivo y
las pruebas físicas se registran en
[`PRODUCCION_CONTROLPLANE_V1_20261102.md`](auditorias/PRODUCCION_CONTROLPLANE_V1_20261102.md).

## Manifiestos y separación

| Stack | Manifiesto | Imagen | Destino inicial | Persistencia |
| --- | --- | --- | --- | --- |
| Desarrollo | `deploy/portainer/docker-compose.yml` con variables de desarrollo | SHA inmutable; ruta pendiente de DNS y TLS | clúster MNG, entorno lógico `lab` | PostgreSQL y workdata propios; state Terraform local |
| Producción | `deploy/portainer/docker-compose.yml` con variables de producción | SHA inmutable aprobado | clúster MNG, entorno lógico `lab`; PRX apagado | PostgreSQL y workdata propios; state local persistido mientras `IACP_WASABI_ENABLED=false` |

Cada entorno necesita nombres de volumen exclusivos. Los valores de ejemplo
son `iacp_controlplane_dev_postgres`, `iacp_controlplane_dev_workdata`,
`iacp_controlplane_prod_postgres` e `iacp_controlplane_prod_workdata`. No
compartas volúmenes entre stacks ni montes el volumen del runtime anterior.
La base comienza vacía; no se importan operaciones ni instancias antiguas.

El stack declara PostgreSQL 17 y dos workers (`worker-1`, `worker-2`). La cola
usa locks transaccionales y limita globalmente a dos las operaciones activas.
No escales workers sin cambiar y revisar ese límite.

## Variables y secretos

Usa `deploy/portainer/.env.example` como lista de nombres, no como valores
operativos. Crea secretos diferentes para PostgreSQL, API, Proxmox MNG y
Wasabi. No guardes credenciales reales en Git ni en tickets.

En ambos stacks, comienza con:

```text
IACP_DEFAULT_ENVIRONMENT=lab
IACP_ENABLED_ENVIRONMENTS=lab
```

El entorno lógico `lab` se resuelve al clúster MNG. El catálogo del runtime solo
habilita `mng01`; `mng02` no se ofrecerá hasta validar su storage e imágenes.
El request no contiene nombres de clúster o nodo. `iac.cyberfuel.com` puede admitir PRX
cuando se configure y certifique; `dev-cyberfuel.com` queda limitado a
MNG.

Los rangos VMID son disjuntos y se fijan por manifiesto y archivo de entorno:

| Stack | Host configurado | Rango VMID |
| --- | --- | --- |
| `develop` | `dev-cyberfuel.com` | `40000–49999` |
| `production` | `iac.cyberfuel.com` | `30000–39999` |

Configura ambos límites explícitamente en cada archivo privado. El Compose de
Portainer y `iacpctl` rechazan valores ausentes o un rango asignado al entorno
equivocado. El Compose local usa por defecto el rango de desarrollo.

En el IACP Control Plane (VM 104), `IACP_WASABI_ENABLED=false` mantiene el
state local persistido para el laboratorio MNG, también en el stack cuyo host
es `iac.cyberfuel.com`. Con `false`, deja vacíos bucket, endpoint, región,
prefijo y credenciales en el mismo `/etc/iacp/stacks/prod.env`; `iacpctl`
rechaza una configuración contradictoria y no consulta Wasabi. Solo cuando el
servicio opere como producción real se cambia a `true`, se configura el bucket
`iacp-cyberfuel` y el prefijo `iacp/prod-runtime-v1`, y se cargan las
credenciales en ese archivo. El preflight de Wasabi se ejecuta únicamente con
el booleano en `true`.
La guía oficial de Terraform describe la configuración parcial del backend S3
y el lock file: [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3).
Wasabi recomienda path-style para sus solicitudes S3:
[Wasabi REST API](https://docs.wasabi.com/apidocs/rest-api-introduction).

En desarrollo configura `IACP_RUNTIME_STAGE=develop` y
`IACP_WASABI_ENABLED=false`. No añadas llaves Wasabi ni un prefijo S3 al stack
de desarrollo.

## Despliegue con imágenes precargadas

Si el registry no está disponible, conserva el archivo de imagen dentro del
release privado, verifica su SHA-256 y cárgalo con `docker load`. Antes de
iniciar servicios, `iacpctl deploy --preloaded-images` comprueba que cada
servicio del manifiesto (API, workers y PostgreSQL) tenga su imagen local y un
digest Docker válido. Después utiliza el mismo Compose canónico con
`--pull never`; si falta una imagen, el despliegue se detiene sin iniciar el
stack. La evidencia registra el servicio, referencia y digest de cada imagen.

## PRX permanece deshabilitado

La carpeta `terraform/environments/prx` contiene solo la estructura técnica;
su catálogo se entrega vacío a propósito. No publiques `production` en
`IACP_ENABLED_ENVIRONMENTS` hasta completar y revisar:

1. Endpoint HTTPS y credencial Proxmox de Platinum/PRX.
2. Pool, nodos en línea y storage permitido.
3. Golden Images y VMIDs de template confirmados para PRX.
4. Catálogo con planes e imágenes verificados.
5. Bucket, prefijo, permisos de lectura/escritura y locks.
6. Ensayos aislados con reconciliación comprobada.

La aplicación filtra el entorno si faltan estas configuraciones; el operador
debe revisar logs y la salida de `GET /v1/environments` antes de habilitarlo.

## Verificación local de configuración

Para inspeccionar el Compose local de desarrollo, configura `.env` con las
credenciales MNG locales y `IACP_VMID_MIN=40000` / `IACP_VMID_MAX=49999`.
`deploy/portainer/.env.example` contiene valores de referencia para producción;
no lo copies sin cambiar el rango al usarlo en desarrollo. Luego ejecuta:

```bash
docker compose config
docker compose up -d --build
curl --fail http://localhost:8080/health/ready
```

Para el manifiesto de Portainer usa los archivos de variables correspondientes
al stack y `docker compose -f deploy/portainer/docker-compose.yml config`.
Estas comprobaciones no ejecutan un despliegue ni crean máquinas virtuales.

El health check valida PostgreSQL y configuración de destino. Antes de una
prueba física se requiere identidad de prueba, cola y credenciales aisladas, y
un máximo de dos operaciones simultáneas. Mantén los servicios y volúmenes
anteriores fuera de este stack.

## Cambios futuros

1. Revisa los volúmenes, secretos, imagen, manifiesto y `origin/main`.
2. Ejecuta el protocolo de promoción vigente y registra la versión realmente
   ejecutada, esquema, workers, locks y colas.
3. Certifica los escenarios físicos nuevos de forma aislada y registra cada
   recuperación automática, manual o fail-closed.
4. Cambia rutas públicas solo tras verificar DNS, TLS y readiness del runtime.

No borres volúmenes, estados versionados, locks ni operaciones históricas para
forzar un resultado. El informe de producción vigente está en
[`auditorias/PRODUCCION_CONTROLPLANE_V1_20261102.md`](auditorias/PRODUCCION_CONTROLPLANE_V1_20261102.md).

## Ruta de desarrollo

`deploy/portainer/traefik-dev.yml` define una ruta dinámica separada para
`dev-cyberfuel.com`. La activación requiere que el certificado TLS incluya
`dev-cyberfuel.com` como SAN exacto. El certificado actual contiene
`*.cyberfuel.com` y `cyberfuel.com`, que no cubren ese dominio; tampoco se
resuelve DNS para `dev-cyberfuel.com` desde el entorno de despliegue. La ruta
permanece desactivada hasta configurar ambos. Cuando se active, reutiliza
la lista de IPs internas, sanitización de headers, rate limit y límite de
solicitudes; publica solo el alias `iacp-controlplane-api-dev`. La plantilla
permite el origen web de dev y mantiene el endpoint limitado a MNG por la
configuración del runtime.

Después de desplegar y verificar el stack de desarrollo, `activate-develop`
instala la plantilla atómicamente y confirma readiness y revisión servida. Si
la comprobación externa falla después de crear la ruta, el script retira esa
ruta. `disable-develop` retira únicamente el archivo que coincide exactamente
con la plantilla aprobada; ante una edición manual, se detiene y conserva el
archivo. Ambas acciones guardan evidencia privada. La ruta todavía no se ha
instalado en el IACP Control Plane (VM 104).
