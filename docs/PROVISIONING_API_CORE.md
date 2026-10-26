# Guía breve — Provisioning API Core

IACP recibe solicitudes técnicas autenticadas y administra su ciclo de vida sobre destinos autorizados. El CRM o portal consumidor conserva las decisiones de venta, pedidos, precios y facturación; esos datos no son necesarios para crear una KVM.

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

## Administración

La consola de `/docs` genera el JSON y el cURL, consulta el progreso y asocia automáticamente las acciones `start`, `shutdown`, `reboot` y `DELETE` a la ficha de la instancia. Cada mutación tiene una clave propia; las consultas GET no requieren clave.

## Controles conservados

La eliminación de decisiones comerciales no elimina autenticación Bearer, tenancy, RBAC técnico, capacidad física, locks y state Terraform, protección de VM 104/106/29000, idempotencia, auditoría ni reconciliación.
