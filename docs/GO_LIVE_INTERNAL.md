# Go-live corporate-internal en VM 104

IACP es una API corporativa privada: `https://iac.cyberfuel.com` resuelve por DNS
corporativo a **192.168.1.50**, Control Plane VM 104. No se exige IP global, DNS
público ni NAT WAN para esta API. **190.0.230.162 es la IP WAN de laboratorio de
VM 106**, una asignación independiente que sigue requiriendo validación IPAM,
identidad, redes, certificación histórica y autorización antes de energía.

Este correctivo parte de `origin/main` en `b16374f78a388aac68139487103622e7768d7a8d`
(PR #21). Reutiliza `go_live.py`, el probe existente, los mismos servicios Compose
que ya conservan los datos y el Traefik existente. No modifica código de la API,
pins Terraform, migraciones 0005/0006, postcondiciones ni el certificador. Las
imágenes b16374f construidas/verificadas pueden conservarse con sus IDs aprobados;
no reconstruir sus tags. El checkout/manifest de **automatización** debe fijar
el commit correctivo revisado; `IACP_RELEASE` puede seguir identificando la imagen
b16374f. El registro de cambio/release debe identificar ambos y sus digests.

## Preparar controles y configuración, sin desplegar

Copiar [manifest interno](deployment/manifest.corporate-internal.example.json) a
un archivo privado fuera de Git, completar rutas/digests reales y conservar todos
los overrides activos del sitio/CA/egress. `exposure_mode=corporate-internal`
requiere `api_bind_ip=192.168.1.50`, CIDRs LAN/VPN específicos aprobados e
`identity_mode`. Los manifiestos anteriores sin exposure_mode conservan `public`;
un valor desconocido falla. No basta con cambiar el perfil de un informe público.

Seleccionar explícitamente una política:

- `gateway`: gateway HTTPS corporativo con sus reglas/grupos/MFA aprobados; solo
  recibe Cookie y no el Bearer IACP. El probe requiere una sesión válida.
- `approved-internal-policy`: solo tras aprobación equivalente de Seguridad:
  allowlist de transporte LAN/VPN, acceso de red restringido, Bearer por tenant/
  rol y challenge de docs. No incluye gateway/MFA por defecto. La ausencia de
  gateway no constituye una aprobación. Registrar dueño, alcance, rotación de
  tokens y referencia de política; no emitir tokens anónimos.

La política de generación TLS conserva `lan_cidrs`, `crm_source_cidrs` y
`crm_origins`, con listas reales aprobadas; agrega `identity_mode`. Con gateway
requiere además `admin_forward_auth_url` HTTPS. Sin gateway ese campo se omite.
La unión de LAN/CRM, sin duplicados y en el mismo orden, debe coincidir con
`approved_client_cidrs` del manifest. No usar 0.0.0.0/0 ni CIDRs WAN; se admiten
RFC1918 y rangos privados de VPN. Cada router HTTPS y la redirección HTTP quedan
sujetos a esa allowlist; X-Forwarded-For no concede acceso. Swagger/OpenAPI siempre
requieren token IACP, aun en la LAN. CORS no sustituye identidad.

Infraestructura debe proporcionar fullchain.pem y privkey.pem válidos para
`iac.cyberfuel.com`, mediante su PKI existente o DNS-01 en su proceso corporativo.
DNS-01 no necesita abrir puertos WAN. No se inicia otro emisor ACME ni se copian
credenciales DNS a la API o workers. Un certificado público no vuelve pública
la API. Si hay CA privada, distribuir únicamente su raíz pública aprobada al
trust store/cliente y configurar api_ca_file; nunca -k ni insecureSkipVerify.

Comandos para el operador, **durante preparación/cambio autorizado**:

```sh
cd /opt/iacp/releases/controlplane-v1
export MANIFEST=/etc/iacp/changes/go-live-internal.json
export COMPOSE_FILE=compose.yaml:compose.server.yaml:compose.terraform.yaml:compose.tenants.yaml:compose.lifecycle.yaml:compose.promotion.yaml:compose.tls.yaml:compose.public.yaml
export IACP_RUNTIME_ENV=/etc/iacp/server.env
# Anexar overrides activos de CA/egress/sitio en ambos COMPOSE_FILE y manifest.
dc() { docker compose --env-file "$IACP_RUNTIME_ENV" -p iacp-controlplane "$@"; }
python3 scripts/render-tls.py --corporate-internal --policy /etc/iacp/tls/policy.json \
  --certificates /etc/iacp/tls/certs --output-dir /etc/iacp/tls/dynamic \
  --bind-ip 192.168.1.50
# Si corresponde, añadir --ca-file /etc/iacp/tls/corporate-root.pem.
dc --profile certification config --quiet
```

`compose.public.yaml` conserva su nombre por compatibilidad y sirve también al
perfil interno: reutiliza la configuración estática endurecida public.yml y agrega
80 para redirect. **compose.tls.yaml es el que publica 192.168.1.50:443:443**;
ambos overrides son obligatorios. En server.env fijar IACP_TLS_BIND_IP=192.168.1.50,
directorios externos TLS, proxy_network existente e imagen Traefik aprobada fijada
por digest. No se publican otros servicios. 8080 continúa en 127.0.0.1. No hay
Docker provider/socket ni dashboard. El driver rechaza wildcard, IP diferente,
puertos extra o diagnóstico en LAN, y contrasta los bindings realmente ejecutados.

Completar [publication-review interno](deployment/publication-review.corporate-internal.example.json)
y fijar su SHA-256 en el manifest. Las aprobaciones de DNS, firewall de LAN/VPN,
ausencia de NAT público/exposición WAN, política de identidad y versión del proxy
son **atestaciones del proceso corporativo**; no se deducen del bind privado.
Adjuntar referencias reales a reglas/perímetro y revisión de acceso. Confirmar que
un NAT con SNAT no presenta tráfico WAN como un origen LAN autorizado. No marcar
controles true sin revisión. Los ejemplos son deliberadamente false.

## Secretos UID 10001

Preflight comprueba dueño/grupo/modo y archivos no vacíos, sin lectura mundial.
`verify` comprueba el UID efectivo 10001 y abre un byte de cada secreto montado
sin imprimir contenido. Conserva las rutas y secretos existentes. Para el secreto
Lifecycle root:root 0600, un ajuste mínimo posible **solo en el cambio aprobado**:

```sh
sudo chgrp 10001 /etc/iacp/lifecycle/kvm_credentials.json
sudo chmod 0440 /etc/iacp/lifecycle/kvm_credentials.json
# Comprobar metadatos; no imprimir el secreto:
stat -c '%u:%g %a %n' /etc/iacp/lifecycle/kvm_credentials.json
```

Para cada rol pueden comprobarse lecturas sin mutar DB/colas mediante un
contenedor de mantenimiento con su configuración revisada; no ejecutarlo como
root para ocultar problemas de permisos:

```sh
dc run --rm --no-deps -T --entrypoint python lifecycle-worker - <<'PY'
import os
assert os.geteuid() == 10001
for path in ('/run/secrets/db_password', '/run/secrets/kvm_credentials'):
    with open(path, 'rb') as stream:
        assert stream.read(1)
print('UID10001_SECRET_READS_OK')
PY
```

No usar 0644/0777 ni el token Terraform en Lifecycle. El certificador continúa
siendo mantenimiento explícito, con planes readonly y ambos secretos solo allí.

## Ejecución por fases y cliente LAN/VPN

Usar las mismas aprobaciones, backup/restore ya acreditados, drenado y conservación
histórica de [GO_LIVE_MVP.md](GO_LIVE_MVP.md). No repetir backup/restore aprobado
sin un motivo de vigencia. Cada directorio de evidencia debe ser nuevo:

```sh
export EVIDENCE=/opt/iacp/evidence/CAMBIO-INTERNO-IDENTIFICADOR
python3 scripts/go_live.py preflight --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/preflight"
# Solo ventana aprobada, acción deploy:
python3 scripts/go_live.py promote --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/promote"
python3 scripts/go_live.py verify --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/verify"
# Solo acción publish-api aprobada; mantiene Apply=1, admisión=0 y Lifecycle=0:
python3 scripts/go_live.py publish --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/publish"
```

Publish verifica DNS corporativo exacto, revisión de red/identidad, bindings,
allowlist, middlewares y TLS; después recrea únicamente el Traefik existente y
comprueba handshake con SNI/cadena. No certifica VM 106 ni habilita energía.

Desde **otro cliente autorizado de LAN/VPN**, con manifest revisado, token del
tenant y CA corporativa cuando corresponda (archivos de credenciales 0600):

```sh
python3 scripts/smoke_public.py --manifest /ruta/go-live-internal.json \
  --lan-vpn-vantage --vantage-id CLIENTE-LAN-VPN-IDENTIFICADO \
  --token-file /ruta/tenant.token --ca-file /ruta/corporate-root.pem \
  --output lan-vpn-probe.json
# Con identity_mode=gateway agregar --gateway-cookie-file /ruta/gateway.cookie.
# Con CA del trust store se omite --ca-file.
```

El nombre del script se conserva; ya admite ambos perfiles. Comprueba DNS exacto,
origen local dentro de CIDRs aprobados, TLS/SAN/cadena/vigencia >=14 días, redirect,
rechazo anónimo, docs autorizados, readiness y API de lectura. No publica jobs ni
energía. La ubicación LAN/VPN es declarada por el operador y debe respaldarse con
la identidad del cliente; el probe no prueba por sí solo que no existe un NAT WAN.
No ejecutar desde VM 104 y presentarlo como prueba de otro cliente. Copiar el
informe real a custodia privada y registrar su checksum; no editar passed/checks.

Para activación, partir del ejemplo de activation-review existente, **reemplazar**
external_https_passed por lan_vpn_https_passed y external_probe por lan_vpn_probe
(path + sha256 del informe anterior). Inicializar los controles a false y aprobarlos
solo con evidencia. Conservar assigned_public_ip_validated, assigned_public_ip
**190.0.230.162 de VM 106**, controles PVE, exclusión de 104 y seguridad. No cambiar
esa comprobación a 192.168.1.50 ni certificar automáticamente.

```sh
python3 scripts/go_live.py expectations --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/expectations"
python3 scripts/go_live.py validate-certificate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/validate"
# Revisar evidencia readonly, fijar certification_review y aprobar registro:
python3 scripts/go_live.py record-certificate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/record"
# Fijar recorded_certificate y activation_review aprobados antes de continuar:
python3 scripts/go_live.py activate --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/activate"
# SOLO con autorización independiente first-start, clave e intención conservadas:
python3 scripts/go_live.py first-start --manifest "$MANIFEST" --evidence-dir "$EVIDENCE/start"
```

Activate/first-start exigen un probe **del mismo perfil** reciente (<24 h), revisión
de red/identidad, DNS y bindings internos vigentes. Permanece la certificación
histórica de PR #20, el vínculo y la verificación independiente de la IP WAN de
VM 106. No modifica el state ni sus postcondiciones. guest_ready permanece null.

Rollback conserva exactamente el flujo existente, sin exigir que TLS esté sano
para cerrar admisión: `python3 scripts/go_live.py rollback --manifest "$MANIFEST"
--evidence-dir "$EVIDENCE/rollback"`, con aprobación functional-rollback. Mantiene
Apply=1, esquema 0006, certificados/historia y observación de tareas conocidas;
no downgrade, down -v, SQL de reparación o segundo comando ante incertidumbre.

El perfil público y sus comandos anteriores continúan requiriendo DNS/IP global
y runner externo. No se fabricaron aprobaciones, certificados, probes LAN/WAN ni
certificaciones reales en este correctivo; no se ejecutó ningún cambio de producción.
