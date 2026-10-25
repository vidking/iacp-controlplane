# Fase G: preparación final y controles previos a promoción

Fecha: 25/10/2026, America/Costa_Rica. **NO-GO FASE G.**

Actualización de admisión: Infraestructura autorizó después la identidad
`iacp-admission@pve!capacity-ro`. Se implementó su separación en esta misma rama,
sin reutilizar la credencial Terraform. Consultar el
[informe específico de admisión](FASE_G_ADMISION_CAPACITY_RO.md): todavía no se
localizó su secreto operativo y no se atribuyen HTTP 200 reales a esa identidad.
Los rechazos de la identidad anterior se conservan como diagnóstico histórico.

La capacidad de almacenamiento de MNG se considera confirmada por Infraestructura
según la autorización del responsable. No se repitió una auditoría integral de
Ceph. La promoción sigue condicionada a controles efectivos, secretos y clientes
compatibles; la autorización no permite omitirlos.

## Versiones y estado de ejecución

| Elemento | Evidencia |
|---|---|
| Main integrado | `3e6d0f9923dbd926d379cf379900e49f8ffc6d08` |
| Candidata incluida | `9ce19badd14338e224f57a6e1466f9cb7dcd232f` |
| Merge observado | PR #68; la instrucción lo identificaba como #67 |
| SHA ejecutado conservado | `8a58db244ec1e9cd1a0d627b21e086548489e75d` |
| Esquema ejecutado | `0011_crm_contract` |
| Esquema del código G | `0012_commercial_intents`, migración aditiva |
| Rama de preparación posterior | `feat/fase-g-preparacion-promocion` |
| Promoción | **NO EJECUTADA: comprobaciones previas bloqueadas** |
| E2E kvm-1 / kvm-2 | **BLOCKED, no aprovisionados** |

Se comprobó ascendencia del commit candidato. No se confunde el merge con un
despliegue. API y workers conservan imágenes `fase-f2h-8a58db2`, PostgreSQL y Redis
saludables, y Traefik operativo. Doctor PASS, sin diferencias ni cambios de rutas,
con los siete servicios activos. Evidencia privada del host:
`/opt/iacp/evidence/fase-g-preparacion-20261025T0005CR`.

## Cuotas de prueba preparadas

La VM 106 continúa registrada como instancia histórica protegida y consume
2 vCPU, 2048 MiB y 20 GiB. No se la elimina ni se excluye del cómputo para aprobar
una prueba. No había operaciones concurrentes en el uso observado.

Se creó un archivo candidato nuevo, sin sobrescribir ni activar el perfil actual:
`/etc/iacp/terraform/profiles-comercial-g-20261025.candidate.json`, SHA256
`314627240877aea09b145ff6d49edb0c43d0dc9e051cde9a456b9879205c7ec1`.
Validado mediante el modelo `Profile` disponible en el runtime. Conserva tenant,
imagen 29000, origen, nodo y datastore `DS_CYBERFUEL`, DHCP y llaves públicas.
No modifica el perfil root histórico ni identidades.

| Límite | Perfil lab-dhcp candidato | Tenant completo candidato |
|---|---:|---:|
| Instancias | 1 nueva en ese perfil | 2, incluida VM 106 |
| vCPU | 2 | 4 |
| RAM MiB | 4096 | 6144 |
| Disco GiB | 100 | 120 |
| Operaciones simultáneas | 1 | 1 |
| Planes nuevos autorizados | kvm-1 y kvm-2 | kvm-1 y kvm-2 |

Las comprobaciones aisladas admiten secuencialmente 50 y 100 GiB, preservan la
reserva histórica y rechazan una segunda VM de prueba. Kvm-3/kvm-4 permanecen
definidos, sin presupuesto de ejecución. No se activaron estos límites porque
todavía fallan los controles anteriores a promoción. La configuración activa
sigue en 40 GiB por VM; no se presenta la candidata como cuota efectiva.

[Configuración preparada y límites](deployment/commercial-limits.mng.preparada.json).

## G6: material y persistencia

Se generó exclusivamente una clave AES nueva de 32 bytes, fuera de Git, en
`/etc/iacp/identity/commercial-bootstrap-aes.key`, propietario root, grupo 10001,
modo 0440. No se imprimió, exportó ni sustituyó la clave histórica. Su existencia
no demuestra todavía el bootstrap físico ni su disponibilidad dentro de los
contenedores activos: no se modificaron sus montajes.

No se localizó una identidad privada SSH de bootstrap en los directorios
operativos revisados de VM 104. Los perfiles contienen llaves públicas; eso no
acredita la disponibilidad de la privada en el worker. No se extrajeron claves
privadas de estaciones personales, no se inventó una identidad y no se cambió
el canal SSH autenticado por otra vía.

Redis real está configurado con AOF, `appendfsync everysec` y snapshots
`3600 1 300 100 60 10000`. Por tanto, TTL significa caducidad lógica, **no borrado
físico inmediato** de ciphertext en AOF, snapshots o respaldos. La clave AES debe
permanecer separada, restringida y preservada durante recuperación. No se
deshabilita persistencia de las colas compartidas para ocultar esta condición.

Se reprodujo esa persistencia en Redis descartable: reinicio con AOF todavía
habilitado, lectura autenticada del ciphertext restaurado, TTL de 120 a 97 s,
caducidad posterior y error de secreto ausente. Escaneo de RDB/AOF sin contraseña
señuelo. Se restauró la configuración del Redis de pruebas; el Redis de VM 104
no se reinició ni recibió contraseñas de prueba.

La regresión G6 comprueba confidencialidad de señuelos en DB, cola, artefactos,
logs y errores, entrega SSH por stdin, expiración y reconciliación. No equivale
a revisar o sanear states históricos ni a comprobar contraseña de consola en
una nueva KVM física. Esas pruebas permanecen pendientes.

## Bloqueos concretos de promoción

1. **Admisión sin lecturas físicas autorizadas.** La identidad ya montada en la
   API devuelve `POWER_PERMISSION_DENIED` al consultar `/nodes/mng01/status` y
   `/nodes/mng01/storage/DS_CYBERFUEL/status`. No se consultó de nuevo todo Ceph.
   El código G exige esas lecturas para admitir; la confirmación externa de
   capacidad no se convirtió en un bypass. No se modificaron ACL o tokens.
   La identidad Terraform existente sí pudo obtener ambas métricas; no se la
   copió a la API ni se ampliaron sus secretos para resolverlo a ciegas.
   A las 00:19:25 se observaron 48 CPU lógicas, 189834575872 bytes de RAM libre y
   `DS_CYBERFUEL` rbd activo, con 11627464228864 bytes disponibles. Esto valida
   holgura para los tamaños secuenciales aprobados, sin certificar toda la salud
   Ceph ni resolver el rechazo de la identidad que usa la admisión HTTP.
2. **SSH de bootstrap no disponible en el worker.** Falta un archivo operativo
   autorizado que corresponda a una llave pública del perfil. La clave AES ya
   preparada no sustituye esa identidad.
3. **Cableado G6 faltante en main.** La revisión integrada no conectaba el
   selector comercial, AES/TTL y SSH al Compose canónico, ni el perfil al
   Lifecycle Worker. Se prepara un
   cambio mínimo y verificable, sin nuevos servicios, puertos ni redes.
4. **Preflight de respaldo 0011 rechazado.** El mapa del driver integrado
   omitía `0011_crm_contract` tras mover HEAD a 0012. Se corrige mediante
   validación comprobada del esquema, digest, restauración aislada y tablas;
   no se inventa una atestación ni se deshabilita el validador.
5. **Cliente E2E histórico incompatible.** La versión local real de
   `iacp-load-testing/e2e.py` todavía envía tamaños y política UUID y comprueba
   SSH root por contraseña. No debe ejecutarse contra G. Su incompatibilidad
   se registra expresamente; no se afirma que haya CRM externos migrados.

## Cambios de preparación en revisión

- Registro interno `ubuntu-2604 → 29001`, estado pendiente. Solo se registra
  la identidad comunicada: sin inventar nodo, storage o capacidades no
  certificadas. No se puede resolver para crear y no aparece como habilitada
  en el catálogo público. Ubuntu 24.04/29000 permanece igual. Revisión
  determinista del catálogo `KVMHOST-54.2`; los cuatro planes no cambian.
- Cableado mínimo G6 en los nueve servicios existentes, sin agregar servicios:
  AES para API/IaC Worker, SSH solo IaC Worker y perfil de solo lectura para
  Lifecycle. La observación de bootstrap reutiliza las credenciales Terraform
  existentes mediante GuestClient; no se amplían montajes de credenciales KVM.
- Selector histórico por defecto; un manifiesto explícito debe revisar
  contrato, rutas y TTL. El comparador rechaza otros cambios, rotación implícita,
  TTL fuera de límite y montajes incorrectos. No se amplía una lista genérica
  de deriva permitida.
- Preflight y `env-promote` comprueban material G6 antes de detener API o
  reemplazar el entorno: 32 bytes AES, permisos, parseo SSH y coincidencia de
  llave pública con el perfil dentro de una comprobación aislada sin red.
- Corrección de la atestación de restauración y pruebas de regresión.

Estos cambios necesitan revisión y merge; no se desplegó una rama no integrada.
No se inició `env-promote` ni `promote` con comprobaciones fallidas. No se emitió
un registro de cambio que fingiera exclusión de escritores, respaldo nuevo o
verificaciones aprobadas.

## Consumidores y Swagger

[Inventario de consumidores reales inspeccionados](contracts/consumidores-fase-g-20261025.json).
Las pruebas históricas se conservan como regresión del selector histórico. El
manual y Request Body comercial de la candidata usan `plan_id`, no tamaños.
No se identificó un CRM comercial desplegado en los repositorios locales; no
se inventa uno ni se afirma ausencia global de consumidores externos.

HTTPS real con identidades existentes: `/docs` y `/openapi.json` Basic 200,
OpenAPI 3.1 y `InstanceCreate` histórico; Basic sobre API 401; Bearer válido sobre
`/v1/profiles` 200; Bearer inválido 401. `/v1/plans` y `/v1/images` son 404 en el
runtime histórico. No se presentan como endpoints G desplegados.

Playwright conserva por separado las capturas de la candidata local y el
baseline HTTPS, escritorio/móvil. No hubo errores JavaScript, recursos externos
ni solicitudes mutadoras. No se ejecutó Try it out destructivo.

## Ruta de promoción y reversión pendiente

Tras resolver los bloqueos: verificar main final, migrar/inhibir clientes
incompatibles, comprobar material montable y permisos de lectura, capturar
snapshot, operaciones/colas/locks y respaldar PostgreSQL con restauración
aislada nueva. Registrar el cambio autorizado con evidencias auténticas.
Aplicar el entorno revisado con `iacpctl env-promote`; ejecutar el driver
`go_live.py` mediante `iacpctl`, migración aditiva y verify. Confirmar SHA de
los contenedores, esquema, autenticación, workers y HTTPS antes de admitir.

La reversión tras 0012 necesita un artefacto compatible con 0012, admisión
cerrada y contrato histórico; el binario antiguo no se da por compatible.
Preservar claves, PostgreSQL, Redis, volúmenes y states; no usar downgrade
destructivo ni borrar filas para recuperar readiness. Si se aborta antes de
migrar, como ocurrió aquí, el runtime anterior permanece recuperable e intacto.

E2E obligatorio, aún no ejecutado: crear kvm-1, verificar disco/partición/FS,
DHCP, consola root y llave SSH, Lifecycle, DELETE y limpieza; solo después
repetir con kvm-2. Ante incertidumbre detener expansión y conciliar, sin forzar
locks ni clonar otra VM para ocultar un fallo.

## Dictamen

Regresión final de la candidata con identidad RO: **1012 pruebas aprobadas**,
cero omitidas, 30 advertencias de deprecación, 348,36 s. Playwright de candidata
y baseline HTTPS conservados por separado; 22 manuales/31 contratos validados.
Los snapshots OpenAPI y schemas comerciales son idénticos a main `3e6d0f9`.

**NO-GO FASE G**: no hay promoción ni E2E físico certificado. No se crearon
instancias, no se modificaron VM 104/106/29000/29001 desde la API ni se operó
producción. Se conserva el GO histórico para piloto supervisado y el NO-GO de
resiliencia integral; ambos mantienen su alcance previo.

[Resultados y capturas](validation/phase-g/promocion-20261025/RESULTADOS.md).
