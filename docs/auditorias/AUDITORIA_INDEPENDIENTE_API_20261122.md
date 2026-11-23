# Auditoría independiente de la API de IACP

**22 de noviembre de 2026, hora de Costa Rica. Certificación integral de producción BLOCKED.** La revisión `7c1290a172d576b2d732e5edba2399a363e6eba4` del IACP Control Plane (VM 104) presenta defectos de integridad en operaciones, recuperación y reservas. Las comprobaciones de salud y del contrato básico pasaron. El alcance de esta auditoría permite decidir que la revisión carece de evidencia suficiente para certificarse y que los defectos identificados requieren corrección.

## Alcance e independencia

Seis revisiones estáticas independientes cubrieron HTTP y autorización, persistencia y concurrencia, recuperación de workers, integraciones con Terraform e IPAM, ciclo de vida de aplicaciones, y portal y cadena de suministro. Cada revisión recibió una copia de código sin historial de conversación, pruebas, matrices, auditorías ni configuración de despliegue. La comparación con el remoto, las pruebas existentes y las consultas HTTPS se hicieron durante la consolidación. Las referencias de línea corresponden a `7c1290a`.

`origin/main` era `54fce353f4e9da3b3544b63a5b8d41d6b0896395`. La única diferencia respecto de `7c1290a` era documental. La revisión remota `0f73ffe696b5e0fd4d392c0e5b46755ce787ed4d` de `devops/features` se usó para localizar correcciones candidatas. La verificación de producción se limitó a consultas HTTPS normales y a la revisión que anunció la API en su cabecera.

## Evidencia y dictamen

| Control | Resultado | Evidencia del 22 de noviembre |
| --- | --- | --- |
| API pública en `iac.cyberfuel.com` | **PASS** | `GET /health/live` y `GET /docs` devolvieron `200`. `GET /v1/environments` devolvió `401` sin credenciales. Las tres respuestas anunciaron `7c1290a`. |
| Suite existente en PostgreSQL 17.6 local desechable | **PASS** | `uv sync --locked --extra dev --no-install-project` y `.venv/bin/python -m pytest -q -r fEs` en la copia de `7c1290a`. 439 pasaron, con un aviso de deprecación. |
| Imagen y runtime local aislado | **PASS** | `.venv/bin/python scripts/test_local_docker.py --image iacp-audit:7c1290a-7uikk --evidence-dir /tmp/iacp-audit.7uiKkSY9/docker`. Readiness local, esquema 3, seis workers KVM y dos de servicios. La suite en contenedor también registró 439 pruebas aprobadas. |
| Integridad y disponibilidad del código de producción | **FAIL** | El código permite divergencias entre el recurso que valida la API y el que ejecuta el worker. También permite liberaciones de reservas sin acreditar titularidad y resultados de éxito sin todas las verificaciones operativas. |
| Integración piloto de las correcciones | **BLOCKED** | La comparación de `devops/features` acredita cambios en código. La ejecución de las correcciones en desarrollo y su evidencia operativa quedan pendientes. |
| Certificación integral en el IACP Control Plane (VM 104) | **BLOCKED** | Faltan el cierre de hallazgos bloqueantes, la verificación del digest desplegado, el inventario de servicios, esquema, colas y locks del runtime, y la evidencia física del candidato en el clúster MNG. |

El `200` observado por HTTPS en producción corresponde a `/health/live`. El readiness de la tabla corresponde al runtime local. Las pruebas locales no realizaron operaciones en MNG ni en Platinum/PRX.

## Integridad de operaciones y recursos de MNG

| Prioridad | Hallazgo y alcance | Corrección propuesta | Código |
| --- | --- | --- | --- |
| Alta | La API comprueba actividad y guarda la operación para la instancia declarada. El worker adopta la instancia, el VMID y el workspace del plan, que pueden pertenecer a otro recurso. El control independiente de VMID protegidos sigue presente. | Resolver un destino único, rechazar referencias incompatibles y revalidar titularidad al reclamar el trabajo. | `api/service.py:639`, `api/service.py:798`, `worker/main.py:2029` |
| Alta | La admisión genérica ejecuta comprobaciones y escrituras en autocommit. Un worker puede ver un trabajo antes de que se registre su instancia o evento. La ruta genérica también omite el requisito de apagar la VM que aplica la eliminación dedicada. | Unificar guardas y ejecutar la validación, el encolado y el evento en una transacción. Volver a comprobar identidad y estado antes de la mutación. | `api/service.py:700`, `api/service.py:757`, `api/service.py:841`, `api/db.py:156` |
| Alta | Un `destroy` puede usar state histórico sin comprobar que la VM actual siga siendo la propietaria del plan. El riesgo exige un state anterior, un VMID reutilizado y permisos sobre el recurso actual. | Vincular plan, state, reserva y VM a una identidad persistente. Invalidar planes al quedar obsoleta su reserva. | `worker/main.py:2119`, `worker/main.py:2181`, `terraform/modules/kvm/main.tf:1` |
| Alta | Terraform puede sobrevivir a un fallo del heartbeat. El worker original puede continuar escribiendo mientras otra ejecución inicia la reconciliación. | Supervisar y cerrar el grupo local de procesos. Condicionar escrituras al dueño y a la generación de ejecución. Antes de repetir una mutación, confirmar el resultado de la tarea remota. | `worker/main.py:88`, `worker/main.py:335`, `worker/main.py:742`, `worker/heartbeat.py:37` |
| Alta | Una excepción puede cerrar un aprovisionamiento como exitoso por presencia en inventario. La ruta puede omitir preboot, energía observada e identidad. La persistencia terminal también está separada del estado de la instancia y el evento. | Aplicar los mismos criterios de éxito al flujo normal y a la recuperación. Mantener `uncertain` si falta evidencia. Consolidar en transacción el cierre local y registrar los efectos externos pendientes. | `worker/main.py:1288`, `worker/main.py:2341`, `worker/main.py:2395`, `worker/main.py:2452` |
| Alta | La liberación de VMID filtra solo por número. Un destroy con IPAM pendiente puede liberar la reserva y una reconciliación posterior puede repetirlo tras una reasignación. También hay cierres que informan éxito con `IPAM_RELEASE_PENDING` en la operación fuente. | Liberar por dueño y generación. Retener el VMID mientras queden efectos pendientes y propagar el resultado real al cierre de la reconciliación. | `worker/allocation.py:40`, `worker/allocation.py:61`, `worker/main.py:1271`, `worker/main.py:1323`, `worker/main.py:2377` |
| Alta | Una respuesta de inventario incompleta puede interpretarse como ausencia. NetBox valida el marcador antes de borrar una IP, pero no confirma que siga sin una asignación incompatible. Una caída durante `plan` también puede dejar una reserva pendiente antes de que exista el workspace. | Exigir observaciones válidas y evidencia de ausencia, verificar asignación y titularidad al liberar, y recuperar la intención de IPAM de forma independiente del workspace. | `worker/proxmox.py:196`, `worker/main.py:562`, `worker/main.py:821`, `worker/main.py:2074`, `worker/netbox.py:556` |

Las rutas que reciben un resultado ambiguo de Proxmox pueden intentar otra mutación antes de confirmar la anterior. Las transiciones de potencia deben conservar el identificador de tarea y el detalle del resultado. Si se habilita `IACP_RETENTION_DAYS`, preservar operaciones `uncertain` y planes referenciados por instancias vigentes. `worker/proxmox.py:388`, `worker/main.py:2195`, `api/db.py:381`.

## Disponibilidad HTTP y controles de acceso

- **Readiness con DDL.** `GET /health/ready` ejecuta inicialización y toma el mismo advisory lock que los workers usan para reclamar operaciones. Trasladar DDL al arranque o a un paso de migración y limitar la sonda a una consulta de lectura acotada. `api/main.py:260`, `api/db.py:79`, `api/db.py:366`.
- **Dependencia afectada por CVE-2025-62727.** El lock de producción fija Starlette `0.46.2` y el portal sirve archivos mediante `FileResponse` y `StaticFiles`. Se verificó que `/docs` y sus recursos son públicos. La corrección del aviso se incorporó en `0.49.1`. Debe acreditarse la versión en el digest candidato y en el desplegado. `uv.lock:745`, `api/main.py:214`, `api/main.py:242`. [Notas de Starlette 0.49.1](https://github.com/Kludex/starlette/blob/main/docs/release-notes.md#0491-october-28-2025).
- **Límite de cuerpo dependiente del encabezado.** La API controla el `Content-Length` declarado. Si el proxy entrega un cuerpo sin ese encabezado, la aplicación carece de un tope acumulado. Medir bytes recibidos antes de convertir a JSON y verificar el límite del proxy. `api/main.py:82`.
- **Autenticación y alcance del Bearer.** La configuración por omisión permite iniciar sin exigir token. El manifiesto canónico de producción lo exige y la consulta protegida devolvió `401`. Si hay consumidores con distintos permisos, un Bearer compartido no permite identificar propietario o limitar acciones y entregas. Aplicar autorización por recurso y rol antes de ofrecer acceso a consumidores independientes. `api/main.py:122`, `api/main.py:178`, `api/applications.py:128`, `api/applications.py:283`.
- **Secretos en errores y artefactos.** La respuesta de validación `422` puede incluir la entrada original. La idempotencia guarda una huella sin clave de cuerpos con secretos y Terraform puede conservar contraseñas en sus artefactos. Redactar errores, usar un HMAC versionado con clave separada de PostgreSQL y evitar o controlar la persistencia de credenciales reutilizables en planes y state. `api/service_models.py:93`, `api/service.py:432`, `terraform/modules/kvm/main.tf:82`.
- **Transporte de credenciales.** Los clientes de Proxmox y NetBox admiten configuraciones de transporte inseguras. La exposición depende de los valores que se apliquen en cada entorno. Exigir HTTPS verificado y restringir redirecciones autenticadas al origen esperado. `worker/proxmox.py:21`, `worker/netbox.py:135`.

## Aplicaciones y portal

- **Alta.** n8n publica el proxy antes de confirmar la cuenta propietaria. Si el piloto ejecuta instalaciones y la VM es accesible a terceros, el alta inicial queda expuesta durante esa ventana. Reservar el acceso al canal de activación hasta verificar al propietario. `worker/service_recipes.py:54`, `worker/service_executor.py:531`.
- **Alta.** Un trabajo de aplicación aún en cola puede impedir reconciliar la VM que necesita para arrancar. Cancelar el trabajo tampoco cancela el aprovisionamiento ni programa la revocación de la clave SSH temporal. Distinguir espera de VM y mutación activa, y registrar la compensación antes de finalizar una cancelación. `api/applications.py:28`, `api/applications.py:335`, `api/service.py:970`, `worker/services.py:95`.
- **Media.** Registrar la recuperación de credenciales antes de responder puede consumir la única consulta tras una desconexión. Si el handoff pierde su resultado después de retirar la clave SSH, el reintento depende de la misma clave. Incorporar acuse o rotación autenticada y un canal de verificación de revocación. Comprobar además renovación del certificado por IP, cuya vigencia inicial es de siete días. `api/applications.py:283`, `worker/service_executor.py:395`, `worker/service_tls.py:20`.
- **Media.** Cancelar o reintentar un trabajo histórico puede afectar a un servicio que ya tiene un trabajo sucesor o fue entregado. Vincular las transiciones al trabajo vigente y cerrar los que quedaron superados. `api/applications.py:208`, `api/applications.py:335`, `api/applications.py:376`.
- **Alta para operaciones.** El portal actualiza primero el ID seleccionado y carga después el detalle. Con peticiones simultáneas puede mostrar otra instancia o mezclar sus datos mientras las acciones de energía usan el ID global. Anclar detalle y controles al mismo ID y descartar respuestas obsoletas. `api/static/docs/assets/docs.js:709`, `api/static/docs/assets/docs.js:1016`.
- **Media.** El cURL generado no escapa los valores libres como argumentos de shell. Los portales Swagger y ReDoc también cargan JavaScript de terceros con acceso a su propio origen. Generar argumentos seguros o exportar el cuerpo como archivo, servir dependencias versionadas localmente y aplicar una CSP adecuada al HTML. `api/static/docs/assets/docs.js:925`, `api/main.py:219`.
- **Media.** El build usa etiquetas de bases y paquetes APT sin un snapshot fijado. Conservar el digest de la imagen validada para promoción y fijar los insumos si se requiere reproducir el build. `Dockerfile:1`, `Dockerfile:17`.

## Correcciones candidatas y condiciones de promoción

El diff de `devops/features` incorpora readiness de solo lectura, Starlette `0.50.0`, mitigaciones de supervisión y titularidad del worker y un cambio para permitir reconciliar una VM con un trabajo de aplicación en cola. El mismo diff permite comprobar que permanecen la divergencia entre plan e instancia, la admisión genérica sin transacción y la liberación de VMID por número. Los cambios observados en esa rama son candidatos a verificación.

1. Cerrar identidad, transacciones, recuperación y reservas antes de promover. Verificar los criterios de éxito, estados inciertos y limpieza mediante el contrato normal del sistema. Aplicar los controles HTTP y de credenciales al alcance que se vaya a habilitar.
2. Verificar la misma imagen inmutable en `dev-iac.cyberfuel.com`, en el IACP Control Plane (VM 104). Para la evidencia física en el clúster MNG, confirmar aislamiento de cola, procesos, instancia, credenciales y rango VMID. La matriz requiere seis aprovisionamientos simultáneos con los seis workers sanos e idle antes del inicio. Conservar `mng_credentials_shared=true` y las protecciones de las VM 104, VM 106 y VM 29000.
3. Registrar resultados, contención y recuperación con alcance y evidencia. Verificar digest, esquema, servicios, operaciones, colas, locks y reversión según `scripts/iacpctl` y `scripts/go_live.py` antes de proponer la promoción de ese digest a `iac.cyberfuel.com`.

## Revalidación de desarrollo y estado previo a promoción — 2026-11-23

Esta adenda conserva el dictamen histórico del 22 de noviembre y registra las
comprobaciones posteriores. Las pruebas de desarrollo no cambian el resultado
histórico observado en `iac.cyberfuel.com`.

| Control | Resultado | Evidencia |
| --- | --- | --- |
| API dev | **PASS** | `https://dev-iac.cyberfuel.com/health/ready` respondió `ready` con revisión `922593e993fb9a084b92cdf81346ade374f60ed9`. |
| Suite del candidato en PostgreSQL local y Docker aislado | **PASS** | 513 pruebas; resumen local en `/tmp/iacp-dev-hardening-local-922593e/summary.json`. |
| Matriz KVM 48 en dev | **PASS 48/48**, alcance `lab_dhcp` | Corrida `20261123050435`; `/opt/iacp/evidence/auditoria-dev-922593e-mng-20261123050435/report.json`. La corrida anterior no probó NetBox para KVM. La verificación independiente de borrado por Proxmox devolvió 403; el resultado se apoyó en el `destroy` completado por Terraform y la API. |
| Matriz de servicios en dev | **PASS 16/16 + 2 controles** | `prod_static` y NetBox; corrida `20261123055200`; `/opt/iacp/evidence/auditoria-dev-services-922593e-20261123055200/results.jsonl`. Tres lotes, cada uno con seis aprovisionamientos simultáneos. 693 chequeos PASS, cero chequeos FAIL, 20 timeouts SSH transitorios recuperados. Las tres limpiezas registraron cero operaciones activas, trabajos de servicio, locks advisory o recursos Terraform. |
| Runtime dev posterior a matrices | **PASS** | Seis workers de infraestructura idle, cuatro workers de servicios idle, readiness `ready`, cero operaciones activas o inciertas y cola vacía. |
| Estado de producción antes de promover | **PASS de lectura; sin promoción** | `/health/ready` respondió `ready` con revisión `7c1290a172d576b2d732e5edba2399a363e6eba4`. `iacpctl inventory`, `preflight` y `verify` pasaron. Se observaron seis workers de infraestructura y cuatro de servicios idle, cero operaciones activas o inciertas, cero trabajos de servicio y cero locks advisory. |

La matriz KVM de 48 todavía debe repetirse con `prod_static` para validar el
uso de NetBox en KVM. La promoción y ambas matrices de producción siguen
pendientes. El cambio del runner para producción conserva 16 casos de servicio
(ocho n8n y ocho Nextcloud), dos controles sin llave SSH, los dos casos piloto
iniciales y lotes posteriores de seis; no convierte un preflight en un PASS de
matriz. El resultado de certificación integral continúa **BLOCKED** hasta que
el mismo candidato supere las matrices físicas solicitadas y se cierren los
hallazgos de seguridad pendientes.
