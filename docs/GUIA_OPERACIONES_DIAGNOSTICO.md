# Operaciones y diagnóstico

IACP separa API, Redis/Celery, PostgreSQL, IaC Worker y Lifecycle Worker. La API registra la intención; los workers ejecutan fuera de la petición HTTP y dejan eventos auditables.

Los estados de operación son `pending`, `running`, `completed`, `failed` y `uncertain`. `uncertain` requiere observación del backend y reconciliación por personal autorizado; nunca se repite automáticamente una mutación.

Conserva `operation_id`, `job_id`, `instance_id`, `X-Request-ID` y timestamps. Consulta las operaciones desde la API y el estado operativo mediante el procedimiento autorizado del Control Plane. Si hay colas detenidas, errores sostenidos, locks no terminales o discrepancias entre PostgreSQL y Proxmox, detén nuevos reintentos y escala a Infraestructura. No fuerces locks, no borres state y no ejecutes comandos administrativos desde Desarrollo.

[Ver fuente versionada](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_OPERACIONES_DIAGNOSTICO.md).
