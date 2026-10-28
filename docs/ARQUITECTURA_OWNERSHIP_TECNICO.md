# Ownership técnico único de KVM

Esta revisión usa `provisioning-api-core` como único contrato operativo para
las KVM administradas por IACP. El CRM conserva pedidos, productos y reglas
comerciales; el Control Plane valida identidad técnica, tenant, catálogo,
capacidad, Terraform State, concurrencia y correspondencia física.

## Vínculo persistido

`technical_ownership` relaciona, de forma única por instancia, el tenant,
VMID, nodo, nombre, `state_key`, revisión IaC original, asignación sellada y
una huella SHA-256 de la evidencia de apply. La huella no incluye contraseñas,
llaves privadas ni tokens. VM 104, VM 106, VM 29000 y cualquier VM fuera del
rango de recursos administrables quedan protegidas.

La revisión IaC de una VM existente se conserva. El pin actual se exige para
crear un plan nuevo; no se compara con la revisión histórica para leer,
reconciliar, Lifecycle o destruir un state cuya compatibilidad técnica esté
verificada.

## Migración

La migración `0013_technical_ownership` solo adopta instancias no eliminadas
con Terraform apply completado, identidad de recurso, state key, revisión y
postcondiciones persistidas coherentes. Ignora evidencia incompleta o
colisiones y conserva el historial. Es idempotente: una segunda ejecución no
crea filas ni cambia VMIDs, state, discos, credenciales u operaciones.

En el IACP Control Plane (VM 104), con una ventana de cambio autorizada y un
directorio privado de evidencia, el operador puede ejecutar:

```text
scripts/iacpctl migrate-ownership --manifest ... --env-file ... --evidence-dir ...
```

La herramienta usa el contenedor API y la sesión normal de IACP; no edita
PostgreSQL directamente ni ejecuta Terraform o Proxmox. Las instancias que no
puedan probar su ownership quedan reportadas como `blocked` para una
reconciliación read-only posterior.

## Políticas históricas

`lab_policies` y sus columnas históricas permanecen para auditoría y
reconstrucción de migraciones. No tienen router, worker ni función de decisión
operativa. Ninguna creación, observación de IP, Lifecycle o eliminación Core
consulta vigencia, digest, vencimiento o aprobación LAB.
