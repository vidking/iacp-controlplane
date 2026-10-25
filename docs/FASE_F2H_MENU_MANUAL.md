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

## Correctivo visual de bloques de código

Fecha: 24 de octubre de 2026, 20:24, America/Costa_Rica.

La inspección con `getComputedStyle()` confirmó la causa del defecto: la regla
general `.guide-content code` estaba heredando al `<code>` dentro de los bloques
multilínea el fondo claro, padding y tamaño destinados al código en línea. El
`<pre>` conservaba el fondo oscuro, por lo que cada línea quedaba cubierta por
franjas claras.

El correctivo se limitó a `app/iacp/static/docs/ux-f1.css` y separa ambos
casos: `.guide-content pre code` usa fondo transparente sobre el bloque oscuro,
tipografía monoespaciada heredada, saltos de línea y desplazamiento horizontal;
`.guide-content :not(pre)>code` conserva el estilo claro y compacto del código
en línea. No se modificó layout, API, autenticación ni Swagger UI.

La validación Playwright de candidata, sin extensiones, registró para
Chromium:

- Tema claro de escritorio (1536×1000), tema oscuro nativo emulado y móvil
  (390×844): PASS.
- `pre`: `rgb(16, 36, 58)` con texto `rgb(231, 242, 245)`, contraste 13,79:1.
- `pre code`: fondo transparente, display `block`, `white-space: pre`, fuente
  monoespaciada y texto seleccionable completo.
- Código en línea: contraste 12,81:1, sin regresión visual.
- Sin errores JavaScript, respuestas HTTP fallidas ni solicitudes externas.
- Firefox 157 administrado por Playwright: PASS en escritorio, con los mismos
  estilos calculados, texto seleccionable y sin errores ni respuestas fallidas.

La evidencia detallada y las capturas están en:

- `~/.local/state/iacp-fase-f2/playwright-f2h-code-block.json`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_CODE_LIGHT.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_CODE_DARK.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_CODE_MOBILE.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_CODE_JSON.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_CANDIDATE_F2H_CODE_FIREFOX.png`

Los tres ejemplos de creación y el cuerpo JSON del comando curl se validaron
contra el esquema `InstanceCreate` de OpenAPI 3.1: 3/3 PASS. Los campos
obligatorios son `name` e `image`; no aparece el campo inexistente `profile_id`.
Los `self_service_policy_id` documentados corresponden a la fotografía de
laboratorio indicada arriba y se presentan como valores dinámicos, no como
autorizaciones permanentes.

La suite ejecutada en el stack efímero de pruebas el 24/10/2026 obtuvo 895
aprobadas y 28 advertencias. Las 49 pruebas omitidas de la ejecución candidata
anterior por dependencia de PostgreSQL descartable permanecen registradas como
no ejecutadas en la evidencia histórica; no se presentan como aprobadas por
esta corrección visual.

## Promoción

El manual base `9c68d29` ya está integrado en `main` mediante PR #63, cuyo SHA
actual es `e1a36b3`. El correctivo visual de este informe (`acca80c`) todavía
está pendiente de un PR de seguimiento; por ello no se promocionó ni se declara
un SHA runtime nuevo en el IACP Control Plane (VM 104). El runtime conocido
continúa en `447899ee6240286fe1ac54d398f83ffda33ce3fa`.

Comparación para el PR de seguimiento:
`https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/compare/main...feat/fase-f2-manual-menu`

Después del merge del correctivo debe repetirse `iacpctl env-promote`,
`go_live.py` y toda la validación Playwright sobre HTTPS real, conservando el
respaldo y el rollback vigentes.
