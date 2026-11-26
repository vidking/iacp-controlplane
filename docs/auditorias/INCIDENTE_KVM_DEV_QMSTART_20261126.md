# Incidente KVM en Dev: UPID `qmstart` no observable

- **Fecha:** 2026-11-26 (UTC)
- **Alcance:** `dev-iac.cyberfuel.com`, IACP Control Plane (VM 104), clúster MNG, nodo `mng01`
- **Candidato observado:** `940139c1b46efae1cefa99618e204bfd0145cb76`
- **Imagen observada:** `sha256:b1bc322ff4be4b05368994b657af77debb9a41a54b4cbb5fc662dd1f862e2069`

## Resultado de la matriz

El ciclo KVM 1 de 4, con 48 combinaciones, se detuvo en el cuarto lote de seis. Los primeros tres lotes (18 VMs) pasaron y se limpiaron. El lote cuarto no se cuenta como PASS y el ciclo debe repetirse desde el inicio en el siguiente candidato.

Cinco VMs del lote terminaron listas. `kvm-prueba-505` (instancia `ee2ed2d4-2507-4838-b56b-ba196a4b7998`, VMID `41548`, operación de aprovisionamiento `f466a65e-839f-40d8-a442-90b0c9a73e94`) quedó `uncertain`, con energía sin confirmar. Su lease VMID y la reserva NetBox de `190.0.231.15/24` permanecieron retenidos. La VM coincidía por nombre e identidad y estaba físicamente detenida en MNG.

## Diagnóstico

Proxmox conserva el UPID exacto del `qmstart` como tarea terminada: el historial devuelve tipo `qmstart`, estado `unexpected status` y `starttime`/`endtime` `1795688648` (2026-11-26 10:24:08 UTC). En cambio, consultar `/nodes/mng01/tasks/{UPID}/status` devolvió HTTP 400 `no such task`, y el endpoint de log de esa tarea no devolvió líneas. El acceso de solo lectura al syslog del nodo respondió 403, por lo que no se obtuvo el detalle interno de QEMU.

La causa del estado `uncertain` queda identificada: el worker no pudo leer un resultado terminal en el endpoint del UPID y, correctamente, no repitió la mutación a ciegas. El motivo interno por el que Proxmox registró `unexpected status` no se pudo determinar con la evidencia disponible y queda expresamente abierto; no se atribuye a una falla de la imagen, capacidad o red sin evidencia.

## Recuperación y limpieza

Se usó el endpoint canónico de reconciliación de la API para la instancia exacta, con la operación original como fuente. La reconciliación `c282d611-b881-4402-ab92-7a477e37d562` terminó correctamente y dejó la VM `ready/running`. Después se eliminaron las seis VMs del lote por la API.

Verificación posterior: las seis VMs ausentes del inventario Proxmox; las seis instancias `deleted`; las seis IPs liberadas en API y ausentes de NetBox; los seis leases VMID liberados; cero recursos Terraform activos para ese lote; cero operaciones o trabajos activos y cero locks advisory. La recuperación fue **manual por la API**; no se cuenta como recuperación automática certificada.

## Corrección del worker

Se amplió el reintento de `start` de una sola vez para el caso concreto en que el endpoint de estado responde HTTP 400, siempre que todas estas comprobaciones se cumplan: VM exacta observada detenida, UPID validado contra nodo/VMID/acción, historial con un solo registro del mismo UPID y tiempos de inicio/fin consistentes, tarea no activa, y respuesta no marcada como incierta. Si el historial no confirma la finalización, la operación conserva el comportamiento fail-closed y permanece `uncertain`.

Las pruebas enfocadas de transporte pasaron (**27 PASS**) y la suite local Docker aislada terminó con **702 PASS**, 1 aviso de deprecación de Starlette. Estas pruebas locales no sustituyen la matriz física. El candidato `940139c` no se certifica por este resultado; la validación física debe reiniciarse con el nuevo SHA e imagen.
