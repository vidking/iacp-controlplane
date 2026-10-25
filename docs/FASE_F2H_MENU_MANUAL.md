# Fase F.2H: menú normalizado y manual de uso del MVP KVM

Fecha: 24 de octubre de 2026, America/Costa_Rica.

## Alcance del cambio

La candidata parte de `origin/main` en `ec0b650`, que ya contiene PR #61 y el
correctivo de historial integrado por PR #62. Este cambio sólo modifica la
presentación documental de Swagger, su navegación y las pruebas de
documentación. No modifica rutas HTTP, operationId, schemas, autenticación,
workers, Terraform, PostgreSQL, Redis, Compose ni infraestructura.

El menú dejó de repetir tres enlaces sobre el grupo `instances`. Ahora navega a
los tags existentes de OpenAPI mediante hashes `#/tag/<tag>`:

`instances`, `operations`, `kvm-lifecycle`, `disposable-deletion`,
`observability`, `jobs` y `lab-administration`.

Las operaciones individuales conservan sus deep links originales. Las guías
locales mantienen sus hashes y Forgejo queda como referencia secundaria.

## Manual local

La guía CRM se presenta como «Cómo utilizar el MVP KVM» e incluye:

- alcance funcional y límites del MVP;
- diferencia entre documentación Basic y API Bearer;
- consulta de perfiles y políticas vigentes;
- formato y reuso seguro de `Idempotency-Key`;
- campos reales de `InstanceCreate` y tres ejemplos compatibles;
- HTTP 202, `instance_id`, `operation_id`, `job_id` y `X-Request-ID`;
- estados `queued`, `running`, `completed`, `failed` y `uncertain`;
- DHCP, Guest Agent, Cloud-Init, SSH y verificación de acceso;
- start, shutdown, reboot y seguimiento de Lifecycle;
- DELETE autorizado, plan revisado y límites de recursos protegidos;
- errores 400/422, 401, 403, 409, 429 y 503, con tratamiento recomendado.

También se distingue explícitamente el aprovisionamiento de una KVM del
despliegue o rollback del IACP Control Plane (VM 104).

## Fotografía de laboratorio

Se consultaron `GET /v1/profiles` y `GET /v1/lab/policies` con la identidad
autorizada del laboratorio el 24/10/2026 a las 19:46 CR. Se documentaron los
perfiles `lab-dhcp` y `lab-root-dhcp`, ambos `ubuntu-2404`, DHCP sobre `vmbr0`,
destino `mng01`, límites de 4 vCPU/4096 MB/40 GB por KVM y cuotas de 4
instancias, 8 vCPU, 8192 MB, 80 GB y 2 operaciones concurrentes.

Las políticas aprobadas y vigentes en esa lectura fueron:

- `ac558dfa-4c2b-4e2f-8886-67648e880dc8` para `lab-dhcp`, hasta 30/10/2026
  19:23 CR.
- `67e81062-661d-4edb-9ed2-051bfe73b974` para `lab-root-dhcp`, hasta 30/10/2026
  22:16 CR.

Los valores son dinámicos. La guía exige volver a consultar ambos endpoints
antes de crear; no constituyen una autorización permanente.

## Pruebas de candidata

- Suite completa: 845 aprobadas, 49 omitidas por no disponer de PostgreSQL
  descartable, 12 advertencias; sin fallos.
- Suite documental: 8 aprobadas.
- OpenAPI 3.1.0, 24 paths y proyección contractual `/v1` idéntica.
- Playwright de escritorio: siete tags, tres guías, recarga de tag e historial
  Atrás/Adelante: PASS.
- Playwright móvil: menú se cierra tras seleccionar, categoría y guía visibles,
  sin errores JavaScript: PASS.
- Sin dependencias CDN ni solicitudes externas en la candidata.

Evidencias privadas:

- `~/.local/state/iacp-fase-f2/playwright-candidate-f2h.json`
- `~/.local/state/iacp-fase-f2/playwright-candidate-f2h-mobile.json`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_DESKTOP.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_MOBILE.png`

## Promoción

La candidata aún no está integrada en `main` ni desplegada en el IACP Control
Plane (VM 104). Después del merge debe repetirse `iacpctl env-promote`,
`go_live.py` y toda la validación Playwright sobre HTTPS real. No se declara
ningún SHA runtime nuevo en esta fase de preparación.
