# Seguridad y exposición pública

La API oficial es `https://iac.cyberfuel.com`. Forgejo `scm.cyberfuel.com` no
participa en estos routers ni se modifica. La publicación requiere controles
corporativos pendientes; consultar [el runbook](GO_LIVE_MVP.md).

| Superficie | Exposición propuesta | Control y comprobación |
|---|---|---|
| TCP 443 | Traefik existente, IP LAN explícita detrás de NAT/firewall autorizado | Host/SNI oficial, TLS >=1.2, certificado con cadena/SAN/vigencia comprobados |
| TCP 80 | Solo redirección del hostname oficial a HTTPS | No recibe credenciales; otros hostname no tienen router |
| TCP 8080 | 127.0.0.1 del host, diagnóstico existente | No dashboard; acceso por host/túnel administrativo autorizado |
| API 8000 | Solo redes Docker frontend/backend/egress autorizadas | Sin puerto host; /v1 usa Bearer/tenant/RBAC, docs exige token |
| PostgreSQL 5432 | Solo backend interno | Volumen externo original, secreto por archivo; sin puerto host |
| Redis 6379 | Solo backend interno | Volumen original, sin puerto host; proteger miembros de esta red |
| Workers | Sin puertos host | Colas celery/terraform/lifecycle separadas, concurrencia 1 |
| Lifecycle Worker | backend + egress PVE | DB + token Lifecycle; sin Wasabi, Terraform, planes o socket Docker |
| Terraform Worker | backend + egress PVE/Wasabi | Fuente readonly, planes originales, credencial Terraform; sin token API |
| Certificador | Perfil certification, una ejecución del operador | Ambos secretos solo durante mantenimiento, planes readonly, no servicio permanente |
| Docker API 2375/2376/socket | No expuesta a contenedores ni a Internet | Administración desde host; no montar docker.sock en Traefik |
| Proxmox/Wasabi | Egress desde roles autorizados | TLS verificado; aislamiento y reglas de salida revisadas con Infraestructura |

El Traefik observado utiliza **solo file provider**: Docker provider está
deshabilitado, no existe descubrimiento automático ni socket Docker. Por ello
`exposedByDefault` no aplica al runtime actual; no se declara falsamente haber
observado el valor false. La exposición es una lista explícita de routers/services
en iacp.yml. Si el sitio incorpora un Docker provider en un cambio futuro, debe
configurar **exposedByDefault=false** explícitamente y optar cada servicio con
traefik.enable=true; ese cambio requiere nueva revisión. No habilitar el provider
solo para agregar una propiedad que no afecta a file provider.

En el camino público hay dos rutas: el backend CRM desde su IP/CIDR observado
puede consumir únicamente inventario/creación, power y seguimiento de operaciones;
las demás rutas y las solicitudes desde cualquier otra ubicación pasan por el
gateway corporativo. La selección CRM usa ClientIP del transporte, sin confiar
en X-Forwarded-For. Si un proxy/NAT oculta todas las fuentes bajo una IP compartida,
**no** autorizarla como CIDR CRM; revisar la topología y confiar solo en proxies
específicos mediante un cambio nuevo. La configuración predeterminada no confía
en headers reenviados y Uvicorn los ignora.

El gateway debe autorizar grupos administrativos/MFA y rechazar al público; una
sesión autenticada sin el grupo adecuado también debe ser rechazada. IACP valida
por separado el Bearer, tenant activo y rol. Headers de identidad no conceden
privilegios. Swagger/OpenAPI requieren además token IACP: navegador con challenge
Basic `iacp:<token>` o cliente con Bearer. No se publica ReDoc ni otro endpoint
administrativo. Las operaciones /v1 siguen aceptando exclusivamente Bearer.

| Contrato | CRM | Operador privilegiado |
|---|---|---|
| GET /v1/instances y /{id} | reader/operator/approver, solo su tenant | Igual alcance; no lectura global |
| POST /v1/instances | operator, clave obligatoria; crea operación/plan | Igual; Apply tiene autorización separada |
| GET /v1/instances/{id}/power | Roles de lectura del tenant | Observación PVE; guest_ready=null |
| POST power/start, shutdown, reboot | Solo operator del tenant si el proceso de negocio está autorizado | Contrato/certificado, flags, identidad, locks y clave obligatorios |
| GET /v1/operations/{id} | Roles de lectura del tenant | Seguimiento asíncrono sin acceso directo PVE |
| /docs y /openapi.json | Acceso humano aprobado por gateway + token; sin ruta CRM directa | Mismos controles; Swagger no concede privilegios |
| /v1/instances/{id}/terraform/... | Fuera de la ruta de servicio CRM | Gateway + operator/approver según endpoint, sujeto independiente, plan exacto y aprobaciones existentes |
| /v1/jobs/... | Fuera de la ruta de servicio CRM | Gateway + token/rol del contrato; smoke solo prueba cola/DB |
| Registro de certificados y tenants | Sin endpoint HTTP | Mantenimiento explícito en host y ventana/cambio aprobado |

Los roles actuales son reader/operator/approver; no existe RBAC granular por
acción de energía. Otorgar operator al CRM permite start/shutdown/reboot dentro
de su tenant, sujeto a los demás controles. Si negocio requiere limitarlo solo a
start, cerrar ese gate de autorización antes de emitir la credencial; no prometer
un scope que el código no implementa. Certificar VM 106 no cambia su revisión de
aprovisionamiento. VM 104 se rechaza además por código y constraint DB.

Otro tenant recibe 404 en recursos; reader no muta, approver no ejecuta Apply ni
energía. Nunca habilitar un token anónimo. El token bootstrap continúa limitado
al tenant bootstrap por compatibilidad; para producción distribuir identidades
de servicio con sujeto explícito y rotación, usando el registro existente.

CORS permite únicamente orígenes HTTPS corporativos aprobados y el propio
`https://iac.cyberfuel.com` para Swagger. Un Origin presente ajeno no tiene router;
un backend puede no enviar Origin. El preflight OPTIONS no ejecuta operaciones.
CORS, HTTPS, allowlist y Swagger no sustituyen autenticación/autorización.

Límites públicos: 64 KiB de cuerpo (413), 40 solicitudes concurrentes y promedio
10/s con burst 20 por IP de transporte (429). No se reintentan requests mutables
en Traefik. Ajustar esos límites en un cambio revisado conforme al tráfico real.
Se añaden HSTS un año, nosniff, frame deny, referrer-policy no-referrer y
Cache-Control no-store en API. API genera un X-Request-ID nuevo y lo incluye en
logs estructurados con método, plantilla de ruta, status y duración; no registra
headers, tokens, cookies, cuerpos ni query strings. Traefik registra router,
status, método y duración sin URL/query/headers; correlacionar operaciones por
request_id en API y operation_id/eventos persistidos. La red backend compartida
sigue siendo un límite de confianza: no incorporar contenedores ajenos.

La API PVE devuelve privilegios efectivos como mapa de nombre → flag de
propagación. Un nombre con valor 0 concede acceso al path consultado; no permite
inferir privilegios en descendientes. Se admite 0/1 válidos en `/vms/106` y se
rechazan ausencias/valores inválidos. La comprobación de ámbito `/vms` usada por
reconciliación Terraform conserva su requisito de propagación=1 para demostrar
visibilidad sobre descendientes. Referencias primarias:
[API permissions](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm),
[RPCEnvironment](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/RPCEnvironment.pm).

Referencia de los middlewares utilizados:
[ForwardAuth](https://doc.traefik.io/traefik/v3.1/middlewares/http/forwardauth/),
[Buffering](https://doc.traefik.io/traefik/v3.1/middlewares/http/buffering/),
[RateLimit](https://doc.traefik.io/traefik/v3.1/middlewares/http/ratelimit/).
Las pruebas locales con Traefik 3.1 verifican su comportamiento funcional; no
certifican soporte de seguridad ni la versión finalmente aprobada para Internet.
