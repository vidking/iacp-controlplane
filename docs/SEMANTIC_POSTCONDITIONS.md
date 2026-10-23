# Verificación semántica de postcondiciones de KVM

Base de revisión: `main`, release
`c47f0bf7b0b59ea0f6a451327b9786c8ac16c391`. Terraform 1.16.6,
`bpg/proxmox` 0.116.1, fuente IaC fijada en
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4`.

## Causa raíz y evidencia

Se inspeccionaron en modo de solo lectura el `saved.tfplan` del intento 4,
el objeto actual de Wasabi y el `observation.tfplan` existente en el volumen
privado de planes. No se copiaron sus contenidos privados. El informe
[sanitizado](validation/POSTCONDITIONS_INCIDENT_SAFE.json) conserva únicamente
rutas, categorías de representación, máscaras booleanas y resultados.

El primer control que falla en el release base es
`TerraformExecutor.observe()` → `verify_postconditions(approved, resource)`:
`cpu[0].affinity`, conocido (`after_unknown=false`), `null` en el plan y cadena
vacía en state. El comparador recursivo anterior exige igualdad literal.
La regresión sintética con las representaciones observadas falla en esa llamada
sobre el código base y pasa con la corrección. El fallo ocurre antes de comparar
`refreshed`, verificar energía o ejecutar `verify_checks()`.

Las once rutas comunicadas se confirmaron para el state persistido. En los
valores refrescados existe además `cpu[0].flags`: `null` → lista vacía. Todos
estos valores son conocidos al planificar. La contraseña tiene
`after_sensitive=true`; su valor planificado es nulo y el observado está vacío.
Ningún valor sensible se incluye en el informe.

Los checks de la observación guardada pasan y su estado energético coincide con
la aprobación. El state inspeccionado conserva serial 3, lineage
`d954af88-6ae6-2d04-9ead-55ed1f95d644` y Version ID disponible. La interfaz
seleccionada es válida y coincide entre state y observación. Se verificaron
también el provider y su restricción exacta en el plan aprobado.

El comparador corregido, evaluado sin instalar código y solo sobre esos
artefactos, acepta ambos conjuntos de valores. Esto demuestra la causa del
bloqueo de postcondiciones y la compatibilidad de la corrección con la evidencia
disponible. No equivale a una recuperación completada: no se generó una nueva
observación, no se consultó de nuevo el estado energético nativo y no se ejecutó
`/recover`. La evidencia no demuestra la causa del incidente anterior
`STATE_CHANGED_DURING_OBSERVATION`, cuyos snapshots originales no se conservaron.

## Clasificación de las diferencias

Las referencias enlazan al commit exacto de bpg 0.116.1
`f2dd854b6ecbea09fe1273f233962517071b9b9c`. Las equivalencias se aplican de forma
direccional: un nulo planificado puede verificarse contra el valor vacío que
materializa ese atributo. No se convierte una clave ausente en nulo ni se
normalizan otros atributos.

| Ruta | Semántica y evidencia del provider | Condición aceptada y límites |
|---|---|---|
| `cpu[0].affinity` | Opcional, default vacío; `vmRead` devuelve vacío sin afinidad. [Schema y default](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L731), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L4718). | Nulo → cadena vacía. Cualquier afinidad concreta debe coincidir exactamente. |
| `cpu[0].architecture` | Opcional, default interno vacío; lectura sin `arch` retorna vacío. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L663), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L4613). | Nulo → vacío. No se equipara vacío con `x86_64`, `aarch64` ni otra arquitectura explícita. |
| `disk[0].file_id` | Fuente opcional de imagen, default vacío, ForceNew; conserva el origen conocido al refrescar. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/schema.go#L136), [lectura del origen](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/disk.go#L35). | Nulo → vacío: no se solicitó imagen para importar. Una fuente conocida diferente bloquea. |
| `disk[0].import_from` | Opcional, default vacío, origen usado al crear y retenido en state. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/schema.go#L144), [retención](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/disk.go#L593). | Nulo → vacío solamente. No se ignoran valores conocidos por ser una opción de creación. |
| `disk[0].serial` | Serial del disco, opcional y default vacío; lectura sin serial retorna vacío. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/schema.go#L153), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/disk/disk.go#L494). | Nulo → vacío. Un serial solicitado o materializado no vacío sigue siendo significativo. |
| `initialization[0].interface` | Opcional vacío significa selección automática. En clones conserva la unidad existente o usa `ide2`; el refresh descubre la unidad. [Schema y supresión de diff](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L848), [selección](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L2661), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L5015). | Para nulo/vacío planificado, exigir `ide0..3`, `sata0..5` o `scsi0..30` y misma selección en state y refresh. Una interfaz explícita exige igualdad. Se siguen verificando datastore, IP, gateway, usuario y claves. |
| `initialization[0].user_account[0].password` | Opcional, sensible y default vacío. PVE puede devolver una máscara; el provider preserva el secreto local conocido. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L973), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L5144). | El caso observado es nulo → vacío. Nulo frente a máscara o secreto no vacío bloquea. Un secreto conocido exige igualdad en ambos conjuntos de valores; `after_sensitive` nunca lo exime. No se imprimen valores ni hashes de contraseñas. |
| `memory[0].hugepages` | Opcional; vacío deshabilita hugepages. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L1215), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L5287). | Nulo → vacío. `2`, `1024`, `any` u otra diferencia real bloquean si no coinciden con lo aprobado. |
| `network_device[0].disconnected` | Bool opcional; sin `link_down`, lectura devuelve `false`. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/network/schema.go#L96), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/network/network.go#L155). | Nulo → booleano `false`. `true`, cero numérico u otros tipos bloquean. Bridge, VLAN, modelo y demás valores conocidos se comparan igualmente. |
| `network_device[0].trunks` | String opcional; sin trunks, lectura devuelve vacío. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/network/schema.go#L165), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/network/network.go#L163). | Nulo → vacío. No se aceptan trunks no solicitados ni se reordenan o convierten cadenas arbitrarias. |
| `pool_id` | Opcional y default vacío. La lectura detecta pertenencia directa o consulta pools. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L1345), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L5346). | Nulo → vacío únicamente, como en este incidente. Un pool no vacío exige coincidencia explícita. No se reproduce la supresión amplia de diff de pool del provider. |
| `cpu[0].flags` (adicional, refresh) | Lista opcional, default lista vacía; `vmRead` devuelve lista vacía sin flags. [Schema](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L677), [lectura](https://github.com/bpg/terraform-provider-proxmox/blob/f2dd854b6ecbea09fe1273f233962517071b9b9c/proxmoxtf/resource/vm/vm.go#L4700). | Nulo → lista vacía. Flags no vacíos, cambios de orden y cambios conocidos bloquean. |

## Implementación e invariantes

`verify_postconditions()` sigue recorriendo todos los valores conocidos.
`after_unknown=true` exime únicamente su subárbol; los hermanos conocidos se
verifican. La lista de atributos de diagnóstico solo limita qué nombres pueden
aparecer en una ruta pública; no participa en la decisión de coincidencia.
Las equivalencias requieren identidad de provider `registry.terraform.io/bpg/proxmox`
y restricción exacta 0.116.1 en la configuración del plan aprobado. Una versión o
provider diferente bloquea la normalización para revisión técnica.

`bpg_empty_default()` reconoce rutas y valores concretos; no elimina campos,
no generaliza null/vacío y no modifica plan, variables, state o configuración.
La selección automática de interfaz se verifica además entre ambos conjuntos
de evidencia. Las diferencias conocidas de CPU, memoria, discos, red, identidad,
pool y energía conservan su capacidad de impedir `completed`.

`postcondition_mismatch()` aporta `source`, `path` y `category` al diagnóstico
permitido de `PlanError`. Distingue state persistido, refresh, Proxmox y checks.
Las claves ajenas al contrato de diagnóstico se sustituyen por `<attribute>`;
no se incluyen valores, direcciones de checks ni excepciones del provider.

Se conserva sin cambios el flujo de observación del PR #17: GET actual de S3,
identidad, GET de Proxmox, plan refresh-only con locking Terraform, comparación
de postcondiciones, segundo GET actual con Version ID/lineage/serial/hash,
revalidación de contexto y commit condicional bajo el lock por instancia.
Un error de consulta no demuestra ausencia. Drift y conflicto concurrente
mantienen `uncertain`. Recovery no invoca `apply_saved` ni consume otra
autorización de mutación, aun con `IACP_TERRAFORM_APPLY_ENABLED=1`.

## Validación

`tests/test_semantic_postconditions.py` cubre las once rutas y flags, equivalencias
observadas, diferencias reales en state y refresh, valores explícitos, máscaras
anidadas, secretos conocidos, contraseñas enmascaradas, interfaz inválida o
cambiante, claves ausentes, tipos booleanos y rutas de diagnóstico privadas.
Las fixtures solo contienen valores sintéticos.

`tests/integration/test_readonly_state.py` incorpora recuperación con PostgreSQL
y S3 desechables para los valores equivalentes y los bloqueos de pool,
contraseña, red e interfaz. La suite conserva los casos del PR #16, la
invariancia S3 con Terraform CLI real, escritores externos, dos workers bajo
advisory lock PostgreSQL, Redis y Celery, redelivery e idempotencia del PR #17.
Resultados finales: [informe de validación](validation/SEMANTIC_POSTCONDITIONS_VALIDATION.json).

## Despliegue, recuperación y rollback

No se despliega automáticamente. Tras revisión y merge, fijar el SHA aprobado y
seguir el [procedimiento de despliegue existente](READ_ONLY_RECONCILIATION.md#despliegue-futuro-en-vm-104),
conservando overrides, volúmenes, planes, asignaciones y secretos. Esperar a que
terminen mutaciones activas antes de sustituir workers. No hay migraciones SQL,
cambios de dependencias ni cambios de configuración. Mantener Apply global en 1.

Para el intento 4, verificar por los GET documentados la instancia
`a1b2d063-6240-4969-bc04-e7e0e6be9e73`, el intento, el SHA del plan y la aprobación
`1750ee70-a6ca-4036-a764-c4b077a0b588`. Usar exclusivamente, sin cuerpo:

```text
POST /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform/applies/1750ee70-a6ca-4036-a764-c4b077a0b588/recover
```

202 significa encolado. Esperar `completed`, `error_code=null`, instancia ready,
VMID/ID 106, nodo mng01, address aprobado, `postconditions_verified=true`,
`prior_state.values`, snapshots before/after invariantes y un solo evento de
finalización. Si hay conflicto, discrepancia o 409, investigar el diagnóstico
y consultar la ejecución antes de otra recuperación completa. No usar `/apply`,
`/retry`, `import`, `destroy`, edición de state o SQL ni cambios de permisos.

Rollback: drenar sin interrumpir mutaciones y volver a las imágenes del release
base `c47f0bf7b0b59ea0f6a451327b9786c8ac16c391` con el mismo proyecto y todos sus
montajes y overrides. Mantener Apply en 1. La compatibilidad se conserva al no
cambiar esquema, dependencias, contratos de autorización ni artefactos. No
restaurar PostgreSQL o Wasabi: esta corrección no los transforma. El rollback
reintroduce este bloqueo de comparación; una operación ya completada conserva
su registro y no debe aprovisionarse de nuevo.

Límites: la evidencia de PVE y Terraform pertenece a un intervalo, no a una
transacción distribuida. PVE enmascara la contraseña y no permite demostrar por
lectura su valor dentro del huésped; no se infiere equivalencia de una máscara.
Una actualización del provider requiere revisar estas reglas antes de admitir
nuevas representaciones. El éxito contra artefactos guardados no sustituye la
observación fresca y las comprobaciones de concurrencia al ejecutar `/recover`.

## Gateway DHCP omitido (provider 0.116.1)

El primer plan real DHCP conservó `initialization[0].ip_config[0].ipv4[0].gateway=null` como valor conocido (no after_unknown). La función de lectura del [provider fijado](https://github.com/bpg/terraform-provider-proxmox/blob/v0.116.1/proxmoxtf/resource/vm/vm.go) asigna `""` cuando `GatewayIPv4` es nil. Se admite únicamente null → cadena vacía en esa ruta, con dirección aprobada `dhcp`, gateway aprobado null y el contrato bpg/proxmox 0.116.1 verificado. No admite gateway configurado, otros valores/tipos, direcciones estáticas ni otro provider. La observación nativa exige además `ip=dhcp` y ausencia de `gw` y tag; no se altera plan, revisión histórica ni state para hacer coincidir resultados. Esta corrección fue detectada antes del primer Apply; no acredita una VM creada.
