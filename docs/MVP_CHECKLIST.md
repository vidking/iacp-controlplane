# MVP KVM: estado de acreditación

Actualización 24/10 UTC: candidata `dff8052` desplegada (PR #40), **770 pruebas
aprobadas**. Una única VM nueva, **30009**, UUID
`93899a50-f9e4-48c1-987b-5a05ae79a670`, completó Cloud-Init, DHCP/QGA, SSH root,
rotación por SSH, observación de reconciliación sin cambio de state, start/reboot,
dos Shutdown ordenados y DELETE por API. Contraseña nueva verificada después de
start/reboot; contraseña inicial rechazada. No se usó Stop forzado. Evidencia:
[validación final](validation/MVP_FINAL_E2E.json). La creación requirió reconciliar
una observación post-Apply transitoria, conservando el mismo UUID y un solo Apply.

Locust: `main` externo verificado en `6aeff5a`, revisión histórica sin hallazgos;
[PR externo #1](https://scm.cyberfuel.com/CyberFuel/iacp-load-testing/pulls/1)
con cinco pruebas aprobadas, claves Lifecycle corregidas, tiempos completos y
plan sostenido separado. No se ejecutó carga sostenida ni otra provisión Locust.
Los registros siguientes conservan el cierre histórico de PR #39.

Objetivo: 27 de octubre de 2026. No atribuir evidencia de 0.9.1 al candidato 0.10.0.

| Capacidad | Implementado | Fixtures/integración | Infraestructura |
|---|---|---|---|
| Inventario, cuotas, VMIDs, Lifecycle, DELETE y reconciliación | Sí, base integrada | Suite existente | Acreditado previamente en 0.9.1; no repetido como desarrollo nuevo |
| Nomenclatura IACP/IaC Worker/Lifecycle Worker y rutas compatibles | Sí | Contrato OpenAPI | Desplegado b1883ec en MNG, esquema 0011 |
| Referencias cliente/pedido/producto y replay compatible | Sí | Tests CRM y hash histórico | Compra UUID 35e62b0c-5d49-4e26-82bd-c802bce03bad y replay con mismos IDs |
| Arranque opcional con delegación y fences Lifecycle | Sí | Tests de admisión, drain y replay | Clonación/recursos/arranque demostrados; inicializador falló y SSH no acreditado |
| Root por HTTPS/Cloud-Init y entrada cifrada | Sí, opt-in laboratorio | Tests de cifrado/tenant/secretos; provider validate real sin infraestructura | Clonación/recursos/arranque demostrados; inicializador falló y SSH no acreditado |
| Cloud-Init + SSH con host key QGA | Sí, operación propia | Verificador con fixtures | Cloud-Init sin errores; marcador final ausente, SSH pendiente |
| Locust manual externo | Proyecto independiente | Guardas locales | A breve: 10/25/50/100 usuarios, 334 requests sin errores; B pendiente |
| Resiliencia ante worker/API/state/Proxmox/Redis/DB | Controles existentes y continuación recuperable | Integración controlada | No se inyectan fallos destructivos en servicios reales |
| IaCoC: eventos/diagnóstico/clases de recuperación | Contrato mínimo versionado | Tests por tenant | Contrato desplegado; liveness worker/outbox posteriores |
| Wasabi UUID/state/versiones/locks | Script readonly | Semántica S3 existente | 9 referencias, 8 vacías, 1 con recursos, 0 locks en auditoría inicial |
| Política de retención/ACL/cifrado del bucket | No se cambia | No sustituible con fixtures | Consulta administrativa denegada; requiere evidencia del propietario |
| Bootstrap sin secretos recuperables en state para producción | Requisito identificado | No habilitado | Decisión posterior; riesgo de state aceptado solo en laboratorio |

Actualizar esta tabla con commit, pruebas y operaciones exactas al cerrar la
validación. La evidencia debe distinguir instalado, probado con fixtures,
demostrado en infraestructura y pendiente; una respuesta 202 no es una VM lista.

Cierre abreviado por solicitud del usuario (24/10 UTC): 767 pruebas aprobadas y
fixture TLS real aprobado. PRs 36, 37 y 38 integrados en origin/main. La candidata
MNG **no acredita todavía el MVP completo**: `GUEST_INITIALIZATION_PROGRAM_FAILED`
y `POWER_ACPI_TIMEOUT` en la VM nueva. Arranque y reboot sí terminaron. No se
repitió el inicializador consumido ni se alteraron fences/SQL/state. La limpieza
usa parada administrativa de la misma VM y DELETE por IACP, registrada aparte.
El p95 de HTTP excedió 500 ms en dos etapas; los ensayos de 10 segundos no
acreditan capacidad sostenida. Concurrencia B, fairness multi-tenant y fallos de
servicios reales quedan pendientes. Locust está fuera de IACP, en el proyecto
local `iacp-load-testing`; Forgejo aún responde que CyberFuel/iacp-load-testing
no existe. CI está configurado; no se atribuye un resultado remoto no observado.

El operador confirmó el timeout de `guest-ping` de QEMU Guest Agent en VM30008
al apagar. Se conserva como evidencia reportada por el operador; no se atribuye
aún una causa raíz al fallo anterior del inicializador. VM104 continúa sin
permisos efectivos y los hashes de configuración de VM106/29000 no cambiaron.

Limpieza confirmada: VM30008 eliminada por DELETE IACP; state actual con cero
recursos y referencia PostgreSQL conservada. Las versiones históricas conservan
la contraseña inicial (solo se publican booleanos y metadatos). No se borraron
objetos Wasabi. Resultados y cierre en `docs/validation/MVP_*`.
