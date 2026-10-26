# GO — Documentación MVP publicada y certificada por HTTPS

Fecha: **25/10/2026, America/Costa_Rica**. Promoción completada a las **19:00:43** y activación comercial verificada a las **19:02:27**. Los timestamps técnicos UTC originales se conservan con conversiones en [fechas-costa-rica.json](fechas-costa-rica.json).

**GO para la documentación MVP publicada en https://iac.cyberfuel.com/docs.** Certificación sobre el sitio real, no sobre la candidata local. Se conserva el GO del piloto supervisado KVM 1/2; la resiliencia integral continúa NO-GO.

## Versión y promoción efectivas

| Elemento | Antes | Después |
|---|---|---|
| Release operativo | `dedb344b49d401c4da00d0271882881a0ee34dae` | `ab36a06db3cefb12cd3004cf7921e2af54333c28` |
| Integración | Fase G y piloto KVM 1/2 | Main integra PR #75 y `3bc7a6514748347f4b025e70fd36239f00db0b51` |
| Imagen local API/worker/Lifecycle | `iacp/controlplane-api:fase-g-e855677` | `iacp/controlplane-api:docs-ab36a06` |
| ImageID API | `sha256:511c251830375a201cf6377f477774d07f39c992383cc589decc1fe9ec9ccfa6` | `sha256:be95d630c280a153ce91a1b5cc4cd2594e3e234b5f4b970bf8e4fd3f6aa4e0c6` |
| Imagen Terraform | `iacp/terraform-worker:fase-g-e855677` | Misma imagen y ImageID `sha256:c1157faf2bb71d6469d83c51a6c975b95be24dd8d05610d39c0bfe5c515da3c5` |
| Producto / esquema | 0.10.1 / `0012_commercial_intents` | Sin cambios |

[codigo-runtime.json](codigo-runtime.json) compara **101 archivos reales del contenedor API** con el main aprobado: igualdad completa. No se deduce el SHA solamente del nombre de una imagen. El futuro commit de estas evidencias es documental y no constituye otra versión desplegada. Los ImageID son locales; no se publicó un registry OCI.

Se ejecutaron `iacpctl env-promote`, `go_live.py` mediante `iacpctl deploy`, activación comercial revisada y `iacpctl doctor`. El preflight pasó antes de promover. La API se detuvo mediante el driver para cercar admisión, con snapshot y workers previamente sin actividad. PostgreSQL, Redis y Traefik conservaron sus contenedores; los procesos de aplicación se recrearon de manera controlada. No se introdujeron migraciones, nuevas dependencias ni contratos.

El primer preparador detectó que la variable operativa real es `IACP_RELEASE`, no `IACP_API_IMAGE`; se corrigió únicamente ese preparador temporal antes de instalar el candidato. El entorno activo no había sido modificado por ese intento. No se relajó ningún validador.

## Respaldo y reversión disponibles

Respaldo privado nuevo: `/opt/iacp/backups/docs-ab36a06-0012.dump`, SHA256 `f4906d0a824affbe1a040f32807a9d5d1d390c7ba3d81edc9d536391e7888495`. Restauración real en PostgreSQL 17 aislado, sin red y con almacenamiento temporal: esquema 0012, 16 tablas, PASS. El contenedor de restauración fue retirado. [Evidencia](operacion/docs-ab36a06-preparacion/restauracion.json). El dump no se incorpora a Git.

Backup del entorno: `/opt/iacp/backups/config/server.env.20261026T005928Z`, correspondiente al **25/10/2026 18:59:28 Costa Rica**. Se preservaron las imágenes previas, datos, volúmenes, certificados y secretos.

Manifiesto efectivo privado: `/etc/iacp/changes/manifest-docs-ab36a06.json`. Release del host: `/opt/iacp/releases/iacp-docs-ab36a06`. El registro de cambio recoge la autorización expresa del responsable, la ventana local y las verificaciones reales; no reutiliza la ventana vencida de Fase G.

Reversión funcional preparada mediante el mismo `iacpctl rollback` y manifiesto revisado, conservando datos/state y cerrando admisión. Para restaurar la presentación anterior, utilizar las imágenes retenidas compatibles con 0012 y el procedimiento revisado. **No se ejecutó rollback:** no apareció una regresión que lo requiriera. No usar downgrade de datos, `down -v`, borrado de state ni desbloqueos forzados.

## Certificación de navegador sobre HTTPS real

Se ejecutó `scripts/test-pilot-docs.cjs` con `IACP_DOCS_BASE=https://iac.cyberfuel.com` y autenticación Bearer de automatización ya existente, entregada al proceso por stdin privado. No se copiaron secretos a Git ni se imprimieron sus valores. TLS se validó normalmente, sin `ignoreHTTPSErrors` ni conexiones inseguras.

[Resultado Playwright](navegador/playwright.json): **Chromium escritorio, Chromium móvil y Firefox escritorio PASS; 102 mediciones geométricas**. Para los siete grupos se comprobó clic real, encabezado bajo cabecera, contenido visible, estado activo, cierre/reapertura y recarga. Incluye explícitamente Trabajos y Administración de laboratorio.

También pasaron guías locales, índices internos, hashes normalizados, enlace a operación, Atrás/Adelante y recarga sin crecimiento del historial, apertura de OpenAPI en otra pestaña, menú móvil y ausencia de desbordamiento horizontal inicial. Se verificó la referencia secundaria a Forgejo; la lectura principal no depende de autenticar en Forgejo.

- Copia de JSON y cURL: igualdad exacta con el **portapapeles real de Chromium** en escritorio y móvil.
- Firefox: finalización de `writeText` nativo; no se afirma lectura de clipboard, no soportada por los permisos Playwright de ese navegador.
- Try it out: **GET /v1/images HTTP 200** sobre la API real con identidad autorizada. No se ejecutó ningún endpoint de mutación.
- Cero errores JavaScript, cero solicitudes externas y cero métodos de escritura durante la matriz. Swagger y sus recursos son locales.

### Capturas reales

- [Portada escritorio](navegador/chromium-escritorio-inicio.png).
- [Portada móvil](navegador/chromium-movil-inicio.png).
- [JSON](navegador/chromium-escritorio-json.png) y [cURL](navegador/chromium-escritorio-curl.png).
- [Trabajos](navegador/chromium-escritorio-jobs.png).
- [Administración de laboratorio](navegador/chromium-escritorio-lab-administration.png).

Se incluyen las capturas de todos los grupos en los tres modos. Fueron inspeccionadas visualmente las portadas de escritorio y móvil.

## Contrato, ejemplos y recorrido completo

OpenAPI **3.1.0**, idéntico antes/después sin normalizaciones: [comparación y SHA256](contrato.json), [antes](openapi-antes.json), [después](openapi-despues.json).

Se descargaron el JSON y la respuesta **efectivamente servidos** por HTTPS y se validaron con `CommercialInstanceCreate` e `InstanceAccepted` dentro del contenedor API activo. El cURL servido pasó `bash -n`. [Prueba de ejemplos](ejemplos-https.json). La plantilla requiere valores propios del consumidor; no contiene contraseña o token reales. La respuesta ilustrada conserva los IDs reales de la VM de Fase G ya eliminada y lo indica expresamente.

El recorrido crear → operación → instancia/IP → SSH → Lifecycle → DELETE se respalda con los schemas actuales, los enlaces/códigos servidos y [los E2E físicos KVM 1/2 existentes](../phase-g/cierre-mvp-20261025/INFORME.md). **No se repitieron aprovisionamientos, SSH ni Lifecycle físicos en esta promoción documental.** No se presenta una aceptación 202 como prueba nueva de una VM creada.

La regresión previa al merge del PR #75 fue 1.064 pruebas completas y 69 focalizadas; sus registros se conservan en [la certificación de candidata](../docs-piloto-20261025/INFORME.md). Esta promoción añadió smoke y pruebas reales HTTPS, sin atribuirse otra ejecución de esa suite completa.

## Estado final y restricciones

Doctor PASS sin deriva ni rutas inesperadas; API, worker, Terraform Worker, Lifecycle Worker, PostgreSQL, Redis y Traefik operativos. Esquema 0012; cero operaciones no terminales, colas vacías y cero mensajes sin confirmar en la captura de cierre. [Evidencias operativas](operacion/docs-ab36a06-doctor/).

[Smoke de autenticación](autenticacion-smoke.json): sin credenciales `/docs`, assets y OpenAPI responden 401 con challenge HTTP Basic; `/v1/plans` responde 401 Bearer. Health/live y readiness responden 200. La automatización autorizada obtuvo acceso a documentación/API; no se afirma una nueva prueba de contraseña HTTP Basic interactiva de Desarrollo.

Los límites comerciales efectivos y perfiles permanecen idénticos: KVM 1/2, tenant LAB existente, máximo 2 instancias, 4 vCPU, 6144 MiB, 120 GiB y concurrencia 1. El aprovisionamiento real sigue sujeto a autorización/capacidad. No se modificaron VM 104 desde la API, Golden Images, RBAC, Ceph ni Jira. Las otras imágenes, KVM 3/4, WAN y resiliencia física integral no reciben certificación por este correctivo.

**Dictamen:** GO para documentación MVP publicada y piloto supervisado KVM 1/2; NO-GO separado para resiliencia integral.
