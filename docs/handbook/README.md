# Manual del Control Plane

Este manual describe el código propuesto para IACP, API 0.5.0, y separa las
pruebas locales de la validación pendiente sobre una KVM de laboratorio.
Forgejo es la fuente oficial; ningún procedimiento depende de un equipo personal.
La URL de integración es **https://iac.cyberfuel.com**.

| Equipo | Lectura principal | Responsabilidad |
|---|---|---|
| Desarrollo | [Integración API](API_INTEGRATION.md), [estados y errores](STATES_ERRORS.md) | Identidad de servicio CRM, claves idempotentes, consulta de resultados |
| Infraestructura | [Despliegue y rollback](OPERATIONS.md), [state y recuperación](STATE_RECOVERY.md), [demostración](LAB_DEMO.md) | Imágenes, respaldos, asignaciones autorizadas, redes y estado Terraform |
| Seguridad | [Permisos y aislamiento](SECURITY.md), [arquitectura](ARCHITECTURE.md) | Revisión de identidades, aprobación independiente, secretos, TLS y egress |
| Todos | [Capacidades y FAQ](CAPABILITIES_FAQ.md), [evidencia](VALIDATION.md) | Distinguir implementado, simulado, pendiente y validado |

El incremento 2B y el ejecutor autorizado requieren integración antes de usar los
endpoints nuevos. El override TLS es independiente y requiere configuración
externa autorizada. Revisar las dependencias de los PRs en Forgejo y fijar los
commits finales integrados; no desplegar nombres de ramas móviles.

La demostración no está autorizada por este manual. No se ejecutó apply, destroy,
merge, despliegue ni acceso a VM 104 durante la preparación de estos cambios.
La eliminación de laboratorio requiere un procedimiento separado de Infraestructura;
la API de eliminación todavía no está implementada.
