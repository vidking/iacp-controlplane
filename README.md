# IACP Control Plane V2

Control plane pequeño para ejecutar planes de Terraform en el nodo `mng01` del clúster MNG.

El proyecto es independiente del IACP Control Plane actual. No reemplaza ni modifica la VM 104.

## Principios

- Un repositorio y un Compose.
- Terraform vive bajo `terraform/`.
- La API recibe órdenes técnicas y las entrega a workers.
- No se consultan CRM, ownership ni permisos comerciales.
- SQLite y workers independientes; máximo dos operaciones simultáneas en MNG.
- Cambiar Terraform no requiere reconstruir la imagen.
- IaCoC observa operaciones, eventos y heartbeat mediante la API.
- No hay CRM, ownership, permisos comerciales, SHA de código ni validaciones
  externas para aceptar una orden: la API recibe `plan`, `apply` o `destroy` y
  el worker ejecuta Terraform.

## Inicio local

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8080/health/ready

# Prueba completamente local, sin Proxmox:
IACP_STACK=local IACP_NAME=local-demo ./scripts/plan.sh
```

En el host de desarrollo la API queda disponible en `dev.iac.cyberfuel.com`; el
puerto publicado se controla con `IACP_API_PORT`.

Para Portainer se usa el manifiesto separado
[`deploy/portainer/docker-compose.yml`](deploy/portainer/docker-compose.yml),
que consume una imagen del registry, no publica puertos y expone la API por
Traefik. El Compose standalone de la raíz conserva el flujo local con `build`
y montaje editable de Terraform.

Para ejecutar un apply se necesita el identificador de un plan:

```bash
IACP_PLAN_OPERATION_ID=<operation-id> ./scripts/apply.sh
```

Para destruir la instancia asociada al plan:

```bash
IACP_PLAN_OPERATION_ID=<operation-id> ./scripts/destroy.sh
```

El plan se copia a un workspace independiente en el volumen `iacp_data`. El
apply reutiliza ese workspace y su `plan.tfplan`; no depende de una revisión
Git ni vuelve a resolver una fuente distinta. El destroy usa el mismo state.

## MNG y ejecución en otro host

El entorno real incluido es el clúster MNG, nodo `mng01`, con VMIDs de
laboratorio `40000` a `49999`. La reserva consulta Proxmox y SQLite para evitar
colisiones; el límite normal es de dos operaciones simultáneas. La VM 104, VM
106 y las Golden Images 29000, 29001, 29010 y 29011 no son destinos de
aprovisionamiento.

El worker standalone puede usar un archivo fuera del repositorio con esta forma:

```json
{
  "PROXMOX_VE_ENDPOINT": "https://mng01.cyberfuel.com:8006/",
  "PROXMOX_VE_API_TOKEN": "usuario@pam!token=..."
}
```

Indícalo al arrancar Compose:

```bash
IACP_PROXMOX_CREDENTIALS_FILE=/ruta/segura/credentials.json \
IACP_DEFAULT_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" \
docker compose up -d --build
```

La API y los workers pueden vivir en cualquier host con Docker Compose. Solo se
requieren el repositorio, Docker, la conectividad HTTPS al Proxmox objetivo,
el archivo de credenciales y una clave pública SSH. El Compose monta
`terraform/` en solo lectura; cada operación crea su copia editable dentro del
volumen de trabajo.

El Stack de Portainer usa las variables `IACP_PROXMOX_ENDPOINT`,
`IACP_PROXMOX_API_TOKEN` e `IACP_DEFAULT_SSH_PUBLIC_KEY`; así no necesita
copiar código ni credenciales a carpetas del host. El flujo de publicación está
documentado en [`docs/PORTAINER.md`](docs/PORTAINER.md) y el contrato de la API
en [`docs/API.md`](docs/API.md).

## Flujo de una orden

```text
POST /v1/operations
        |
        v
SQLite: pending -> worker -> Terraform
        |                         |
        +-- heartbeat/events -----+
        |
        +-- plan: reserva VMID y guarda plan.tfplan
        +-- apply: usa el plan guardado
        +-- destroy: usa el state del mismo workspace
```

Endpoints de observación: `/v1/operations`, `/v1/instances/{id}`,
`/v1/events`, `/v1/workers` y `/v1/observability`.

## Archivos editables

| Cambio | Archivo |
| --- | --- |
| Variables de la VM MNG | `terraform/environments/mng01/terraform.tfvars` |
| Imágenes Golden y VMIDs de template | `terraform/environments/mng01/catalog.json` |
| CPU, memoria, red, discos y tags | `terraform/modules/kvm/main.tf` |
| Contrato de órdenes y validaciones técnicas | `api/models.py` y `api/service.py` |
| Ejecución Terraform y lifecycle | `worker/main.py` |
| Consulta de Proxmox | `worker/proxmox.py` |
| Reserva de VMID | `worker/allocation.py` |
| Heartbeat | `worker/heartbeat.py` |
| Imágenes, secretos, puertos y volúmenes | `compose.yaml` y `.env` |
| Stack Portainer y Traefik | `deploy/portainer/docker-compose.yml` |
| Publicación manual de imagen | `scripts/publish-image.sh` |

Para cambiar el comportamiento basta normalmente con editar el módulo
Terraform, el catálogo o `worker/main.py`. No se editan manifests dispersos ni
se actualiza un SHA de origen.

Los valores reales de Proxmox, tokens, claves SSH y estados no se guardan en Git.

## Gitflow

- `develop`: desarrollo diario.
- `release/*`: preparación de una versión.
- `main`: versión estable.

