# Despliegue en Portainer

Este despliegue usa `deploy/portainer/docker-compose.yml`. El
`compose.yaml` de Docker standalone permanece separado y no se modifica.

## Preparación

1. Configurar `scm.cyberfuel.com` como registry privado de lectura en Portainer.
2. Confirmar que existe la red Docker externa `proxy_network` y que Traefik
   está conectado a ella.
3. Crear el Stack desde el repositorio `CyberFuel/iacp-controlplanev2`.
4. Seleccionar `deploy/portainer/docker-compose.yml`.
5. Introducir las variables de `deploy/portainer/.env.example` en el panel de
   variables de Portainer, sustituyendo los placeholders.

`IACP_PROXMOX_HOST_IP` debe contener la IP verificada del nodo Proxmox que
corresponde a `IACP_PROXMOX_HOSTNAME`. El Stack la fija en `/etc/hosts` de la
API y de los workers para que una caída momentánea del DNS Docker no convierta
una operación física en `uncertain`; el endpoint HTTPS conserva el nombre para
validar el certificado.

El Stack exige `IACP_API_TOKEN` y activa `IACP_REQUIRE_API_TOKEN=1`; si falta el
token la API no inicia. `IACP_ROOT_PASSWORD_MIN_LENGTH` queda en 12 por defecto.
`IACP_RETENTION_DAYS=0` conserva todo el historial; si se configura un valor
mayor, solo se compactan operaciones terminales e idempotencias antiguas.

No se necesitan carpetas de código ni archivos de Terraform en el host. La
imagen contiene el código y Terraform; el volumen nombrado
`iacp_controlplanev2_data` conserva SQLite, workspaces y estados.
El Stack define seis workers estables (`worker-1` a `worker-6`) sobre el mismo
volumen y usa `IACP_MAX_CONCURRENT=6`. `IACP_VMID_REUSE=1` hace que, después de
confirmar un destroy, el allocator vuelva a elegir el menor VMID libre desde
40001; el VMID 40000 permanece reservado.
El provider certificado de Proxmox se inicializa durante el build de la imagen
y los workspaces lo reutilizan localmente, evitando una descarga por cada VM.

## Actualización rápida

Para desarrollo:

```bash
docker login scm.cyberfuel.com
IACP_IMAGE_TAG=develop ./scripts/publish-image.sh
```

Después, en Portainer, seleccionar `Update the stack`, habilitar la descarga
de la imagen y redeployar. Para una release, publicar un tag versionado y
cambiar solamente `IACP_IMAGE_TAG`.

No usar `latest`, no borrar el volumen y no ejecutar `down -v`.

## Verificación

```bash
curl --fail https://dev-iac.cyberfuel.com/health/live
curl --fail https://dev-iac.cyberfuel.com/health/ready
```

Luego abrir `/docs`, autorizarse con Bearer y probar `GET /v1/plans` seguido de
`POST /v1/instances` con un `plan_id` e `image_id`. El API no
publica un puerto del host; el único acceso externo es Traefik por
`websecure`. La consola operativa simplificada queda disponible en `/console`.

El perfil `lab_dhcp` queda habilitado por defecto. Para preparar IPAM estático,
consultar primero `GET /v1/network-profiles` y configurar el ID exacto del
prefijo `/24` en `IACP_NETBOX_PREFIX_ID`, el token en
`IACP_NETBOX_TOKEN` y luego `IACP_PROD_STATIC_ENABLED=1`. La URL base es
`https://netbox.cyberfuel.com/api`; `/api/status/` es únicamente el endpoint
de salud. El orquestador reserva `.1` como gateway, crea la IP libre con la
metadata de la instancia y la libera tras un destroy confirmado. No activar el
perfil con un prefijo pendiente ni desactivar la verificación TLS en producción.

La API recibe también las variables de conexión necesarias para consultar y
operar el estado de energía. Los workers ejecutan `start`, `shutdown` y
`reboot`, y cada acción se confirma mediante su `operation_id`.
Si una operación queda excepcionalmente en `uncertain`, se puede encolar
`POST /v1/instances/{instance_id}/reconcile` con `Idempotency-Key`; el worker
observa primero Proxmox y recupera el estado sin reiniciar la VM.
Los heartbeats ahora exponen antigüedad y estado `healthy`, `degraded`, `stale`
o `unknown`; una operación abandonada por un worker se cierra como `uncertain`
y genera automáticamente una reconciliación pendiente.
