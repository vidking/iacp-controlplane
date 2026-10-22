# Propiedad de energía KVM y operaciones Lifecycle

Estado: implementación propuesta; activación dependiente de promoción IaC
independiente. Sin número reservado.

Terraform conserva atributos declarativos y la energía inicial de creación.
IACP Lifecycle administra start, shutdown y reboot posteriores mediante tareas
Proxmox y operaciones asíncronas persistidas. No se utiliza Terraform como
ejecutor de comandos imperativos de energía.

La transferencia requiere `ignore_changes = [started]` en la revisión IaC
fijada, conservando `started = var.started` durante create. La revisión actual
carece de esta regla; la compuerta compilada permanece no promovida y no puede
habilitarse mediante configuración o parámetros cliente. Tras revisión del
cambio independiente, ambos pins deben promoverse al mismo commit inmutable.

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
