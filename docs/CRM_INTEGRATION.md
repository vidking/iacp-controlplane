# Integración de Desarrollo/CRM con IACP

El [runbook MVP 0.10.0](MVP_CRM_RUNBOOK.md) documenta el contrato candidato,
referencias comerciales, arranque y entrada root. La evidencia de 0.9.1 mantiene
su alcance histórico; comprobar la versión OpenAPI desplegada antes de enviar
campos nuevos.

El laboratorio no depende de ningún sistema comercial. Un CRM futuro actuará como
cliente de la API; IACP conserva autorización técnica, cuotas, propiedad y auditoría.
No se integró ni contactó un sistema comercial en esta implementación.

Usar una identidad API distinta por tenant y función, entregada mediante el gestor
de secretos. Reader puede consultar; operator puede crear/operar; approver queda
fuera del CRM y revisa políticas o planes según los controles vigentes. No entregar
secretos docs-only, Proxmox, SSH, Wasabi ni del IaC Worker al navegador del cliente.
El login de Swagger solo controla acceso a documentación; no sustituye Bearer/RBAC.

Tras la autorización comercial **externa a IACP**, el backend del CRM conserva su
ID de solicitud y genera una Idempotency-Key estable para cada intención técnica.
Registra cuerpo canónico, tenant implícito por token, instance_id y operation_id
devueltos por POST /v1/instances. La API no admite un tenant arbitrario en el cuerpo
y una política de laboratorio no acredita autorización comercial de producción.
Los IDs comerciales se conservan en el CRM. Desde 0.10.0 el campo opcional
`commercial_context` conserva customer_id/order_id/product_id como referencias
opacas; no acredita pago ni concede permisos técnicos.

Consultar GET /v1/operations/{operation_id} con backoff acotado, por ejemplo 1, 2,
4 y hasta 15 segundos. Guardar X-Request-ID para diagnóstico. Tratar
`normalized_status` como queued/running/completed/failed/uncertain. En completed,
leer GET /v1/instances/{instance_id}: recursos, VMID/nodo de inventario, direcciones
conocidas e infrastructure_kind. No considerar IP pendiente o guest_ready=null un
fallo del aprovisionamiento; una aplicación necesita su propio readiness.

Un timeout HTTP no autoriza otra VM: repetir la misma clave y cuerpo. Un 409 por
cuota/política requiere revisar capacidad/permisos, un 422 corregir el contrato y
un 503 respetar Retry-After o consultar la intención persistida. Un 404 también
oculta recursos de otro tenant. Usar error_code, error_message y eventos; no
interpretar stderr del motor. Si la creación queda incierta, POST /reconcile
sobre el mismo UUID; si solo falló planificación sin Apply consumido, usar
retry-provision con expected_attempt y clave estable. Ver [guía completa](SELF_SERVICE.md).

Start, shutdown y reboot son operaciones asíncronas independientes con claves
propias. En Ubuntu DHCP el primer start agenda `instance.network.initialize`;
consultar el historial por instance_id y esperar completed e IP conocida antes de
publicar la dirección. Una inicialización incierta se observa con `/reconcile`,
sin duplicar la VM ni ejecutar comandos del invitado desde el CRM. Para una suspensión comercial, revocar acceso comercial en el CRM y
solicitar shutdown solo cuando el tenant/política lo autorice; IACP no implementa
pause, suspensión de RAM ni facturación. Una política puede omitir start o delete.
Eliminación bajo política aplica únicamente a recursos nuevos propios descartables,
apagados y verificados. Recursos preexistentes, ajenos, acciones sensibles y
operaciones fuera de cuota mantienen revisión independiente; no escalar permisos
del CRM ni ejecutar comandos directos de Proxmox para compensar un rechazo.

En producción, preservar el flujo estático/IPAM y sus asignaciones aprobadas;
no extender automáticamente una política DHCP de laboratorio a perfiles de
producción. La integración futura de IPAM debe conservar tenant, idempotencia,
reservas, recuperación y propiedad, sin cambiar el contrato de UUID/operación.
El CRM consume `/docs` y `/openapi.json`; la administración utiliza `/admin/docs`
y `/admin/openapi.json` con permisos adicionales. Las rutas administrativas
anteriores se retiraron tras migrar sus consumidores internos conocidos. El
[contrato administrativo](PROVISIONING_CONTRACT.md) registra el mapeo y las decisiones.

Desde 0.10.1 se conserva el campo histórico `executor`, con valores `simulated` o
`provisioning`, y `provisioning_service=provisioning`. Desarrollo confirmó que no
conoce dependencia del valor anterior. Crear, consultar, operar energía y eliminar
conservan rutas, cuerpos, identificadores, respuestas HTTP e idempotencia de `/v1`.
El cliente decide disponibilidad con status/infrastructure_kind/operaciones.
Los detalles privados de ejecución se conservan en auditoría interna; las respuestas
comerciales contienen evidencia de recursos y códigos neutrales.

Las credenciales CRM son operator/reader sin el permiso servidor `administration`.
Pueden usar políticas ya aprobadas; no proponerlas, aprobar planes, ejecutar planes
manuales, cancelarlos ni resolver ausencias. El rol approver es exclusivamente
administrativo. La identidad bootstrap es de plataforma y no debe entregarse al CRM.

Los códigos publicados de provisioning usan el prefijo `PROVISIONING_`; los
eventos de ese servicio usan `provisioning_` y actor `worker:provisioning`.
Conservar event_id para deduplicar; el registro interno mantiene sus códigos
históricos. `result` contiene la observación comercial de recursos, sin ubicación
del almacén de estado ni manifiestos privados de ejecución.
