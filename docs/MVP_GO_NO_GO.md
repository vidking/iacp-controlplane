# Dictamen MVP KVM — 24/10/2026

## Dictamen específico de rendimiento de Fase E — 24/10/2026, Costa Rica

**GO delimitado para lectura a 10 RPS:** 30 minutos, 18.000 solicitudes,
cero errores y p95 de 190,6 ms en el IACP Control Plane (VM 104), IACP 0.10.1 /
`7c0804d`. La primera degradación ensayada fue 20 RPS por respuestas 429 del
limitador por router/fuente. No se determinó el máximo físico del backend.
[Informe, métricas, recuperación y condiciones de demo](FASE_E_CAPACIDAD.md).

Se mantienen separados el **GO técnico para piloto supervisado** y el
**NO-GO de resiliencia integral**. La muerte física del worker sigue BLOCKED;
normalización y rotación de tokens permanecen diferidas después del MVP.

Anotación de Fase D, 24/10/2026: se aplica la [nomenclatura obligatoria](CONVENCIONES_INFRAESTRUCTURA.md) y se añade la [explicación en español de la evidencia original](validation/phase-d/INTERPRETACION_FASE_C_ES.md). Los resultados históricos de Fase C conservan su fecha y alcance.

## Dictamen actualizado de Fase D — 24/10/2026

| Alcance | Dictamen fundamentado |
|---|---|
| MVP KVM para integración piloto con Desarrollo | **GO técnico para piloto supervisado en el clúster MNG**, con políticas/identidades autorizadas y cliente que conserve intención y gestione `uncertain` por observación acotada. No equivale a aprobación de seguridad ni a uso de producción. |
| MVP con certificación integral de resiliencia | **NO-GO**: D3 físico y escenarios críticos de dependencias permanecen BLOCKED; la seguridad D6 está diferida y hay P1 de trazabilidad causal. |

Versión exacta presentable el 27/10: **IACP 0.10.1, SHA `7c0804d5f6106a7142a14b7bb649fe3e5c6c43aa`**, realmente ejecutado en los cuatro roles de aplicación del **IACP Control Plane (VM 104)**. [Promoción, imágenes y riesgos](FASE_D_PROMOCION.md), [prueba de código ejecutado](validation/phase-d/PROMOCION_VERIFICADA.json). El main posterior `116ed8c…` incorpora operación/documentación; no se confunde con el SHA de las imágenes.

**Demostrado:** 886 regresiones IACP y 36 del cliente aprobadas; E2E individual/doble automáticos con VM 30016/30017/30018 en `mng01` del clúster MNG; una mutación/Apply por VM, SSH/QGA/Lifecycle y DELETE normales; state vacío y sin locks. HTTPS/Swagger/readiness y contrato neutral `/v1` sin cambios. p95 global 260 ms y operaciones 360 ms a 5 RPS/240 s, cero errores de benchmark. [Evidencia](validation/phase-d/REGRESIONES.json), [E2E](validation/phase-d/RECUPERACION_OBSERVADA.json), [rendimiento](validation/phase-d/COMPARACION_HTTP.json).

**Recuperación observada:** en dos creaciones el cliente solicitó reconciliación readonly automáticamente, una y dos veces; se conservó el mismo recurso y un Apply consumido. No fue reparación de infraestructura ni recuperación física tras muerte del worker. D3 sigue BLOCKED hasta demostrar aislamiento por cola, proceso, instancia y credenciales; [preparación mínima](FASE_D_ENSAYO_AISLADO.md).

**P0:** supervisor corregido/desplegado y pruebas locales aprobadas; el gate de verificación física P0-C02 queda abierto para GO integral. **P1:** causa inicial de los episodios inciertos perdida tras reconciliar; preservar primer fallo sanitizado y repetir prueba conocida sin mutación adicional. También siguen pendientes DR conjunto de VM 104, revisión del peer TLS tras reinicios, permisos mínimos Lifecycle y colas p99. **P2:** correlación Celery/job, observabilidad de progreso y RTO por caso. No existe HA/failover independiente demostrado ni outbox/republisher automático.

**D6 PENDIENTE**, diferido expresamente por el usuario para después del MVP y sin bloquear las demás actividades. No hay identidad confirmada de cada Bearer expuesto ni constancia verificable de revocación/rotación/expiración; no se modificaron credenciales ni se declara seguridad aprobada.

El [cierre real](validation/phase-d/DEPENDENCIAS_CIERRE.json) conserva las protegidas y no tiene operaciones pendientes, mensajes unacked, advisory locks o VMs descartables. Platinum/PRX no se utilizó. Los PASS corresponden a su alcance probado; los resultados históricos siguientes no se modifican retroactivamente.

## Dictamen histórico de Fase C — previo a la promoción


**NO-GO para la certificación integral de resiliencia Fase C.** El objetivo antes del 27/10 requiere cerrar el P0 del runtime y completar los ensayos críticos que siguen BLOCKED. La evidencia de Fase B conserva su alcance de operación normal individual/doble.

| Exigencia | Dictamen defendible |
|---|---|
| Cero duplicaciones indebidas | PASS en idempotencia/concurrencia y fences locales; Fase B demuestra una mutación Apply por VM. Falta muerte real del worker durante una operación descartable |
| Cero destrucciones no autorizadas | PASS en controles locales y observaciones readonly de protegidas. Ninguna mutación de VM ni destrucción realizada en Fase C |
| Trazabilidad completa | PASS en registros y correlaciones revisados; limitación: task-id Celery no queda unido de forma obligatoria al job UUID y no hay outbox |
| State consistente o incertidumbre contenida | PASS en fixtures de versiones, errores, locks y fences; snapshots MNG cerrados consistentes. Inyección real de state/dependencias pendiente |
| Recuperación crítica comprobada | PASS local; BLOCKED MNG para muerte durante clone y trabajo en vuelo. El arranque en vacío tras reinicio de VM104 no satisface este gate |
| Ausencia de P0 abiertos | **FAIL en runtime**: P0-C02 no desplegado ni certificado físicamente |
| p95 agregado <500 ms a 5 RPS | PASS en nueva muestra de 240 s, p95 450 ms/0 errores; P1 abierto en listado de operaciones, p95 530 ms |

## P0 — Requisito previo a Go

**P0-C02: ejecución local fuera de control después de SIGKILL del worker.** Reproducción sintética: Terraform y su provider siguieron ejecutándose cuando el worker murió, pese al cleanup en finally. La muerte del worker puede liberar el lock DB antes de que termine el proceso externo. Los fences impiden repetir el Apply consumido, pero no detienen por sí solos al escritor huérfano ni certifican su finalización.

Corrección: supervisor independiente con lease por pipe, limpieza del grupo Terraform/provider y canal de estado sanitizado. Cierra también la ventana de excepción durante startup; conserva `TERRAFORM_UNAVAILABLE` y códigos negativos de señal. Regresiones: SIGKILL, propietario muerto antes de spawn, excepción después de spawn, binario ausente/no ejecutable, señal, timeout/cancel, una sola mutación y reconciliación del mismo binding. El PR #44 está integrado en main (`bc93861`) pero **no desplegado** según imágenes/revisión observadas a las 18:18 UTC. El P0 permanece abierto operativamente.

Plan de cierre: PR pequeño revisado → versión/imagen inmutables → despliegue del IACP Control Plane (VM 104) con cola drenada por procedimiento vigente → prueba descartable aislada sin afectar workers/colas compartidos → reconciliación readonly con mismo UUID/VMID/key → shutdown/DELETE normal y evidencia DB/PVE/Wasabi/locks/colas. No declarar cerrado por código aprobado localmente. El ensayo debe demostrar que el supervisor sobrevive a la modalidad de interrupción, que el provider queda en su grupo y que las tareas Proxmox ya enviadas se observan hasta resolverlas.

No se añadieron retry loops, force-unlock, imports, ediciones SQL de reparación ni recreaciones ciegas. El motor Terraform 1.16.6, módulos/providers y contratos `/v1` se conservan.

## P1 — Operación y alcance

- Desplegar y medir el [PR #45](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/45) después de su revisión: 325→9 consultas y mediana directa 211→21 ms para 50 filas, con payload idéntico. La ruta desplegada sigue con p95 530 ms; falta comparar por HTTP bajo la misma mezcla y límites.
- Certificar C-01/C-02/C-12 con tareas reales descartables tras cerrar P0; evaluar child prefork perdido, worker completo, hard timeout y broker reconnection por separado. No atribuir recuperación automática al ACK tardío.
- Preparar recuperación controlada para Power consumido sin UPID: hoy puede quedar permanentemente uncertain; conservar el bloqueo hasta contar con evidencia y un procedimiento revisado. No resetear el fence.
- Verificar respaldo/restauración conjunta DB/planes/secretos y recuperación de VM104 ante desastre. La VM 104 sigue siendo punto común de fallo del Control Plane; el clúster MNG tiene dos nodos.
- Revisar el alcance de la credencial Lifecycle: VM104 está excluida, pero hay `VM.PowerMgmt` efectivo en VM106/29000. Conservar los bloqueos API por ownership/scope/template; cualquier reducción de ACL debe tramitarse sin alterar la protección ni el contrato histórico autorizado.
- Revalidar los ensayos de dependencias con aislamiento por operación antes de inyectar contra MNG. Está prohibido cortar globalmente Redis, PostgreSQL, Wasabi o la red del plano compartido.
- Ampliar la ventana sostenida dentro del presupuesto autorizado y recuperar la evidencia original 1.104/520 ms si se exige una comparación histórica formal. No relajar cuotas para extender la duración.

## P2 — Evidencia y mantenibilidad

- Mejorar la unión job-id/task-id y observabilidad de active/reserved/unacked; especificar explícitamente configuración de delivery y sus límites, sin afirmar exactly-once.
- Corregir documentación histórica contradictoria sobre flags y rutas legacy cuando se revise su runbook; esta entrega usa el contrato neutral vigente.
- Revisar capturas privadas para identificar credenciales expuestas y tramitar su rotación; no hay material suficiente aquí para identificar cuáles aparecieron.
- Conservar resultados por escenario con tiempos individuales. Algunas regresiones históricas comprueban estados, pero no retienen RTO por caso; esos tiempos figuran como no medidos en la matriz.

## Alcance de la entrega

Se entregan [auditoría y baseline](RESILIENCE_CERTIFICATION.md), [matriz](FAILURE_MATRIX.md), [runbook](RECOVERY_RUNBOOK.md), [rendimiento](PERFORMANCE_BASELINE.md), pruebas y evidencia sanitizada en el [PR #46](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/46), separado de las correcciones #44/#45. Las pruebas físicas no ejecutadas conservan BLOCKED y los cambios integrados no se confunden con release desplegada. [Versiones, PRs y alcance](validation/phase-c/CHANGESET.json); [875 pruebas completas](validation/phase-c/COMBINED.junit.xml), cero fallos/omisiones. Los fixtures locales se retiraron tras terminar; el IACP Control Plane (VM 104) quedó sin operaciones pendientes, mensajes unacked ni advisory locks en el snapshot de cierre.
