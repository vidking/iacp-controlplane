# Convenciones de idioma e infraestructura

Vigentes desde la Fase D, 24/10/2026. [Instrucciones del repositorio](../AGENTS.md).

| Denominación | Significado y alcance |
|---|---|
| MNG | Clúster Proxmox de laboratorio formado por `mng01` y `mng02` |
| IACP Control Plane (VM 104) | Plataforma de orquestación de `iacp-controlplane-01`, actualmente alojada en `mng01` |
| VM 106 | Instancia histórica de laboratorio protegida |
| VM 29000 | Golden Image Ubuntu 24.04 protegida |
| Platinum/PRX | Clúster Proxmox de producción, independiente de MNG; excluido de los ensayos |

Los despliegues, actualizaciones, reinicios y supervisión de contenedores del orquestador corresponden al **IACP Control Plane (VM 104)**. La creación, clonación, eliminación y administración de VMs de prueba corresponden al **clúster MNG**, con el nodo indicado cuando se conozca. MNG nunca sustituye el nombre de la VM 104 ni del Control Plane.

La protección de la VM 104 contra aprovisionamiento, Lifecycle y eliminación desde la API permite actualizar de forma controlada el software dentro del propio Control Plane. Las VM 106 y VM 29000 permanecen protegidas.

Los nuevos commits, PR, documentos técnicos, auditorías, explicaciones y reportes se redactan en español. Se conservan identificadores técnicos, endpoints, métodos, parámetros, archivos y salidas originales de herramientas.

## Interpretación de la evidencia histórica

Estas denominaciones no cambian los resultados históricos del PR #46. En sus archivos `HOST_*`, las 8 CPU y aproximadamente 15,8 GiB corresponden a la **VM 104**, no a la capacidad física de `mng01` ni del clúster completo. `RUNTIME_*` describe los servicios del **IACP Control Plane (VM 104)** y, por separado, las lecturas del inventario del **clúster MNG** y metadatos remotos de Wasabi. `QUERY_PROFILE_*` describe observadores de consultas en un proceso separado del contenedor API; no acredita un despliegue.

La [versión explicativa en español de los JSON de Fase C](validation/phase-d/INTERPRETACION_FASE_C_ES.md) conserva referencias a los originales. Los estados técnicos PASS, FAIL, BLOCKED, `uncertain`, `queued` y `running` se mantienen. No se reescribe el historial Git ni se sustituyen evidencias originales para traducirlas.
