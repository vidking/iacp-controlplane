# Compatibilidad y conciliación de planes históricos

## Incidente y causa raíz

| Dato comunicado | Valor |
|---|---|
| Instancia | `a1b2d063-6240-4969-bc04-e7e0e6be9e73` |
| Autorización | `5d38691f-f7fb-41c6-a0e8-6944cddf8ab7` |
| Intento histórico | 2 |
| SHA-256 del plan | `8193a6eef8972fc2aded60e6ff5802d0cb28a02e891240e839b1c3555b831bd5` |
| Apply original | `uncertain` / `TERRAFORM_COMMAND_FAILED` |
| Recuperación en `8558f08` | `PLAN_INTEGRITY_FAILED` |
| Backend remoto | Serial 1; cero recursos |
| Proxmox | VMID 106 ausente |
| API y worker | `IACP_TERRAFORM_APPLY_ENABLED=0` |

El historial prueba que `06c0190e76ffdf51ba48b7d17c28a3efdbb8bcee`, integrado
en PR #14 y en `8558f08`, introdujo `execution_context_salt` y
`execution_context_sha256`. El padre de ese PR, `a97c5ac`, no los escribía.
Por tanto, esos campos son posteriores a un intento 2 generado con el código
anterior al PR #14. La fecha exacta del intento debe corroborarse con su registro
y artefacto originales: esta corrección no accedió al servidor ni inspeccionó el
manifest del incidente. Los valores de la tabla son evidencia comunicada, no
una comprobación nueva de infraestructura.

`recover` llamaba a `prepare_apply`, que accedía obligatoriamente a esos campos.
Su ausencia producía `KeyError`, convertido en `PLAN_INTEGRITY_FAILED`, incluso
con un plan binario íntegro. Este fallo de compatibilidad no demuestra corrupción
del plan ni explica el error original de Terraform. El resultado incierto original
no permite atribuir una fase o causa que no quedó registrada.

Comprobación reproducible, sin red:

```sh
git log -S execution_context_salt -- app/iacp/terraform/executor.py
git show a97c5ac:app/iacp/terraform/executor.py
git show 06c0190 -- app/iacp/terraform/executor.py
```

## Esquemas y garantías

Ambos manifests son objetos JSON sin un campo explícito de versión:

| Esquema | Campos |
|---|---|
| Anterior a PR #14 | `sha256`, `source_revision`, `state_key`, `variables`, `backend`, `summary` |
| Desde PR #14 | Los seis anteriores más `execution_context_salt` y `execution_context_sha256` |

La conciliación acepta ambos esquemas. Verifica el SHA-256 del binario contra
la autorización y el manifest, revisión fijada del módulo, intento, state key,
asignación, backend y variables, incluidos sus archivos locales. Una pareja de
campos de contexto incompleta o mal formada se rechaza. Nunca modifica el
manifest, rellena huellas retrospectivas ni omite el hash del plan.

Una mutación nueva exige además la huella original completa y coincidente con
credenciales, endpoint y CA. Un plan histórico sin huella no puede aplicarse.
La conciliación no exige la identidad secreta original: permite credenciales de
lectura vigentes, cuyo alcance sobre el mismo backend y clúster debe verificar
Infraestructura. Una credencial rotada no demuestra por sí sola pertenencia.

`recover` siempre publica `reconcile`. Para un run `queued`, adquiere primero el
lock de instancia y cambia a `uncertain`, cercando cualquier entrega tardía de
`apply`. Si otro worker posee el lock, devuelve 409. Un fallo de publicación
conserva el cerco y permite repetir `recover`. Ninguna de esas rutas concede una
autorización nueva. Repetir `/apply` con su clave original devuelve el run anterior
sin republicar; otra clave o aprobación del mismo intento se rechaza.

La observación ejecuta `version`, `init -reconfigure`, lectura remota con
`state pull`, GET nativos Proxmox y, si hay recurso en ambas fuentes,
`plan -refresh-only` y `show` del plan de observación, leyendo `prior_state.values`.
Después verifica que el state remoto no haya cambiado. No ejecuta Apply ni persiste el refresh en el
state remoto. Mantiene el locking de Terraform: el backend puede crear y retirar
su `.tflock`; «solo lectura» se refiere al recurso y al contenido del state, no
a eliminar el mecanismo de exclusión. No se ejecutó esta observación contra
Wasabi o Proxmox durante el desarrollo.

## Interpretación de los resultados

| Resultado de conciliación | Persistencia y decisión |
|---|---|
| Recurso exacto observado | Autorización `completed`; instancia `ready` con observación del provider |
| State vacío y VM ausente con alcance de clúster verificado | `uncertain` / `RESOURCE_ABSENT`; requiere revisión independiente antes del cierre |
| VM presente solo en Proxmox | `uncertain` / `UNMANAGED_RESOURCE`; nunca crear ni importar automáticamente |
| VM presente solo en state | `uncertain` / `STATE_RESOURCE_MISSING`; drift, nunca éxito |
| Recurso ajeno o distinto | `uncertain`; no adoptar ni declarar éxito |
| Error, timeout o respuesta inválida del provider | `uncertain`; no inferir ausencia |
| Artefactos alterados o incompletos | `uncertain` / `PLAN_INTEGRITY_FAILED`; detener la investigación de infraestructura por esta ruta |
| Ausencia en ambas fuentes revisada por un aprobador | `failed` / `RESOURCE_ABSENCE_CONFIRMED`; aprovisionamiento fallido, instancia `failed` |

El modo [refresh-only de Terraform](https://developer.hashicorp.com/terraform/cli/commands/plan#planning-modes)
no descubre una VM que nunca quedó registrada en el state.
Por ello, un state vacío requiere una comprobación independiente del VMID en todo
el clúster Proxmox, incluida cualquier tarea de clonación aún en curso. La
resolución por ausencia es una atestación humana auditable de esas comprobaciones;
el endpoint no consulta esas fuentes ni valida automáticamente sus documentos.
`result.reconciliation` conserva el hash verificado y si el manifest era histórico.
La corrección del intento 4 exige observación negativa de ambas fuentes antes de
aceptar `resolve-absence`; resultados antiguos requieren otra conciliación. Ver
[causa raíz y procedimiento actualizado](PRESENT_RESOURCE_RECONCILIATION.md).

## Procedimiento de recuperación

Este procedimiento es para una ejecución operativa posterior, revisada por
Infraestructura. No forma parte del despliegue del PR. Mantener `apply=0` en API
y worker y detener escritores externos sobre la asignación durante la revisión.
No modificar secretos, manifests, planes, state, locks o filas SQL para forzar
una conclusión.

1. Preservar la autorización, los eventos, el intento 2, su manifest y
   `saved.tfplan` en almacenamiento privado. Correlacionar fecha del intento y
   versión del código. Verificar que no quede ningún worker o proceso Terraform
   mutante activo. Conservar revisión del módulo y archivos de backend/variables.
2. Calcular `sha256sum` sobre el plan original y comparar el valor completo con
   la autorización y la tabla del incidente. Si difiere, detenerse: restaurar el
   artefacto original desde respaldo verificado; no recalcular la autorización.
3. Revisar y desplegar esta corrección mediante el proceso habitual, manteniendo
   `IACP_TERRAFORM_APPLY_ENABLED=0`. Este PR no despliega automáticamente.
4. Una identidad operadora del tenant solicita
   `POST /v1/instances/{instance_id}/terraform/applies/{approval_id}/recover`.
   Consultar `GET .../terraform/applies` y la operación hasta finalizar la
   observación. La caducidad de la aprobación no impide observar. No habilitar
   apply para superar un error de conciliación.
5. Si termina `uncertain` / `RESOURCE_ABSENT`, recopilar evidencia privada,
   fechada después del inicio del run: bucket/key exactos, versión del objeto,
   checksum SHA-256 de sus bytes, lineage, serial y cero recursos administrados.
   Confirmar la versión vigente y conservar las versiones anteriores. Para este
   incidente se espera serial 1, pero debe comprobarse nuevamente.
6. Comprobar el VMID 106 en el inventario completo del clúster autorizado,
   comprobar el nodo asignado y descartar tareas activas o recursos parciales.
   Un 403, un timeout, una lista filtrada o un error de red no prueban ausencia.
   Registrar alcance, fecha y referencia de evidencia en el ticket del incidente.
   Las observaciones de ambas fuentes deben corresponder al mismo periodo con
   escritores detenidos. Si hay discrepancias, conservar `uncertain`.
7. Un sujeto `approver` del mismo tenant, distinto del solicitante original y del
   operador que pidió apply, revisa ambas evidencias y solicita
   `POST .../applies/{approval_id}/resolve-absence`, con `Idempotency-Key` estable.
   Solo se admite después de una conciliación que verificó artefactos y terminó
   `RESOURCE_ABSENT`. Las referencias apuntan a evidencia privada; no adjuntar
   secretos, planes ni contenido completo del state.

Ejemplo del cuerpo; sustituir todos los valores de evidencia con los realmente
comprobados. No usar un checksum del plan como checksum del state:

```json
{
  "attempt": 2,
  "plan_sha256": "8193a6eef8972fc2aded60e6ff5802d0cb28a02e891240e839b1c3555b831bd5",
  "reason": "Ausencia en backend y clúster revisada con escritores detenidos",
  "state": {
    "bucket": "<bucket autorizado>",
    "key": "<state key persistida>",
    "version_id": "<versión vigente comprobada>",
    "lineage": "<lineage comprobado>",
    "serial": 1,
    "sha256": "<64 caracteres hexadecimales del objeto remoto>",
    "resource_count": 0,
    "observed_at": "<fecha ISO 8601 con zona horaria>",
    "evidence_ref": "<referencia privada de evidencia Wasabi>"
  },
  "proxmox": {
    "vmid": 106,
    "node": "<nodo de la asignación persistida>",
    "scope": "cluster",
    "absent": true,
    "observed_at": "<fecha ISO 8601 con zona horaria>",
    "evidence_ref": "<referencia privada de evidencia Proxmox>"
  }
}
```

El cierre conserva `started_at`, autorización, intento y reserva de VMID. Registra
actor, fecha, clave, hash del cuerpo, evidencia y evento
`terraform_absence_resolved`. Job, operación e instancia quedan `failed`; no se
afirma éxito de aprovisionamiento. La misma clave, cuerpo y actor devuelven el
mismo resultado; cualquier variación entra en conflicto. La cancelación posterior
no reabre el caso y mensajes tardíos de apply/reconcile no vuelven a ejecutarlo.
No se requiere migración ni actualización SQL manual.

Una futura decisión de aprovisionamiento es separada: cancelar el plan anterior,
solicitar otro intento con `expected_attempt=2`, conservar instancia, asignación,
VMID y state key, revisar un plan nuevo y obtener una autorización nueva. El
cierre no encola ninguno de esos pasos ni habilita apply. Conservar la evidencia
del incidente aunque se planifique otro intento.

## Validación reproducible

```sh
bash scripts/test-integration.sh
```

La suite usa PostgreSQL 17 y Redis 7 desechables, Celery real y ejecutores
Terraform simulados. Los nuevos casos de `tests/test_historical_recovery.py`
cubren ambos esquemas, intento 2, credenciales de lectura rotadas, integridad
alterada, state vacío, recurso existente, errores del provider, cierre auditado,
autorización consumida, cerco y repetición idempotente. No monta credenciales de
infraestructura ni ejecuta Terraform real.

El contrato se genera con `python scripts/export-openapi.py`; los manuales se
validan con `python scripts/validate-handbook.py`, ambos dentro de la imagen de
pruebas si el host no tiene las dependencias. Las pruebas verifican que OpenAPI
coincida con el código. `apply=0` permanece como predeterminado en Compose.

Resultado de esta corrección sobre la base `8558f08`: **213 pruebas aprobadas**
en Python 3.12, con PostgreSQL 17 y Redis 7 locales desechables. Se conserva una
advertencia de obsolescencia de Starlette/AnyIO, sin fallos. El validador de
manuales aprobó los diez documentos y sus nueve contratos autenticados.
