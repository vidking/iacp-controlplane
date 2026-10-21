# Demostración KVM de laboratorio MNG

El material prepara una demostración futura. No se ejecutó apply/destroy real ni
se modificó VM 104. La creación está implementada con controles en el PR del
ejecutor; la eliminación de KVM todavía no tiene endpoint API y se realiza como
cambio de Infraestructura separado, con plan y aprobación propios.

## Información que debe autorizarse

No usar valores de producción por defecto ni copiar fixtures de pruebas.
Infraestructura y Seguridad deben entregar o confirmar:

| Parámetro | Validación requerida |
|---|---|
| Tenant/identidades | Tenant activo, operador CRM, reader y approver independiente; tokens fuera de Git |
| `image` y nombre | Etiqueta lógica aprobada y nombre único en el tenant |
| `vm_id` | VMID libre, reservado exclusivamente para la KVM lab; nunca 104 ni la plantilla |
| `allowed_nodes`, `node_name` | Subconjunto autorizado de mng01/mng02 y nodo de destino con capacidad |
| `template_vm_id`, `template_node_name` | Plantilla aprobada, existencia, origen y permisos; nunca VM 104 |
| `pool_id`, `clone_datastore_id` | Pool/datastore de laboratorio cuando corresponda, sin inferir valores |
| `network_bridge`, `vlan_id` | Bridge/VLAN de laboratorio, conectividad y políticas aprobadas |
| `ipv4_cidr`, `ipv4_gateway` | Reserva IPAM y gateway autorizados, sin inventar IP/rango |
| `cloudinit_datastore_id`, `cloudinit_username`, `ssh_public_keys` | Almacenamiento, usuario y claves públicas aprobados, sin contraseñas en tfvars |
| `boot_disk.interface/datastore_id/minimum_size_gb` | Interfaz exacta de la plantilla, datastore y tamaño mínimo real verificado |
| `disk_gb`, CPU y RAM | Recursos aprobados y disco solicitado >= mínimo; verificar opciones/formato del disco |
| `started`, `guest_agent_enabled` | Decisión explícita; defaults false no prueban arranque ni crecimiento del filesystem |
| `backend.bucket/region/endpoint` | Backend Wasabi existente y prefijo autorizado, versionado, cifrado y locking comprobados |
| Credenciales | Identidades de Proxmox y Wasabi con privilegios mínimos; endpoint HTTPS y CA confiable |
| TLS/red | DNS oficial, certificado corporativo, LAN, origen/IP efectiva del CRM y egress autorizados |
| Recuperación | Backup PostgreSQL restaurado de prueba, Redis, secretos y planes; responsable y ventana de rollback |

Las opciones del disco (`file_format`, `aio`, `cache`, `discard`, `backup`,
`replicate`, `iothread`, `ssd`) deben corresponder a la plantilla. El módulo
mantiene compatibilidad con discos heredados cuando no se configura el objeto;
el Control Plane requiere un disco explícito para comparar la observación.
No reduce discos ni certifica automáticamente la expansión del filesystem invitado.

La asignación de runtime es un JSON externo indexado por `tenant_uuid/nombre`.
El validador sin red recibe el objeto individual y una petición InstanceCreate:

```sh
# Dentro de la imagen de release, con archivos externos de solo lectura.
python -m iacp.terraform.lab --allocation /run/config/lab-allocation.json \
  --request /run/config/lab-request.json
```

Valida tipos, entorno lab, restricciones, imagen y tamaño. No confirma la
existencia de una plantilla, disponibilidad IPAM, capacidad, permisos ni acceso a
Wasabi; esos puntos requieren la revisión autorizada del laboratorio.

## Crear y consultar

1. Registrar el commit de release integrado, revisión fija de iac-proxmox y
   digests de imágenes. Verificar todos los respaldos y condiciones del runbook.
2. Con apply todavía deshabilitado, habilitar únicamente planificación en el
   worker autorizado. Crear la instancia mediante los ejemplos de
   [integración API](API_INTEGRATION.md); guardar IDs, claves y X-Request-ID.
3. Esperar planned. Infraestructura revisa `terraform show -json saved.tfplan`
   en el volumen privado: una sola creación/no-op en la dirección del módulo,
   VMID/nodo/recursos/red/plantilla autorizados, state exclusivo y ausencia de 104.
4. Verificar locking Wasabi con un procedimiento de laboratorio aprobado;
   las pruebas locales no sustituyen la validación real de solicitudes condicionales.
5. Obtener autorización explícita para la operación real y para habilitar
   `IACP_TERRAFORM_APPLY_ENABLED=1` en API y worker. El cambio de entorno requiere
   una ventana operativa y no está autorizado por la aprobación del PR.
6. Un approver independiente emite autorización con intento/hash y vigencia.
   El operador solicita apply una sola vez con su clave estable. Observar el job
   nuevo y la operación original, no volver a solicitar creación por un timeout.
7. Exigir run completed, instancia ready, infrastructure_kind kvm, VMID/nodo
   correctos, recursos devueltos por provider y fecha de observación. Capturar
   eventos sin secretos y confirmar la misma KVM mediante lectura autorizada.
8. Deshabilitar apply al terminar la ventana. Probar recuperación solo bajo un
   protocolo aprobado; nunca inducir pérdida de datos o de state como demostración.

Criterios de éxito: una única KVM, un único state por UUID, un único inicio de
mutación registrado, pertenencia al tenant, dimensiones del disco correctas y
ningún cambio sobre VM 104. Si aparece uncertain, detener la demo y conciliar.

## Eliminar con control de Infraestructura

La API actual rechaza planes destructivos y no expone DELETE. La demostración
completa combina creación por API con eliminación controlada fuera de ella.
No se debe presentar esta parte como una función automática del Control Plane.

Antes de eliminar: autorización explícita distinta de la creación, registro del
VMID/nodo/state/lineage/serial, backup del state, prueba de pertenencia exclusiva,
KVM sin datos de terceros y plan de recuperación. Detener nuevas mutaciones y
verificar que no haya procesos Terraform activos. Mantener el mismo módulo,
variables congeladas y backend de la instancia, sin copiar su state a otro UUID.

En un entorno operativo aprobado, Infraestructura prepara un contenedor de
operación desde la imagen terraform-worker del release, con los mismos montajes
privados y credenciales cargadas desde el gestor autorizado. No requiere Terraform
del host. Debe fijar TF_WORKSPACE=default y TF_DATA_DIR del workspace original,
sin TF_CLI_ARGS heredados. Con la identidad y directorio exactos del workspace
lab, prepara y revisa estos comandos. Son instrucciones
para una ventana futura y no se ejecutan mediante las pruebas de este repositorio:

```sh
# TF_LAB_ROOT es el workspace verificado de ESTA instancia; credenciales por entorno seguro.
: "${TF_LAB_ROOT:?Workspace autorizado de laboratorio}"
cd "$TF_LAB_ROOT"
terraform version
terraform init -input=false -no-color -lockfile=readonly -backend-config=backend.tfbackend.json
terraform plan -destroy -input=false -no-color -lock-timeout=30s -out=lab-delete.tfplan
chmod 600 lab-delete.tfplan
terraform show -json lab-delete.tfplan > lab-delete.review.json
chmod 600 lab-delete.review.json
sha256sum lab-delete.tfplan
```

Revisar que resource_changes contenga únicamente
`module.kvm.proxmox_virtual_environment_vm.this`, actions `delete`, before.vm_id
igual al reservado y distinto de 104, nodo correcto y ningún recurso ajeno en
prior_state. Eliminar la KVM implica pérdida de su disco: exigir la aprobación
específica y el hash de ese plan antes del siguiente paso.

```sh
# Solo después de autorización explícita de eliminación y verificación del hash aprobado.
: "${LAB_DELETE_APPROVED_SHA256:?Hash registrado por el aprobador de eliminación}"
test "$(sha256sum lab-delete.tfplan | cut -d ' ' -f 1)" = "$LAB_DELETE_APPROVED_SHA256"
terraform apply -input=false -no-color -lock-timeout=30s lab-delete.tfplan
terraform state list
```

No usar `terraform destroy`, `-auto-approve`, `-lock=false`, state rm ni edición
manual para ocultar fallos. Un apply de plan destructivo también es una operación
real y requiere autorización, aunque el nombre del comando sea apply.

Tras completar: confirmar ausencia por lectura del provider/Proxmox, state sin
recursos, release de lock y evidencia de VMID/nodo/hash/fecha/aprobador. Conservar
state versionado y eventos; no borrar la fila de instancia ni reutilizar VMID a
ciegas. El inventario de IACP no se actualiza automáticamente tras una eliminación
externa: documentar esa limitación y cerrar la evidencia en el registro de cambio.
La automatización de eliminación y la conciliación periódica quedan pendientes.
