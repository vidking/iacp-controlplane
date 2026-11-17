# Evidencias de servicios en desarrollo — 2026-11-17

Ámbito: `dev-iac.cyberfuel.com`, IACP Control Plane (VM 104), clúster MNG,
nodo `mng01`. Rama `devops/features`. Producción permanece sin actualizar en
`85e54ac063ce2ae2fed4b80b760fdf3f90f0fe82`.

| Comprobación | Resultado | Evidencia y límite |
| --- | --- | --- |
| Runtime local con seis workers de infraestructura y dos de servicios | PASS | `/tmp/iacp-servicios-ui-bootstrap-v2-20261117/`, 209 pruebas; infraestructura simulada |
| Formulario Nextcloud/n8n en Chromium | PASS | `tests/browser_services.cjs`, contrato n8n, imágenes compatibles, contraseña redactada, consulta explícita única de credenciales y entrega; API simulada |
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
