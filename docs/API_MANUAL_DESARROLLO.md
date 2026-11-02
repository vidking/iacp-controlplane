# Manual de consumo de la API IACP Control Plane

*Contrato HTTP v1 para integraciones, portales y servicios de backend*

El consumidor declara qué VM necesita mediante campos técnicos estables. IACP
valida el catálogo, asigna recursos, ejecuta Terraform y conserva el progreso
como operaciones observables. Un `202 Accepted` confirma que la solicitud fue
aceptada para procesamiento; no significa que la VM ya esté lista.

## Contenido

1. Contrato y autenticación
2. Descubrir entornos, planes e imágenes
3. Estructura del request
4. Crear y seguir una VM
5. Tags Proxmox
6. Inventario, red y lifecycle
7. Idempotencia, errores y reconciliación
8. API explícita para operadores
9. Swagger y documentación

## 1. Contrato y autenticación

El contrato público usa rutas `/v1`. No existe un prefijo `/v2`: el nombre
histórico del repositorio de origen no cambia la versión visible del API.

| Runtime | Base URL planeada | Destino lógico inicial |
| --- | --- | --- |
| Desarrollo | `https://dev.iac.cyberfuel.com` | `lab`, resuelto a MNG |
| Producción | `https://iac.cyberfuel.com` | `lab`, resuelto a MNG; PRX queda apagado hasta su certificación |

La API privada requiere HTTPS y, si `IACP_REQUIRE_API_TOKEN=1`, un token Bearer:

```bash
export IACP_BASE_URL=https://dev.iac.cyberfuel.com
export IACP_API_TOKEN=TOKEN_DE_INTEGRACION

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/environments"
```

Guarda el token y las contraseñas en el backend de la integración. No los
incluyas en JavaScript del navegador, URLs, logs o repositorios.

El consumidor elige `environment=lab`; no manda `cluster`, `node`, `pool_id`,
`target_node`, nombre de stack ni configuración Terraform. El runtime de
desarrollo solo ofrece MNG. Producción comienza también en MNG y podrá mostrar
PRX cuando la configuración y certificación estén completas.

## 2. Descubrir entornos, planes e imágenes

Consulta los catálogos al iniciar la integración y cuando se abra el formulario.
No mantengas una copia independiente de planes o templates.

```bash
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/environments"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/plans?environment=lab"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/images?environment=lab"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/network-profiles"
```

`GET /v1/environments` devuelve `default_environment` y la lista de valores
lógicos habilitados. `GET /v1/plans` y `/v1/images` aceptan `environment`
opcional; si se omite, el servidor utiliza su entorno predeterminado.

Los IDs actuales de catálogo pueden incluir planes `kvm-1` a `kvm-4` e
imágenes `ubuntu-2404`, `ubuntu-2604`, `almalinux-9` y `almalinux-10`. El
catálogo del runtime es la autoridad y puede diferir entre entornos.

El perfil disponible es `lab_dhcp`. La VM usa DHCP en la red que configura el
entorno. El consumidor no envía una dirección, prefijo, gateway, VLAN ni una
selección de IPAM.

## 3. Estructura fija del request

`POST /v1/instances` recibe el siguiente objeto. Los campos se mantienen en
esta forma para que Swagger, el portal y las integraciones muestren un contrato
legible y uniforme:

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

| Campo | Tipo | Regla |
| --- | --- | --- |
| `name` | string | Obligatorio; de 1 a 63 caracteres, minúsculas, números y guiones. Debe empezar y terminar con letra o número. |
| `environment` | string | `lab` o un valor que haya publicado `/v1/environments`. Si se omite, se usa el default del runtime. |
| `plan_id` | string | Obligatorio; identificador devuelto por `/v1/plans`. |
| `image_id` | string | Obligatorio; identificador devuelto por `/v1/images`. |
| `network_profile` | string | `lab_dhcp`; no se acepta IP estática en este contrato. |
| `tags` | array de strings | Opcional; lista descriptiva de etiquetas Proxmox. Vacía significa que no se agregan etiquetas. |
| `access.root_password` | string | Contraseña temporal de la cuenta inicial `root`; respeta la longitud mínima del runtime. |
| `access.ssh_public_keys` | array de strings | Una o más claves públicas; puede usarse sin contraseña. |
| `start_on_create` | boolean | `true` solicita iniciar la VM después del aprovisionamiento; `false` la deja apagada. |

Los campos desconocidos se rechazan para detectar errores de integración en vez
de aceptar un JSON que el servicio no interpreta. No se envían campos de
cliente, orden, producto, `cluster`, `node`, credenciales Proxmox ni estado
Terraform.

## 4. Crear y seguir una VM

Envía una `Idempotency-Key` estable para que un reintento de red no cree otra
intención. El mismo valor debe acompañar al mismo cuerpo.

```bash
cat > request.json <<'JSON'
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
JSON

curl --fail-with-body --silent --show-error \
  -X POST "${IACP_BASE_URL}/v1/instances" \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H 'Idempotency-Key: alta-cl-105-kvm-001' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

La respuesta `202` entrega los identificadores para seguimiento:

```json
{
  "operation_id": "22222222-2222-4222-8222-222222222222",
  "job_id": "22222222-2222-4222-8222-222222222222",
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "environment": "lab",
  "idempotency_key": "alta-cl-105-kvm-001"
}
```

Consulta hasta un estado terminal. La operación representa plan y apply; no
envíes `apply` como una segunda creación.

```bash
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/operations/22222222-2222-4222-8222-222222222222"
curl -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/instances/11111111-1111-4111-8111-111111111111"
```

Estados de operación: `queued`, `running`, `completed`, `failed` y
`uncertain`. Estados de instancia: `pending`, `provisioning`, `ready`,
`stopped`, `deleting`, `failed`, `uncertain` y `deleted`. Un `ready` describe
una VM encendida; `stopped` es una VM administrada que está apagada.

## 5. Tags Proxmox

Los tags se declaran como array JSON, no como objeto ni como una única cadena:

```json
"tags": ["cl-105", "kvm-001", "postal", "smtp-relay"]
```

Esto permite enviar etiquetas de cliente, nombre técnico u otra clasificación
sin modificar el esquema cada vez que aparece un nuevo tipo. Cada valor debe
usar minúsculas, números, guion o guion bajo, empezar con letra o número y no
superar 63 caracteres. Los duplicados se eliminan conservando el primer orden
en el registro de la API. Proxmox puede mostrar el conjunto ordenado.

El orquestador aplica únicamente los elementos enviados. No crea por su cuenta
`iacp`, el entorno, el cliente ni el nombre como tags. El nombre de la VM sigue
siendo el campo independiente `name`.

## 6. Inventario, red y lifecycle

| Método y ruta | Uso |
| --- | --- |
| `GET /v1/instances` | Listar instancias con paginación y filtros. |
| `GET /v1/instances/{instance_id}` | Consultar detalle, spec y última operación. |
| `GET /v1/instances/{instance_id}/network` | Consultar IP observada por QEMU Guest Agent. |
| `GET /v1/instances/{instance_id}/power` | Observar el estado de energía. |
| `POST /v1/instances/{instance_id}/power/start` | Encolar inicio. |
| `POST /v1/instances/{instance_id}/power/shutdown` | Encolar apagado ordenado. |
| `POST /v1/instances/{instance_id}/power/reboot` | Encolar reinicio. |
| `DELETE /v1/instances/{instance_id}` | Destruir una VM administrada apagada. |

Las acciones de lifecycle y delete son asíncronas y devuelven `operation_id`.
Sigue la operación hasta el estado terminal. El servicio no debe reportar
éxito si Proxmox no confirmó el cambio.

La dirección DHCP aparece en `observed_ipv4` solo cuando responde Guest Agent.
Un resultado `unavailable` indica que la IP no se pudo confirmar; no significa
que el contrato permita fijar una IP desde el consumidor.

La API bloquea mutaciones a recursos protegidos. La VM 104, la VM 106 y las
Golden Images base no son instancias de consumidor.

## 7. Idempotencia, errores y reconciliación

Usa una llave diferente por intención de cambio y conserva la llave mientras
reintentas esa misma intención. Reutilizarla con otro cuerpo produce `409`.
Al exceder `IACP_MAX_QUEUE_DEPTH`, la API devuelve `429` con `Retry-After`;
espera y reintenta conservando la llave.

Una mutación física puede perder su respuesta aunque Proxmox la haya aplicado.
En ese caso el worker observa primero la VM. Si no logra determinar el estado,
la operación termina `uncertain`; no la trates como `failed` ni vuelvas a
aprovisionar con otra llave.

Consulta eventos en orden cronológico:

```http
GET /v1/events?operation_id={operation_id}&sort=asc
GET /v1/observability
```

La reconciliación disponible para una instancia incierta se encola con:

```http
POST /v1/instances/{instance_id}/reconcile
Authorization: Bearer <token>
Idempotency-Key: reconcile-{operation_id}
```

La reconciliación compara la intención persistida con el estado físico antes
de cerrar una operación. No borra states ni fuerza locks.

## 8. API explícita para operadores

`POST /v1/operations` permite encolar `plan`, `apply`, `destroy`, `power` o
`reconcile` con un request técnico explícito. Para un plan, `spec` admite
`name`, `image`, `vcpus`, `memory_mb`, `disk_gb`, acceso, estado inicial y
`tags`. Sigue usando `environment` lógico; no admite `stack`, `cluster` o
`target_node` del consumidor.

Las integraciones de aprovisionamiento deben preferir `POST /v1/instances`,
que valida `plan_id` e `image_id` y ejecuta plan + apply en una sola intención.

## 9. Swagger y documentación

El portal `/docs` explica el flujo, permite seleccionar entorno y catálogo, y
genera el JSON y cURL desde los valores del formulario. La contraseña se
redacta en la vista previa. `/swagger` presenta el esquema OpenAPI con
descripciones y ejemplos enlazados al modelo Pydantic; `/redoc` y
`/openapi.json` ofrecen las otras vistas del mismo contrato.

Rutas públicas de salud y documentación:

- `/health/live`
- `/health/ready`
- `/docs`
- `/swagger`
- `/redoc`
- `/openapi.json`

Las operaciones protegidas requieren el Bearer que configure el runtime.
