# Guía de integración CRM: catálogo comercial G

Actualizada el 25/10/2026, America/Costa_Rica. Guía de la candidata G: comprobar
el OpenAPI realmente servido antes de enviar el contrato nuevo. La documentación
principal se consulta localmente en `https://iac.cyberfuel.com/docs`.

HTTP Basic protege la documentación. Para API utiliza `Authorization: Bearer
<token-del-tenant>` con rol y derechos autorizados; en Swagger pulsa Authorize.
El tenant procede de esa identidad, nunca del cuerpo. No envíes VMID, nodo,
storage ni credenciales de infraestructura.

## Consultar antes de crear

1. `GET /v1/plans`: cuatro productos, recursos exactos y disponibilidad del tenant.
2. `GET /v1/images`: estado y disponibilidad de cada sistema operativo.
3. `GET /v1/profiles`: perfiles de red autorizados; el piloto usa `lab-dhcp`.

| image_id | Sistema | Estado de integración |
|---|---|---|
| ubuntu-2404 | Ubuntu 24.04 | Habilitada por certificación histórica; comprobar capacidad y G6 |
| ubuntu-2604 | Ubuntu 26.04 | Pendiente, no seleccionable |
| almalinux-9 | AlmaLinux 9 | Pendiente, no seleccionable |
| almalinux-10 | AlmaLinux 10 | Pendiente, no seleccionable |

La presencia no concede permiso para crear. `available=false` expresa que no está
disponible; capacidades `null` y planes compatibles vacíos en imágenes pendientes
significan verificación incompleta. Guest Agent configurado no demuestra DHCP,
SSH o filesystem funcionales.

## Crear mediante plan_id

Ejemplo ilustrativo compatible con `CommercialInstanceCreate`; la contraseña es
un marcador y debe sustituirse por la inicial del cliente, gestionada por el CRM.
No usar el marcador como contraseña de una VM real.

```json
{
  "name": "cliente-kvm-2-001",
  "plan_id": "kvm-2",
  "image_id": "ubuntu-2404",
  "network": {"profile_id": "lab-dhcp"},
  "access": {
    "root_password": "CONTRASENA_ILUSTRATIVA_CAMBIAR",
    "ssh_public_keys": []
  },
  "start_on_create": true,
  "commercial_context": {"order_id": "ORD-1002", "product_id": "CRM-KVM-2"}
}
```

Envía `POST /v1/instances` mediante HTTPS, `Content-Type: application/json` e
`Idempotency-Key: crm-pedido-1002-creacion`. Kvm-2 resuelve internamente
2 vCPU, 4096 MiB y 100 GiB; no admite overrides. Para kvm-1 cambia el plan y las
referencias de una nueva intención: 1 vCPU, 2048 MiB y 50 GiB. Kvm-3/kvm-4
conservan 4/8192/200 y 8/16384/400, pero requieren presupuesto explícito;
el laboratorio no obtiene esos derechos por existir los productos.

No envíes tamaños libres, VMIDs ni `self_service_policy_id`. `profile_id`
pertenece a `network`, no a la raíz. La misma intención conserva cuerpo, tenant y
clave. Tras timeout consulta y repite esa intención; no generes otra clave sin
comprobar la anterior. Otro cuerpo con la misma clave devuelve 409.

## Seguimiento, red y acceso

HTTP 202 admite una intención; no demuestra una VM lista. Conserva `instance_id`,
`operation_id`, `job_id` y `X-Request-ID`. Consulta
`GET /v1/operations/{operation_id}` hasta `completed`, `failed` o `uncertain`,
y `GET /v1/instances/{instance_id}` para recursos, fulfillment y direcciones.
Queued/running requieren seguimiento. Completed no equivale a SSH disponible:
el invitado puede esperar Cloud-Init, DHCP y Guest Agent.

La contraseña root se entrega mediante bootstrap seguro cuando el guest está
preparado, sin pasar por Terraform ni la cola. Habilita consola root; SSH root
es por llave pública opcional, no contraseña. Si el secreto expira, conserva
la intención y sigue la recuperación autorizada. Ante entrega incierta, procede
por observación y escalamiento. No publiques secretos en logs o evidencias.

## Lifecycle y eliminación

Consulta `GET /v1/instances/{instance_id}/power`. Las acciones son
`POST /v1/instances/{instance_id}/power/start`, `/power/shutdown` y
`/power/reboot`, con Idempotency-Key por intención y seguimiento de la operación.

`DELETE /v1/instances/{instance_id}` exige propiedad, autorización, VM apagada y
ausencia de locks. En la vía comercial autorizada, el cuerpo `{}` solicita el
flujo asíncrono de eliminación verificado. Un 202 no confirma ausencia: sigue
la operación y comprueba limpieza final. Plantillas y recursos protegidos no son
eliminables desde este recorrido.

## Errores y reintentos

| Código/estado | Tratamiento |
|---|---|
| 400/422 | Corregir formato; no enviar tamaños ni VMIDs. |
| 401/403 | Revisar identidad, tenant y permisos; Bearer válido no concede todos los derechos. |
| 409 | Revisar intención, estado, catálogo, cuota o capacidad; no reducir el plan automáticamente. |
| 429 | Aplicar backoff y respetar el límite. |
| 503 | Conservar intención, consultar si hubo admisión y esperar recuperación. |
| uncertain | Conservar IDs y reconciliar por observación; no crear una sustituta. |

## Fuente versionada

La explicación principal está dentro de IACP. Referencia adicional:
[Ver fuente versionada en Forgejo](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_CRM_DESARROLLO.md).
