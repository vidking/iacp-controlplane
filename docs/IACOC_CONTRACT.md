# IaCoC: contrato mínimo independiente

IaCoC consume evidencia y solicita recuperación a IACP Orchestrator. No es un
motor de aprovisionamiento y no tiene acceso de escritura a PostgreSQL o Wasabi.
Todas las llamadas /v1 usan Bearer, tenant y RBAC existentes.

`GET /v1/events?after=0&limit=100` entrega `iacp.audit.v1`: event_id, correlation_id,
operation_id, instance_id, tenant_id, occurred_at, kind, status, code, actor y service.
Correlation se fija al admitir operaciones nuevas. Para historia sin correlation_id
se publica operation_id como fallback, sin reescribir filas antiguas.

El cursor permite replay y deduplicación por event_id. No representa un outbox
transaccional: IDs asignados dentro de transacciones concurrentes pueden confirmar
fuera de orden. Solapar páginas/ventanas y reconciliar el historial de cada operación;
para una garantía de entrega sin pérdida hace falta un publicador/outbox posterior.
El feed no incluye cuerpos, secretos, stderr ni contraseñas de clientes.

`GET /v1/observability` entrega `iacp.health.v1`: dependencias observadas,
operaciones activas/inciertas y edad de la operación activa más antigua por tenant.
Incluye Apply incierto aunque el estado histórico de creación diga failed.
`worker_liveness=unknown` es explícito: datos persistidos no acreditan un proceso
worker vivo. No se exponen profundidades globales de colas de otros tenants.
`/health/live` y `/health/ready` conservan sus contratos y verificaciones DB/esquema/
Redis. Un colector operativo readonly puede complementar colas, clientes, pools y
heartbeats sin convertirse en otro ejecutor.

| Clase | Operación | Control |
|---|---|---|
| Readonly | GET inventario, operaciones, eventos, energía, diagnóstico | reader del tenant |
| Recuperación segura | POST /instances/{id}/reconcile | operator; solo observa comando consumido |
| Observación de acceso | POST /instances/{id}/access/verify | operator, propiedad, clave y lock; no cambia contraseña |
| Continuación autorizada | POST /instances/{id}/provision o retry-provision | operator; política/delegación/fences o expected_attempt |
| Mutación | creación, Lifecycle, DELETE | operator, propiedad, cuotas, habilitación y política aplicables |
| Aprobación | revisión de plan/política | approver independiente, digest exacto y vigencia |

IaCoC debe mostrar qué clase solicita antes de emitirla. No resetea fences ni
marca completed por SQL, no escribe/reemplaza state y no usa `force-unlock` como
recuperación automática. Una incidencia incierta se observa sobre el mismo UUID.

Umbrales iniciales de laboratorio: alerta con cualquier operación incierta,
antigüedad de cola >60s, falta de progreso >120s, dependencia no ready >30s,
HTTP inesperados >=1% en A, p95>=500ms o p99>=1500ms. Medir tiempo completo de
provisión/Cloud-Init/SSH; deadline inicial 900s. Estos límites deben calibrarse con
resultados reales. No se instala todavía UI, nuevo servicio, agente ni publicador.
