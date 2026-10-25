# Correctivo documental: crear una VM piloto desde IACP

Fecha: **25/10/2026, America/Costa_Rica**. Alcance: presentación y navegación de documentación; sin nuevas funcionalidades del orquestador.

## Resultado y estado de despliegue

**Candidata validada. No desplegada.** La portada comienza con «Crear una VM piloto — Ubuntu 24.04», utiliza KVM 1 y `lab-dhcp`, y recorre autenticación, JSON, cURL, respuesta, seguimiento, IP, SSH, Lifecycle y DELETE. Conserva identidad visual, ancho, Swagger, OpenAPI, autorización y Try it out.

Base Git: main `5916a6df21fcec09e21215dafd24605dd8fb474c`, que integra PR #73. El último release operativo certificado sigue siendo dedb344. Esta rama requiere revisión e integración antes de promocionar; no se confunde certificación local con HTTPS del nuevo diseño en VM 104.

Se consultaron por HTTPS el OpenAPI y los catálogos desplegados, perfiles, salud y uso LAB. [Inventario real](inventario-runtime.json). KVM 1/2 y Ubuntu 24.04 disponibles; las otras imágenes permanecen pendientes. No se modificaron cuotas ni se aprovisionaron/eliminaron VMs en este correctivo.

## Causas corregidas

1. La portada mezclaba revisión comercial vigente, introducción extensa y ejemplos históricos con recursos libres/UUID de política. Se reemplazó el recorrido principal por el contrato comercial certificado; la referencia avanzada conserva límites, recuperación y trazabilidad sin presentar peticiones obsoletas como ejecutables.
2. El controlador podía localizar el `<h3>` de Swagger sin abrir el grupo real. Ahora utiliza las acciones de estado de Swagger para abrir grupos y operaciones, incluidos Trabajos y Administración de laboratorio.
3. El plugin nativo de deepLinking de Swagger 5.34.0 escribe `history.pushState` al ejecutar `layoutActions.show`; competía con el historial propio. Se conserva el formato de los enlaces de operaciones y se asigna historial/scroll a un único controlador mediante la API de plugins. `deepLinking: false` desactiva esa escritura duplicada nativa; no elimina los deep links, que se verifican por clic, URL directa, recarga y Atrás/Adelante.
4. Existe un contenedor interno Swagger `id="operations"`. Se evita confundirlo con la categoría funcional Operaciones: las categorías se reconocen por su mapa de menú y se resuelven a `operations-tag-*`.
5. Los últimos grupos necesitan recorrido de scroll suficiente para alinearse bajo la cabecera. Se añade espacio final exclusivamente al main exterior, con altura calculada del viewport; todos los destinos comparten medición de cabecera + 20 px y requestAnimationFrame, sin sleeps arbitrarios. No se alteró el ancho del contenido.

## Datos y ejemplos

[`piloto.json`](../../../app/iacp/static/docs/piloto.json) es la fuente del JSON y de la respuesta. El JSON es una **plantilla para una nueva intención**, con nombre/pedido sustituibles y marcador explícito de contraseña; no se presenta como una solicitud ya ejecutada. El cURL solicita contraseña única y llave pública del consumidor, genera un cuerpo privado y lo elimina después del envío. No incluye credenciales reales ni una identidad inventada.

La respuesta mostrada procede literalmente de la admisión E2E KVM 1 de Fase G: instancia `b50a71f8-210c-44da-b5ca-f73b7ce00ee9`, operación `8cf904bd-459d-469e-b553-40acc7dede9f`, VM 30023 posteriormente eliminada. La página explica que el consumidor debe extraer **sus propios IDs** de `respuesta.json`.

El flujo exige Bearer/tenant/derechos/cuota/capacidad vigentes. No pide UUID de política comercial ni aprobación individual. SSH requiere la llave privada correspondiente a la pública enviada y verificar el host por el canal autorizado; no habilita root SSH por contraseña. DELETE comercial usa `{}` tras shutdown terminal y ausencia de trabajos activos. No se inventa una nueva certificación física: se reutilizan las evidencias [Fase G](../phase-g/cierre-mvp-20261025/INFORME.md), con su alcance e incidentes conservados.

## Validaciones

- Suite completa: **1.064 pruebas aprobadas, cero omitidas**, 30 advertencias de deprecación, 158,63 s. [Registro](regresion-completa.log).
- Revalidación focalizada de documentación y catálogo: **69 aprobadas**. [Registro](regresion-documental.log). Incluye validación Pydantic/JSON Schema, respuesta real y ejecución del generador Python extraído del cURL efectivamente renderizado, sin enviar una mutación.
- OpenAPI **3.1.0** validado y contrato completo igual al HTTPS desplegado, incluidos schemas, campos, operationId, cabeceras, seguridad y respuestas. La única diferencia del fixture es su `servers` de loopback para que Try it out no contacte infraestructura real; se normaliza explícitamente antes de comparar. [Diff y SHA256](contrato-comparado.json).
- Playwright: Chromium escritorio (1536×1000), Chromium móvil (390×844) y Firefox escritorio. [Resultado](playwright.json).
- Todos los grupos: clic, contenido desplegado, estado activo, cierre y reapertura, coordenadas bajo cabecera y recarga del hash. Guías locales, índices, hashes normalizados, enlace a operación, Atrás/Adelante y recarga sin crecimiento de historial.
- Copia JSON/cURL: comparación exacta con portapapeles real en Chromium escritorio/móvil. Firefox verifica finalización de escritura nativa; no se afirma lectura de clipboard, cuyo permiso Playwright no soporta allí.
- Try it out: autorización con identidad **sintética del fixture** y GET `/v1/images` HTTP 200. El fixture selecciona explícitamente commercial-g y su servidor descartable rechaza mutaciones, y el navegador bloquea destinos externos y métodos de escritura. No utiliza credenciales de laboratorio.
- Sintaxis del cURL efectivamente renderizado: `bash -n` PASS. El punto de entrada anterior `scripts/test-catalog-docs.cjs` delega en la matriz completa para evitar una segunda batería obsoleta.
- Apertura real de OpenAPI en pestaña local; fuente Forgejo identificada como enlace secundario. Recursos locales, cero errores JavaScript, cero dependencias externas y cero mutaciones en la prueba de navegador.

Las primeras ejecuciones detectaron aserciones antiguas de la portada y la colisión de `operations`; se corrigieron y se repitieron las comprobaciones. Solo los registros finales aprobados se incluyen como evidencia de aprobación.

## Capturas de la candidata

- [Portada escritorio](chromium-escritorio-inicio.png).
- [Portada móvil](chromium-movil-inicio.png).
- [JSON](chromium-escritorio-json.png) y [cURL](chromium-escritorio-curl.png).
- [Trabajos](chromium-escritorio-jobs.png).
- [Administración de laboratorio](chromium-escritorio-lab-administration.png).

Se conservan capturas de los siete grupos para cada navegador/viewport. Son capturas reales del **fixture local de la candidata**, sin extensiones; no se presentan como diseño ya desplegado por HTTPS.

## Reproducción

Usar la imagen de tests soportada, con el checkout candidato montado de solo lectura y dependencias de test aisladas. El servidor `tests/serve_catalog_docs.py` debe exponerse solo en loopback y usa SQLite temporal, identidad sintética y barrera contra métodos de escritura. Nunca cargar secretos LAB en ese servidor.

```bash
IACP_DOCS_EVIDENCE_DIR=/ruta/nueva/privada \
  node scripts/test-pilot-docs.cjs
```

Playwright debe estar disponible en el entorno Node existente. El script escribe resultados y capturas; no instala bibliotecas ni contacta CDN. Por defecto usa `http://127.0.0.1:8765`; no ejecuta una creación real.

## Promoción preparada y pendientes

Ver [DESPLIEGUE.md](DESPLIEGUE.md). No ejecutar promoción antes del merge ni declarar cerrado el correctivo web hasta repetir la matriz sobre HTTPS real. El GO previo del piloto supervisado KVM 1/2 se conserva y la resiliencia integral permanece NO-GO.

No se modificaron API funcional, workers, motor IaC, estado, base de datos, Compose, credenciales, RBAC, Ceph, Golden Images o Jira. La certificación KVM 3/4 y las otras imágenes queda pospuesta por esta prioridad documental.
