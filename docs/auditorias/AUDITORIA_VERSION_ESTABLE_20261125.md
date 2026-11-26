# Auditoría para la versión estable — 25 de noviembre de 2026

## Alcance

Revisión previa al etiquetado de la primera versión estable. Se auditó
`origin/main` en `ca643ae2b01448f1e5c707e29461e560b7b97a27`, cuyo único cambio
respecto de `c6b1a9c3a75abd0b55afc1e718ed172839583df4` es documental.

La revisión manual fue dirigida, no línea por línea: API HTTP y autenticación,
persistencia, admisión de operaciones, cola, recuperación y ejecución del
worker, clientes Proxmox y NetBox (transporte), worker de servicios, secretos,
imagen, Compose y los cambios de runtime posteriores a `8371323`. Quedaron
cubiertos solo por las pruebas automáticas la lógica de reconciliación anterior
a esos cambios, la asignación de NetBox, las recetas de aplicaciones, el portal
web y los programas de matrices.

No se desplegó, reinició ni modificó el IACP Control Plane (VM 104), el clúster
MNG ni Platinum/PRX. La única consulta remota fue `GET /health/ready` y la
lectura del manifiesto de la imagen en el registro.

## Dictamen

| Objeto | Resultado | Alcance |
| --- | --- | --- |
| Integración de `c6b1a9c` | **PASS** | Matrices físicas del 24 de noviembre (48 KVM y 24 de aplicaciones sobre esa revisión) y suite local. Tiene un defecto conocido (hallazgo 1). |
| Candidato con las correcciones de esta auditoría | **PASS local; físico pendiente** | Suite y runtime 6+6 en Docker. Sin matrices en MNG. |
| Certificación integral | **BLOCKED** | Ver «Pendientes». |

## Evidencia

| Control | Resultado |
| --- | --- |
| Revisión servida por `iac.cyberfuel.com` y `dev-iac.cyberfuel.com` | `c6b1a9c3a75abd0b55afc1e718ed172839583df4` en ambos |
| Imagen en el registro para `c6b1a9c` | `sha256:1d76913cead816f02d578b4ee774018d9940dc78fc88933ceac7954b399e9d09` (amd64). No se comprobó que los contenedores en ejecución usen ese digest |
| Suite Docker aislada sobre `ca643ae` | 689 PASS, 0 FAIL |
| Suite Docker aislada sobre el candidato | 711 PASS, 0 FAIL |
| `pip-audit` sobre `uv.lock` | Sin vulnerabilidades conocidas |
| Bandit, severidad alta | Sin hallazgos en `api`, `worker` y `scripts`. Las alertas medias de `api` y `worker` son B608 (SQL con marcadores) y no se revisaron una por una |
| Ruff `E4,E7,E9,F` en `api` y `worker` (candidato) | PASS. En `tests` y `scripts` quedan avisos previos |
| `terraform validate` y `fmt -check` en la imagen candidata | PASS (`mng01` y `prx`), usuario 999 y sistema de archivos de solo lectura |
| Búsqueda de claves y tokens con patrones | Sin coincidencias en el árbol. No sustituye un escáner completo del historial |

Las dos cifras de pruebas incluyen 15 de `tests/test_e2e_matrix_scope.py`,
archivo que sigue sin seguimiento en Git.

No se ejecutaron Pyright ni un escaneo de vulnerabilidades del sistema operativo
de la imagen.

## Adenda del 25 de noviembre: paridad entre canales sobre `c6b1a9c`

Fuente: reporte del equipo de operaciones recibido el 25 de noviembre. Su
evidencia no se revisó en esta auditoría:
`/opt/iacp/evidence/paridad-canales-20261125182044/api-retry/summary.json`,
en el IACP Control Plane (VM 104).

| Canal | Resultado reportado |
| --- | --- |
| `/docs` con Chromium | 12/12 PASS |
| CLI | 12/12 PASS |
| API directa (repetición) | 12/12 PASS |

El reporte indica seis altas en desarrollo y seis en producción, 63,5 segundos
de ejecución simultánea, eliminación confirmada, IP liberadas en NetBox y cero
operaciones, trabajos o locks pendientes.

Antes de la repetición hubo que recuperar la VM 30004 de forma **manual**:
eliminación física y cierre manual de su trabajo de servicio nunca ejecutado,
después de verificar ausencia y reservas liberadas. El reporte no registra la
causa; queda abierto determinarla y comprobar si la API permite recuperar ese
estado sin intervención manual. Estas pruebas no incluyen las correcciones de
esta auditoría.

## Hallazgos corregidos en el candidato

| # | Prioridad | Hallazgo en `c6b1a9c` | Corrección |
| --- | --- | --- | --- |
| 1 | Alta | Los clientes Proxmox y NetBox no capturaban una conexión cortada o truncada durante la respuesta (`RemoteDisconnected`, `ConnectionResetError`, `IncompleteRead`, `BadStatusLine`). Las lecturas no se reintentaban y las mutaciones no quedaban marcadas como inciertas: una orden de energía posiblemente aplicada podía cerrarse como `failed`, y el manejador de fallos podía interrumpirse. Reproducido con un servidor local para Proxmox | Ambos clientes tratan `OSError` y `HTTPException`: reintentan lecturas y declaran inciertas las mutaciones. `worker/proxmox.py`, `worker/netbox.py` |
| 2 | Media | Si fallaba la observación posterior a una orden de energía ya enviada, la operación podía cerrarse como `failed` | Esos errores quedan `uncertain`. Los rechazos deterministas (identidad, permisos) conservan su resultado. `worker/main.py`, `worker/proxmox.py` |
| 3 | Baja | El worker de servicios convertía excepciones imprevistas en `uncertain` sin registrar nada | Registra el tipo de excepción y el trabajo, sin el mensaje. `worker/services.py` |
| 4 | Optimización | `/v1/observability` recorría `operations` completa para las 100 más recientes | Índice aditivo `idx_operations_status_finished`. Datos sintéticos en PostgreSQL 17.6: consulta de 48 ms a 0,12 ms y respuesta de 69 ms a 31 ms con 50 000 operaciones (22 ms a 12 ms con 5 000). El esquema sigue en versión 3 |

## Pendientes que mantienen la certificación integral en BLOCKED

1. Falla intermitente de red en el primer arranque de AlmaLinux 10 `kvm-1`/`kvm-4`,
   sin causa determinada y sin reintento automático.
2. `scripts/wan_matrix.py` (`required_cycles`) pide cuatro ciclos para `kvm` y
   `services`. Sobre `c6b1a9c` consta un ciclo (el ciclo 1 se ejecutó sobre
   `126ab4a`, con cambios de runtime posteriores). El equipo de operaciones debe
   confirmar si esa regla aplica a las matrices de 48 y 24.
3. No existe `/etc/iacp/certification/controlplane-v1.json`, exigido por
   `iacpctl activate`.
4. El repositorio no contiene evidencia del ensayo de reinicio de PostgreSQL y
   de un worker durante un `apply`, ni de respaldos programados.
5. Hermes Agent y OpenClaw están en el catálogo con validación solo local.
6. El digest de imagen es opcional en Compose y `iacpctl`; para la versión
   estable conviene declararlo en el manifiesto.
7. No hay integración continua en el repositorio.

## Etiquetado y reversión

La etiqueta estable debe apuntar a una revisión con imagen publicada y matrices
registradas. Hoy esa revisión es `c6b1a9c`, no la punta documental `ca643ae`,
porque la imagen se identifica con el SHA del commit.

Para volver a ella: desplegar con `IACP_IMAGE_TAG` igual al SHA completo de la
etiqueta y `IACP_IMAGE_DIGEST` igual al digest registrado, mediante
`scripts/iacpctl` (respaldo, `backup-verify`, `deploy`, `verify`). No se borran
volúmenes ni states. El índice añadido por el candidato es compatible con la
revisión anterior.

El candidato debe etiquetarse como versión siguiente solo después de desplegar
una imagen inmutable en `dev-iac.cyberfuel.com`, completar las matrices con seis
aprovisionamientos simultáneos y promover ese mismo digest.
