# API del IACP

Para el manual completo de integración y desarrollo, consultar
[`API_MANUAL_DESARROLLO.md`](API_MANUAL_DESARROLLO.md). Este archivo conserva
el contrato resumido de la API pública.

La documentación interactiva está disponible en:

- `https://iac.cyberfuel.com/docs`
- `https://iac.cyberfuel.com/swagger` (Swagger técnico)
- `https://iac.cyberfuel.com/redoc`
- `https://iac.cyberfuel.com/openapi.json`
- `https://iac.cyberfuel.com/console` (alias compatible de `/docs#console`)

`/docs` es el portal visual de referencia y contiene la consola de creación,
inventario, operaciones, lifecycle, eliminación autorizada y guías. Para la referencia OpenAPI clásica se usa `/swagger` o
`/redoc`. Durante la creación, el portal genera el JSON del contrato y el
cURL equivalente con secretos redactados. En Portainer, usar el botón `Authorize` de Swagger e introducir el
valor de `IACP_API_TOKEN`. La API agrega el esquema `Bearer` automáticamente.

## Contrato estable v1

El consumidor público selecciona identificadores del catálogo y trabaja con
los contratos JSON publicados por la API.

Los planes y las Golden Images se consultan, no se redefinen en cada request:

- `GET /v1/plans`: devuelve `kvm-1` a `kvm-4` con sus vCPU, memoria y disco publicados.
- `GET /v1/images`: devuelve `ubuntu-2404`, `ubuntu-2604`, `almalinux-9` y
  `almalinux-10` con sus datos publicados.
- `GET /v1/network-profiles`: devuelve `lab_dhcp` y, cuando está configurado,
  `prod_static`; el consumidor selecciona el perfil, no una IP arbitraria.

### Crear una instancia

`POST /v1/instances` procesa el aprovisionamiento como una sola operación
asíncrona. `Idempotency-Key` es opcional; si se omite, la API genera una llave
de intención y la devuelve. Para reintentos seguros, el consumidor puede
enviar una llave propia y reutilizarla con el mismo cuerpo; el API devuelve la
misma respuesta. Reutilizarla con otro cuerpo devuelve `409`.

```http
POST /v1/instances
Authorization: Bearer <token>
Idempotency-Key: alta-ORD-1001
Content-Type: application/json
```

```json
{
  "name": "cliente-cl-0000",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
  "network_profile": "lab_dhcp",
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
  "start_on_create": true
}
```

`customer_id` es metadata de trazabilidad. `customer_tag` se conserva como
referencia de negocio.
El bloque comercial es opcional, pero si se envía debe contener los cuatro
campos. La contraseña se utiliza solo durante el aprovisionamiento; no se
persiste en el payload ni se devuelve por la API. La cuenta inicial es
`root` para Ubuntu y AlmaLinux.
En Portainer, `IACP_ROOT_PASSWORD_MIN_LENGTH` establece la longitud mínima
(12 por defecto); una contraseña que no cumple se rechaza con `422` antes de
crear la operación.

`network_profile=lab_dhcp` usa la red de laboratorio. `network_profile=prod_static`
solo se habilita cuando el despliegue tiene configurado el prefijo `/24`
autorizado en NetBox; el worker reserva una IP libre, usa `x.x.x.1` como
gateway y registra la asignación. El consumidor no envía `ipv4_cidr`,
`ipv4_gateway` ni `prefix_id`.

La respuesta es `202 Accepted`:

```json
{
  "operation_id": "<uuid>",
  "job_id": "<el-mismo-uuid>",
  "instance_id": "<uuid>",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "idempotency_key": "<generada-o-enviada>"
}
```

Consultar `GET /v1/operations/{operation_id}` hasta que `status` sea
`completed`, `failed` o `uncertain`. La instancia se observa con
`GET /v1/instances/{instance_id}`. Los estados públicos de operación son
`queued`, `running`, `completed`, `failed` y `uncertain`; los de instancia
son `pending`, `provisioning`, `ready`, `deleting`, `failed`, `uncertain` y
`deleted`.
Si la cola alcanza `IACP_MAX_QUEUE_DEPTH`, las nuevas mutaciones responden
`429` con `Retry-After`; el consumidor debe conservar la misma intención y
reintentar después.

En la respuesta actual, `job_id` es un alias de `operation_id` y ambos
contienen el mismo UUID. Para consultar el progreso, utilizar `operation_id`.
La respuesta también incluye `phase`, `last_heartbeat`,
`heartbeat_age_seconds` y `heartbeat_stale` cuando la operación ya fue tomada
por un worker.

### Red e IP observada

`GET /v1/instances/{instance_id}/network` separa la configuración declarada
de la conectividad observada. `observed_ipv4` solo se llena con direcciones
confirmadas por la API; si no hay una dirección disponible, el API devuelve
`status=unavailable` y no presenta el CIDR configurado como una IP confirmada.
Para `prod_static`, `network_allocation` informa `provider=netbox`, `address`,
`gateway` y el estado de la reserva. Al destruir la VM, la IP se libera y el
objeto vuelve a estar disponible en NetBox; si la liberación no puede
confirmarse, la operación queda `uncertain`.

## Power y destrucción

Las rutas disponibles son:

- `GET /v1/instances/{instance_id}/power`
- `POST /v1/instances/{instance_id}/power/start`
- `POST /v1/instances/{instance_id}/power/shutdown`
- `POST /v1/instances/{instance_id}/power/reboot`
- `DELETE /v1/instances/{instance_id}`

Las cuatro mutaciones aceptan `Idempotency-Key` opcional y se ejecutan de forma
asíncrona. Si una acción no puede ejecutarse, la operación termina como
`failed` con un `error_code`, por ejemplo `POWER_PERMISSION_DENIED`; nunca se
reporta éxito falso.
Los reintentos del consumidor deben reutilizar la misma `Idempotency-Key`.
Después de una pérdida de respuesta en una mutación física, el worker observa
Proxmox antes de repetirla; si no puede confirmar el resultado, termina en
`uncertain` y encola una reconciliación segura.

## Operación interna

Se conserva `POST /v1/operations` para integraciones existentes que
necesiten enviar explícitamente `plan`, `apply`, `destroy` o `power`. Este
endpoint no es necesario para el consumo público estable de `/v1/instances`.
