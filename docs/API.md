# API IACP Control Plane v1

Esta referencia resume el contrato HTTP público. La referencia interactiva y
el OpenAPI vigente se generan desde la aplicación:

- `/docs`: guía y consola visual; genera el JSON y cURL desde el formulario.
- `/swagger`: Swagger UI con ejemplos de request del esquema Pydantic.
- `/redoc`: referencia de lectura.
- `/openapi.json`: contrato descargable.

Las rutas operativas conservan el prefijo `/v1`.

## Autenticación y catálogo

Las rutas `/v1` requieren `Authorization: Bearer <token>` cuando el runtime
tiene activado `IACP_REQUIRE_API_TOKEN`.

Consulta primero los destinos lógicos y catálogos autorizados:

```http
GET /v1/environments
GET /v1/plans?environment=lab
GET /v1/images?environment=lab
GET /v1/network-profiles
```

`environment` es opcional al consultar planes e imágenes. En las solicitudes
de aprovisionamiento puede omitirse para usar el default publicado. El
consumidor no envía clúster, nodo, pool, VLAN, IP, gateway ni parámetros de
Terraform.

## Crear una máquina virtual

`POST /v1/instances` devuelve `202 Accepted`. El contrato tiene campos
estables: nombre, entorno lógico, plan, imagen, perfil de red, tags, acceso
inicial y estado de encendido.

```json
{
  "name": "cliente-cl-105",
  "environment": "lab",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
  "network_profile": "lab_dhcp",
  "tags": ["cl-105", "kvm-001", "postal"],
  "access": {
    "root_password": "CAMBIAR_POR_SECRETO",
    "ssh_public_keys": []
  },
  "start_on_create": true
}
```

Las etiquetas son opcionales y se envían como una lista de strings. Si la
lista está vacía, no se aplican tags. El orquestador no infiere `cl-xxx`,
`kvm-xxx` ni ningún tag desde `name`.

`name` admite minúsculas, números y guiones, con longitud de 1 a 63 caracteres.
Los IDs de plan e imagen deben aparecer en el catálogo del entorno elegido.
`network_profile` solo admite `lab_dhcp`; no se acepta configuración de IP
estática.

La contraseña inicial se entrega mediante `access.root_password`; también se
puede enviar una o varias claves en `access.ssh_public_keys`. El secreto no se
devuelve en las respuestas. Configura al menos una de esas alternativas o una
clave SSH por defecto en el runtime.

Ejemplo de envío:

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$IACP_BASE_URL/v1/instances" \
  -H "Authorization: Bearer $IACP_API_TOKEN" \
  -H 'Idempotency-Key: alta-cl-105-kvm-001' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

Respuesta abreviada:

```json
{
  "operation_id": "<uuid>",
  "job_id": "<mismo-uuid>",
  "instance_id": "<uuid>",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "environment": "lab",
  "idempotency_key": "alta-cl-105-kvm-001"
}
```

## Seguimiento y recuperación

Consulta `GET /v1/operations/{operation_id}` hasta `completed`, `failed` o
`uncertain`. `job_id` es un alias de `operation_id`. Los estados `uncertain`
requieren verificación física; no significan que la mutación haya fallado.

```http
GET /v1/operations/{operation_id}
GET /v1/instances/{instance_id}
GET /v1/events?operation_id={operation_id}&sort=asc
POST /v1/instances/{instance_id}/reconcile
```

El worker observa Proxmox antes de volver a intentar una mutación con resultado
ambiguo. Si no puede confirmar el estado, conserva la operación como incierta
y programa reconciliación. Reutiliza la misma `Idempotency-Key` al reintentar
una intención; si se usa la misma llave con otro cuerpo, la API responde `409`.
Cuando se llena la cola, responde `429` y `Retry-After`.

## Lifecycle, red y eliminación

```http
GET    /v1/instances/{instance_id}/power
POST   /v1/instances/{instance_id}/power/start
POST   /v1/instances/{instance_id}/power/shutdown
POST   /v1/instances/{instance_id}/power/reboot
GET    /v1/instances/{instance_id}/network
DELETE /v1/instances/{instance_id}
```

Las mutaciones son asíncronas. Para eliminar, la VM debe aparecer apagada; la
API reutiliza el workspace y state originales. La respuesta de red informa
direcciones observadas por QEMU Guest Agent, sin presentar DHCP como una
reserva o conectividad confirmada.

La VM 104, VM 106 y las imágenes base protegidas no pueden modificarse desde
estas rutas.

## Operación y diagnóstico

`POST /v1/operations` conserva el flujo explícito para operadores que necesitan
`plan`, `apply`, `destroy` o una reconciliación manual. La creación normal de
consumidores debe usar `POST /v1/instances`.

```http
GET /v1/instances
GET /v1/operations
GET /v1/workers
GET /v1/observability
GET /v1/events
```

Los errores son códigos estables y no exponen rutas internas, secretos ni
salida completa de Terraform. Consulta `/swagger` para modelos, parámetros,
ejemplos de request y respuestas publicados por el runtime.
