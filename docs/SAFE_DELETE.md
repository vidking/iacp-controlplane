# DELETE de KVM descartable y piloto funcional

Esta rama propone API 0.7.0 y Alembic 0007_safe_deletion. **No está desplegada** por
el piloto: VM 104 conserva e03807d y schema 0006. No se borró ninguna VM empresarial.
Los resultados reales del piloto se separan de las pruebas con fixtures en
[el registro de validación](validation/PILOT_FUNCTIONAL.json).

## Contrato y límites

| Acción | Endpoint | Rol | Efecto |
|---|---|---|---|
| Preparar | POST /v1/instances/{instance_id}/provisioning/deletion-plan | operator | Plan destroy guardado; sin Apply |
| Revisar | GET /v1/operations/{operation_id} | reader/operator/approver del tenant | deletion.phase, digest y auditoría |
| Aprobar | POST /v1/instances/{instance_id}/provisioning/deletion-approval | approver distinto del solicitante | Aprobación ligada a plan e instancia, 60–3600 s |
| Ejecutar | DELETE /v1/instances/{instance_id} | operator original | Aplica exclusivamente el plan aprobado |
| Reconciliar | Mismo DELETE o POST /provisioning/deletions/{operation_id}/recover | operator original | Después de started_at, solo lectura |
| Cancelar | POST /provisioning/deletions/{operation_id}/cancel | operator original | Solo antes de started_at; conserva plan e historia |

DELETE está OFF por defecto y requiere Apply=1 más IACP_TERRAFORM_DELETE_ENABLED=1
en API y Terraform worker. Requiere UUID exacto en IACP_DISPOSABLE_INSTANCE_IDS,
VMID >=30000 y propiedad acreditada por el Apply de creación completed con SHA
actual, asignación lab y postcondiciones verificadas. VM 104 y VM 106 se rechazan
en admisión, worker y verificación del plan; la tabla añade una restricción SQL.
La KVM debe estar detenida, sin pending, lock, pausa o HA; no se apaga a la fuerza.

El worker crea un directorio nuevo `UUID/deletion-OPERATION_UUID`, conserva intactos
los planes de creación y usa el backend/state key de su registro de propiedad.
Exige un único resource_changes con actions=[delete], dirección KVM y VMID/nodo/
nombre exactos; rechaza reemplazos, imports, recursos adicionales o planes incompletos.
Antes del envío relee el state versionado, la identidad y la configuración PVE,
valida plan binario, variables, backend y contexto de credenciales, y consume la
autorización en PostgreSQL antes de ejecutar Terraform. No invoca DELETE nativo.

La reserva usa el advisory lock existente y un índice único; bloquea Lifecycle.
Todas las mutaciones vuelven a comprobar tenant, flags y vínculo de propiedad.
Tras interrupción o fallo de Terraform/Proxmox después del fence, queda uncertain
con la instancia deleting. El replay solo consulta Wasabi y Proxmox, sin init,
plan, Apply, force-unlock ni modificación de state. Completed exige ausencia en
ambos sistemas, lineage original, serial avanzado y dos lecturas S3 coherentes.
La ausencia PVE exige VM.Audit heredable en /vms, no un inventario parcial por ACL.
Se conserva el inventario como tombstone deleted, su VMID y su historia original.
No se libera ni recicla automáticamente una IP, VMID o nombre.

## Promoción y activación separadas

Desplegar una release revisada con el driver oficial, drenado, backup y comparación,
y aplicar 0007 únicamente en la ventana autorizada. No se ha ejecutado esa promoción.
La comparación solo tolera introducir las nuevas variables OFF/vacías al actualizar;
no tolera activarlas como drift. DB/Redis/Traefik no se recrean. El rollback mantiene
0007 y la release compatible; e03807d no es rollback binario compatible tras 0007.
No se elimina la tabla ni la auditoría. Un resultado uncertain requiere observación.

La habilitación requiere acción `enable-delete` vigente, admisión=0, Lifecycle=0,
workers ociosos y deletion_review con path/sha256 reales en el manifest. Ese archivo
es una revisión explícita del operador, ligada a release y change_reference:

```json
{
  "approved": false,
  "release": "COMMIT_COMPLETO_REVISADO",
  "change_reference": "REFERENCIA_REAL",
  "instance_ids": ["UUID_DESCARTABLE_CONFIRMADO"],
  "identities": {
    "UUID_DESCARTABLE_CONFIRMADO": {"vmid": 30000, "node": "mng01", "name": "NOMBRE_CONFIRMADO"}
  }
}
```

Es un ejemplo pendiente, no una aprobación. Generar los valores desde la nueva
instancia del Frente A y comparar su identidad antes de aprobar. `iacpctl enable-delete`
verifica los registros reales y promueve flag/allowlist mediante el driver existente,
recreando únicamente API/Terraform worker y conservando admisión y Lifecycle OFF.
No hay otro Compose ni orquestador. API y worker vuelven a validar state/PVE al ejecutar.

Para abrir admisión sin habilitar Lifecycle existe `iacpctl admit`, con acción
`enable-admission`, admisión/Lifecycle inicialmente OFF y admission_review revisado:

```json
{
  "approved": false,
  "release": "COMMIT_COMPLETO_REVISADO",
  "change_reference": "REFERENCIA_REAL",
  "allocation_file": {"path": "/etc/iacp/terraform/allocations.json", "sha256": "DIGEST_REAL"},
  "targets": ["TENANT_UUID/NOMBRE_NUEVO_AUTORIZADO"]
}
```

Valida el archivo montado y su hash, tenant activo, nombre nuevo, VMID >=30000
sin duplicación, IPv4 sin duplicación entre asignaciones y ausencia PVE con alcance
suficiente. Allocation rechaza DHCP y VLAN ausente: este comando no fabrica soporte.
Ante fallo de readiness después de abrir, cierra admisión y conserva Apply=1/Lifecycle=0.
`doctor` contrasta los flags configurados y ejecutados; su PASS no sustituye una autorización.

## Ejecución autenticada en Swagger

Basic permite entrar en /docs. En **Authorize** introducir únicamente el token
operator del tenant, sin prefijo Bearer. El password docs no es un token API.
No habilitar persistAuthorization ni pegar secretos en ejemplos, Git o evidencias.

1. Crear la VM descartable mediante el contrato de creación y su asignación aprobada.
   HTTP 202 no es una VM ready: seguir plan, aprobación y Apply hasta completed.
2. Confirmar UUID, VMID, nodo, nombre, propiedad y apagado ordenado usando
   `power/shutdown`, no `/stop`. Lifecycle exige su certificación/habilitación aparte.
3. Preparar deletion-plan con Idempotency-Key única y conservar el operation_id.
4. Leer GET /v1/operations/{operation_id}: esperar deletion.status=planned y anotar
   plan_sha256. El aprobador revisa evidencia privada de plan/state/PVE, no solo el digest.
5. Como approver distinto, POST deletion-approval con operation_id, plan_sha256 y
   valid_for_seconds. Esta aprobación no ejecuta Terraform.
6. Como operator original, DELETE con esos mismos operation_id/plan_sha256 y la
   **misma clave de deletion-plan**. Esperar 202 y consultar la operación hasta terminal.
7. Repetir idéntico DELETE para idempotencia. Si uncertain, esa misma repetición
   solo observa; nunca usar otra clave para sortear el bloqueo.

401 autenticación, 403 rol/tenant, 404 recurso de otro tenant, 409 propiedad/plan/
aprobación/clave/concurrencia/feature OFF, 422 cuerpo o clave inválidos, 503 broker
no confirmado o admisión cerrada. Nunca enviar force, nodo o VMID en el cuerpo.

La futura prueba real queda limitada a la nueva KVM descartable, después de su
confirmación explícita. Esta rama no registra una eliminación real como aprobada.

Los endpoints cancel/recover anteriores conservan autenticación y propiedad durante
el drenado: son las únicas excepciones nuevas al cierre de admisión. Cancel no
libera una mutación consumida; recover nunca puede iniciar una mutación. Un plan
caducado se cancela antes de preparar un nuevo intento con otra clave revisada.
