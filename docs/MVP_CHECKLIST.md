# MVP KVM: estado de acreditación

Objetivo: 27 de octubre de 2026. No atribuir evidencia de 0.9.1 al candidato 0.10.0.

| Capacidad | Implementado | Fixtures/integración | Infraestructura |
|---|---|---|---|
| Inventario, cuotas, VMIDs, Lifecycle, DELETE y reconciliación | Sí, base integrada | Suite existente | Acreditado previamente en 0.9.1; no repetido como desarrollo nuevo |
| Nomenclatura IACP/IaC Worker/Lifecycle Worker y rutas compatibles | Sí | Contrato OpenAPI | Pendiente promoción candidata |
| Referencias cliente/pedido/producto y replay compatible | Sí | Tests CRM y hash histórico | Pendiente compra simulada nueva |
| Arranque opcional con delegación y fences Lifecycle | Sí | Tests de admisión, drain y replay | Pendiente piloto nuevo |
| Root por HTTPS/Cloud-Init y entrada cifrada | Sí, opt-in laboratorio | Tests de cifrado/tenant/secretos; provider validate real sin infraestructura | Pendiente piloto nuevo |
| Cloud-Init + SSH con host key QGA | Sí, operación propia | Verificador con fixtures | Pendiente autenticación real nueva |
| Locust manual externo | Proyecto independiente | Guardas locales | Pendiente A/B desde laptop |
| Resiliencia ante worker/API/state/Proxmox/Redis/DB | Controles existentes y continuación recuperable | Integración controlada | No se inyectan fallos destructivos en servicios reales |
| IaCoC: eventos/diagnóstico/clases de recuperación | Contrato mínimo versionado | Tests por tenant | Pendiente contrato candidato desplegado; liveness worker/outbox posteriores |
| Wasabi UUID/state/versiones/locks | Script readonly | Semántica S3 existente | 9 referencias, 8 vacías, 1 con recursos, 0 locks en auditoría inicial |
| Política de retención/ACL/cifrado del bucket | No se cambia | No sustituible con fixtures | Consulta administrativa denegada; requiere evidencia del propietario |
| Bootstrap sin secretos recuperables en state para producción | Requisito identificado | No habilitado | Decisión posterior; riesgo de state aceptado solo en laboratorio |

Actualizar esta tabla con commit, pruebas y operaciones exactas al cerrar la
validación. La evidencia debe distinguir instalado, probado con fixtures,
demostrado en infraestructura y pendiente; una respuesta 202 no es una VM lista.
