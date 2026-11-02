# Despliegue en Portainer

El manifiesto para el runtime migrado está en
`deploy/portainer/docker-compose.yml`. La API se publica bajo
`https://iac.cyberfuel.com` y mantiene el contrato `/v1`.

## Preparación

1. Confirmar que el registry `scm.cyberfuel.com` está configurado en Portainer.
2. Confirmar que existe la red Docker externa `proxy_network` y que Traefik
   está conectado a ella.
3. Crear el Stack desde `CyberFuel/iacp-controlplane`, usando la rama `develop`
   para validar la integración.
4. Seleccionar `deploy/portainer/docker-compose.yml` e introducir las variables
   de `deploy/portainer/.env.example` en Portainer.
5. Sustituir cada placeholder por un valor seguro. Codificar caracteres
   reservados de la contraseña de PostgreSQL en `IACP_DATABASE_URL`.

`IACP_PROXMOX_HOST_IP` debe ser la dirección verificada del nodo indicado por
`IACP_PROXMOX_HOSTNAME`. El Stack fija esa correspondencia para la API y los
workers. El endpoint HTTPS conserva el nombre del host para validar el
certificado.

La API requiere `IACP_API_TOKEN` y no inicia si falta. La contraseña root debe
tener al menos 12 caracteres. `IACP_RETENTION_DAYS=0` conserva el historial.
El perfil estático de NetBox permanece desactivado hasta configurar y revisar
el prefijo autorizado.

## Persistencia y workers

PostgreSQL inicia con un esquema vacío en el volumen
`iacp_controlplane_postgres`. El volumen `iacp_controlplane_workdata` contiene
solo datos de trabajo, secretos temporales y workspaces Terraform. No se
importan registros, VMs, VMID leases ni Terraform states del runtime anterior.
El Stack anterior y sus volúmenes deben conservarse durante la transición;
este manifiesto no los monta ni los modifica.

El Stack ejecuta dos workers (`worker-1` y `worker-2`) y limita las mutaciones
físicas simultáneas a dos. El allocator asigna VMID desde 40001; 40000 queda
reservado. La imagen de Terraform se prepara durante la construcción para
evitar descargar el provider en cada aprovisionamiento.

## Publicación y actualización

Para publicar una imagen de integración:

```bash
docker login scm.cyberfuel.com
IACP_IMAGE_TAG=develop ./scripts/publish-image.sh
```

En Portainer, actualizar el Stack y descargar la nueva imagen. La promoción a
producción requiere revisar el manifiesto, la imagen, la configuración y el
procedimiento vigente de `scripts/iacpctl` y `scripts/go_live.py`. No usar
`latest`, no borrar volúmenes y no ejecutar `docker compose down -v`.

## Verificación posterior al despliegue

```bash
curl --fail https://iac.cyberfuel.com/health/live
curl --fail https://iac.cyberfuel.com/health/ready
```

Después, abrir `/docs`, autorizarse con Bearer y probar `GET /v1/plans` y
`POST /v1/instances`. Swagger muestra el JSON de aprovisionamiento y permite
probarlo desde la documentación. El tráfico externo entra por Traefik en
`websecure`; la API no publica un puerto del host.

`lab_dhcp` queda habilitado por defecto. Para preparar IPAM estático, consultar
`GET /v1/network-profiles`, configurar el ID exacto del prefijo `/24`, el token
de NetBox y revisar el entorno antes de activar `IACP_PROD_STATIC_ENABLED=1`.
No activar ese perfil con un prefijo pendiente ni deshabilitar la verificación
TLS en producción.

Las operaciones físicas se siguen mediante su `operation_id`. Si una operación
queda `uncertain`, `POST /v1/instances/{instance_id}/reconcile` solicita al
worker una observación de Proxmox y del state antes de decidir el resultado.
Las operaciones stale generan una reconciliación pendiente automáticamente;
no se repite una mutación física a ciegas ni se fuerzan locks.
