# Matriz de fallos — Fase C

Anotación de Fase D, 24/10/2026: se aplica la [nomenclatura obligatoria](CONVENCIONES_INFRAESTRUCTURA.md) y se añade la [explicación en español de la evidencia original](validation/phase-d/INTERPRETACION_FASE_C_ES.md). Los resultados históricos de Fase C conservan su fecha y alcance.

Revisión 24/10/2026. Baseline IACP `a4f63a9` / runtime `4f153e8`; candidato local descrito en [certificación](RESILIENCE_CERTIFICATION.md). PASS se refiere exclusivamente al alcance probado. Una prueba local aprobada no acredita su equivalente real MNG.

La suite completa y pruebas dirigidas están en [TEST_RESULTS](validation/phase-c/TEST_RESULTS.json); las fuentes citadas permiten reproducir los escenarios con dependencias descartables. Las pruebas nuevas de resiliencia pertenecen al commit [3d815d1](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/commit/3d815d17cc1c2d83fb34e16c5a7e25f93577729a/tests/test_resilience.py) y su [integración](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/commit/3d815d17cc1c2d83fb34e16c5a7e25f93577729a/tests/integration/test_resilience.py). PostgreSQL/Redis reales de integración son locales, sin salida externa; Proxmox/Wasabi son fixtures salvo los snapshots explícitamente readonly de MNG.

| Caso | Resultado local | Ensayo real en IACP Control Plane (VM 104) / clúster MNG | Recuperación observada |
|---|---|---|---|
| C-01 worker antes de ejecución | PASS: intención y cerco de intento conservados | BLOCKED: sin inyección real | Manual/controlada para ventana commit→publish; automático solo si el mensaje sigue pendiente y es entregado |
| C-02 después de clone antes de resultado | FAIL baseline; PASS candidato sintético | BLOCKED: P0 continúa abierto en runtime | Supervisor automático local; cierre de operación por reconciliación readonly controlada |
| C-03 Redis inaccesible | PASS en errores de publicación/readiness simulados | BLOCKED | Fail-closed; replay/continuación explícita según operación |
| C-04 PostgreSQL inaccesible | PASS en pérdida real de conexión del lock de fixture | BLOCKED | Lock liberado por PG; resultado no autorizado; recuperación controlada |
| C-05 Wasabi inaccesible | PASS con HTTP S3 503 loopback y restauración | BLOCKED | Dos intentos SDK totales por lectura; nueva lectura controlada |
| C-06 lock ocupado/residual | PASS con Terraform real/S3 loopback | BLOCKED | Fail-closed; liberación solo por propietario en fixture |
| C-07 respuesta Proxmox incierta | PASS simulado para Power/Apply | BLOCKED para inyección física | Fail-closed y observación; sin reenviar mutación consumida |
| C-08 VM running sin readiness guest | PASS controles sintéticos existentes | BLOCKED para fallo físico | Pending/failed/uncertain según fase; recuperación de observación explícita |
| C-09 idempotency concurrente | PASS PostgreSQL real, cuerpo igual/distinto | BLOCKED para repetición MNG | Arbitraje automático por UNIQUE; 409 al cuerpo distinto |
| C-10 tenants cruzados | PASS con tenants dummy | BLOCKED para dos identidades reales | Rechazo 403/404 antes de infraestructura |
| C-11 protegidas | PASS guardas dummy + permisos/hashes reales readonly | BLOCKED para ensayo de autorización real; PASS observación readonly | Fail-closed; no se emitieron mutaciones sobre las protegidas |
| C-12 reinicio con pendientes | PASS prefork/Redis/PG locales y power con UPID en suite | BLOCKED con trabajo real pendiente | Automático para cola pendiente; replay explícito en recuperación Power |

## C-01 — Worker antes de ejecutar

- Hipótesis: perder el productor o worker antes de ejecución no debe perder la intención ni reasignar VMID/state.
- Precondiciones: DB de fixture, executor Terraform y ningún apply consumido.
- Inyección: `WorkerKilled(BaseException)` entre commit y publish; retry controlado; entrega tardía del intento anterior.
- Esperado/observado: intención queued durable; nuevo intento sobre mismo UUID/state; entrega anterior ignorada; una planificación. PASS con recuperación **manual**, no outbox automático.
- PostgreSQL: intento anterior interrupted, intento 2 planned; job y operación conservados. Proxmox/Wasabi: no contactados; ninguna clonación ni state físico creado.
- Cola/locks: publisher dummy; exclusión local; la recuperación se solicita después de liberar el lock. Reintentos: 1 planificación controlada; 0 retries Celery.
- Duración de fallo/recuperación: no cronometradas individualmente; secuencia determinista dentro de la suite.
- Evidencia: `tests/test_resilience.py::test_c01_commit_publish_gap_keeps_intent_and_manual_retry_fences_late_delivery` y `tests/test_terraform.py::test_recover_unpublished_queued_attempt_fences_late_delivery`.

## C-02 — Worker después de clonación

- Hipótesis: la muerte del worker debe detener su grupo Terraform/provider, conservar el fence y permitir observar el mismo recurso.
- Precondiciones: proceso sintético con un descendiente; apply de fixture con `started_at` consumido.
- Inyección: SIGKILL al proceso padre real; por separado, interrupción abrupta del fake executor después de representar la clonación y antes del resultado DB.
- Esperado: cero descendientes activos y cero nueva mutación por redelivery. Observado baseline: Terraform/provider siguen vivos tras 5 s; **FAIL P0-C02**. El fixture los retiró. Candidato con supervisor: ambos dejan de ejecutar y pasa la regresión.
- PostgreSQL simulado: apply/job running después de interrupción; redelivery apply no ejecuta; reconcile explícito termina completed/ready con mismo VMID/key/intento. Proxmox: contador fake = 1; no VM real. Wasabi: key conservada, sin contacto real en esta prueba.
- Cola/locks: no broker en la prueba de proceso; lock de instancia liberado al salir; ordinary apply tardío cercado por `started_at`.
- Reintentos: 0 mutaciones adicionales; 1 reconciliación readonly controlada. Fallo/recuperación: límite de observación 5 s; tiempo por escenario no retenido. Corrida inicial 6,65 s; pruebas dirigidas corregidas 6,23 s (34 casos).
- Evidencia: `tests/test_resilience.py::test_c02_killed_worker_cannot_leave_terraform_or_provider_running`, `::test_c02_mutation_fence_survives_interruption_and_recovery_is_readonly`, `::test_c02_supervisor_refuses_to_spawn_after_owner_already_died`.
- Reprueba crítica: integrar/desplegar el candidato revisado, demostrar aislamiento de una sola operación descartable y repetir muerte en fase controlada, inventario/state/locks y DELETE normal. Hasta entonces MNG BLOCKED y P0 abierto.

## C-03 — Redis inaccesible

- Hipótesis: una publicación sin confirmación no puede generar una segunda intención ni sobrescribir el resultado de un worker que ya avanzó.
- Precondiciones: publisher sustituido y DB de fixture; readiness de API comprobable.
- Inyección: error/timeout del publisher, incluidos mensaje entregado con ACK perdido y error antes de entrega; fallo del ping Redis en readiness.
- Esperado/observado: error sanitizado; creación no reclamada puede quedar failed/DISPATCH_FAILED; Apply conserva queued; Power conserva reserva y admite replay del mismo ID. PASS en este alcance.
- PostgreSQL: intención/eventos conservados, sin rollback de un resultado ya completed. Proxmox/Wasabi: adaptadores dummy; sin efectos reales.
- Cola/locks: publicación ambigua representada por fixture; no corte global. Reintentos: 0 autoretries; un replay explícito en los casos de recuperación.
- Duraciones: no medidas por escenario. Recuperación: manual/continuación explícita, con rechazo mientras la dependencia falla.
- Evidencia: `tests/test_instances.py`, `tests/test_api.py::test_readiness_hides_dependency_failures`, `tests/test_power.py::test_broker_failure_can_republish_same_operation_safely`, `tests/integration/test_terraform.py::test_ambiguous_dispatch_and_worker_claim_use_consistent_lock_order`.

## C-04 — PostgreSQL inaccesible

- Hipótesis: perder la conexión propietaria del advisory lock impide confirmar éxito desde ese propietario.
- Precondiciones: PostgreSQL 17 descartable, DB exclusiva `iacp_test_*`, UUID conocido del fixture.
- Inyección: `pg_terminate_backend` únicamente sobre el PID del lock exacto de la prueba; heartbeat posterior.
- Esperado/observado: excepción de SQLAlchemy; lock liberado por PostgreSQL y nueva adquisición válida. PASS para la exclusión; los tests existentes comprueban que una pérdida de heartbeat no confirma Apply/Power.
- PostgreSQL: sin operación física en esta prueba, sesión del lock terminada; no force-unlock. Proxmox/Wasabi: no contactados.
- Cola/locks: cola no implicada; nuevo propietario puede adquirir tras desconexión. Reintentos: 1 adquisición de comprobación, 0 mutaciones.
- Duraciones: no cronometradas; recuperación comprobada dentro del caso. Modo: fail-closed y recuperación controlada; no acredita caída total/restauración de PostgreSQL.
- Evidencia: `tests/integration/test_resilience.py::test_c04_lost_postgres_lock_connection_fails_closed_and_releases_lock`, `tests/test_readonly_reconciliation.py::test_lost_instance_lock_prevents_terminal_database_commit`.

## C-05 — Wasabi inaccesible

- Hipótesis: un 503 de state significa lectura desconocida, nunca recurso ausente ni éxito.
- Precondiciones: S3 HTTP loopback con un state v4 versionado; ningún endpoint externo.
- Inyección: fixture responde 503 solo para state; se restaura después de agotar el límite del SDK.
- Esperado/observado: `STATE_BACKEND_READ_FAILED`; exactamente dos GET fallidos; GET nuevo tras restauración devuelve serial 1. PASS.
- PostgreSQL/Proxmox: no implicados en esta prueba de lector; la contención de Apply está cubierta por `test_backend_failure_on_verification_never_commits_success`. Wasabi simulado: bytes y versión 1 invariantes, sin PUT/DELETE.
- Cola/locks: no implicados; sin eliminación de locks. Reintentos: 2 intentos totales SDK, luego 1 lectura manual/controlada.
- Duraciones: no medidas individualmente; deadline runner 15/60 s según fixture, SDK acotado. Recuperación: readonly controlada.
- Evidencia: `tests/integration/test_resilience.py::test_c05_temporary_state_outage_is_not_absence_and_retry_is_readonly`, `tests/test_readonly_reconciliation.py::test_backend_errors_never_mean_resource_absence`.

## C-06 — Lock ocupado o residual

- Hipótesis: Terraform no debe ignorar ni retirar un lock ajeno para obtener éxito.
- Precondiciones: CLI 1.16.6, S3 loopback, lock con ID/propietario conocido del fixture.
- Inyección: preexistencia de `.tflock`; plan refresh-only con `-lock-timeout=1s`.
- Esperado/observado: comando falla; lock idéntico, state/serial/version invariantes; cero DELETE de `.tflock`. PASS. El propietario del fixture libera después su propio lock.
- PostgreSQL/Proxmox: no implicados. Wasabi simulado: state vacío legible, versión 1 sin cambios.
- Cola/locks: lock permanece durante fallo; no bypass. Reintentos: espera nativa hasta 1 s, número de PUT internos no fijado; 0 reintentos de mutación IACP.
- Duración del fallo: timeout de lock configurado 1 s, tiempo real no retenido. Recuperación: liberación manual del propietario en fixture; no certifica desbloqueo automático ni unlock de Wasabi real.
- Evidencia: `tests/integration/test_resilience.py::test_c06_occupied_native_state_lock_is_preserved`; prueba nativa de refresh-only en `tests/integration/test_readonly_state.py`.

## C-07 — Proxmox con resultado incierto

- Hipótesis: pérdida de respuesta después del envío debe conservar incertidumbre y bloquear un segundo envío, especialmente reboot.
- Precondiciones: adaptador Power/Apply fake, ownership válido y fence persistido.
- Inyección: pérdida de respuesta POST, timeout ACPI, respuesta final incompatible y errores de observación.
- Esperado/observado: una mutación consumida; sin UPID se conserva `POWER_TASK_UNKNOWN/uncertain`; UPID conocido permite monitorizar; reboot no se infiere solo de running. PASS sintético.
- PostgreSQL: fence/submitted_at/UPID cuando existe, errores/eventos persistidos; reserva active/uncertain bloquea incompatibles. Proxmox: contador fake = 1. Wasabi: no escrito por Power; Apply observado sin reaplicar.
- Cola/locks: replay vuelve a observar; exclusión por instancia. Reintentos: 0 POST adicionales; polling acotado. Duraciones: según deadlines de fixtures; no latencia de fallo físico medida.
- Recuperación: readonly/manual; sin UPID puede requerir procedimiento de investigación sin cierre automático disponible.
- Evidencia: `tests/test_power.py::test_ambiguous_submit_never_replays_reboot`, `::test_shutdown_timeout_never_forces_or_repeats_shutdown`, `tests/test_apply.py::test_uncertain_apply_requires_observation_never_reapplication`.

## C-08 — Running sin Cloud-Init/QGA/SSH

- Hipótesis: energía running no equivale a guest listo ni debe justificar re-crear una VM.
- Precondiciones: guest/power/access de fixture y perfil revisado.
- Inyección: observación QGA/SSH no confirmada, error de autenticación/host key y pérdida de respuesta de inicialización.
- Esperado/observado: `guest_ready=null`; verificación access failed/uncertain y diagnóstico sanitizado; inicialización consumida solo observa. PASS en escenarios sintéticos existentes, sin inyección física.
- PostgreSQL: operaciones guest/access y sus fences conservados, readiness separado de creación. Proxmox: fixture running; 0 nuevas clones. Wasabi: sin reparación ni escritura de state por estos observadores.
- Cola/locks: operación de guest pendiente bloquea power/delete incompatibles y sigue contando cuota. Reintentos: 0 exec adicionales tras fence; observación explícita según test.
- Duraciones: no medidas por caso; inicialización tiene deadline 210 s y tarea 250/270 s; access 55/65 s. Recuperación: controlada; falta de prueba física = BLOCKED MNG.
- Evidencia: `tests/test_guest_identity.py::test_lost_exec_response_reconciles_readonly_with_admission_and_policy_closed`, `::test_unfinished_initialization_blocks_power_and_delete_and_counts_quota`, `tests/test_crm_contract.py::test_access_verification_async_and_provider_error_is_sanitized`, `tests/test_power_proxmox.py`.

## C-09 — Idempotency concurrente

- Hipótesis: dos solicitudes simultáneas con misma clave/cuerpo producen una intención; cuerpo distinto produce conflicto.
- Precondiciones: PostgreSQL real descartable y dos conexiones sincronizadas mediante barrier.
- Inyección: dos threads intentan crear simultáneamente, parametrizados con cuerpo idéntico o distinto.
- Esperado/observado: idéntico conserva un job/operación/instancia/evento y una publicación; distinto permite una intención y rechaza la otra. PASS.
- PostgreSQL: UNIQUE `(tenant_id,idempotency_key)` arbitra; un registro por entidad. Proxmox/Wasabi: no contactados.
- Cola/locks: publisher contador, exactamente 1; locks transaccionales liberados. Reintentos: 0; 2 solicitudes concurrentes.
- Duraciones: no cronometradas individualmente; barrier timeout 10 s. Recuperación: automática por control de unicidad, no compensación posterior.
- Evidencia: `tests/integration/test_instances.py::test_database_arbitrates_concurrent_idempotent_creation[False/True]` y `::test_duplicate_concurrent_delivery_executes_once`.

## C-10 — Tenants cruzados

- Hipótesis: una identidad de B no lee ni muta recursos de A; un header/body no sustituye al tenant autenticado.
- Precondiciones: tenants/credenciales dummy A/B y roles reader/operator/admin/approver.
- Inyección: UUIDs ajenos en inventario, jobs, operaciones, power, provisioning, políticas, DELETE y eventos.
- Esperado/observado: 403 por rol/scope o 404 por ownership; sin publisher/provider. PASS en las rutas cubiertas por la suite; no se afirma pentest exhaustivo.
- PostgreSQL: filas e historial de A preservados. Proxmox/Wasabi: no efectos, adaptadores mock.
- Cola/locks: no nueva tarea; sin locks persistentes. Reintentos: 0. Duraciones: no medidas.
- Recuperación: fail-closed. Evidencia: `tests/test_instances.py`, `tests/test_provisioning_contract.py`, `tests/test_lab_self_service.py::test_cross_tenant_inventory_policy_commands_and_catalog_are_isolated`, `tests/test_deletion.py::test_auth_and_tenant_boundary`.

## C-11 — VM 104, VM 106 histórica y VM 29000 Golden Image Ubuntu 24.04

- Hipótesis: el ensayo debe rechazar targets protegidos sin enviar una mutación física.
- Precondiciones: recursos dummy protegidos; para MNG, credenciales actuales de auditoría y GET únicamente.
- Inyección: guardas de allocation/Target/delete/ownership con VM104/106 y suelo VMID que excluye 29000; no se envió POST/DELETE contra las VMs reales.
- Esperado/observado: rechazo antes del executor/publisher; runtime VM104 sin privilegios y VM106 `mutations_available=false/POWER_INSTANCE_NOT_AUTHORIZED`; hashes VM106/29000 invariantes. PASS de lectura y controles locales; intento real de mutación = BLOCKED deliberadamente.
- PostgreSQL: historial preservado; VM106 continúa administrada. Proxmox: protegidas sin cambios observados. Wasabi: states históricos preservados; sin borrado de versiones.
- Cola/locks: sin nuevas tareas por esta prueba; cierre sin pendientes. Reintentos: 0. Duraciones de fallo/recuperación: no aplican al rechazo de autorización.
- Recuperación: fail-closed. Evidencia: [snapshots](validation/phase-c/RUNTIME_AFTER.json), [credencial Lifecycle](validation/phase-c/LIFECYCLE_PROTECTION.json), [power readonly](validation/phase-c/QUERY_PROFILE_BEFORE.json), `tests/test_deletion.py::test_controlplane_historical_and_non_disposable_ids_protected`, `tests/test_lab_self_service.py::test_lifecycle_and_historical_scope_stay_separate`. PowerMgmt en 106/29000 está concedido al token; la protección API no se atribuye a una denegación de ACL inexistente.

## C-12 — Reinicio con trabajos pendientes

- Hipótesis: un trabajo ya aceptado y aún en cola se conserva cuando el worker está detenido y completa al arrancar.
- Precondiciones: PostgreSQL/Redis locales y worker prefork de fixture; smoke no crea infraestructura.
- Inyección: aceptar job con worker detenido; arrancar y observar completed; detener y repetir con un segundo job antes del siguiente arranque.
- Esperado/observado: ambos completan manteniendo IDs/historial. PASS para trabajos pendientes sin comenzar. La suite Power además mata un worker con UPID persistido y recupera mediante replay explícito.
- PostgreSQL: queued→completed para dos jobs. Proxmox/Wasabi: no contactados en smoke; Power usa fixture, sin segundo POST.
- Cola/locks: entregas reales Redis; worker prefork una plaza; no bloqueo residual observado. Reintentos: 0 retries de negocio; 2 arranques.
- Duraciones: cada espera está limitada a 25 s y parada a 10 s; no se retuvieron tiempos individuales. Recuperación: automática para cola pendiente; manual/replay para Power.
- Evidencia: `tests/integration/test_resilience.py::test_c12_pending_jobs_complete_after_prefork_worker_restart`, `tests/integration/test_power.py::test_real_celery_worker_interruption_redelivery_and_api_restart`.

## Condición de salida

El aislamiento real para estas inyecciones todavía no está establecido: falta un worker/cola exclusivo con routing por operación comprobado y un punto de interrupción que no pueda afectar tareas ajenas. C-03…C-07 necesitan además sustituir o fallar conexiones únicamente de ese ensayo; no se permite cortar las dependencias compartidas. C-10 requiere dos identidades reales con alcance aprobado para el ensayo; no se crearon credenciales para simularlo. C-11 conserva únicamente autorización sintética y observación real mientras no exista un ensayo de denegación que garantice cero envío físico. Estos BLOCKED reflejan precondiciones no demostradas, no pruebas aprobadas ni una obligación de cortar servicios para completarlas.

Cada FAIL crítico requiere corrección y reprueba. P0-C02 tiene candidato y regresión local; continúa abierto para el runtime hasta despliegue revisado y ensayo real aislado. Las pruebas físicas C-01…C-12 no ejecutadas conservan BLOCKED. No se añadirá una nueva inyección real si aparece cualquier operación uncertain; su reconciliación y el cierre consistente son prerrequisitos.
