# IACP e IaC Worker: inventario del motor

**IACP Orchestrator** es la API y **IaC Worker** el componente de aprovisionamiento.
El contrato de Desarrollo identifica instancias/operaciones, perfiles y cuotas.
No expone un selector de motor ni exige instalar una CLI IaC en el CRM.

El componente actualmente ejecuta **Terraform CLI 1.16.6**, fijado en
`app/Dockerfile` y `iacp.terraform.config.TERRAFORM_VERSION`. Esta versión incluye
Business Source License 1.1 y su Additional Use Grant; conservar el texto y los
avisos del [LICENSE oficial de esa versión](https://github.com/hashicorp/terraform/blob/v1.16.6/LICENSE).
La denominación comercial IaC Worker no renombra el binario ni cambia su licencia.

El stack fijado de `CyberFuel/iac-proxmox`, revisión
`2c0f319ba5d7a45987cd9d0395673828c28f678b`, usa el provider
**bpg/proxmox 0.116.1**, bajo [Mozilla Public License 2.0 en su versión fijada](https://github.com/bpg/terraform-provider-proxmox/blob/v0.116.1/LICENSE).
Los checks del driver conservan revisión y SHA-256 del archivo Git autorizado.
El contrato de propiedad started continúa separado de la energía imperativa.

**OpenTofu no se ejecuta ni se certifica en esta release.** Una implementación
futura exige otro adaptador validado, inventario de versiones/licencias, revisión
de planes/state y pruebas de los mismos controles. Independencia del contrato no
equivale a declarar intercambiables los binarios.

Para auditoría conservar commit del control plane, labels e IDs de imágenes
ejecutadas, revisión/archivo de IaC, lockfile del provider, versión CLI, plan
guardado y digest, contexto de credenciales sin secretos, versión/lineage/serial
de state y las operaciones IACP. No retirar avisos de componentes de terceros.
