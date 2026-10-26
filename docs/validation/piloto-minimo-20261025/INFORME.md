# Piloto LAB: token existente, JSON mínimo y recorrido plegable

Fecha: **25/10/2026, America/Costa_Rica**. Base: main `96118bfdb074906c5bee59f9cd728fa14e5d2131`. **Candidata validada para revisión; no fusionada ni desplegada.** El IACP Control Plane (VM 104) conserva su release certificado `ab36a06db3cefb12cd3004cf7921e2af54333c28`.

## Resultado

`POST /v1/instances` admite nombre, plan, imagen y acceso como cuerpo mínimo únicamente para KVM 1/2 y Ubuntu 24.04 en el piloto LAB autorizado. La red omitida resuelve `lab-dhcp`, sin fallback; el contexto omitido se conserva como ausencia, sin fabricar un pedido comercial. La solicitud completa anterior continúa siendo válida.

El tenant procede de la identidad Bearer opaca existente; no se acepta tenant_id ni se crean tenants por petición. El consumidor configura `IACP_API_TOKEN` en su backend. No se introducen tokens JWT, nuevos headers de autenticación, roles ni concesiones operativas. Los ejemplos nuevos pertenecen a la candidata: antes de utilizarlos contra VM 104 deberá integrarse y promoverse esta revisión.

## Alcance de código y seguridad

- `commercial_contract.py`: network y commercial_context pasan a omitibles. No aceptan null explícito. Cuando se envía contexto, order_id continúa siendo obligatorio. Acceso root, tipos, límites y rechazo de campos adicionales se conservan.
- `services/commercial.py`: admite la omisión solo después de comprobar derechos comerciales, perfil autorizado LAB y plan/imagen del piloto. Se mantienen cuota, capacidad real, reserva, concurrencia, locks y cifrado efímero G6.
- `openapi_presentation.py`: ejemplo mínimo adicional, conservando los dos ejemplos completos y los mismos endpoints, operationId y respuestas.
- La respuesta 202 permanece íntegra. La documentación destaca instance_id, status y GET /v1/instances/{instance_id}; conserva seguimiento técnico para diagnóstico. Ready no equivale a acceso listo: se comprueban access.status e IP.
- Sin cambios en autenticación, Compose, credenciales, Alembic, Terraform/provider, state, catálogos, cuotas operativas, RBAC, Golden Images, Ceph o Jira. No hubo conexiones administrativas ni promoción a VM 104.

## Idempotencia y datos históricos

Para cuerpos completos se conserva **exactamente** el algoritmo previo de fingerprint y su forma persistida; una prueba calcula el digest anterior y exige igualdad. No se reescriben registros ni referencias históricas.

Los cuerpos con omisiones utilizan un marcador interno `lab-dhcp-v1` y la lista de campos omitidos. La huella identifica la intención recibida; la especificación almacena además los valores efectivos y el perfil revisado. Un retry idéntico devuelve los mismos IDs antes de volver a resolver perfiles o reservar recursos, incluso si cambia el perfil actual. No cambia el presupuesto ni reencola planificación.

Cambiar entre omisión y valor explícito cambia la representación del cuerpo: con la misma Idempotency-Key devuelve 409. Cambiar contraseña, claves SSH, plan, nombre, arranque o contexto tampoco puede reemplazar silenciosamente la intención original. El mecanismo de recuperación de secretos expirados conserva la misma instancia para cuerpos completos y mínimos; una entrega incierta continúa por observación.

## UX y documentación

La tarjeta principal es un `<details open>` nativo, completamente plegable. Sus ocho pasos también se pliegan: acceso, JSON, cURL, respuesta, seguimiento/IP, SSH, Lifecycle y eliminación. El JSON se muestra inicialmente; los pasos restantes se abren desde sus encabezados o el índice. Minimizar el recorrido oculta su contenido completo, manteniendo el título.

El controlador existente abre los ancestros necesarios en navegación profunda y conserva scroll, historial y estado activo. No se introduce otra biblioteca ni un controlador paralelo. Identidad visual, ancho y recursos locales conservados; las guías avanzadas permanecen disponibles.

El JSON y cURL se actualizaron después de las primeras pruebas del contrato mínimo. El cURL usa el API token ya configurado o lo solicita sin eco, no fabrica order_id y solicita la llave pública para el recorrido SSH. La contraseña continúa siendo proporcionada por el consumidor y nunca se incorpora como valor real al repositorio.

## Pruebas y evidencias

- **1.087 aprobadas, cero omitidas**, 32 advertencias de deprecación, 164,39 s: [regresión completa](regresion-completa.log), con PostgreSQL 17 y Redis 7 descartables locales.
- **96 aprobadas** en [revalidación focalizada](regresion-focalizada.log).
- [OpenAPI de candidata](openapi-candidata.json), válido 3.1, y [diff contractual](diff-contractual.json). La prueba estricta exige que solo los dos campos indicados dejen de ser requeridos, además de anotaciones/ejemplos. Todos los demás schemas, respuestas, cabeceras, seguridad, rutas y operationId permanecen iguales.
- Pruebas HTTPS TestClient con token de servicio registrado: tenant persistente, reader rechazado, tenant inactivo rechazado, aislamiento de lectura, tenant sin derechos rechazado, tenant_id/recursos libres rechazados, Idempotency-Key requerida y WAN bloqueada.
- Dos tenants autorizados con la misma clave mantienen intenciones distintas y sus respectivos propietarios. Dos reintentos mínimos concurrentes en PostgreSQL real publican un solo job. La cuota concurrente también se prueba con ambos cuerpos.
- Bootstrap G6 parametrizado para completo/mínimo: entrega, limpieza, duplicado de tarea, expiración/reentrega sin clonación nueva, resultado incierto y protecciones. Se comprueba ausencia del señuelo en PostgreSQL/logs/variables de planificación y se conserva la batería existente de Redis/cola cifrada.
- Lectura de registros completos y mínimos al seleccionar el router histórico con admisión cerrada: PASS. Esto no equivale a probar un downgrade físico de binarios; ver reversión.

[Playwright](navegador/playwright.json): Chromium escritorio 1536×1000, Chromium móvil 390×844 y Firefox escritorio. Comprueba apertura inicial, minimización total, reapertura desde menú, plegado y apertura de pasos mediante índice, contenido visible, posición bajo cabecera, copia, deep links, recarga, Atrás/Adelante, guías y los siete grupos Swagger. Try it out ejecuta solamente GET /v1/images en el fixture local de solo lectura.

Copia real exacta del portapapeles Chromium para JSON/cURL en escritorio y móvil. Firefox confirma escritura nativa; no se afirma lectura de clipboard no soportada por sus permisos Playwright. Cero errores JavaScript, cero dependencias externas y cero mutaciones de infraestructura durante la matriz.

Capturas de candidata, no de VM 104:

- [Escritorio abierto](navegador/chromium-escritorio-inicio.png).
- [Móvil abierto](navegador/chromium-movil-inicio.png).
- [Móvil minimizado](navegador/chromium-movil-piloto-minimizado.png).
- [JSON mínimo](navegador/chromium-escritorio-json.png) y [cURL](navegador/chromium-escritorio-curl.png).

Las primeras pruebas señalaron dos aserciones del contrato anterior que todavía exigían network/contexto. Se actualizaron conforme al cambio aprobado y se repitió la suite completa. Los registros aprobados no se presentan como pruebas físicas nuevas de KVM.

## Integración, promoción y reversión

Un único PR incorpora implementación, pruebas, manual y evidencias disponibles. No se inicia una promoción antes de su integración. No se alteraron instancias históricas ni Terraform state y no se crearon VMs de ensayo.

Después del merge: identificar el SHA real integrado, construir las imágenes de aplicación correspondientes, verificar ausencia de trabajos/colas/locks, preparar respaldo restaurable y manifiesto revisado y usar iacpctl/go_live.py. Conservar secretos, perfiles, cuotas, esquema 0012, imágenes anteriores y state. Repetir pruebas HTTPS sobre el runtime realmente desplegado antes de declarar publicada esta entrada mínima.

No hay migración de datos ni downgrade Alembic. Un binario anterior puede leer los registros, pero **no sabe admitir o recuperar el cuerpo mínimo mediante POST**. Antes de retroceder, cerrar nuevas admisiones y resolver/observar las intenciones mínimas activas con código que soporte esta revisión. No regenerar su fingerprint ni convertirlas a un pedido comercial ficticio para forzar un replay. Mantener la candidata disponible para recuperación controlada de secretos pendientes; no prometer una reversión transparente con trabajos mínimos en curso.

**Dictamen:** implementación compatible validada para revisión. El GO KVM 1/2 existente se conserva en VM 104; no se afirma E2E físico del cuerpo mínimo ni certificación de despliegue. La resiliencia integral continúa NO-GO por su evidencia independiente pendiente.
