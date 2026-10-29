# API del IACP Control Plane V2

La documentación interactiva está disponible en:

- `https://dev.iac.cyberfuel.com/docs`
- `https://dev.iac.cyberfuel.com/redoc`
- `https://dev.iac.cyberfuel.com/openapi.json`
- `https://dev.iac.cyberfuel.com/console`

En Portainer, usar el botón `Authorize` de Swagger e introducir el valor de
`IACP_API_TOKEN`. La API agrega el esquema `Bearer` automáticamente.

## Flujo mínimo de aprovisionamiento

1. `POST /v1/instances` con la especificación técnica.
2. Consultar `GET /v1/operations/{operation_id}` hasta `succeeded`.
3. Ejecutar `POST /v1/instances/{instance_id}/provision` con el
   `plan_operation_id` recibido.
4. Consultar `GET /v1/instances` y `GET /v1/instances/{instance_id}`.
5. Observar `GET /v1/events`, `GET /v1/workers` y
   `GET /v1/observability`.
6. Apagar la VM con `POST /v1/instances/{instance_id}/power/shutdown`.
7. Ejecutar `DELETE /v1/instances/{instance_id}`.

También se conservan las órdenes genéricas `POST /v1/operations` para
consumidores que necesiten enviar directamente `plan`, `apply`, `destroy` o
`power`.

## Power Lifecycle

Las rutas disponibles son:

- `GET /v1/instances/{instance_id}/power`
- `POST /v1/instances/{instance_id}/power/start`
- `POST /v1/instances/{instance_id}/power/shutdown`
- `POST /v1/instances/{instance_id}/power/reboot`

Las mutaciones se ejecutan de forma asíncrona por los workers. Si Proxmox
rechaza el token por falta de `VM.PowerMgmt`, la operación termina como
`failed` con `POWER_PERMISSION_DENIED`; nunca se reporta éxito falso.

La prueba inicial recomendada es Ubuntu 24.04, DHCP, `started=false` y un
nombre descartable. El worker toma la clave pública configurada en Portainer
cuando `ssh_public_keys` llega vacío.
