# Control Plane técnico simplificado

## Alcance

Esta revisión convierte el repositorio `iacp-controlplane` en un orquestador
técnico de KVM para el clúster MNG (`mng01` y `mng02`). El repositorio y el
orquestador `iacp-controlplanev2` permanecen independientes y no forman parte
de la migración: no se leen sus operaciones, estados, volúmenes ni registros de
VM, y no se corrigen sus estados históricos.

El runtime nuevo se ejecuta directamente en el **IACP Control Plane (VM 104)**
mediante `compose.technical.yaml`. Portainer no es una dependencia del runtime
nuevo.

## Modelo operativo

- FastAPI técnico y contrato directo de imágenes, planes, instancias y
  operaciones.
- PostgreSQL dedicado para operaciones, eventos, workers, instancias y leases
  de VMID. El volumen técnico es independiente del volumen histórico del
  runtime anterior.
- Seis procesos de worker (`worker-1` a `worker-6`) con un límite global de dos
  operaciones físicas simultáneas.
- Terraform 1.16.6 con el provider Proxmox `bpg/proxmox` 0.116.1.
- State remoto por instancia en Wasabi, con una clave derivada del
  `instance_id`, locking de Terraform y credenciales montadas únicamente en
  los workers.
- QEMU Guest Agent para esperar Cloud-Init, configurar acceso técnico y
  verificar SSH root. En MNG la contraseña root temporal es obligatoria para
  completar la verificación; es efímera y se cifra con Fernet hasta su consumo
  o expiración. Nunca se guarda en el payload ni en el State.
- Rango de asignación por defecto `30000–39999`, con consulta del inventario
  real de Proxmox y un advisory lock PostgreSQL. Los leases históricos no se
  reutilizan automáticamente.

## Separación de privilegios

La API recibe únicamente el token, el acceso cifrado, PostgreSQL y una
credencial Proxmox separada de solo lectura para observar energía. Los workers
son los únicos servicios que reciben credenciales Proxmox mutables, Wasabi y la
clave SSH pública por defecto. Ningún secreto se imprime en logs, respuestas API
o Terraform State.

La red `backend` es interna. Los workers tienen la salida necesaria hacia
Proxmox y Wasabi; el API publica únicamente el puerto interno del Compose para
el ingress configurado del **IACP Control Plane (VM 104)**.

## Migración y rollback

La revisión `0001_technical_schema` se aplica mediante el servicio `migrate`
antes de API y workers. No se ejecuta `docker compose down -v`, no se eliminan
volúmenes y no se reutiliza el State del orquestador anterior. El rollback de
software conserva PostgreSQL, `technical_data`, secretos, certificados y los
objetos de State remoto.

La primera activación debe ejecutarse con el Compose canónico, comprobar
`/health/ready`, registrar los seis heartbeats y confirmar que el API y todos
los workers usan la misma imagen aprobada. Los ensayos físicos deben usar como
máximo dos aprovisionamientos simultáneos y detener nuevas inyecciones ante
`FAIL`, `UNCERTAIN`, timeout o pérdida de evidencia.
