# IACP Control Plane

IACP Control Plane aprovisiona y administra máquinas virtuales KVM con una API
HTTP pública **v1**, workers Python y Terraform. El código de este candidato
proviene del repositorio `controlplanev2`; ese nombre identifica su origen, no
una versión diferente del contrato. El servicio y la documentación mantienen
el nombre IACP Control Plane y las rutas `/v1`.

## Arquitectura

- FastAPI publica catálogo, aprovisionamiento, lifecycle, reconciliación y
  observabilidad.
- PostgreSQL 17 conserva operaciones, instancias, eventos, leases de VMID,
  workers e idempotencia.
- Dos workers como máximo reclaman operaciones desde una cola PostgreSQL; un
  lock transaccional mantiene el límite global de concurrencia en 2.
- Terraform administra el state de cada instancia. El runtime `production`
  usa Wasabi S3 en `iacp-cyberfuel` con el prefijo
  `iacp/prod-runtime-v1`; `develop` usa state local persistido
  en su volumen de trabajo.
- Los nodos, pools, credenciales, catálogos y directorios Terraform se resuelven
  en el servidor. El consumidor solo elige un entorno lógico publicado por la
  API.
- Los tags son una lista descriptiva de Proxmox. Se aplican los valores que el
  consumidor envía; no se agregan tags automáticamente.

La instancia IACP Control Plane (VM 104), la VM 106 y las Golden Images están
protegidas frente a aprovisionamiento, lifecycle y eliminación desde la API.
El candidato no importa registros ni VMs del runtime anterior.

## Entornos

| Runtime | URL planeada | Entornos publicados al inicio | State Terraform |
| --- | --- | --- | --- |
| `develop` | `https://dev.iac.cyberfuel.com` | `lab` → MNG; solo `mng01`; VMID `40000–49999` | Local en volumen persistente de desarrollo |
| `production` | `https://iac.cyberfuel.com` | `lab` → MNG; solo `mng01`; VMID `30000–39999`; PRX deshabilitado | Wasabi S3, prefijo `iacp/prod-runtime-v1` |

La URL y la promoción se configuran en despliegue. Este checkout no cambia DNS,
contenedores, bases de datos ni volúmenes de ningún runtime.

## Ejecución local

1. Crea `.env` para desarrollo con credenciales MNG, la IP verificada del
   endpoint Proxmox, una contraseña local de PostgreSQL y el rango
   `IACP_VMID_MIN=40000` / `IACP_VMID_MAX=49999`. El archivo
   `deploy/portainer/.env.example` sirve como referencia productiva y usa el
   rango `30000–39999`.
2. Levanta el stack local:

   ```bash
   docker compose up -d --build
   ```

3. Consulta `http://localhost:8080/health/ready`, abre `/docs` y autoriza con
   el token si `IACP_REQUIRE_API_TOKEN=1`.

El Compose local crea los volúmenes `iacp_controlplane_dev_postgres` y
`iacp_controlplane_dev_workdata`. No los compartas con producción. El stack
solo expone el puerto local y utiliza MNG. No ejecutes aprovisionamientos
físicos hasta tener autorización y credenciales de ensayo aisladas.

## Contrato de aprovisionamiento

Antes de crear una instancia, el consumidor consulta:

- `GET /v1/environments`
- `GET /v1/plans?environment=lab`
- `GET /v1/images?environment=lab`
- `GET /v1/network-profiles`

El request `POST /v1/instances` mantiene una estructura fija. `tags` admite
valores como `cl-105`, `kvm-001` y `postal`; puede incluir cero o varias
etiquetas. El servidor no deriva tags desde el nombre.

El portal `/docs` construye el JSON y cURL a partir del formulario. Swagger en
`/swagger` y OpenAPI en `/openapi.json` muestran el ejemplo generado desde el
mismo modelo del request.

La guía completa está en [`docs/API_MANUAL_DESARROLLO.md`](docs/API_MANUAL_DESARROLLO.md)
y el contrato resumido en [`docs/API.md`](docs/API.md).

## Despliegue y migración

El Compose de Portainer define volúmenes nuevos y exclusivos para PostgreSQL y
workspaces. El ejemplo de producción usa la imagen
`scm.cyberfuel.com/cyberfuel/iacp-controlplane`; sus secretos son marcadores y
deben configurarse en Portainer. No reutilices volúmenes, bases de datos ni
estados de otro entorno. El manual de consumo canónico es Markdown; el archivo
Word anterior se retiró porque describía un contrato obsoleto.

PRX se publica solo cuando endpoint, credenciales, pool, catálogo, Terraform y
backend pasan las validaciones; por ahora `IACP_PRX_ENABLED=0` y solo `lab`
está habilitado. El detalle del corte futuro, los bloqueos pendientes y las
evidencias requeridas están en
[`docs/MIGRACION_CONTROLPLANE.md`](docs/MIGRACION_CONTROLPLANE.md).

Consulta [`docs/PORTAINER.md`](docs/PORTAINER.md) para preparar los stacks de
desarrollo y producción. La preparación descrita allí no ejecuta ni aprueba un
despliegue.
