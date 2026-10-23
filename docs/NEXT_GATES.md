# Gates posteriores al primer smoke

Actualización go-live 2026-10-23: el respaldo y su restauración aislada ya fueron
aprobados; no repetir ese ensayo. La inspección actual confirma esquema 0004,
sin operaciones no terminales ni colas pendientes. Migraciones/runtime, registro
de certificación y energía siguen pendientes de ventana. DNS público devuelve
192.168.1.50 y no hay listener 443. Seguir [el procedimiento consolidado](GO_LIVE_MVP.md),
[controles de exposición](GO_LIVE_SECURITY.md) y [evidencia](validation/GO_LIVE_ENVIRONMENT.json).
Los puntos siguientes conservan su contexto histórico; no invalidan controles
ya acreditados ni autorizan repetir cambios operativos.

1. Registrar VM 104 e IP 192.168.1.50/20 en inventario/IPAM.
2. Verificar que el disco `scsi0` de VM 104 **no** lleve `backup=0`.
3. Configurar respaldo PBS y ensayar restore **aislado** de la VM 104.
4. Implementar backup lógico de PostgreSQL, cifrado y custodia externa.
   Bloqueo confirmado el 2026-10-21: no hay respaldo; no continuar el clon,
   migración o despliegue en VM 104 hasta acreditar backup y restauración.
5. Migraciones Alembic implementadas en incremento 1; pendiente validar backup y
   aprobar su ejecución en VM 104. API y worker esperan al servicio `migrate`.
6. Tener contrato OpenAPI revisado por desarrollo y control de autenticación/RBAC.
7. Incorporar outbox, reconciliación, locks DB y Wasabi, idempotency-key por tenant.
8. Worker Terraform CLI aislado y sin Docker socket; plan exacto aprobado;
   reutilizar módulo KVM existente y sus versiones/credenciales de servicio.
9. No ejecutar operaciones LXD hasta cerrar provider y ownership.
10. Si Portainer corporativo administra deploy desde Forgejo, elegirlo como
    único reconciliador del stack. Hasta entonces Compose CLI es dueño.
