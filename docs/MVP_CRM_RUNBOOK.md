# MVP KVM y contrato CRM 0.10.0

Candidato desde `origin/main` 4771f8e, que integra PRs #28–#35. La release operativa
de referencia es 724fa10/0.9.1/0010. Este incremento añade 0011 y no reescribe sus
directorios, registros, planes ni evidencias. [Checklist](MVP_CHECKLIST.md).

## Producto y contrato

**IACP Orchestrator** es la plataforma; **IaC Worker** aprovisiona y **Lifecycle
Worker** administra energía. Swagger separa autoservicio de administración del
aprovisionamiento. Se conservan rutas históricas `/terraform`, operationIds y
campos `executor` por compatibilidad; no se elimina ninguna ruta ni se exige al
CRM un motor. No se necesitan alias nuevos para instances, operations, provision,
retry-provision o reconcile, que ya son neutrales. Una futura retirada exige una
versión de API y ventana de migración; no se declara deprecación sin reemplazo.

El backend CRM valida la compra antes de invocar IACP. El tenant proviene del
Bearer, nunca del JSON. `commercial_context` contiene referencias opacas
`customer_id`, `order_id`, `product_id`; no concede permisos ni certifica pago.
CPU, RAM, disco, imagen, nodos y red siguen limitados por perfiles/políticas.
Cada intención usa una `Idempotency-Key` estable generada y conservada por el CRM.
IACP persiste la clave junto a UUIDs de instancia/operación/job. Un mismo pedido
puede tener varias intenciones; el pedido no sustituye la clave de cada acción.

Ejemplo DHCP compatible con el esquema (revisar política vigente antes de enviar):

```json
{
  "name": "crm-demo-01",
  "image": "ubuntu-2404",
  "vcpus": 1,
  "memory_mb": 1536,
  "disk_gb": 20,
  "network_profile": "lab-dhcp",
  "self_service_policy_id": "ac558dfa-4c2b-4e2f-8886-67648e880dc8",
  "commercial_context": {
    "customer_id": "cliente-demo-01",
    "order_id": "pedido-demo-01",
    "product_id": "kvm-small"
  },
  "start_on_create": true
}
```

Para otro tamaño cambiar, por ejemplo, a 2 vCPU/2048 MiB/24 GiB **dentro de las
cuotas disponibles**. Consultar `/v1/profiles` y `/v1/lab/usage`. El laboratorio
usa Ubuntu 24.04/29000, VirtIO/vmbr0/DHCP/sin VLAN. El perfil estático conserva
IPAM, VLAN 204 y revisión individual; no recibe delegación DHCP ni bootstrap root
por extensión implícita. Clúster y nodos se seleccionan mediante el perfil del
servidor; `node_preference` solo admite destinos autorizados.

JSON sin secretos para Swagger: [DHCP pequeño](examples/crm/dhcp-small.json),
[otros recursos](examples/crm/dhcp-medium.json) y [arranque automático](examples/crm/dhcp-autostart.json).
La comprobación contra el contrato desplegado se registra en la evidencia final;
validar el esquema no equivale a ejecutar cada ejemplo ni a renovar su política.

## Entrada root de laboratorio

La decisión del usuario acepta la persistencia recuperable de la contraseña
inicial en **state Wasabi y versiones históricas** del laboratorio MNG, incluso
después de DELETE. También contienen secretos los planes, variables y manifest
privados del worker. `sensitive` oculta presentación CLI; no elimina persistencia.
La contraseña es única por VM, no un valor predeterminado en la Golden Image.
La plantilla compartida 29000 nunca se modifica por este flujo.

En un perfil nuevo con `root_password_bootstrap=true`, enviar adicionalmente
`access.username=root` y `access.password` desde memoria del backend por HTTPS.
No se proporciona un valor de contraseña en ejemplos, Swagger, resultados ni
evidencias. En las consultas `access` solo contiene estado y comprobaciones.
La API rechaza HTTP; detrás del terminador TLS confía únicamente en las IPs de
proxy declaradas y verificadas por el driver. No confía en un X-Forwarded-Proto
de un cliente arbitrario ni cambia la política de proxy headers existente.

La entrada queda cifrada Fernet en PostgreSQL con contexto de tenant/UUID y clave
externa. Un HMAC separado comprueba unicidad y replay sin persistir un hash simple
de contraseña. La clave se monta exclusivamente en API/IaC Worker; Lifecycle
continúa sin credenciales Wasabi, fuente ni planes. Conservar y respaldar la clave
fuera de Git; regenerarla rompe la recuperación de las entradas existentes.

El adaptador versionado se aplica **solo al workspace nuevo** que pide acceso
root. Conserva fuente 2c0f319, provider bpg/proxmox 0.116.1 y CLI 1.16.6, y sella
su SHA-256 junto al plan. El JSON de variables, plan guardado y configuración
generada deben coincidir antes de ejecutar o reconciliar. La trazabilidad incluye
commit IACP, fuente original y digest del adaptador; ver [inventario](ENGINE_INVENTORY.md).

Cloud-Init configura usuario/contraseña. La variante revisada del inicializador
de laboratorio configura explícitamente PermitRootLogin/PasswordAuthentication,
valida `sshd -t` y la configuración efectiva, y recarga SSH exclusivamente en la
clon nueva. Se mantiene el programa anterior para perfiles históricos. No acepta
scripts, rutas o comandos del CRM. El perfil nuevo y el hash del programa requieren
revisión independiente de política; habilitar root no amplía ACLs existentes.

## Seguimiento y recuperación del CRM

1. `POST /v1/instances`, Bearer y clave estable: 202 acredita admisión.
2. `GET /v1/operations/{operation_id}` hasta `normalized_status=completed`.
3. Si se pidió arranque, esperar también `fulfillment.status=completed`. Sus fases
   distinguen aprovisionamiento, start, inicialización y verificación de acceso.
4. `GET /v1/instances/{instance_id}`: recursos, UUID, VMID/nodo, IPs y `access`.
   `access.ssh_authenticated=true` acredita el acceso inicial observado, no una
   comprobación continua. `guest_ready=null` no promete readiness de la aplicación.
5. Lifecycle: `POST /power/start`, `/power/reboot`, `/power/shutdown`, cuerpo `{}`
   y clave propia por acción. Consultar cada operación; sin flags force.
6. DELETE con `{}` y clave propia, exclusivamente con propiedad/política, VM nueva
   descartable apagada y verificaciones vigentes. Esperar operación e inventario
   `deleted`. VM104/106 y recursos ajenos siguen protegidos.

Un timeout HTTP se recupera repitiendo exactamente **cuerpo y clave**, nunca con
otra VM. `/provision` continúa la misma instancia cuando se pausó una entrega de
plan/arranque. `/retry-provision` exige `expected_attempt` y solo replanifica antes
de un Apply consumido. `/reconcile` observa mutaciones inciertas, no las repite.
`POST /access/verify` es una observación idempotente que recupera Cloud-Init/SSH sin
cambiar credenciales. Consultar `/v1/operations?instance_id=...` para todo el historial.

El arranque se despacha al Lifecycle Worker después del Apply verificado, con
clave determinista y fences existentes. Una publicación perdida deja una intención
observable y recuperable. La verificación SSH fija la host key obtenida del mismo
Guest Agent vinculado a MAC/UUID; no usa AutoAddPolicy, archivos privados del host
ni credenciales en argumentos de procesos. Cloud-Init y autenticación deben estar
verificados antes de marcar acceso completo.

| HTTP | Tratamiento del CRM |
|---|---|
| 401 | Renovar/entregar identidad válida; no repetir en bucle |
| 403 | Revisar rol/tenant; no ampliar permisos como compensación |
| 404 | Ausente o ajeno; no revela inventario de otro tenant |
| 409 | Leer código: cuota, política, conflicto de clave, operación activa o contraseña reutilizada |
| 422 | Corregir contrato/HTTPS; errores no repiten valores de entrada |
| 429 | Rate limit del proxy si corresponde: backoff con jitter y Retry-After |
| 5xx | Backoff acotado, conservar intención, consultar operación y repetir misma clave/cuerpo |

## Promoción controlada

Usar exclusivamente checkout nuevo, `deploy/mng/compose.yaml`, `server.env.next`,
manifest revisado y `scripts/iacpctl`. No crear otro Compose ni cambiar DB/Redis/
Traefik/redes/puertos o restricciones de acceso. Antes de promover: suite completa,
OpenAPI generado, Terraform/provider validate, backup privado restaurado en base
aislada, doctor readonly y colas vacías. Mantener la release 724fa10 intacta.

Crear una clave Fernet privada fuera de Git, legible por UID/GID10001. Referencias:
`IACP_ROOT_INPUT_KEY_PATH`, `IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED=1`,
`IACP_TLS_PROXY_IPS` con IPs exactas del proxy. El manifest declara
`root_bootstrap_configuration` con `key_path`, `enabled` y `proxy_ips`. El driver
solo permite esos cambios tipados y comprueba aislamiento, propiedad y permisos.
Publicar un perfil nuevo derivado del actual con usuario root/29000 y revisión
independiente; no editar los snapshots de políticas ya aprobadas por SQL.

La migración 0011 añade campos sin reinterpretar filas. Los snapshots comparan
proyecciones históricas y defaults nuevos. No hacer downgrade ni restaurar una DB
vieja sobre operaciones/VMs nuevas. Ante fallo, cerrar admisión/bootstrap y hacer
rollforward compatible con 0011, preservando clave, historial y state. La imagen
0.9.1 exige su head anterior para readiness; no prometer rollback de aplicación
tras migrar sin un escenario coherente y verificado de restauración.

Rotación posterior del cliente: conectarse por SSH con host key validada, cambiar
interactivamente con `passwd`, registrar en el CRM actor/UUID/fecha/referencia de
secreto sin su valor y verificar acceso nuevo. No editar state, planes, PostgreSQL
o Cloud-Init para forzar una rotación; el secreto inicial histórico continúa siendo
sensible. Una API dedicada de rotación requiere su propio contrato/fence y no está
incluida en esta primera entrega. Para producción: gestor de secretos protegido y
bootstrap fuera de argumentos recuperables del provider, por ejemplo entrega de
corta duración por QGA con verificación y fence independientes.

Locust se prepara y ejecuta manualmente en **iacp-load-testing**, fuera del runtime.
Ver [contrato IaCoC](IACOC_CONTRACT.md) y [auditoría/retención](STATE_RETENTION_AUDIT.md).
