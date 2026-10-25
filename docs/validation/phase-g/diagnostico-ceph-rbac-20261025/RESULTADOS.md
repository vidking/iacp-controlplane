# Fase G: dependencias KVM y alcance de Terraform

Fecha: 25/10/2026, America/Costa_Rica. Candidata base: `9f2dfcc`.
Estado: **NO-GO FASE G**. Sin merge, promoción, apply ni E2E físico.

## Publicación

SSH recuperado con la identidad de trabajo existente, puerto 22.
Main remoto comprobado: `d8ac23e5bfd9d210a3e2f1eec24a1255fb6f7db6`.
La rama `fix/fase-g-barrera-ejecucion` se publicó y su referencia remota se
comprobó contra `9f2dfccd60a70d9d234681ff4869c960d0601ea8`.
El correctivo de este informe continúa esa rama, sin reescribirla ni fusionarla.

## Diagnóstico específico y restricción nueva

La lectura inicial mostró dos comprobaciones de severidad HEALTH_ERR:
`AUTH_INSECURE_SERVICE_TICKETS` y `AUTH_INSECURE_SERVICE_KEY_TYPE`.
Esta última identifica claves de tipo `aes` en 16 OSD y dos gestores; la primera
señala tickets de servicio con el cifrado anterior. Los otros cuatro avisos
son HEALTH_WARN sobre clientes, claves rotatorias y configuración de autenticación.
La muestra tenía 129 PG `active+clean`. Es evidencia histórica de diagnóstico,
no autorización para operar ni certificación de escritura.

El significado de esos identificadores está descrito en la
[referencia oficial de Proxmox](https://github.com/proxmox/pve-docs/blob/master/pveceph.adoc#health-check-reference).
No son errores HTTP del Control Plane o del worker. Tampoco se reclasifican
como HEALTH_OK: son avisos de seguridad externos al alcance ahora autorizado.
La [lectura inicial sanitizada](lectura.json) conserva los estados reales.

**No se intervino Ceph.** Tras la restricción del responsable no se realizaron
nuevas consultas globales de salud ni cambios de pools, OSD, CephX, servicios
o configuración. No se prepararon comandos de reparación o silenciamiento.

## Barrera de las dependencias reales

Se sustituyó la dependencia de salud global por lecturas del backend concreto
mediante Proxmox, con TLS y la identidad propia del worker:

- Datastores de disco, clonación y Cloud-Init activos y habilitados.
- Tipo y métricas válidos; capacidad para el disco contratado, sin reducirlo.
- Soporte de imágenes y enumeración real de discos del datastore autorizado.
- Rechazo ante permisos denegados, timeout, backend ilegible, datos inválidos,
  falta de espacio o cancelación. Sin reintentos de escritura ni modo inseguro.

Las comprobaciones permanecen antes del plan y antes de consumir autorización
y ejecutar apply. Catálogo, tenant, cuotas, idempotencia, permisos de clonación,
locks y validación del plan siguen independientes y acumulativos.

A las 02:55:55, `DS_CYBERFUEL` devolvió HTTP 200 para estado y contenido:
activo/habilitado, tipo rbd, contenido `rootdir,images`, 30 discos enumerados
y 11.624.821.817.344 bytes disponibles. No se inventariaron Ceph ni su capacidad
integral. A las 02:58:15 la función candidata, ejecutada solo en memoria contra
ese backend, pasó las lecturas para 50 y 100 GiB: [evidencia](barrera-kvm.json).
Esto no prueba clonación, escritura, filesystem o bootstrap: lo harán los E2E.

## Permisos reales y decisión sobre VM 106

Identidad: `iacp-provision@pve!terraform-lab`, con `privsep=1`.
Usuario y token tienen **Administrator en `/` y `/vms`**. La separación del token
no reduce esos permisos porque ambos sujetos reciben los mismos privilegios.
También existe `IACPVM106Provision` sobre `/vms/106` para ambos: concede
configuración, Allocate y PowerMgmt. El flag efectivo 0 concede el privilegio
en el recurso exacto; no significa denegación.

**Actualización del responsable durante esta ejecución: VM 106 es descartable.**
Su acceso deja de ser bloqueo y no se propone una ACL NoAccess para ella.
La guarda nueva de apply protege VM 104 y las cuatro Golden Images. No convierte
VM 106 ni sus registros históricos en una instancia comercial nueva.

VM 104 tiene NoAccess para usuario y token y ninguna autorización efectiva en
la lectura exacta. Sin embargo, Administrator en `/` incluye Permissions.Modify:
la identidad sigue siendo excesiva y podría cambiar ACL fuera de IACP.
Las cuatro plantillas tienen VM.Audit/VM.Clone concedidos y también permisos
de administración innecesarios. No existe un déficit demostrado de clonación.

`iacp-lab` contiene VM 104, VM 106 y las cuatro plantillas. No añadir allí un rol
de administración amplio: alcanzaría plantillas. No se creó ningún pool.

## Restricción administrativa pendiente, para Infraestructura

La restricción sigue siendo de ejecución manual autorizada; no se aplicó ningún
cambio a ACL, usuarios, tokens o pools. El objetivo mínimo a revisar es:

1. Conservar NoAccess de VM 104 en usuario y token.
2. Sustituir Administrator de `/` y `/vms`, y los roles de provisión globales,
   por escritura únicamente en el ámbito aprobado de KVM administradas.
3. Mantener VM.Audit/VM.Clone sobre el ámbito de plantillas, sin configuración,
   PowerMgmt o eliminación. Mantener el uso acotado de datastore y red.
4. No aplicar un rol de ejecución al pool mixto actual. Infraestructura debe
   definir su delimitación antes de sustituir las ACL globales. No se impone
   otro pool ni ACL individuales por cada futura VM.

Si se adopta ejecución limitada a pools, hay que adaptar previamente el preflight
de VM.Allocate y las comprobaciones actuales de inventario/ausencia globales.
No se deben retirar permisos a ciegas ni interpretar un inventario filtrado
como prueba de ausencia. El proveedor ya soporta pool_id para nuevas instancias.

Comprobaciones manuales de solo lectura, sin exponer secretos:

```bash
pvesh get /access/acl --output-format json
pvesh get /access/permissions --userid 'iacp-provision@pve' --path /vms/104 --output-format json
pvesh get /access/permissions --userid 'iacp-provision@pve!terraform-lab' --path /vms/104 --output-format json
pvesh get /pools/iacp-lab --output-format json
```

## Pruebas y promoción

Regresión completa con PostgreSQL/Redis descartables: **1051 PASS**, cero
omitidos, 140,42 s. Esa corrida incluía todavía VM 106 entre los destinos
protegidos. Tras la aclaración, se retiró de esa guarda y se añadió la prueba
que conserva el resto de verificaciones: **193 PASS**, cero omitidos, 21,76 s.
Se revalidaron salud, contrato, recuperación histórica, admisión y bootstrap.
Los registros `regresion.log` y `revalidacion.log` conservan resultados y
diagnósticos; solo se normalizaron espacios finales para versionarlos.
El manifiesto de integridad contiene los nuevos hashes explícitos; no se cambia
el sello histórico. Contratos y autenticación se conservan.

No se modificaron presentación Swagger, configuración operativa, secretos,
Compose, states ni imágenes. Servicios de pruebas retirados sin `-v`.
No hubo acciones en Jira. La candidata **no está desplegada**.

Promoción y E2E kvm-1/kvm-2 permanecen pendientes de revisión/fusión y de la
restricción administrativa de la identidad. La promoción deberá validar además
material G6, TTL/persistencia, esquema 0012, respaldos y rollback compatible.
VM 106 no bloquea; Ceph queda fuera de este trabajo. La disponibilidad de lectura
del datastore no sustituye el E2E ni autoriza a declarar GO.
