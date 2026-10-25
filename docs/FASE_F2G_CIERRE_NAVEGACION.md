# Cierre Fase F.2G: posicionamiento global de Swagger

Fecha del informe: 24 de octubre de 2026, America/Costa_Rica.

## Alcance

Se verificó la promoción del correctivo global de navegación de Swagger integrado
por el PR #61. El cambio conserva el contrato `/v1`, la autenticación, el layout,
los servicios y la infraestructura del IACP Control Plane (VM 104).

El cambio integrado en `main` es `447899ee6240286fe1ac54d398f83ffda33ce3fa`,
mediante el merge `221484744653e5283ab7be7e131e168509438618`. El SHA ejecutado
antes de la promoción era `39cc232150c20407e04518823811738ec8027c57`.

## Promoción verificada

La promoción se realizó el 24 de octubre de 2026 entre las 19:30 y 19:33
(America/Costa_Rica), utilizando `iacpctl env-promote` y `go_live.py` con
`inventory`, `preflight`, `promote` y `verify` aprobados.

La evidencia privada queda en:

`/opt/iacp/evidence/fase-f2g-20261024T2000CR/`

Se conservó el respaldo de configuración registrado por `env-promote` y las
imágenes anteriores. Las imágenes ejecutadas después de la promoción fueron:

| Servicio | Referencia | Digest SHA256 |
| --- | --- | --- |
| API, worker y lifecycle worker | `iacp/controlplane-api:fase-f2g-447899e` | `dfc2013778e847cdb1553cc92ff54e371f137fd87d88c4a6a2fa296c1595a263` |
| Terraform worker | `iacp/terraform-worker:fase-f2g-447899e` | `a0ede1425d5c65b457a6419c8902fda65898b97df00d220e9db46295b1cafd3a` |

## HTTPS y Playwright

La ejecución se realizó contra `https://iac.cyberfuel.com/docs` con autenticación
de documentación, sin mutaciones de infraestructura. Las seis categorías del
recorrido CRM fueron medidas mediante `getBoundingClientRect()` después de
finalizar el desplazamiento:

- `listAuthorizedProfiles`, `createInstance`, `listInstances`,
  `listOperations`, `getInstancePower` y `deleteDisposableInstance`: PASS.
- Los encabezados de grupo quedaron aproximadamente entre 107 y 108 px; la
  cabecera fija termina en 88 px y se conserva el margen visual.
- Las operaciones seleccionadas permanecieron visibles en el viewport.
- Las tres guías locales abrieron su `<details>`, mostraron Markdown real y
  posicionaron la tarjeta raíz aproximadamente en 108 px.
- Recargas por hash, índices internos, deep link de operaciones, menú móvil,
  cierre del menú y recursos locales: PASS.
- No se observaron errores JavaScript ni solicitudes a orígenes externos/CDN.

Capturas sanitizadas y resultado Playwright:

- `~/.local/state/iacp-fase-f2/SWAGGER_VM104_F2G_DESKTOP_FINAL.png`
- `~/.local/state/iacp-fase-f2/SWAGGER_VM104_F2G_MOBILE_FINAL.png`
- `~/.local/state/iacp-fase-f2/playwright-f2g-final.json`

## Regresión de runtime

- `/openapi.json`: OpenAPI `3.1.0`, versión `0.10.1`, 24 paths.
- Proyección contractual `/v1` idéntica al snapshot anterior.
- `/v1/profiles` con Bearer autorizado: HTTP 200.
- Readiness HTTPS: HTTP 200.
- API, workers, PostgreSQL, Redis y Traefik: contenedores operativos; API,
  PostgreSQL y Redis saludables.
- Esquema efectivo: `0011_crm_contract`.
- Snapshot posterior: `unacked=0`, colas vacías y ninguna operación, tarea,
  ejecución de energía o apply Terraform no terminal.
- Los hashes y conteos de tablas permanecieron iguales entre preflight y verify;
  no se mutaron VM 104, VM 106 ni VM 29000.

## Limitación pendiente

El runtime actualmente desplegado conserva una interacción conocida de Swagger
UI: al cambiar entre dos guías con `location.hash`, se generan dos entradas de
historial. Por eso una sola acción Atrás puede permanecer en la misma URL; una
segunda acción vuelve a la guía anterior. El posicionamiento visual no se ve
afectado, pero el criterio de historial de un solo paso no debe marcarse PASS.

Se preparó el correctivo mínimo `bb3814b` en la rama
`fix/fase-f2-historial-global`, sustituyendo ese cambio por `history.pushState`.
La rama no está integrada en `main` ni promocionada al IACP Control Plane
(VM 104); no se declara como parte de este runtime.

## Dictamen

- **PASS:** posicionamiento global de categorías, operaciones y guías locales en
  el runtime `447899e`; contrato y servicios sin regresiones observadas.
- **PENDIENTE:** integración y certificación del correctivo de historial
  `bb3814b` para cerrar el criterio completo Atrás/Adelante.
- **OCI:** permanece BLOCKED de forma independiente por falta de autenticación
  autorizada del registro Forgejo.
- No se modificaron Compose, credenciales, Terraform, imágenes OCI ni la
  infraestructura protegida.
