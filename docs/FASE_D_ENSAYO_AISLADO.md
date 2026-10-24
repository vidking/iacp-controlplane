# Preparación mínima del ensayo de interrupción de Fase D

Fecha: 24/10/2026. **D3 físico: BLOCKED hasta demostrar todos los aislamientos siguientes.** Esta preparación no acredita recuperación física ni permite interrumpir un worker compartido del **IACP Control Plane (VM 104)**.

## Situación comprobada

Los workers ordinarios del IACP Control Plane (VM 104) consumen las colas compartidas `celery`, `terraform` y `lifecycle`. Los publicadores de planificación, Apply y continuaciones fijan colas concretas; añadir un consumidor con otro `-Q` no desvía automáticamente esa cadena. El inventario del driver exige los tres workers esperados: un consumidor adicional sobre el broker operativo alteraría ese inventario.

Las credenciales montadas en los workers pertenecen al servicio existente; no se ha demostrado un ámbito exclusivo para la nueva instancia y su state. No se creó una identidad ni una credencial de backend para este ensayo. D6 y la rotación de credenciales existentes quedaron diferidos por el usuario; esa decisión no demuestra el aislamiento necesario para una inyección.

La imagen corregida, los hashes del supervisor y las pruebas locales de SIGKILL acreditan código ejecutado y comportamiento de integración. Un E2E normal sobre el clúster MNG tampoco acredita muerte del worker durante clonación.

## Preparación necesaria

| Frontera | Preparación mínima | Evidencia exigida antes de inyectar |
|---|---|---|
| Cola | Broker Redis exclusivo y efímero; API de ensayo y todos sus workers conectados a él. Conservar los nombres de cola internos en ese broker. | DSN sin valores secretos en el informe; IDs de contenedores, routing de toda la cadena, cero mensajes/tareas cruzados con el broker ordinario. |
| Proceso | Worker de ensayo con un solo proceso prefork; identificar tarea, PID propietario, supervisor y PGID Terraform/provider. | Relación PID/tarea/UUID obtenida antes del fallo y prueba de que cada PID pertenece al contenedor de ensayo. Nunca seleccionar procesos globalmente por nombre. |
| Instancia | Una única intención sobre una VM KVM descartable del clúster MNG, nodo autorizado `mng01`; conservar UUID, VMID, job, operación, intento y state key. | Política vigente, disponibilidad de cuota, ausencia de otros ensayos inciertos, VMID >=30000 y exclusión de VM 104, VM 106 y VM 29000. |
| Credenciales | Identidad de ensayo y credenciales Proxmox/Wasabi cuyo ámbito sobre recurso/prefix esté demostrado. No sustituir las credenciales de los servicios compartidos. | Revisión de permisos sin valores de tokens; exclusión de las VMs protegidas y de states históricos ajenos. |
| Persistencia | Operaciones e historial durables y observables aun después de retirar los procesos de ensayo; planes y state preservados. | Acceso de recuperación controlada al mismo UUID/key, sin recreación, importación, reparación SQL ni borrado de locks. |

No se añaden estos servicios al Compose operativo en este PR. El diseño requiere una preparación independiente de ensayo, con límites de recursos, red y rutas verificados; no se trata de una propuesta de reestructurar el Control Plane.

## Secuencia al disponer del aislamiento

1. Repetir pruebas simuladas/de integración; registrar baseline y ausencia de escritores ajenos. Reservar como máximo una VM para esta secuencia; nunca superar tres VMs de ensayo ni dos aprovisionamientos concurrentes.
2. Detener solo el consumidor de ensayo antes de entrega; aceptar una intención y demostrar que el mensaje y registro persisten. Arrancarlo y observar su procesamiento.
3. En otro caso, identificar el proceso propietario durante una ejecución de esa misma instancia. Interrumpir únicamente ese PID, con supervisor/grupo previamente identificados. Observar tareas Proxmox ya enviadas: terminar Terraform local no las cancela.
4. Confirmar una sola VM y un solo Apply consumido. La redelivery ordinaria debe conservar el fence; cualquier resultado no demostrado permanece `uncertain` y bloquea mutaciones incompatibles.
5. Reiniciar únicamente el worker de ensayo y solicitar recuperación readonly del mismo binding cuando corresponda. Registrar separadamente la terminación automática de hijos y el cierre manual/controlado de la operación.
6. Ante cualquier incertidumbre, detener los demás ensayos hasta resolverla. Conservar DB, cola, locks y metadatos de versiones de Wasabi; no forzar desbloqueos.
7. Cuando haya postcondición verificable, shutdown y DELETE normales por IACP; confirmar ausencia de la VM en todo el clúster MNG, state vacío versionado, operaciones terminales y ausencia de locks/huérfanos. Retirar solo los recursos efímeros propios.

Cada ejecución debe conservar hipótesis, estado inicial, inyección exacta, resultados DB/Proxmox/Wasabi/colas/locks, reintentos, duración del fallo, duración de recuperación, versión y referencias de evidencia. La intervención humana debe declararse. Mientras no exista esta evidencia, los escenarios críticos físicos conservan BLOCKED y la certificación integral conserva NO-GO.
