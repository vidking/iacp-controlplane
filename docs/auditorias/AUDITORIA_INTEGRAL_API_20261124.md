# Auditoría integral de la API y el runtime (2026-11-24)

**Alcance:** rama `devops/features` fusionada con `main` (la API es única; la
única diferencia entre ramas era el commit `25f2f9e`). Revisión estática
(bandit, ruff, pyright), revisión manual de autenticación, secretos,
contenedores y despliegue, y pruebas locales contra PostgreSQL 16 desechable.

**Dictamen:** pruebas locales PASS (607). No equivale a certificación: no se
ejecutó la matriz física en el IACP Control Plane (VM 104) ni en el clúster
MNG. Ese dictamen queda **PENDIENTE** hasta desplegar la imagen en
`dev-iac.cyberfuel.com` y correr la matriz de seis aprovisionamientos.

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
