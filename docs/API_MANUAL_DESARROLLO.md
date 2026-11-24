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

Cada ruta `/v1` requiere el Bearer de servicio configurado en el backend del
portal:

```http
Authorization: Bearer <token>
```

El portal inyecta este Bearer desde su backend. Los clientes finales usan el
portal y no reciben el token ni llaman directamente a esta API. Conserva el
token y las contraseñas en el backend; no los incluyas en JavaScript público,
URLs, logs ni repositorios.

## 2. Consultar catálogos

Carga los catálogos al iniciar la integración y cuando se abra el formulario.
Así el consumidor siempre usa identificadores publicados por el servidor.

```bash
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/environments"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/plans"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/images"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/network-profiles"
```

`GET /v1/environments` devuelve únicamente `lab`. En `POST /v1/instances`,
envía `"environment": "lab"`; el servidor resuelve MNG y elige internamente
el nodo. El JSON no permite seleccionar `mng01` ni `mng02`.

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

| Campo | Tipo | Regla |
| --- | --- | --- |
| `name` | string | Obligatorio; de 1 a 63 caracteres, minúsculas, números y guiones; comienza y termina con letra o número. |
| `plan_id` | string | Obligatorio; ID publicado por `GET /v1/plans`. |
| `image_id` | string | Obligatorio; ID publicado por `GET /v1/images`. |
| `environment` | string | `lab`; opcional por compatibilidad y predeterminado a `lab`. |
| `access.root_password` | string o `null` | Opcional; se valida con `IACP_ROOT_PASSWORD_MIN_LENGTH` y nunca se devuelve. |
| `access.ssh_public_keys` | array de strings | Opcional; acepta claves `ssh-ed25519`, `ssh-rsa` o `ecdsa-sha2-*`. Puede aplicarse una clave predeterminada del runtime. |
| `commercial_context` | objeto o `null` | Opcional. Si se envía, requiere `customer_id`, `customer_tag`, `order_id` y `product_id`. Solo aporta trazabilidad. |
| `network_profile` | string | `lab_dhcp` o `prod_static`; este último puede estar deshabilitado hasta configurar NetBox. |
| `tags` | array de strings | Opcional; etiquetas Proxmox adicionales. Cada valor cumple el formato descrito abajo. |
| `start_on_create` | boolean | Si es `true`, solicita iniciar la KVM al terminar; si es `false`, la deja apagada. |

El servidor resuelve el destino físico a partir de `environment: "lab"`.
Tampoco se aceptan `cluster`, `node`, `pool`, `vmid`, VLAN, IP, gateway,
credenciales Proxmox o datos de Terraform.

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
encola la creación completa. Si se usa el endpoint técnico, envía
`environment: "lab"`; el request no admite `stack` ni `node`. El servidor
resuelve el destino físico y conserva seis workers con un límite global de seis
operaciones concurrentes.

El Compose declara seis workers (`worker-1` a `worker-6`) y limita la ejecución
global a seis operaciones concurrentes. `/v1/workers` y `/v1/observability`
permiten revisar heartbeats, cola y operaciones activas.

## 7. Swagger y el formulario de documentación

`/docs` permite consultar los catálogos y completar el formulario. El portal
genera el JSON y el cURL con los mismos campos del contrato; la contraseña se
redacta en la vista previa. El modelo Pydantic genera el ejemplo de
`POST /v1/instances` que Swagger muestra en `/swagger`. Descarga el esquema
completo en `/openapi.json` o léelo en `/redoc`.

## 8. Crear y probar Nextcloud o n8n

Consulta primero `GET /v1/applications` y usa solo una aplicación habilitada.
Las pruebas actuales usan `lab_dhcp`, sin reserva NetBox ni IP pública. El
servicio requiere Ubuntu Server 24.04 (`ubuntu-2404`), una llave SSH del consumidor e
inicio automático. AlmaLinux y las demás imágenes se rechazan para nuevas
provisiones de servicios. Para TLS por IP, `configuration.access_mode` es `ip` y no se
envía `domain`; para el modo dominio se requiere que el FQDN ya resuelva hacia
la VM.

Guarda el cuerpo en un archivo privado, modifica el nombre en cada ensayo y
reemplaza la llave por una pública controlada por el operador:

```bash
umask 077
cat > service-request.json <<'JSON'
{
  "application_id": "n8n",
  "instance": {
    "name": "prueba-service-n8n-manual-001",
    "plan_id": "kvm-1",
    "image_id": "ubuntu-2404",
    "environment": "lab",
    "network_profile": "lab_dhcp",
    "access": {
      "root_password": "REEMPLAZAR_POR_SECRETO_TEMPORAL",
      "ssh_public_keys": ["ssh-ed25519 CLAVE_PUBLICA_DE_PRUEBA"]
    },
    "tags": ["prueba-servicio"],
    "start_on_create": true
  },
  "configuration": {
    "access_mode": "ip",
    "owner_email": "admin@example.invalid",
    "owner_first_name": "Admin",
    "owner_last_name": "Cliente"
  },
  "secrets": {"owner_password": "REEMPLAZAR_POR_SECRETO_TEMPORAL"}
}
JSON
chmod 600 service-request.json

curl --fail-with-body --silent --show-error \
  -X POST "${IACP_BASE_URL}/v1/service-instances" \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H 'Idempotency-Key: prueba-service-n8n-manual-001' \
  -H 'Content-Type: application/json' \
  --data-binary @service-request.json > service-response.json
chmod 600 service-response.json
```

Para Nextcloud, cambia `application_id` a `nextcloud`, usa un nombre como
`prueba-service-nextcloud-manual-001` y agrega `configuration.admin_user` más
`secrets.admin_password` al cuerpo. Conserva un nombre distinto y una
`Idempotency-Key` nueva para cada ensayo. Si el `POST` devuelve `202`, extrae
los identificadores y consulta el estado; una aceptación no significa que la
instalación terminó:

```bash
SERVICE_ID=$(jq -r .service_id service-response.json)
JOB_ID=$(jq -r .job_id service-response.json)
INSTANCE_ID=$(jq -r .instance_id service-response.json)
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}"
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/service-jobs/${JOB_ID}"
```

Espera a que el trabajo indique `succeeded` y la aplicación llegue a
`awaiting_customer`. n8n debe responder su readiness cuando PostgreSQL está
conectado y migrado. Consulta una sola vez las credenciales de Nextcloud o n8n;
si enviaste `owner_*`, el propietario de n8n se crea durante el bootstrap. Si
omitiste esos campos, n8n mantiene su wizard de primer acceso. Guarda también la
CA pública del piloto para TLS por IP; no
publiques esos archivos ni su contenido en terminales compartidas o tickets:

```bash
umask 077
curl --fail-with-body --silent --show-error \
  -X POST -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}/credentials" \
  > service-credentials.json
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}/tls-ca" \
  > iacp-pilot-ca.crt
chmod 600 service-credentials.json iacp-pilot-ca.crt
```

Verifica HTTPS y acceso SSH, inicia sesión en Nextcloud o n8n y valida la aplicación. El proxy no
solicita usuario y contraseña HTTP Basic. Después de guardar de forma segura
las credenciales de aplicación, completa la entrega:

```bash
curl --fail-with-body --silent --show-error \
  -X POST -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H "Idempotency-Key: entrega-prueba-service-n8n-manual-001" \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}/deliver"
```

La vista `/docs#create` genera el JSON y el cURL equivalentes, permite consultar
el inventario y administrar cada servicio seleccionado. La sección de
inventario se pliega para dejar libre el formulario. Para retirar un ensayo,
apaga primero su KVM mediante `POST /v1/instances/{instance_id}/power/shutdown`,
espera la operación y luego elimina el servicio:

```bash
curl --fail-with-body --silent --show-error \
  -X DELETE -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H 'Idempotency-Key: retirar-prueba-service-n8n-manual-001' \
  "${IACP_BASE_URL}/v1/service-instances/${SERVICE_ID}"
```

No uses un resultado de LAN DHCP para declarar disponibilidad WAN. La IP,
gateway, VLAN y reserva NetBox los decide el perfil del servidor; no se envían
en el request.
