# Instrucciones de colaboración de IACP

Estas instrucciones complementan las guías operativas existentes; no sustituyen sus controles de promoción, revisión o recuperación.

## Idioma obligatorio

Redactar en español los nuevos mensajes de commits, títulos y descripciones de PR, documentación técnica, auditorías, conclusiones, reportes y explicaciones de evidencias. Se conservan los nombres técnicos originales de endpoints, métodos, bibliotecas, tecnologías, parámetros y archivos.

Ejemplos: `fix(worker): corregir supervisión de procesos de aprovisionamiento`, `perf(operaciones): reducir consultas redundantes` y `docs(certificación): registrar evidencias de recuperación`.

No reescribir el historial Git fusionado para traducirlo. Conservar las evidencias originales y añadir traducciones o aclaraciones fechadas que identifiquen su fuente. Una traducción no cambia el resultado histórico.

## Nomenclatura obligatoria de infraestructura

- **MNG:** clúster Proxmox de laboratorio, compuesto por `mng01` y `mng02`.
- **IACP Control Plane (VM 104):** plataforma de orquestación de `iacp-controlplane-01`, actualmente alojada en `mng01`.
- **VM 106:** instancia histórica de laboratorio protegida.
- **VM 29000:** Golden Image Ubuntu 24.04 protegida.
- **Platinum/PRX:** clúster Proxmox de producción, independiente de MNG y excluido de estos ensayos.

Nunca usar MNG como sinónimo de la VM 104 ni del Control Plane. Al desplegar, actualizar, reiniciar o supervisar contenedores del orquestador, mencionar expresamente el **IACP Control Plane (VM 104)**. Al crear, clonar, eliminar o administrar VMs de prueba, mencionar el **clúster MNG** e indicar el nodo cuando corresponda. Aplicar esta convención también a las fases C y D.

La VM 104 permanece protegida frente a aprovisionamiento, Lifecycle y eliminación desde la API de IACP. Esta protección permite los despliegues controlados de software dentro del propio Control Plane. Conservar también la protección de la VM 106 y la VM 29000.

## Promoción, ensayos y evidencias

Comprobar `origin/main`, versión realmente ejecutada, imágenes, servicios, esquema, operaciones, colas y locks antes de modificar el runtime. No equiparar código integrado, código desplegado y comportamiento certificado.

Utilizar el procedimiento vigente de `scripts/iacpctl` y `scripts/go_live.py`, el Compose canónico y manifiestos revisados. Preservar volúmenes, configuraciones, secretos, credenciales y certificados. Nunca ejecutar `docker compose down -v`, alterar producción, borrar states/versiones históricos ni forzar desbloqueos para aprobar una prueba.

Los ensayos físicos requieren aislamiento demostrado por cola, proceso, instancia y credenciales de prueba. No interrumpir workers ni dependencias compartidos. Detener nuevas inyecciones ante incertidumbre; conservar la intención y requerir recuperación controlada. Mantener las cuotas autorizadas y como máximo dos aprovisionamientos simultáneos.

Registrar PASS, FAIL o BLOCKED con alcance, versión, fecha y evidencia. Distinguir recuperación automática, manual y contención fail-closed. Separar el dictamen de integración piloto del dictamen de certificación integral. No declarar GO integral con escenarios críticos pendientes.

No imprimir ni incorporar tokens, contraseñas, valores de secretos o copias sensibles de Terraform State a Git. La revisión y eventual rotación de credenciales se rigen por el alcance autorizado; una observación pendiente no equivale a seguridad aprobada.
