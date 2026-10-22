# Promoción del contrato y certificación histórica

Fuente certificada: **`9aeb8ec846342d279a5d16be7c8e20157e9bf5ae`**, merge de
[iac-proxmox PR #7](https://scm.cyberfuel.com/CyberFuel/iac-proxmox/pulls/7) en
`main`. Base Control Plane: `05ee32857201797de9f8c4df4e16d74d4dc9f8d6`, que
incluye [PR #19](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/19).
SHA-256 de `git archive` de la fuente:
`98f0900d571a8d05e4bbb0eabdb10550a372655f61fda35aaa3a601537daa94a`.

Los dos pins compilados usan ese SHA completo. La revisión
`40dc5d10387cfb50384ecd3ddddfd75289fe7bc4` sigue explícitamente rechazada como
propietaria de energía. Ninguna variable de entorno ni argumento HTTP puede
cambiar los pins. Lifecycle permanece deshabilitado por defecto. **Este PR no
certifica operativamente VM 106, no despliega ni ejecuta energía.**

## Compatibilidad sin reescribir el aprovisionamiento

Las instancias creadas desde la fuente promovida pueden acreditar su contrato
mediante su ejecución original y su Apply completed. Las históricas necesitan
además un `PowerOwnershipCertificate` explícito. Una revisión histórica diferente
de la conocida se rechaza; requeriría otra implementación revisada.

VM 106 conserva su `TerraformExecution.source_revision`, intento, hash del plan,
aprobación, resultados, timestamps y state originales. La certificación inserta
solo un certificado y un evento `power_ownership_certified` en su operación de
creación. No constituye otra creación, aprobación de Apply ni recuperación.

El certificado vincula tenant/instancia/destino, especificación, asignación,
state key, intento, plan original, aprobación y evidencia original mediante un
digest. Conserva actor, referencia de cambio, release/digest, versiones PVE/CLI/
provider, hash del plan nuevo y metadatos del state. No guarda credenciales,
configuración del invitado ni el contenido del state o plan.

La API exige ese vínculo antes de publicar. El worker lo revalida al reclamar y
justo antes de consumir el comando. Congela también el ID del certificado: otra
instancia/aprobación/revisión o cualquier cambio del vínculo bloquean el envío.
Las tareas ya consumidas siguen pudiendo observarse con la admisión deshabilitada.

`verify_artifacts`, Apply y reconcile mantienen el rechazo de planes de la
revisión anterior. Solo el certificador dispone de inspección histórica, limitada
al SHA legado conocido y sin una ruta de ejecución de ese plan.

## Qué verifica el certificador

`python -m iacp.lifecycle.certify --expectations <archivo>` valida sin escribir
SQL. Agregar `--record` registra la certificación únicamente después de aprobar
todas las comprobaciones. No existe endpoint de certificación ni tarea automática.
Es una acción del operador autorizado con acceso al servicio de mantenimiento.
Actor y referencia son su declaración auditada; no se deducen de una sesión HTTP.

1. Lifecycle debe estar en 0 y Apply en 1. Se obtiene el advisory lock ya usado por
   Terraform y filas bloqueadas de la instancia/ejecución/operación. Se exige
   creación completed, instancia ready/KVM, Apply consumido/completed, sin energía
   activa y coincidencia de todos los valores esperados por el operador.
2. Se verifica el plan histórico binario contra su SHA, manifest, backend e inputs
   congelados. Se lee su snapshot sellado sin init ni Apply. `show -json` se
   decodifica con el provider instalado en el workspace aislado del paso 5, sin
   credenciales reales ni inicialización del backend histórico.
   Se permiten manifests antiguos sin inventarles huellas nuevas.
3. Dos GET actuales de S3 acotan la comprobación: Version ID no nulo, lineage,
   serial, SHA de bytes y de contenido deben ser idénticos. Una reescritura ABA
   de bytes iguales cambia Version ID y bloquea. Se comprueban identidad/dirección,
   ausencia de recursos ajenos/tainted/deposed y postcondiciones originales.
4. La credencial dedicada Lifecycle verifica VM.Audit/VM.PowerMgmt mediante GET,
   versión PVE esperada, VMID/nodo/nombre, energía y ausencia de HA, locks, pausa
   y pending. Las dos observaciones Proxmox deben coincidir con el state.
5. Se extrae un archivo Git del SHA promovido, verificando también su SHA-256.
   En un directorio temporal privado se quita exclusivamente el backend S3 y se
   sustituye el endpoint/token del provider por valores sintéticos en loopback.
   Se copia el state solo ahí; no se toca el volumen de planes históricos.
6. Terraform 1.16.6 y bpg 0.116.1 con lockfile readonly ejecutan init local y
   `plan -refresh=false`. Se exige plan completo, no aplicable, un único recurso
   existente con `actions=["no-op"]`, sin reemplazo, acciones/deferred/drift,
   checks fallidos, valores desconocidos ni cambios de outputs; identidad y
   energía permanecen iguales. El hash del state local tampoco puede cambiar.
7. Antes del commit se comprueban de nuevo lock y vínculo histórico. Certificado
   y evento se confirman juntos; un replay válido devuelve el mismo certificado.
   Un fallo no inserta certificado. Los archivos temporales se eliminan al salir.

El plan offline certifica la configuración contra el state leído y la identidad/
energía observadas. No es un refresh de todos los atributos contra PVE. La
exclusión de escritores externos y la revisión de configuración siguen siendo
condiciones operativas. `running` tampoco certifica SO, Guest Agent, Cloud-Init
ni accesibilidad. El certificador no llama `submit`, Apply, Import ni Destroy.

## Preparar el despliegue autorizado en VM 104

Estos comandos son para una ventana futura autorizada, tras revisión y merge.
No se ejecutaron contra VM 104. Primero acreditar respaldo PostgreSQL y ensayo
de restauración conforme a [NEXT_GATES.md](NEXT_GATES.md), inventariar todos los
workers/escritores externos y drenar los antiguos. Resolver planes/autorizaciones
antiguos no terminales mediante el procedimiento aprobado; no aplicar ninguno
como paso de promoción. La aprobación completed de VM 106 permanece consumida.

Usar el reconciliador ya propietario del stack. Para Compose CLI, conservar el
proyecto, volúmenes externos, secretos, asignaciones y todos los overrides activos
de Terraform, tenants y TLS. El siguiente ejemplo presupone esos overrides:

```sh
cd /ruta/autorizada/iacp-controlplane
export IACP_RUNTIME_ENV=/ruta/segura/runtime.env
export COMPOSE_FILE=compose.yaml:compose.server.yaml:compose.terraform.yaml:compose.tenants.yaml:compose.tls.yaml:compose.lifecycle.yaml
# Si hay override de CA/egress o del sitio, anexarlo también a COMPOSE_FILE.
dc() { docker compose --env-file "$IACP_RUNTIME_ENV" -p iacp-controlplane "$@"; }
export IACP_TERRAFORM_APPLY_ENABLED=1
export IACP_KVM_LIFECYCLE_ENABLED=0
```

En `runtime.env`, fijar `IACP_RELEASE` al commit de Control Plane aprobado y
`IACP_TF_WORKER_IMAGE` al release Terraform correspondiente. Construir/revisar sus
digests; no reutilizar bootstrap ni tags con contenido cambiado. Configurar
`IACP_KVM_CREDENTIALS_PATH` fuera de Git: token dedicado con VM.Audit/PowerMgmt
solo en `/vms/106`, TLS verificado y visibilidad de sus tareas. Los secretos
Terraform existentes no se montan en Lifecycle Worker. El servicio opcional
`ownership-certifier`, perfil `certification`, incorpora ambos secretos solo
para la acción de mantenimiento y monta planes **readonly**. Su comando por
defecto muestra ayuda; un `up` normal no lo inicia.

Si PVE usa CA privada, conservar/agregar un override que monte su certificado
público readonly y configure `IACP_KVM_CA_FILE` en api, lifecycle-worker y
ownership-certifier, e `IACP_TERRAFORM_CA_FILE` en terraform-worker y
ownership-certifier. No desactivar TLS. Revisar `dc config` en almacenamiento
privado; contiene rutas y configuración del sitio, no publicarlo en Git.

Actualizar el clon IaC autorizado fuera del contenedor y comprobar, **en cada
worker Terraform**, el objeto Git requerido, archivo del recurso y checksum:

```sh
dc build api worker migrate terraform-worker lifecycle-worker
dc run --rm --no-deps --entrypoint git terraform-worker \
  -c safe.directory=/iac-source -C /iac-source show \
  9aeb8ec846342d279a5d16be7c8e20157e9bf5ae:modules/compute/kvm/main.tf
dc run --rm --no-deps --entrypoint git terraform-worker \
  -c safe.directory=/iac-source -C /iac-source archive \
  9aeb8ec846342d279a5d16be7c8e20157e9bf5ae | sha256sum
# Debe ser 98f0900d571a8d05e4bbb0eabdb10550a372655f61fda35aaa3a601537daa94a.
```

No volver a arrancar workers viejos. En la ventana de migración detener primero
la admisión externa y drenar API/workers; no interrumpir tareas nativas en curso.
El siguiente bloque presupone que el drenado terminó:

```sh
dc stop api worker terraform-worker lifecycle-worker
dc run --rm --no-deps migrate
dc up -d --no-deps api worker terraform-worker lifecycle-worker
dc exec -T api alembic current
dc exec -T api python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health/ready").status)'
```

Desde 0004 se ejecuta **0005 intacta**, seguida de **0006_power_ownership**.
Desde 0005 solo se agrega 0006. No hay backfill de certificados. La revisión
head/readiness es 0006; downgrade permanece prohibido. Comparar cantidades e
identificadores históricos antes/después con lecturas autorizadas y archivar
la evidencia. No usar SQL para cambiar revisiones, resultados ni aprobaciones.

Comprobar las imágenes realmente ejecutadas, colas, UID, montajes y flags:

```sh
dc images
dc exec -T terraform-worker python -c 'import os; from iacp.terraform.config import SOURCE_REVISION; assert os.environ["IACP_TERRAFORM_APPLY_ENABLED"] == "1"; print(SOURCE_REVISION)'
dc exec -T api python -c 'import os; from iacp.lifecycle.ownership import ownership_revision; assert os.environ["IACP_KVM_LIFECYCLE_ENABLED"] == "0"; print(ownership_revision())'
dc exec -T lifecycle-worker python -c 'import os; from iacp.lifecycle.ownership import ownership_revision; assert os.environ["IACP_KVM_LIFECYCLE_ENABLED"] == "0"; print(ownership_revision())'
dc exec -T lifecycle-worker celery -A iacp.lifecycle.worker:celery_app inspect active_queues
```

Exigir ambos pins iguales al SHA certificado y worker en cola `lifecycle`.
Verificar también Apply=1 en API, exclusión de VM 104, red/TLS/RBAC, aislamiento
de la credencial y ausencia de escritores antiguos. Mantener cerrado el acceso
de mutación mientras se certifica.

## Procedimiento exacto para VM 106

Identidad esperada del hito: instancia
`a1b2d063-6240-4969-bc04-e7e0e6be9e73`, intento **4**, aprobación
`1750ee70-a6ca-4036-a764-c4b077a0b588`, VMID **106**, nodo **mng01**, nombre
**iacp-kvm-lab-01**. El lineage de referencia es
`d954af88-6ae6-2d04-9ead-55ed1f95d644` y serial de referencia 3. Se deben leer
los valores actuales; no forzar números antiguos ni copiar un hash de este doc.

En la ventana autorizada y sin escritores externos, obtener por lectura la
evidencia esperada en un archivo privado. El siguiente comando NO certifica ni
modifica SQL, VM, artefactos o state; aborta si la identidad del hito no coincide.
Pasar actor, cambio aprobado y digest real del release del registro de releases:

```sh
umask 077
export CERT_ACTOR='<identidad del operador autorizado>'
export CERT_CHANGE='<referencia de la ventana/cambio aprobado>'
export CERT_RELEASE='<commit Control Plane; API y TF image@sha256:digests>'
dc run --rm --no-deps -T -e CERT_ACTOR -e CERT_CHANGE -e CERT_RELEASE \
  --entrypoint python ownership-certifier - > /ruta/segura/vm106-expectations.json <<'PY'
import json, os, tempfile
from pathlib import Path
from iacp.db import get_session_factory
from iacp.models import Instance, TerraformExecution, TerraformApply
from iacp.lifecycle.proxmox import ProxmoxPower
from iacp.terraform.config import Allocation
from iacp.terraform.executor import TerraformExecutor
from iacp.terraform.state import S3StateReader
with get_session_factory()() as session:
    i = session.get(Instance, 'a1b2d063-6240-4969-bc04-e7e0e6be9e73')
    e = session.get(TerraformExecution, i.id)
    a = session.get(TerraformApply, '1750ee70-a6ca-4036-a764-c4b077a0b588')
    assert (i.vmid, i.node, i.name, e.attempt, e.source_revision, a.status) == (
        106, 'mng01', 'iacp-kvm-lab-01', 4, '40dc5d10387cfb50384ecd3ddddfd75289fe7bc4', 'completed')
    assert a.instance_id == i.id and a.attempt == e.attempt and a.plan_sha256 == e.plan_sha256
    executor = TerraformExecutor(Path(os.environ['IACP_TERRAFORM_SOURCE_DIR']), Path(os.environ['IACP_TERRAFORM_ARTIFACTS_DIR']))
    with tempfile.TemporaryDirectory() as directory:
        reader = S3StateReader(Allocation.model_validate(e.allocation).backend.arguments(e.state_key), executor.environment(Path(directory)))
        try:
            metadata = reader.read(lambda: False).metadata()
        finally:
            reader.close()
    version = ProxmoxPower.configured()._request('GET', '/version')['version']
    print(json.dumps(dict(instance_id=i.id, tenant_id=i.tenant_id, approval_id=a.id,
        attempt=e.attempt, vmid=i.vmid, node=i.node, name=i.name,
        source_revision=e.source_revision, plan_sha256=e.plan_sha256, state_key=e.state_key,
        state=metadata, pve_version=version, actor=os.environ['CERT_ACTOR'],
        change_reference=os.environ['CERT_CHANGE'], release_reference=os.environ['CERT_RELEASE']), indent=2))
PY
```

Revisar ese archivo contra el inventario, tenant, aprobación y evidencia PVE/
Wasabi independientes. Si los valores cambiaron desde su lectura, el comando
de certificación abortará. No editar la DB ni el state para hacerlos coincidir.

```sh
dc run --rm --no-deps -T \
  -v /ruta/segura/vm106-expectations.json:/run/config/ownership.json:ro \
  ownership-certifier python -m iacp.lifecycle.certify \
  --expectations /run/config/ownership.json > /ruta/segura/vm106-check.json
# Revisar validated=true, recorded=false, no-op, invariancia y versiones.
# Tras aprobar esa evidencia, repetir todos los controles y registrar:
dc run --rm --no-deps -T \
  -v /ruta/segura/vm106-expectations.json:/run/config/ownership.json:ro \
  ownership-certifier python -m iacp.lifecycle.certify \
  --expectations /run/config/ownership.json --record > /ruta/segura/vm106-certificate.json
```

Exigir `recorded=true`, ID de certificado y evento `power_ownership_certified`
con actor correcto. Archivar ambos informes, checksums de fuente e imágenes,
versión PVE desplegada, permisos, ausencia de pending/HA y ventana aprobada.
El intento 4, aprobación y resultados originales deben permanecer iguales.
Antes de activar, GET `/power` debe seguir indicando mutaciones deshabilitadas.

## Activación y primer encendido mediante API

Solo después de completar la certificación y autorizar la ventana de energía:

```sh
export IACP_TERRAFORM_APPLY_ENABLED=1
export IACP_KVM_LIFECYCLE_ENABLED=1
dc up -d --no-deps --force-recreate api lifecycle-worker
dc exec -T api python -c 'import os; assert os.environ["IACP_TERRAFORM_APPLY_ENABLED"] == "1"; assert os.environ["IACP_KVM_LIFECYCLE_ENABLED"] == "1"'
dc exec -T lifecycle-worker python -c 'import os; assert os.environ["IACP_KVM_LIFECYCLE_ENABLED"] == "1"'
```

Persistir esos valores también en el entorno del reconciliador para que una
recreación futura no los cambie. Usar el endpoint HTTPS oficial desde el origen/IP
del CRM autorizado. El archivo de cabecera, modo 0600, debe contener
`Authorization: Bearer <token operator del tenant>`; no imprimir el token:

```sh
export IACP_AUTH_HEADER_FILE=/ruta/segura/operator.header
export IACP_URL=https://iac.cyberfuel.com
export INSTANCE_ID=a1b2d063-6240-4969-bc04-e7e0e6be9e73
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" \
  "$IACP_URL/v1/instances/$INSTANCE_ID/power"
# Exigir stopped, mutations_available=true, blocked_reason=null.
# Elegir y conservar una sola clave para esta solicitud:
export START_KEY='vm106-first-start-<identificador-de-la-ventana>'
curl --fail-with-body --silent --show-error -X POST \
  -H @"$IACP_AUTH_HEADER_FILE" -H "Idempotency-Key: $START_KEY" \
  "$IACP_URL/v1/instances/$INSTANCE_ID/power/start" \
  > /ruta/segura/vm106-first-start.json
export OPERATION_ID='<operation_id recibido en el 202>'
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" \
  "$IACP_URL/v1/operations/$OPERATION_ID"
curl --fail-with-body --silent --show-error -H @"$IACP_AUTH_HEADER_FILE" \
  "$IACP_URL/v1/instances/$INSTANCE_ID/power"
```

Usar `CURL_CA_BUNDLE` si la CA corporativa lo exige; nunca `-k`. Añadir la
cabecera Origin del CRM si la política del sitio la requiere. Exigir completed,
un solo `power_completed`, energía running y `guest_ready=null`. Contrastar
tarea/auditoría PVE, ausencia de otra creación/Apply y metadatos Wasabi antes/
después por lectura: Version ID, lineage, serial y hashes no deben cambiar.
Lifecycle no recibe secretos ni acceso al backend Terraform.

Ante timeout/publicación ambigua, conservar la misma clave, acción e IDs; repetir
solo esa solicitud para observar, nunca generar otra clave ni encadenar shutdown/
reboot. Un UPID perdido requiere investigación auditada. No apagar automáticamente
para deshacer la prueba, ni usar stop/reset/force.

## Rollback compatible con 0005 y 0006

```sh
export IACP_TERRAFORM_APPLY_ENABLED=1
export IACP_KVM_LIFECYCLE_ENABLED=0
dc up -d --no-deps --force-recreate api lifecycle-worker
```

Mantener el release de esta promoción (o un correctivo compatible con 0006),
ambos pins promovidos y `ignore_changes`. No hacer downgrade de 0005/0006,
restaurar/borrar DB o Wasabi, ni desplegar PR #18 o #19 sobre el esquema head:
readiness/modelos no son compatibles con 0006, y PR #19 usa la fuente antigua.
La operación de rollback es funcional por flag, no una devolución de propiedad
a Terraform. La certificación y auditoría se conservan para la investigación.

Antes de recrear workers, dejar que terminen tareas activas; si hubo interrupción,
continuar observación de UPID conocidos con el flag 0 mediante redelivery/replay
de la misma clave. `uncertain` desconocidos conservan su reserva. No liberar
reservas con SQL ni reiniciar energía para resolver incertidumbre. Conservar
Apply=1 y drenado de escritores antiguos también durante rollback.

## Pruebas y límites

[POWER_OWNERSHIP_VALIDATION.json](validation/POWER_OWNERSHIP_VALIDATION.json)
registra resultados, versiones, hashes y evidencia. Reproducir la suite con
`bash scripts/test-integration.sh`: Compose aislado, PostgreSQL 17, Redis 7 y
Celery reales, HTTPS/S3 loopback y datos sintéticos. La integración histórica
stubbea únicamente el plan offline bpg; su política/orquestación se prueba por
separado, junto con la matriz del provider real del SHA IaC certificado.

No se han ejecutado Apply, Import, Destroy, operaciones reales de energía,
certificación real de VM 106 ni despliegue. No se accedió al PostgreSQL empresarial
ni a Wasabi. La certificación operativa y activación siguen pendientes de la
ventana y las verificaciones descritas arriba.
