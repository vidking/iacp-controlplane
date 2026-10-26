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
`80f2d310ee0d8fe85ca4f7aef5fbc1c70248df3b`, usa el provider
**bpg/proxmox 0.116.1**, bajo [Mozilla Public License 2.0 en su versión fijada](https://github.com/bpg/terraform-provider-proxmox/blob/v0.116.1/LICENSE).
Los checks del driver conservan revisión y SHA-256 del archivo Git autorizado.
El contrato de propiedad started continúa separado de la energía imperativa.

En 0.10.0, las solicitudes root de laboratorio añaden el adaptador de
`iacp.terraform.access_adapter` al workspace extraído, con SHA-256
`bcab3f43fa4adb68df1e07e19e97c27188bea9e616c557d2219ae3e4bcc82fba`.
Conserva exactamente la llamada base antes de conectar la variable sensible,
añade sus declaraciones y reemplaza el bloque de inicialización en la clon.
El manifest sella el digest y el verificador comprueba cada archivo del adaptador.
La fuente original, CLI, provider y propiedad de energía permanecen fijados.
El adaptador validó con los binarios reales sin credenciales ni llamadas Proxmox.
Password es `sensitive`, recuperable en state/planes: riesgo aceptado solo en MNG.

**OpenTofu no se ejecuta ni se certifica en esta release.** Una implementación
futura exige otro adaptador validado, inventario de versiones/licencias, revisión
de planes/state y pruebas de los mismos controles. Independencia del contrato no
equivale a declarar intercambiables los binarios.

Para auditoría conservar commit del control plane, labels e IDs de imágenes
ejecutadas, revisión/archivo de IaC, lockfile del provider, versión CLI, plan
guardado y digest, contexto de credenciales sin secretos, versión/lineage/serial
de state y las operaciones IACP. No retirar avisos de componentes de terceros.
