# Explicación en español de las evidencias del PR #46

Anotación de Fase D, 24/10/2026. Fuente histórica: PR #46, integrado como `7c0804d5f6106a7142a14b7bb649fe3e5c6c43aa`. Los archivos originales de [Fase C](../phase-c/) permanecen intactos. Esta versión traduce sus explicaciones sin alterar fechas, identificadores, métricas ni resultados. [Nomenclatura](../../CONVENCIONES_INFRAESTRUCTURA.md).

## Versiones y pruebas

[CHANGESET.json](../phase-c/CHANGESET.json): dictamen histórico NO-GO para certificación integral. La corrección local estaba validada; faltaban despliegue e interrupción real aislada. Los merges #44/#45 observados no fueron ejecutados por esa sesión. No cambiaron contratos públicos, motor/módulos/provider/backend, Compose o cuotas. No hubo mutaciones de VMs, borrado de versiones Wasabi ni reescritura de main. Sus referencias de despliegue corresponden al **IACP Control Plane (VM 104)**.

[TEST_RESULTS.json](../phase-c/TEST_RESULTS.json): imagen y dependencias locales existentes, sin descargas; repositorio solo para lectura; entorno vacío con variables permitidas; red Docker interna sin salida externa; PostgreSQL 17 y Redis 7 descartables. Límites: 3 CPU, 2 GiB, 256 procesos, 768 MiB temporales, 128 MiB por archivo y 360 segundos finales. Las bases `iacp_test_*` y Redis DB 15 eran exclusivas del ensayo.

| Nombre técnico original | Explicación en español |
|---|---|
| `initial baseline unit run` | 807 aprobadas, un fallo y 49 excluidas. El límite de archivo estaba expresado en bytes por error; se corrigió. No es una regresión del producto. |
| `C-02 regression before fix` | Una aprobada y una fallida: Terraform/provider sintéticos siguieron activos tras SIGKILL; el fixture los retiró. |
| `P0 directed regression after fix` | 34 aprobadas después de la primera corrección. |
| `full regression with first six Phase C cases` | 863 aprobadas para la primera ampliación. |
| `scanner semantic tests and C-01/C-02 regressions` | Seis aprobadas para escaneo y recuperación simulada. |
| `C-04/C-05/C-06/C-12 integration and scanner semantic tests` | Seis aprobadas con dependencias locales aisladas. |
| `directed runner/apply/process regression after independent review` | 70 aprobadas tras atender la revisión independiente. |
| `P0 full regression` | 873 aprobadas; su XML completo fue efímero y no quedó archivado. |
| `external client offline regression` | 36 aprobadas sin red, con advertencia opcional del detector de caracteres. |
| `archived directed Phase C regressions` | 16 aprobadas con XML dirigido conservado. |
| `operations batching and public-contract regressions` | 108 aprobadas. Antes, el caso de conteo falló con 98 consultas para 12 filas; la proyección mixta ya coincidía. |
| `final combined full regression` | 875 aprobadas, cero errores/fallos/omisiones; árbol equivalente a main `7379e84`, XML completo conservado. |

El padre capturó el XML final acotado desde stdout, validó tamaño/estructura/contadores y creó el artefacto exclusivamente. No montó un directorio retenido con escritura dentro del ejecutor. Dos intentos intermedios detenidos para incorporar correcciones no cuentan como completos. C-02 conservó una mutación simulada y el mismo VMID/key/intento; cercó la redelivery y cerró mediante reconciliación readonly explícita, no reparación automática de infraestructura.

## P0 y rendimiento

[P0_REVIEW.json](../phase-c/P0_REVIEW.json): revisión independiente del supervisor. Se atendieron la ventana de excepción durante arranque y la ambigüedad del canal de estado antes de aceptar el candidato. La aprobación de código no certifica recuperación física: faltaba el ensayo con una VM descartable del **clúster MNG**, sin afectar otros trabajos del **IACP Control Plane (VM 104)**.

[PERFORMANCE_COMPARISON.json](../phase-c/PERFORMANCE_COMPARISON.json): observadores secuenciales dentro del contenedor API, SQL readonly y tres muestras por página/versión sobre 167 operaciones. No se reemplazaron archivos, rutas o procesos del servidor. Coincidieron doce pares de hashes. Para 50 filas: 325→9 consultas y mediana directa 211,388→20,946 ms. La muestra es pequeña, excluye HTTP/middleware/red y conserva consultas de fulfillment por creación. Los perfiles paralelos fueron exploratorios y no sustentan la comparación secuencial. Faltaba medir HTTP tras desplegar.

[HTTP_5RPS.json](../phase-c/HTTP_5RPS.json): `Aggregated` significa agregado; `authorized`, acceso autenticado; `physical power`, consulta de energía física. Son etiquetas de rutas, no permisos de mutación. Se observaron 1.204 solicitudes, cero errores y p95 global 450 ms durante 240 segundos. Los percentiles aproximados provienen del histograma Locust, no de promediar percentiles por ruta.

[HTTP_EXCLUDED.json](../phase-c/HTTP_EXCLUDED.json): corrida excluida por configuración previa a 10 RPS. Se interrumpió y sustituyó. Sus 263 solicitudes del CSV y tasa observada de 7,51 RPS no acreditan cumplimiento de 5 RPS. No produjo mutaciones de VMs.

## Cierre y seguridad de evidencias

[LOCAL_SANDBOX_CLOSURE.json](../phase-c/LOCAL_SANDBOX_CLOSURE.json): regresión combinada completa y retirada exclusiva de contenedores/red propios con etiqueta `iacp.phase=c-local`. Bases en tmpfs; sin borrado de volúmenes de usuario, imágenes o recursos ajenos. `MNG_services_interrupted=0` expresa ausencia de interrupciones de servicios del **IACP Control Plane (VM 104)** y del **clúster MNG**. Se conservaron worktrees/evidencias; no hubo limpieza Docker indiscriminada.

[SECRET_SCAN.json](../phase-c/SECRET_SCAN.json): refs Git disponibles localmente y blobs de texto de hasta 16 MiB; refs no descargadas y capturas quedaron sin inspeccionar. `review-required` requiere revisión; `synthetic-or-reference` identifica fixture/referencia. No se emitieron valores.

[SECRET_REVIEW.json](../phase-c/SECRET_REVIEW.json): revisión independiente de 47 candidatos: 36 fixtures, nueve nombres de pruebas y dos referencias; cero credenciales reales confirmadas en ese alcance. Otras 34 ubicaciones se clasificaron como sintéticas/referencias. No demuestra ausencia universal de secretos ni revocación de los expuestos en capturas.

[SECRET_DIFF_SCAN.json](../phase-c/SECRET_DIFF_SCAN.json): escaneo de archivos nuevos/modificados. El primer intento no tenía Git en la imagen; el padre suministró las rutas obtenidas por Git para completar el escaneo.

[SECRET_DIFF_REVIEW.json](../phase-c/SECRET_DIFF_REVIEW.json): revisión del padre de las ubicaciones del diff: identificadores de pruebas, marcador BEGIN sin cuerpo de llave y contraseña de fixture. Cero credenciales reales confirmadas; no fue auditoría independiente exhaustiva ni revisión de capturas.

[SECURITY_AUDIT_STATUS.json](../phase-c/SECURITY_AUDIT_STATUS.json): auditoría rápida acotada, incompleta por límite de capacidad de agentes. Una unidad cubierta, dos diferidas y sin crítica final de cobertura. Sí terminaron la revisión independiente del P0 y las disposiciones de candidatos. Cero hallazgos confirmados no equivale a seguridad certificada.

En Fase D el usuario difirió D6 y la eventual rotación para después del MVP. No se identificaron verificablemente los Bearer de las capturas ni se afirmó su revocación, rotación o expiración. No se modificaron credenciales; este pendiente se registra separado de las actividades operativas autorizadas.
