# Evaluación E4: backend IaC sintético aislado

Fecha: 24/10/2026, `America/Costa_Rica` (UTC−06:00). Código evaluado:
`origin/main=94cc0fb`; aplicación del IACP Control Plane (VM 104): IACP 0.10.1,
`7c0804d`. Alcance: evaluación técnica, sin instalar un simulador.

## Hallazgo y decisión

`app/iacp/executors.py::SimulatedExecutor.create` devuelve inmediatamente
`ExecutionResult(details={"simulated": True, ...})`. El servicio de ejecución
genérico persiste queued/running/completed o failed y eventos `worker:simulated`,
pero ese adaptador no modela duración variable, etapas de Terraform, plan/apply,
locks remotos, redelivery ni estados `uncertain` del backend real. Por ello no
sirve como sustituto de las mediciones de aprovisionamiento real de E5.

La instancia activa usa `IACP_INSTANCE_EXECUTOR=terraform`; se comprobó su
etiqueta/entorno permitido sin cambiarla. No se añade ningún selector de backend,
flag falso, ruta ni excepción a las guardas comerciales. Las pruebas existentes
con fixtures no certifican una nueva plataforma sintética.

**PASS de evaluación técnica; simulador de resistencia no implementado ni
ejecutado.** Se prioriza medir el runtime real con GET y mantener la separación
de resultados. Implementarlo ahora no elimina el límite de admisión HTTPS ni
aporta evidencia de recuperación física de un worker.

## Diseño mínimo para un ensayo futuro

| Componente | Aislamiento exigido | Comportamiento comprobable |
|---|---|---|
| API sintética | Proyecto/host/origen propios; nunca VM 104 | Intenciones idempotentes y tenants exclusivamente de prueba |
| PostgreSQL | DB/usuario/volumen independientes | Operaciones, eventos y timestamps; ninguna conexión a la DB activa |
| Redis y workers | Instancia, colas y procesos propios | Esperas variables, cola observable, redelivery y progreso |
| Adaptador | Inyección explícita al construir solo la aplicación de ensayo | Máquina de estados, fallos deterministas y deadlines; sin acceso al ejecutor real |
| Red | Sin rutas de salida a Proxmox ni Wasabi | Prueba de denegación de conexión a ambos destinos |
| Credenciales | Identidades ficticias del entorno separado | Sin mounts ni copias de secretos del IACP Control Plane (VM 104) |
| Evidencia | Etiqueta `sintética`, semilla y guion fijados | Nunca se combina con PASS de aprovisionamiento físico |

Un guion debería cubrir queued → running → completed; errores antes/después de
la aceptación; queued/running → failed; timeout → uncertain; duplicación de
mensaje con la misma intención; cancelación y eventos repetidos. Los retardos
deben ser acotados y reproducibles. Sus percentiles medirían scheduler, API y
persistencia sintéticos, sin certificar clonación, Cloud-Init, DHCP, SSH,
consistencia de Terraform State ni recuperación real de infraestructura.

El paso previo a ejecutarlo es demostrar aislamiento de DB, Redis, procesos,
red y credenciales. No basta otro nombre de cola. La muerte física del worker
compartido conserva el dictamen **BLOCKED** y la resiliencia integral conserva
**NO-GO**.
