# Resultados de la candidata de cierre G

Fecha: 24/10/2026, America/Costa_Rica. Rama `feat/fase-g-cierre-bootstrap`;
base `29aec3e5b2ad9b09460d8ec63b54cbd162d16bd1`. Evidencia local, sin despliegue.

| Verificación | Resultado | Alcance |
|---|---|---|
| Pytest completo | PASS, 980, sin omitidas | PostgreSQL/Redis descartables; 30 avisos de deprecación |
| Tiempo de suite | 136,97 s | Ejecución final completa |
| AES-GCM, TTL, tenant y expiración | PASS | Redis real de pruebas y dobles controlados |
| Admisión, cuotas y concurrencia | PASS | PostgreSQL de pruebas; capacidad física simulada |
| Contraseña señuelo fuera de DB/cola/artefactos | PASS | Flujo candidato controlado, no historial ni guest real |
| Entrega SSH, host key y stdin | PASS | SSH sustituido por doble; sin contraseña en argv |
| Migración 0011 → 0012 | PASS | Filas históricas preservadas; datos descartables |
| Reversión compatible, admisión cerrada | PASS | Código nuevo, esquema 0012, contrato histórico |
| Manuales | PASS | 22 documentos, 31 contratos autenticados, sin red |
| Playwright Chromium | PASS | Escritorio y móvil, candidata HTTP local autenticada |
| Playwright Firefox | PASS | Escritorio, candidata HTTP local autenticada |
| Recursos externos/errores JS/mutaciones UI | 0 / 0 / 0 | Informe automatizado enlazado |
| Doctor del runtime histórico | PASS | VM 104, sin cambios; evidencia privada en host |
| HTTPS `/health/live` y `/health/ready` | PASS | Runtime histórico conservado, respuestas live/ready |
| E2E real KVM 1/KVM 2 | BLOCKED | Cuota y capacidad física sin acreditar |
| Certificación G sobre HTTPS VM 104 | NO EJECUTADA | No se promocionó |

Comando de suite: `docker compose -p iacp-g-cierre-tests -f compose.test.yaml run --build --rm -T tests python -m pytest -p no:cacheprovider -q`.
Comando documental: `python scripts/validate-handbook.py` en contenedor aislado.
Playwright: `node scripts/test-catalog-docs.cjs`, servidor de pruebas con
mutaciones HTTP deshabilitadas y credenciales sintéticas.

Capturas reales de navegador sobre la **candidata local**, sin extensiones:

- [Chromium escritorio](ui/chromium-escritorio.png).
- [Chromium móvil](ui/chromium-movil.png).
- [Firefox escritorio](ui/firefox-escritorio.png).
- [Resultados y estilos calculados](ui/playwright.json).

No confundir estas capturas con certificación del runtime. La comprobación
física de disco, filesystem y autenticación root no se sustituyó por simulaciones.
Consultar [informe de preparación](../../../FASE_G_CIERRE_PREPARACION.md).

Después del ajuste informativo de esquema en las guías se repitieron 63 pruebas
documentales/de catálogo (PASS) y la matriz Playwright (PASS). El recuento de 980
corresponde a la suite completa; estas 63 no se suman como pruebas distintas.

El escáner de archivos de texto modificados señaló 12 coincidencias: ejemplos
de contraseña explícitamente ilustrativos, identidad sintética documental,
fixtures y un marcador de clave privada usado para comprobar su rechazo.
Revisión contextual sin credenciales operativas ni valores sensibles emitidos.
Las capturas fueron inspeccionadas visualmente; muestran ejemplos ilustrativos.
Este control no certifica el saneamiento de los datos históricos.
