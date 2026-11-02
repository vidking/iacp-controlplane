# Terraform MNG

Este es el entorno Terraform del clúster de laboratorio MNG.

- Clúster: MNG.
- Nodo de ejecución: `mng01`.
- Template protegido: VM 29000.
- VM 104 y VM 106 quedan fuera de las operaciones del proyecto.

Configura los valores reales fuera de Git usando `terraform.tfvars`.
El ejemplo está en `terraform.tfvars.example`.

En la ruta normal de la API no se edita este `tfvars`: el worker genera
`generated.auto.tfvars.json` desde la orden y la Golden Image observada. El
catálogo visible para editar imágenes y templates es `catalog.json`.

La red usa DHCP en `vmbr0`. El worker lee de la Golden Image la configuración
de CPU, el controlador de disco y el orden de arranque. Los tags del consumidor
se aplican literalmente; el orquestador no agrega tags automáticos.

Validación local:

```bash
terraform init -backend=false
terraform validate
```

