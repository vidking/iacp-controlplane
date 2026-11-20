# Revisión integral del código — 2026-11-19

## Alcance

- Base: `origin/main` en `5eae7c8` (el `main` local estaba 65 commits detrás).
- Rama: `mejoras/revision-integral-20261119`.
- Revisión estática y pruebas en la estación de trabajo, con un PostgreSQL
  17.6 efímero local. **No** se tocó el IACP Control Plane (VM 104), el
  clúster MNG, Platinum/PRX, volúmenes, secretos ni operaciones reales.
- Dictamen: correcciones de código **integradas en la rama, no desplegadas ni
  certificadas**. Antes de promoverlas se requiere revisión de PR y el
  procedimiento vigente de `scripts/iacpctl` / `scripts/go_live.py`.

## Inventario de servicios

Cada Compose (`compose.yaml` de desarrollo y
`deploy/portainer/docker-compose.yml` de producción) define **12 servicios**:

| Servicio | Cantidad | Perfil | Función |
| --- | ---: | --- | --- |
| `postgres` | 1 | siempre | Estado, cola, leases, idempotencia |
| `api` | 1 | siempre | FastAPI `/v1`, portal `/docs` y consola |
| `worker-1` … `worker-6` | 6 | siempre | Terraform / Proxmox / lifecycle |
| `service-worker-1`, `-2` | 2 | `services` | Instalación de aplicaciones |
| `service-worker-3`, `-4` | 2 | `service-workers-3-4` | Capacidad adicional de servicios |

Sin perfiles activos se ejecutan 8 contenedores. El catálogo de aplicaciones
publica 4 (`nextcloud`, `n8n`, `hermes`, `openclaw`), todas deshabilitadas
por defecto.

## Línea base

- `pytest` sin base de datos: 112 PASS y 301 omitidas.
- `pytest` con PostgreSQL efímero: 413 PASS.
- `ruff` (E, F, W, B, S, SIM, PL): sin errores `F`; 47 avisos de estilo o
  seguridad revisados de forma manual.

## Defectos corregidos

| # | Severidad | Ubicación | Defecto | Corrección |
| --- | --- | --- | --- | --- |
| 1 | Alta | `worker/main.py` `run_commands` | Terraform corría con `stdout/stderr=PIPE`, pero las tuberías no se leían hasta que terminaba. Con más de ~64 KiB de salida el proceso quedaba bloqueado hasta el timeout (1800 s) y se mataba, incluso durante `apply`/`destroy`, lo que dejaba la operación en `uncertain`. Reproducido: un proceso instantáneo con 300 KB terminó en `TimeoutError`. | La salida se vuelca a `tempfile.TemporaryFile`; se conservan el truncado y la redacción del secreto. |
| 2 | Media (latente) | `api/db.py` `_postgres_sql` | El `%` literal no se escapaba y `execute()` usaba `parameters=()`, por lo que `SELECT 'a%b'` fallaba con `ProgrammingError`. `prune_history` mezclaba `%s` nativo con el dialecto `?`. | Escapado de `%` cuando hay parámetros, `parameters=None` por defecto y `prune_history` con `?`. Se simplificó el manejo de comillas. |
| 3 | Media | `api/main.py` `authorize` | El token Bearer se comparaba con `!=`, que no es de tiempo constante. | `hmac.compare_digest` sobre bytes UTF-8. |
| 4 | Baja | `worker/main.py` `_recovery_operation_exists` | `payload_json LIKE '%id%'` recorría todo el texto y coincidía con subcadenas o con otros campos. | Comparación exacta `payload_json::jsonb ->> 'source_operation_id'`. |
| 5 | Baja | `api/main.py` | `@app.on_event("startup")` está obsoleto en FastAPI. | Migrado a `lifespan`. |

Las pruebas están en `tests/test_revision_integral.py`. Resultado final:
**420 PASS** con PostgreSQL efímero.

## Mejoras recomendadas (no aplicadas)

1. **Conexiones PostgreSQL:** `database()` abre una conexión nueva por
   consulta. Seis workers en reposo consultan cada 0,5 s (unas 12 conexiones
   por segundo), y cada worker ocupado envía un heartbeat por segundo. Se
   recomienda reutilizar conexiones (`psycopg_pool`) o espaciar los heartbeats
   durante Terraform, ya que el umbral de caducidad es de 900 s. La rama
   `origin/mejoras/revision-20261115`, no integrada, aborda parte de esto y
   debería rebasarse sobre `main`.
2. **Healthcheck de workers:** ningún worker ni service-worker tiene
   `healthcheck` en Compose; la salud solo se observa por `/v1/workers`.
3. **Contención de cerrojos:** `worker/services.py` ejecuta `recover()` con
   `BEGIN IMMEDIATE` (advisory lock `104,101`) en cada iteración, el mismo
   cerrojo que el claim de los workers de infraestructura. El umbral de 900 s
   está fijo en el código.
4. **Bucles sin contención de errores:** un fallo transitorio de PostgreSQL en
   `claim()`, `recover()` o en el heartbeat de reposo termina el proceso del
   worker. Docker lo reinicia, pero se pierde el contexto.
5. **Duplicación en Compose:** los 10 bloques de worker son casi idénticos;
   un ancla YAML `x-worker` reduciría la deriva entre entornos. Requiere
   validar `go_live.py` antes de cambiarlo.
6. **Columnas en `update_operation(**values)`:** interpola nombres de columna
   sin lista permitida; hoy solo recibe claves internas, pero conviene
   restringirlas.
7. **Credenciales de servicio:** `installation_credentials` usa
   `uuid4().hex`; `secrets.token_urlsafe` es la primitiva idiomática.
8. **Secreto huérfano:** si falla el `UPDATE`/`COMMIT` de idempotencia después
   de `write_secret`, el archivo de contraseña queda en el volumen.
9. **NetBox:** `IACP_NETBOX_VERIFY_TLS=false` desactiva la verificación TLS;
   conviene registrar una advertencia en el arranque cuando se usa.

## Ensayos físicos en desarrollo — 2026-11-20

Alcance: stack `iacp-controlplane-dev-v1` del IACP Control Plane (VM 104),
`dev-iac.cyberfuel.com`, clúster MNG, nodo `mng01`, VMID 40000–49999. La
matriz física se ejecutó con seis aprovisionamientos simultáneos y compuertas
de runtime en reposo. Producción (`iac.cyberfuel.com`) permaneció en `5eae7c8`,
sin cambios. La evidencia privada está en `/opt/iacp/evidence/iac-dev-mng/<run>/`
y `/etc/iacp/certification/dev-<sha>-*/` dentro de la VM 104.

| Corrida | Revisión | Resultado | Observación |
| --- | --- | --- | --- |
| `20261120040951` | `14c9c8e` | FAIL 10/12 (abortada tras la oleada 2) | 2 AlmaLinux 9 con `power start` explícito: `POWER_SERVICE_UNAVAILABLE`. Las VMs se eliminaron por la API. |
| `20261120054847` | `b7dc1dc` | FAIL 8/12 (abortada tras la oleada 2) | Reintentar lecturas del Guest Agent (`6a32c1c`) convirtió el fallo en `SERVICE_BOOTSTRAP_TIMEOUT` `uncertain` en los 4 AlmaLinux 9. Recuperación manual controlada: reconcile `completed`, apagado y destroy por la API; VMID ausentes en MNG. `6a32c1c` revertido en `4b15f60`. |
| `20261120062759` | `0acbd8e` | **PASS 16/16** | Ubuntu 24.04/26.04 y AlmaLinux 9/10, planes kvm-1…kvm-4, contraseña y llave, inicio explícito y automático. Pico de 6 workers; 16 instancias `deleted` y VMID ausentes. |

### Causa raíz AlmaLinux 9

Proxmox activa `ciupgrade` por defecto y el user-data generado incluye
`package_upgrade: true`. En el primer arranque, `dnf` actualizó
`qemu-guest-agent` (10.1.0-24.el9_9.1); el `%postun` del RPM reinició el
servicio y systemd terminó su cgroup, incluido el helper
`iacp-guest-bootstrap` lanzado por QGA. El agente nuevo no reconoce el pid y
`exec-status` falla de forma permanente (HTTP 596). Evidencia por SSH en la
VMID 40320: reinicio de qemu-ga a las 05:51:51 UTC, sin marcador
`/var/lib/iacp` y cloud-init `done`. El acceso confinado del agente
(`virt_qemu_ga_t`) no permite ejecutar `cloud-init` ni leer `/var/lib/cloud`;
esas observaciones no indican ausencia de cloud-init.

Corrección `d74396a`: `initialization.upgrade = false` en el módulo `kvm` y
compuerta preboot `ciupgrade=0` para las expectativas nuevas (fail-closed).
Las expectativas anteriores no exigen la marca, para no bloquear el
`power start` de VMs existentes. Prueba de humo previa (VMID 40325):
`ciupgrade: 0`, cero reinicios de qemu-ga y marcador de identidad presente.

### Dictamen y pendientes

- Integración en desarrollo: PASS para `0acbd8e` en la matriz de 16 casos.
- No equivale a certificación integral ni a promoción a producción.
- Decisión pendiente: sin actualización en el primer arranque, los parches de
  seguridad deben incorporarse en la preparación de las Golden Images
  (`scripts/prepare_golden_copies.py`).
- Fedora 45 y Debian 13 están publicadas en el catálogo dev, pero la matriz no
  las cubre.

## Certificación de desarrollo previa a `main` — 2026-11-20

Criterio acordado: actualizar `main` solo con la matriz KVM 48/48 PASS y la
matriz de aplicaciones 16/16 PASS sobre la misma revisión desplegada. Ambas se
ejecutaron en el stack dev del IACP Control Plane (VM 104) contra el clúster
MNG, nodo `mng01`, con seis aprovisionamientos simultáneos.

| Matriz | Corrida | Revisión | Resultado |
| --- | --- | --- | --- |
| KVM 48 (previa) | `20261120070143` | `0acbd8e` | PASS 48/48 |
| KVM 48 (aprobación) | `20261120072512` | `9b70d77` | **PASS 48/48** |
| Aplicaciones | `20261120074421` | `9b70d77` | **PASS 16/16 + 2/2 controles** |

**KVM 48:** 6 imágenes (Ubuntu 24.04/26.04, AlmaLinux 9/10, Fedora 45 y
Debian 13) × 4 planes × 2 modos de acceso de root (24 con contraseña y 24 con
llave SSH). Cada caso cubre aprovisionamiento con replay idempotente, inicio
explícito o automático, SSH, llave presente en `authorized_keys`, token de
persistencia, reboot, SSH y persistencia, apagado, encendido, SSH y
persistencia, apagado final, destroy y ausencia física de la VMID. Resultado
`PASS` y contención `False`. Duración: 18,1 min. Por caso: p50 126 s y p95
148 s.

**Aplicaciones:** 8 n8n y 8 Nextcloud en Ubuntu 24.04, planes kvm-1 a kvm-4
(dos por producto), y 2 controles con acceso solo por contraseña. Red WAN
`prod_static` en `vmbr3` con IPs reservadas en NetBox `190.0.231.0/24`. Los
18 casos ejecutaron los 29 checks A01–E06: 693 checks PASS y ninguno fallido.
Entre ellos: SSH con llave (B01), con contraseña (B02), rechazo de una
contraseña incorrecta (B03), acceso tras el reinicio (B04), ausencia de
credenciales en la evidencia (B05), GUI por HTTPS con CA y SAN IP (C03),
login y WebDAV (C04), datos sintéticos con checksum o ejecución de workflow
(C05), reinicio de contenedores (D02), reboot (D03), apagado y encendido
(D04), persistencia (D01 y D05) y coherencia entre el estado API y el estado
Proxmox (D06). Tras cada tanda de seis: 0 operaciones activas, 0 trabajos de
servicio, 0 advisory locks y 0 recursos Terraform. Evidencia:
`/opt/iacp/evidence/wan-servicios-ubuntu-20261120074421/` en la VM 104.

### Alcance del dictamen

- PASS de integración y certificación en **desarrollo** para `9b70d77`.
  Los commits posteriores en `devops/features` solo modifican documentación;
  el runtime certificado es el mismo.
- No es un GO de producción: `iac.cyberfuel.com` sigue en `5eae7c8`.

### Plan de promoción a producción (no ejecutado)

1. Revisar el PR o la fusión `devops/features` → `main` y fijar el SHA candidato.
2. Construir la imagen del SHA desde `git archive`, publicarla o precargarla
   en el IACP Control Plane (VM 104) y verificar SHA-256 e ID de imagen.
3. Respaldar `prod.env` y `manifest-prod.json` con el SHA anterior; actualizar
   solo `IACP_IMAGE_TAG` e `image_tag`.
4. Ejecutar `iacpctl preflight`, `deploy --preloaded-images`, `verify` y
   `activate` sobre `manifest-prod.json`, con producción en reposo: 0
   operaciones activas o inciertas y seis workers idle.
5. Ejecutar la matriz KVM de 48 en el rango 30000–39999 y la de aplicaciones
   (`--production --exact-cases`) con credenciales de ensayo autorizadas.
6. Plan de reversión: restaurar los respaldos del paso 3 y volver a
   desplegar el SHA anterior con `iacpctl`.

### Cambios operativos que la promoción debe considerar

- `initialization.upgrade = false`: las VMs nuevas no ejecutan `dnf` ni `apt`
  upgrade en el primer arranque. Los parches deben incorporarse en la
  preparación de las Golden Images.
- Nueva dependencia `psycopg-pool==3.2.6`. Cada proceso mantiene un pool de
  hasta 5 conexiones (11 procesos por stack, máximo 55).
