# Manual del Control Plane

Este manual describe el código propuesto para IACP, API 0.7.0 (propuesta; runtime e03807d continúa en 0.6.0), y separa las
pruebas locales de la validación pendiente sobre una KVM de laboratorio.
Forgejo es la fuente oficial; ningún procedimiento depende de un equipo personal.
La URL de integración es **https://iac.cyberfuel.com**.

El incremento [KVM Lifecycle API](../KVM_LIFECYCLE_API.md) parte de main con
PR #18 y del hito confirmado de VMID 106, intento 4 completed. Agrega consulta de
energía y operaciones asíncronas; las mutaciones permanecen bloqueadas hasta la
certificación operativa explícita. El contrato del PR #7 de iac-proxmox ya está
promovido en código. [La promoción y certificación histórica](../KVM_POWER_PROMOTION.md)
describe despliegue con migraciones 0005/0006, certificación de VM 106, activación
y rollback compatible, conservando Apply=1.

| Equipo | Lectura principal | Responsabilidad |
|---|---|---|
| Desarrollo | [Integración API](API_INTEGRATION.md), [estados y errores](STATES_ERRORS.md) | Identidad de servicio CRM, claves idempotentes, consulta de resultados |
| Infraestructura | [Despliegue y rollback](OPERATIONS.md), [state y recuperación](STATE_RECOVERY.md), [demostración](LAB_DEMO.md) | Imágenes, respaldos, asignaciones autorizadas, redes y estado Terraform |
| Seguridad | [Permisos y aislamiento](SECURITY.md), [arquitectura](ARCHITECTURE.md) | Revisión de identidades, aprobación independiente, secretos, TLS y egress |
| Todos | [Capacidades y FAQ](CAPABILITIES_FAQ.md), [evidencia](VALIDATION.md) | Distinguir implementado, simulado, pendiente y validado |

La base verificada `main` 2ed4104 ya incorpora 2B, el ejecutor autorizado
(integrado externamente mediante PR #9) y TLS (PR #8). La corrección posterior del
conflicto HTTP del plan se revisa por separado. El override TLS requiere
configuración externa autorizada; su integración no lo habilita en el servidor.
Este manual no incorpora código de esas entregas. Fijar el commit final aprobado
y los digests antes de cualquier ventana; no desplegar nombres de ramas móviles.

La demostración no está autorizada por este manual. No se ejecutó apply, destroy,
merge, despliegue ni acceso a VM 104 durante la preparación de estos cambios.
La eliminación API descartable está implementada en esta rama y permanece OFF;
requiere migración 0007, propiedad y aprobación separada. No se ha ejecutado en MNG.
Ver [DELETE seguro](../SAFE_DELETE.md).
