# API oficial v1

La API de `iac.cyberfuel.com` utiliza una sola superficie pública: `/v1`.
Este es el contrato de aprovisionamiento de IACP Control Plane.

## Aprovisionamiento

```http
POST /v1/instances
Authorization: Bearer <TOKEN>
Idempotency-Key: alta-ORD-1001
Content-Type: application/json
```

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

`commercial_context` es trazabilidad opcional. Si se envía, debe contener sus
cuatro campos. `tags` es opcional, acepta etiquetas Proxmox en minúsculas y
números, guiones o guion bajo, y se deduplica. `customer_tag`, cuando existe,
se agrega a la lista efectiva antes de enviarla a Terraform.

La respuesta es `202 Accepted`:

```json
{
  "operation_id": "<uuid>",
  "job_id": "<el-mismo-uuid>",
  "instance_id": "<uuid>",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "idempotency_key": "alta-ORD-1001"
}
```

## Catálogo y seguimiento

- `GET /v1/plans`
- `GET /v1/images`
- `GET /v1/network-profiles`
- `GET /v1/instances`
- `GET /v1/instances/{instance_id}`
- `GET /v1/operations/{operation_id}`
- `GET /v1/events?operation_id={operation_id}`
- `GET /v1/observability`

Estados terminales de operación: `completed`, `failed` y `uncertain`. Un
`uncertain` no significa éxito ni fallo seguro; requiere reconciliación física.

## Lifecycle y recuperación

- `POST /v1/instances/{instance_id}/power/start`
- `POST /v1/instances/{instance_id}/power/shutdown`
- `POST /v1/instances/{instance_id}/power/reboot`
- `DELETE /v1/instances/{instance_id}`
- `POST /v1/instances/{instance_id}/reconcile`

Los workers mantienen heartbeat, limitan la concurrencia a dos operaciones
físicas y recuperan operaciones stale mediante observación. No se reejecuta una
mutación física sin evidencia de que el recurso no existe o de que la operación
anterior fue consumida.
