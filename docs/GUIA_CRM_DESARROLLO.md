# Guía de integración: Provisioning API Core

Actualizada el 25/10/2026, America/Costa_Rica. Guía del piloto LAB/MNG. Ubuntu 24.04 KVM 1/2 conserva su E2E histórico; las cuatro plantillas están validadas por Infraestructura y habilitadas en la candidata de integración.
Comprobar siempre el OpenAPI y la disponibilidad realmente servidos. La documentación
principal se consulta localmente en `https://iac.cyberfuel.com/docs`.

HTTP Basic protege la documentación. Para API utiliza `Authorization: Bearer
<token-del-tenant>` con rol y derechos autorizados; en Swagger pulsa Authorize.
El tenant procede de esa identidad, nunca del cuerpo. No envíes VMID, nodo,
storage ni credenciales de infraestructura.

## Token y JSON mínimo del piloto

El backend consumidor configura `IACP_API_TOKEN` como secreto de entorno y envía
`Authorization: Bearer` con la credencial opaca existente. No introducir tokens en
el frontend, Git, logs ni capturas. La identidad de servicio se asocia al tenant
persistente autorizado; no se crea un tenant por solicitud ni se envía tenant_id.
HTTP Basic de documentación conserva su función y credencial separadas.

Esta revisión permite omitir `network` y `commercial_context` **para el contrato
técnico Provisioning API Core**, con las imágenes y planes que el catálogo autorice.
IACP resuelve la red DHCP técnica, conserva propiedad, catálogo, capacidad física e
idempotencia y no evalúa pedidos, productos, suscripciones ni presupuestos comerciales.
No hay fallback automático a otra red ni pedido CRM ficticio. `commercial_context`,
cuando se envía, es solo correlación opaca histórica; `null` no equivale a omitir y
los cuerpos completos siguen siendo válidos.
Verificar en el OpenAPI servido que esos dos campos ya no figuren en required
antes de usar el mínimo contra una instalación anterior.

```json
{
  "name": "piloto-001",
  "plan_id": "kvm-1",
  "image_id": "ubuntu-2404",
  "access": {
    "root_password": "REEMPLAZAR_POR_CONTRASENA_UNICA",
    "ssh_public_keys": []
  }
}
```

La contraseña es un marcador: sustituirla por una única del consumidor. La llave
pública es opcional para crear; es necesaria para acceso SSH con la llave propia.
El cURL local acepta IACP_API_TOKEN ya configurado o lo solicita sin eco.

La respuesta 202 conserva todos sus campos por compatibilidad. Para el recorrido
normal destacar `instance_id` y `status`, y consultar `GET /v1/instances/{instance_id}`.
Esperar también access.status=completed e IP observada antes de SSH; ready no basta.
operation_id y job_id continúan disponibles para diagnóstico, auditoría y soporte.

Conservar exactamente clave y cuerpo en reintentos: cambiar entre campos omitidos
y explícitos es un cambio de representación y devuelve 409 con la misma clave.
La intención almacena los valores efectivos y el perfil revisado; un retry no vuelve
a reservar ni cambia esos valores porque posteriormente cambie la configuración.

## Crear máquina virtual

La portada local de `/docs` contiene JSON válido, cURL completo, botones de copia,
una respuesta real del E2E del 25/10/2026 (VM ya eliminada) y el recorrido hasta
DELETE. No copiar los UUID de esa evidencia como si fueran los de una nueva VM.

1. Obtener Bearer autorizado del tenant por el canal existente; HTTP Basic solo
   permite leer documentación. Authorize recibe el token API y no amplía permisos.
2. Pulsar Authorize, después Consultar catálogo técnico. Seleccionar plan e imagen disponibles; la consola genera JSON, cURL e Idempotency-Key. `lab-dhcp` se resuelve automáticamente.
3. Pulsar Crear máquina virtual. La consola conserva instance_id y operation_id y sigue la operación; no es necesario copiar IDs ni claves. Para una integración programática, enviar el mismo cuerpo y una clave estable por intención. Para SSH aportar una llave
   pública propia; nunca una privada. El cURL solicita los valores por terminal,
   genera el cuerpo temporal privado y lo elimina después del envío. Guardarlo
   como `crear-piloto.sh` y usar `source ./crear-piloto.sh` desde Bash, fuera de Git.
4. Conservar `respuesta.json`, `X-Request-ID` e `Idempotency-Key`. Consultar los IDs
   devueltos hasta conocer operación y acceso; 202 no significa SSH listo.
5. Consultar `ip_addresses`; descubrir red si falta IP y esperar la operación.
   Verificar huella del host por el canal autorizado. Root SSH es por llave;
   la contraseña inicial permite consola y SSH root en el piloto LAB autorizado.
6. Ejecutar una acción Lifecycle cada vez, esperar terminal e invitado listo antes
   de reboot. Ante timeout conservar clave/cuerpo, sin nuevas intenciones ciegas.
7. Apagar y esperar terminal; DELETE autorizado con `{}`, después seguir operación
   y confirmar eliminación/conciliación. No borrar state ni forzar locks.

Fuentes exactas del recorrido: [`piloto.json`](../app/iacp/static/docs/piloto.json),
[`piloto-creacion.sh`](../app/iacp/static/docs/piloto-creacion.sh) (plantilla renderizada
por IACP; copiar la versión completa desde `/docs`) y
[evidencia física Fase G](validation/phase-g/cierre-mvp-20261025/INFORME.md).

## Consultar antes de crear

1. `GET /v1/plans`: cuatro planes técnicos, recursos exactos y disponibilidad del tenant.
2. `GET /v1/images`: estado y disponibilidad de cada sistema operativo.
3. `GET /v1/profiles`: referencia técnica de red autorizada; la consola resuelve `lab-dhcp` sin pedirlo en el formulario.

| image_id | Sistema | Estado de integración |
|---|---|---|
| ubuntu-2404 | Ubuntu 24.04 | KVM 1/2 y G6 validados en Fase G; comprobar disponibilidad actual |
| ubuntu-2604 | Ubuntu 26.04 | Plantilla validada por Infraestructura; habilitada en LAB/MNG; E2E del orquestador por ejecutar |
| almalinux-9 | AlmaLinux 9 | Plantilla validada por Infraestructura; habilitada en LAB/MNG; E2E del orquestador por ejecutar |
| almalinux-10 | AlmaLinux 10 | Plantilla validada por Infraestructura; habilitada en LAB/MNG; E2E del orquestador por ejecutar |

La presencia no concede permiso para crear. `available=false` expresa que no está
disponible para esa identidad/configuración/capacidad. Las cuatro imágenes admiten los
cuatro planes en LAB si la capacidad lo permite. Habilitada no significa E2E IACP certificado. Guest Agent configurado no demuestra DHCP,
SSH o filesystem funcionales.

## Crear mediante plan_id

Ejemplo ilustrativo compatible con el contrato técnico de `POST /v1/instances`; la contraseña es
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

Envía `POST /v1/instances` mediante HTTPS, `Content-Type: application/json` y
una `Idempotency-Key` estable por intención (por ejemplo, `crm-creacion-1002`). Kvm-2 resuelve internamente
2 vCPU, 4096 MiB y 100 GiB; no admite overrides. Para kvm-1 cambia el plan y las
referencias de una nueva intención: 1 vCPU, 2048 MiB y 50 GiB. Kvm-3/kvm-4
conservan 4/8192/200 y 8/16384/400. El catálogo expone los cuatro planes cuando
la capacidad física y la autorización técnica lo permiten; no se requiere una
aprobación comercial por solicitud.

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
ausencia de locks. El cuerpo `{}` solicita el flujo asíncrono de eliminación
verificado. Un 202 no confirma ausencia: sigue
la operación y comprueba limpieza final. Plantillas y recursos protegidos no son
eliminables desde este recorrido.

## Errores y reintentos

| Código/estado | Tratamiento |
|---|---|
| 400/422 | Corregir formato; no enviar tamaños ni VMIDs. |
| 401/403 | Revisar identidad, tenant y permisos; Bearer válido no concede todos los derechos. |
| 409 | Revisar intención, estado, catálogo, capacidad o protección técnica; no reducir el plan automáticamente. |
| 429 | Aplicar backoff y respetar el límite. |
| 503 | Conservar intención, consultar si hubo admisión y esperar recuperación. |
| uncertain | Conservar IDs y reconciliar por observación; no crear una sustituta. |

## Fuente versionada

La explicación principal está dentro de IACP. Referencia adicional:
[Ver fuente versionada en Forgejo](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_CRM_DESARROLLO.md).
