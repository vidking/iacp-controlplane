# Fase G: políticas comerciales, capacity-ro y cuatro Golden Images

Fecha: 25/10/2026, America/Costa_Rica.

## Políticas efectivas y límites encontrados

Main comprobado: `d8ac23e5bfd9d210a3e2f1eec24a1255fb6f7db6`; contiene
`430b15aedb3aa16505f2ecfa5b9aaf17fbf4f035`. El historial registra el merge del
trabajo referido como PR #69 mediante PR #70. No se reescribió historial.

Runtime conservado: `8a58db244ec1e9cd1a0d627b21e086548489e75d`, IACP 0.10.1,
esquema `0011_crm_contract`. El código candidato mantiene la migración aditiva
`0012_commercial_intents`, sin migraciones nuevas ni cambios a states.

Los cuatro productos permanecen exactos: kvm-1 1/2048/50, kvm-2 2/4096/100,
kvm-3 4/8192/200, kvm-4 8/16384/400, en vCPU/MiB/GiB. CPU/RAM/disco libres y
VMIDs recibidos desde CRM siguen prohibidos. La autenticación opaca, tenant
activo, rol de escritura, derecho explícito por tenant/plan, imagen habilitada,
red, capacidad, idempotencia, locks y protección de recursos son acumulativos.

Se encontró una contradicción: la reserva de G y la disponibilidad heredaban
los límites del perfil de la vía antigua (4 vCPU/4096 MiB/40 GiB y acumulados
8 vCPU/8192 MiB/80 GiB). La candidata separa esas comprobaciones:

- La vía histórica conserva sus límites y evidencias.
- G exige coincidencia exacta con un plan y presupuesto explícito del tenant,
  incluso dentro de la reserva interna; no hereda ni reduce tamaños por esos
  topes históricos. Conserva autorizaciones de imagen, red, nodo, datastore,
  VMIDs de clones y prohibición de reducir disco.
- La disponibilidad G aplica la misma separación y no anuncia límites
  contradictorios de la vía antigua.
- El piloto sigue permitiendo únicamente kvm-1/kvm-2 al tenant existente
  `00000000-0000-0000-0000-000000000001`. Presupuesto total: dos instancias,
  4 vCPU, 6144 MiB, 120 GiB y una operación concurrente. Incluye VM 106:
  una instancia, 2 vCPU, 2048 MiB y 20 GiB. Solo queda presupuesto para una
  prueba nueva secuencial, nunca para dos KVM simultáneas.
- Celery conserva sus colas y límites de ejecución. No se añadió un scheduler
  ni una aprobación por distribución. La cuota de admisión sigue rechazando
  solicitudes fuera del presupuesto o de la concurrencia admitida; el tamaño
  contratado nunca se modifica para sortear el rechazo.

La configuración candidata nueva no se activó. Ubicación administrativa:
`/etc/iacp/terraform/profiles-comercial-g-cierre-20261025.candidate.json`, SHA256
`a4695827171040e822e19663dc136b7b7986c66d849921997b620500e52f9b58`.
Sus techos de producto reflejan 8 vCPU/16384 MiB/400 GiB; esos valores no otorgan
derechos: los límites de tenant anteriores siguen siendo los que autorizan G.
El perfil root histórico permanece intacto.

## Correcciones realizadas y catálogo de imágenes

Registro versionado `KVMHOST-54.3`. Inventario de solo lectura comprobado:

| image_id | VMID interno | Nombre Proxmox | Origen autorizado | Estado IACP |
|---|---:|---|---|---|
| ubuntu-2404 | 29000 | iacp-ubuntu-2404-golden | mng01 / DS_CYBERFUEL | Habilitada por certificación histórica |
| ubuntu-2604 | 29001 | iacp-ubuntu-2604-golden | mng01 / DS_CYBERFUEL | Pendiente de integración |
| almalinux-9 | 29010 | iacp-almalinux-9-golden | mng01 / DS_CYBERFUEL | Pendiente de integración |
| almalinux-10 | 29011 | iacp-almalinux-10-golden | mng01 / DS_CYBERFUEL | Pendiente de integración |

Las cuatro están detenidas y marcadas `template=1`, sin lock, con disco
Cloud-Init y `agent=enabled=1`. No se modificaron ni reconstruyeron.
La configuración de Guest Agent y la presencia del disco no prueban su
funcionamiento dentro de un guest arrancado.

La candidata expone las cuatro mediante `GET /v1/images`, conserva VMID/nodo/
datastore internos y rechaza las tres pendientes para creación. Estas muestran
`available=false`, `authorized=false`, motivo `IMAGE_CERTIFICATION_PENDING`,
capacidades y requisitos todavía no verificados como `null` y compatibilidad
de planes vacía. No se inventan mínimos, contraseñas ni resultados por OS.
Los cuatro planes siguen completamente separados del sistema operativo.

Swagger incorpora las cuatro en la descripción, ejemplo de respuesta y manual
local, con estados claros. Se amplía el schema de lectura `ImageResponse` para
representar capacidades desconocidas mediante null; los campos JSON de creación
comercial no cambian. Los ejemplos conservan null explícitos y validan contra
OpenAPI 3.1. No se publica un ejemplo ejecutable que seleccione una pendiente.

No se habilitaron Ubuntu 26.04 ni AlmaLinux. El inicializador y bootstrap
actuales están acotados explícitamente a Ubuntu 24.04; no deben aplicarse a las
otras distribuciones sin su adaptación específica y pruebas de integración.
Arranque, DHCP, QGA funcional, root, SSH y filesystem en clones de esas imágenes
no se probaron porque la admisión completa sigue bloqueada. Es una falta de
certificación, no evidencia inventada de fallo dentro de esos invitados.

## Resultado HTTP real de capacity-ro

Se reutilizó el secreto instalado, sin imprimirlo ni modificar su modo 0600 o
propietario root. Se preparó una referencia independiente JSON, root:10001/0440,
en `/etc/iacp/admission/capacity-ro.json`, para el montaje de solo lectura ya
versionado en API. No contiene ni reutiliza la identidad Terraform.

`mng01.cyberfuel.com` es el FQDN corporativo ya usado por la configuración
operativa existente y resuelve a `192.168.1.113`. TLS 1.3 validó nombre y cadena
con el trust store: SAN `*.cyberfuel.com`/`cyberfuel.com`, emisor Sectigo Public
Server Authentication CA DV R36. No se usó el nombre corto ni TLS inseguro.

| Consulta con iacp-admission@pve!capacity-ro | HTTP real |
|---|---:|
| GET /access/permissions | 200, privilegios de auditoría |
| GET /nodes/mng01/status | 200 |
| GET /nodes/mng01/storage/DS_CYBERFUEL/status | 200, datastore activo |
| GET /nodes/mng01/ceph/status | **403** |
| PUT vacío /nodes/mng01/config, tras verificar ausencia de escritura | **403**, escritura denegada |

Las ACL efectivas de esta identidad aparecen en `/nodes/mng01` y
`/storage/DS_CYBERFUEL`, no en `/`. El esquema servido por MNG 9.2.20 exige
`Sys.Audit` o `Datastore.Audit` en `/` para el alias Ceph. No se concedió esa ACL,
no se cambió el token y no se retiró la comprobación `HEALTH_OK` de admisión.
El rechazo es un bloqueo real, no una falla de DNS/TLS ni falta de disco.

El cliente candidato también se ejecutó en un proceso descartable en VM 104,
UID 10001 y con solo el secreto RO para esas consultas: nodo/datastore 200,
Ceph bloqueado. Eso verifica lectura del montaje y TLS del contenedor, pero no
equivale a una promoción. El inventario de plantillas utilizó la observación
autorizada del worker; no se atribuyen esas lecturas a capacity-ro.

## Estado del bootstrap G6

Se creó en VM 104 la identidad técnica Ed25519 dedicada:
`/opt/iacp/stacks/controlplane/secrets/guest_bootstrap_ed25519`, root:10001/0440.
No se copió a Proxmox ni a Git. El perfil comercial candidato contiene únicamente
su pública para el usuario técnico Ubuntu, sin llaves personales del administrador.
Las llaves opcionales del cliente siguen siendo independientes para root.

Preflight aislado PASS bajo UID 10001: clave AES operativa de 32 bytes, prueba
AES-GCM solo en memoria, parseo Ed25519 y coincidencia pública con el perfil.
No se imprimió clave, contraseña ni ciphertext. Los montajes operativos activos
todavía no se cambiaron. No se autenticó un nuevo guest ni se configuró root en
una VM real; G6 físico sigue pendiente. Las pruebas de confidencialidad de código
no sustituyen esa comprobación.

## Pruebas y consumidores

Los resultados de regresión y navegador se conservan en
[evidencias](validation/phase-g/cierre-imagenes-20261025/RESULTADOS.md).
Regresión completa: **1021 aprobadas**, cero omitidas, 30 advertencias de
deprecación, 272,86 s. Playwright Chromium escritorio/móvil y Firefox PASS,
con las cuatro filas visibles y cero errores JavaScript, recursos externos o
mutaciones. El manual Markdown y sus ejemplos también se validaron.
Incluyen los cuatro tamaños con derechos explícitos de pruebas y con topes
históricos presentes; no se aprovisionan físicamente kvm-3/kvm-4.

El cliente real `iacp-load-testing/e2e.py` sigue incompatible con G (tamaños y
política UUID; SSH root por contraseña); permanece identificado y no se ejecutó.
Swagger y los ejemplos comerciales usan plan_id. La lista de imágenes ahora
contiene registros pendientes con campos null: los consumidores deben atender
estado/available y no interpretar la presencia en el catálogo como autorización.
No se inventaron consumidores CRM ni se convirtieron instancias históricas.

## SHA, promoción, E2E y bloqueos restantes

Rama candidata: `feat/fase-g-cierre-ejecucion`, basada en main `d8ac23e`.
El SHA final se identifica por el commit de la rama; no se crea tag
ni se declara una imagen ejecutada distinta de la verificada.

Al cerrar evidencias, Forgejo SSH en `192.168.3.110:2222` rechazó conexiones
tanto desde la estación como desde VM 104 (TCP 111). El API HTTPS consultado
devolvió 403. La actualización final de refs y la publicación no pudieron
confirmarse; `d8ac23e` es la última referencia remota verificada. No se inventa
un PR publicado ni se modifica Forgejo para completar este trabajo.

**Promoción: NO EJECUTADA. E2E kvm-1/kvm-2: BLOCKED, no ejecutados.**
No se presenta readiness del runtime antiguo como GO del contrato nuevo.
No se crearon VMs desechables ni se dejaron huérfanas. VM 104, VM 106 y las
cuatro plantillas no se mutaron mediante aprovisionamiento, Lifecycle o DELETE.

Bloqueos concretos: autorización de la lectura Ceph requerida con capacity-ro;
revisión/merge del correctivo; promoción con respaldo/restauración y rollback
compatible con esquema 0012; certificación G6 y E2E secuenciales de KVM 1/2;
adaptación/certificación en clones antes de habilitar las otras imágenes.
No se utiliza el binario 8a58db2 contra esquema 0012 sin compatibilidad demostrada.
El release estable permanece recuperable y sin migración aplicada.

Comprobación final del 25/10/2026, 01:45:44, America/Costa_Rica: esquema 0011,
seis clases no terminales vacías, colas vacías, unacked=0, readiness saludable.
Doctor PASS, siete servicios activos y ninguna diferencia de configuración.
Evidencia privada: `/opt/iacp/evidence/fase-g-cierre-imagenes-20261025-doctor`.
API conserva la imagen `sha256:8bfe4a02e4e38aadb738463309319f9dffc4e2465055bc37dd29ea9a055765a5`,
ligada al release 8a58db2 por el manifiesto verificado; no tiene etiqueta OCI de
revisión y no se inventó esa metadata.

**NO-GO FASE G.** Se conservan GO histórico para piloto supervisado y NO-GO de
resiliencia integral, con sus alcances previos.
