# Promoción operativa del MVP

El hito **todavía no está completado**. La [evidencia real](validation/GO_LIVE_ENVIRONMENT.json)
registra esquema 0004, API/TF Worker anteriores, ausencia de Lifecycle Worker,
DNS público hacia una IP privada y ningún listener 443. Este cambio entrega el
procedimiento reproducible para cerrar esos gates; no acredita un despliegue ni
un encendido empresarial. Fuente: `origin/main` en `622d607649bcfb6ed179d78246c8e8df50c900c8`.

La lectura real de VM 106 y Wasabi no es la certificación offline de PR #20.
Se conservan los pins IaC, la revisión histórica, el intento 4 y la aprobación
consumida. [KVM_POWER_PROMOTION.md](KVM_POWER_PROMOTION.md) sigue definiendo el
contrato del certificador. No se ejecutan Apply, Import, Destroy ni escrituras de
state durante certificación o energía. No se habilita LXD.

## Preparar una release revisable

El reconciliador observado es Compose CLI. Conservarlo como único dueño; no
activar Portainer en paralelo. Trabajar en el directorio existente y mantener
todos los overrides activos, incluidos los del sitio/CA/egress que aparezcan en
las etiquetas de los contenedores. `go_live.py` rechaza omitir un archivo activo.
No recrea DB/Redis/Traefik durante la promoción de aplicaciones.

Después de revisar e integrar este PR, fijar el commit **de este cambio** en
`IACP_RELEASE` y una imagen Terraform correspondiente en `IACP_TF_WORKER_IMAGE`.
Las imágenes 622d607 ya construidas no contienen la protección nueva de Swagger,
admisión o corrección de permisos. No reconstruirlas con el mismo tag.

```sh
cd /opt/iacp/releases/controlplane-v1
export IACP_RUNTIME_ENV=/etc/iacp/server.env
export COMPOSE_FILE=compose.yaml:compose.server.yaml:compose.terraform.yaml:compose.tenants.yaml:compose.lifecycle.yaml:compose.promotion.yaml
# Anexar overrides de sitio/CA/egress existentes. No cargar compose.dev.yaml.
dc() { docker compose --env-file "$IACP_RUNTIME_ENV" -p iacp-controlplane "$@"; }
dc config --quiet
# Solo después de fijar release y fuente en server.env, construir las imágenes nuevas:
dc build api worker migrate terraform-worker lifecycle-worker
dc images
docker image inspect "iacp/controlplane-api:$IACP_RELEASE" --format '{{.Id}}'
docker image inspect "$IACP_TF_WORKER_IMAGE" --format '{{.Id}}'
```

Registrar IDs de imagen `sha256:...` locales completos en el manifest. Para un
registro, conservar además `image@sha256:...` en el registro de release. Los IDs
efectivamente ejecutados se vuelven a comprobar después de recrear servicios.
Actualizar `IACP_TF_SOURCE_PATH` al clon ya verificado
`/opt/iacp/sources/iac-proxmox-power-9aeb8ec`; no alterar los planes históricos.
Mantener Apply=1 y Plan=1. Incorporar `IACP_KVM_CREDENTIALS_PATH` al token dedicado.
Conceder **durante el cambio aprobado** lectura al UID/GID 10001 sin permiso
mundial; el archivo observado era root:root 0600. No copiar ese token a Git ni
reutilizar el token Terraform. La API y Lifecycle necesitan verificar TLS PVE.
Si se requiere CA privada, conservar el override público de CA readonly en API,
Lifecycle y certificador, y las variables de CA documentadas en PR #20.

Crear un manifest privado desde [el ejemplo](deployment/manifest.example.json).
Usar rutas absolutas para evidencia y cambios fuera de Git. Verificar que los
secretos, allocations, fuente y volúmenes corresponden al runtime. Los flags se
persisten atómicamente en `server.env`; el driver elimina overrides del ambiente
del shell y usa siempre el socket Docker local. No imprime secretos, config de
Compose, planes ni state. Sus archivos de evidencia son 0600 y no sobrescriben
informes previos. Un lock local excluye dos promociones simultáneas.

El [registro de cambio](deployment/change.example.json) debe proceder del proceso
corporativo, incluir ventana UTC, actor, release y acciones concretas, y tener su
SHA-256 fijado en el manifest. Es una **atestación del operador**, no una firma
digital ni un sistema de aprobaciones nuevo. Custodiar archivo/manifest con
permisos de administrador y revisión independiente; quien controla Docker/root
ya tiene privilegios de administración. La automatización no puede sustituir
la aprobación empresarial o demostrar que no existen escritores externos.

Acciones reconocidas: `deploy`, `publish-api`, `validate-certificate`,
`record-certificate`, `activate-lifecycle`, `first-start`, `functional-rollback`.
Autorizar únicamente las fases revisadas. Los ejemplos tienen approved=false,
controles false y ventana vencida deliberadamente.

El backup ya restaurado se comprueba por checksum; **no repetir la restauración**.
Encapsular el informe aprobado existente con el formato de
[restore-evidence](deployment/restore-evidence.example.json), manteniendo su
referencia original y digest. No convertir una simulación en evidencia real.
Si hubo escrituras después del respaldo, evaluar su vigencia en el cambio y
tomar un respaldo adicional conforme a la política de recuperación.

## Secuencia automatizada

Ejecutar con el usuario que administra el stack y lee sus secretos. Cada fase
recibe un directorio de evidencia **nuevo**. Los comandos siguientes son para la
ventana autorizada; no se ejecutaron en VM 104 durante este desarrollo.

```sh
export MANIFEST=/etc/iacp/changes/go-live-manifest.json
export EVIDENCE=/opt/iacp/evidence/change-IDENTIFICADOR
python3 scripts/go_live.py inventory --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/inventory"
python3 scripts/go_live.py preflight --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/preflight"
python3 scripts/go_live.py promote --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/promote"
python3 scripts/go_live.py verify --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/verify"
python3 scripts/go_live.py expectations --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/expectations"
python3 scripts/go_live.py validate-certificate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/validate"
```

`inventory` no drena ni migra; lee DB/broker y comprueba disponibilidad/inactividad
de los workers observados. `preflight` valida host, checkout limpio, imágenes,
overrides, volúmenes existentes, secretos legibles, aislamiento, pin/checksum IaC,
backup y evidencia de restore. No publica tareas. La inspección falla si faltan
workers o respuestas Celery; no interpreta un timeout como cola vacía.

`promote` exige el cambio vigente, cierra admisión deteniendo API, espera hasta
cinco minutos a que terminen trabajos/colas, y detiene los workers ya drenados.
Inspecciona también operaciones uncertain, aprobaciones sin consumir, jobs,
reservas Celery, colas de prioridad y unacked. No cancela ni corrige operaciones.
Una cola externa o un worker inesperado bloquea la promoción.

El servicio migrate recorre 0005 intacta y 0006. Después se levantan únicamente
API, worker convencional, Terraform Worker y Lifecycle Worker. Se verifican los
IDs reales de imagen, mounts, flags Apply=1/Lifecycle=0/admisión=0, pins, Terraform
1.16.6, readiness, esquema 0006 y las tres colas con sus workers. Los hashes y
cantidades de **todas las filas históricas** deben ser idénticos antes/después.
La API queda disponible para lecturas autenticadas y rechaza mutaciones con 503.

Alembic y Compose son idempotentes. Un fallo deja la admisión cerrada. Si se
interrumpe la promoción, revisar su journal y repetir `promote` con evidencia
nueva, misma release y ventana vigente; el snapshot también funciona mediante
un contenedor de mantenimiento cuando los workers están detenidos. No se inicia
un worker antiguo para resolver una interrupción. Un trabajo no terminal exige
su procedimiento de recuperación antes de proseguir.

Revisar expectations contra tenant, intento, aprobación, PVE y Wasabi. El
certificador usa sus propios controles/locks y verifica nuevamente source,
artefactos, plan offline no-op, provider 0.116.1, identidad y dos lecturas de state.
No copia el contenido de state al informe. `expectations` exige stopped y que el
token no tenga VM.PowerMgmt en VM 104. No fuerza hashes o seriales históricos.

Fijar en `certification_review` el archivo `*-certificate.json` de la validación
readonly, su checksum y la aprobación de registro. Luego:

```sh
python3 scripts/go_live.py record-certificate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/record"
```

Debe producir validated=true, recorded=true, ID y evento power_ownership_certified.
El registro repite toda la certificación y es idempotente. Conservar el informe
original y fijarlo en `recorded_certificate`. La certificación no habilita energía.

## Publicación y acceso de desarrollo

Seguir [la matriz y política pública](GO_LIVE_SECURITY.md). Inspeccionar primero
la PKI corporativa: en VM 104 no se encontró un mecanismo TLS/ACME activo ni
certificados montados en Traefik. El camino preparado usa fullchain.pem y
privkey.pem externos. Elegir emisión/renovación corporativa con Infraestructura;
no iniciar ACME ni otro reverse proxy automáticamente.

La política pública requiere los tres campos de [TLS_NETWORK.md](TLS_NETWORK.md)
y `admin_forward_auth_url`, URL HTTPS del gateway corporativo autorizado. El
gateway debe validar usuarios/grupos administrativos/MFA; devuelve 2xx solo tras
autorizarlos y 401/403 o redirect de login para los demás. Solo recibe Cookie,
construye contexto desde headers saneados y no recibe el Bearer IACP. Revisar ese
contrato con el dueño de identidad. Su CA debe ser confiable por Traefik; si es
privada, agregar `tls.ca` en la configuración revisada y montar únicamente su CA
pública readonly. No convertir Authorization ni headers de usuario en identidad
IACP. Sin un gateway confirmado no aprobar publicación administrativa.

```sh
python3 scripts/render-tls.py --public --policy /etc/iacp/tls/policy.json \
  --certificates /etc/iacp/tls/certs --output-dir /etc/iacp/tls/dynamic \
  --bind-ip 192.168.1.50
# Añadir --ca-file /ruta/raiz-corporativa.pem si corresponde.
```

El renderer verifica par, hostname, cadena y al menos 14 días de vigencia. Los
routers solo aceptan el hostname oficial; 80 redirige a 443. No crear un router
para scm.cyberfuel.com. Mantener el directorio dinámico exclusivo para iacp.yml.
Inspeccionar la versión de Traefik y su soporte/correcciones de seguridad; la
versión 3.1 observada **no constituye aprobación de exposición pública**. Fijar
una imagen revisada `IACP_TRAEFIK_IMAGE=traefik@sha256:...` sin añadir un servicio.

Agregar compose.tls.yaml y compose.public.yaml al manifest/COMPOSE_FILE sin quitar
los demás; configurar bind IP, proxy_network y directorios TLS en server.env.
Fijar checksum del iacp.yml generado y el ID local de la imagen proxy. Completar
[publication-review](deployment/publication-review.example.json) desde los
controles de DNS público, firewall/NAT, identidad y versión del proxy.

```sh
python3 scripts/go_live.py publish --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/publish"
```

Se recrea solo el servicio Traefik existente. La publicación exige esquema nuevo,
admisión/Lifecycle en 0 y controles aprobados. No afirma accesibilidad externa.
Desde un runner **fuera de LAN/VPN** autorizado, con token reader/operator del
tenant y Cookie de sesión del gateway en archivos 0600:

```sh
python3 scripts/smoke_public.py --external-vantage --vantage-id RUNNER-EXTERNO \
  --expected-public-ip IP_PUBLICA_VALIDADA_POR_INFRA \
  --token-file /ruta/tenant.token --gateway-cookie-file /ruta/gateway.cookie \
  --output external-probe.json
# Añadir --other-tenant-token-file y --operation-id para pruebas de lectura adicionales.
```

El probe no muta: valida todas las direcciones DNS contra las IP públicas
aprobadas, TCP/TLS/SAN/cadena/vigencia, redirect, rechazo anónimo, identidad
doble para docs, readiness, endpoints y guest_ready=null. No sigue redirects
con secretos ni usa proxies del ambiente. `--external-vantage` declara la
ubicación; conservar evidencia independiente del runner. Una prueba desde este
equipo o VM 104 no acredita una red externa. Programar este probe y el control
de caducidad en el monitor corporativo; alertar antes de 14 días.

En navegador: completar login/MFA del gateway, abrir `https://iac.cyberfuel.com/docs`,
responder al challenge con usuario `iacp` y token del tenant como contraseña.
Swagger carga OpenAPI con esa identidad; para **Try it out**, usar Authorize →
HTTPBearer e ingresar el mismo token sin el prefijo Bearer. La autorización no
se conserva entre recargas. El HTTP Basic de docs no autoriza endpoints /v1.
Cerrar sesión del gateway y el contexto del navegador al terminar.

## Activación y primer start controlado

Completar [activation-review](deployment/activation-review.example.json): IPAM
debe validar la IP pública **asignada a VM 106** independientemente de la IP de
publicación de la API. La configuración observada `190.0.230.162/24` por sí sola
no prueba asignación/rutas/ausencia de conflicto. Adjuntar informe externo real
reciente y checksum. Verificar RBAC efectivo, aislamiento y exclusión de 104.
La activación requiere acciones validate-certificate y activate-lifecycle en la
ventana: vuelve a validar certificación y comprueba su ID en la DB operativa.

```sh
python3 scripts/go_live.py activate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/activate"
# SOLO con first-start aprobado, token operator, controles e intención revisados:
python3 scripts/go_live.py first-start --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/start"
```

`activate` persiste Apply=1/Lifecycle=1/admisión=1 y recrea API/Lifecycle Worker.
`first-start` conserva una única clave y un archivo de intención privado antes
del POST IACP. Exige stopped, mutations_available=true y guest_ready=null. Lee
Wasabi antes/después, sigue GET operation hasta terminal, exige exactamente un
power_completed, running, historia original y state invariantes. Un replay usa
esa misma clave e intención. Una respuesta perdida/timeout/uncertain requiere
investigación; no generar otra clave, apagar para compensar o enviar comandos
directos a Proxmox. Esta prueba **no se ejecutó** durante el desarrollo.

Running acredita energía, no SO, QEMU Guest Agent, Cloud-Init ni acceso de red.
La opción agent configurada en PVE no demuestra agente operativo. guest_ready
sigue null; la Golden Image se certifica en su frente correspondiente.

## Rollback y recuperación

```sh
python3 scripts/go_live.py rollback --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/rollback"
```

Exige functional-rollback aprobado. Persiste admisión=0/Lifecycle=0/Apply=1,
recrea API para cerrar nuevas mutaciones, espera workers sin tareas activas y
recrea Lifecycle con 0. Conserva esquema 0006, certificados, auditoría y datos.
Si el drenado falla, API queda cerrada; revisar la tarea y repetir la fase. Los
UPID conocidos continúan observándose con Lifecycle=0; recuperar por redelivery
o replay de la misma clave. Los uncertain sin UPID conservan su reserva; no
resolver mediante SQL, otro start, stop/reset/force ni un Apply adicional.

Nunca usar downgrade 0005/0006, down -v, reset de state o restauración sobre la
DB viva como rollback. No ejecutar PR #18/#19 sobre head 0006. Ante migración
interrumpida, inspeccionar alembic current y reejecutar el servicio migrate desde
la misma release compatible dentro de la ventana; Alembic conserva su control
transaccional. No marcar revisiones manualmente. Restauraciones ante desastre
son un cambio separado, primero aisladas y bajo el runbook de backups.

Si una publicación defectuosa exige cerrar 443, revertir solo Traefik con la
configuración anterior archivada mediante cambio aprobado, conservando su
listener loopback y todos los servicios/datos. No quitar overrides de aplicación
ni retirar las protecciones de autenticación de docs. Un certificado próximo a
caducar exige renovación del par/cadena por su mecanismo corporativo y nuevo
probe; nunca desactivar verificación TLS.

Las pruebas y sus límites se registran en [GO_LIVE_VALIDATION.md](GO_LIVE_VALIDATION.md).
