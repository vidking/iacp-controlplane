# Integración técnica de Desarrollo — contrato efectivo

Fecha: 25/10/2026, America/Costa_Rica. Fuente: GET HTTPS `/openapi.json` autenticado, OpenAPI 3.1.0. Este documento describe el runtime actual; las pruebas ampliadas siguen pendientes.

## Acceso y recorrido

Base oficial: `https://iac.cyberfuel.com`. Documentación protegida mediante HTTP Basic; API mediante Bearer opaco del tenant. Obtener credenciales por el canal autorizado; no incluirlas en tickets, capturas ni Git. `Authorize` recibe el token API; cerrar la autorización al terminar. El tenant procede de la identidad, no de un campo JSON.

Consultar planes, imágenes y perfiles antes de crear. `availability` es una observación, no una reserva. HTTP 202 admite una intención: conservar `instance_id`, `operation_id`, `job_id` e `Idempotency-Key`, y seguir la operación. La VM arrancada no implica Cloud-Init terminado ni SSH disponible.

## Endpoints extraídos del OpenAPI servido

| Método | Ruta | operationId | Respuestas declaradas |
|---|---|---|---|
| GET | `/v1/instances` | `listInstances` | 200, 401, 403, 503, 422 |
| POST | `/v1/instances` | `createInstance` | 202, 401, 403, 409, 503, 422 |
| GET | `/v1/instances/{instance_id}` | `getInstance` | 200, 401, 403, 503, 404, 422 |
| DELETE | `/v1/instances/{instance_id}` | `deleteDisposableInstance` | 202, 401, 403, 404, 409, 503, 422 |
| POST | `/v1/instances/{instance_id}/network/discover` | `discoverInstanceNetwork` | 202, 401, 403, 503, 404, 409, 422 |
| GET | `/v1/plans` | `listKVMPlans` | 200, 401, 403, 503 |
| GET | `/v1/images` | `listGoldenImages` | 200, 401, 403, 503 |
| GET | `/v1/operations` | `listOperations` | 200, 401, 403, 404, 503, 422 |
| GET | `/v1/operations/{operation_id}` | `getOperation` | 200, 401, 403, 404, 503, 422 |
| GET | `/v1/instances/{instance_id}/power` | `getInstancePower` | 200, 401, 403, 404, 409, 503, 422 |
| POST | `/v1/instances/{instance_id}/power/start` | `startInstancePower` | 202, 401, 403, 404, 409, 503, 422 |
| POST | `/v1/instances/{instance_id}/power/shutdown` | `shutdownInstancePower` | 202, 401, 403, 404, 409, 503, 422 |
| POST | `/v1/instances/{instance_id}/power/reboot` | `rebootInstancePower` | 202, 401, 403, 404, 409, 503, 422 |
| GET | `/v1/profiles` | `listAuthorizedProfiles` | 200, 401, 403, 404, 409, 503 |
| GET | `/v1/lab/usage` | `getLabUsage` | 200, 401, 403, 404, 409, 503 |
| POST | `/v1/instances/{instance_id}/reconcile` | `reconcileInstance` | 202, 401, 403, 404, 409, 503, 422 |

La tabla refleja códigos declarados por la aplicación. Un 429 adicional puede proceder del limitador de Traefik; no se añade ficticiamente al schema servido. Los endpoints de reconciliación requieren los permisos correspondientes; no son reintentos de creación indiscriminados.

## Campos de creación

| Campo | Obligatorio | Tipo o referencia | Límites del schema |
|---|---|---|---|
| `CommercialInstanceCreate.name` | Sí | `string` | `{"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"}` |
| `CommercialInstanceCreate.plan_id` | Sí | `string` | `{"enum": ["kvm-1", "kvm-2", "kvm-3", "kvm-4"]}` |
| `CommercialInstanceCreate.image_id` | Sí | `string` | `{"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9][a-z0-9._-]*$"}` |
| `CommercialInstanceCreate.network` | Sí | `[{'$ref': '#/components/schemas/DHCPNetwork'}, {'$ref': '#/components/schemas/StaticNetwork'}]` | `{}` |
| `CommercialInstanceCreate.access` | Sí | `#/components/schemas/CommercialAccess` | `{}` |
| `CommercialInstanceCreate.start_on_create` | No | `boolean` | `{"default": true}` |
| `CommercialInstanceCreate.commercial_context` | Sí | `#/components/schemas/OrderContext` | `{}` |
| `CommercialAccess.root_password` | Sí | `string` | `{"minLength": 16, "maxLength": 128, "writeOnly": true, "format": "password"}` |
| `CommercialAccess.ssh_public_keys` | No | `array` | `{}` |
| `OrderContext.order_id` | Sí | `string` | `{"minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9._:-]+$"}` |
| `OrderContext.product_id` | No | `[{'type': 'string', 'maxLength': 128, 'minLength': 1, 'pattern': '^[A-Za-z0-9._:-]+$'}, {'type': 'null'}]` | `{}` |
| `DHCPNetwork.profile_id` | Sí | `string` | `{"const": "lab-dhcp"}` |
| `StaticNetwork.profile_id` | Sí | `string` | `{"const": "wan-static"}` |
| `StaticNetwork.ipv4_address` | Sí | `string` | `{}` |
| `StaticNetwork.prefix_length` | Sí | `integer` | `{"minimum": 1.0, "maximum": 30.0}` |
| `StaticNetwork.gateway` | Sí | `string` | `{}` |

Los modelos comerciales rechazan campos adicionales. No enviar `vcpus`, `memory_mb`, `disk_gb`, `self_service_policy_id`, tenant, VMID, VLAN ni datastore libres. WAN tiene schema pero su ejecución sigue rechazada con `WAN_STATIC_EXECUTION_NOT_CERTIFIED`.

## Ejemplo ejecutable con valores del consumidor

Este cuerpo es ilustrativo. Sustituir nombre/pedido y contraseña por valores propios; verificar autorización actual. No ejecutar KVM 3/4 ni imágenes pendientes sobre el runtime actual.

```json
{
  "access": {
    "root_password": "CONTRASENA_ILUSTRATIVA_CAMBIAR",
    "ssh_public_keys": []
  },
  "commercial_context": {
    "order_id": "ORD-1002",
    "product_id": "CRM-KVM-2"
  },
  "image_id": "ubuntu-2404",
  "name": "cliente-kvm-2-001",
  "network": {
    "profile_id": "lab-dhcp"
  },
  "plan_id": "kvm-2",
  "start_on_create": true
}
```

Enviar `POST /v1/instances` con `Authorization: Bearer <token-autorizado>` e `Idempotency-Key: crm-pedido-unico-creacion`. Conservar misma clave y cuerpo tras un timeout; un cambio de intención requiere una clave distinta. No pegar una contraseña real en la documentación.

## Seguimiento y errores

Consultar `GET /v1/operations/{operation_id}` y `GET /v1/instances/{instance_id}` hasta conocer el resultado normalizado. Ante `uncertain`, no clonar otra VM: conservar identificadores y solicitar observación/reconciliación autorizada. Tras un start esperar DHCP, Guest Agent, Cloud-Init y verificación de acceso antes de reboot.

401: revisar identidad; 403: revisar autorización/tenant; 409: conflicto o cuota/estado, inspeccionar detalle; 422: corregir cuerpo; 503: dependencia o admisión no disponible. Respetar 429 del proxy con espera y la misma intención. No reintentar una mutación con claves nuevas para eludir un resultado incierto.

Lifecycle y DELETE también son asíncronos. Revisar el requestBody exacto en el snapshot; utilizar solo los permisos y recursos propios autorizados. Un 202 de DELETE no demuestra eliminación: esperar resultado terminal y conciliación de infraestructura/state.

## Trazabilidad del contrato

El archivo `endpoints-integracion.json` conserva parámetros, cuerpos y respuestas directamente extraídos. `openapi-runtime.json` congela la respuesta HTTP real; no se regeneró ni alteró el backend para este documento.
