# Cierre de Fase F — MVP KVM

Fecha del informe: 24 de octubre de 2026, horario `America/Costa_Rica` (UTC−06:00).

## Completado

- El PR #51 de normalización profesional de Swagger fue integrado en `origin/main` mediante el merge `1f0708f`; su commit de contenido es `e01c27a219471bf32cfac14d3b89df14d0bb0a34`.
- La batería completa de integración quedó registrada con 892 pruebas aprobadas, sin fallos ni omisiones. OpenAPI 3.1 y la proyección contractual HTTP fueron validados; el contrato `/v1` no cambia.
- El candidato incluye Swagger UI 5.34.0 local, ejemplos sanitizados, navegación CRM, búsqueda, guía de integración y autenticación Basic/Bearer sin alterar sus mecanismos.
- La evidencia candidata de escritorio y móvil está separada de la captura HTTPS histórica de VM 104.

## Verificado en runtime

El IACP Control Plane (VM 104) respondió por HTTPS el 24/10/2026 a las 17:09–17:10 CR:

- readiness: HTTP 200;
- OpenAPI: HTTP 200, OpenAPI 3.1.0, versión 0.10.1, título `IACP Orchestrator`;
- documentación: HTTP 200 con autenticación válida.

La VM 104 continúa ejecutando la release Fase D, SHA efectivo reportado `7c0804d`, con imágenes `iacp/controlplane-api:fase-d-7c0804d` e `iacp/terraform-worker:fase-d-7c0804d`. Los servicios API, workers, Traefik, PostgreSQL y Redis permanecen activos; no se reiniciaron ni sustituyeron contenedores durante este cierre.

La documentación actualmente servida aún referencia Swagger UI desde CDN y el favicon de FastAPI. Por tanto, la interfaz local del PR #51 todavía no está desplegada en VM 104. No se afirma equivalencia entre el candidato y el runtime.

## Pendiente o BLOCKED

- **Promoción Swagger:** pendiente de una ventana y manifiesto de promoción revisados que apunten a una release instalada. No se ejecutó una promoción improvisada ni se modificó Compose, estado, credenciales o el motor IaC.
- **OCI Forgejo:** BLOCKED. El registro está habilitado y exige autenticación (`401`), pero no existe una identidad de publicación autorizada comprobable en las referencias inspeccionadas. No se inventaron credenciales, no se reutilizó el token de IACP y no se publicaron imágenes. En consecuencia no hay tags, digests ni pull de verificación que declarar.
- **Release candidate:** no se creó tag ni release formal. Se conserva como referencia ejecutada la release Fase D y el merge de Swagger como código integrado posterior; no se presenta como versión desplegada.
- **Ensayo general E2E y nueva demo de rendimiento:** no se repitieron. Se conservan las evidencias certificadas de Fase E: 10 RPS durante 30 minutos, 18.000 solicitudes y cero errores.
- **Capturas finales:** no se generó una captura Proxmox nueva; MNG y VM 104 se mantienen separados y no se fabrican evidencias.

## Dictamen

- **GO:** MVP KVM y piloto supervisado de integración CRM, dentro del alcance ya certificado y con las limitaciones documentadas.
- **NO-GO:** resiliencia integral; la certificación física de muerte y recuperación de worker continúa bloqueada por aislamiento insuficiente.

Quedan explícitamente fuera de este cierre la normalización de `/opt/iacp`, Compose, centralización/rotación planificada de credenciales, Garbage Collector de Wasabi, alta disponibilidad del Control Plane, IaCoC, LXD y extensiones comerciales avanzadas.
