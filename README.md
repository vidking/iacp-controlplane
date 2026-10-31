# IACP Control Plane V2

Control plane pequeño para ejecutar Terraform sobre el clúster MNG (`mng01`).

El proyecto es independiente del IACP Control Plane actual. No reemplaza ni modifica la VM 104.

## Principios

- Un repositorio y un Compose.
- Terraform vive bajo `terraform/`.
- La API pública recibe `plan_id` e `image_id` estables y entrega una operación
  transparente a los workers.
- `customer_id`, `customer_tag`, `order_id` y `product_id` son trazabilidad; no
  conceden permisos ni cambian el plan de infraestructura.
- SQLite y workers independientes; el Stack de Portainer usa seis workers y seis operaciones simultáneas en MNG.
- Cambiar Terraform no requiere reconstruir la imagen.
- IaCoC observa operaciones, eventos y heartbeat mediante la API.
- El consumidor no selecciona nodo ni construye Terraform: el worker ejecuta
  sobre el nodo MNG certificado y mantiene plan + apply en una operación.

## Inicio local

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8080/health/ready

# Crear una instancia pública; el script usa kvm-2 y Ubuntu 24.04 por defecto:
IACP_NAME=demo-mng ./scripts/plan.sh
```

En el host de desarrollo la API queda disponible en `dev-iac.cyberfuel.com`; el
puerto publicado se controla con `IACP_API_PORT`.

Para Portainer se usa el manifiesto separado
[`deploy/portainer/docker-compose.yml`](deploy/portainer/docker-compose.yml),
que consume una imagen del registry, no publica puertos y expone la API por
Traefik. El Compose standalone de la raíz conserva el flujo local con `build`
y montaje editable de Terraform.

El flujo público no requiere un apply separado. `scripts/apply.sh` y
`scripts/destroy.sh` se conservan para operadores que trabajan directamente
con `POST /v1/operations` y un workspace ya creado:

```bash
IACP_PLAN_OPERATION_ID=<operation-id> ./scripts/apply.sh
```

Para destruir una instancia por el contrato público:

```bash
IACP_INSTANCE_ID=<instance-id> ./scripts/destroy.sh
```

El worker copia Terraform a un workspace independiente en el volumen
`iacp_data`; el apply interno reutiliza el plan y el state de esa operación.

## MNG y ejecución en otro host

El entorno real incluido es el clúster MNG, con nodo `mng01` y VMIDs de
laboratorio `40001` a `49999`; el VMID `40000` queda reservado. La reserva consulta Proxmox y SQLite
para evitar colisiones y reutiliza el menor VMID físicamente libre después de un destroy confirmado.
El Stack de Portainer mantiene seis workers explícitos y limita a seis las operaciones simultáneas.
La VM 104, VM 106 y las Golden Images 29000, 29001, 29010 y 29011 no son
destinos de aprovisionamiento.

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
el archivo de credenciales y, si se desea omitir la clave en cada request, una
clave pública SSH por defecto. El Compose monta
`terraform/` en solo lectura; cada operación crea su copia editable dentro del
volumen de trabajo.

El Stack de Portainer usa las variables `IACP_PROXMOX_ENDPOINT` y
`IACP_PROXMOX_API_TOKEN`; `IACP_DEFAULT_SSH_PUBLIC_KEY` es opcional y sirve
como clave por defecto cuando el request no trae una. Así no necesita
copiar código ni credenciales a carpetas del host. El flujo de publicación está
documentado en [`docs/PORTAINER.md`](docs/PORTAINER.md) y el contrato de la API
en [`docs/API.md`](docs/API.md). El manual de consumo para desarrollo está en
[`docs/API_MANUAL_DESARROLLO.md`](docs/API_MANUAL_DESARROLLO.md).

## IPAM NetBox y red estática

El perfil público por defecto es `lab_dhcp`. La integración preparada para
producción usa `prod_static` y consulta NetBox en
`https://netbox.cyberfuel.com/api` (su endpoint de salud es
`/api/status/`). El consumidor solo envía `network_profile`; el worker reserva
la primera IP libre del prefijo `/24` autorizado, mantiene `.1` como gateway,
registra el nombre/cliente/VMID en la IP y libera el objeto al destruir la
KVM.

El perfil permanece desactivado hasta configurar, en el entorno de despliegue,
`IACP_PROD_STATIC_ENABLED=1`, `IACP_NETBOX_PREFIX_ID` con el ID exacto del
prefijo, `IACP_NETBOX_TOKEN` como secreto y las etiquetas `ocupada` y
`reservada`. `IACP_NETBOX_VERIFY_TLS=1` es el valor recomendado; si la cadena
interna no está en la confianza del contenedor, montar la CA y usar
`IACP_NETBOX_CA_FILE`. No poner tokens reales en `.env`, Git ni tickets.

## Flujo de una orden

```text
POST /v1/instances
        |
        v
SQLite: pending -> worker -> Terraform
        |                         |
        +-- heartbeat/events -----+
        |
        +-- plan + apply: reserva VMID y crea la instancia
        +-- destroy: usa el state del mismo workspace
```

Endpoints principales: `/v1/instances`, `/v1/instances/{id}`,
`DELETE /v1/instances/{id}` y
`/v1/instances/{id}/power/{start|shutdown|reboot}`. Observación:
`/v1/operations`, `/v1/events`, `/v1/workers` y `/v1/observability`.
Para una operación físicamente ambigua existe
`POST /v1/instances/{id}/reconcile`; reobserva y recupera el estado sin
reiniciar la VM.

El portal visual de referencia y consola técnica está disponible en `/docs`;
`/console` conserva un alias compatible. Swagger técnico queda en `/swagger`,
ReDoc en `/redoc` y el contrato descargable en `/openapi.json`.
Power Lifecycle aparece en el contrato aunque el token no tenga todavía
`VM.PowerMgmt`: en ese caso la operación termina con
`POWER_PERMISSION_DENIED`. El V2 utiliza el mismo endpoint y token de
Provisioning de Proxmox; no consulta la API antigua de Lifecycle.

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
| Portal visual, consola y guías | `api/static/docs/` |

Para cambiar el comportamiento basta normalmente con editar el módulo
Terraform, el catálogo o `worker/main.py`. No se editan manifests dispersos ni
se actualiza un SHA de origen.

Los valores reales de Proxmox, tokens, claves SSH y estados no se guardan en Git.

## Gitflow

- `develop`: desarrollo diario.
- `release/*`: preparación de una versión.
- `main`: versión estable.

