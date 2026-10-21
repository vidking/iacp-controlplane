# Auditoría del Control Plane — Fase 1

Fecha: 2026-10-21. Base inspeccionada: `a964850`.
Rama de trabajo: `feature/KVMHOST-91-controlplane-core`.

## Alcance y evidencia

Se revisaron todos los archivos versionados, incluidos configuración de Compose,
Traefik, Dockerfile, dependencias, scripts, documentación y los cinco módulos
Python. No se inspeccionó ni modificó VM 104, Proxmox ni recursos de producción.
El despliegue descrito en README es evidencia documental del bootstrap; esta
auditoría no verifica su estado remoto.

## Componentes que se conservan

| Componente | Evidencia | Decisión |
| --- | --- | --- |
| FastAPI y autenticación de laboratorio | `app/iacp/main.py` | Conservar endpoints y Bearer, separar responsabilidades |
| PostgreSQL y SQLAlchemy 2 | `db.py`, `models.py` | Conservar tabla `jobs` y datos; introducir migraciones |
| Redis y Celery | `worker.py` | Conservar tarea registrada `iacp.smoke` y entrypoint del worker |
| Compose | `compose.yaml` | Conservar servicios, redes, secretos y nombres de volúmenes |
| Traefik | `traefik/*.yml` | Conservar exposición en loopback; TLS es un gate posterior |
| Operación del laboratorio | `scripts/*.sh` | Conservar smoke; no ejecutar scripts sobre infraestructura existente |
| Bloqueo de aprovisionamiento | `POST /v1/instances` | Mantener 503 hasta validar el núcleo y el ejecutor |

## Hallazgos

La prioridad expresa impacto potencial al ampliar el laboratorio, no certifica
explotabilidad ni vulnerabilidades de versiones específicas.

| Prioridad | Hallazgo y evidencia | Consecuencia / trabajo necesario |
| --- | --- | --- |
| Alta | Token compartido; no hay identidad, roles ni tenant (`main.py`) | Cualquier poseedor puede consultar cualquier job. No conectar clientes hasta aislar consultas y permisos |
| Alta | `create_all` en lifespan (`main.py`) | No versiona cambios ni detecta drift; ambos roles pueden usar credenciales con DDL. Migraciones explícitas y separación de privilegios posterior |
| Alta | Commit del job y `smoke.delay` son operaciones separadas | Caída tras commit deja `queued` sin mensaje; timeout de publicación es ambiguo. Requiere outbox y reconciliación |
| Alta | Worker lee y escribe estados sin reclamar atómicamente el job | Entregas duplicadas/concurrentes pueden ejecutar dos veces. Requiere transición condicional, fencing y política de recuperación |
| Alta | Sin manejo persistente de excepción del worker, heartbeat ni lease | Un crash puede dejar `running` indefinidamente; `acks_late` no resuelve por sí solo recuperación ni ejecución única |
| Alta | Sin idempotency key ni restricciones por operación/tenant | Reintentos HTTP crean trabajos distintos. Añadir clave, hash del contrato y respuesta reproducible |
| Alta | No hay pruebas, CI ni migraciones | No se detectan regresiones del API, seguridad ni conservación de datos |
| Media | Rutas, auth, DB y publicación acopladas; import requiere secretos | Dificulta inyectar dependencias y probar sin infraestructura |
| Media | Sin modelos de respuesta ni esquema de errores publicado | OpenAPI incompleto; no hay contrato verificable con desarrollo |
| Media | Auth acepta token sin prefijo Bearer; secreto ausente provoca error no controlado | Requiere parsing estricto y fallo cerrado sin revelar configuración |
| Media | Readiness sin timeouts explícitos y sin comprobar versión del esquema | Puede bloquear o declarar disponible una base no migrada |
| Media | Sin eventos persistentes de auditoría, actor ni correlación | No puede reconstruirse quién pidió qué ni cada transición |
| Media | Sin modelo de instancias ni relación tenant/job/recurso | No hay inventario de estado deseado/observado ni ownership |
| Media | Redis sin autenticación/TLS, worker con egress general, roles DB compartidos | Aceptable solo bajo límites del laboratorio; requiere segmentación y mínimo privilegio antes de habilitar IaC |
| Media | Imágenes con tags mutables y dependencias fijadas sin proceso de revisión | Falta escaneo y política de actualización/digests; no se presume que las versiones sean seguras |
| Media | Backup/restore y propiedad del despliegue pendientes (`NEXT_GATES.md`) | No considerar este runtime protegido ni listo para producción |
| Baja | Scripts smoke usa sudo y espera fija; preflight no automatiza decisiones | Útiles para laboratorio; complementar con pruebas reproducibles aisladas |

## Incrementos propuestos

1. **Fundación compatible (este PR).** App factory, routers, dependencias
   inyectables, servicio de jobs, contratos Pydantic, OpenAPI exportado,
   Alembic con adopción validada de la tabla bootstrap y paso de migración en
   Compose. Auth Bearer estricto, timeouts y readiness de esquema. Pruebas de
   rutas, migraciones, conservación de jobs y flujo con worker real.
2. **Identidad, tenants e inventario.** Elegir emisor corporativo y validar
   firma/issuer/audience/expiración; actor, roles y scopes. Modelar tenants,
   instancias y relaciones; todas las consultas y permisos acotados por tenant.
   Definir adopción explícita de jobs de laboratorio, sin asignar ownership
   arbitrario. Pruebas negativas de acceso cruzado y escalada de privilegios.
3. **Entrega y ejecución confiables.** Idempotencia transaccional por tenant y
   operación, outbox, publicación recuperable, máquina de estados persistente,
   claims atómicos, límites de concurrencia, leases/fencing y reconciliación.
   Pruebas de duplicados, carreras, timeouts, caídas y recuperación.
4. **Auditoría y contratos de desarrollo.** Eventos append-only con actor,
   tenant, recurso, request ID y transiciones; política de redacción y retención.
   Logging estructurado, contratos de errores y OpenAPI versionado con pruebas.
5. **Preparación del Terraform Worker.** Protocolo `IaCExecutor` para
   plan/apply/inspect/reconcile, resultados estructurados y errores recuperables;
   simulador determinista sin shell, red ni Proxmox. Vincular ejecución a un
   plan/version/state autorizado. Probar bloqueos y recuperación con el simulador.
   Referenciar `CyberFuel/iac-proxmox`; no copiar módulos Terraform.

## Límites del primer incremento

La API conserva el token de laboratorio y acceso global a jobs; no es todavía
multi-tenant. No se promete entrega exactamente una vez, idempotencia, auditoría
completa ni recuperación de jobs. La simulación y el ejecutor real siguen
pendientes. `POST /v1/instances` permanece bloqueado. Estas limitaciones impiden
habilitar aprovisionamiento y exposición a clientes.

Toda migración del entorno existente requiere revisión, backup consistente y
aprobación de despliegue. Ninguna instrucción de este documento autoriza actuar
en VM 104. Este PR no despliega ni elimina volúmenes.
