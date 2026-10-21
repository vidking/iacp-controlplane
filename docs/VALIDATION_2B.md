# Validación del ejecutor Terraform y del disco KVM

Fecha: 2026-10-21. Base: 2A integrado en `main`, commit
`4e764f4` (merge del PR #5). El módulo compartido se referencia por commit
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4`, publicado en su PR independiente.
Se utilizó únicamente Docker local; no se accedió a VM 104 ni a servicios reales
Proxmox/Wasabi y no se ejecutó ningún apply/destroy de infraestructura.

## Control Plane

| Ejecución | Resultado |
| --- | --- |
| `bash scripts/validate-docker.sh` | Aprobada: 129 pruebas, API/Traefik, Celery y persistencia tras recreación |
| `bash scripts/test-integration.sh` con los últimos controles de cancelación/backend y orden de locks | 132 aprobadas, incluidas 14 de integración; 18.23 s |
| `IACP_IMAGE_TARGET=terraform-worker bash scripts/test-image-permissions.sh` | Aprobada como UID/GID 10001, Terraform CLI 1.16.6 y Git disponibles |
| Imagen API por defecto desde contexto con archivos 600/directorios 700 | Aprobada; código root:root legible sin escritura |
| Alembic heads en imagen | `0003_terraform_plans (head)` |
| Compose efectivo con servidor, tenants y override Terraform | `config --quiet` aprobado con rutas/nombres ficticios exclusivos de validación, sin ejecutar up |
| Sintaxis Bash, compilación Python, diff y contrato OpenAPI | Aprobados |

La suite conserva la advertencia de deprecación de Starlette/AnyIO existente,
sin pruebas omitidas ni fallos en la ejecución final.

Cobertura adicional a 2A:

- Cola Celery dedicada y PostgreSQL reales con ejecutor Terraform simulado;
  consulta del plan persistido después de recrear la aplicación.
- Advisory lock PostgreSQL entre conexiones, entrega duplicada y liberación del
  lock antes de devolver la conexión al pool. Confirmación de publicación ambigua
  durante el claim del worker, con un orden consistente de locks ejecución/job.
- Cancelación antes de consumir, durante ejecución y después de preparar el
  plan; invalidación del SHA actual y limpieza del resultado de instancia.
- Timeout y cancelación de un subproceso real local, terminando su grupo;
  stderr separado de JSON y rechazo de comandos apply/destroy/state push.
- Reintentos idempotentes, fallos de publicación y recuperación de queued sin
  publicación o running interrumpido; los mensajes de intentos viejos no ejecutan.
- Una sola reserva VMID; asignación/state congelados entre intentos; rechazo de
  VM 104, plantilla/destino iguales, nodo ajeno y entradas inválidas.
- Política de plan que rechaza borrado, reemplazo, discrepancias de recursos y
  state ajeno; SHA del binario y vínculo del manifiesto con entradas/backend.
- Aislamiento de tenant, reader sin permiso de mutación y apply siempre bloqueado.
- `0003` desde una base 2A poblada conserva tenants, instancias, operaciones,
  eventos y jobs. Las pruebas existentes también conservan el bootstrap sin versión.

`0001` y `0002` son idénticas a la base aprobada; `0003` solo añade dos tablas.
No cambiaron los Compose base, de desarrollo ni de servidor, ni sus volúmenes.

## Persistencia tras recreación

El proyecto `iacp-dev-check-1792600203-16827` completó smoke y simulación de 2A
por Traefik y worker, recreó sus contenedores y volvió a completar el flujo.
El ensayo informó:

```json
{"status":"passed","bootstrap_job_preserved":true,"redis_marker_preserved":true,"secrets_preserved":true,"simulated_inventory_preserved":true}
```

Se conservaron los volúmenes `dev_postgres_data`, `dev_redis_data` y `dev_secrets`
del proyecto, comprobando igualdad de nombre y fecha de creación. Solo se
retiraron contenedores/redes. No se usaron ni eliminaron volúmenes del servidor.

## Módulo original iac-proxmox

`bash scripts/test-docker.sh` aprobó con Terraform CLI 1.16.6 y
`bpg/proxmox 0.116.1`, usando el lockfile existente, backend deshabilitado y
provider simulado:

- `terraform fmt -check -recursive` y `validate` aprobados.
- Módulo KVM: 8 pruebas nativas aprobadas.
- Stack KVM: 5 pruebas nativas aprobadas.
- Contratos previos sin boot_disk, configuración de tamaño/opciones, rechazo de
  reducción/interfaz inválida y transmisión del disco desde la raíz al módulo.

Ninguna prueba nativa utiliza `command = apply`. Los valores de red, nodos,
plantillas, storage y claves de los fixtures no autorizan recursos reales.

## Límites y siguiente validación

La suite no certifica conectividad ni permisos efectivos de un token Proxmox,
acceso Wasabi, un plan remoto real o crecimiento de disco/filesystem. La evidencia
anterior del baseline Wasabi permanece en su repositorio; no fue reejecutada.
Los parámetros exactos que debe aprobar el operador están en
[TERRAFORM_EXECUTOR.md](TERRAFORM_EXECUTOR.md). La falta de credenciales no impide
repetir estas pruebas. No se realizaron merge, migración operativa ni despliegue.
