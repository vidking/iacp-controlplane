# Cierre: promoción del JSON mínimo y preparación de identidades LAB

Fecha: **25/10/2026, America/Costa_Rica**. Promoción: **21:00:24**; activación comercial verificada: **21:01:41**. Las marcas técnicas originales UTC se conservan con su conversión local en fechas-costa-rica.json.

## Etapa 1 — GO de promoción, contrato y documentación

Main integra PR #78 y `16af883ee76ae0b2ae9411e7e19bb8dbcfe781cd`. Release efectivamente desplegado: **`54d418119200710ee00eb7a9d406b9322fe8e40c`**, anterior **`ab36a06db3cefb12cd3004cf7921e2af54333c28`**. Los árboles de código de la candidata y del merge son equivalentes. No hay cambios funcionales pendientes respecto de ese main. El eventual commit de este informe es documental, no una nueva versión ejecutada.

[API](codigo-runtime.json) y [Terraform Worker](codigo-runtime-terraform.json): **101 archivos comprobados en cada contenedor**, iguales al código aprobado. No se identifica la versión únicamente por Git o por un tag Docker.

| Servicio | Imagen local efectiva | ImageID SHA256 |
|---|---|---|
| API, worker y Lifecycle Worker | `iacp/controlplane-api:piloto-54d4181` | `720ce48ef3153ab356bc5007e140ba2d3c64e5b7100e28cb326936dc006c6093` |
| Terraform Worker | `iacp/terraform-worker:piloto-54d4181` | `b193e25876b76ef74acd25e949ef90626e59ffba2bc14d0702feb7b7eff66df8` |

Se actualizaron los cuatro servicios de aplicación para ejecutar la revisión compartida de contrato/admisión/bootstrap. PostgreSQL, Redis y Traefik conservaron sus contenedores; volúmenes, configuración operativa, secretos, cuotas, perfiles y state permanecieron preservados. Producto 0.10.1, esquema **0012_commercial_intents**, sin migraciones nuevas. Imágenes locales verificadas, sin publicación OCI.

Se ejecutaron los mecanismos existentes: preflight sobre candidato revisado, `iacpctl env-promote`, `iacpctl deploy`/`go_live.py`, `activate-lab` con los mismos límites y doctor. [Evidencias](operacion/). El preparador temporal corrigió una referencia al manifiesto anterior antes de modificar el entorno; no se deshabilitó ningún validador ni se necesitó rollback por una regresión.

### Respaldo y rollback

Respaldo privado nuevo `/opt/iacp/backups/piloto-54d4181-0012.dump`, SHA256 `7b59c124b4f401875cc58d0d3e1f3ebdd5192ad81445d3526e8d82e232d9c26b`. Restauración real en PostgreSQL 17 aislado sin red, esquema 0012 y 16 tablas, PASS. [Prueba](operacion/piloto-54d4181-preparacion/restauracion.json). No se exportó el dump.

Las imágenes anteriores y el manifiesto previo `/etc/iacp/changes/manifest-docs-ab36a06.json` se conservan. Manifiesto efectivo nuevo `/etc/iacp/changes/manifest-piloto-54d4181.json`; release host `/opt/iacp/releases/iacp-piloto-54d4181`. El backup del entorno `server.env.20261026T025909Z` corresponde al **25/10/2026 20:59:09 Costa Rica**. La autorización nueva quedó registrada para este SHA; no se reutilizó una ventana vencida.

Rollback funcional disponible mediante iacpctl/go_live.py para cerrar admisión y conservar datos. No fue ejecutado: todos los controles finales pasaron. No hay downgrade Alembic ni borrado de state. Un binario anterior no admite el cuerpo mínimo: si posteriormente existen intenciones mínimas activas, conservar código capaz de resolverlas antes de retroceder de versión. No convertir fingerprints ni inventar pedidos para recuperar una operación.

### Contrato real y pruebas

- [OpenAPI antes](openapi-antes.json) y [después](openapi-despues.json), 3.1.0. El contrato de creación requiere ahora **name, plan_id, image_id y access**. network omitido resuelve lab-dhcp y contexto omitido no crea order_id, exclusivamente bajo el piloto LAB autorizado.
- [Validación dentro del contenedor activo](contrato-runtime.json): JSON mínimo servido por HTTPS, ambos ejemplos completos KVM 1/2, rechazo de null explícito, respuesta anterior y sintaxis Bash del cURL: PASS.
- **104 pruebas aplicables aprobadas sobre main**, cero omitidas, con PostgreSQL/Redis locales descartables: [registro](pruebas-main.log). Revalida permisos, aislamiento, concurrencia, idempotencia, G6, compatibilidad y OpenAPI. La suite completa previa al merge fue **1.087**, conservada en [la candidata](../piloto-minimo-20261025/INFORME.md); no se presenta como una nueva ejecución completa.
- [Playwright HTTPS real](navegador/playwright.json): Chromium escritorio, Chromium móvil y Firefox escritorio, PASS; **123 coordenadas**. Plegado total, apertura inicial, pasos, índices, menús completos, deep links, historial, recarga y copia. Try it out ejecutó solamente GET /v1/images HTTP 200.
- Portapapeles real Chromium coincide con JSON/cURL completos, en escritorio y móvil. Firefox confirma escritura nativa; no se afirma lectura de clipboard no soportada por su permiso Playwright. Cero errores JavaScript, cero recursos externos y cero métodos de escritura durante la matriz.

[Persistencia sin cambios](persistencia-sin-cambios.json): **15 tablas con counts y hashes idénticos antes/después**. Los 30 registros históricos de terraform_applies conservan su hash; se ejecutaron **cero apply nuevos**. Cero nuevas operaciones, colas vacías, cero mensajes sin confirmar y cero operaciones no terminales en la captura de cierre. No se creó ninguna KVM.

API, worker, Terraform Worker, Lifecycle Worker, Redis, PostgreSQL y Traefik operativos; doctor PASS sin deriva. Health/live y readiness HTTPS HTTP 200; docs y catálogo sin credencial HTTP 401. El acceso autorizado utiliza el Bearer operativo existente por stdin privado; no se emitieron tokens durante la certificación.

La validación positiva del nuevo cuerpo fue de modelo y contrato en runtime; **no se envió un POST válido de creación ni se repitió E2E físico**, conforme a la restricción del responsable. El GO KVM 1/2 existente se conserva, respaldado por sus E2E anteriores. No se amplía la certificación a otras imágenes, WAN, KVM 3/4 o resiliencia física.

### Capturas del dominio real

- [Escritorio](navegador/chromium-escritorio-inicio.png).
- [Móvil](navegador/chromium-movil-inicio.png).
- [Recorrido minimizado](navegador/chromium-movil-piloto-minimizado.png).
- [JSON](navegador/chromium-escritorio-json.png) y [cURL](navegador/chromium-escritorio-curl.png).
- [Trabajos](navegador/chromium-escritorio-jobs.png) y [Administración LAB](navegador/chromium-escritorio-lab-administration.png).

## Etapa 2 — Auditoría completada; alta pendiente de aprobación

[Inventario, matriz y procedimiento](IDENTIDADES_LAB.md). No se modificó el núcleo de autorización, el registro, roles, tenants, credenciales, cuotas o políticas durante esta etapa.

El rol apropiado ya existe: **operator con administration=false**. Se proponen `lab-swagger-postman` y `lab-operation-center`, independientes y vinculados al tenant LAB persistente `00000000-0000-0000-0000-000000000001`. No se crearon. La identidad bootstrap es administrativa y no debe distribuirse; approver tiene acceso administrativo de aprobación incluso con administration=false.

El registro actual contiene tres identidades: approver LAB, docs LAB y reader de aislamiento. No hay operadores de integración no administrativos. El procedimiento utiliza el esquema de registro vigente, sin nuevos mecanismos de autenticación, pero **no existe un instalador CLI genérico de API tokens**: docs-rotate/docs-install no sirven para esta alta y no deben relajarse. Se propone instalación manual del secreto bajo cambio autorizado, con validación, archivo privado y remontaje controlado de solo API. Tokens/hashes no aparecen en las evidencias.

Dos identidades del mismo tenant compartirán recursos, cuotas y espacio de idempotencia. La cuota permanece 2 instancias totales y concurrencia 1; no se concede un cupo por token. La rotación/revocación se detalla operativamente, sin añadir campos de expiración que el core no implementa ni emitir secretos ahora.

**Dictamen final:** GO para promoción/contrato/UX del JSON mínimo y continuidad del piloto supervisado KVM 1/2. Preparación IAM lista para revisión, alta de identidades pendiente. **NO-GO de resiliencia integral permanece independiente.** Sin Ceph, producción, Jira, Golden Images o Terraform state modificados.
