# Read-Only Infrastructure Reconciliation

Estado: propuesto. Sin número reservado; asignarlo conforme al registro del
repositorio al aceptar la decisión.

## Contexto

Un Apply puede terminar en infraestructura y perder su confirmación en PostgreSQL.
Repetirlo puede duplicar efectos o consumir una autorización ya utilizada.
La existencia del recurso en dos fuentes no prueba todas las postcondiciones.
Además, Terraform state pull reserializa datos: una comparación posicional de
checks no identifica una escritura del objeto remoto.

## Decisión

Separar provisioning de reconciliation y drift detection. Reconciliation observa
mediante GET y planes no mutadores; nunca ejecuta Apply, refresh, import o destroy
ni sincroniza state automáticamente. Terraform conserva su propiedad exclusiva.

Vincular la observación a la aprobación, Instance ID, intento, SHA, revisión,
variables, backend y workspace. Verificar identidad y todos los valores conocidos
del plan aprobado en state y observación refrescada, más los checks y Proxmox.
Cuando el snapshot sellado en el plan contiene lineage, exigir su continuidad y
un serial actual no inferior, sin modificar ni completar manifests históricos.

Tomar dos snapshots actuales del objeto S3, cada uno con bytes y versión de una
misma respuesta. Exigir Version ID inmutable, lineage, serial y hashes iguales.
Canonicalizar solo colecciones estructurales para diagnóstico sin descartar datos.
Todo cambio o error conserva uncertain y requiere observación nueva.

Reutilizar el advisory lock por instancia y el locking nativo de Terraform. No
introducir otro motor de locks. Revalidar contexto y exclusión antes del commit
condicional; persistir resultado y evento terminal en una transacción.
Recovery no depende de deshabilitar Apply globalmente.

## Alternativas evaluadas

- Ignorar serial, checks o el error: descarta evidencia y puede aceptar conflictos.
- Comparar únicamente state pull canonicalizado: corrige la permutación pero no
  identifica reescrituras con contenido igual ni ABA.
- Aplicar refresh-only para sincronizar: convierte recuperación en mutación del
  state y mezcla responsabilidades.
- Otro servicio de locking: duplica las garantías existentes y no coordina
  automáticamente escritores externos.

## Consecuencias

Recovery es repetible sin reprovisionar y conserva evidencia verificable. Requiere
versionado inmutable y GET consistente de S3; defaults o atributos no demostrables
pueden mantener uncertain. No existe una transacción atómica entre tres sistemas;
la exclusión de IACP no sustituye la coordinación de escritores externos.

Implementación, pruebas, despliegue y rollback:
[READ_ONLY_RECONCILIATION.md](../READ_ONLY_RECONCILIATION.md).
