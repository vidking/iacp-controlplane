# Evidencias de servicios en desarrollo — 2026-11-17

Ámbito: `dev-iac.cyberfuel.com`, IACP Control Plane (VM 104), clúster MNG,
nodo `mng01`. Rama `devops/features`. Producción permanece sin actualizar en
`85e54ac063ce2ae2fed4b80b760fdf3f90f0fe82`.

| Comprobación | Resultado | Evidencia y límite |
| --- | --- | --- |
| Runtime local con seis workers de infraestructura y dos de servicios | PASS | `/tmp/iacp-servicios-ui-admin-readiness-20261117/`, 211 pruebas; infraestructura simulada |
| Formulario Nextcloud/n8n en Chromium | PASS | `tests/browser_services.cjs`, contrato n8n, imágenes compatibles, contraseña redactada, consulta explícita única de credenciales y entrega; API simulada |
| Alta de propietario, login n8n, rechazo de clave inválida, workflow y reinicio | PASS | `scripts/test_n8n_owner_docker.py`; Docker aislado sin red externa, no certifica MNG |
| Nextcloud real, PostgreSQL, Redis, Caddy/TLS por IP, WebDAV y persistencia | PASS | `scripts/test_nextcloud_docker.py`; imagen fijada, Docker local, no certifica MNG ni Golden Images |
| Handshake TLS por IP y protección inicial de n8n | PASS | `.venv/bin/python scripts/test_ip_tls_docker.py`; Caddy local y CA temporal |
| Autorización del dominio dev | PASS | Chromium contra dev: valor, comillas, Bearer y asignación; token leído privadamente en el host |
| Bootstrap AlmaLinux v2 | PASS | Nuevas copias 29014/29015 desde 29010/29011; SELinux Enforcing y protocolo 1; no certifica aplicaciones |
| Tanda física LAN de servicios anterior | FAIL | `/opt/iacp/evidence/lan-services-ciclo1-202611170901/`; cuatro Ubuntu llegaron a ready y tres Nextcloud finalizaron instalación; AlmaLinux falló durante arranque |
| Limpieza de esa tanda | PASS | `cleanup.json` en el mismo directorio; seis instancias deleted y ausentes de Proxmox; cero operaciones activas o inciertas |
| Matrices integrales LAN y WAN | BLOCKED | Ningún ciclo completo aprobado todavía; repetir LAN con bootstrap v2. WAN pendiente de confirmar VLAN 211/gateway |

## Correcciones derivadas del ensayo

- Cloud-init emite puntos de progreso sin salto de línea cuando se espera su
  finalización en formato tabular. El bootstrap v2 consume JSON y rechaza errores
  reales; solo admite las deprecaciones conocidas.
- AlmaLinux 10 puede tener `resolvectl` instalado con resolved inactivo. En ese
  caso se observa el DNS efectivo de NetworkManager.
- La reconciliación de una intención antigua no debe revertir una eliminación
  posterior. Se conserva `deleted` al confirmar ausencia y liberación; IPAM
  pendiente sigue visible como incertidumbre. La tanda anterior requirió una
  eliminación lógica adicional por API; fue recuperación controlada.
- La ejecución CLI del workflow n8n necesita un puerto interno de broker distinto
  del proceso principal. Se comprobó el conflicto y su corrección en Docker.
- La clave de host AlmaLinux se obtiene mediante el helper fijo por Proxmox
  autenticado; no se elude SELinux ni se aceptan claves sin verificar.

Las copias 29012/29013 se conservan como revisiones anteriores. No se modifican
las originales ni las VMs 104, 106 y 29000. Las credenciales temporales de fábrica
se retiraron de las nuevas copias antes de convertirlas en templates.

## Actualización del ensayo — 2026-11-17

| Comprobación | Resultado | Evidencia y límite |
| --- | --- | --- |
| Ciclo LAN de servicios `202611171525` | BLOCKED | `/opt/iacp/evidence/lan-services-ciclo1-202611171525/results.jsonl`; seis Nextcloud alcanzaron KVM lista y completaron instalación, pero el runner perdió su salida SSH (`BrokenPipeError`) con solo dos verificaciones de aplicación registradas; no es un ciclo aprobado |
| Recuperación de los seis recursos propios | PASS | `/opt/iacp/evidence/lan-services-ciclo1-202611171525/recovery-attempt-02.json`; eliminación por API y ausencia física confirmada en Proxmox; no se tocaron VMs ajenas |
| Matriz completa de Nextcloud y n8n en MNG | BLOCKED | No existe todavía un ciclo completo 16+2. La recuperación anterior no sustituye las verificaciones pendientes ni certifica n8n |

El runner ahora conserva la evidencia y continúa hacia limpieza aunque se cierre
su salida SSH. La receta de n8n espera `/healthz/readiness`, que comprueba la
conexión y migraciones de la base de datos; la matriz también verifica el alta
del propietario, login real, rechazo de una contraseña incorrecta, acceso al
workflow y persistencia después del reinicio. El contenedor local pasó esas
comprobaciones sin acceso de red externa.

La primera ejecución del smoke test de Nextcloud detectó un defecto en el
propio test: consultar `occ status` como root mientras la instalación seguía
inicializando creaba un `config.php` vacío propiedad de root. Se corrigió el
test para esperar `/status.php` por HTTPS y ejecutar `occ` como `www-data`; la
repetición terminó PASS y comprobó WebDAV y persistencia tras reiniciar la app.

La interfaz de servicios quedó plegable por defecto, permite elegir Nextcloud o
n8n tras consultar el catálogo y presenta acciones de administración. El
inventario excluye registros eliminados sin borrar su historial. El código se
publicó en `devops/features` como revisión
`5566266c7b52eed0a8376b05ef56e61db77f9d53`; su despliegue en desarrollo sigue
pendiente. Las pruebas Docker y del navegador no equivalen a una prueba física
en MNG.

## Continuidad

Priorizar Nextcloud/n8n: cuatro planes por Ubuntu 24.04 y AlmaLinux 9, 16 casos
más dos controles por ciclo y tandas de seis. Los nombres nuevos serán
`prueba-service-nextcloud-...` o `prueba-service-n8n-...`. Validar SSH con llave y
contraseña, red LAN, HTTPS con CA, operación de aplicación, entrega, persistencia
tras reboot y eliminación antes de aprobar cada caso.

Los agentes se abordarán después de estabilizar ambos servicios. No hay
certificación integral ni autorización de promoción a producción derivada de
estas evidencias parciales. Los archivos privados de evidencia permanecen en el
host; no copiar secretos, claves privadas ni Terraform State a Git.
