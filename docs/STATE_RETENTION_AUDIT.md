# Wasabi: auditoría readonly y retención

`scripts/audit_state.py --tenant UUID` se ejecuta dentro del IaC Worker existente.
Solo usa GET/LIST, sin escribir SQL ni objetos/versiones. Vincula UUID/tenant y
state_key de PostgreSQL, observa lineage/serial/version/hash, cantidad de recursos,
versiones históricas y existencia de `.tflock`. Los bytes permanecen en memoria;
no se publican atributos, contraseñas, cuerpos, contenido de locks ni credenciales.

La inspección inicial de esta entrega constató **9 referencias**, **8 states con
cero recursos**, **1 con recursos**, sin locks activos y sin cambios de versión
durante la lectura. Son observaciones del tenant de laboratorio, no una auditoría
global de todos los objetos del bucket. Se conservará evidencia final tras el piloto.

Las credenciales actuales recibieron AccessDenied para consultar configuración de
versioning, retención, cifrado, policy y ACL del bucket. Se mantienen sus permisos.
Los VersionId/listados históricos observados acreditan versiones de esos objetos;
no permiten afirmar cuál es la política administrativa completa. La revisión de
retención/ACL/cifrado debe completarse con evidencia readonly del propietario del
bucket, sin entregar permisos administrativos al worker ni al CRM.

El backend exige HTTPS, cifrado solicitado y lockfile. API/tenants no reciben claves
Wasabi ni rutas de descarga de planes/state. La lectura y escritura técnicas las
realiza el servicio de confianza con referencias selladas por tenant/UUID; revisar
sus IAM/prefixes con el propietario del bucket antes de producción. Archivos de
planes/variables/manifest quedan en workspace privado 0700, archivos 0600; no
compartir el volumen con aplicaciones de clientes o con Lifecycle Worker.

Un state vacío tras DELETE **se conserva**, con lineage y serial y referencia
histórica. No equivale a objeto huérfano. State y versiones antiguas pueden guardar
la contraseña inicial recuperable del laboratorio, incluso después de eliminar la
VM; también son sensibles planes, tfvars y manifest. La entrada de PostgreSQL está
cifrada, su clave externa y backups privados forman parte del tratamiento de secretos.

Recuperación: observar versiones y comando aprobado, comprobar identidad, hash,
lineage/serial y ausencia/presencia real mediante endpoints IACP. No descargar un
state para editarlo, promover una versión antigua sobre un UUID activo ni usar SQL
para forzar resultados. No borrar `.tflock` sin probar propiedad/ausencia de un
holder en un procedimiento independiente. Reconciliación nunca repite Apply incierto.

Propuesta posterior de limpieza: inventario readonly por prefijo de tenant,
correspondencia con PostgreSQL y operaciones terminales, doble comprobación de
ausencia de VM, conservación de manifest/digests/linaje y retención aprobada por
Seguridad/propietario del bucket. Primero reporte/quarantine lógico; después un
job específico con autorización, auditoría y restore probado. Esta entrega no
elimina objetos, versiones ni evidencia y no supone que DELETE purga secretos.
