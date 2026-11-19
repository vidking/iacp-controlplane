# Experiencia de aprovisionamiento de Nextcloud y n8n

Este documento define el flujo que puede consumir el portal de clientes. El
Control Plane recibe la orden, valida el catálogo, crea una KVM en Proxmox MNG,
instala la aplicación, verifica su estado y entrega el acceso inicial. La API no
debe pedir credenciales de proveedores externos para iniciar sesión en estos
dos productos.

## Datos que solicita el portal

| Campo | Nextcloud | n8n | Motivo |
| --- | --- | --- | --- |
| Plan, imagen y nombre | Sí | Sí | Recursos y sistema operativo certificado |
| Perfil de red | Sí | Sí | En el piloto se usa `lab_dhcp`; producción usará el perfil WAN autorizado |
| Contraseña root | Sí en el portal actual | Sí en el portal actual | Acceso SSH al sistema operativo, no es la contraseña de la aplicación |
| Llave pública SSH | Opcional | Opcional | Acceso al sistema sin contraseña; la contraseña root basta si no se envía llave |
| Dominio | Si se elige acceso por dominio | Si se elige acceso por dominio | Debe resolver a la IP de la instancia antes del aprovisionamiento |
| Token de Google, Cloudflare u otro proveedor | No | No | No es necesario para crear o iniciar sesión; se configura en la aplicación únicamente para integraciones que lo requieran |

La orden debe incluir al menos un método de acceso al sistema: `root_password`,
`ssh_public_keys` o ambos. El portal de desarrollo solicita contraseña root y
deja opcional la llave SSH. El Control Plane agrega una llave temporal propia
para la automatización; no reemplaza ni expone la llave del cliente y se revoca
al completar la entrega.

## Flujo recomendado del portal

1. Consultar `GET /v1/applications`, `GET /v1/plans`, `GET /v1/images` y
   `GET /v1/network-profiles`. Presentar únicamente aplicaciones y recursos que
   la API devuelve como habilitados.
2. El cliente elige Nextcloud o n8n, plan, Ubuntu Server 24.04, nombre,
   perfil de red y método de acceso al sistema. En el piloto, el modo web por IP
   usa una CA privada descargable; el modo por dominio requiere DNS preparado.
   En este contrato, `1.1.1.1` y `8.8.8.8` son resolvers de la KVM; no son
   credenciales DNS ni crean registros. El dominio debe apuntar a la IP antes de
   pedir el certificado HTTPS. El portal no debe pedir el token de la zona DNS
   para estas recetas.
3. Enviar `POST /v1/service-instances` con una `Idempotency-Key`. No generar
   contraseña de aplicación en el portal ni incluir secretos de proveedores
   salvo que una función futura los requiera expresamente.
4. Consultar la operación y el trabajo de servicio hasta que la operación
   termine y el servicio llegue a `awaiting_customer`. `ready` de la KVM por sí
   solo no significa que la aplicación ya esté instalada.
5. Mostrar el botón `Abrir servicio` y las instrucciones según el producto:
   - **Nextcloud:** consultar una sola vez
     `POST /v1/service-instances/{id}/credentials`. La respuesta contiene
     `admin_user` y `admin_password`; guardar la contraseña en un gestor seguro.
     El enlace abre la pantalla de inicio de sesión de Nextcloud.
   - **n8n:** consultar el mismo endpoint para confirmar
     `owner_setup_required`. El enlace abre el asistente nativo de n8n para
     crear la cuenta propietaria. El cliente define y conserva su correo y
     contraseña en n8n; no hay contraseña de proxy ni credencial n8n generada
     por IACP.
6. En acceso de piloto por IP, instalar la CA que devuelve
   `GET /v1/service-instances/{id}/tls-ca` en el equipo del cliente. Esto
   establece confianza HTTPS y no sustituye el login de la aplicación. Para
   acceso por dominio se requiere DNS público correcto y certificado válido.
7. Completar `POST /v1/service-instances/{id}/deliver` una vez que el cliente
   haya recibido el acceso. La operación revoca la llave temporal de
   automatización. La recuperación de credenciales es de una sola lectura, por
   lo que el portal debe mostrarlas sin cachearlas y advertir que se guarden.

Las llamadas de creación, entrega, ciclo de energía y eliminación deben usar
`Idempotency-Key`. El portal puede administrar cada instancia mediante
`GET /v1/service-instances/{id}`, los endpoints de energía de la KVM y
`DELETE /v1/service-instances/{id}`. Para eliminar un servicio, primero se apaga
la KVM; la eliminación confirmada también libera la IP de NetBox cuando se usa
el perfil estático.

## Acceso por aplicación

El acceso de sistema operativo y el acceso web son credenciales diferentes:

- **SSH:** el cliente usa su llave SSH si la entregó; si no, usa la contraseña
  root. La llave temporal interna de instalación se elimina durante la entrega.
- **Nextcloud:** usa la cuenta administrativa que devuelve la API una sola vez.
- **n8n:** usa su asistente nativo la primera vez y después su pantalla normal de
  login. Caddy solo termina TLS y reenvía el tráfico; no agrega autenticación
  HTTP Basic, que causaría el diálogo del navegador ajeno a n8n.
- **Integraciones:** tokens de Google, Cloudflare y otros proveedores se
  configuran en la aplicación solo cuando el cliente conecte una integración.
  No son necesarios para desplegar ni para entrar a la aplicación. La creación
  de registros DNS queda fuera del contrato actual; cuando se automatice, el
  token de DNS debe ser una credencial de plataforma de alcance mínimo y el
  worker debe operar sobre la zona autorizada sin entregarlo a la KVM.

## Contrato mínimo de creación

```json
{
  "application_id": "n8n",
  "instance": {
    "name": "servicio-cliente-001",
    "plan_id": "kvm-2",
    "image_id": "ubuntu-2404",
    "environment": "lab",
    "network_profile": "lab_dhcp",
    "access": {
      "root_password": "<secreto-del-cliente>",
      "ssh_public_keys": []
    },
    "start_on_create": true
  },
  "configuration": {"access_mode": "ip"}
}
```

La respuesta de creación entrega identificadores de operación, instancia,
servicio y trabajo; no devuelve la contraseña root ni credenciales de aplicación.
El ejemplo es ilustrativo: no reutilizar la contraseña del texto.
