# Estabilización del contrato de aprovisionamiento

Base auditada: `61e6bcb` (24/10/2026). Inventario realizado antes de editar rutas.
La implementación sigue siendo Terraform CLI. Este documento es administrativo;
la integración comercial usa CRM_INTEGRATION.md y /openapi.json.

## Inventario y consumidores

Todas las rutas siguientes estaban registradas en main.create_app, publicadas en
Swagger/OpenAPI autenticados y alcanzables por el router PathPrefix de Traefik.
No existe una ACL de red por ruta que convierta include_in_schema en seguridad.
Los permisos previos son del tenant; reader podía leer y operator mutar.

Prefijo original: `/v1/instances/{instance_id}/terraform`.

| Método y sufijo anterior | Router y handler | Permiso anterior | Sufijo neutral bajo provisioning |
|---|---|---|---|
| GET (raíz) | api/terraform.py:get_execution | reader/operator/approver | /plan |
| POST /cancel | api/terraform.py:cancel_execution | operator | /plan/cancel |
| POST /retry | api/terraform.py:retry_execution | operator | /plan/retry |
| POST /approvals | api/applies.py:approve_plan | approver independiente | /approvals |
| POST /apply | api/applies.py:apply_plan | operator solicitante | /executions |
| GET /applies | api/applies.py:list_applies | reader/operator/approver | /executions |
| POST /applies/{approval_id}/cancel | api/applies.py:cancel | operator | /executions/{approval_id}/cancel |
| POST /applies/{approval_id}/recover | api/applies.py:recover | operator | /executions/{approval_id}/recover |
| POST /applies/{approval_id}/resolve-absence | api/applies.py:resolve | approver independiente | /executions/{approval_id}/resolve-absence |
| POST /deletion-plan | api/deletion.py:plan | operator | /deletion-plan |
| POST /deletion-approval | api/deletion.py:approve | approver independiente | /deletion-approval |
| POST /deletions/{operation_id}/cancel | api/deletion.py:cancel | operator | /deletions/{operation_id}/cancel |
| POST /deletions/{operation_id}/recover | api/deletion.py:recover | operator | /deletions/{operation_id}/recover |

Consumidores comprobados: tests/test_terraform.py, test_apply.py,
test_historical_recovery.py, test_present_resource_reconciliation.py,
test_readonly_reconciliation.py, test_deletion.py, test_dhcp_profiles.py,
test_lab_self_service.py y pruebas de integración; scripts/validate-handbook.py;
manuales AUTHORIZED_APPLY, DHCP_RECOVERY, SAFE_DELETE, SELF_SERVICE y handbook.
Los scripts de observación VM106 usan modelos internos, no estas rutas.
El cliente Locust del repositorio hermano iacp-load-testing usa instancias,
operaciones, Lifecycle y DELETE; no usa rutas del motor ni interpreta executor.
No se encontró integración CRM externa instalada; CRM_INTEGRATION documenta un
cliente futuro. No se deduce ausencia global de consumidores de un grep o logs
acotados. Las evidencias históricas no se reescriben.

El CRM necesita POST/GET instances, GET operations/jobs/profiles/policies/usage,
Lifecycle, DELETE, provision/retry-provision/reconcile, network y access/verify.
No necesita las 13 rutas administrativas anteriores. Lab policy administration
se conserva separada de consulta de políticas, perfiles y uso.

## Entidades y compatibilidad

TerraformExecution es planificación por instance_id; TerraformAttempt representa
un intento numerado. TerraformApply.id es approval_id: la autorización de un uso
y su ejecución comparten esa fila y sus fences. No se inventa execution_id.
operation_id y job_id conservan su entidad. DeletionExecution usa operation_id.
Se mantienen cuerpos, códigos HTTP, idempotencia, separación de solicitante y
approver, tiempos, estados y límites de tenant. El backend y el worker no cambian.

La transición de consumidores administrativos conocidos se realiza en este PR:
rutas neutrales, mismos handlers y servicios. Las rutas originales se retiran y
responden 404; no hay alias documentales accesibles al CRM. Antes de una promoción
externa, el propietario debe contrastar su registro de consumidores; si aparece
un consumidor externo, posponer su retirada y acordar una transición privada.

Desarrollo confirmó en esta sesión que no conoce dependencia del enum executor.
Se conserva el campo y se publica simulated/provisioning. Se conserva
provisioning_service con el nombre oficial provisioning. Los identificadores y
payloads de entrada de negocio no cambian. Resultados de auditoría/state se
proyectan a evidencia comercial; los detalles íntegros permanecen internos.

## RBAC y documentación

`/docs` y `/openapi.json` describen consumidores. `/admin/docs` y
`/admin/openapi.json` describen también las 17 operaciones administrativas y
requieren identidad administrativa. ReDoc sigue deshabilitado. Para el navegador
administrativo: Basic usuario `iacp-admin`, token administrativo como contraseña;
para automatización: Bearer. Las identidades docs-only no adquieren ese permiso.
El esquema no concede privilegios y el backend vuelve a autorizar cada llamada.

El registro existente admite el atributo opcional `administration` (false por
defecto). Operator/reader del CRM no lo reciben. Un operator administrativo lo
necesita para planes y propuestas de políticas; reader administrativo solo lee.
Approver conserva su función administrativa independiente. Bootstrap permanece
como operador de plataforma para recuperación legítima, nunca como identidad CRM.
No se cambia almacén de identidades, tokens, roles existentes ni sus valores.
El grant se configura fuera de Git por el propietario del registro; no puede
solicitarse por body, header o query. No se requiere modificar Compose.

ProvisioningService es una frontera pequeña sobre las funciones comprobadas;
no añade adaptadores, selección de motores ni ejecución duplicada. Las mejoras
internas en lab/profiles, ORM y publishers se posponen porque exigirían tocar
lógica crítica antes del MVP. Workers, colas, migraciones, state y módulos no se
renombran ni reimplementan. `provisioning-invariants.json` fija sus checksums.

## Validación y límites

El diff reproducible se genera con `scripts/diff-provisioning-contract.py`;
`contracts/openapi-before-provisioning.json` conserva el contrato previo y
`openapi.json`/`openapi-admin.json` los posteriores. Los cambios permitidos son
separación administrativa, rutas neutrales y los valores de servicio acordados.
No se certifica compatibilidad de un consumidor externo desconocido.
Los logs consultados de API y proxy (últimas 48 h, máximo 12 000 líneas por
contenedor) no mostraron uso de rutas heredadas; esa ventana no sustituye un
registro externo de consumidores.

También se encontraron nueve clientes administrativos locales de soporte en
`/tmp` con llamadas heredadas: inventario y checksums en
[internal-client-migration.json](contracts/internal-client-migration.json).
Se migraron únicamente sus literales de ruta HTTP y se comprobó sintaxis, con
copia previa privada. No se ejecutaron sus acciones sobre VMs históricas.
Los scripts de preparación que mencionan directorios/state no son consumidores
HTTP y permanecieron intactos. Esta actualización de los clientes conocidos se
realiza antes del cambio del runtime; no justifica mantener alias públicos.

Los eventos publicados traducen terraform_* a provisioning_* y los actores de
worker al servicio neutral. Los códigos internos de error se proyectan igual;
los registros persistidos conservan su valor original. Los jobs conservan sus
seis campos y presentan mensajes neutrales. Estas traducciones y la proyección
explícita de result forman parte del cambio contractual documentado.
