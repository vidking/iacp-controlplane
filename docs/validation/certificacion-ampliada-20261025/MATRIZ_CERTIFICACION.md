# Matriz de certificación por SO y funcionalidad

Fecha: 25/10/2026, America/Costa_Rica. **VALIDADO** exige evidencia física del alcance indicado; **PENDIENTE** significa no demostrado; **FALLIDO** requiere una prueba ejecutada que no cumplió el criterio. No hay nuevos resultados FALLIDO en esta inspección de lectura.

| Sistema operativo | Plantilla existente | Registro GET /v1/images | KVM 1 completo | KVM 2 completo | KVM 3 completo | KVM 4 completo | Disponible para crear hoy |
|---|---|---|---|---|---|---|---|
| Ubuntu 24.04 | VALIDADO · 29000 | VALIDADO | VALIDADO | VALIDADO | PENDIENTE | PENDIENTE | Sí, solo planes autorizados |
| Ubuntu 26.04 | VALIDADO · 29001 | VALIDADO · pendiente | PENDIENTE | PENDIENTE | PENDIENTE | PENDIENTE | No |
| AlmaLinux 9 | VALIDADO · 29010 | VALIDADO · pendiente | PENDIENTE | PENDIENTE | PENDIENTE | PENDIENTE | No |
| AlmaLinux 10 | VALIDADO · 29011 | VALIDADO · pendiente | PENDIENTE | PENDIENTE | PENDIENTE | PENDIENTE | No |

| Funcionalidad | Ubuntu 24.04 KVM 1/2 | Otras combinaciones | Evidencia / límite |
|---|---|---|---|
| CPU/RAM/disco nominal | VALIDADO | PENDIENTE | Ensayos Fase G: 1/2048/50 y 2/4096/100 |
| Partición y filesystem | VALIDADO | PENDIENTE | Geometría/espacio documentados; no inferir soporte AlmaLinux |
| Cloud-Init y DHCP | VALIDADO | PENDIENTE | Advertencia DEPRECATED conservada |
| QEMU Guest Agent | VALIDADO | PENDIENTE | Configuración agent=enabled no prueba ejecución en el invitado |
| Root PAM y SSH con llave | VALIDADO | PENDIENTE | Root SSH por contraseña no habilitado |
| Bootstrap sin plaintext persistido | VALIDADO con alcance | PENDIENTE | KVM 1: PG/Redis/state/artefactos; KVM 2 añade barrido final de logs; confidencialidad adicional por pruebas de código |
| Start/shutdown/reboot | VALIDADO final | PENDIENTE | Timeout ACPI inicial KVM 1 recuperado y conservado |
| DELETE/state/lock/limpieza | VALIDADO | PENDIENTE | No se borran versiones históricas del state |
| WAN VLAN 204 | PENDIENTE | PENDIENTE | Bridge permite tag; conectividad extremo a extremo no demostrada |
| Recuperación física de worker / HA | PENDIENTE · NO-GO integral | PENDIENTE | No realizar inyecciones en dependencias compartidas |

Fuente de validación física: [cierre de Fase G](../phase-g/cierre-mvp-20261025/INFORME.md). Fuente actual de disponibilidad: [inventario-runtime.json](inventario-runtime.json). La matriz no convierte evidencias anteriores en nuevas ejecuciones.
