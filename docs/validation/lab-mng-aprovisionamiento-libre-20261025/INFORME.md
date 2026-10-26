# Modo piloto libre de aprovisionamiento LAB/MNG

Fecha: 25/10/2026, America/Costa_Rica. Candidata basada en main `5b1c0a8c20f773d975a6ee2c8cb1946256848a30` (PR #79). **No desplegada ni certificada físicamente.** Runtime comprobado: `54d418119200710ee00eb7a9d406b9322fe8e40c` en el IACP Control Plane (VM 104).

## Corrección

`lab_mng_physical_capacity_only=true` es una configuración persistente del tenant, no una propiedad aceptada del JSON de creación. Desactiva únicamente los presupuestos comerciales acumulados de instancias, vCPU, RAM y disco. Los números anteriores siguen en el esquema interno para compatibilidad; **no limitan el aprovisionamiento en este modo**. No se subieron a otro cupo ni se eliminaron reservas históricas.

La configuración versionada [commercial-limits.lab-mng.json](../../deployment/commercial-limits.lab-mng.json) autoriza exactamente los cuatro planes oficiales. El alcance se verifica al cargar límites y al admitir: tenant LAB existente, perfil autorizado lab-dhcp, nodos mng01/mng02 y DS_CYBERFUEL. Una copia de la excepción en otro tenant o destino se rechaza. El rol operator, Bearer, tenant persistente, idempotencia, snapshots, locks, protección de recursos, G6 y barreras del worker permanecen intactos.

Concurrencia **1** permanece como protección técnica; no es una cuota comercial de cantidad de VMs. La capacidad física se comprueba antes de cada admisión y el worker conserva su barrera antes de modificar infraestructura. No se permite CPU/RAM/disco arbitrarios. Los tenants sin el modo explícito conservan sus presupuestos anteriores.

En este modo los cuatro planes pueden utilizar el cuerpo mínimo sin network ni commercial_context; no se genera un order_id ficticio ni aprobación por VM. La omisión fuera del modo conserva el piloto KVM 1/2 vigente. El contrato público conserva campos, tipos, respuestas, operationId y autenticación; solo se aclaran descripciones. [Comparación contractual](diff-contractual.json).

## Swagger operacional

La entrada principal se llama **Crear máquina virtual**. Conserva plegado, navegación, índices y copia. Authorize reutiliza el Bearer existente; **Consultar catálogo autorizado** hace únicamente GET /v1/plans y GET /v1/images. La selección actualiza JSON y cURL; las opciones no disponibles se deshabilitan y muestran sus motivos. No guarda tokens en el DOM, storage ni logs. Se explica exactamente dónde pulsar Try it out, introducir Idempotency-Key y Execute, y cómo seguir estado, red, acceso, Lifecycle y DELETE.

Los catálogos del navegador de la candidata se simulan únicamente en la prueba de interfaz, identificados como fixtures. **No representan disponibilidad ni E2E del runtime.** La prueba contra HTTPS real se repetirá después de la integración y promoción.

## Infraestructura real observada, sin mutaciones

[Preparación de runtime](preparacion-runtime.json): siete servicios ejecutándose, API/PostgreSQL/Redis saludables, imágenes previas conservadas. Consultas HTTP autenticadas de capacidad con su identidad dedicada: nodo mng01 y DS_CYBERFUEL activos/habilitados; 48 CPU, aproximadamente 170 GiB RAM libre y 10,5 TiB de almacenamiento disponible al observar. No es una reserva: comprobar de nuevo antes de cada prueba, descontando reservas de IACP. No se consultó ni modificó Ceph.

[Plantillas por lectura](plantillas-lectura.json): las cuatro existen como template en mng01 y tienen configuración Cloud-Init y Agent. Se utilizó la identidad ya configurada del componente Lifecycle para consultar configuración; no se modificó ninguna plantilla. **Existencia y configuración no equivalen a funcionamiento físico de un clon.**

| Imagen | Plantilla real | Catálogo de ejecución actual | KVM 1/2 | KVM 3/4 |
|---|---:|---|---|---|
| ubuntu-2404 | 29000 | Habilitada; disponibilidad depende de admisión actual | E2E histórico validado; repetición con token operator y modo libre PENDIENTE | PENDIENTE de E2E físico |
| ubuntu-2604 | 29001 | Pendiente, no seleccionable | PENDIENTE de integración/arranque/acceso/disco | PENDIENTE |
| almalinux-9 | 29010 | Pendiente, no seleccionable | PENDIENTE de integración/arranque/acceso/disco | PENDIENTE |
| almalinux-10 | 29011 | Pendiente, no seleccionable | PENDIENTE de integración/arranque/acceso/disco | PENDIENTE |

No se habilitaron imágenes pendientes por el solo hecho de existir. El inicializador certificado actual es específico de Ubuntu 24.04; no se afirma compatibilidad de las otras distribuciones ni se las ofrece como ejecutables. No se cambiaron Golden Images ni autenticación.

## Pruebas y evidencias

- Pruebas focalizadas: [registro](pruebas-focales.log).
- Seguridad y modo LAB: [registro](pruebas-seguridad.log), incluye los cuatro planes mínimos, idempotencia, ausencia de publicación si falla capacidad y rechazo de destinos/tenants fuera de alcance.
- Regresión completa con PostgreSQL y Redis descartables: **1103 aprobadas, cero omitidas**, [registro](pruebas-regresion.log). La primera ejecución detectó una aserción documental que todavía exigía el título antiguo; se actualizó al título solicitado y se repitió la suite. No fue un fallo del aprovisionamiento.
- Playwright Chromium escritorio/móvil y Firefox: resultados y capturas en navegador. Catálogo, copia completa, siete categorías, guías, historial, deep links, márgenes, recursos locales y Try it out de **solo lectura**. Sin Execute de creación ni Terraform apply durante esta validación.

## Promoción y ensayo posterior a la integración

1. Verificar merge y SHA exacto de main; no promover la rama sin integrar. Crear release e imágenes desde ese SHA y manifiesto revisado mediante el procedimiento vigente. Inventario, snapshot, workers ociosos, respaldo PostgreSQL con restauración aislada, copia privada de configuración y registro de autorización nuevo. Preservar el registro de cinco credenciales y los secretos G6.
2. Preparar una copia de la configuración activa de perfiles. Cambiar **solo** la entrada del tenant LAB en commercial_limits usando la entrada versionada de este PR; conservar perfiles, otros tenants y snapshots de autorización. La ruta activa observada es `/etc/iacp/terraform/profiles-comercial-g-cierre-20261025.candidate.json`, montada readonly como `/run/config/profiles.json`. El nombre histórico no cambia el hecho de que es configuración operativa persistente. No cambiar Compose ni secreto de autenticación.
3. Revisar/pinear SHA256 del candidato de perfiles y su reemplazo autorizado en el manifiesto según el procedimiento actual. La revisión de activación debe contener los límites efectivos completos, incluyendo **lab_mng_physical_capacity_only=true**, y el hash nuevo; no reutilizar el hash/revisión anterior ni eliminar su validador. Con admisión excluida y sin trabajo, instalar atómicamente conservando dueño/modo y remontar todos los servicios afectados mediante la promoción. El montaje de archivo no debe conservar un inode anterior.
4. Ejecutar `go_live.py preflight`, `iacpctl env-promote`, `go_live.py promote` y `activate-lab` con manifiesto, backup, revisión y directorios de evidencia nuevos. Ejecutar verify/doctor y comprobar los SHA y archivos efectivos en cada servicio. PostgreSQL, Redis, Traefik, volúmenes e identidades no se sustituyen.
5. Validar por HTTPS con lab-swagger-postman, sin usar bootstrap: tenant correcto, cuatro planes, imágenes, documentos y autenticación. Repetir Playwright en el dominio real. La disponibilidad de plan debe depender de capacidad técnica, no del contador histórico de dos instancias.
6. Ejecutar secuencialmente el runner existente ampliado, en VM 104 y desde el release integrado. `--minimal` exige un archivo de token de integración explícito; nunca lo pasa por argumentos ni lo imprime. No borra las instancias previas ni exige que el inventario esté vacío. Cada prueba verifica que su limpieza restaura la ocupación comercial inicial.

```bash
# Variables de rutas privadas obtenidas del manifiesto y entrega vigentes;
# no introducir el contenido del token en la línea de comandos.
python3 scripts/e2e-commercial-sequential.py --execute --minimal \
  --token-file "$TOKEN_LAB_FILE" --plan kvm-1 --evidence-dir "$EVIDENCIA_KVM1"
# Solo tras PASS y limpieza confirmada, repetir con kvm-2, kvm-3 y kvm-4,
# cada uno con un directorio nuevo y disponibilidad física revalidada.
```

El runner comprueba HTTP 202 y replay idempotente, procesamiento real, recursos exactos, partición/filesystem, Cloud-Init, DHCP, host SSH autenticado, acceso root por llave y contraseña PAM suministrada mediante G6, Lifecycle, DELETE, ausencia física/state, limpieza de referencia temporal y confidencialidad en artefactos y logs. Detiene la secuencia ante fallo; conservar instance_id y evidencias para recuperación autorizada, sin repetir clonación ni forzar state. No se admiten pruebas concurrentes.

## Reversión y dictamen

Conservar imágenes, manifiesto y configuración anteriores. Si falla preflight no modificar runtime. Si falla la promoción cerrar admisión mediante el driver y recuperar la release compatible, conservando datos y esquema. No hacer downgrade ni borrar estados. Si existen intenciones del nuevo modo, resolverlas con código capaz de interpretarlas antes de retroceder; restaurar cuotas anteriores puede impedir nuevas admisiones pero no justifica borrar recursos/reservas. La configuración anterior no conoce la bandera nueva: al revertir al binario previo debe restaurarse también su copia de configuración bajo el procedimiento controlado.

**NO-GO para certificar este modo hasta promoción y E2E físicos completos.** El piloto histórico KVM 1/2 permanece independiente. El límite comercial activo no se declara eliminado mientras VM 104 siga ejecutando el binario/configuración anteriores. No hay nueva certificación de resiliencia integral.
