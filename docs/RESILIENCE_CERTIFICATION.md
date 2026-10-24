# Fase C — Certificación de resiliencia y fallos

Anotación de Fase D, 24/10/2026: se aplica la [nomenclatura obligatoria](CONVENCIONES_INFRAESTRUCTURA.md) y se añade la [explicación en español de la evidencia original](validation/phase-d/INTERPRETACION_FASE_C_ES.md). Los resultados históricos de Fase C conservan su fecha y alcance.

## Actualización verificada de Fase D — 24/10/2026, 19:22 UTC

El **IACP Control Plane (VM 104)** ejecuta **`7c0804d5f6106a7142a14b7bb649fe3e5c6c43aa`**, IACP 0.10.1, con los PR #44/#45/#46 incluidos. Se verificaron imágenes y hashes del runner, supervisor y listado de operaciones dentro de los contenedores; no se deduce el despliegue de Git. El esquema sigue en `0011_crm_contract`, DB/Redis/Traefik e historial previo se preservaron y HTTPS/Swagger/readiness aprobaron. [Informe de promoción](FASE_D_PROMOCION.md), [evidencia de versión/código](validation/phase-d/PROMOCION_VERIFICADA.json), [contrato HTTPS sin cambios](validation/phase-d/HTTPS_CONTRATO.json).

La regresión posterior aprobó **886 pruebas IACP** y **36 del cliente externo**. Los E2E individual y doble cerraron automáticamente las VM **30016, 30017 y 30018** del **clúster MNG**, nodo `mng01`, mediante DELETE normal, state vacío y sin locks. Cada una tuvo un solo Apply consumido. Dos episodios de incertidumbre requirieron reconciliación readonly solicitada por el cliente; no hubo reparación de infraestructura ni interrupción de worker. [Resultados reales y límites](validation/phase-d/RECUPERACION_OBSERVADA.json).

El p95 HTTP global fue **260 ms** y el de operaciones **360 ms**, a 5 RPS/240 s y cero errores. El dictamen actual distingue **GO técnico para piloto supervisado** y **NO-GO integral**: D3 físico permanece BLOCKED y D6 diferido. Se confirmó un P1 de pérdida del diagnóstico inicial tras reconciliar; la causa de las incidencias observadas no está determinada. [Dictámenes y riesgos](MVP_GO_NO_GO.md).

El [cierre](validation/phase-d/DEPENDENCIAS_CIERRE.json) no tiene operaciones/jobs/applies pendientes, mensajes unacked, advisory locks ni VMs descartables. Los hashes de las protegidas se conservan. Estas nuevas observaciones no sustituyen los resultados históricos siguientes.

## Registro histórico de Fase C — previo a la promoción


Fecha: 24/10/2026. Dictamen actual: **NO-GO para certificar resiliencia integral del MVP KVM**.
La operación normal individual/doble tiene evidencia previa; la recuperación ante muerte abrupta del worker requiere cerrar P0-C02 y verificarla en MNG. Este informe registra un avance de certificación, no una aprobación de los escenarios reales pendientes.

## Baseline comprobado

| Elemento | Evidencia y alcance |
|---|---|
| Repositorio IACP | `a4f63a9740a3b75bba3940e58a309285c43aaf63`, main limpio al inicio; main remoto contrastado por `ls-remote` |
| IACP Control Plane (VM 104) | `4f153e89826f8e8decc25bcd083e093db37668fd`, API 0.10.1; imágenes API `24572f9…` y Terraform Worker `3ce85fc…`; esquema `0011_crm_contract` |
| Locust externo | `927a5c56b9d7b7a4d16e53e1e97f2bc161eeb09b`, PR [#5](https://scm.cyberfuel.com/CyberFuel/iacp-load-testing/pulls/5) integrado |
| Pruebas previas | [857 IACP](validation/PROVISIONING_CONTRACT.junit.xml) y [36 del cliente](https://scm.cyberfuel.com/CyberFuel/iacp-load-testing/src/commit/927a5c56b9d7b7a4d16e53e1e97f2bc161eeb09b/docs/evidence/runner-tests.txt); atribución histórica |
| E2E autónomo | [Fase B](https://scm.cyberfuel.com/CyberFuel/iacp-load-testing/src/commit/927a5c56b9d7b7a4d16e53e1e97f2bc161eeb09b/docs/evidence/PHASE_B_RESULT.json): VM30012, VM30013/14, sin intervención en las corridas aprobadas |
| VM30015 | UUID `ace24782-e376-45b9-a247-79930d9ee21e`: DB deleted, ausente del inventario de prueba, state serial 2 vacío, sin lock. La observación actual confirma cierre; no reproduce ni acredita por sí sola el uso histórico de Swagger |
| Protegidas | VM104 sin privilegios efectivos para la identidad inspeccionada; hashes de VM106 y 29000 coincidentes con Fase B y sin cambios durante esta auditoría |
| Carga histórica 1.104 / p95 520 ms | Comunicada por el solicitante. No se localizó el CSV/ventana correspondiente en ambos repositorios ni en las ubicaciones operativas inspeccionadas; no se atribuye como medición nueva |
| Situación actual | Cero operaciones/jobs/applies pendientes, cero advisory locks, cero unacked y sin VMs descartables presentes |

El IACP Control Plane (VM 104), `iacp-controlplane-01`, había reiniciado alrededor de 09:10 UTC antes de nuestra inspección. Sus siete contenedores arrancaron y no había trabajo pendiente cuando los observamos. Esto demuestra arranque en vacío; **no demuestra recuperación de trabajos en vuelo**. Las etiquetas de Compose aún reflejan tres procedencias históricas: aplicación, Traefik y DB/Redis. No se refactorizaron.

Evidencias actuales: [runtime inicial](validation/phase-c/RUNTIME_BASELINE.json), [host durante carga](validation/phase-c/HOST_DURING.json), [runtime de cierre 18:18 UTC](validation/phase-c/RUNTIME_CLOSURE.json), [host de cierre](validation/phase-c/HOST_CLOSURE.json). La comprobación de cierre conserva la misma revisión desplegada, sin pendientes ni locks anómalos en el alcance observado. [Retirada de fixtures locales](validation/phase-c/LOCAL_SANDBOX_CLOSURE.json).

## Auditoría de los doce mecanismos

| Mecanismo | Implementado | Demostración y límite |
|---|---|---|
| Celery/Redis | `acks_late=True`, prefetch 1, resultados ignorados, publicación sin retry, autoretry vacío. Redis AOF; fsync efectivo everysec. Visibility timeout efectivo por defecto 3.600 s | [Configuración efectiva por task](validation/phase-c/RUNTIME_BASELINE.json). Plan/apply no activan `reject_on_worker_lost`; otras tareas sí. `max_retries=3` no implica reintentos automáticos cuando no se llama retry |
| Persistencia | Instance, Operation, Job, Execution/Attempt y reserva se confirman juntos; eventos operativos persistidos | PostgreSQL real en suite aislada. Job UUID de negocio no se vincula obligatoriamente al task-id Celery generado al publicar |
| DB/cola | Commit DB precede publish; commit terminal precede ACK. No hay transacción distribuida, outbox, inbox ni republisher periódico | C-01 reproduce interrupción en la ventana commit→publish. La intención permanece; el operador recupera con mismo UUID y nuevo intento de planificación cercado |
| Locks/concurrencia | Advisory lock transaccional global para admisión/cuotas; lock de sesión por UUID; índices de unicidad y fences de mutación; workers Terraform/Lifecycle con concurrencia 1 cada uno | Pruebas PostgreSQL de exclusión y pérdida de conexión. No existe un semáforo global de dos Terraform entre réplicas: la cuota de política y los workers efectivos son controles diferentes |
| Backend remoto | Wasabi S3 con `use_lockfile=true`, cifrado, timeout lock 30 s; observación compara VersionId, lineage, serial y hashes antes/después | Terraform 1.16.6 real contra S3 loopback: lock ocupado se conserva; ningún force-unlock. Los GET/HEAD de Wasabi real verificaron states cerrados, no una inyección de fallo |
| Procesos Terraform | Baseline usa nueva sesión y cleanup en finally | **FAIL baseline C-02**: SIGKILL dejó Terraform/provider activos. Corrección candidata añade supervisor por pipe/EOF. No está desplegada; procesos Proxmox ya enviados no se cancelan por terminar el proceso local |
| Correlación | UUID de instancia estable → operación de creación → job/ejecución → VMID reservado e intento → key `iacp/lab/tenants/{tenant}/instances/{UUID}/terraform.tfstate`; apply tiene approval_id y su job | Se conservan VMID/state key/intento tras muerte simulada y reconciliación. No se usan VMID ni nombre como sustitutos del UUID/idempotency key |
| Timeout Proxmox | Power consume `submitted_at` antes de POST; persiste UPID después; sin POST retry. Apply consume `started_at` antes de Terraform | Tests verifican un único envío y uncertain ante respuesta perdida. El retry interno del provider Terraform durante clone no queda certificado: requiere revisión del provider fijado e inyección específica |
| Uncertain | Apply, DELETE, Power y guest conservan fences; replan y acciones incompatibles se bloquean. Reconcile observa; resolve-absence exige revisión independiente | Recuperación readonly probada localmente y previamente observada en E2E. No existe reparación automática de state, importación, recreación ni eliminación automática de locks. Power sin UPID puede permanecer uncertain sin cierre automático |
| Tenant/ownership | Tenant derivado del token; búsquedas filtradas; revisiones independientes; bindings de plan/política; allowlist/suelo VMID para DELETE y guest | Suite de tenants y recursos protegidos. VM106 tiene soporte histórico de power autorizado por certificado/scope: su protección actual depende también de configuración efectiva; no es una prohibición universal compilada |
| Dependencias/readiness | API readiness verifica DB/esquema/Redis; arranque condicionado por health/migración; restart unless-stopped | Runtime sano y workers con ping. Readiness no verifica cola progresando, Wasabi, Proxmox, Cloud-Init/SSH ni heartbeat de todos los workers. Una cola vacía no acredita liveness |
| VM104 | API, DB, broker, proxy, workers y artefactos locales comparten VM | Fallo común de host/disco/energía; sin HA ni failover independiente demostrado. Redis everysec tiene ventana de durabilidad y PostgreSQL/planes dependen de respaldos/restauración vigentes. No se interrumpió VM104 |

Fuentes principales: [worker](../app/iacp/worker.py), [creación](../app/iacp/services/instances.py), [locks](../app/iacp/terraform/locks.py), [apply](../app/iacp/terraform/apply.py), [runner](../app/iacp/terraform/runner.py), [supervisor candidato](../app/iacp/terraform/process_supervisor.py), [state](../app/iacp/terraform/state.py), [power](../app/iacp/lifecycle/execution.py), [guest](../app/iacp/services/guest_identity.py), [Compose del IACP Control Plane (VM 104)](../deploy/mng/compose.yaml).

La [comprobación de la credencial dedicada Lifecycle](validation/phase-c/LIFECYCLE_PROTECTION.json) también confirma ausencia de privilegios en VM104. Sí tiene `VM.PowerMgmt` en VM106 y 29000: en Proxmox los flags efectivos 0 y 1 conceden el privilegio en la ruta exacta. La contención depende del scope/ownership de IACP y del rechazo de templates; no se declara protección únicamente por ACL. Revisar menor privilegio como P1, sin cambiar permisos durante el ensayo.

## Resultados y gates

Ver [matriz C-01…C-12](FAILURE_MATRIX.md), [runbook](RECOVERY_RUNBOOK.md), [rendimiento](PERFORMANCE_BASELINE.md) y [dictamen](MVP_GO_NO_GO.md). La [evidencia de pruebas](validation/phase-c/TEST_RESULTS.json) distingue el fallo inicial, la corrección candidata y las limitaciones de captura.

Corrección aislada en [PR #44](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/44), commit `3d815d17cc1c2d83fb34e16c5a7e25f93577729a`. [Revisión independiente](validation/phase-c/P0_REVIEW.json). El remoto registra su integración en main como `bc93861d36142fad2c48d38d78ffc25a0b92e94d` a las 18:04:25 UTC; esta sesión no ejecutó ese merge. La comprobación de imágenes de 18:18 UTC confirma que **no está desplegada en el IACP Control Plane (VM 104)**. El [PR #45](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/45) contiene únicamente la mejora de lectura de operaciones. [Versiones y alcance de cambios](validation/phase-c/CHANGESET.json).

Regresión conjunta final: **875 aprobadas**, cero fallos/skips, 234,62 s; cliente externo: **36 aprobadas** sin red. Se conserva el [JUnit completo](validation/phase-c/COMBINED.junit.xml), además del [JUnit dirigido](validation/phase-c/DIRECTED.junit.xml). El resultado previo de 873 casos del P0 no tenía XML retenido; no se confunde con esta captura completa. El árbol de aplicación probado equivale al main remoto `7379e840155e8a8b426c6d05a3683fa6a4a1229e`, que integra también el PR #45 a las 18:16:24 UTC; este merge tampoco fue ejecutado por esta sesión. Las inyecciones usaron PostgreSQL/Redis descartables, procesos sintéticos y S3/Proxmox de fixture. Se ejecutó carga HTTP readonly desde el cliente externo. La validación real de interrupción durante clonación permanece BLOCKED mientras el runtime conserve el defecto P0 y no haya una ejecución certificada de la corrección. No se creó ni destruyó ninguna VM durante esta Fase C.

## Seguridad de evidencias

Se escanearon por contenido 763 blobs Git alcanzables de IACP y 57 de Locust, incluyendo JSON/JSONL, campos sensibles anidados, asignaciones, llaves privadas, formatos de tokens y entropía contextual. [Resultados sin valores](validation/phase-c/SECRET_SCAN.json). La [revisión independiente](validation/phase-c/SECRET_REVIEW.json) atribuye los 47 candidatos a 36 fixtures, 9 nombres de pruebas y 2 referencias; cero credenciales reales confirmadas en este alcance. Otras 34 ubicaciones quedaron clasificadas automáticamente como sintéticas/referencias. El alcance excluye refs no descargadas, capturas compartidas no disponibles y credenciales fuera de Git; no equivale a ausencia universal de secretos.

Las capturas previas deben revisarse en su canal privado por su propietario. Rotar las identidades efectivamente expuestas —Bearer API, docs, token Proxmox o password inicial según la captura— por el procedimiento autorizado; no se infiere cuál apareció sin verla. Conservar solo IDs de credencial y referencias privadas de rotación. Si se confirma un secreto en Git, revocar/rotar primero y preparar un plan `git filter-repo` sobre copia aislada, inventario de refs/forks/caches y ventana coordinada. No se reescribió main ni se borraron versiones de Wasabi.

El [escaneo de contenido de los archivos nuevos/modificados](validation/phase-c/SECRET_DIFF_SCAN.json) añadió dos candidatos para revisión y un fixture explícito. La [revisión de esas ubicaciones](validation/phase-c/SECRET_DIFF_REVIEW.json) identificó nombres de pruebas y un marcador BEGIN sin cuerpo de llave; cero credenciales reales confirmadas en ese diff. No se emitieron valores de credenciales.

La auditoría formal de seguridad es [parcial](validation/phase-c/SECURITY_AUDIT_STATUS.json): el límite de capacidad de los agentes interrumpió dos revisiones y la crítica final de cobertura. La revisión independiente del P0 y las disposiciones de los 47 candidatos sí terminaron. No se presenta el escaneo o la suite de regresión como una certificación exhaustiva de seguridad.
