# Catálogo de servicios y agentes — integración piloto

## Alcance y estado

Esta ampliación añade pedidos de VM + aplicación, una cola PostgreSQL independiente,
dos workers de servicios, sobres cifrados, recetas Ansible/Compose e identificación
IPAM. El entorno sigue siendo `lab` en el clúster MNG. `prod_static` es un perfil de
red, no habilita Platinum/PRX. No se han ejecutado despliegues físicos ni certificado
imágenes de aplicación. La disponibilidad comercial no se deduce de las pruebas de código.

Las seis plazas de infraestructura permanecen independientes de las dos plazas de
servicios. La API guarda pedido y dependencia en una transacción; el pool consulta
la VM y no consume un trabajo antes de `ready` + `running`. No se introducen brokers.

Los endpoints conservan la autenticación del API existente. El contexto comercial
identifica pedidos; no implementa autorización entre clientes. Un portal público
necesita realizar esa autorización en su backend antes de consumir IACP. No entregar
el token global de IACP al navegador de un cliente.

## Configuración y responsabilidades

Las instalaciones están deshabilitadas por defecto:

- `IACP_SERVICES_ENABLED=0`: no aceptar pedidos de aplicación.
- `IACP_SERVICE_EXECUTION_ENABLED=0`: bloquear ejecución Ansible incluso si hay pedidos.
- Perfil Compose `services`: los dos contenedores adicionales se incluyen solo al activarlo.
- `IACP_SERVICE_CONFIG_DIR`: directorio del operador, preferiblemente absoluto,
  montado en API y workers como `/run/iacp-services`, solo lectura. No montar el socket Docker.

Contenido requerido del directorio (no incorporar secretos a Git):

1. `key`: clave Fernet propia de dev, legible solo por el UID/GID 999 del runtime.
   `prepare_develop.py` prepara el directorio con permisos 700 y el archivo con 600.
   Su copia de recuperación debe mantenerse separada de PostgreSQL; perderla impide
   continuar instalaciones y recuperar sus credenciales. La rotación requiere recifrar
   los sobres antes de sustituirla; no existe aún una utilidad automática de rotación.
2. Los bindings se preparan automáticamente por QEMU Guest Agent y se guardan en
   PostgreSQL, después de verificar identidad y pertenencia al pool de la VM.
   `bindings/<instance_id>.json` se conserva únicamente como alternativa operativa
   verificada por el administrador; no se acepta automáticamente una salida de ssh-keyscan.
3. Las releases probadas se incluyen en `/app/services/releases.json`. Un archivo
   externo es opcional y se selecciona con `IACP_SERVICE_RELEASES_FILE`.
   La primera instalación fija la huella; no se actualizan instancias por cambiar etiquetas.

Binding de ejemplo (valores ilustrativos):

```json
{
  "instance_id": "UUID_DE_LA_INSTANCIA",
  "vmid": 40001,
  "address": "190.0.231.20",
  "domain": "cloud.cliente.example",
  "host_key": "ssh-ed25519 CLAVE_PUBLICA_REAL_DEL_HOST"
}
```

El ejecutor comprueba VMID, instancia, dirección estática declarada, host key y
coincidencia del dominio aprobado. Para aplicaciones web exige que DNS resuelva
hacia esa dirección. Los registros de dominios deben existir previamente; la creación automática de
registros está fuera de esta etapa. Cloudflare y Google se usan como resolvers
`1.1.1.1` y `8.8.8.8`, no como APIs de publicación DNS.

Estructura de una entrada de `releases.json`:

```json
{
  "nextcloud": {
    "approved": false,
    "recipe_version": "1",
    "images": {
      "app": "nextcloud@sha256:DIGEST_REAL",
      "postgres": "postgres@sha256:DIGEST_REAL",
      "redis": "redis@sha256:DIGEST_REAL",
      "caddy": "caddy@sha256:DIGEST_REAL"
    },
    "verification_argv": ["php", "--version"]
  }
}
```

El ejemplo no es ejecutable: exige digests de 64 caracteres hexadecimales y
`approved=true`. La comprobación ilustrativa de PHP no certifica Nextcloud.
El operador debe seleccionar una comprobación funcional válida para la versión
fijada. Para n8n se necesitan `app`, `postgres`, `caddy`; para los agentes, `app`.
La imagen PostgreSQL debe usar `/var/lib/postgresql/data` (por ejemplo la familia 17).
La huella del manifiesto queda fijada en la primera ejecución: un reintento con
otro manifiesto se bloquea con `SERVICE_RELEASE_CHANGED`. No usar esta ruta para
actualizaciones de versiones. Las imágenes Nextcloud deben ser la variante Apache y las imágenes de agentes
necesitan los entrypoints/CLI documentados en la receta; no todas las variantes son intercambiables.

## Flujo de API

Todas las mutaciones de pedidos/trabajos llevan `Idempotency-Key`. La recuperación
de credenciales es una lectura destructiva explícita y no se guarda en la tabla de
idempotencia para evitar persistir una respuesta con secretos en texto claro.

1. `GET /v1/applications`: catálogo separado de `/v1/images`.
2. `POST /v1/service-instances`: `application_id`, `instance` con el contrato KVM
   actual, `configuration` y `secrets`. Requiere Ubuntu 24.04, inicio automático y
   una clave SSH del cliente. Añade una clave pública temporal y `app-<aplicación>`.
3. `GET /v1/service-instances/{id}`: infraestructura y aplicación por separado.
4. `GET /v1/service-jobs/{id}`: trabajo, generación, error y eventos.
5. `POST /v1/service-jobs/{id}/retry`: reintento; para `uncertain` requiere
   `{"previous_execution_stopped":true}` después de verificar procesos y lock remoto.
6. `POST /v1/service-jobs/{id}/cancel`: solo trabajos `queued` o `blocked`;
   no mata procesos remotos ni elimina VM/IP. No existe cancelación forzada de una ejecución.
7. `POST /v1/service-instances/{id}/connections`: actualizar claves y usuarios
   Telegram de un agente bloqueado o pendiente de entrega; genera otro trabajo.
8. `POST /v1/service-instances/{id}/credentials`: recuperación única con
   `Cache-Control: no-store`; el consumidor debe conservar el resultado de forma segura.
9. `POST /v1/service-instances/{id}/deliver`: tras recuperar credenciales y completar
   configuración, encola revocación SSH. Al confirmar, elimina el sobre del control plane.
10. `GET /v1/service-workers`: capacidad y heartbeats independientes.

Ejemplo de pedido (marcadores, no credenciales reales):

```json
{
  "application_id": "nextcloud",
  "instance": {
    "name": "cloud-cl-0001", "plan_id": "kvm-2", "image_id": "ubuntu-2404",
    "environment": "lab", "network_profile": "prod_static",
    "access": {"ssh_public_keys": ["ssh-ed25519 CLAVE_PUBLICA_CLIENTE"]},
    "commercial_context": {
      "customer_id": "CL-0001", "customer_tag": "cl-0001",
      "order_id": "ORD-0001", "product_id": "NEXTCLOUD-2"
    },
    "tags": ["cloud-cl-0001"], "start_on_create": true
  },
  "configuration": {"domain": "cloud.cliente.example", "admin_user": "admin"}
}
```

Estados de aplicación: `waiting_vm`, `queued`, `installing`, `delivering`, `blocked`, `uncertain`,
`awaiting_customer`, `delivered`, `cancelled`, `deleted`. El trabajo contiene la
fase; que Terraform termine no significa que el servicio esté instalado.
Los errores de aplicación no destruyen la VM ni liberan su dirección.

## Diferencias por aplicación

- **Nextcloud:** PostgreSQL, Redis, cron, Caddy y administrador inicial. Se comprueba
  instalación mediante `occ status` además de HTTPS y la comprobación revisada.
- **n8n:** PostgreSQL y clave de cifrado estable. Se protege el alta de propietario
  con Basic Auth en Caddy: usuario `installer` y contraseña recuperada por API.
  El cliente crea su propietario en la interfaz, configura sus integraciones y
  decide cuándo retirar esa protección del proxy. La contraseña inicial es para
  el proxy, no una cuenta precreada de n8n. Confirmar licencia del modelo comercial.
- **Hermes:** configuración persistente y proveedor OpenRouter en esta receta v1.
  `configuration.model` y `secrets.provider_api_key` son obligatorios. Telegram es
  opcional y requiere token y `telegram_allowed_users` explícitos.
- **OpenClaw:** onboarding no interactivo, proveedor OpenRouter y selección del modelo.
  Usa gateway sin publicación de puertos. La sintaxis CLI debe certificarse contra
  la imagen fijada; las versiones del proyecto cambian y pueden requerir otra receta.
- **Agentes:** acceso inicial mediante SSH y CLI/consola dentro del contenedor.
  No se entrega un dashboard público ni se habilitan plugins o Docker socket.
  OAuth y QR se completan con intervención del cliente; el instalador no los simula.

## IPAM y trazabilidad

El cliente NetBox valida exactamente `190.0.231.0/24`, exige el gateway `.1` reservado
y excluye red, broadcast y gateway. El ID del prefijo se configura en servidor.
Propaga `customer_tag`, etiquetas explícitas y etiqueta de aplicación; conserva
las etiquetas ajenas al recuperar una reserva y crea las de identificación ausentes.
Las etiquetas operativas `ocupada` y `reservada` deben existir previamente.

La selección de candidatos se serializa entre aprovisionadores IACP por prefijo.
La recuperación busca una IP cuyo marcador identifica la instancia antes de reservar
otra. Esa exclusión no coordina otros asignadores externos: el equipo de operaciones debe comprobar
las restricciones de unicidad y política de escrituras de la instalación NetBox.

El perfil mantiene `IACP_PROD_STATIC_ENABLED=0` hasta verificar prefijo, gateway,
bridge/VLAN, enrutamiento WAN, firewall y token. Registrar una IP en NetBox no
configura esos elementos de red. No modificar la Golden Image protegida 29000.

## Recuperación y entrega al equipo de operaciones

Se mantiene una exclusión durable por trabajo y un directorio lock en el invitado
`/var/lib/iacp-service-install.lock`. Una caída de heartbeat lleva a `uncertain`;
no se expira el lock remoto ni se repite automáticamente una mutación incierta.
Antes de autorizar el reintento, comprobar que el proceso anterior terminó, examinar
la aplicación y retirar el lock solo con evidencia. Los reintentos conservan secretos
y volúmenes. Si la pérdida ocurre durante la revocación de la clave, comprobar su
retirada por el canal del operador: no asumir una entrega correcta por falta de SSH.

No habilitar la ejecución antes de:

- Verificar código, imagen y esquema realmente ejecutados en el IACP Control Plane (VM 104).
- Preparar configuración cifrada, bindings SSH y releases certificadas por aplicación.
- Verificar la red y DNS desde los workers hacia las VMs del clúster MNG.
- Aplicar el procedimiento vigente de iacpctl/go_live y conservar seis workers KVM.
- Ejecutar los ensayos aislados exigidos por AGENTS.md, incluidos reinicio,
  recuperación, dos instalaciones y seis aprovisionamientos simultáneos.

La configuración de firewall del invitado se conserva: esta versión no instala
reglas de red ni afirma haberlas comprobado. Caddy publica 80/443 dentro del VPS;
PostgreSQL y Redis no publican puertos. Las actualizaciones posteriores y respaldos
administrados no forman parte del producto VPS preinstalado.

Referencias revisadas: [Nextcloud Docker](https://github.com/nextcloud/docker),
[Hermes Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker),
[configuración Hermes](https://github.com/NousResearch/hermes-agent/blob/main/cli-config.yaml.example),
[OpenClaw automatización](https://docs.openclaw.ai/start/wizard-cli-automation).

## Evidencia local — 2026-11-16

Alcance: rama `feature/service-agent-provisioning`, esquema 3, recetas 1;
cambios locales sin commit. No corresponde a una versión desplegada.

- **PASS:** `.venv/bin/pytest -q` ejecutado con `IACP_TEST_DATABASE_URL` hacia
  un PostgreSQL temporal local, creado y detenido con pgserver: **154 passed**,
  tres advertencias de deprecación. Incluye independencia de seis plazas KVM y
  dos de servicios, concurrencia de claims, migración, cifrado, recuperación,
  idempotencia, entrega y validación de sintaxis Ansible de las cuatro recetas.
- **PASS:** `docker compose -f compose.yaml --profile services config --quiet` y
  `docker compose -f deploy/portainer/docker-compose.yml --profile services config --quiet`,
  con variables obligatorias ficticias. No se contactó al daemon para estas validaciones.
- **PASS:** `git diff --check`, `.venv/bin/python -m compileall -q api worker tests`,
  `uv lock --check --python 3.12` y Ruff F sobre los módulos nuevos.
- **BLOCKED / pendiente de ensayo operativo:** construcción de imagen Docker
  (el usuario del entorno no tiene acceso al daemon), SSH real, API NetBox real,
  publicación DNS/TLS, reinicios y recuperación física en MNG. No existe dictamen
  GO integral ni certificación comercial de ninguna aplicación.


## Adenda: candidato dev y promoción con servicios

El generador `scripts/prepare_develop.py` crea un directorio candidato nuevo y
privado. No sobrescribe `/etc/iacp/stacks/dev.env`, no genera tokens de Proxmox,
no configura red y no despliega. Requiere un token y pool de MNG exclusivos de dev;
compara el token con el archivo productivo, genera nuevas claves de API/PostgreSQL
y una clave Fernet propia. Se ejecuta como root para asignar el secreto al UID/GID
999 de la imagen, conservando permisos mínimos. No se deben pasar secretos por
argumentos de shell: el token se recibe desde un archivo privado con modo 600.

Ejemplo para el equipo de operaciones, después de contar con una revisión inmutable autorizada:

```bash
sudo /ruta/al/release/.venv/bin/python scripts/prepare_develop.py \
  --output-dir /etc/iacp/stacks/dev-candidate-REVISION \
  --proxmox-token-file /ruta/privada/token-dev \
  --reference-env /etc/iacp/stacks/prod.env \
  --image-tag REVISION_DE_40_CARACTERES \
  --proxmox-endpoint https://mng01.cyberfuel.com:8006/ \
  --proxmox-host-ip IP_VERIFICADA_MNG01 \
  --pool-id POOL_EXCLUSIVO_DEV
```

El resultado fija dominio `dev-iac.cyberfuel.com`, esquema 3, seis operaciones KVM,
dos workers de servicios, rango 40000–49999, Wasabi apagado y ejecución Ansible
deshabilitada inicialmente. El archivo productivo solo se lee. El token y pool
deben existir y tener los permisos verificados por el operador antes del preflight.

`iacpctl/go_live` activa el perfil `services` según `services_enabled` del
manifiesto. El preflight rechaza credenciales dev compartidas o heredadas desde
variables externas que no coincidan con el archivo privado; comprueba imágenes,
base y cantidad de servicios. El verificador exige esquema 3, tablas nuevas,
clave Fernet legible, endpoints y heartbeats recientes de los pools 6+2. Los
manifiestos históricos sin estas opciones mantienen el contrato de esquema 2.

Para repetir la verificación aislada completa en la máquina de desarrollo:

```bash
.venv/bin/python scripts/test_local_docker.py --evidence-dir /tmp/iacp-dev-validacion-NUEVA
```

El comando construye la imagen real y una derivada con pytest; usa una red Docker
interna sin puertos publicados y credenciales ficticias. Arranca PostgreSQL y los
dos pools, ejecuta el verificador real de promoción contra ese stack y después la
suite. Conserva evidencias en el directorio indicado y retira exclusivamente sus
contenedores, red y volúmenes temporales. No ejecuta operaciones contra MNG.

Las releases incluidas corresponden a Nextcloud 35.0.3, n8n 2.47.7, Hermes 0.23.2 y
OpenClaw 2026.11.4, con digests fijados. Su alcance de aprobación es Docker local;
no equivale a certificación física ni a aprobación de producción.

### Revisión del servidor (solo lectura)

En el IACP Control Plane (VM 104), la revisión observada fue `85e54ac063ce2ae2fed4b80b760fdf3f90f0fe82`,
esquema 2, seis workers idle, 59 operaciones exitosas y ningún lock esperando.
No existían stack ni volúmenes dev activos. El manifiesto dev conservaba otro
nombre de dominio y concurrencia 1, y sus credenciales coincidían con producción.
La comprobación directa con `curl --resolve dev-iac.cyberfuel.com:443:192.168.1.50`
dio 404. Es una observación fechada de esta revisión, no el estado esperado tras
el despliegue. No se cambiaron esos archivos ni se activó la ruta.

El equipo de operaciones debe ejecutar preflight, despliegue y verificación con el manifiesto
candidato y la imagen autorizada; luego `activate-develop`, comprobar HTTPS y la
revisión con resolución directa y repetir mediante DNS normal. El despliegue no
requiere reiniciar otros stacks ni aplicar las actualizaciones de Ubuntu.
Los ensayos físicos y la habilitación de Ansible se realizan después de revisar
conectividad, permisos y aislamiento, manteniendo seis aprovisionamientos simultáneos.

### Evidencia de integración local — 2026-11-17

- **PASS:** `.venv/bin/python scripts/test_local_docker.py --evidence-dir /tmp/iacp-dev-validation-final-20261117`.
  Construcción de la imagen real y de su derivada de pruebas; **161 pruebas aprobadas**,
  tres advertencias de deprecación. El verificador real comprobó readiness, esquema 3,
  endpoints de servicios, las seis tablas de aplicaciones, clave Fernet legible y
  heartbeats recientes de seis workers de infraestructura y dos de servicios.
- **PASS:** `terraform validate -no-color` dentro de la imagen para los directorios
  `environments/mng01` y `environments/prx`, sin red. Esta comprobación no habilita PRX.
- **PASS:** compilación Python, Ruff F sobre los módulos revisados y `git diff --check`.
- Imagen local resultante: `sha256:0bb9ea3e3b4f1f7ccc8234f4ef8a2e314a821479ef1404b56e26e0947dba1c79`.
  Revisión declarada para la prueba: `local-uncommitted-validation`; no es un commit
  publicado ni una imagen autorizada para promoción.
- Evidencia detallada: `/tmp/iacp-dev-validation-final-20261117/summary.json`,
  `runtime.json`, `pytest.log`, `build.log` y `containers.log`.
- **BLOCKED / pendiente operativo:** emisión del token y pool exclusivos de dev,
  revisión inmutable autorizada, despliegue del candidato en el IACP Control Plane
  (VM 104), activación de la ruta y matriz física en MNG. No se emitió GO integral.

Esta evidencia supera el bloqueo anterior de acceso al daemon Docker para el alcance
local. No cambia los dictámenes históricos ni certifica la instalación por SSH en MNG.
Los contenedores y volúmenes temporales de esta ejecución se retiraron al finalizar.

### Actualización de condiciones del piloto — 2026-11-17

Esta actualización sustituye las condiciones anteriores de credenciales exclusivas
Proxmox y de aplazamiento de IPAM. El usuario autorizó reutilizar únicamente el
token y el pool `iacp-lab` del clúster MNG. El generador admite `--shared-mng` y
`--ipam-env /etc/iacp/stacks/dev.env`, y deja constancia mediante
`mng_credentials_shared=true`. API y PostgreSQL conservan credenciales distintas;
procesos, colas, bases, volúmenes y rango VMID siguen aislados de producción.

Durante el piloto la API y la consola permiten ambos perfiles:

| Perfil | Configuración administrada |
| --- | --- |
| `lab_dhcp` | DHCP sobre `vmbr0`, sin VLAN tag |
| `prod_static` | Solo `190.0.231.0/24`, gateway `190.0.231.1`, VLAN 211 y DNS `1.1.1.1`, `8.8.8.8` |

`IACP_ENFORCE_WAN_ONLY=0` mantiene esa selección, incluso con el runtime histórico
identificado como producción. No activar el bloqueo durante estas pruebas.
La promoción definitiva con servicios requiere `IACP_ENFORCE_WAN_ONLY=1`: la API
elige WAN al omitir el perfil y rechaza LAN; los workers también comprueban la
política antes de ejecutar operaciones. `prod_static` sigue perteneciendo al
entorno `lab` de MNG y no habilita Platinum/PRX.

NetBox usa el prefijo 52, VRF global. La asignación utiliza el endpoint atómico
`POST /ipam/prefixes/52/available-ips/`, con marcador de propiedad por instancia.
Una respuesta incierta se recupera buscando ese marcador; no se repite ciegamente
la reserva. La IP del gateway queda reservada y nunca se entrega a una VM.
La eliminación confirma ausencia física de la VM y propiedad/dirección de la
reserva antes de liberar. Si no puede demostrarlo, conserva la IP como
`release_pending` para recuperación controlada.

El token se configura únicamente en el host del IACP Control Plane (VM 104),
en `/etc/iacp/stacks/dev.env`, variable `IACP_NETBOX_TOKEN`; el generador lo copia
al archivo privado del candidato. No incluirlo en comandos, evidencias ni Git.
Habilitar `IACP_PROD_STATIC_ENABLED=1` solo después de validar IPAM y conectividad.
La preparación deja Ansible deshabilitado hasta verificar el stack y sus accesos.

Las pruebas locales y las evidencias históricas no certifican despliegues físicos,
inferencia real de agentes ni la promoción a producción. Sus dictámenes mantienen
el alcance y revisión registrados.

### Evidencia de cierre local del candidato — 2026-11-17

- **PASS:** `.venv/bin/python scripts/test_local_docker.py --evidence-dir /tmp/iacp-validacion-20261117-final-v2`:
  168 pruebas, tres advertencias de deprecación, esquema 3 y runtime 6+2 verificados.
- **PASS:** `node --check` para `console.js` y `docs.js`, compilación Python,
  `ruff check --select F` sobre API, workers y herramientas revisadas, y `git diff --check`.
- **PASS IPAM real:** cliente nuevo ejecutado en el IACP Control Plane (VM 104),
  reserva atómica temporal ID 2011 (`190.0.231.2/24`), eliminación y búsqueda
  posterior sin reserva propia. Gateway `190.0.231.1/24` reservado y conservado.
  La prueba no creó VMs ni consumió una IP permanentemente.
- **Pendiente:** matriz física de seis aprovisionamientos simultáneos en MNG,
  instalación SSH de aplicaciones y validación funcional con proveedores reales
  de agentes. No se declara certificación integral ni GO de producción.

### Primer ensayo dev físico — 2026-11-17

El commit `1a2b0afc5d654d33914b860b9f09c0950d9bbcef` se desplegó exclusivamente
en el IACP Control Plane (VM 104) con `iacpctl deploy --preloaded-images`.
Preflight, verificación y `activate-develop` dieron PASS; HTTPS respondió readiness
por resolución directa y DNS normal en `dev-iac.cyberfuel.com`. Esquema 3,
seis workers de infraestructura y dos de servicios. Ansible seguía deshabilitado.

El primer ensayo de seis solicitudes mezcló tres LAN y tres WAN. **FAIL WAN:**
el gateway devuelto por NetBox incluía `/24`, rechazado por la validación de
Terraform antes de crear las tres VMs WAN. Las tres reservas se liberaron
automáticamente y quedaron `released`, sin error en el ledger. Se conserva la
evidencia en `/opt/iacp/evidence/dev-1a2b0af-fisica-20261117/provision.jsonl`.
La corrección devuelve una dirección de gateway sin prefijo y añade una regresión
que verifica tanto la consulta CIDR a NetBox como el contrato entregado a Terraform.
No se interpreta este ensayo como aprobación de concurrencia física ni GO integral.

### Rama de desarrollo y acceso por IP — 2026-11-17

La siguiente versión vive en `devops/features`; `main` conserva la versión estable.
Las diferencias de dominio, imagen y políticas son configuración de despliegue.
No se debe cambiar el código de la API para alternar entre dominios.

La configuración canónica del piloto es `/etc/iacp/stacks/dev.env`, y su manifiesto
`/etc/iacp/stacks/manifest-dev.json`. El token dev es propio: copiar privadamente el
valor de `IACP_API_TOKEN` desde ese archivo al formulario Authorize. El archivo
histórico tenía otro token y no era el consumido por el stack candidato, lo que
explica el 401. Se conservó un respaldo privado antes de unificarlo.

El contrato incorpora `configuration.access_mode="ip"` para Nextcloud/n8n sin
`domain`, únicamente con `instance.network_profile="prod_static"`. La dirección
se obtiene del ledger IPAM. La respuesta del servicio incluye `access_url` y
`GET /v1/service-instances/{service_id}/tls-ca` entrega solo el certificado público
con autenticación. El modo por dominio conserva su comportamiento anterior.

El operador prepara una sola CA del piloto con `scripts/prepare_pilot_ca.py` en el
directorio privado de servicios. Los workers requieren
`IACP_SERVICE_IP_TLS_ENABLED=1`, emiten certificados con SAN de la IP y duración de
siete días, y copian solo el certificado de servidor, su clave y la CA pública al
invitado. La clave privada de la CA permanece en el Control Plane. No existe aún
renovación automática: este modo certifica el piloto y no equivale a un producto
público con TLS confiable de forma predeterminada.

Las recetas admiten Ubuntu 24.04 y AlmaLinux 9; los contenedores conservan los
mismos digests. En AlmaLinux se instala Docker desde su repositorio firmado y se
mantiene SELinux, etiquetando los bind mounts necesarios. La verificación WAN
observa interfaces, rutas y DNS del sistema operativo sin imponer Netplan a AlmaLinux.

`configuration.agent_mode="local"` permite validar Hermes/OpenClaw sin credencial
real de proveedor. El modo predeterminado `connected` sigue exigiendo modelo y
clave. La aprobación local excluye inferencia, Telegram y conexiones externas.

`scripts/wan_matrix.py` ejecuta una categoría y ciclo con 16 combinaciones y dos
controles adicionales, en tres tandas de seis. Requiere el runtime dev idle y
sin operaciones inciertas. No admite URL productiva. Conserva evidencia fuera de
workdata, valida identidad y SSH, reinicio/persistencia, entrega, eliminación e
IPAM; interrumpe la inyección al fallar. Las credenciales de aplicación se consumen
una sola vez y permanecen en memoria durante el caso, nunca en los resultados.
La clave SSH exclusiva del ensayo se conserva en el directorio privado de evidencia.

Matrices: cuatro imágenes × cuatro planes para KVM; dos aplicaciones × dos
sistemas × cuatro planes para servicios y para agentes. Completar dos ciclos
limpios de cada categoría antes de preparar una promoción; no confundir los
controles adicionales ni los ensayos fallidos con los 16 casos de aceptación.

Las recetas Docker y TLS se apoyan en la documentación oficial de
[instalación RPM de Docker](https://docs.docker.com/engine/install/centos/) y
[certificados explícitos de Caddy](https://caddyserver.com/docs/caddyfile/directives/tls).
La compatibilidad concreta con las Golden Images de AlmaLinux se determina con
la matriz física; la prueba de sintaxis de Ansible por sí sola no la certifica.

### Resultado de implementación en dev — 2026-11-17

**PASS integración piloto:** revisión `b7947f613661db368c85bea44aa2b78b7e78ab79`,
publicada en `devops/features`, desplegada exclusivamente en el IACP Control Plane
(VM 104) mediante el Compose canónico e `iacpctl`. Preflight y verificación PASS.
Configuración canónica unificada; autenticación real devolvió HTTP 200. Prueba
local Docker: 173 tests PASS, esquema 3 y pools 6+2. Caddy real validó ambas
configuraciones TLS por IP con código 0.

**PASS recuperación controlada:** las VMs WAN 40007, 40008 y 40010 se reconciliaron
por API después de corregir el payload de identidad. No se reclonaron ni cambiaron
sus IP durante la recuperación. La tanda histórica alcanzó seis operaciones
simultáneas; esto no constituye una matriz WAN aprobada.

**BLOCKED conectividad WAN:** en la VM 40008 de `mng01`, se observó por QEMU Guest
Agent la dirección `190.0.231.3/24`, ruta por `190.0.231.1`, interfaz `eth0` y SSH
escuchando en 22. La tabla de vecinos mostró `190.0.231.1 INCOMPLETE`, sin MAC;
`ping` devolvió Destination Host Unreachable y DNS agotó el tiempo. Desde el
Control Plane y la estación local no se obtuvo conexión SSH WAN. La API Proxmox
mostró `vmbr0` activo sobre `bond1`, VLAN aware y `bridge_vids=2-4094`.
Estos datos acotan el bloqueo al acceso de capa 2 al gateway; no prueban en qué
tramo concreto del bridge, uplink, trunk o gateway está la interrupción.

**PASS eliminación y liberación:** las seis VMs del segundo ensayo se apagaron
y eliminaron mediante API. Sus tres reservas WAN quedaron `released`. Evidencia
operativa: `/opt/iacp/evidence/dev-b7947f6-recovery-20261117/cleanup.json`.
La evidencia original de aprovisionamiento y las operaciones se conservaron.

**Pendiente del equipo de redes:** verificar que el gateway `190.0.231.1/24` esté
activo en VLAN 211, y comprobar el paso de dicha VLAN entre `vmbr0`, `bond1` y el
puerto/trunk del clúster MNG. No cambiar a LAN ni retirar el tag para aprobar WAN.
No se modificaron switches, firewalls, gateway, Ceph ni producción.

Se añade una comprobación de vecino del gateway antes de declarar preparada una
VM estática. Una dirección y una ruta configuradas no bastan: si no hay vecino
resuelto, el worker conserva la VM y la reserva en estado incierto con
`STATIC_GATEWAY_UNREACHABLE` para recuperación. No libera una IP con VM presente.

**Certificación integral: BLOCKED.** Las matrices de 16 casos por categoría y sus
dos ciclos no se ejecutaron mientras faltaba conectividad WAN. Ansible permanece
deshabilitado. No hay GO integral ni promoción hacia `main`.

Validación final de la protección del gateway: **174 tests PASS** mediante
`.venv/bin/python scripts/test_local_docker.py --evidence-dir /tmp/iacp-devops-local-gateway-final-20261117`,
con tres advertencias de deprecación. Ruff F, compilación Python, sintaxis JavaScript
y `git diff --check` también dieron PASS. Las direcciones `.2`, `.3` y `.4` volvieron
a aparecer disponibles en NetBox, el gateway siguió reservado y no quedaron VMs
de los ensayos. No había operaciones activas ni inciertas; seis workers idle.
