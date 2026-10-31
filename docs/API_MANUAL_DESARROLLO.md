# Manual para desarrolladores de consumo de la API IACP

*Integración de instancias y ciclo de vida de máquinas virtuales*

Guía práctica para construir un servicio de backend que consulte el catálogo, solicite VMs, siga operaciones y controle su ciclo de vida.

| Campo | Valor |
| --- | --- |
| Versión de API | IACP API v1 y operaciones 0.10.1 |
| Formato | JSON sobre HTTPS |
| Base oficial | https://iac.cyberfuel.com |
| Audiencia | Desarrolladores de integraciones y servicios de consumo |
| Incluye | Catálogo, aprovisionamiento, seguimiento, red, energía y eliminación |

**Idea principal.** El consumidor trabaja con identificadores estables como `plan_id`, `image_id`, `instance_id` y `operation_id`. La integración conserva esos identificadores y consulta la API para mostrar un estado confiable.

**Regla de integración.** Un código `202 Accepted` significa que la orden fue aceptada para procesamiento asíncrono; no significa que la VM ya esté lista. La integración debe consultar la operación hasta obtener `completed`, `failed` o `uncertain`.

## Contenido

El manual está ordenado según el recorrido de una integración que solicita una VM mediante la API.

1 Alcance del consumo
2 Preparar el cliente
3 Datos de acceso y trazabilidad
4 Consultar planes e imágenes
5 Crear una instancia
6 Seguir una operación
7 Inventario, red y energía
8 Eliminar una instancia
9 Errores y reintentos
10 Matriz de endpoints
11 Modelos y estados

## 1 Alcance y contratos

El IACP expone dos contratos compatibles. El contrato v1 recomendado recibe una intención de negocio en `POST /v1/instances`; el contrato de operaciones conserva la ejecución explícita de `plan`, `apply`, `destroy` y `power` mediante `POST /v1/operations`.

| Contrato v1 recomendado | Contrato de operaciones explícito |
| --- | --- |
| `POST /v1/instances` | `POST /v1/operations` |
| `plan_id`, `image_id`, `access` y `commercial_context` | `command`, `action` y `spec` |
| `instance_id`, `operation_id` y `job_id` | `plan_operation_id`, `instance_id` y `operation_id` |
| Plan y apply transparentes | Plan, apply, destroy y power explícitos |

El consumidor selecciona un plan y una Golden Image del catálogo, conserva el resultado de la operación y trabaja únicamente con los datos publicados por la API.

| La integración envía | La integración conserva |
| --- | --- |
| `name`, `plan_id` e `image_id` | `instance_id` y `operation_id` |
| Acceso inicial mediante `root_password` o `ssh_public_keys` | Estado de la VM y estado de la última operación |
| Referencias de negocio dentro de `commercial_context` | El cuerpo lógico enviado y la clave de idempotencia utilizada |
| `start_on_create` para definir el estado inicial | La respuesta original para mostrar seguimiento |

Los datos comerciales sirven para trazabilidad; no cambian el contrato ni la selección del plan.

Las instancias creadas mediante el contrato v1 se ejecutan en el pool técnico certificado `iacp-lab`. El consumidor no envía ni modifica `pool_id`, nodo, vCPU, memoria o disco; esos valores los determina el catálogo y el orquestador.

## 2 Preparar el cliente

La integración debe ejecutarse desde un servicio de backend. El navegador no debe guardar tokens, contraseñas ni secretos de acceso.

| Elemento | Uso |
| --- | --- |
| Base URL oficial | `https://iac.cyberfuel.com` |
| Authorization | Enviar `Bearer` seguido del token de integración en las rutas `/v1`. |
| Content-Type | Usar `application/json` en solicitudes con cuerpo. |
| Idempotency-Key | Opcional; se recomienda una clave estable para cada intención que cambie una instancia. |

```bash
export IACP_BASE_URL=https://iac.cyberfuel.com
export IACP_API_TOKEN=TOKEN_DEL_BACKEND

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/plans"
```

`/health/live`, `/health/ready`, `/docs`, `/swagger`, `/redoc` y `/openapi.json` sirven para disponibilidad o documentación. El contrato operativo de `/v1` debe consumirse con autenticación. Los valores de ejemplo son marcadores y nunca deben guardarse en Git, tickets o respuestas del navegador.

## 3 Datos de acceso y trazabilidad

El cuerpo público separa los datos de la VM, el acceso inicial y la información comercial. Esta separación permite agregar referencias de cliente sin mezclar los datos de consumo con las reglas del producto.

| Campo | Dónde va | Regla para la integración |
| --- | --- | --- |
| `name` | Nivel principal | Obligatorio; minúsculas, números y guiones; entre 1 y 63 caracteres; no terminar con guion. |
| `plan_id` | Nivel principal | Obligatorio; debe existir en `GET /v1/plans`. |
| `image_id` | Nivel principal | Obligatorio; debe existir en `GET /v1/images`. |
| `network_profile` | Nivel principal | `lab_dhcp` por defecto; `prod_static` solo cuando `GET /v1/network-profiles` lo devuelve habilitado. |
| `access.root_password` | `access` | Contraseña del usuario inicial `root`; se transporta mediante un secreto temporal. |
| `access.ssh_public_keys` | `access` | Lista opcional de claves SSH públicas; se puede usar sin contraseña. |
| `commercial_context` | Nivel principal | Si se envía, debe incluir `customer_id`, `customer_tag`, `order_id` y `product_id`. |
| `start_on_create` | Nivel principal | `true` enciende la VM al terminar la creación; `false` la deja apagada. |

### Perfiles de red

Consultar `GET /v1/network-profiles` antes de construir el formulario o el
request. `lab_dhcp` es el perfil de laboratorio y se mantiene como valor por
defecto. `prod_static` usa el IPAM NetBox del orquestador: reserva la primera
IP libre del prefijo `/24` configurado, mantiene `x.x.x.1` como gateway y
devuelve la reserva en `network_allocation`. El consumidor nunca calcula ni
envía la IP, el gateway o el prefijo; tampoco debe tratar una dirección
observada por Guest Agent como una reserva de IPAM.

Todas las Golden Images publicadas usan `root` como usuario inicial, incluyendo `ubuntu-2404`, `ubuntu-2604`, `almalinux-9` y `almalinux-10`. Para que el aprovisionamiento concluya, la integración debe enviar una contraseña root o una clave SSH; si no existe ninguna alternativa, la operación termina con `SSH_PUBLIC_KEY_REQUIRED`.

La contraseña se utiliza únicamente durante el aprovisionamiento, no se persiste en el payload operativo y no se devuelve en la API. `customer_tag` se conserva como referencia de trazabilidad.

### Contrato de operación explícita

Para integrar el contrato de operaciones, `POST /v1/operations` acepta `command=plan`, `provision`, `apply`, `destroy` o `power`. El flujo de dos pasos conserva el `operation_id` del plan y lo envía como `plan_operation_id` al aplicar. Las integraciones nuevas deben preferir `POST /v1/instances`:

```json
{
  "command": "plan",
  "action": "provision",
  "plan_id": "kvm-2",
  "spec": {
    "name": "cliente-cl-0000",
    "image": "ubuntu-2404",
    "vcpus": 2,
    "memory_mb": 4096,
    "disk_gb": 100,
    "network_mode": "dhcp",
    "started": true,
    "ssh_public_keys": []
  }
}
```

La `spec` heredada también admite `network_mode=static` con `ipv4_cidr` e `ipv4_gateway`, además de `vlan_id` y `tags`. Para altas nuevas, usar `POST /v1/instances`.

## 4 Consultar planes e imágenes

La integración debe cargar el catálogo desde la API para no duplicar reglas en el frontend. Guardar el identificador seleccionado, no solo el texto mostrado al usuario.

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/plans"

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/images"

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/network-profiles"
```

### Planes publicados

| `plan_id` | vCPU | Memoria MB | Disco GB |
| --- | --- | --- | --- |
| `kvm-1` | 1 | 2048 | 50 |
| `kvm-2` | 2 | 4096 | 100 |
| `kvm-3` | 4 | 8192 | 200 |
| `kvm-4` | 8 | 16384 | 400 |

### Golden Images publicadas

| `image_id` | Texto sugerido | Usuario inicial |
| --- | --- | --- |
| `ubuntu-2404` | Ubuntu 24.04 | `root` |
| `ubuntu-2604` | Ubuntu 26.04 | `root` |
| `almalinux-9` | AlmaLinux 9 | `root` |
| `almalinux-10` | AlmaLinux 10 | `root` |

El catálogo puede incluir los mínimos y metadatos de cada imagen. La integración debe aceptar únicamente identificadores que la API devuelva como disponibles; el catálogo puede cambiar sin modificar el contrato de consumo.

## 5 Crear una instancia

`POST /v1/instances` es la entrada pública recomendada. La API valida `plan_id` e `image_id` contra el catálogo, registra la instancia y procesa el aprovisionamiento de forma asíncrona.

El ejemplo usa datos ilustrativos. La contraseña debe provenir de un mecanismo seguro y el usuario de conexión inicial es `root` también para Ubuntu.

```bash
export CREATE_KEY=crm-create-ORD-1001

cat > request.json <<'JSON'
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
JSON

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: ${CREATE_KEY}" \
  --data-binary @request.json \
  "${IACP_BASE_URL}/v1/instances"
```

### Respuesta aceptada

```json
{
  "operation_id": "22222222-2222-4222-8222-222222222222",
  "job_id": "22222222-2222-4222-8222-222222222222",
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "status": "queued",
  "command": "provision",
  "action": "provision",
  "idempotency_key": "crm-create-ORD-1001"
}
```

Conservar `instance_id` y `operation_id`. En la respuesta actual, `job_id` es un alias de `operation_id` y ambos contienen el mismo UUID; la consulta de seguimiento se realiza con `operation_id`. Si la integración reenvía el mismo cuerpo con la misma `Idempotency-Key`, la API devuelve la intención existente. Si se reutiliza la clave con otro cuerpo, la API responde `409`.

## 6 Seguir una operación

Toda acción que devuelve `202` debe seguirse con `GET /v1/operations/{operation_id}`. La integración puede consultar con un intervalo moderado y refrescar la VM cuando la operación termine.

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/operations/${OPERATION_ID}"
```

| Estado público | Qué mostrar |
| --- | --- |
| `queued` | Solicitud recibida; mostrar pendiente. |
| `running` | Acción en progreso; mantenerla pendiente hasta recibir el resultado. |
| `completed` | Acción completada; consultar la instancia y refrescar la vista. |
| `failed` | Mostrar el `error_code` y permitir una decisión del usuario. |
| `uncertain` | No crear otra VM ni repetir automáticamente; conservar los identificadores y solicitar revisión. |

### Flujo recomendado

1. Enviar la solicitud y guardar el JSON, `instance_id`, `operation_id` y la clave usada.
2. Consultar la operación hasta `completed`, `failed` o `uncertain`.
3. Consultar `GET /v1/instances/{instance_id}` para refrescar la información de la VM.
4. Actualizar el estado sin crear otra solicitud por un simple timeout del cliente.

### Eventos, workers y seguimiento en vivo

`GET /v1/events` devuelve los eventos más recientes primero (`sort=desc`). Para reconstruir una operación en orden cronológico usa `operation_id` y `sort=asc`:

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  "${IACP_BASE_URL}/v1/events?operation_id=${OPERATION_ID}&sort=asc&limit=200"
```

Cada evento incluye, cuando existe, `worker_id`, `instance_id`, `instance_name`, `command` y `action`. `GET /v1/workers` muestra la operación actual, la última operación y su último evento; `GET /v1/observability` agrega los conteos, eventos recientes, operaciones activas, profundidad de cola, `max_concurrent` (capacidad configurada) y rendimiento reciente p50/p95. Cada operación también expone `timings.queue_seconds`, `timings.execution_seconds` y `timings.total_seconds`. Para una consola interactiva, consultar cada 2 segundos mientras haya operaciones `queued`/`running` y espaciar las consultas cuando el sistema esté inactivo.

Las respuestas públicas entregan una bitácora de orquestación, no la salida de las herramientas internas. `output` resume la etapa y conserva datos útiles como VMID; `error_code` identifica la causa accionable; `detail` contiene únicamente contexto de la operación. No se deben buscar ni depender de nombres de ejecutores, proveedores, rutas de workspace o comandos internos: esos datos se filtran para mantener estable el contrato del consumidor.

## 7 Inventario, red y energía

El inventario permite actualizar una fila o consultar una VM puntual. `GET /v1/instances` acepta `limit`, `offset`, `status`, `name` e `include_deleted`.

La ruta `GET /v1/instances/{instance_id}/network` separa la configuración declarada de la información observada por la API. `observed_ipv4` solo contiene direcciones confirmadas por el servicio; el CIDR configurado no debe presentarse como una IP confirmada.

Cuando el perfil es `prod_static`, la respuesta agrega `network_allocation`.
Su `status` puede ser `pending`, `allocated`, `released` o `release_pending`;
`allocated` incluye la IP pública y el gateway asignados por NetBox. La
eliminación libera el objeto de IP únicamente después de comprobar que la
reserva pertenece a la instancia; ante una respuesta ambigua se conserva
`release_pending` y la operación se expone como `uncertain` para evitar
reutilizar una IP posiblemente ocupada.

| Acción | Endpoint | Resultado |
| --- | --- | --- |
| Consultar instancia | `GET /v1/instances/{instance_id}` | Detalle de la VM y última operación. |
| Consultar red | `GET /v1/instances/{instance_id}<br>/network` | `observed_ipv4`, `source`, `status` y `reason`. |
| Consultar energía | `GET /v1/instances/{instance_id}<br>/power` | `power_state`: `running` o `stopped`. |
| Encender | `POST /v1/instances/{instance_id}<br>/power/start` | `202` y `operation_id`. |
| Apagar | `POST /v1/instances/{instance_id}<br>/power/shutdown` | `202` y `operation_id`; si Proxmox informa timeout de ACPI/Guest Agent, el worker completa con stop seguro. |
| Reiniciar | `POST /v1/instances/{instance_id}<br>/power/reboot` | `202` y `operation_id`. |

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H "Idempotency-Key: power-ORD-1001-001" \
  -H "Content-Type: application/json" \
  --data '{}' \
  "${IACP_BASE_URL}/v1/instances/${INSTANCE_ID}/power/start"
```

Las acciones de energía son asíncronas y usan su propia clave de idempotencia. El indicador visual debe reflejar el último estado confirmado por la API; no inferir que una acción terminó solo porque la solicitud HTTP fue aceptada. Si una acción no puede ejecutarse, la operación termina con un `error_code` accionable.

## 8 Eliminar una instancia

La eliminación debe requerir una confirmación explícita en la integración. La API no destruye una VM que todavía está en estado de energía `running`; primero se debe solicitar apagado y esperar su resultado.

```bash
curl --fail-with-body --silent --show-error \
  -X DELETE \
  -H "Authorization: Bearer ${IACP_API_TOKEN}" \
  -H "Idempotency-Key: delete-ORD-1001-001" \
  "${IACP_BASE_URL}/v1/instances/${INSTANCE_ID}"
```

| Momento | Comportamiento |
| --- | --- |
| Antes de enviar | Solicitar confirmación y verificar que el usuario eligió la VM correcta. |
| Respuesta `202` | Guardar `operation_id` y mostrar eliminación en progreso. |
| `completed` | Actualizar la fila como eliminada o retirarla según la UX del producto. |
| `failed` o `uncertain` | Conservar la fila y mostrar que se requiere revisión; no repetir automáticamente. |

## 9 Errores y reintentos

| HTTP | Significado | Acción |
| --- | --- | --- |
| `401` | Token ausente o no válido. | Corregir la configuración del backend; no cambiar el JSON. |
| `403` | Acción de energía bloqueada por permisos. | Revisar el contrato de acceso y el `error_code` devuelto por la operación. |
| `404` | La VM, operación o identificador no existe. | Actualizar el inventario y validar el identificador. |
| `409` | Intención repetida, VM ocupada, clave reutilizada con otro cuerpo o estado incompatible. | Consultar la VM y la operación existente antes de intentar otra acción. |
| `422` | JSON, catálogo, nombre o valor de red no válido. | Mostrar el campo que debe corregirse; no reintentar sin cambios. |
| `503` | El perfil está deshabilitado o el servicio no está disponible para aceptar o confirmar una acción. | Consultar el catálogo; conservar la intención y reintentar con espera controlada solo si el estado no es `uncertain`. |

### Reglas simples para no duplicar VMs

- No crear una VM sustituta porque una consulta tardó más de lo esperado.
- No cambiar la `Idempotency-Key` al repetir la misma petición por un timeout.
- No enviar una nueva acción mientras la anterior de la misma VM siga en progreso.
- Si la API responde `uncertain`, conservar la intención y escalar la revisión en lugar de duplicarla.
- No registrar `root_password` ni mostrarlo en logs, tickets, navegador o respuestas de soporte.

## 10 Matriz de endpoints

Esta es la superficie funcional para consumir el catálogo, el aprovisionamiento, el inventario y el ciclo de vida de VMs.

| Método y ruta | Uso | Entrada | Respuesta |
| --- | --- | --- | --- |
| `GET /health/live` | Comprobación rápida para validar que el servicio responde. | Sin autenticación ni cuerpo. | `200` con `status=alive`. |
| `GET /health/ready` | Comprobación previa a una acción de la integración. | Sin autenticación ni cuerpo. | `200` con `status=ready`. |
| `GET /v1/plans` | Poblar el selector de tamaño. | Bearer; sin cuerpo. | `200` lista de planes. |
| `GET /v1/images` | Poblar el selector de Golden Image. | Bearer; sin cuerpo. | `200` lista de imágenes y usuario inicial. |
| `GET /v1/network-profiles` | Poblar el selector de red. | Bearer; sin cuerpo. | `200` perfiles y bandera `enabled`; no expone secretos ni el prefijo. |
| `GET /v1/instances` | Cargar la lista principal de VMs. | Filtros opcionales, límite, desplazamiento y eliminadas. | `200` lista paginada de instancias. |
| `POST /v1/instances` | Crear la intención pública de aprovisionamiento. | Bearer, JSON, acceso y `Idempotency-Key` opcional. | `202` con `instance_id`, `operation_id` y `job_id`. |
| `GET<br>/v1/instances/{instance_id}` | Actualizar el detalle o la fila de una VM. | `instance_id` en la ruta. | `200` detalle de instancia. |
| `GET<br>/v1/instances/{instance_id}<br>/network` | Observar la información de red disponible. | `instance_id` en la ruta. | `200` red declarada y observada. |
| `GET<br>/v1/instances/{instance_id}<br>/power` | Consultar el estado de energía. | `instance_id` en la ruta. | `200` `PowerState` y permisos disponibles. |
| `POST<br>/v1/instances/{instance_id}<br>/power/start` | Encender una VM. | Cuerpo `{}` y clave opcional. | `202` con `operation_id`. |
| `POST<br>/v1/instances/{instance_id}<br>/power/shutdown` | Apagar ordenadamente; fallback seguro ante timeout de ACPI/Guest Agent. | Cuerpo `{}` y clave opcional. | `202` con `operation_id`. |
| `POST<br>/v1/instances/{instance_id}<br>/power/reboot` | Reiniciar una VM encendida. | Cuerpo `{}` y clave opcional. | `202` con `operation_id`. |
| `DELETE<br>/v1/instances/{instance_id}` | Eliminar una VM después de confirmación. | `instance_id` y clave opcional. | `202` con `operation_id`. |
| `GET /v1/operations` | Mostrar actividad reciente. | Filtros opcionales por VM y estado. | `200` lista paginada de operaciones. |
| `GET<br>/v1/operations/{operation_id}` | Consultar el resultado de una acción. | `operation_id` en la ruta. | `200` operación normalizada. |
| `GET /v1/events` | Obtener trazabilidad técnica reciente o la línea de tiempo de una operación. | `sort`, `operation_id`, `limit`, `offset` y `after` opcionales. | `200` eventos enriquecidos y paginados. |
| `GET /v1/workers` | Observar heartbeats y ejecución por worker. | Sin cuerpo. | `200` workers, operación actual y última operación. |
| `GET /v1/observability` | Resumen del tenant, capacidad, rendimiento y eventos recientes. | Sin cuerpo. | `200` conteos, `max_concurrent`, p50/p95, throughput, operaciones activas, workers y eventos. |
| `POST<br>/v1/instances/{instance_id}<br>/provision` | Ejecutar el apply del último plan exitoso. | `instance_id` y `plan_operation_id` opcional. | `200` orden de apply. |
| `POST /v1/operations` | Contrato de operaciones para `plan`, `provision`, `apply`, `destroy` o `power`. | JSON `OperationRequest`. | `200` operación aceptada. |

Las rutas `/v1` requieren `Authorization: Bearer`. La API interactiva se publica en `/docs`, `/swagger` y `/redoc`; el contrato descargable está en `/openapi.json`.

## 11 Modelos y estados

### Proyección recomendada de una instancia

| Dato | Uso |
| --- | --- |
| `instance_id` | Identificador de la VM en la integración. |
| `operation_id` | Identificador de la orden de aprovisionamiento original. |
| `name` | Nombre mostrado al usuario. |
| `image_id` y `plan_id` | Golden Image y tamaño seleccionados. |
| `network_profile` | Política de red seleccionada; no contiene una IP asignada. |
| `status` | `pending`, `provisioning`, `ready`, `deleting`, `failed`, `uncertain` o `deleted`. |
| `power_state` | Último estado confirmado: normalmente `running` o `stopped`. |
| `commercial_context` | Referencias de cliente, orden y producto cuando fueron enviadas. |
| `latest_operation` | Última operación normalizada para la VM. |
| `network_allocation` | Reserva IPAM, si existe; contiene dirección, gateway y estado, nunca el token de NetBox. |

### Respuesta normalizada de operación

```json
{
  "operation_id": "22222222-2222-4222-8222-222222222222",
  "instance_id": "11111111-1111-4111-8111-111111111111",
  "status": "completed",
  "normalized_status": "completed",
  "error_code": null
}
```

El consumidor puede trabajar con los estados públicos de la API. En el contrato de operaciones, la operación `plan` debe terminar correctamente antes de enviar `apply` o `destroy`; `power` requiere `instance_id` y `power_action`. En el contrato v1, la creación de la instancia se presenta como una sola intención de aprovisionamiento.

### Evolución del JSON

Si desarrollo necesita más variables, se deben definir su nombre, ubicación y comportamiento antes de agregarlas a la guía de consumo y a las pruebas. La información comercial debe permanecer en `commercial_context`; los cambios de tamaño o sistema deben validarse contra el catálogo.

| Necesidad | Lugar recomendado |
| --- | --- |
| Identificación comercial | `commercial_context`. |
| Datos que cambian el tamaño o sistema | `plan_id`, `image_id` y validación explícita del catálogo. |
| Preferencias del producto | Base de datos de la integración; no enviarlas a la API si no participan en la ejecución. |
| Acceso inicial | `access.root_password` o `access.ssh_public_keys`; nunca en texto libre fuera del cuerpo seguro. |

Esta guía corresponde al contrato público `/v1` del IACP. Si la API publica una versión distinta, revisar el catálogo, los estados y los campos antes de integrar nuevas variables.
