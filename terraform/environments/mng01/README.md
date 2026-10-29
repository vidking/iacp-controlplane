# Terraform MNG01

Este es el único entorno Terraform incluido en IACP Control Plane V2.

- Clúster: MNG.
- Nodo objetivo inicial: `mng01`.
- Template protegido: VM 29000.
- VM 104 y VM 106 quedan fuera de las operaciones del proyecto.

Configura los valores reales fuera de Git usando `terraform.tfvars`.
El ejemplo está en `terraform.tfvars.example`.

Validación local:

```bash
terraform init -backend=false
terraform validate
```

