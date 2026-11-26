# Reincidencia de `qmstart` en la matriz KVM de Dev

- **Fecha:** 2026-11-26 (UTC)
- **Alcance:** `dev-iac.cyberfuel.com`, IACP Control Plane (VM 104), clúster MNG, nodo `mng01`
- **Candidato ejecutado:** `8bd184fdd19a59cd8d2516524f1822ce074be2d6`
- **Imagen:** `sha256:a16a5848956df21e25d0e15ecb8c71d1e283a937051461d136fdb8f1ddc3f1b0`
- **Matriz:** KVM `prod_static`, ciclo 1 de 4, 48 combinaciones, seis aprovisionamientos por lote

## Resultado

La ejecución `20261126133001` quedó **BLOCKED**, no PASS, en el lote 7. Los primeros seis lotes completaron 36 casos con todas sus comprobaciones y limpieza; el séptimo lanzó otros seis casos y se detuvo al detectar una operación `uncertain`. En total quedaron registrados 42 aprovisionamientos, pero solo los primeros 36 son casos completos aprobados. Los seis del lote 7 no cuentan como PASS.

Evidencia de la matriz: `/opt/iacp/evidence/wan-dev-8bd184f-kvm-cycle1/results.jsonl`.

## Incidente y causa

`kvm-prueba-547` (instancia `c6b5f110-099f-4ba8-acbb-1efc1a973c48`, operación `238f4194-f880-4b51-9940-34e5a4c064c5`, VMID `41587`, imagen `fedora-45`, plan `kvm-4`, IP `190.0.231.12/24`) terminó `uncertain` con `POWER_OPERATION_UNCERTAIN`. La operación inició el `qmstart` a las `17:04:10 UTC` y quedó incierta a las `17:09:12 UTC`. Proxmox mostró la VM detenida (`qmpstatus=stopped`) y un único `qmstart` para ese VMID con estado terminal `unexpected status`; `starttime` y `endtime` fueron `1795712650` (17:04:10 UTC). El endpoint de estado de ese UPID devolvía `stopped` / `unexpected status`.

La causa inmediata queda confirmada en el worker: ante una respuesta de observación marcada incierta, el reintento de `start` volvía a consultar el UPID y convertía su fallo terminal en `POWER_OPERATION_UNCERTAIN`, aunque Proxmox ya registraba esa tarea exacta como terminada con error y la VM estaba detenida. El código anterior solo repetía el encendido tras un fallo explícito no incierto o un caso HTTP 400 específico. La respuesta inicial no guardó el UPID en el error persistido; el evento `terraform.provision.failed` conservó `POWER_OPERATION_UNCERTAIN` y `POWER_PROXMOX_UNAVAILABLE`. Por ello el diagnóstico se hizo correlacionando la operación, la única tarea `qmstart` del VMID y el historial. No se determinó la causa interna de Proxmox para `unexpected status`; no se atribuye a la imagen, al guest o a la red sin evidencia.

## Recuperación y limpieza

Se validaron la identidad de la VM, la reserva NetBox y el lease antes de encolar la reconciliación canónica para la instancia y operación exactas. El trabajo de reconciliación `489b0f2a-81b9-4c4b-8de0-0287abdd56a2` terminó `completed` a las `17:20:30 UTC`; la VM quedó `ready/running`. La recuperación fue manual por API y no cuenta como recuperación automática certificada.

Después se limpiaron los seis casos del lote mediante la API. Verificación: las seis instancias `deleted`, VMs ausentes del inventario del clúster MNG, leases VMID liberados, IPs liberadas y ausentes de NetBox, sin recursos Terraform activos, operaciones o trabajos activos ni locks advisory. Evidencia de limpieza: `/opt/iacp/evidence/manual-cleanup-batch7-20261126T172132Z/results.jsonl`. La evidencia de reconciliación se conserva en `/opt/iacp/evidence/manual-reconcile-batch7-20261126T172010Z/reconciliacion.json`.

## Corrección local y validación

La corrección añade una comprobación del historial que distingue una tarea terminal fallida de una tarea terminal exitosa. El worker permite un solo reintento de `start` cuando coinciden el nodo, VMID, acción y UPID, el historial demuestra que terminó con error y la observación física confirma que la VM está detenida. Si la tarea está activa, tuvo éxito, no se puede consultar o no coincide la identidad, conserva el estado incierto y no repite la mutación.

- Pruebas enfocadas: **29 PASS** (`tests/test_transporte_proveedores.py`).
- Suite Docker local aislada: **704 PASS**, un aviso de deprecación de Starlette. Evidencia: `/tmp/iacp-dev-validation-8a4f5886`.
- La corrección todavía no está desplegada en Dev. La matriz del candidato `8bd184f` queda bloqueada y debe reiniciarse desde el caso 1 con la nueva imagen; los 36 PASS previos no certifican la revisión siguiente.
- Producción no se modificó.
