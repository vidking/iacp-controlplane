# Propiedad de energía KVM y operaciones Lifecycle

Estado: contrato IaC promovido tras el merge del PR #7; activación operativa
pendiente de certificación explícita. Sin número reservado.

Terraform conserva atributos declarativos y la energía inicial de creación.
IACP Lifecycle administra start, shutdown y reboot posteriores mediante tareas
Proxmox y operaciones asíncronas persistidas. No se utiliza Terraform como
ejecutor de comandos imperativos de energía.

La transferencia requiere `ignore_changes = [started]` en la revisión IaC
fijada, conservando `started = var.started` durante create. Ambos pins usan
`9aeb8ec846342d279a5d16be7c8e20157e9bf5ae`. La revisión anterior permanece
rechazada como propietaria de energía. Las instancias históricas requieren un
certificado auditado separado que vincula identidad, evidencia original, state
estable y plan local no-op. No se reescribe la revisión del aprovisionamiento;
actualizar los pins o habilitar el flag no certifica instancias históricas.
La migración aditiva 0006 conserva intacta la 0005 y no genera certificados.

Se reutilizan autenticación por tenant, operación/job/evento, PostgreSQL, Celery,
Redis y advisory lock por instancia. Una reserva persistente admite una sola
operación activa. Se consume el envío antes del POST, se persiste UPID y se
confirman tarea y energía. Replays posteriores observan; un envío de resultado
desconocido no se repite. No se promete exactly-once entre dos sistemas sin
transacción distribuida ni idempotencia nativa.

Running describe energía de Proxmox y no prueba salud del SO, agente, Cloud-Init
o accesibilidad. No se implementa apagado forzado automático. El worker no tiene
Terraform CLI, secretos AWS ni acceso al state.

La incertidumbre bloquea operaciones contradictorias y conserva evidencia para
recuperación o auditoría. El rollback funcional pausa admisión, conserva la
migración y observa tareas existentes. La devolución de energía a Terraform no
forma parte de ese rollback.

Contratos, propuesta IaC, límites, pruebas y procedimiento de primera operación:
[KVM_LIFECYCLE_API.md](../KVM_LIFECYCLE_API.md).
Certificación, despliegue, activación y rollback:
[KVM_POWER_PROMOTION.md](../KVM_POWER_PROMOTION.md).
