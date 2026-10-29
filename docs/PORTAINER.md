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

No se necesitan carpetas de código ni archivos de Terraform en el host. La
imagen contiene el código y Terraform; el volumen nombrado
`iacp_controlplanev2_data` conserva SQLite, workspaces y estados.

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
curl --fail https://dev.iac.cyberfuel.com/health/live
curl --fail https://dev.iac.cyberfuel.com/health/ready
```

Luego abrir `/docs`, autorizarse con Bearer y probar primero `plan`. El API no
publica un puerto del host; el único acceso externo es Traefik por
`websecure`. La consola operativa simplificada queda disponible en `/console`.

La API recibe también las variables Proxmox para consultar el estado de energía
en modo lectura. Los workers son quienes ejecutan `start`, `shutdown` y
`reboot`; si el mismo token aún no tiene `VM.PowerMgmt`, Swagger y `/console`
siguen mostrando las rutas y la operación termina con
`POWER_PERMISSION_DENIED`.
