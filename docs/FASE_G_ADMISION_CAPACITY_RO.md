# Admisión de capacidad con identidad Proxmox independiente

Fecha: 25/10/2026, America/Costa_Rica.

> Actualización posterior del 25/10/2026: el secreto ya fue instalado, TLS se
> verificó mediante FQDN y nodo/datastore devolvieron 200. Ceph continúa en 403.
> La clave SSH técnica ya está preparada. Consultar el
> [cierre comercial e imágenes](FASE_G_CIERRE_COMERCIAL_IMAGENES.md).
> Los resultados originales siguientes conservan su alcance histórico.

Identidad autorizada por Infraestructura: `iacp-admission@pve!capacity-ro`.
**Implementación candidata; sin promoción y sin HTTP reales certificados con
esta identidad hasta disponer de su secreto operativo.**

## Endpoints y permisos comprobados

Se inspeccionó `live_capacity()` y el esquema API servido por el propio clúster
MNG, Proxmox VE **9.2.20**. No se dedujeron los permisos a partir del nombre
del token ni se solicitó otra auditoría integral de Ceph.

| Método y ruta real | Permiso del esquema instalado | Elección de solo lectura |
|---|---|---|
| GET `/nodes/mng01/status` | `Sys.Audit` en `/nodes/mng01` | `Sys.Audit` |
| GET `/nodes/mng01/storage/DS_CYBERFUEL/status` | `Datastore.Audit` o `Datastore.AllocateSpace` en `/storage/DS_CYBERFUEL` | Solo `Datastore.Audit` |
| GET `/nodes/mng01/ceph/status` | `Sys.Audit` o `Datastore.Audit` en `/` | Auditoría únicamente; sin conceder escritura |

El alias de Ceph es una consulta de salud del clúster y exige auditoría en `/`.
Una ACL limitada a nodo y datastore puede no bastar para esta tercera llamada.
No se omite la comprobación ni se modifica automáticamente una ACL para superar
el rechazo. Infraestructura debe verificar que la identidad autorizada cumple
los permisos efectivos que requiere su versión instalada.

Referencias primarias: [API Viewer oficial de Proxmox](https://pve.proxmox.com/pve-docs/api-viewer/)
y [gestión oficial de permisos y tokens](https://github.com/proxmox/pve-docs/blob/master/pveum.adoc).
La versión y los permisos de la tabla proceden del esquema servido por MNG;
no se supone que una edición futura del sitio público sea idéntica.

## Conexión y aislamiento

- `app/iacp/services/capacity_readonly.py` es un cliente HTTPS exclusivo: admite
  solo GET de las tres rutas y la consulta de permisos propios. No tiene
  operaciones de clonación, Lifecycle, guest, DELETE ni escritura.
- El secreto JSON independiente contiene únicamente `endpoint` y `api_token`.
  El token debe identificarse exactamente como `iacp-admission@pve!capacity-ro`.
  Se rechazan identidades de Terraform/Lifecycle, archivos inseguros, HTTP,
  redirecciones y datos adicionales. Los errores no exponen valores.
- El host suministra la referencia operativa verificada mediante
  `IACP_ADMISSION_CREDENTIALS_PATH`. Solo **API** monta ese archivo como
  `/run/secrets/admission_credentials`; `IACP_ADMISSION_CREDENTIALS_FILE`
  identifica el montaje para el proceso. No se monta en workers ni se incorpora
  a imágenes, cola, DB o contexto comercial. No hay fallback a credenciales
  del worker ni se modifican permisos del CRM.
- La CA del endpoint debe validar con el trust store existente o con la
  referencia pública `IACP_ADMISSION_CA_FILE` explícitamente revisada. No se
  permite `verify=false`, certificados ignorados ni `curl -k`.
- El manifiesto revisa `commercial_bootstrap_configuration` con `contract`,
  `aes_key_path`, `ssh_key_path`, `admission_credentials_path` y `ttl_seconds`.
  El preflight comprueba los archivos y el parseo de la identidad antes de
  reemplazar el entorno o detener la API. `/dev/null` solo es un marcador
  inactivo del contrato histórico; no habilita G.

Los controles de catálogo, imagen, tenant, cuotas, concurrencia, propiedad,
idempotencia y recursos protegidos permanecen intactos. `live_capacity()` sigue
descontando reservas y requiere el datastore activo, memoria/CPU/disco suficientes
y `HEALTH_OK` de Ceph cuando se utiliza rbd/cephfs. Una credencial válida no
convierte una solicitud inválida en una ejecución autorizada.

## Certificación real pendiente y probe de escritura

`scripts/audit-admission-capacity.py` utiliza únicamente el secreto RO:

1. Lee permisos propios y rechaza cualquier privilegio de escritura o valor
   ambiguo antes de probar nada.
2. Exige HTTP 200 en las tres lecturas reales. El cliente no acepta otra respuesta.
3. Con `--verify-write-denied`, envía un **PUT vacío** a
   `/nodes/mng01/config`, que exige `Sys.Modify` en `/`. No establece parámetros,
   no toca VMs, no solicita reboot/shutdown ni cambia ACL. Solo HTTP **403**
   certifica denegación. 401, 404, errores de transporte o un 200 no cuentan
   como PASS. Si detecta facultades de escritura, aborta antes del probe.
4. Emite solo fechas locales, identidad no secreta, rutas, códigos y estado.

El probe es una herramienta administrativa explícita, no una capacidad expuesta
por la API de admisión. No se ejecutó con la credencial Terraform para atribuirle
resultados al token RO.

## Resultado actual

Regresión final: 1012 pruebas aprobadas, cero omitidas; 144 focalizadas de
admisión, catálogo y despliegue aprobadas. Los rechazos de escritura de las
pruebas con transporte controlado no se presentan como HTTP 403 reales.

La búsqueda por metadatos y referencias de la identidad en `/etc/iacp`,
`/opt/iacp/config` y los secretos existentes del stack no encontró su material
de autenticación. No se creó un archivo vacío, un token ficticio ni una copia
de las credenciales privilegiadas para completar el procedimiento.

| Comprobación | Estado |
|---|---|
| Cliente exclusivo, sin fallback | Implementado en candidata |
| Montaje limitado a API | Implementado y comprobado por prueba |
| Escrituras bloqueadas antes de transporte | Pruebas de código |
| Clasificación de HTTP 403 y rechazos ambiguos | Pruebas con transporte controlado |
| Lecturas HTTP 200 con `capacity-ro` | **BLOCKED: secreto operativo no localizado** |
| Escritura real denegada con `capacity-ro` | **NO EJECUTADA**, mismo bloqueo |
| G6 físico y E2E KVM 1/KVM 2 | **BLOCKED**, además falta identidad privada SSH operativa |

No se fusiona ni promociona el correctivo automáticamente. El runtime histórico
permanece en `8a58db244ec1e9cd1a0d627b21e086548489e75d`. **NO-GO FASE G** hasta
certificar identidad RO real, bootstrap, promoción y ambos E2E secuenciales.
