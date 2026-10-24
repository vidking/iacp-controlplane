# Guía local de integración CRM

La URL base autorizada es `https://iac.cyberfuel.com`. Usa `Authorization: Bearer <token-del-tenant>` y una identidad de tenant con los perfiles permitidos. No envíes VMID, nodo, state ni credenciales de infraestructura.

## Flujo

1. Consulta `GET /v1/profiles` y selecciona un perfil vigente.
2. Envía `POST /v1/instances` con `Idempotency-Key` único. La respuesta `202` confirma admisión, no que la VM esté lista.
3. Conserva `instance_id`, `operation_id` y `job_id`; sigue `GET /v1/operations/{operation_id}` hasta `completed`, `failed` o `uncertain`.
4. Distingue VM creada, arrancada y lista: comprueba el estado de operación, la red y el acceso antes de entregar el recurso.
5. Usa `POST /v1/instances/{instance_id}/start`, `/shutdown` o `/reboot` para Lifecycle y `DELETE /v1/instances/{instance_id}` para eliminación autorizada.

```bash
curl -sS https://iac.cyberfuel.com/v1/profiles \
  -H 'Authorization: Bearer TOKEN_DEL_TENANT'

curl -sS -X POST https://iac.cyberfuel.com/v1/instances \
  -H 'Authorization: Bearer TOKEN_DEL_TENANT' \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: CRM-INTENCION-001' \
  -d '{"name":"crm-demo","profile_id":"44444444-4444-4444-8444-444444444444","start_on_create":true}'
```

Reintenta la misma intención con la misma clave y cuerpo. Un cuerpo distinto con esa clave devuelve `409`. Trata `401` como identidad ausente/expirada, `403` como permiso insuficiente, `409` como conflicto o cuota, `429` como límite temporal y `503` como admisión/dependencia no disponible. Ante `uncertain`, no crees una instancia sustituta: conserva el UUID y solicita reconciliación autorizada.

## Fuente versionada

La explicación principal está disponible aquí sin depender de Forgejo. Para revisar el Markdown versionado: [Ver fuente versionada](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/src/branch/main/docs/GUIA_CRM_DESARROLLO.md).
