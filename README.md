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

## Inicio local

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8080/health/ready
./scripts/plan.sh
```

En el host de desarrollo la API queda disponible en `dev.iac.cyberfuel.com`; el
puerto publicado se controla con `IACP_API_PORT`.

Para ejecutar un apply se necesita el identificador de un plan:

```bash
IACP_PLAN_OPERATION_ID=<operation-id> ./scripts/apply.sh
```

## Archivos editables

| Cambio | Archivo |
| --- | --- |
| Variables de la VM MNG | `terraform/environments/mng01/terraform.tfvars` |
| CPU, memoria, red, discos y tags | `terraform/modules/kvm/main.tf` |
| Entrada de órdenes | `api/main.py` |
| Ejecución Terraform | `worker/main.py` |
| Heartbeat | `worker/heartbeat.py` |
| Imágenes, puertos y volúmenes | `compose.yaml` |

Los valores reales de Proxmox, tokens, claves SSH y estados no se guardan en Git.

## Gitflow

- `develop`: desarrollo diario.
- `release/*`: preparación de una versión.
- `main`: versión estable.

