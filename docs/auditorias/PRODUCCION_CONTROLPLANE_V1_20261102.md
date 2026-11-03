# Promoción del IACP Control Plane a producción

- **Fecha:** 2026-11-02, hora de Costa Rica
- **Repositorio:** `iacp-controlplane`
- **Commit desplegado:** `365e3a2ec0187810bfcb0377c544152ed0bdc8da`
- **Etiqueta:** `controlplane-v1-20261102-r2`
- **Imagen:** `scm.cyberfuel.com/cyberfuel/iacp-controlplane:365e3a2ec0187810bfcb0377c544152ed0bdc8da`
- **API pública:** `1.0.0`, contrato `/v1`
- **Runtime:** IACP Control Plane (VM 104), API y workers nuevos con PostgreSQL propio
- **Destino habilitado:** clúster MNG, nodo `mng01`; VMID `30000–39999`
- **State:** Wasabi `iacp-cyberfuel/iacp/prod-runtime-v1`

## Dictamen

**PASS — piloto de integración productivo en MNG.** La API nueva está activa en
`https://iac.cyberfuel.com`; pasaron el despliegue, readiness, contrato público,
autenticación, aprovisionamiento físico, ciclo de energía, reconciliación,
eliminación de canarias y preservación del state.

Este resultado no es un GO integral. Platinum/PRX permanece deshabilitado y
`dev-cyberfuel.com` no se activó porque aún no resuelve DNS desde el IACP
Control Plane (VM 104) y el certificado instalado no contiene ese nombre.

## Release y rollback

- El commit `365e3a2` está integrado en `origin/main` y en
  `migration/controlplane-baseline`; la etiqueta anotada
  `controlplane-v1-20261102-r2` conserva la revisión desplegada.
- El runtime candidato ejecuta los cuatro servicios esperados. API y PostgreSQL
  están `healthy`; los dos workers están `idle`.
- El runtime legacy sigue `healthy` en el IACP Control Plane (VM 104), con la
  imagen `iacp/controlplane-api:a4c1107949249fa5ec48e0142b2e260021e4e095`.
  Permanece disponible para restaurar el upstream si hiciera falta.
- La imagen se transfirió como archivo porque el registry no permitió descarga
  anónima. El archivo persiste en
  `/opt/iacp/releases/controlplane-365e3a2/image.tar`, SHA-256
  `d9d001d5acd19347add5c352b5728e381d05601c7f4c32f265a247cd59baac08`.
  El release también conserva `source.tar.gz` y el registro de digest local.
- Antes del cambio se respaldaron el manifiesto de producción, `prod.env` y la
  certificación en el directorio privado
  `/opt/iacp/evidence/controlplane-365e3a2-change-control`, con permisos
  `0700/0600`. No se copiaron secretos al repositorio.

La revisión `365e3a2` solo cambia documentación estática dentro de la imagen.
`api/main.py`, los demás módulos de API, `worker`, `terraform`, el `Dockerfile`
y las dependencias no difieren respecto a `2dc2629`. Las pruebas físicas de ese
release se reutilizaron para el código sin cambios; después de rotar el token
MNG se ejecutó además un ciclo físico completo sobre `365e3a2`.

## Verificaciones

| Control | Resultado | Evidencia |
| --- | --- | --- |
| Suite completa | **PASS**, 83 pruebas; 3 avisos deprecados de dependencias | Ejecutada con el commit `365e3a2` y PostgreSQL temporal local |
| Configuración y preflight productivo | **PASS**; Wasabi autorizó lectura del prefijo esperado | `/opt/iacp/evidence/controlplane-365e3a2-config-check/`, `...-preflight-2/` |
| Rotación y recarga de token en producción | **PASS**; preflight, deploy precargado y verify posteriores a la edición de `prod.env` | `/opt/iacp/evidence/prod-token-preflight/`, `...-deploy/`, `...-verify/` |
| Preflight de configuración develop | **PASS**; la ruta y el stack siguen inactivos | `/opt/iacp/evidence/prod-token-dev-preflight/` |
| Despliegue con imágenes precargadas | **PASS**; SHA del archivo comprobado antes de `docker load` | `/opt/iacp/evidence/controlplane-365e3a2-deploy/` |
| Verificación del stack | **PASS**; API 1.0.0, `/v1/instances`, PostgreSQL y API saludables | `/opt/iacp/evidence/controlplane-365e3a2-verify/` |
| Activación | **PASS**; upstream ya apuntaba al alias nuevo y la revisión externa coincidió | `/opt/iacp/evidence/controlplane-365e3a2-activation/` |
| Smoke público | **PASS**; readiness 200, OpenAPI y `/docs` 200, `/v1/instances` sin token 401 | `/opt/iacp/evidence/controlplane-365e3a2-public-smoke.json` |
| Token MNG rotado | **PASS**; ciclo público, lectura de config/tags Proxmox y VM física ausente tras destroy | `/opt/iacp/evidence/prod-token-rotation-canary/result.corrected.json` |
| Producción en reposo | **PASS**; 10 operaciones `succeeded`, 0 activas, 2 workers `idle`, 3 registros de canaria `deleted` | PostgreSQL del proyecto `iacp-controlplane-prod-v1` |

La API pública anuncia `X-IACP-Build-Revision: 365e3a2ec0187810bfcb0377c544152ed0bdc8da`.
Swagger/OpenAPI mantiene versión `1.0.0` y las rutas `/v1`; no se publicó un
contrato `/v2`.

## Pruebas físicas y reconciliación

Los ensayos se ejecutaron secuencialmente en el clúster MNG, nodo `mng01`, con
VMID `30000`, reservado dentro del rango de producción y reutilizado solo
después de retirar la canaria anterior:

1. **Canaria interna:** `canary-prod-d3e57f6`; VM iniciada con Ubuntu 24.04 y
   tags exactos `cl-test;kvm-canary;migracion-20261102`. Se observó el lock S3
   durante Terraform. Tras apagar la VM y confirmar el estado físico `stopped`,
   se inyectó `uncertain` únicamente en la operación de esa canaria y en la base
   aislada del runtime nuevo. La API reconcilió el estado observado sin repetir
   la mutación. Luego se eliminó la VM mediante la API.
2. **Canaria pública:** `canary-public-2dc2629`; se creó desde
   `iac.cyberfuel.com`, se apagó y se eliminó por API. El inventario MNG confirmó
   la VM ausente.
3. **Canaria tras la rotación del token MNG:** `canary-token-365e3a2-627a00a7`,
   plan `kvm-1`, Ubuntu 24.04, VMID `30000`, tags físicos exactos
   `cl-test;kvm-token-rotated;run-627a00a7`. Se aprovisionó y encendió desde la
   API pública; el worker leyó su configuración Proxmox con el token nuevo.
   Luego la API confirmó `running`, ejecutó apagado ordenado, confirmó
   `stopped` y completó el destroy. La consulta directa al inventario Proxmox
   confirmó que VMID `30000` ya no estaba presente. El objeto state quedó y no
   quedó lock S3.

Evidencia física detallada:

- Aprovisionamiento: `/opt/iacp/evidence/controlplane-d3e57f6-prod-physical-provision/result.json`
- Reconciliación: `/opt/iacp/evidence/controlplane-d3e57f6-prod-physical-reconcile.json`
- Eliminación: `/opt/iacp/evidence/controlplane-d3e57f6-prod-physical-delete.json`
- Ciclo público: `/opt/iacp/evidence/controlplane-2dc2629-prod-public-cycle/result.json`
- Prueba del token rotado: `/opt/iacp/evidence/prod-token-rotation-canary/result.corrected.json`

Los objetos Terraform state se conservaron después de eliminar las canarias; los
locks se liberaron. No se purgó el bucket, no se borraron objetos versionados ni
se forzó ningún unlock. La base nueva conserva los registros de auditoría de
las tres canarias como `deleted`; no importó instancias del runtime anterior.

El evaluador inicial de la prueba del token guardó `FAIL` porque comparó todos
los checks como booleanos; `preflight_api_and_queue` se representa como la
cadena `PASS`. Se preservó el archivo original como
`result.initial-evaluator.json` y se añadió `result.corrected.json`, donde la
regla distingue correctamente ese check textual de los nueve booleanos. Los
diez checks individuales pasaron.

## Rangos, entornos y protecciones

| Entorno | Host | Destino | VMID | Estado |
| --- | --- | --- | --- | --- |
| Producción | `iac.cyberfuel.com` | MNG, `mng01`; PRX deshabilitado | `30000–39999` | Activo |
| Desarrollo | `dev-cyberfuel.com` | MNG | `40000–49999` | Sin ruta pública |

Los manifiestos privados de desarrollo se actualizaron a `dev-cyberfuel.com` y
al rango `40000–49999`. El preflight de desarrollo pasó configuración tras la
rotación; su token no se probó aprovisionando porque ese runtime sigue inactivo.
`getent` en VM 104 no devolvió dirección para ese host;
el certificado solo contiene `*.cyberfuel.com` y `cyberfuel.com`, no el SAN
exacto `dev-cyberfuel.com`. La plantilla de ruta está revisada, pero no se copió
al directorio dinámico ni se activó el stack de desarrollo.

La API mantiene protegidos el IACP Control Plane (VM 104), VM 106 y las Golden
Images `29000`, `29001`, `29010` y `29011`. En el inventario MNG consultado
durante este trabajo, VMID 106 apareció en `mng02`; no se operó ni modificó.

## Evidencia de repositorio

El plan de migración histórico permanece intacto. Las correcciones actuales de
hostname, rangos, contrato visual y operación están en `origin/main`; esta
auditoría añade el resultado del despliegue sin reescribir registros previos.
