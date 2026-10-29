# API del IACP Control Plane V2

La documentación interactiva está disponible en:

- `https://dev.iac.cyberfuel.com/docs`
- `https://dev.iac.cyberfuel.com/redoc`
- `https://dev.iac.cyberfuel.com/openapi.json`

En Portainer, usar el botón `Authorize` de Swagger e introducir el valor de
`IACP_API_TOKEN`. La API agrega el esquema `Bearer` automáticamente.

## Flujo mínimo

1. `POST /v1/operations` con `command=plan` y `action=provision`.
2. Consultar `GET /v1/operations/{operation_id}` hasta `succeeded`.
3. Ejecutar `POST /v1/operations` con `command=apply` y el
   `plan_operation_id` recibido.
4. Observar `GET /v1/events`, `GET /v1/workers` y
   `GET /v1/observability`.
5. Ejecutar `destroy` con el mismo `plan_operation_id`.

La prueba inicial recomendada es Ubuntu 24.04, DHCP, `started=false` y un
nombre descartable. El worker toma la clave pública configurada en Portainer
cuando `ssh_public_keys` llega vacío.
