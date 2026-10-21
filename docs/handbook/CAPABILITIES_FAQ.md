# Capacidades y FAQ técnico

Estado del código propuesto; no equivale a una validación nueva de producción.

| Capacidad | Implementación | Evidencia / validación pendiente |
|---|---|---|
| FastAPI modular, PostgreSQL/Redis/Celery/Compose | Implementada | Suite Docker y recreación del entorno de desarrollo |
| Tenants, inventario, operaciones, jobs/eventos | Implementada | API, autorización, migraciones y persistencia probadas |
| Tokens de servicio y roles reader/operator/approver | Implementada, control mínimo | Separación de tenant/sujeto y permisos probados; OIDC/RBAC granular pendientes |
| Creación idempotente | Implementada | Transacciones y concurrencia PostgreSQL probadas |
| Aprovisionamiento simulado | Implementado y predeterminado | Flujo completo sin Proxmox; no representa una KVM real |
| Terraform plan y módulos reutilizados | Implementado y optativo | Adaptador/cola simulados; configuración y plan reales de laboratorio pendientes |
| Disco configurable en iac-proxmox | Implementado en PR independiente | 13 pruebas nativas del módulo/stack documentadas en ese repositorio; invitado real pendiente |
| Wasabi aislado por instancia, locking requerido | Implementado sobre baseline existente | Argumentos/política probados; revalidación del backend real pendiente |
| Apply de plan aprobado | Implementado, deshabilitado por defecto | Gate doble, claim, caducidad y simulación probados; ningún apply real ejecutado |
| VMID/nodo/recursos/encendido observados | Implementado | Proyección y validación del provider probadas con fixtures; KVM lab pendiente |
| Recuperación de apply incierto | Implementada, explícita, solo observación | Fallos/cancelación/reintentos probados; incidentes reales requieren runbook |
| HTTPS/allowlist/Origin/certificados externos | Preparado en PR TLS independiente | Traefik real probado en Docker con certificado efímero; DNS/CA/firewall corporativos pendientes |
| Contrato CRM/OpenAPI y ejemplos oficiales | Implementado/documentado | Snapshot y pruebas; homologación desde backend CRM pendiente |
| Eliminación KVM por API | Pendiente | Guía de eliminación manual por Infraestructura con aprobación distinta |
| Conciliación periódica / inventario tras cambios externos | Pendiente | La observación actual ocurre tras apply o recuperación explícita |
| Cuotas, OIDC, rate limiting, webhooks, SIEM inmutable | Pendientes | Requieren un incremento posterior; no se simulan como implementados |
| Backup/restauración de producción | Requisito operativo, no confirmado aquí | Sin evidencia de restauración válida no se autoriza transición/migración |
| Merge/despliegue/VM 104 | Fuera de ejecución de estos PRs | Requieren aprobación y ventana separadas |

## Preguntas frecuentes

**¿Un HTTP 202 significa que la KVM existe?** No. Significa que la intención y el
job quedaron registrados. Consultar operación, ejecución e instancia.

**¿Por qué el job plan está completed y la instancia pending?** El plan terminó y
espera autorización. La operación continúa running hasta aplicar y observar.

**¿Puedo confiar en ready sin mirar executor?** No. Una instancia simulada también
puede estar ready. Usar infrastructure_kind, executor y result.simulated; para una
KVM revisar además observed_at y power_state.

**¿El CRM puede elegir tenant, VMID o red?** No en el contrato de creación. El
contexto autenticado determina tenant y la asignación externa determina recursos
de infraestructura autorizados. Una etiqueta image no concede permisos Proxmox.

**¿Cómo evito duplicados después de perder una respuesta?** Repetir la misma
clave/cuerpo, recuperar IDs y consultar. No crear otra intención para sortear un
error. Recover nunca reaplica un run iniciado.

**¿Se aplica el plan más reciente disponible?** Se aplica el binario cuyo hash e
intento fueron autorizados, tras verificar manifest, variables, backend y source.
No se genera un plan de creación nuevo dentro de apply.

**¿Qué ocurre si cambia el state después de aprobar?** Terraform puede rechazar
el plan obsoleto. El sistema no lo sustituye automáticamente: revisar el resultado
y started_at, conciliar si hubo intento de mutación y emitir otra aprobación solo
cuando sea seguro y exista un plan nuevo.

**¿Apagar apply cancela instantáneamente la tarea?** El worker revisa el gate en
sus controles periódicos. Si ya empezó la mutación, cualquier interrupción queda
uncertain y requiere observar al provider; no implica un rollback de recursos.

**¿Los timeouts están validados contra una clonación real?** Todavía no. El límite
por comando es 300 s y del flujo 900 s; medir laboratorio antes de una demostración
con una plantilla grande. Un timeout no garantiza que Proxmox haya cancelado su tarea.

**¿Se administrará la VM 104 mediante el state de un cliente?** No. Se rechaza
VMID/plantilla 104, se verifica pertenencia del state y cada instancia tiene una
clave nueva estable. No importar infraestructura del Control Plane.

**¿Por qué no hay DELETE de instancias?** El ejecutor vigente admite create/no-op.
La eliminación necesita su propio contrato, estados, aprobación y recuperación.
La guía lab utiliza un cambio separado de Infraestructura y conserva evidencia.

**¿Puedo borrar un lock o restaurar un state anterior para recuperar rápido?** No
sin inspección y autorización. Un objeto tflock antiguo puede tener un proceso
activo. Restaurar state no revierte los recursos y puede provocar duplicados.

**¿Se puede volver a la imagen 2A después de migrar 0004?** No se ha validado ese
rollback binario; readiness exige el head exacto. Conservar una imagen compatible
con 0004 y deshabilitar funciones nuevas; probar una imagen de compatibilidad o
restauración aislada antes de ofrecer una reversión de esquema.

**¿TLS queda activo al fusionar el PR?** No. El override exige certificados,
política y parámetros externos. La configuración activa del servidor no cambia.

**¿CORS autoriza al CRM?** No. Se filtra IP efectiva y Origin cuando existe; el
token y los permisos por tenant siguen siendo obligatorios. Un backend puede no
enviar Origin, por lo que CORS no sustituye una allowlist ni autenticación.

**¿Qué herramientas necesita otro equipo?** Git y Docker Compose para construir
y probar; no rutas personales, dependencias del escritorio ni Terraform del host.
Las imágenes incluyen sus runtimes. La autenticación Git y secretos son externos.
