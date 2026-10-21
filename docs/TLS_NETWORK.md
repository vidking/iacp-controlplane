# HTTPS y acceso de red

`compose.tls.yaml` es optativo y no modifica la configuración activa. Añade
443 en una IP LAN explícita, conserva `127.0.0.1:8080` y utiliza Traefik con
certificados corporativos externos. No habilita un dashboard ni monta Docker socket.
La API sigue en frontend/backend; únicamente Traefik se conecta a la red externa
`proxy_network`. PostgreSQL, Redis, migraciones y workers no publican puertos.

Infraestructura debe autorizar DNS interno `iac.cyberfuel.com`, IP LAN de escucha,
miembros de `proxy_network`, rangos LAN, IP/CIDR de salida real del backend CRM y
orígenes HTTPS exactos del CRM. Si hay NAT se autoriza el origen observado, no un
valor enviado en headers. Seguridad debe validar firewall, rutas, cadena y SAN
`iac.cyberfuel.com`, vigencia del certificado y distribución de su CA al CRM.

Mantener fuera de Git y fuera del directorio de build:

- Directorio de certificados: `fullchain.pem` y `privkey.pem` con acceso mínimo.
- Política JSON con `lan_cidrs`, `crm_source_cidrs`, `crm_origins`, listas no vacías
  autorizadas. No hay rangos ni orígenes de producción predeterminados.
- Directorio de salida exclusivo para la configuración generada, sin otros routers.
- Archivo de entorno externo con `IACP_TLS_BIND_IP`, `IACP_PROXY_NETWORK`,
  `IACP_TLS_CERTS_DIR`, `IACP_TLS_DYNAMIC_DIR` y las variables server existentes.

Preparación local, sin ejecutar Compose ni cambiar el servidor:

```sh
python3 scripts/render-tls.py --policy "$TLS_POLICY_FILE" \
  --certificates "$IACP_TLS_CERTS_DIR" --output-dir "$IACP_TLS_DYNAMIC_DIR" \
  --bind-ip "$IACP_TLS_BIND_IP"
docker compose --env-file "$IACP_ENV_FILE" -f compose.yaml \
  -f compose.server.yaml -f compose.tls.yaml config --quiet
```

El generador comprueba pares certificado/clave y valida la política; la revisión
corporativa debe comprobar SAN, cadena y caducidad. La configuración generada
contiene rutas fijas de contenedor, nunca claves privadas. La habilitación requiere
aprobación de despliegue; no basta con generar el archivo.

El router HTTPS exige el Host oficial, TLS >=1.2 y SNI válido. La allowlist utiliza
la IP de transporte: `X-Forwarded-For` no concede acceso. Una cabecera `Origin`
presente debe coincidir exactamente con la lista CRM; su ausencia es compatible
con backends servidor a servidor. CORS no autentica ni reemplaza el filtro IP.
El token de servicio y el tenant siguen siendo obligatorios en las rutas protegidas.
No habilitar forwardedHeaders insecure ni ipStrategy depth sin revisar proxies
intermedios autorizados y sus límites de confianza.

Las cabeceras se omiten del access log, salvo X-Request-ID. El listener diagnóstico
conserva las rutas HTTP durante la transición; solo se publica en loopback. Los
miembros de las redes Docker deben considerarse confiables y restringirse.

Pruebas reproducibles:

```sh
bash scripts/test-integration.sh
bash scripts/test-tls.sh
```

La segunda prueba genera certificado y clave efímeros dentro de un volumen de
pruebas, sin archivos sensibles versionados ni puertos de host. Comprueba HTTPS,
CA, 401, bloqueo de Origin, IP no autorizada con XFF falsificado y diagnóstico.
No valida DNS, firewall ni certificado corporativo reales.

Tras autorización operativa: comprobar desde LAN/CRM con la CA corporativa
`curl --cacert "$CORPORATE_CA" https://iac.cyberfuel.com/health/live` y verificar
401 sin token en `/v1/instances`; desde un origen no autorizado debe haber bloqueo.
Comprobar el diagnóstico mediante túnel/local host. Revertir quitando únicamente
el override TLS y recreando Traefik con la configuración anterior; conservar datos,
secretos y volúmenes de aplicación. No ejecutar down con eliminación de volúmenes.

Referencia:
[IPAllowList](https://doc.traefik.io/traefik/v3.1/middlewares/http/ipallowlist/),
[certificados y TLS](https://doc.traefik.io/traefik/v3.1/https/tls/),
[file provider](https://doc.traefik.io/traefik/v3.1/providers/file/).

Evidencia del 21 de octubre de 2026: 108 pruebas aprobadas en Docker (14.01 s),
configuración Compose efectiva validada con nombres externos de fixture y prueba
Traefik v3.1 completada: HTTPS/CA, 401, Origin, filtro IP con XFF falsificado y
listener diagnóstico. Las claves se generaron durante la prueba, sin versionarlas.
