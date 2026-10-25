# Fase G: catálogo KVMHOST-54 y transición del contrato CRM

> Documento histórico de la implementación parcial. Actualización del
> 24/10/2026, America/Costa_Rica: consultar el
> [cierre de código y preparación](FASE_G_CIERRE_PREPARACION.md) para el estado
> candidato posterior. Las observaciones originales se conservan como evidencia.

Fecha de auditoría: 24/10/2026, America/Costa_Rica.

Estado: **implementación parcial candidata; no fusionar ni promocionar el cambio
de creación**. El requisito G6 bloquea la ejecución comercial. Este documento no
modifica las decisiones de negocio ni acredita E2E nuevos.

## Baseline comprobado

- `origin/main`: `8a58db244ec1e9cd1a0d627b21e086548489e75d`.
- IACP Control Plane (VM 104): API `iacp/controlplane-api:fase-f2h-8a58db2`,
  imagen `sha256:8bfe4a02e4e38aadb738463309319f9dffc4e2465055bc37dd29ea9a055765a5`.
- IaC Worker: `iacp/terraform-worker:fase-f2h-8a58db2`,
  imagen `sha256:9da348a98ab299706aa0230fa70a9caf79b1dcd71af8cd3f8773d5f0e64d140b`.
- API, PostgreSQL y Redis saludables; workers y Traefik operativos al inicio.
- Motor interno fijado a `iac-proxmox` SHA
  `2c0f319ba5d7a45987cd9d0395673828c28f678b`, no al HEAD del clon local.
- Fuente Golden Image observada: Ubuntu 24.04, VM 29000, `mng01`,
  `DS_CYBERFUEL`. No se modificó la plantilla.

## Causa del bloqueo de acceso G6

La implementación histórica no cumple el nuevo límite de persistencia:

1. `schemas.RootAccess` acepta `username/password`; incluso su descripción
   informa la posible retención histórica en plan/state de laboratorio.
2. `services.instances.create_instance` asigna `encrypt_password(...)` a
   `Instance.initial_access_ciphertext`, persistido en PostgreSQL. Cifrarlo no
   satisface la prohibición nueva de guardar la contraseña en PostgreSQL.
3. `terraform.config.Allocation.variables` descifra y agrega
   `iacp_root_password` a las variables.
4. `terraform.access_adapter` asigna esa variable a
   `initialization.user_account.password` del recurso Proxmox.
5. `terraform.executor.plan` escribe las variables en
   `iacp.auto.tfvars.json`, produce `saved.tfplan` e incorpora las variables al
   manifiesto privado. `sensitive=true` oculta presentaciones, pero no excluye
   el secreto de los artefactos persistentes ni del state remoto.

No se inspeccionaron valores de contraseñas, planes ni states para demostrar
esto: la ruta de datos es explícita en código. No se borraron evidencias,
credenciales ni versiones históricas.

`CommercialAccess` valida el formato nuevo con `SecretStr`, admite claves SSH
públicas opcionales junto a la contraseña y oculta entradas en la representación
de errores. **Eso valida el contrato; no implementa entrega segura**.
`require_safe_execution` siempre rechaza la ejecución nueva. No existe una
variable de entorno que habilite una conversión al adaptador histórico.

Pendiente técnico concreto: implementar y certificar una entrega temporal
vinculada a instancia, fuera de PostgreSQL, Redis, plan/state, argumentos de
procesos y trazas, con consumo único, limpieza y recuperación por observación.
La vía Guest Agent existente inicializa identidad/SSH, pero actualmente la
contraseña sigue llegando por Cloud-Init/Terraform; por sí sola no resuelve G6.

## Matriz KVMHOST-54 contra implementación

| Plan estable | vCPU | RAM MiB | Disco GiB | Resolución de código | E2E físico nuevo |
|---|---:|---:|---:|---|---|
| kvm-1 | 1 | 2048 | 50 | Implementada y validable sin ejecución | BLOCKED: límite actual de 40 GiB y G6 |
| kvm-2 | 2 | 4096 | 100 | Implementada y validable sin ejecución | BLOCKED: 40 GiB por VM, 80 GiB agregados y G6 |
| kvm-3 | 4 | 8192 | 200 | Implementada y validable sin ejecución | No certificado físicamente |
| kvm-4 | 8 | 16384 | 400 | Implementada y validable sin ejecución | No certificado físicamente |

Los planes son constantes inmutables en `catalog.py`, versión `KVMHOST-54.1` y
digest SHA256 reproducible de sus metadatos. No contienen VMIDs ni credenciales.
El registro de imágenes es interno y separado; la respuesta pública no expone
VMID, nodo ni storage. Solo existe `ubuntu-2404`; no se registraron otras imágenes.

La disponibilidad es conservadora: consulta el perfil autorizado y uso agregado
del tenant, incluyendo reservas fallidas/inciertas. No consulta ni reserva
capacidad física del hipervisor. `capacity_verified=false` y los motivos impiden
confundir un producto definido con una VM aprovisionable.

Los límites actuales observados en `lab-dhcp` y el perfil histórico root son
4 instancias, hasta 4 vCPU/4096 MiB/40 GiB por VM y 8 vCPU/8192 MiB/80 GiB
agregados, hasta 2 operaciones concurrentes. No se trasladaron cupos de producción
ni se aumentó ninguno de esos límites.

## Contratos y autenticación

- `GET /v1/plans`: cuatro productos, tamaños y disponibilidad por entorno/tenant.
- `GET /v1/images`: imagen habilitada, capacidades históricas, mínimos,
  compatibilidad de planes, autorización y bloqueo actual.
- Bearer opaco y tenant derivado del servidor; reader puede consultar.
- Cabeceras `X-IACP-Catalog-Version` y `X-IACP-Catalog-SHA256` identifican el
  catálogo, no constituyen un ETag de disponibilidad ni una reserva.
- No hay aprobaciones humanas individuales en el diseño comercial G aprobado.
  La admisión automática nueva **aún no está conectada**: no se ocultan políticas
  históricas mediante una traducción implícita de solicitudes.
- `POST /v1/instances` servido conserva temporalmente `InstanceCreate` histórico.
  El schema nuevo `CommercialInstanceCreate` se exporta aparte para migración.
- `docs/contracts/openapi-fase-g-candidato.json` documenta la revisión incompatible
  que seguirá en `/v1`, marcada `BLOCKED_G6_Y_MIGRACION`. No es el contrato activo.

La revisión candidata exige `name`, `plan_id`, `image_id`, `network.profile_id`,
`access.root_password` y `commercial_context.order_id`. `start_on_create` vale
`true` por defecto. SSH pública y `product_id` son opcionales. Rechaza con
`extra_forbidden` tamaños libres, UUID de política, tenant, nodo o storage del
cliente. `Idempotency-Key` y `createInstance` permanecen en la ruta documentada.

## Red y almacenamiento

Solo `lab-dhcp` y `wan-static` figuran en el contrato G. El perfil root histórico
no se publica como red comercial nueva. DHCP rechaza direcciones estáticas y VLAN
del cliente. WAN exige `ipv4_address`, `prefix_length` y `gateway`, valida IPv4 y
coherencia de subred y rechaza VLAN/bridge arbitrarios. VLAN 204 queda como diseño
de Infraestructura, nunca parámetro libre. Toda ejecución WAN se bloquea, incluso
si el JSON es válido: no hay reservas certificadas ni conectividad habilitada.
La comprobación de duplicados/reserva real sigue BLOCKED junto con su ejecución;
validar sintaxis no certifica que una dirección esté reservada.

El resolvedor nuevo produce CPU/RAM/disco a partir del plan, sin secretos. Los
tests comprueban la expansión de variables del motor fijado para los cuatro
tamaños. No ejecutan Apply ni certifican discos/filesystems del invitado. El
crecimiento físico y `growpart`/filesystem requieren E2E después de resolver G6
y disponer de cuota autorizada. No se permiten reducciones ni storage libre.

## Consumidores y ventana de transición

| Consumidor identificado | Estado observado | Acción necesaria antes de promoción comercial |
|---|---|---|
| Manual/Swagger actual | Campos `image`, tamaños y política histórica | Usar nuevo cuerpo y ejemplos al activar revisión G |
| `iacp-load-testing/e2e.py` | Envía recursos y `self_service_policy_id` | Migrar creación/preflight/cleanup al contrato y reglas G |
| `scripts/check_runtime.py`, manuales y ejemplos CRM | Creación histórica | Migrar explícitamente, conservar claves e IDs en reintentos |
| Suite de tests de creación, root, políticas y Lifecycle | Bindings históricos | Mantener regresión histórica y añadir admisión G con cuotas/concurrencia |
| CRM externo | No verificado en este workspace | Requiere evidencia del consumidor migrado; no presumir compatibilidad |

La ventana de transición está **sin abrir**. Inicio: solo tras G6, capacidad,
admisión G, consumidores identificados migrados y regresión aprobada. Fin: tras
despliegue controlado y certificación HTTPS/E2E. No se inventan fechas de corte.
Hasta entonces siguen vigentes los campos antiguos en el runtime, no en el
schema comercial candidato. El nuevo contrato no los acepta silenciosamente.

## Migración y reversión

1. Conservar baseline, snapshot OpenAPI, configuración efectiva y backups.
2. Completar entrega segura G6 y admisión interna: identidad/propiedad separadas
   del catálogo, cuotas y reglas de ejecución; sin nuevas políticas por producto.
3. Añadir persistencia versionada de `plan_id`/`image_id` e intención G sin secretos.
   No inferir planes para instancias históricas: muchas no coinciden con el catálogo.
4. Mantener policies, review_history, UUID, hashes, state_key y bindings históricos
   intactos para observación, Lifecycle, eliminación y auditoría existentes.
5. Migrar consumidores y sus tests; no reencolar ni reinterpretar operaciones en
   curso. Una clave histórica conserva su contrato/digest, no se reutiliza con
   el cuerpo incompatible. Añadir discriminación explícita de revisión de intención.
6. Verificar cuotas autorizadas, reserva/capacidad, lock e idempotencia dentro de
   la misma transacción. Mantener como máximo dos aprovisionamientos simultáneos.
7. Abrir ventana de transición solo con las evidencias anteriores. Promocionar con
   `iacpctl` y `go_live.py`; nunca migrar automáticamente al publicar un catálogo.
8. Revertir código con la admisión cerrada y el release anterior preservado. Las
   migraciones futuras deben ser aditivas y toleradas por rollback; no hacer
   downgrade destructivo, borrar filas/state ni repetir mutaciones consumidas.

No se añadió migración de DB en esta entrega parcial porque el nuevo flujo no
admite ni persiste intenciones. La normalización del historial queda pendiente
como migración aditiva, no como borrado de autorizaciones.

## Validación y dictamen

Los resultados ejecutados se registran en `docs/validation/phase-g/RESULTADOS.md`.
Las pruebas nuevas comprueban catálogo, autorización, límites, formato y rechazo
de campos obsoletos, SSH válida/duplicada, resolución de tamaños y contrato
candidato. La regresión histórica conserva los handlers y fences del MVP.

**G completa: NO-GO** hasta G3/G6, consumidores y E2E KVM 1/2. **MVP histórico:
conserva su GO de piloto supervisado**, sin ampliar la evidencia. **Resiliencia
integral: NO-GO**; la certificación física del worker sigue pendiente.

No se modificaron Compose, credenciales, estados, imágenes OCI ni Golden Image.
