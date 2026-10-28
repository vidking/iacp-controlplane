# Guía breve — Provisioning API Core

IACP recibe solicitudes técnicas autenticadas y administra su ciclo de vida
sobre destinos autorizados. El CRM o portal consumidor conserva compras,
productos, cuotas comerciales, pedidos, precios, reglas de negocio y
facturación; esos datos no son necesarios para crear una KVM.

## Conexión

- Base URL: `https://iac.cyberfuel.com`
- Autenticación: `Authorization: Bearer <token>` de una identidad técnica con
  rol `operator`.
- Cada mutación usa una `Idempotency-Key` nueva. Un reintento de la misma
  intención reutiliza exactamente la misma clave; una acción diferente
  (`start`, `shutdown`, `reboot` o `DELETE`) usa otra. Las consultas `GET` no
  requieren la cabecera.

## Crear una instancia

1. Obtenga el catálogo con `GET /v1/plans` y `GET /v1/images` usando el Bearer
   de la identidad técnica.
2. Seleccione un plan y una Golden Image cuya disponibilidad sea `true`.
3. Envíe `POST /v1/instances` por HTTPS con una `Idempotency-Key` nueva por
   intención:

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

`tenant_id`, VMID, nodo, almacenamiento, worker y executor los obtiene IACP de
la identidad y del catálogo. `network` y `commercial_context` son opcionales;
el segundo solo sirve como correlación histórica y no autoriza nada.

Un `202` confirma la admisión, no la disponibilidad. Conserve `instance_id`,
`operation_id` y `job_id`; consulte la operación y la instancia hasta un estado
terminal. No repita `apply` mientras el resultado sea `uncertain`.

El flujo efectivo de `provisioning-api-core` es: autenticación técnica,
validación, catálogo, capacidad física, reserva de VMID, Terraform plan/apply,
Cloud-Init, QGA y SSH. `READY` solo se publica con la evidencia persistida.
IACP no consulta ni exige `LabPolicy`, `policy_sha256`, aprobación independiente,
renovación, caducidad o `activate-lab`, y no aplica cuotas comerciales.

## Seguimiento y ciclo de vida

1. `GET /v1/operations/{operation_id}` muestra `queued`, `running`, `completed`,
   `failed` o `uncertain`, junto con eventos, fase Terraform y errores.
2. `GET /v1/instances/{instance_id}` muestra VMID, nodo, recursos, estado y
   direcciones DHCP observadas.
3. `GET /v1/instances/{instance_id}/power` consulta el estado efectivo de
   Proxmox.
4. `POST /v1/instances/{instance_id}/power/start`, `/shutdown` o `/reboot`
   ejecuta Lifecycle con una clave propia.
5. `DELETE /v1/instances/{instance_id}` solicita la eliminación auditada y
   espera `instance.delete` en estado `completed` antes de liberar la
   intención.

La misma identidad técnica puede solicitar Lifecycle y DELETE. Apply y destroy
siguen siendo Terraform controlados; la delegación automática se audita con
`iacp-automation:provisioning-api-core`, una identidad interna que no representa
a una persona ni fabrica una aprobación humana.

Errores como `POWER_GUEST_AGENT_UNAVAILABLE`, `POWER_ACPI_TIMEOUT`,
`POWER_RESULT_MISMATCH`, `DELETE_STOPPED_KVM_REQUIRED` o `PROVISIONING_*` son
técnicos. Ante `uncertain`, no cree otra instancia ni repita la mutación:
consulte la misma operación para reconciliación.

## Compatibilidad histórica

Las tablas y registros de `LabPolicy` se conservan para consulta y para operar
instancias históricas que todavía estén ligadas a ellas. Bajo Core, las rutas
administrativas de propuesta, aprobación, renovación y revocación responden
`410 LAB_POLICY_ADMINISTRATION_DISCONTINUED`; las consultas históricas siguen
disponibles. Ninguna política antigua bloquea una nueva solicitud Core.

Cada VM conserva su `source_revision`, state, ownership y auditoría originales.
Un release nuevo no adopta ni reescribe states históricos: Lifecycle y DELETE
solo avanzan cuando la vinculación técnica y la compatibilidad están verificadas.

## Administración y promoción

La consola de `/docs` genera el JSON y el cURL, consulta el progreso y asocia
automáticamente las acciones `start`, `shutdown`, `reboot` y `DELETE` a la ficha
de la instancia. Cada mutación tiene una clave propia; las consultas GET no
requieren clave.

`scripts/go_live.py promote` ejecuta preflight, drena temporalmente la admisión,
verifica workers/servicios/schema/history y restaura automáticamente la admisión,
Lifecycle y DELETE que estaban habilitados. Para Core deja
`IACP_LAB_SELF_SERVICE_ENABLED=0`, porque ya no es un gate del producto. No se
detienen PostgreSQL, Redis ni Traefik y no se modifican Golden Images, VM 106,
VM 29000 ni el state.

## Controles conservados

La eliminación de decisiones comerciales no elimina autenticación Bearer,
tenant isolation, RBAC técnico, ownership, catálogo, capacidad física, locks,
fencing, state Terraform, protección del IACP Control Plane (VM 104), VM 106,
VM 29000, idempotencia, auditoría ni reconciliación.
