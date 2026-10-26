# Guía breve — Provisioning API Core

IACP recibe solicitudes técnicas autenticadas y administra su ciclo de vida sobre destinos autorizados. El CRM o portal consumidor conserva las decisiones de venta, pedidos, precios y facturación; esos datos no son necesarios para crear una KVM.

## Conexión

- Base URL: `https://iac.cyberfuel.com`
- Autenticación: `Authorization: Bearer <token>` de una identidad técnica con rol `operator`.
- Cada mutación usa una `Idempotency-Key` nueva, por ejemplo un UUID generado por el consumidor. Un reintento de la misma intención reutiliza exactamente la misma clave; una acción diferente (`start`, `shutdown`, `reboot` o `DELETE`) usa otra. Las consultas `GET` no requieren la cabecera.

## Crear una instancia

1. Obtenga el catálogo con `GET /v1/plans` y `GET /v1/images` usando el Bearer de la identidad técnica.
2. Seleccione un plan y una Golden Image cuya disponibilidad sea `true`.
3. Envíe `POST /v1/instances` por HTTPS con una `Idempotency-Key` nueva por intención:

```json
{
  "name": "vm-prueba-001",
  "plan_id": "kvm-1",
  "image_id": "ubuntu-2404",
  "access": {
    "root_password": "CONTRASENA_UNICA_DE_16_O_MAS"
  }
}
```

`tenant_id`, VMID, nodo, almacenamiento, worker y executor los obtiene IACP de la identidad y del catálogo. `network` y `commercial_context` son opcionales; el segundo solo sirve como correlación histórica y no autoriza nada.

Un `202` confirma la admisión, no la disponibilidad. Conserve `instance_id`, `operation_id` y `job_id`; consulte la operación y la instancia hasta un estado terminal. Los reintentos de la misma intención reutilizan exactamente la misma clave. No repita `apply` mientras el resultado sea `uncertain`.

## Seguimiento y ciclo de vida

1. `GET /v1/operations/{operation_id}` muestra `queued`, `running`, `completed`, `failed` o `uncertain`, junto con eventos, fase Terraform y errores.
2. `GET /v1/instances/{instance_id}` muestra VMID, nodo, recursos, estado y direcciones DHCP observadas.
3. `GET /v1/instances/{instance_id}/power` consulta el estado efectivo de Proxmox.
4. `POST /v1/instances/{instance_id}/power/start`, `/shutdown` o `/reboot` ejecuta Lifecycle con una clave propia.
5. `DELETE /v1/instances/{instance_id}` solicita la eliminación auditada y espera `instance.delete` en estado `completed` antes de liberar la intención.

Errores como `POWER_GUEST_AGENT_UNAVAILABLE`, `POWER_ACPI_TIMEOUT`, `POWER_RESULT_MISMATCH`, `DELETE_STOPPED_KVM_REQUIRED` o `PROVISIONING_*` son técnicos. Ante `uncertain`, no cree otra instancia ni repita la mutación: consulte la misma operación para reconciliación.

## Administración

La consola de `/docs` genera el JSON y el cURL, consulta el progreso y asocia automáticamente las acciones `start`, `shutdown`, `reboot` y `DELETE` a la ficha de la instancia. Cada mutación tiene una clave propia; las consultas GET no requieren clave.

## Controles conservados

La eliminación de decisiones comerciales no elimina autenticación Bearer, tenancy, RBAC técnico, capacidad física, locks y state Terraform, protección de VM 104/106/29000, idempotencia, auditoría ni reconciliación.
