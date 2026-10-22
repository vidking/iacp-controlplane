# Reconciliación de solo lectura y consistencia del state

## Alcance y evidencia

Base examinada: `main` en `74a1e92a9a6b9fd4614c547c748dfba328b77c2d`,
incluido PR #16. Esta corrección no despliega, no recupera operaciones reales,
no modifica VMID 106 y no cambia `IACP_TERRAFORM_APPLY_ENABLED=1`.

Se demostró un defecto de comparación que produce exactamente
`STATE_CHANGED_DURING_OBSERVATION` / `observe.state_verify` sin ninguna escritura
del objeto de state. Se confirmó que no se conservaron los dos snapshots
originales del incidente; esta reproducción no permite atribuirle una causa
productiva definitiva ni descartar un escritor externo. La recuperación real y
su evidencia siguen pendientes. La [validación final del PR #17](VALIDATION_PR17.md)
completó las pruebas antes omitidas sin modificar el código de producción.

## Causa raíz reproducida

`TerraformExecutor.observe()` hacía dos `terraform state pull`, decodificaba JSON
y exigía `latest == state`. Comparaba representaciones exportadas por Terraform,
no snapshots identificados del objeto Wasabi.

En Terraform 1.16.6, `StatePullCommand.Run()` lee el state y vuelve a serializarlo
mediante `statemgr.Export()` y `statefile.Write()`.
`encodeCheckResultsV4()` recorre mapas Go para producir `check_results` y sus
`objects`; `stateV4.normalize()` ordena recursos e instancias, pero no esos checks.
Dos procesos pueden exportar listas con distinto orden a partir de los mismos
bytes. Python compara listas por posición. El resultado es un falso conflicto.

Fuentes fijadas:
[state_pull.go](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/command/state_pull.go),
[version4.go, encodeCheckResultsV4 y normalize](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/states/statefile/version4.go).

La regresión determinista
`test_same_state_with_permuted_check_results_is_not_a_concurrent_write` falló
en el código base con el error y fase del incidente. Se ejecutó además:

```sh
.venv/bin/python scripts/reproduce-state-observation.py --terraform /ruta/terraform-1.16.6
```

Resultado observado con el CLI 1.16.6 y un módulo `terraform_data` integrado:

| Medida | Resultado |
| --- | --- |
| Lecturas del mismo archivo sintético | 32 |
| Objetos JSON distintos tras decodificar | 6 |
| Objetos distintos ordenando únicamente checks | 1 |
| Lineage / serial | constantes / 3 |
| Bytes del archivo antes/después de lecturas y refresh-only | idénticos |
| SHA-256 del archivo sintético | `a046448fc94dc67edfa2e602d2cb7b33bb1e369b6a857158b2a788c2e80feae9` |
| Recurso refrescado | `prior_state.values.root_module.child_modules` |

El script solo usa directorios temporales, estado sintético, init sin backend,
state pull, plan refresh-only y show. No instala providers ni contacta servicios.

| Hipótesis | Conclusión de la reproducción |
| --- | --- |
| El observador modifica el objeto de state | No ocurrió en la reproducción; bytes idénticos |
| Representaciones distintas del mismo state | Demostrado: permutación de checks |
| Modificación concurrente auténtica | No fue necesaria para reproducir; se prueba por separado con mocks |
| Interpretación incorrecta de refresh-only | PR #16 sigue siendo correcto: los valores están en prior_state.values |
| Otra causa | El diagnóstico antiguo no guardaba snapshots para atribuir la divergencia real |

El [plan backend de Terraform](https://github.com/hashicorp/terraform/blob/v1.16.6/internal/backend/local/backend_plan.go)
guarda el refresh dentro del binario del plan; su persistencia requiere una
aplicación posterior. La corrección nunca aplica ese binario.

## Flujo y responsabilidades

```mermaid
sequenceDiagram
  participant API
  participant PG as PostgreSQL
  participant W as Worker Terraform
  participant S3 as Wasabi
  participant PVE as Proxmox
  API->>PG: recover: cercar entregas apply pendientes
  API->>W: publicar reconcile(approval_id)
  W->>PG: advisory lock existente por instancia + reclamar run
  W->>W: verificar plan, manifest, variables, backend e intento
  W->>S3: GET actual: bytes + VersionId (A)
  W->>PVE: GET inventario y configuración
  W->>W: init y show del plan aprobado si ambas fuentes presentes
  W->>S3: plan refresh-only con lock nativo .tflock
  W->>W: show: prior_state.values + postcondiciones
  W->>S3: GET actual: bytes + VersionId (B)
  W->>W: exigir A == B; revalidar artefactos
  W->>PG: comprobar lock, run y vínculo; commit único
```

`state.py:S3StateReader` hace únicamente `GetObject`, con endpoint, bucket, key,
región, credenciales y CA del contexto verificado del worker. Usa path style,
TLS verificado, sin proxy ambiental, timeout y reintentos acotados. Lee como máximo
16 MiB, cierra el stream y el cliente, y conserva errores sin cuerpos ni secretos.
Lee contenido y Version ID de la misma respuesta, evitando una carrera HEAD/GET.
No pide una versión histórica: ambos GET solicitan la versión actual.
Solo requiere `s3:GetObject` sobre la key actual, no `GetObjectVersion` ni cambios
de permisos. Véase [contrato GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html).

La observación no ejecuta Terraform cuando el objeto falta, no es legible o alguna
de las dos fuentes no contiene el recurso. Así no inicializa un state para tratar
un error de lectura como ausencia. Un objeto v4 vacío y legible sí permite evaluar
ausencia del recurso mediante el procedimiento auditado existente.

`executor.py:observe` conserva refresh-only y la lectura de `prior_state.values`
del PR #16. `managed_resources` recorre módulos; `state_resource` valida el address
plano v4, una única instancia sin índice/deposed/taint, ID, VMID, nodo y nombre.
`verify_artifacts` vincula el directorio de Instance ID/intento al SHA aprobado,
revisión fijada, variables, backend completo y workspace `default`.
`approved_state_metadata` lee únicamente la cabecera `tfstate` sellada dentro del
binario aprobado 1.16.6, sin extraer archivos ni modificarlo. Si el plan contenía
un lineage, debe coincidir con el actual; el serial actual no puede ser inferior
al del plan. Un plan originalmente sin state admite cabecera vacía con serial 0,
sin inventar lineage retrospectivo. Esto conserva los manifests históricos.
La reproducción con CLI real también verificó esa cabecera del binario. Un
lineage reemplazado o lectura anterior al snapshot aprobado produce
`APPROVED_STATE_MISMATCH`, aunque las dos lecturas actuales sean estables.

`verify_postconditions` exige **todos los valores conocidos de change.after del
plan aprobado**, tanto en el state persistido como en los valores refrescados.
Respeta únicamente las máscaras explícitas de `after_unknown` para datos
calculados. Incluye red, VLAN, cloud-init, claves públicas, agente, pool, opciones
del disco y valores nulos cuando estén en el plan, además de identidad/CPU/RAM.
La interpretación de nulos opcionales y de la interfaz automática está acotada
al contrato bpg 0.116.1 y documentada por atributo en
[SEMANTIC_POSTCONDITIONS.md](SEMANTIC_POSTCONDITIONS.md); los valores sensibles
conocidos siguen sujetos a comparación. No se omiten campos para aceptar éxito.
No registra esos valores en diagnósticos. Las listas de atributos mantienen su
orden. `verify_checks` rechaza checks fallidos, desconocidos o con error.
El estado de encendido de Proxmox y del provider debe coincidir con el solicitado;
ya no se sobrescribe un resultado incompatible con el valor de la API nativa.

Provisioning conserva `apply()` y su autorización de un solo uso. Reconciliation
solo observa y confirma evidencias. Drift Detection no sincroniza el state:
una diferencia requiere investigación y, si procede, otra operación autorizada.

## Invariantes

| Invariante | Garantía |
| --- | --- |
| INV-01 | Reconcile solo usa GET S3, GET Proxmox, version, init, show y plan refresh-only; nunca apply/import/destroy/refresh |
| INV-02 | Ningún PUT/DELETE del objeto state ni edición de sus datos; Terraform sigue siendo su propietario |
| INV-03 | Identidades y artefactos vinculados a la aprobación; todas las postcondiciones conocidas deben cumplirse |
| INV-04 | Lock PostgreSQL por instancia, locking remoto nativo, versión inmutable y hashes de dos snapshots |
| INV-05 | Mensajes duplicados no reclaman un run terminal; recover no vuelve a consumir autorización de mutación |
| INV-06 | Divergencias y errores producen uncertain con diagnóstico específico; desconocido nunca equivale a ausencia |
| INV-07 | Crear/modificar, observar/recuperar y detectar drift conservan funciones separadas |

Terraform puede crear y retirar `.tflock` durante el plan. Es metadata de exclusión
del backend, no una escritura del objeto de state. Los artefactos locales de
observación son distintos del `saved.tfplan` aprobado, que no se sobrescribe.

## Consistencia y concurrencia

`StateSnapshot` conserva Version ID, lineage, serial, hash SHA-256 de bytes y hash
SHA-256 de contenido canónico completo. La canonicalización solo ordena recursos,
instancias y checks/objects estructurales. Conserva todos los valores y statuses;
no ordena listas arbitrarias del provider ni elimina metadata para lograr éxito.
Rechaza JSON ambiguo, números no finitos y cabeceras inválidas.

Se exige igualdad de **todos** los identificadores y hashes, además de Version ID
no vacío y distinto de `null`. `STATE_VERSION_UNAVAILABLE` conserva uncertain si
el backend no ofrece versionado inmutable; no se acepta el riesgo ABA a partir de
serial/hash solamente. Comprobar esta condición en Wasabi antes de la ventana.
La corrección no activa versioning ni cambia permisos automáticamente.

| Cambio de snapshots | Diagnóstico state_consistency.change | Decisión |
| --- | --- | --- |
| Todo idéntico, versión inmutable | unchanged | Permitir evaluar éxito |
| Lineage distinto | lineage_changed | uncertain |
| Serial mayor | serial_advanced | uncertain; no atribuir escritor solo por el serial |
| Serial menor | stale_or_regressed | uncertain; posible lectura obsoleta o rollback externo |
| Contenido diferente con igual serial | content_changed | uncertain |
| Contenido equivalente con otros bytes | representation_rewrite | uncertain; reevaluar con dos lecturas nuevas |
| Mismos bytes con otra versión | version_rewrite | uncertain; detecta rewrite/ABA versionado |
| Mismo Version ID con otros bytes | inconsistent_version_read | uncertain; lectura/backend incoherente |

Todos los cambios producen `STATE_CHANGED_DURING_OBSERVATION` y guardan ambos
snapshots en el diagnóstico. Una mera diferencia de representación **entre pulls**
no afecta la nueva decisión porque esos pulls ya no se usan. Una reescritura real
del objeto, aunque solo cambie su representación, requiere observación nueva.
El proceso no escribe state, por lo que no hay un cambio propio que eximir de
integridad. Sin auditoría externa no se atribuye la identidad del escritor.

`apply.py:execute_apply` conserva el `instance_lock` existente. En PostgreSQL es
un advisory lock de sesión con heartbeat; las pruebas SQLite usan el sustituto
local ya existente. No se añade otro motor de locks. Dos workers no observan la
misma instancia simultáneamente. `recover_apply` cerca mensajes Apply tardíos
antes de publicar reconcile, aunque la publicación falle.

Antes de confirmar, `_execute` revalida los artefactos aprobados y vuelve a
comprobar el vínculo persistido bajo locks de fila. Solo finaliza si el run sigue
`running`. Una cancelación concurrente se conserva. Un heartbeat fallido impide
el commit terminal y deja el run recuperable; perder exclusión no da derecho a
publicar un resultado. La transacción final actualiza run, job, operación,
instancia y evento juntos. Se registra approval_id, Instance ID, intento,
operation_id y evidencia de observación.
`cancel_apply` actualiza también la instancia a failed/uncertain cuando ya hubo
mutación; el worker no necesita sobrescribir esa decisión para quitar el estado
provisioning.

## Máquina de estados

| State | Proxmox | Resultado |
| --- | --- | --- |
| Recurso presente | Presente, consistente y postcondiciones satisfechas | completed / instancia ready |
| Recurso ausente en objeto legible | Ausente con alcance Audit suficiente | uncertain / RESOURCE_ABSENT; permite resolve-absence auditado |
| Ausente | Presente | uncertain / UNMANAGED_RESOURCE |
| Presente | Ausente | uncertain / STATE_RESOURCE_MISSING |
| Cambia durante observación | Cualquiera | uncertain / STATE_CHANGED_DURING_OBSERVATION |
| Sin versión inmutable | Cualquiera | uncertain / STATE_VERSION_UNAVAILABLE |
| Error de backend o objeto no legible | Desconocido | uncertain / STATE_BACKEND_READ_FAILED; nunca ausencia |
| Objeto inexistente | Desconocido | uncertain / STATE_BACKEND_OBJECT_MISSING; no crear state |
| Error Proxmox / alcance insuficiente | Desconocido | uncertain / PROXMOX_OBSERVATION_FAILED o SCOPE_INSUFFICIENT |
| Identidad o postcondiciones distintas | Presente | uncertain / OBSERVED_RESOURCE_MISMATCH o APPROVED_POSTCONDITION_MISMATCH |
| State estable pero ajeno al lineage aprobado o anterior a él | Cualquiera | uncertain / APPROVED_STATE_MISMATCH |

`resolve-absence` sigue exigiendo evidencia externa, revisión independiente y
clave idempotente. Cierra como `failed / RESOURCE_ABSENCE_CONFIRMED`, nunca como
completed. No corresponde al incidente de VMID 106 presente.

## Despliegue futuro en VM 104

Este procedimiento es para una ventana operativa posterior al merge y revisión;
no se ejecuta al preparar el PR. No requiere migración de esquema.

1. Fijar el SHA aprobado del merge de este PR y conservar las imágenes de
   `74a1e92`. Inventariar el proyecto Compose y **todos** los overrides efectivos
   del servidor, incluidos tenants/TLS/CA. Conservar volúmenes PostgreSQL/Redis,
   secretos, asignaciones, clon IaC y volumen privado de planes. Verificar los
   respaldos según el procedimiento operativo existente.
2. Revisar colas y ejecuciones activas; esperar a que terminen operaciones de
   mutación antes de drenar el worker Terraform. Coordinar que no ingresen nuevas
   solicitudes durante la sustitución. **Mantener Apply global en 1** en API y
   worker; no usar la variable global como mecanismo de recuperación.
3. En un checkout del Control Plane del SHA aprobado, validar la configuración y
   construir las imágenes con las dependencias actualizadas. Ejemplo Bash, usando
   la lista completa de overrides del inventario:

   ```bash
   cd "$IACP_CHECKOUT"
   # Esta lista debe incluir también los overrides efectivos de tenants y TLS.
   compose=(docker compose --env-file "$IACP_ENV_FILE" -p "$IACP_COMPOSE_PROJECT"
     -f compose.yaml -f compose.server.yaml -f compose.terraform.yaml)
   "${compose[@]}" config --quiet
   "${compose[@]}" build api worker terraform-worker
   # Solo después de drenar; no interrumpir un Apply activo.
   "${compose[@]}" stop terraform-worker
   "${compose[@]}" up -d --no-deps --no-build api worker terraform-worker
   ```

   Fijar `IACP_RELEASE` e `IACP_TF_WORKER_IMAGE` al release aprobado en el archivo
   externo existente; conservar `IACP_TERRAFORM_APPLY_ENABLED=1`. No ejecutar
   `down -v`, regenerar secretos ni levantar un segundo proyecto sobre los datos.
4. Verificar health/readiness, SHA y digests de imágenes, versión 1.16.6 y provider
   0.116.1 fijados, conectividad TLS, montajes existentes y Apply=1. Mediante GET
   autorizado constatar que el objeto de state ofrece Version ID inmutable y que
   el backend sigue siendo el de la ejecución. No cambiar Wasabi si falta esta
   condición: registrar el bloqueo para revisión.
5. Solicitar recuperación únicamente por la API descrita abajo. Conservar eventos
   y evidencia de los snapshots. No ejecutar comandos Terraform de mutación.

## Recuperación del intento 4 existente

Usar una identidad operadora autorizada del tenant; no crear una aprobación ni
intento nuevos. Obtener por GET la ejecución y sus applies, y constatar:

- Instance ID `a1b2d063-6240-4969-bc04-e7e0e6be9e73`, intento 4;
- approval_id `1750ee70-a6ca-4036-a764-c4b077a0b588`, mismo SHA aprobado;
- VMID/ID Terraform 106, nodo `mng01`, nombre `iacp-kvm-lab-01`;
- address `module.kvm.proxmox_virtual_environment_vm.this`;
- backend y key persistidos, workspace `default`, lineage
  `d954af88-6ae6-2d04-9ead-55ed1f95d644`. Serial 3 es una referencia histórica,
  no una constante que deba forzarse; investigar cualquier cambio posterior.

Rutas (autenticación conforme al cliente operativo, sin imprimir tokens):

```text
GET  /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform
GET  /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform/applies
GET  /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73
POST /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform/applies/1750ee70-a6ca-4036-a764-c4b077a0b588/recover
```

POST sin cuerpo; 202 solo significa encolado. Si devuelve 409 por lock activo,
esperar a que finalice el worker y consultar antes de repetir. Consultar los GET
hasta que finalice la observación. Exigir `completed`, `error_code=null`, instancia
`ready`, identidades esperadas, `postconditions_verified=true`, origen
`prior_state.values`, snapshots before/after idénticos y Version ID vigente.
Confirmar un único evento terminal y ausencia de nueva mutación/attempt.

Ante uncertain, guardar el código y el diagnóstico. Un cambio de versión exige
investigar escritores/lecturas y otra observación completa una vez estable; no
reutilizar snapshots anteriores ni ejecutar `/apply`, `/retry`, import, destroy,
ediciones SQL/state o cambios de permisos para forzar el cierre. Una observación
fallida no revoca Apply globalmente y tampoco lo consume de nuevo.

## Límites, riesgos y rollback

- Se requiere GET actual consistente y versionado inmutable del backend. Un
  proveedor S3 que sirva dos lecturas obsoletas idénticas viola ese contrato; sin
  evidencia del servicio no puede detectarse únicamente desde el cliente.
- La evidencia corresponde a un intervalo, no a una transacción distribuida con
  Proxmox/Wasabi/PostgreSQL. El advisory lock serializa IACP; el lock remoto cubre
  el plan. Un escritor externo puede actuar después del último GET. Debe usar el
  locking nativo y coordinarse durante la recuperación. Drift posterior se trata
  por separado. No se afirma que los recursos no puedan cambiar después.
- Una versión diferente incluso con bytes iguales bloquea la confirmación. Esto
  exige otro recover y ofrece protección frente a ABA; no es un motivo para
  ignorar la verificación. No hay bucle de reintentos de provisioning.
- Comparar todos los valores conocidos puede conservar uncertain por defaults
  sin equivalencia demostrada, listas ordenadas de forma distinta o propiedades
  que el provider no pueda demostrar. Requiere investigar el atributo con
  evidencia privada y justificar su semántica. El provider y la API son las
  fuentes de observación; no prueban
  historial de clonación, salud del sistema invitado ni efectos fuera del plan.
- Se añade boto3 fijado a las imágenes. No se amplían permisos ni cambia el
  esquema PostgreSQL. Los diagnósticos no contienen state, planes, credenciales
  ni valores de cloud-init.
- La lectura acotada de la cabecera embebida está fijada al formato del CLI
  1.16.6. Actualizar Terraform requiere repetir la reproducción y validar este
  contrato. Un binario sin cabecera válida falla por integridad, sin reescribir
  artefactos históricos para hacerlo aceptable.

Rollback: drenar recuperaciones y workers sin interrumpir mutaciones activas;
volver a las imágenes `74a1e92` con el mismo proyecto, esquema y volúmenes.
Conservar Apply global en 1. Suspender el uso de recover del release anterior,
que reintroduce el falso conflicto, hasta corregirlo de nuevo. No revertir state,
VMs, planes privados, autorizaciones ni datos PostgreSQL. Un rollback de código
no deshace una transición ya confirmada. Registrar los resultados antes de volver.

## Validación

La suite usa fixtures sintéticos y mocks de Wasabi/Proxmox; las pruebas de
integración usan exclusivamente PostgreSQL/Redis desechables. Cubre la matriz,
representación, versiones/serial/lineage/hashes, errores en ambas lecturas,
postcondiciones completas, replay, dos workers, pérdida de lock, cancelación,
contexto cambiado y Apply global habilitado sin volver a provisionar.
Resultados finales: [informe del PR #17](VALIDATION_PR17.md), con JUnit y
snapshots sintéticos exportados. Sustituyen la ejecución inicial de 322 aprobadas
y 17 omitidas por falta de acceso al socket local.

- Integración originalmente pendiente: **17 aprobadas, 0 fallidas, 0 omitidas**.
- Integración ampliada con S3: **20 aprobadas, 0 fallidas, 0 omitidas**.
- Suite completa: **342 aprobadas, 0 fallidas, 0 errores, 0 omitidas** en Python
  3.12.15, PostgreSQL 17.11, Redis 7.4.11 y Celery 5.5.2 desechables.
- Docker se utilizó con la pertenencia de grupo ya autorizada mediante newgrp,
  sin modificar permisos. Se retiraron exclusivamente los proyectos de prueba.
- Verificación S3: recovery con GET reales y Terraform 1.16.6 con backend S3
  controlado; Version ID, lineage, serial y contenido invariantes. Solo se crean
  y retiran locks nativos `.tflock`, nunca el objeto de state.
- Siete advertencias de obsolescencia de Starlette/AnyIO y botocore; sin fallos.
- Reproducción con CLI real: 32 lecturas, 6 representaciones, 1 contenido,
  lineage/serial constantes y bytes intactos tras refresh-only.
- `python scripts/validate-handbook.py`: diez documentos y nueve contratos
  autenticados aprobados, sin red. `git diff --check`: aprobado.
- Sin ejecución real de recuperación, despliegue ni mutaciones de infraestructura.

## Archivos y funciones examinados

| Archivo | Funciones y cambio |
| --- | --- |
| `app/iacp/terraform/executor.py` | `observe`, `prepare_reconcile`, `approved_state_metadata`, `managed_resources`, `verify_postconditions`, `verify_checks`: snapshots, vínculo aprobado y postcondiciones |
| `app/iacp/terraform/state.py` | Nuevo `S3StateReader`, `StateSnapshot`, `canonical_state`: solo GET, versiones y comparación íntegra |
| `app/iacp/terraform/apply.py` | `_execute`, `check_binding`, `cancel_apply`: exclusión, condición final y cancelación coherente; `recover_apply` y `resolve_absence` revisados y conservados |
| `app/iacp/terraform/config.py` | `PlanError.diagnostic`: metadata permitida de consistencia |
| `app/iacp/terraform/runner.py` | Revisado sin cambios: `run` rechaza mutaciones; `apply_saved` solo pertenece al provisioning autorizado |
| `app/iacp/terraform/observation.py` | Revisado sin cambios: GET Proxmox, identidad, locks de recurso y alcance Audit para ausencia |
| `app/iacp/terraform/lifecycle.py`, `locks.py` | Revisados sin cambios: factory de observación y exclusión PostgreSQL existente |
| `app/requirements.txt` | Cliente S3 fijado, incluido por las imágenes API/worker ya existentes |
| `tests/test_readonly_reconciliation.py`, regresiones históricas/PR #16 e integración | Mocks de Wasabi/Proxmox, postcondiciones y concurrencia; fixtures de planes sintéticos con cabecera embebida |
| `scripts/reproduce-state-observation.py` | Prueba reproducible con CLI real y state local sintético |

Decisión propuesta: [ADR sin número: Read-Only Infrastructure Reconciliation](adr/read-only-infrastructure-reconciliation.md).
