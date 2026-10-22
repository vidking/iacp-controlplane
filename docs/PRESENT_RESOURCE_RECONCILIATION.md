# Incidente: KVM presente clasificada RESOURCE_ABSENT

## Alcance y evidencia

Base revisada: release `8d75c7c223fb2e80dda8db938c902432553ca65e` de
`iacp-controlplane`. IaC permanece fijada en
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4`, Terraform 1.16.6 y provider
`bpg/proxmox` 0.116.1. No se cambian dependencias, backend ni módulo IaC.

Evidencia comunicada por el operador; no se volvió a consultar infraestructura:

| Vínculo | Valor |
|---|---|
| Instancia | `a1b2d063-6240-4969-bc04-e7e0e6be9e73` |
| Intento | 4 |
| Approval ID | `1750ee70-a6ca-4036-a764-c4b077a0b588` |
| SHA-256 del plan | `3e05b23314ab036d526f802fb81f5f01e125cd501ee8cda5fb82dc741225052c` |
| State | lineage `d954af88-6ae6-2d04-9ead-55ed1f95d644`, serial 3, un recurso |
| Address | `module.kvm.proxmox_virtual_environment_vm.this` |
| Identidad | Terraform ID `106`, VMID 106, nodo `mng01`, nombre `iacp-kvm-lab-01` |

El operador informó HTTP 200 al consultar Proxmox desde el worker, CPU 2, RAM
2048 MiB, disco `DS_CYBERFUEL:vm-106-disk-0` de 20 GiB, `vmbr0`/VLAN 204,
Cloud-Init `190.0.230.162/24`, gateway `190.0.230.1`, tags `iacp;lab`.
Estas evidencias justifican investigar la interpretación del resultado, sin
autorizar otro Apply ni afirmar que todos los controles de integridad ya pasaron.

## Causa raíz demostrada

En `app/iacp/terraform/executor.py`, `TerraformExecutor.observe()` ejecutaba
`plan -refresh-only`, inspeccionaba su binario con `show -json` y pasaba únicamente
`planned_values` a `observed_resource()`. Terraform 1.16.6 no genera cambios de
recursos en ese modo: `planned_values.root_module` queda vacío aun existiendo la
VM. Los valores refrescados están en `prior_state.values`. `observed_resource()`
convertía ese vacío en `RESOURCE_ABSENT`, sin leer el state remoto ni consultar el
inventario nativo para distinguir presencia parcial. La recursión de módulos ya
existía; el error era elegir el contenedor JSON equivocado.

Esto coincide con las implementaciones oficiales fijadas:
[refreshOnlyPlan](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/terraform/context_plan.go),
[marshalPlannedValues](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/command/jsonplan/values.go)
y [serialización del prior state](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/command/jsonplan/plan.go).

Reproducción antes de corregir: al sustituir la salida artificial de las pruebas
por `planned_values: {root_module: {}}` y el recurso bajo
`prior_state.values.root_module.child_modules`, las dos variantes de
`test_historical_and_current_manifests_reconcile_without_rewriting` fallaron con
`RESOURCE_ABSENT` en `executor.py:observe -> observed_resource`.

Además se ejecutó Terraform CLI 1.16.6 oficial, con checksum verificado, en
`/tmp/iacp-reconcile-local-proof`. Solo se usó el recurso integrado
`terraform_data`, un módulo local y un state sintético escrito como fixture.
Los únicos comandos fueron `init -backend=false`, `plan -refresh-only` y
`show -json`; no se ejecutó Apply, import ni destroy. El resultado real fue:

```json
{
  "terraform_version": "1.16.6",
  "planned_values": {"root_module": {}},
  "complete": true,
  "errored": false
}
```

El recurso estaba en `prior_state.values.root_module.child_modules[0].resources`.
Se verificó igualdad de los bytes del state antes y después. La salida completa
se conserva en `tests/fixtures/terraform_1_16_6_refresh_only.json`; contiene solo
datos sintéticos del provider integrado. Las pruebas sustituyen los atributos
por la fixture KVM, conservando la forma real del documento. Para reproducirla,
ver `tests/fixtures/README.md`.

## Auditoría del flujo y exclusiones

| Fase | Archivo y funciones | Hallazgo |
|---|---|---|
| HTTP / autorización | `api/applies.py:apply_plan`, `terraform/apply.py:dispatch` | Valida tenant, rol, independencia, aprobación y clave; persiste queued antes de publicar; 202 no significa éxito |
| Cola | `api/dependencies.py:get_apply_publisher`, `terraform/worker.py:apply_instance` | Cola terraform; modo apply/reconcile explícito; sin autoretry de Apply |
| Exclusión y consumo | `terraform/apply.py:execute_apply`, `_execute`, `before_mutation`; `terraform/locks.py:instance_lock` | Lock PostgreSQL por instancia; started_at se confirma antes de mutar, impidiendo repetir Apply tras una caída |
| Proceso | `executor.py:prepare_apply`, `apply`; `runner.py:apply_saved`, `_execute` | SHA-256, intento, revisión, backend, variables y contexto; Apply fijo del binario aprobado con lock S3, límites y terminación de subprocesos |
| Observación defectuosa | `executor.py:observe`, `observed_resource` | Leía planned_values de refresh-only; no hacía state pull ni inventario nativo independiente |
| Persistencia | `terraform/apply.py:_execute` | Resultado, autorización, job, operación, instancia y evento se confirman en una transacción posterior al proceso Terraform |
| Recuperación | `terraform/apply.py:recover_apply`, `_execute` | Publica reconcile nuevo, verifica artefactos otra vez y observa; no reutiliza el resultado anterior ni vuelve a aplicar |
| Cierre de ausencia | `terraform/apply.py:resolve_absence` | Sigue requiriendo approver independiente, evidencia externa y clave idempotente; ahora exige también ambas observaciones negativas recientes |

Hay un intervalo inevitable entre el fin de Terraform y la transacción de
resultado: una caída deja running/uncertain con autorización consumida. No se
demostró que ese intervalo haya causado el incidente original; la prueba nueva
simula una caída ahí y verifica recuperación sin segunda mutación. El falso
RESOURCE_ABSENT posterior al Apply sí queda explicado por el defecto reproducido.

Backend: `verify_artifacts()` exige que la asignación persistida, el manifest y
`source/stacks/kvm-instance/backend.tfbackend.json` coincidan exactamente con
`Allocation.backend.arguments(execution.state_key)`. Es S3 Wasabi, cifrado,
`use_lockfile=true`. `initialize()` usa ese archivo con `init -reconfigure`.
Workspace Terraform: **default**, fijado en `environment()`, con `TF_DATA_DIR`
exclusivo de `<artifacts>/<instance>/attempt-4/data`. El directorio del intento no
es un workspace Terraform adicional. La state key se conserva desde
`lifecycle.initial_execution()`:
`iacp/lab/tenants/<tenant_id>/instances/<instance_id>/terraform.tfstate`.
El bucket y tenant concretos del incidente no fueron suministrados ni consultados;
no se inventan sus valores. La conciliación registra bucket, key y workspace
verificados para permitir correlacionarlos operativamente.

Credenciales: el mismo Terraform Worker construye el entorno de Apply y de
observación mediante `TerraformExecutor.environment()` a partir de
`IACP_TERRAFORM_CREDENTIALS_FILE`; fija credenciales AWS/Proxmox y CA autorizada,
sin heredar workspace, argumentos Terraform ni perfiles del shell. La identidad
de lectura puede haber rotado respecto al plan histórico, por diseño existente.
La corrección reutiliza **el mismo objeto de entorno** para state pull, refresh y
GET nativos. No hay evidencia de otro bucket, workspace o credenciales erróneas
en este incidente.

`execution_enabled: false` era metadato de `prepare_reconcile()`: indica que no
se autoriza mutación; nunca suprimió `observe()`. Sí había otra condición real:
`lifecycle.build_executor()` rechazaba incluso observar con PLAN_ENABLED=0. Se
añade el modo interno `observation=True`, usado exclusivamente por reconcile;
Plan y Apply nuevos conservan sus gates. Una prueba usa la factory real con
ambos flags en 0, evitando el falso positivo de un ejecutor inyectado.

## Comportamiento corregido

`observe()` lee state remoto v4 con `state pull`, valida el address de módulo,
instancia única no tainted/deposed/indexada, ambos `id` y `vm_id`, nodo y nombre.
Un error de lectura o JSON inválido nunca se convierte en ausencia. El comando
nuevo permitido por `CommandRunner.run()` es exactamente `state pull`.

La observación nativa en `terraform/observation.py:proxmox_resource()` realiza
solo GET, verifica TLS con la misma CA y token del worker, no sigue redirects y
acota tiempo y tamaño. Consulta inventario de clúster y configuración del VMID,
comprueba tipo qemu, nodo, nombre, estado y ausencia de lock de tarea. Una lista
vacía requiere VM.Audit propagado en `/vms` para evitar inferir ausencia a partir
de un inventario filtrado. Ver [permisos efectivos](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm)
y [filtrado de inventario](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm).

| State | Proxmox | Resultado |
|---|---|---|
| Presente | Presente | Refresh completo, sin acciones, lee prior_state.values y exige identidad, CPU, RAM, disco y datastore autorizados; completed/ready únicamente si todo coincide |
| Vacío | Ausente con alcance verificado | uncertain / RESOURCE_ABSENT; habilita solo la revisión controlada de ausencia, nunca un Apply automático |
| Vacío | Presente | uncertain / UNMANAGED_RESOURCE; no adoptar, importar ni crear otra VM |
| Presente | Ausente con alcance verificado | uncertain / STATE_RESOURCE_MISSING; drift, nunca éxito |
| Error en alguna fuente | Desconocido | uncertain con fase/código/status HTTP; nunca ausencia demostrada |
| Presente en ambas, refresh sin recurso | Presente | uncertain / PROVIDER_OBSERVATION_INCONSISTENT; investigar alcance/provider |

El estado de potencia se obtiene del inventario observado. Se vuelve a leer el
state después de observar y se exige igualdad; cambios durante el intervalo
producen STATE_CHANGED_DURING_OBSERVATION. Esto detecta cambios concurrentes,
pero no constituye una transacción distribuida con Proxmox. La recuperación
operativa exige detener escritores externos y tareas mutantes. No se relaja el
lock de Terraform: refresh-only puede crear y retirar `.tflock`, sin escribir el
contenido del state. No se aplican los planes de observación.

Diagnósticos persistidos: fase, subcomando, exit code, status HTTP y presencias
conocidas; metadatos verificados de conciliación y serial en caso de éxito.
No se guardan stdout, stderr, argumentos, token, cuerpos HTTP ni atributos secretos
del state. Un resultado RESOURCE_ABSENT del release anterior no basta para
`resolve-absence`: requiere otra conciliación con ambas observaciones negativas.

## Recuperación segura del intento 4

Procedimiento para operación posterior al PR y despliegue autorizado; **no se
ejecutó desde desarrollo**:

1. Mantener `IACP_TERRAFORM_APPLY_ENABLED=0` en API y worker. Confirmar que no
   quede ningún Apply, clonación ni escritor externo activo sobre VMID 106/state.
   Preservar artefactos originales del intento 4, autorización, eventos y evidencia
   privada de state/Proxmox. No alterar PostgreSQL, manifest, plan, state ni locks.
2. Revisar y desplegar la misma imagen corregida en API y Terraform Worker.
   No requiere migración. Conservar el volumen de planes y fuente fijada, las
   credenciales vigentes del worker y CA con el alcance de lectura autorizado.
   PLAN_ENABLED puede permanecer en 0. No ampliar permisos para forzar un cierre.
3. Verificar por API el intento 4 y el SHA-256 completo de la tabla, y comprobar
   privadamente el hash del saved.tfplan original. Confirmar bucket/key/endpoint
   de asignación, manifest y backend local; los tres deben coincidir.
4. Una identidad operator del mismo tenant solicita
   `POST /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform/applies/1750ee70-a6ca-4036-a764-c4b077a0b588/recover`.
   El 202 confirma publicación/aceptación; consultar `GET .../terraform/applies`,
   instancia y operación hasta terminar la observación. No solicitar otro Apply.
5. Para completed, exigir hash/intento correctos, `workspace=default`, bucket/key
   esperados, `state_present=true`, `proxmox_present=true`, origen
   `prior_state.values`, VMID 106, mng01 y nombre esperado. Comparar recursos con
   la asignación aprobada; ready no certifica disponibilidad del invitado.
6. Cualquier discrepancia, cambio de state, permiso insuficiente o error conserva
   uncertain y el bloqueo de reaprovisionamiento. Corregir la observabilidad o
   configuración externa mediante el procedimiento autorizado y repetir recover
   sobre el mismo run. No manipular filas SQL ni regenerar la aprobación.
   La evidencia actual de presencia en ambos sistemas **no permite** usar
   resolve-absence. La recuperación real queda pendiente del despliegue revisado.

## Riesgos y rollback

Hay dos lecturas adicionales de S3 y hasta dos GET Proxmox por observación.
Un alcance limitado, CA incorrecta, API incompatible, tarea activa, respuesta
incompleta o cambio concurrente puede mantener uncertain; es el resultado seguro.
No se obtiene atomicidad distribuida ni prueba de salud del sistema invitado.
La prueba HTTP usa mocks; no se afirma validación contra Proxmox/Wasabi reales.

Rollback: detener solicitudes de recuperación y drenar workers; mantener Apply
deshabilitado y volver a la imagen `8d75c7c` compatible con el mismo esquema.
No hay migración que revertir ni state que restaurar. Conservar volúmenes,
autorizaciones, VMID y evidencia. El rollback reintroduce el falso RESOURCE_ABSENT:
no usar el cierre de ausencia del release anterior para ese resultado sin revisión
independiente; suspender esa operación hasta desplegar nuevamente la corrección.

## Validación

- Regresión roja en el release base: dos variantes fallaron con RESOURCE_ABSENT
  al recibir la forma JSON correcta, antes de implementar la corrección.
- Terraform CLI 1.16.6 local: reprodujo planned_values vacío con recurso presente
  en prior_state; state sintético sin cambios. No usa Proxmox ni Wasabi.
- `bash scripts/test-integration.sh`: **273 passed**, Python 3.12.15,
  PostgreSQL 17 y Redis 7 desechables, Celery real, Terraform/HTTP simulados.
  Incluye 60 casos nuevos en `test_present_resource_reconciliation.py` y las
  regresiones históricas corregidas. Una advertencia Starlette/AnyIO preexistente.
- `python scripts/validate-handbook.py`: diez documentos y nueve contratos
  autenticados aprobados, sin red. `git diff --check`: sin errores.
- No se midió cobertura porcentual ni se ejecutó recuperación, despliegue o
  mutación de infraestructura real.
