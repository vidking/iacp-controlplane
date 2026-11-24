# Auditoría integral de la API y el runtime (2026-11-24)

**Alcance:** rama `devops/features` fusionada con `main` (la API es única; la
única diferencia entre ramas era el commit `25f2f9e`). Revisión estática
(bandit, ruff, pyright), revisión manual de autenticación, secretos,
contenedores y despliegue, y pruebas locales contra PostgreSQL 16 desechable.

**Dictamen histórico:** pruebas locales PASS (607). No equivale a certificación: no se
ejecutó la matriz física en el IACP Control Plane (VM 104) ni en el clúster
MNG. Ese dictamen queda **PENDIENTE** hasta desplegar la imagen en
`dev-iac.cyberfuel.com` y correr la matriz de seis aprovisionamientos.

## Reauditoría y matrices — 24 de noviembre de 2026

Esta actualización revisa la rama remota integrada en `8371323` más los
cambios de corrección que se incluyen con esta actualización.

### Verificación local

- La suite Docker aislada 6+6 terminó en **624 PASS, 0 FAIL**, con una
  advertencia deprecada de Starlette/httpx. Evidencia local:
  `/tmp/iacp-validation-8371323-release-candidate`.
- Ruff, reglas `E4,E7,E9,F`, pasó en los cinco archivos modificados.
- Bandit no reportó hallazgos de severidad alta. El escaneo completo aún muestra
  18 alertas medias y 13 bajas; incluye SQL generado con marcadores y listas
  controladas, además de hallazgos de subprocess y valores de ejemplo. Esas
  alertas requieren mantener la revisión manual documentada; no se declara un
  PASS estático global.
- Se cerraron en el código dos alertas revisadas: el firmador TLS ahora envía su
  bearer únicamente al servicio interno fijo `api:8080`; el valor efímero que
  Terraform necesita durante `destroy` ya no es una contraseña fija en el
  código. La observación de VMID ahora diferencia `present`, `absent` y
  `unknown`.
- Pyright, ejecutado sobre los módulos principales, aún devuelve diagnósticos de
  tipos de filas PostgreSQL, valores opcionales y flujo no acotado. No se declara
  PASS de tipado estático.

### Desarrollo físico

- El runtime desplegado durante el ensayo siguió en `e5aabca`; tenía seis
  workers de infraestructura y seis de servicios sanos.
- La matriz KVM de 48 casos lanzó un lote de seis y registró **12 comprobaciones
  A01/A02 PASS**. Se detuvo antes de Terraform apply. El resultado es
  **BLOCKED**, no PASS: las seis operaciones quedaron `uncertain`, con VMID
  40855–40860 e IPAM/leases retenidos. El token de Proxmox devuelve HTTP 403 al
  consultar esos VMID, y el inventario no los muestra; eso no demuestra que
  estén físicamente ausentes. Evidencia:
  `/opt/iacp/evidence/matrix-dev-kvm48-e5a-20261124`.
- El preflight solo lectura de la matriz de servicios de 24 pasó NetBox,
  Ubuntu 24.04, `prod_static`, bridge, capacidad y plantilla; terminó en
  `RUNTIME_NOT_IDLE` por las seis operaciones inciertas. No creó recursos.
  Evidencia:
  `/opt/iacp/evidence/matrix-preflight-dev-services-e5a-20261124-rerun`.
- El token requiere visibilidad `VM.Audit` suficiente para confirmar presencia o
  404 sobre los VMID del pool. Hasta poder distinguir 403 de 404, no se deben
  liberar IPs/leases ni lanzar otro lote. La reconciliación debe continuar por
  el procedimiento oficial después de confirmar cada recurso en MNG.

### Producción y dictamen

- No se promovió ni modificó Producción: permanece en `5cd54b1`, con seis
  workers de infraestructura y cuatro de servicios.
- No se ejecutaron las matrices físicas de Producción ni las matrices de
  servicios de Desarrollo/Producción; el preflight de Desarrollo no está idle.
- La certificación integral permanece **BLOCKED**. El candidato nuevo no debe
  desplegarse mientras el gate oficial detecte operaciones inciertas. Primero
  hay que restablecer lectura de VMID y reconciliar las seis reservas de forma
  verificada; después se repiten las matrices completas en Desarrollo, se
  promueve el mismo digest y se ejecutan las matrices de Producción.

## Hallazgos y resolución

| # | Hallazgo | Resolución |
|---|----------|------------|
| 1 | `renew_tls` fallaba con `TypeError` en el manejador si la lectura inicial del servicio fallaba y el job quedaba en `running` | `worker/services.py` lee el estado vigente dentro de la transacción; prueba de regresión |
| 2 | Prueba de NetBox marcada `no_database` aunque `release` usa advisory lock | Se quitó la marca |
| 3 | `compose.yaml` sin healthcheck de workers, sin `stop_grace_period` y sin rotación de logs (el stack de Portainer ya los tenía) | Igualados en `compose.yaml` |
| 4 | Consola y `/docs` sin `X-Frame-Options` ni `Referrer-Policy` | Cabeceras globales en `HTTPGuards` |
| 5 | Variables de entorno mal escritas (`IACP_WORKER_STALE_SECONDS`, `IACP_RETENTION_DAYS`) impedían el arranque | Valores por defecto seguros |
| 6 | Token compartido sin límite de intentos ni longitud mínima | Limitador de fallos por origen (solo frena tokens inválidos; el token válido nunca se bloquea) y mínimo de 32 caracteres al arrancar |
| 7 | `chown -R iacp:iacp /app`: el usuario de runtime podía modificar código y proveedores | `/app` y la caché de proveedores quedan de root, solo lectura; solo el volumen de trabajo es escribible. Verificado: `terraform init` y `validate` pasan con el sistema de archivos de solo lectura |
| 8 | Sin límites de memoria ni procesos | `mem_limit` y `pids_limit` en todos los contenedores, ajustables por variable |
| 9 | PostgreSQL sin restricciones | `read_only`, `no-new-privileges`, `cap_drop: ALL` con las cinco capacidades que exige su entrypoint, límites. Verificado con volumen nuevo y reinicio sobre volumen existente |
| 11 | **El firmador de certificados TLS por IP (`/v1/internal/service-jobs/{id}/tls-certificate`, commit `ccd2f0e`) era inalcanzable**: la guardia exigía el token de la API en todo POST bajo `/v1/` y el worker presenta otra credencial, por lo que respondía siempre 401. Las pruebas llamaban a la función directamente y no atravesaban el middleware | Las rutas `/v1/internal/` se validan con su propia credencial; pruebas que atraviesan el middleware real |
| 12 | El worker enviaba la **clave maestra Fernet** como bearer a la API (HTTP interno). Una fuga del bearer permitía descifrar todos los secretos de servicios; además `compare_digest` con texto no ASCII provocaba `TypeError` (500) | Bearer derivado por HMAC-SHA256 de la clave (`signer_token`), comparación en bytes |
| 10 | Duplicados en conjuntos (`{1, True}`), sumideros de socket HTTPError, clave maestra releída en cada operación | Corregidos |

## Notas operativas antes de promover

- **`IACP_API_TOKEN` debe tener al menos 32 caracteres** o la API no arranca. El
  generador de `scripts/prepare_develop.py` produce 64. Revisar el token de
  `iac.cyberfuel.com` antes de promover; `IACP_API_TOKEN_MIN_LENGTH` permite
  ajustar el mínimo, pero no se recomienda bajarlo.
- El limitador es por proceso (`--workers 3`), así que el umbral efectivo es
  `IACP_AUTH_MAX_FAILURES` (10 por 60 s) por proceso. Detrás de Traefik el
  origen visible es el proxy; por diseño eso no bloquea a operadores válidos.
- Techos por defecto: workers 1536 MiB y 512 procesos, API 1 GiB y 256, PostgreSQL
  2 GiB y 512 (`IACP_WORKER_MEM_LIMIT`, `IACP_API_MEM_LIMIT`,
  `IACP_POSTGRES_MEM_LIMIT` y equivalentes `*_PIDS_LIMIT`). Verificar con la
  matriz de seis aprovisionamientos simultáneos y subirlos si hay `OOMKilled`.
- Reversión: la imagen anterior sigue siendo la referencia inmutable; los cambios
  de Compose se revierten con el commit previo sin tocar volúmenes.

## Coordinación con el trabajo en paralelo (Codex)

- Ambas partes trabajan sobre `devops/features`. Convención: `git fetch` antes de
  cada push, avance rápido (sin reescribir historial publicado) y commits pequeños
  por tema. No usar `git stash` en este árbol de trabajo compartido.
- Las pruebas de endpoints internos deben atravesar el middleware (`TestClient`),
  no invocar la función del endpoint: así se detectó el hallazgo 11.
- La imagen desplegada hoy en dev (`e5aabca`) **no incluye** los hallazgos 6 a 12.
  Hay que construir una imagen nueva y repetir el despliegue en
  `dev-iac.cyberfuel.com` antes de la matriz. Al desplegar, `IACP_API_TOKEN` debe
  tener 32 o más caracteres.
