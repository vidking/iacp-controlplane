# Fase G: cierre de código y preparación de promoción

Fecha de corte: 24/10/2026, 23:54, America/Costa_Rica.

**NO-GO FASE G PARA PROMOCIÓN.** Implementación candidata probada en aislamiento;
no se desplegó ni se autorizó su ejecución comercial en el IACP Control Plane
(VM 104). Las pruebas físicas y la migración de consumidores siguen pendientes.

## Versiones y alcance comprobados

- `origin/main`: `29aec3e5b2ad9b09460d8ec63b54cbd162d16bd1`.
- Contiene el commit previsto `1baa8a824f29b223e50e64e6a516f1697624d8f0`.
  El historial identifica su integración como PR #66; la instrucción recibida
  la refería como #65. Se comprobó ascendencia, sin reescribir historial.
- Rama candidata: `feat/fase-g-cierre-bootstrap`.
- Runtime conservado: `8a58db244ec1e9cd1a0d627b21e086548489e75d`, IACP 0.10.1,
  esquema `0011_crm_contract`.
- API/worker/lifecycle: `iacp/controlplane-api:fase-f2h-8a58db2`, imagen
  `sha256:8bfe4a02e4e38aadb738463309319f9dffc4e2465055bc37dd29ea9a055765a5`.
- IaC Worker: `iacp/terraform-worker:fase-f2h-8a58db2`, imagen
  `sha256:9da348a98ab299706aa0230fa70a9caf79b1dcd71af8cd3f8773d5f0e64d140b`.
- Módulo IaC vigente: `2c0f319ba5d7a45987cd9d0395673828c28f678b`, sin cambios.

El doctor del procedimiento vigente terminó PASS, sin rutas modificadas ni
inesperadas, con siete servicios operativos. Evidencia privada del host:
`/opt/iacp/evidence/fase-g-cierre-20261024T2350CR`.
El snapshot previo del 24/10/2026, 22:54:51, America/Costa_Rica, mostró cero
operaciones no terminales en las seis clases comprobadas, colas vacías y cero
mensajes sin reconocer. No se reiniciaron servicios del runtime.

## Implementación candidata

### Catálogo y admisión automática

Se conservan únicamente KVMHOST-54.1: kvm-1 (1/2048/50), kvm-2 (2/4096/100),
kvm-3 (4/8192/200) y kvm-4 (8/16384/400), expresados en vCPU/MiB/GiB.
La imagen inicial sigue siendo `ubuntu-2404`; no se modifica VM 29000.

La creación comercial recibe `plan_id`, `image_id`, `network`, `access`,
`start_on_create` y `commercial_context`. Rechaza tamaños libres, campos antiguos
y asignación de tenant desde JSON. Conserva Bearer opaco y `Idempotency-Key`.
No crea políticas por plan ni requiere aprobación humana individual.

La admisión verifica identidad, tenant, producto, imagen, perfil, límites,
reservas y capacidad del nodo/datastore elegido. Ceph requiere salud válida.
Sin evidencia o configuración válida, rechaza. Las reservas incluyen instancias
no eliminadas, también fallidas o inciertas; la estimación es conservadora y no
equivale a un planificador global del hipervisor. WAN estática sigue bloqueada.

Cada rechazo HTTP de creación queda registrado sin cuerpo sensible y no se
despacha. Una entrega de cola incierta **posterior a la admisión** se registra
por separado: no se afirma falsamente que no hubo ejecución. El reintento
conserva clave, cuerpo e identificadores.

Los planes verificados se ejecutan con autorización interna vinculada al
catálogo. Lifecycle y DELETE reutilizan controles de propiedad, locks y
autorización; recursos protegidos y bootstrap activo bloquean mutaciones.
Los controles históricos permanecen para las instancias históricas.

### Bootstrap G6

- La API valida y cifra inmediatamente con AES-256-GCM; clave independiente de
  32 bytes desde un archivo secreto operativo. Redis almacena ciphertext con
  TTL configurable de 60 a 3600 segundos (predeterminado 900).
- El contexto autenticado incluye tenant, instancia y referencia aleatoria.
  La cola contiene identificadores opacos; PostgreSQL conserva referencia,
  estado y especificación no sensible, nunca contraseña ni ciphertext nuevo.
- Terraform no recibe la contraseña. El worker fija la host key SSH obtenida
  mediante Guest Agent autenticado y verifica Cloud-Init antes de entregar el
  secreto por stdin a un programa fijo; no lo coloca en argumentos.
- La contraseña se configura para root en consola. SSH root permanece limitado
  a llave pública (`PermitRootLogin prohibit-password`), comprobado contra la
  configuración efectiva antes de aplicar la contraseña.
- Al completar se elimina la referencia Redis. Si falla la limpieza, queda
  registrada y el TTL acota la retención. No existe garantía de borrado físico
  de memoria de Python ni se afirma borrado de memoria del invitado.
- Si expira antes de iniciar, la misma intención autenticada puede restaurar
  el mismo secreto/referencia sin clonar otra VM. Si comenzó y se perdió la
  respuesta, queda incierto y solo se observa el marcador; no se repite la
  mutación a ciegas ni se usa una vía alternativa insegura.

La prueba física de contraseña de consola, llave SSH y filesystem sigue
pendiente. Las pruebas de confidencialidad son de código e integración aislada,
no acreditan todavía el flujo completo sobre una VM real del clúster MNG.

## Capacidad y cuota: BLOCKED

La identidad operativa existente permite ver `mng01` y `mng02` online, pero no
ofrece cifras CPU/RAM en el inventario consultado. El inventario de storage
resultó vacío; el estado de `DS_CYBERFUEL` y Ceph fue denegado por permisos.
No se elevaron ACL, no se buscaron identidades nuevas y no se aumentaron cuotas.

El perfil vigente limita cada VM a 40 GiB y el agregado a 80 GiB; bloquea los
discos de 50/100 GiB. Sus otros límites observados son 4 instancias,
4 vCPU/4096 MiB por VM, 8 vCPU/8192 MiB agregados y concurrencia 2.

[Propuesta inactiva](deployment/commercial-limits.mng.propuesta.json): pruebas
secuenciales kvm-1/kvm-2, una instancia y una operación simultánea, hasta
2 vCPU/4096 MiB/100 GiB. `tenants` está vacío deliberadamente. Requiere capacidad
documentada de nodo, datastore y Ceph, reserva y revisión del perfil antes de
activarla. Kvm-3/kvm-4 no tienen presupuesto de ejecución autorizado en MNG.

## Datos históricos sensibles

Auditoría de metadatos únicamente: 23 instancias, 13 registros históricos con
contraseña cifrada; 27 manifiestos, de los cuales 13 contienen la variable de
contraseña histórica, y 28 planes guardados. No se imprimieron valores ni se
leyeron states para este recuento. No se borraron datos ni evidencias.
El nuevo flujo no elimina el riesgo de artefactos históricos; su tratamiento
requiere revisión y alcance propio. No afirmar ausencia global de secretos.

## Migración y reversión

1. Mantener `IACP_CREATION_CONTRACT=historical` por defecto. La revisión
   `commercial-g` cambia de forma incompatible exclusivamente la solicitud
   comercial de creación en `/v1`; no activarla con consumidores antiguos.
2. Inventariar y migrar CRM y scripts. El repositorio externo
   `iacp-load-testing`, SHA observado `f0fe343`, conserva un E2E que envía
   tamaños y UUID de política; no se modificó ni se certificó migrado.
3. Aplicar en aislamiento la migración aditiva `0012_commercial_intents`:
   columna nullable y tabla nueva. No convierte filas históricas ni borra
   autorizaciones, operaciones o contraseñas históricas. La respuesta permite
   el contexto comercial nuevo sin inventar un `customer_id`.
4. Revisar los archivos operativos de límites y el
   [ejemplo mínimo de secretos](deployment/bootstrap-secrets.override.example.yaml).
   Es una plantilla no aplicada; no modifica Compose canónico. API y IaC Worker
   deben compartir la misma clave persistente fuera de Redis; el worker recibe
   su identidad SSH autorizada. No incluir claves en Git ni en imágenes.
5. Completar E2E secuenciales desechables de kvm-1/kvm-2: recursos, disco,
   partición/filesystem, DHCP, consola root, SSH, idempotencia, Lifecycle,
   DELETE y conciliación final. No aprovisionar con capacidad no comprobada.
6. Solo después de revisión, merge y aceptación: manifiesto con SHA/digests,
   inventory, respaldo, preflight, `iacpctl env-promote`/`go_live.py`, verify y
   pruebas HTTPS reales. Preservar persistencia, secretos e imágenes anteriores.

El rollback probado en aislamiento utiliza **código compatible con esquema
0012**, contrato histórico y admisión cerrada; mantiene visibles las filas G.
No se certificó ejecutar el binario antiguo `8a58db2` contra el esquema nuevo.
Se debe identificar el artefacto compatible de reversión antes de promover.
El downgrade destructivo está prohibido; rollback no deshace VMs ni state.
No volver a admitir solicitudes hasta conciliar operaciones en curso.

## Pruebas y dictámenes

- **PASS:** 980 pruebas, sin omitidas, con PostgreSQL y Redis descartables;
  30 advertencias de deprecación, 136,97 segundos.
- **PASS candidata:** Playwright Chromium escritorio/móvil y Firefox escritorio;
  sin errores JavaScript, dependencias externas ni peticiones mutadoras.
- **PASS:** manuales/enlaces/ejemplos y 31 contratos autenticados, sin red.
- **PASS:** contrato histórico conservado por defecto; diff explícito de revisión
  comercial y migración aditiva, con pruebas de concurrencia y confidencialidad.
- **BLOCKED:** capacidad/reserva verificable de MNG, configuración operativa de
  secretos y cuota candidata, migración integral de consumidores, E2E físico G.
- **NO EJECUTADO:** promoción G y certificación HTTPS del contrato G en VM 104.

Evidencias: [resultados](validation/phase-g/cierre/RESULTADOS.md),
[Playwright](validation/phase-g/cierre/ui/playwright.json).

Se mantiene el **GO histórico para piloto supervisado**, con su alcance previo.
Se mantiene **NO-GO para resiliencia integral** por certificación física del
worker pendiente. El cierre de código candidato no cambia ninguno de esos
dictámenes ni constituye **GO FASE G PARA PROMOCIÓN**.
