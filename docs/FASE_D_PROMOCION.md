# Fase D — Promoción y certificación operativa

Fecha: 24/10/2026. Destino: **IACP Control Plane (VM 104)**, `iacp-controlplane-01`, alojado en `mng01`. Las VMs descartables se administraron únicamente en el **clúster MNG**, compuesto por `mng01` y `mng02`. **Platinum/PRX** quedó excluido. Se conservaron protegidas la **VM 104**, la **VM 106 histórica** y la **VM 29000 Golden Image Ubuntu 24.04**.

## Dictámenes diferenciados

| Alcance | Dictamen | Condiciones y límites |
|---|---|---|
| MVP KVM para integración piloto con Desarrollo | **GO técnico limitado a piloto supervisado en el clúster MNG** | Contrato neutral, políticas vigentes, identidades autorizadas, como máximo tres VMs de ensayo y dos flujos de aprovisionamiento concurrentes. El cliente debe conservar intención/IDs y gestionar `uncertain` mediante reconciliación readonly acotada. No implica aprobación de seguridad, uso de producción ni recuperación certificada del worker. |
| MVP con certificación integral de resiliencia | **NO-GO** | D3 físico permanece BLOCKED por aislamiento insuficiente; también quedan escenarios críticos de dependencias sin inyección física. D6 está diferido por decisión explícita del usuario. La pérdida del diagnóstico inicial tras reconciliar es P1 abierto. |

El piloto acredita operación normal y contención/reconciliación observadas. La corrección del P0 está desplegada y su regresión local pasa; **el gate de certificación física del P0-C02 permanece abierto**. No se declara GO integral ni recuperación automática de infraestructura.

## D1/D2 — Versión y promoción verificadas

| Elemento | Resultado |
|---|---|
| SHA anterior ejecutado | `4f153e89826f8e8decc25bcd083e093db37668fd` |
| SHA de aplicación desplegado y presentable el 27/10 | **`7c0804d5f6106a7142a14b7bb649fe3e5c6c43aa`**, IACP **0.10.1** |
| PR incluidos en esa versión | #44, #45 y #46; comprobados antes de construir |
| Imagen API/worker/Lifecycle | `sha256:23bdc4736b06310ef74ef49dbf9332a6ced54c16fe9aab193c6a0a2d770011c5` |
| Imagen IaC Worker | `sha256:751dd7d24532ca1f257e027a411532bb01e196340ba75f6a2fb3bc1696e7b365` |
| Esquema | `0011_crm_contract`, sin migración nueva ni downgrade |
| Contrato público HTTPS | SHA canónico `5c8916b438e2d178c3fc495acd57a2edc5760e8c1bd60f33489af69c4377e3aa`, idéntico al histórico de `4f153e8` |
| DB, Redis y Traefik | IDs, imágenes, arranque y mounts preservados durante la promoción |
| Historial previo | Comparación oficial de cantidades/hashes aprobada antes/después del despliegue |

Evidencias: [promoción y hashes de código en ejecución](validation/phase-d/PROMOCION_VERIFICADA.json), [contenedores desplegados](validation/phase-d/IACP_CONTROL_PLANE_DESPLEGADO.json), [dependencias](validation/phase-d/DEPENDENCIAS_DESPLEGADAS.json), [HTTPS/Swagger/contrato](validation/phase-d/HTTPS_CONTRATO.json), [respaldo restaurado](validation/phase-d/RESPALDO_RESTAURADO.json).

Se verificaron las rutas y servicios reales por sus etiquetas Docker. Se construyeron los destinos existentes `api` y `terraform-worker` de `app/Dockerfile`, sin cambiar Terraform 1.16.6, módulos, provider o backend. Se conservó el entorno privado y se restauró un respaldo nuevo en PostgreSQL descartable sin red, con 15 tablas y el esquema esperado. El dump permanece privado; no se incorporó a Git.

La promoción utilizó `scripts/iacpctl`, `scripts/go_live.py` y `deploy/mng/compose.yaml`: drenado con capacidades cerradas, candidato revisado, `env-promote`, `deploy`, `activate-lab` y `doctor`. Los registros nuevos vinculan la autorización explícita de esta Fase D a actor, ventana UTC, SHA y acciones concretas; no se reutilizaron ventanas vencidas. No se recrearon DB/Redis/Traefik ni se usó `down -v`.

La política root `179f58db-59ee-4da2-a676-30c1642441a8` se renovó por el endpoint existente y la identidad aprobadora independiente, sin cambiar sus cuotas o snapshots. La política `67e81062…` seguía vigente pero tenía snapshot desactualizado y no se utilizó. Se preservó también la política vigente `ac558dfa…`. El cliente mantuvo presupuesto de tres VMs y concurrencia máxima dos.

## Bloqueo real de preparación y corrección TLS

El preflight detectó que, tras el reinicio previo de la VM 104, Traefik tenía `172.20.0.2` en la red compartida y `IACP_TLS_PROXY_IPS` conservaba `172.20.0.3`, ocupada por la propia API. La promoción se detuvo; no se eliminó esa guarda.

El [PR #47](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/47), controlador `04a1bd3f1ad46fae80a2e75c364f6e427c738de4`, añade una transición explícita mediante `env-promote`: acción adicional `update-tls-peer`, IPs exactas anterior/nueva, ID del proxy observado y una única red compartida. Se rechazaron peers ajenos, rangos, drift y cambios adicionales; 43 pruebas dirigidas y revisión independiente aprobaron el cambio.

Se corrigió primero el peer **con imágenes anteriores, capacidades cerradas y `configuration_changes=[]`**, se aplicó `rollback` oficial para alinear los contenedores con el entorno y se aprobó el doctor ordinario. Después pasó el preflight original de `7c0804d`, sin nuevas excepciones. No se modificaron credenciales, Compose, puertos, redes ni reglas TLS de Traefik.

El PR #47 se integró como `fd054bd…` y el [PR #48](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/48), de idioma/nomenclatura, como `116ed8c…`. Esos commits posteriores afectan operación/documentación/pruebas; el árbol `app` es idéntico al SHA de aplicación desplegado. No se afirma que las imágenes ejecuten `116ed8c`.

También se registraron dos incidencias de preparación: directorio de evidencia previamente creado antes del primer preflight, y una referencia privada de token ya inexistente antes de la primera intención E2E. Se corrigieron conservando evidencia y sin atribuirlas al producto ni a recuperación de una operación aceptada.

## D3/D4 — Resiliencia y límites de certificación

[Preparación mínima de aislamiento](FASE_D_ENSAYO_AISLADO.md) y [matriz actualizada](FAILURE_MATRIX.md). No se mataron procesos compartidos ni se cortaron Redis, PostgreSQL, Proxmox, Wasabi o red del Control Plane.

La regresión verifica SIGKILL del propietario, terminación del grupo Terraform/provider, intención durable, fences, PostgreSQL perdido, Redis/publicación ambigua, S3 503, lock ocupado, redelivery, idempotencia concurrente y tenants/protegidas con fixtures. Los hashes del runner/supervisor desplegados coinciden con los aprobados. Esto no sustituye una interrupción física con VM en vuelo.

En el E2E real se observaron dos episodios `uncertain`: VM 30016 y VM 30017. El cliente solicitó una y dos reconciliaciones readonly, respectivamente; hubo una ejecución de reconciliación y un único Apply consumido por VM. El intervalo desde el evento de incertidumbre al cierre se conserva en [RECUPERACION_OBSERVADA.json](validation/phase-d/RECUPERACION_OBSERVADA.json). No se repitió la mutación ni se recreó una VM. La causa inicial no puede determinarse: el éxito sobrescribió el diagnóstico previo; los eventos sí conservan la secuencia y los IDs. No se atribuye a un timeout Proxmox ni a muerte del worker.

## D5 — Regresión, E2E y rendimiento

- **886 pruebas IACP aprobadas**, cero fallos/omisiones, 238,03 s; [JUnit completo](validation/phase-d/REGRESION_IACP.junit.xml).
- **36 pruebas del cliente externo aprobadas**, 0,821 s, sin red. [Entornos y versiones](validation/phase-d/REGRESIONES.json).
- **E2E individual automático:** VM 30016, UUID `470d7a05-37a2-4fe3-81a5-1a5a8881671f`; [resumen](validation/phase-d/INDIVIDUAL_RESUMEN.json), [eventos](validation/phase-d/INDIVIDUAL_EVENTOS.jsonl).
- **E2E doble automático:** VM 30017 (`fe42848c-c448-4ca2-a700-34c3721c2218`) y VM 30018 (`b3bacd63-5057-4cbc-bf97-b3fb35719770`); [resumen](validation/phase-d/DOBLE_RESUMEN.json), [eventos](validation/phase-d/DOBLE_EVENTOS.jsonl).

Las tres VMs se crearon en `mng01` del clúster MNG y verificaron acceso SSH root con clave de host fijada, QGA, reboot, shutdown, start y DELETE normal. Cada una tuvo un solo Apply consumido. Al cierre: DB `deleted`, ausencia física, state vacío y sin lock. El doble acredita dos flujos activos; no promete dos CLI simultáneos: el IaC Worker conservó concurrencia uno. Los 13 y 11 HTTP 409 de los E2E son conflictos controlados de lock/cuota/replay; no se confunden con errores funcionales ni con el benchmark HTTP sin errores.

| Medición a 5 RPS/240 s | Fase C, `4f153e8` | Fase D, `7c0804d` |
|---|---:|---:|
| Requests del CSV | 1.204 | 1.198 |
| Errores HTTP | 0 | 0 |
| p50/p95/p99 global, ms | 71 / 450 / 530 | 74 / 260 / 410 |
| p50/p95/p99 operaciones, ms | 380 / 530 / 640 | 150 / 360 / 740 |
| SQL para 50 operaciones | 325 | 9 |

[CSV posterior original](validation/phase-d/HTTP_5RPS_DESPUES.csv), [comparación HTTP](validation/phase-d/COMPARACION_HTTP.json), [comparación SQL](validation/phase-d/COMPARACION_SQL.json). El benchmark se ejecutó antes de los E2E para conservar 167 operaciones; no se mezcló con aprovisionamiento. Doce pares de payloads directos coincidieron. Las frecuencias por ruta son aleatorias; cuatro minutos no acreditan capacidad productiva sostenida. El p99 de operaciones no mejoró y el de energía física posterior fue 1.400 ms; permanecen visibles.

## Riesgos y cierre

| Prioridad | Estado y acción necesaria |
|---|---|
| P0 — gate de certificación | P0-C02 corregido/desplegado; **verificación física BLOCKED**. Preparar aislamiento completo y repetir interrupción/contención/recuperación del mismo binding antes de GO integral. |
| P1 — diagnóstico histórico | **FAIL de trazabilidad causal:** reconciliar limpia el error/diagnóstico previo. Preparar PR pequeño que preserve el primer fallo sanitizado en el JSON existente; reprueba: fallo conocido tras fence → reconcile exitoso → diagnóstico conservado, mismo `started_at`, una mutación y rechazo de reapply tardío. No se modifica el motor ni se repara state para ocultarlo. |
| P1 — puntos comunes de fallo | API, DB, Redis, workers y planes comparten VM 104; respaldo DB restaurado no acredita restauración conjunta de Control Plane ni failover. Verificar DR, dependencia de IP dinámica del proxy y reintentos internos del provider. |
| P1 — seguridad diferida | D6 **PENDIENTE**, por instrucción del usuario. Tokens expuestos no identificados verificablemente; sin constancia de revocación/rotación/expiración. No se modificaron credenciales ni se declara seguridad aprobada. |
| P1 — latencia de cola | Investigar colas p99 de operaciones/energía con trazas acotadas antes de nuevas optimizaciones; p95 sí cumple en la ventana medida. |
| P2 — observabilidad | Unir task-id Celery/job UUID, medir RTO por escenario y mejorar supervisión de progreso. No hay outbox ni reparación automática de infraestructura demostrados. |

El [cierre observado](validation/phase-d/DEPENDENCIAS_CIERRE.json) no tiene operaciones/jobs/applies pendientes, mensajes unacked, advisory locks ni VMs descartables. Los cuatro states recientes observados están vacíos y sin locks; los hashes de VM 106 y VM 29000 coinciden con el baseline. No es una inspección universal de todos los objetos históricos de Wasabi.

Antes de la demostración del 27/10, revalidar versión, salud, peer TLS y política vigente. La renovación utilizada para estos ensayos vence el **24/10 a las 22:46 UTC**; no acredita autorización para el día 27. Renovarla por el endpoint y la aprobación independientes existentes dentro de la ventana correspondiente, sin ampliar cuotas ni editar snapshots por SQL.

Rollback disponible: `iacpctl rollback`, con manifiesto y cambio vigentes, cierra admisión/Lifecycle/DELETE/delegación y conserva Apply=1, esquema e historial. No devuelve automáticamente los binarios a `4f153e8`; ese retorno requeriría otra promoción coordinada y reabriría el defecto anterior. Los dumps, secretos, certificados y entorno anteriores permanecen privados. No se borraron versiones de Wasabi ni se restauró una DB vieja sobre operaciones nuevas.

Evidencia privada del procedimiento: `/opt/iacp/evidence/fase-d-20261024T184048Z/`, en el IACP Control Plane (VM 104). En Git solo se incorporan proyecciones sanitizadas, hashes y los tres archivos expresamente publicables de cada E2E; no configuraciones privadas, contraseñas ni Terraform State.
