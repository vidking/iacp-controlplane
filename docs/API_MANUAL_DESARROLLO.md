# Manual de consumo de la API IACP Control Plane

*Contrato HTTP `/v1` para integraciones, portales y servicios de backend*

El consumidor describe la KVM que necesita mediante un JSON estable. IACP
valida planes, imágenes y red, persiste la intención y encola el
aprovisionamiento. Una respuesta `202 Accepted` confirma que la operación fue
aceptada; no que la VM ya esté lista.

## 1. Contrato, destino y autenticación

El runtime proviene del repositorio `controlplanev2`, pero el producto y la
versión pública se llaman **IACP Control Plane v1**. No se publica una ruta
`/v2`.

El destino de Proxmox, el nodo, el pool, el rango de VMID y Terraform se
resuelven dentro del servidor. El consumidor no envía clúster, nodo ni
parámetros de infraestructura. En esta configuración, la API publica el perfil
de laboratorio MNG; el servidor decide el destino físico.

Si `IACP_REQUIRE_API_TOKEN` está habilitado, cada ruta `/v1` requiere:

```http
Authorization: Bearer <token>
```

Guarda el token y las contraseñas en el backend de tu integración. No los
incluyas en JavaScript público, URLs, logs ni repositorios.

## 2. Consultar catálogos

Carga los catálogos al iniciar la integración y cuando se abra el formulario.
Así el consumidor siempre usa identificadores publicados por el servidor.

```bash
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/plans"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/images"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/network-profiles"
```

La API no ofrece una ruta `/v1/environments`; tampoco acepta `environment` en
el cuerpo de aprovisionamiento ni como selector de catálogo. `GET /v1/plans`,
`GET /v1/images` y `GET /v1/network-profiles` son las rutas de catálogo.

Los planes e imágenes disponibles dependen del catálogo instalado. El perfil
`lab_dhcp` está disponible para DHCP. `prod_static` se publica deshabilitado
hasta que el IPAM autorizado esté configurado. El consumidor elige el perfil,
pero nunca envía IP, prefijo, gateway, VLAN ni una selección de nodo.

## 3. Estructura fija del request

`POST /v1/instances` acepta este JSON. Los campos desconocidos se rechazan:

```json
{
  "name": "kvm-cl-105",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
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

| Campo | Tipo | Regla |
| --- | --- | --- |
| `name` | string | Obligatorio; de 1 a 63 caracteres, minúsculas, números y guiones; comienza y termina con letra o número. |
| `plan_id` | string | Obligatorio; ID publicado por `GET /v1/plans`. |
| `image_id` | string | Obligatorio; ID publicado por `GET /v1/images`. |
| `access.root_password` | string o `null` | Opcional; se valida con `IACP_ROOT_PASSWORD_MIN_LENGTH` y nunca se devuelve. |
| `access.ssh_public_keys` | array de strings | Opcional; acepta claves `ssh-ed25519`, `ssh-rsa` o `ecdsa-sha2-*`. Puede aplicarse una clave predeterminada del runtime. |
| `commercial_context` | objeto o `null` | Opcional. Si se envía, requiere `customer_id`, `customer_tag`, `order_id` y `product_id`. Solo aporta trazabilidad. |
| `network_profile` | string | `lab_dhcp` o `prod_static`; este último puede estar deshabilitado hasta configurar NetBox. |
| `tags` | array de strings | Opcional; etiquetas Proxmox adicionales. Cada valor cumple el formato descrito abajo. |
| `start_on_create` | boolean | Si es `true`, solicita iniciar la KVM al terminar; si es `false`, la deja apagada. |

El campo `environment` no forma parte del contrato. El servidor elige el
destino físico. Tampoco se aceptan `cluster`, `node`, `pool`, `vmid`, VLAN,
IP, gateway, credenciales Proxmox o datos de Terraform.

### Tags Proxmox

Envía los tags adicionales como una lista JSON:

```json
"tags": ["kvm-001", "postal", "smtp-relay"]
```

Cada tag debe usar minúsculas, números, guion o guion bajo, comenzar con letra
o número y tener hasta 63 caracteres. Los duplicados se eliminan conservando
el primer orden. `commercial_context.customer_tag` se aplica automáticamente
junto con los tags de `tags`; no hace falta duplicarlo dentro de esa lista.
El servidor no deriva etiquetas del nombre de la KVM.

Por ejemplo, con `customer_tag: "cl-105"` y `tags: ["kvm-001", "postal"]`,
Proxmox recibe `cl-105`, `kvm-001` y `postal`.

## 4. Crear y seguir una KVM

Envía una llave de idempotencia estable. Si hay un timeout de red, repite el
mismo cuerpo con la misma llave; no crees otra intención.

```bash
cat > request.json <<'JSON'
{
  "name": "kvm-cl-105",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
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
  "tags": ["kvm-001", "postal"],
  "start_on_create": true
}
JSON

curl --fail-with-body --silent --show-error \
  -X POST "${IACP_BASE_URL}/v1/instances" \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H 'Idempotency-Key: alta-cl-105-kvm-001' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

La respuesta `202` entrega los IDs para consultar el progreso:

```json
{
  "operation_id": "22222222-2222-4222-8222-222222222222",
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "job_id": "22222222-2222-4222-8222-222222222222",
  "idempotency_key": "alta-cl-105-kvm-001"
}
```

Consulta la operación hasta `completed`, `failed` o `uncertain`:

```bash
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/operations/22222222-2222-4222-8222-222222222222"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/instances/11111111-1111-4111-8111-111111111111"
```

`POST /v1/instances` ejecuta el aprovisionamiento en una sola intención; la
integración de consumo no necesita enviar un `apply` separado. Si la API
devuelve `uncertain`, inspecciona la operación y reconcilia antes de reintentar.

## 5. Inventario, red y ciclo de vida

| Método y ruta | Uso |
| --- | --- |
| `GET /v1/instances` | Lista instancias; admite `limit`, `offset`, `status`, `name` e `include_deleted`. |
| `GET /v1/instances/{instance_id}` | Consulta detalle, configuración y última operación. |
| `GET /v1/instances/{instance_id}/network` | Consulta IP observada por QEMU Guest Agent y asignación IPAM. |
| `GET /v1/instances/{instance_id}/power` | Consulta energía directamente en Proxmox. |
| `POST /v1/instances/{instance_id}/power/start` | Encola inicio. |
| `POST /v1/instances/{instance_id}/power/shutdown` | Encola apagado ordenado. |
| `POST /v1/instances/{instance_id}/power/reboot` | Encola reinicio. |
| `DELETE /v1/instances/{instance_id}` | Encola la eliminación administrada. |
| `POST /v1/instances/{instance_id}/reconcile` | Compara la intención incierta con el estado observado. |

Las solicitudes que cambian estado aceptan `Idempotency-Key`. Si la misma
llave se reutiliza con un cuerpo distinto, la API devuelve `409`. La IP
observada solo aparece cuando QEMU Guest Agent la reporta; `unavailable` no
equivale a asignación manual.

## 6. Operaciones y observabilidad

```http
GET /v1/operations?limit=50&offset=0
GET /v1/operations/{operation_id}
GET /v1/events?operation_id={operation_id}&sort=asc
GET /v1/workers
GET /v1/observability
```

`uncertain` indica que no fue posible confirmar el resultado físico; no lo
interpretes como una falla limpia. La reconciliación vuelve a observar el
estado y no borra Terraform state ni fuerza locks.

`POST /v1/operations` es el endpoint técnico explícito para operadores. Las
integraciones deben preferir `POST /v1/instances`, que valida plan e imagen y
encola la creación completa.

El Compose declara seis workers (`worker-1` a `worker-6`) y limita la ejecución
global a seis operaciones concurrentes. `/v1/workers` y `/v1/observability`
permiten revisar heartbeats, cola y operaciones activas.

## 7. Swagger y el formulario de documentación

`/docs` permite consultar los catálogos y completar el formulario. El portal
genera el JSON y el cURL con los mismos campos del contrato; la contraseña se
redacta en la vista previa. El modelo Pydantic genera el ejemplo de
`POST /v1/instances` que Swagger muestra en `/swagger`. Descarga el esquema
completo en `/openapi.json` o léelo en `/redoc`.
