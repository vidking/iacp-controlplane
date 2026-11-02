# Terraform PRX

Base técnica preparada para el clúster Platinum/PRX. El target permanece
deshabilitado hasta que se incorporen datos verificados del clúster.

- No habilitar `production` en `IACP_ENABLED_ENVIRONMENTS` mientras el catálogo
  no incluya nodos, planes, storage e imágenes verificados.
- Completar pool, endpoint y token en el gestor de secretos del despliegue.
- Confirmar una Golden Image autorizada y validar sus VMID y nodo de origen.
- El backend productivo usa exclusivamente `iacp/prod-runtime-v1` en el bucket Wasabi
  separado del state de laboratorio histórico.

`catalog.json` se deja intencionalmente vacío. La validación de configuración
no publica este entorno en `/v1/environments` hasta que el catálogo y las
credenciales estén completos. No copiar valores de MNG para hacer que pase la
validación.

Para inspeccionar solo la sintaxis Terraform, sin apuntar a infraestructura:

```bash
terraform init -backend=false
terraform validate
```
