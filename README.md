# IACP Control Plane

Runtime de destino para `iac.cyberfuel.com`. El nombre del repositorio no crea
una segunda versión: la única API pública es `/v1`. PostgreSQL es la única
persistencia y dos workers limitan a dos las mutaciones físicas simultáneas.

## Principios operativos

- El consumidor selecciona `plan_id`, `image_id`, `network_profile`, acceso,
  contexto comercial opcional y una lista flexible de `tags`.
- `customer_tag` y `tags` se validan con el formato de etiquetas de Proxmox,
  se deduplican y terminan en el recurso KVM como metadata; no conceden
  permisos ni seleccionan nodo, VMID o almacenamiento.
- `POST /v1/instances` encola plan y apply como una operación asíncrona e
  idempotente. Nunca espera Terraform dentro de la petición HTTP.
- Una mutación física que pierde confirmación queda `uncertain`. El worker
  observa Proxmox y el state antes de reconciliar; no repite a ciegas ni fuerza
  locks.
- La base nueva se crea vacía. No se importan operaciones, leases, secretos,
  Terraform state ni VMs del runtime anterior.

## Arranque local

```bash
cp .env.example .env
# Define POSTGRES_PASSWORD e IACP_DATABASE_URL en .env.
docker compose up -d --build
curl http://localhost:8080/health/ready
```

Para una prueba local con el Compose incluido, `IACP_DATABASE_URL` puede ser:

```text
postgresql://iacp:CONTRASENA@postgres:5432/iacp_controlplane
```

El Compose contiene PostgreSQL, el API y dos workers. El volumen de PostgreSQL
es nuevo para esta línea y el volumen de trabajo solo contiene planes efímeros,
secretos temporales y workspaces de Terraform. Nunca se debe ejecutar
`docker compose down -v` sobre un entorno con datos que deban conservarse.

## Contrato oficial `/v1`

La documentación interactiva y el ejemplo JSON generado están disponibles en:

- `https://iac.cyberfuel.com/docs`
- `https://iac.cyberfuel.com/swagger`
- `https://iac.cyberfuel.com/redoc`
- `https://iac.cyberfuel.com/openapi.json`

Swagger permite autorizarse con Bearer, ejecutar `GET /v1/plans` y probar
`POST /v1/instances`. Su formulario genera la vista previa del mismo JSON que
se enviará, incluidos los tags adicionales.

Ejemplo de creación:

```json
{
  "name": "cliente-cl-0000",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
  "access": {
    "root_password": "CAMBIAR_POR_SECRETO",
    "ssh_public_keys": []
  },
  "commercial_context": {
    "customer_id": "CL-0000",
    "customer_tag": "cl-0000",
    "order_id": "ORD-1001",
    "product_id": "KVM-2"
  },
  "network_profile": "lab_dhcp",
  "start_on_create": true,
  "tags": ["kvm-cliente-cl-0000", "smtp-relay"]
}
```

La contraseña solo se entrega por HTTPS y no se devuelve ni se almacena en
PostgreSQL. La respuesta `202` devuelve `operation_id`, `job_id` e
`instance_id`; el consumidor debe seguir `/v1/operations/{operation_id}` hasta
`completed`, `failed` o `uncertain`.

## Reconciliación y seguridad

`POST /v1/instances/{instance_id}/reconcile` reobserva una operación incierta
con la misma identidad y el mismo state. Las operaciones stale crean una
revisión de recuperación; el worker conserva la intención original, registra
eventos y queda fail-closed si no existe evidencia suficiente.

La VM 104 (IACP Control Plane) está protegida contra aprovisionamiento,
Lifecycle y eliminación desde la API. La VM 106 y la VM 29000 también están
protegidas. Las pruebas físicas se ejecutan únicamente sobre el clúster MNG,
con nodo explícito y máximo dos aprovisionamientos simultáneos. Platinum/PRX
queda fuera de estos ensayos.

## Desarrollo

- `develop`: integración diaria de la API v1.
- `main`: línea estable después de las pruebas y la promoción controlada.
- `api/`: contrato HTTP, PostgreSQL, catálogos y portal de documentación.
- `worker/`: cola, Terraform, Proxmox, leases, heartbeat y reconciliación.
- `terraform/`: módulos y catálogo del clúster MNG.

Antes de promocionar se debe verificar `origin/main`, imagen realmente
ejecutada, servicios, esquema PostgreSQL, operaciones, colas y locks. La
integración del código no equivale a un GO de producción.
