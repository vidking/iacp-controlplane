# Terraform MNG01

Este es el entorno Terraform de MNG usado por IACP Control Plane.

- Clúster: MNG.
- Nodo de ejecución: `mng01`.
- Template protegido: VM 29000.
- VM 104 y VM 106 quedan fuera de las operaciones del proyecto.

Configura los valores reales fuera de Git usando `terraform.tfvars`.
El ejemplo está en `terraform.tfvars.example`.

En la ruta normal de la API no se edita este `tfvars`: el worker genera
`generated.auto.tfvars.json` desde la orden y la Golden Image observada. El
catálogo visible para producción es `catalog.json`. El stack de desarrollo
selecciona explícitamente `catalog-dev.json` mediante `IACP_MNG_CATALOG`.
Además de las imágenes compartidas, el catálogo DEV incluye Fedora 45
(VMID 29005) y Debian 13 (VMID 29006), ambos en `mng01` y `DS_CYBERFUEL`.
Estos IDs no se agregan al catálogo estable hasta una promoción independiente.

La red de laboratorio usa DHCP en `vmbr0` y no fija VLAN ni gateway. El worker
lee de la Golden Image el CPU (`x86-64-v4`, flags, NUMA), el controlador de
disco, el orden de arranque y los tags antes de generar el plan.

Validación local:

```bash
terraform init -backend=false
terraform validate
```

