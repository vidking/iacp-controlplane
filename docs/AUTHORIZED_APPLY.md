# Aplicación autorizada de un plan

Este cambio amplía 2B. `IACP_TERRAFORM_APPLY_ENABLED=0` sigue siendo el valor
predeterminado en API y worker. Terraform y Proxmox no se ejecutan en las pruebas:
el adaptador se simula con PostgreSQL 17, Redis 7 y Celery reales en Docker local.

1. El operador crea la instancia; el worker prepara un plan con la asignación
   externa autorizada y el módulo fijado de `iac-proxmox`.
2. Una identidad `approver` del mismo tenant, distinta del solicitante, revisa
   el plan y llama a `POST /v1/instances/{id}/provisioning/approvals` con
   `attempt`, `plan_sha256` y `valid_for_seconds` (60–3600; predeterminado 900).
3. El operador solicita `POST .../provisioning/executions` con `approval_id` e
   `Idempotency-Key`. La API y el worker exigen la habilitación del entorno.
4. El worker verifica intento, hash, source, backend, variables y caducidad;
   también exige la misma identidad de backend/provider, endpoint y CA que se
   usaron al planificar mediante una huella salada que no revela credenciales;
   adquiere el lock PostgreSQL por instancia y aplica **solamente** `saved.tfplan`.
   Terraform conserva `use_lockfile=true` en Wasabi y espera hasta 30 s el lock.
5. State pull, GET nativos Proxmox y `plan -refresh-only` verifican presencia,
   VMID, nodo, nombre y recursos; se leen los valores de `prior_state.values`,
   nunca planned_values del refresh-only. PostgreSQL registra la observación. `ready` significa KVM observada,
   no disponibilidad del sistema operativo o de la aplicación invitada.

Las autorizaciones son de un solo uso. Caducan antes de iniciar la mutación y no
permiten cambiar el plan. El código conserva los jobs anteriores y agrega un job
`instance.apply`; los eventos permanecen ligados a la operación original.
`infrastructure_kind` distingue `simulated`, `unprovisioned` y `kvm`.

`GET .../provisioning/executions` muestra la evidencia. `POST .../applies/{approval_id}/cancel`
cancela antes de mutar o exige conciliación si la mutación ya empezó.
`POST .../applies/{approval_id}/recover` concilia `queued`, `running` y `uncertain`
exclusivamente por observación; cerca entregas apply pendientes antes de publicar.
Un recurso ausente, parcial, distinto o inaccesible exige intervención.
Nunca se vuelve a aplicar automáticamente un resultado incierto.

Los planes anteriores a PR #14 se concilian sin fabricar huellas de contexto,
conservando la verificación del SHA-256 y del vínculo persistido. El cierre por
ausencia requiere evidencia independiente de state y Proxmox revisada por un
approver y termina en `failed`, sin afirmar aprovisionamiento exitoso. Consultar
[compatibilidad y procedimiento auditado](HISTORICAL_PLAN_RECOVERY.md).

## Diagnóstico por fase y análisis del incidente MNG

`TERRAFORM_COMMAND_FAILED` no identifica por sí solo un fallo del provider. Ese
código puede originarse en `init`, `show`, `apply` o en el `plan -refresh-only`
de observación. La autorización registra ahora, dentro de `result.diagnostic`,
solo `phase`, nombre del subcomando y código de salida. Las fases posibles son
`apply.preflight`, `apply.init`, `apply.inspect`, `apply`, `observe.preflight`,
`observe.init`, `observe.refresh` y `observe.inspect`. No se conservan argumentos,
stdout ni stderr, porque pueden contener rutas, topología o material sensible.

Los fallos de integridad, versión, inicialización e inspección anteriores a
`terraform apply saved.tfplan` terminan en `failed` sin `started_at`. Justo antes
de invocar el único comando mutante se consume la autorización y se fija
`started_at`; desde ese punto, cualquier error de apply u observación termina en
`uncertain`. La conciliación de `uncertain` ejecuta exclusivamente inicialización,
state pull, GET nativos, refresh-only y show; nunca vuelve a ejecutar `apply`.
`observe.state`, `observe.proxmox` y `observe.state_verify` identifican las lecturas
nuevas; los diagnósticos incluyen status HTTP y presencia conocida de cada fuente,
sin respuestas ni secretos. La conciliación funciona con Plan y Apply en 0.
Consultar [el incidente del intento 4 y su corrección](PRESENT_RESOURCE_RECONCILIATION.md).

Para el incidente del release `a97c5ac`, la evidencia disponible no permite
atribuir con certeza el código genérico al provider: tampoco distingue si falló
el apply guardado o la observación posterior. La modificación del state remoto
confirma actividad sobre el backend, pero no demuestra creación de la VM ni
autoriza otro apply. La ausencia de VMID 106 mantiene el resultado incierto hasta
correlacionar la versión del objeto, serial/lineage, eventos de Proxmox y el run.
Sí se identificó una divergencia reproducible: el código anterior reconstruía
credenciales y CA al aplicar, sin comprobar que fueran las usadas al planificar.
Ahora cualquier rotación o cambio exige un plan y aprobación nuevos.

Cada subproceso tiene 300 s, 16 MiB de salida y terminación del grupo de procesos;
el flujo tiene 900 s y el task límites soft/hard 910/930 s. Un timeout después
del inicio de la mutación produce `uncertain`. Antes de cambiar esos límites se
debe medir una clonación autorizada de laboratorio; no se ha ejecutado ninguna.

La migración `0004_authorized_apply` agrega una tabla; no modifica 0001–0003.
La reversión inmediata deshabilita apply y conserva una imagen compatible con
0004, la base ampliada, planes y volúmenes. Las imágenes anteriores que esperan
0002/0003 rechazan 0004 en readiness; no constituyen un rollback binario probado.
Una restauración de datos o una imagen de compatibilidad requieren validación y
autorización separadas. No ejecutar downgrade ni borrar state/locks a ciegas.

Validación de asignación sin red (archivos autorizados externos):

```sh
python -m iacp.terraform.lab --allocation /run/config/lab-allocation.json --request /run/config/lab-request.json
```

La API todavía no implementa eliminación de KVM. La demostración debe tratar la
eliminación como un cambio separado de Infraestructura: plan de destrucción con
el mismo módulo/state, revisión y autorización explícita, y comprobación del
recurso eliminado. El ejecutor rechaza planes delete/replace y `terraform destroy`.
El inventario no se elimina para conservar evidencia.

Referencias de comportamiento de Terraform:
[plan guardado](https://developer.hashicorp.com/terraform/cli/commands/apply),
[locking S3](https://developer.hashicorp.com/terraform/language/backend/s3).
