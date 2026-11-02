# Preparación de stacks Portainer

Este documento describe los manifiestos del candidato. No confirma promoción,
no cambia el IACP Control Plane (VM 104) y no autoriza un corte de dominio.

## Manifiestos y separación

| Stack | Manifiesto | Imagen | Destino inicial | Persistencia |
| --- | --- | --- | --- | --- |
| Desarrollo | `deploy/portainer/docker-compose.yml` con variables de desarrollo | tag `develop` | MNG, entorno lógico `lab` | PostgreSQL y workdata propios; state Terraform local |
| Producción | `deploy/portainer/docker-compose.yml` con variables de producción | tag aprobado | MNG, entorno lógico `lab`; PRX apagado | PostgreSQL y workdata propios; Wasabi `iacp/prod-runtime-v1` |

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

El entorno lógico `lab` se resuelve a MNG. El catálogo de este candidato solo
habilita `mng01`; `mng02` no se ofrecerá hasta validar su storage e imágenes.
El request no contiene nombres de clúster o nodo. `iac.cyberfuel.com` puede admitir PRX
cuando se configure y certifique; `dev.iac.cyberfuel.com` queda limitado a
MNG.

Los rangos VMID son disjuntos y se fijan por manifiesto y archivo de entorno:

| Stack | Host configurado | Rango VMID |
| --- | --- | --- |
| `develop` | `dev.iac.cyberfuel.com` | `40000–49999` |
| `production` | `iac.cyberfuel.com` | `30000–39999` |

Configura ambos límites explícitamente en cada archivo privado. El Compose de
Portainer y `iacpctl` rechazan valores ausentes o un rango asignado al entorno
equivocado. El Compose local usa por defecto el rango de desarrollo.

En el runtime productivo configura `IACP_RUNTIME_STAGE=production`,
`IACP_TERRAFORM_BACKEND=s3`, el bucket `iacp-cyberfuel` y el prefijo
exacto `iacp/prod-runtime-v1`. Las llaves `IACP_WASABI_ACCESS_KEY_ID` y
`IACP_WASABI_SECRET_ACCESS_KEY` se entregan solo a los workers para operar el
backend. El endpoint se configura con HTTPS y path-style para Wasabi; Terraform
usa un lock file S3. Revisa permisos y versionado del bucket antes del corte.
El bucket se renombró a `iacp-cyberfuel`. El acceso actual de runtime recibe
HTTP 403, por lo que no se debe ejecutar `deploy` ni aprovisionar hasta que la
política permita operar el prefijo productivo. Se autorizó borrar todos los
objetos y todas sus versiones del bucket, sin respaldo; esa purga requiere una
credencial administrativa y no se ha ejecutado. Tras la purga, limita la llave
de runtime a `iacp/prod-runtime-v1`. El `preflight` firma una consulta
`ListObjectsV2` limitada a ese prefijo y no escribe objetos; el piloto físico
debe certificar aparte la escritura, el lock S3 y su reconciliación.
La guía oficial de Terraform describe la configuración parcial del backend S3
y el lock file: [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3).
Wasabi recomienda path-style para sus solicitudes S3:
[Wasabi REST API](https://docs.wasabi.com/apidocs/rest-api-introduction).

En desarrollo configura `IACP_RUNTIME_STAGE=develop` y
`IACP_TERRAFORM_BACKEND=local`. No añadas llaves Wasabi ni un prefijo S3 al
stack de desarrollo.

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

## Promoción futura

1. Revisa los volúmenes nuevos, secretos, imagen, manifiesto y `origin/main`.
2. Ejecuta el protocolo de promoción vigente y registra la versión realmente
   ejecutada, esquema, workers, locks y colas.
3. Certifica el piloto técnico y, por separado, cada escenario crítico de
   recuperación.
4. Cambia el endpoint público solo después de un dictamen integral aprobado.

No borres volúmenes, estados versionados, locks ni operaciones históricas para
forzar un resultado. Los gates pendientes y el procedimiento del corte están
en [`MIGRACION_CONTROLPLANE.md`](MIGRACION_CONTROLPLANE.md).

## Ruta de desarrollo

`deploy/portainer/traefik-dev.yml` define una ruta dinámica separada para
`dev.iac.cyberfuel.com`. La activación requiere que el certificado TLS tenga
`dev.iac.cyberfuel.com` o `*.iac.cyberfuel.com` en SAN; el certificado actual
solo incluye `*.cyberfuel.com` y no cubre el subdominio anidado. La ruta reutiliza
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
