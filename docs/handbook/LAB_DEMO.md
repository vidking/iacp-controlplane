# Demostración KVM de laboratorio MNG

El material prepara una demostración futura. No se ejecutó apply/destroy real ni
se modificó VM 104. La creación está implementada con controles en el PR del
ejecutor. La eliminación API nueva permanece OFF y exige plan y aprobación
independientes; no está desplegada ni ejecutada en este piloto.

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

## Eliminación API descartable

La rama 0.7.0 incorpora un plan DELETE revisado, aprobación independiente y un
worker Terraform con recuperación por observación. No se ha desplegado ni
probado sobre una KVM empresarial. Ver [DELETE seguro](../SAFE_DELETE.md).
No usar el antiguo procedimiento manual de destroy para sortear los controles
de IACP. Solo podrá probarse contra la nueva KVM descartable confirmada, nunca
VM 106 o VM 104. DHCP/untagged sigue sin soporte en el perfil actual.
