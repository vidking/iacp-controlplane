# Resultados del cierre comercial y catálogo de imágenes

Fecha: 25/10/2026, America/Costa_Rica. Rama `feat/fase-g-cierre-ejecucion`, base
`d8ac23e5bfd9d210a3e2f1eec24a1255fb6f7db6`. Candidata sin desplegar.

| Comprobación | Resultado | Alcance |
|---|---|---|
| Regresión completa | PASS: 1021, cero omitidas, 30 avisos, 272,86 s | PostgreSQL/Redis descartables |
| Focalizadas | PASS: 185 | Catálogo, admisión, DHCP, contrato y documentación |
| Cuatro tamaños sin topes heredados | PASS | Derechos y capacidad simulados; no E2E físico |
| Rechazo de tamaños libres, VMIDs e imágenes pendientes | PASS | Sin ejecución de infraestructura |
| OpenAPI 3.1 y ejemplos | PASS | Pendientes con null explícitos, sin inventar capacidades |
| POST de creación comparado con main | Idéntico | El cambio de lectura está en GET /v1/images |
| Manuales | PASS: 22 documentos, 31 contratos | Sin red; guía CRM migrada sin policy UUID ficticio |
| Playwright Chromium escritorio/móvil y Firefox | PASS | Local, cuatro imágenes visibles, cero JS/CDN/mutaciones |
| TLS corporativo y lecturas RO | PASS parcial | Nodo y datastore 200; Ceph 403 |
| Escritura con capacity-ro | 403 real | PUT vacío de configuración, sin tocar VMs |
| Material AES y SSH técnico | PASS de preflight | UID 10001, clave coincide con perfil; sin autenticar guest |
| Inventario de cuatro plantillas | PASS de observación | Template=1, detenidas, Cloud-Init/QGA configurados, sin locks |
| Arranque, DHCP, root, SSH y filesystem de clones nuevos | NO EJECUTADOS | Bloqueo Ceph; no hay evidencia física nueva |
| Promoción / E2E KVM 1 y KVM 2 | BLOCKED | No se promovió ni se crearon VMs |
| Publicación Forgejo | BLOCKED | SSH 2222 rechaza conexión desde estación y VM 104 |

Pruebas completas:
`docker compose -p iacp-g-cierre-real-tests -f compose.test.yaml run --build --rm -T tests python -m pytest -p no:cacheprovider -q`.
Playwright: `node scripts/test-catalog-docs.cjs`, después de comprobar liveness del
servidor descartable. El primer intento coincidió con el arranque del fixture y
no llegó a cargar la página; no se contó como PASS. La ejecución completa posterior
verificó las tres vistas y las cuatro filas visibles mediante geometría de viewport.

- [HTTP, TLS, G6 e inventario sanitizados](operativo.json).
- [Playwright](ui/playwright.json).
- [Catálogo en Chromium escritorio](ui/chromium-escritorio-imagenes.png).
- [Catálogo en móvil](ui/chromium-movil-imagenes.png).
- [Catálogo en Firefox](ui/firefox-escritorio-imagenes.png).
- [Ejemplo JSON escritorio](ui/chromium-escritorio.png) y [móvil](ui/chromium-movil.png).

Las capturas son navegadores reales sobre candidata local; no son certificación
del runtime HTTPS de VM 104. El inventario es evidencia Proxmox de solo lectura,
no una captura simulada ni prueba de clonación. No contiene valores de secretos.

Revisión contextual del escáner: once coincidencias en marcadores ilustrativos,
identidades sintéticas y fixtures negativos; ninguna credencial operativa nueva
en Git. Las capturas se inspeccionaron visualmente. Este resultado no acredita
saneamiento del historial ni borrado de antiguos states.

[Informe y bloqueos](../../../FASE_G_CIERRE_COMERCIAL_IMAGENES.md).
