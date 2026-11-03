# API IACP Control Plane v1

El servicio basado en el código de `controlplanev2` conserva el nombre IACP
Control Plane y publica el contrato HTTP `/v1`.

- `/docs`: guía operativa y formulario que genera el JSON y el cURL.
- `/swagger`: OpenAPI interactivo con ejemplos del modelo.
- `/redoc` y `/openapi.json`: vistas y contrato descargable.

## Autenticación y catálogos

Cuando `IACP_REQUIRE_API_TOKEN` está habilitado, envía el token en:

```http
Authorization: Bearer <token>
```

Consulta las opciones válidas antes de crear una VM:

```http
GET /v1/environments
GET /v1/plans
GET /v1/images
GET /v1/network-profiles
```

`GET /v1/environments` publica únicamente `lab` en este runtime. El servidor
resuelve ese entorno al clúster MNG y selecciona internamente un nodo. El cuerpo
no acepta selección de nodo, clúster, pool, VLAN, IP, gateway ni parámetros
Terraform.

## Crear una instancia

`POST /v1/instances` devuelve `202 Accepted` y encola el aprovisionamiento
completo. Este es el cuerpo fijo:

```json
{
  "name": "kvm-cl-105",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
  "environment": "lab",
  "access": {
    "root_password": "CAMBIAR_POR_SECRETO",
    "ssh_public_keys": []
  },
  "commercial_context": {
    "customer_id": "CL-105",
    "customer_tag": "cl-105",
    "order_id": "ORD-1001",
    "product_id": "KVM-2"
  },
  "network_profile": "lab_dhcp",
  "tags": ["kvm-001", "postal", "smtp-relay"],
  "start_on_create": true
}
```

`commercial_context` es opcional; si se envía, incluye sus cuatro campos. Su
`customer_tag` se agrega automáticamente a las etiquetas de Proxmox. `tags`
agrega cero o más etiquetas adicionales y no se deriva del nombre. Cada tag
debe usar minúsculas, números, guion o guion bajo, empezar con letra o número y
tener hasta 63 caracteres. Los duplicados se eliminan conservando el primer
orden.

`environment` acepta `lab` y, si se omite, usa el mismo valor predeterminado.
`name` admite minúsculas, números y guiones, entre 1 y 63 caracteres.
`plan_id`, `image_id` y `network_profile` deben existir en los catálogos.
`network_profile` admite `lab_dhcp` y `prod_static`; el segundo puede estar
deshabilitado hasta configurar el IPAM autorizado. No se envían IP ni gateway.
Campos adicionales se rechazan.

La contraseña se valida según `IACP_ROOT_PASSWORD_MIN_LENGTH` y nunca se
devuelve. Se puede usar `access.ssh_public_keys`; el runtime también puede
aportar una clave SSH predeterminada.

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$IACP_BASE_URL/v1/instances" \
  -H "Authorization: Bearer $IACP_API_TOKEN" \
  -H 'Idempotency-Key: alta-cl-105-kvm-001' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

La respuesta contiene `operation_id`, `job_id`, `instance_id`, `status`,
`command` y `action`. Consulta la operación hasta alcanzar un estado terminal:

```http
GET /v1/operations/{operation_id}
GET /v1/instances/{instance_id}
GET /v1/events?operation_id={operation_id}&sort=asc
```

Estados públicos de operación: `queued`, `running`, `completed`, `failed` y
`uncertain`. Un estado `uncertain` requiere observación y reconciliación antes
de repetir la intención.

## Inventario y ciclo de vida

| Método y ruta | Uso |
| --- | --- |
| `GET /v1/instances` | Lista con filtros y paginación. |
| `GET /v1/instances/{instance_id}` | Detalle de instancia administrada. |
| `GET /v1/instances/{instance_id}/network` | IP observada por QEMU Guest Agent e información de IPAM. |
| `GET /v1/instances/{instance_id}/power` | Consulta estado de energía en Proxmox. |
| `POST /v1/instances/{instance_id}/power/start` | Encola inicio. |
| `POST /v1/instances/{instance_id}/power/shutdown` | Encola apagado ordenado. |
| `POST /v1/instances/{instance_id}/power/reboot` | Encola reinicio. |
| `DELETE /v1/instances/{instance_id}` | Encola destroy de una VM administrada. |
| `POST /v1/instances/{instance_id}/reconcile` | Reconcilia el estado de una operación incierta. |

Las acciones asíncronas aceptan `Idempotency-Key`. Reutiliza la misma llave
solo para reintentar la misma intención con el mismo cuerpo. Una llave asociada
a otro cuerpo devuelve `409`.

## Operaciones y observabilidad

```http
GET /v1/operations?limit=50&offset=0
GET /v1/operations/{operation_id}
GET /v1/events?after=0&limit=100&sort=desc
GET /v1/workers
GET /v1/observability
```

`POST /v1/operations` conserva el flujo técnico explícito para operadores.
Acepta `environment: "lab"`; `stack` y la selección de nodo no forman parte
del contrato público. Las integraciones de consumo deben preferir
`POST /v1/instances`, que valida los catálogos y encola la creación como una
sola intención.

El runtime está configurado con seis workers y un máximo global de seis
operaciones concurrentes. Las vistas `/docs`, `/swagger` y el OpenAPI se
generan desde el contrato que ejecuta la API.
