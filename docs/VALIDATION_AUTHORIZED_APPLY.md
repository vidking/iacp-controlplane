# Evidencia del ejecutor autorizado

Validación local del 21 de octubre de 2026, basada en 2B `0c7219d`.

| Comprobación | Resultado |
|---|---|
| `bash scripts/validate-docker.sh` | 159 pruebas aprobadas, 33.00 s; 1 aviso de deprecación de la dependencia de pruebas |
| Migración 0004 | Tabla nueva, comparación de modelos y preservación de tablas/datos anteriores; downgrade bloqueado |
| PostgreSQL 17 + Redis 7 + Celery | Cola de apply, claim único, reinicio de API y exclusión por instancia con adaptador simulado |
| Autorización | Rol approver separado, sujeto independiente, tenant, caducidad y vínculo de plan |
| Idempotencia y recuperación | Solicitudes repetidas, publicación ambigua, duplicados, cancelación y conciliación sin reaplicar |
| Ejecutor | Verificación de plan/backend/variables, argumentos fijos de apply, consulta refresh-only y filtrado del resultado |
| Límites | Terminación por timeout/cancelación y límite de salida de subprocesos |
| Imagen API | UID/GID 10001; código root:root, legible y no escribible; Alembic heads e importaciones sin red |
| `IACP_IMAGE_TARGET=terraform-worker bash scripts/test-image-permissions.sh` | Aprobada; Terraform 1.16.6, Git 2.47.3 y migración 0004 disponibles |
| Recreación del entorno de desarrollo | Job histórico, inventario simulado, Redis, secretos y nombres/fechas de volúmenes conservados |

La salida del script incluye comprobaciones JSON `status=passed` del job, del
inventario y de persistencia antes/después de recrear contenedores. Los proyectos
se crean con identificadores independientes; los volúmenes quedan conservados.
No se usaron secretos del servidor ni se accedió a VM 104.

No validado en esta entrega: conexión a Wasabi/Proxmox, locking real nuevo,
clonación de una KVM, tamaño efectivo del disco en un invitado y duración real de
una clonación. Requieren configuración y autorización de laboratorio. El baseline
Wasabi y las pruebas nativas del módulo de disco permanecen documentados en
`iac-proxmox`; no se duplicó Terraform en el Control Plane.

Reproducción desde un clon autorizado con Docker local:

```sh
bash scripts/validate-docker.sh
IACP_IMAGE_TARGET=terraform-worker bash scripts/test-image-permissions.sh
```

No ejecutar los scripts sobre un daemon remoto ni combinarlos con los Compose del
servidor. `apply` y `destroy` reales no forman parte de ninguna prueba.

Validación conjunta posterior a la integración externa de ejecutor y TLS:
172 pruebas aprobadas en Docker (27.47 s), incluida la regresión HTTP 409 cuando
el plan cambia antes de encolar apply. La prueba HTTPS también pasó sobre la
combinación anterior de 171 pruebas; el ajuste posterior solo afecta ese conflicto
HTTP. La validación conjunta no realizó un merge ni ejecutó Terraform real.
