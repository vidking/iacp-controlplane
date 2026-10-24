# Baseline de rendimiento — Fase C

Anotación de Fase D, 24/10/2026: se aplica la [nomenclatura obligatoria](CONVENCIONES_INFRAESTRUCTURA.md) y se añade la [explicación en español de la evidencia original](validation/phase-d/INTERPRETACION_FASE_C_ES.md). Los resultados históricos de Fase C conservan su fecha y alcance.

IACP Control Plane (VM 104), IACP 0.10.1 / `4f153e8`, cliente externo Locust `927a5c5`, 24/10/2026. No se desplegó optimización en el runtime durante esta medición.

## Carga readonly supervisada

Corrida `20261024T092210Z-u10`, 240 s configurados, 10 usuarios, 5 RPS globales, presupuesto 1.600 requests, cero creación/energía/DELETE. [Estadísticas sanitizadas y hash del CSV original](validation/phase-c/HTTP_5RPS.json).

| Ruta | Requests | Errores | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| Agregado | 1.204 | 0 | 71 | 450 | 530 |
| GET /v1/operations | 288 | 0 | 380 | 530 | 640 |
| GET operación individual | 138 | 0 | 38 | 64 | 150 |
| GET inventario | 363 | 0 | 67 | 130 | 200 |
| GET instancia individual | 206 | 0 | 32 | 59 | 82 |
| GET energía física | 76 | 0 | 230 | 290 | 310 |
| GET OpenAPI autenticado | 68 | 0 | 150 | 240 | 370 |
| GET Swagger autenticado | 65 | 0 | 29 | 54 | 120 |

RPS medido: 5,0034; máximo agregado 874,27 ms. Percentiles aproximados del histograma Locust, no media de percentiles por ruta. La muestra acredita cuatro minutos y esa mezcla de lecturas; no capacidad productiva ni una prueba de quince minutos por etapa. No se mezcló con aprovisionamiento real.

El p95 global de 450 ms cumple <500 ms en esta corrida; operaciones sigue incumpliendo por ruta. La medición comunicada de 1.104/p95 520 ms permanece como antecedente sin CSV localizado: no se afirma mejora antes/después contra una ventana cuya mezcla no se pudo verificar.

### Corrida excluida

La configuración privada previa tenía 10 RPS. Un fallo del encadenamiento del preflight permitió iniciar la corrida `20261024T092134Z-u10`; se interrumpió con SIGINT alrededor de 35 s y se reemplazó por una configuración nueva readonly a 5 RPS. [CSV resumido excluido](validation/phase-c/HTTP_EXCLUDED.json): 263 requests/0 errores en el CSV, frente a 266 al cierre de consola por completaciones durante apagado. Configurada a 10 RPS, tasa observada del CSV 7,51 RPS. No se usa para aprobar ningún límite. No produjo mutaciones.

## Evidencia antes de optimizar

[Perfil readonly en el proceso observador dentro del contenedor API](validation/phase-c/QUERY_PROFILE_BEFORE.json). Mismo código y DB desplegados; estas llamadas directas excluyen middleware, autenticación HTTP y red del cliente.

| Página | SQL por llamada | Tiempos totales ms, tres muestras | SQL acumulado ms |
|---|---:|---|---|
| 1 operación | 10 | 61,522 / 9,753 / 9,479 | 20,185 / 4,055 / 4,121 |
| 10 operaciones | 67 | 51,171 / 50,659 / 50,587 | 22,170 / 21,857 / 21,861 |
| 50 operaciones | 325 | 213,615 / 215,032 / 211,015 | 78,397 / 78,873 / 76,552 |
| 100 operaciones | 648 | 415,561 / 412,801 / 411,759 | 143,678 / 144,196 / 142,716 |

Hay 167 operaciones del tenant. El SELECT principal con LIMIT 50 ejecutó en 0,230 ms; PostgreSQL eligió scan secuencial de 167 filas + top-N heapsort de 52 kB, 13 buffers hit, sin lecturas físicas ni temporales. Esa elección es razonable para este tamaño. Los índices existentes están conservados en el perfil; no se añadió un índice sin evidencia de necesidad.

La ruta de listado selecciona la página y luego llama al handler individual por cada fila, que vuelve a seleccionar la operación y consulta eventos y cuatro tipos de ejecución. Creaciones añaden ejecución/approval/fulfillment. **Inferencia apoyada por el conteo:** el trabajo repetido de ORM/SQL y serialización de eventos contribuye a la latencia del listado; el plan del SELECT principal no la explica por sí solo. No se atribuye todo el p95 a una causa única.

GET de energía física hizo tres GET Proxmox secuenciales por muestra: permisos, config y estado actual. Sumaron aproximadamente 173–192 ms; llamadas directas totales 177–198 ms. Se consultó VM106 únicamente por lectura; `mutations_available=false`, `POWER_INSTANCE_NOT_AUTHORIZED`. El endpoint sigue informando guest_ready desconocido.

Middleware registra duración/route/status/request_id sin cuerpos. Esta instrumentación no separa por sí sola serialización, espera de pool, cada consulta y logging. Las muestras de la VM 104 muestran 8 CPU, unos 15,8 GiB, carga baja y más de 14 GiB disponibles; no hay evidencia de saturación sostenida de CPU/RAM. Los snapshots puntuales no excluyen picos breves. [Durante](validation/phase-c/HOST_DURING.json), [después](validation/phase-c/HOST_AFTER.json), [actividad DB y Redis](validation/phase-c/RUNTIME_AFTER.json).

## Cambio puntual y comparación del candidato

[PR #45](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane/pulls/45), commit `ba88083ed5a2622e2e83402ce6f1e49860c4b3f3`: carga por lotes de eventos, ejecuciones y approval de la página ya autorizada. Comparte la proyección con el detalle y conserva tenant, filtros, orden, errores, incertidumbre y todos los eventos. No introduce caché ni cambia `/v1`.

Integrado en main remoto como `7379e840155e8a8b426c6d05a3683fa6a4a1229e` a las 18:16:24 UTC. Esa integración no convierte la observación del candidato en una medición HTTP desplegada.

Comparación **secuencial**, tres muestras por página y versión, sobre las mismas 167 operaciones: [antes](validation/phase-c/QUERY_PROFILE_SEQUENTIAL_BEFORE.json), [candidato](validation/phase-c/QUERY_PROFILE_SEQUENTIAL_AFTER.json), [comparación verificable](validation/phase-c/PERFORMANCE_COMPARISON.json). El candidato se cargó exclusivamente en el proceso observador de una llamada `docker exec`, con transacciones SQL readonly; no se reemplazaron archivos, rutas ni procesos Uvicorn. Los doce hashes de payload coinciden por página/repetición.

| Página | SQL antes → candidato | Mediana directa antes → candidato, ms |
|---|---:|---:|
| 1 | 10 → 9 | 12,324 → 9,131 |
| 10 | 67 → 9 | 50,910 → 11,537 |
| 50 | 325 → 9 | 211,388 → 20,946 |
| 100 | 648 → 20 | 413,829 → 50,173 |

Quedan consultas adicionales de fulfillment para algunas creaciones; no se afirma complejidad constante para todas las páginas. Son muestras pequeñas de llamadas directas, sin middleware ni red HTTP; no equivalen a p95 desplegado. Una exploración anterior se ejecutó en paralelo y se conserva identificada por `QUERY_PROFILE_MATCH_BEFORE` / `QUERY_PROFILE_CANDIDATE_AFTER`; sus tiempos no sustentan esta comparación secuencial.

Regresión dirigida de listado/contratos: 108 aprobadas, 31,60 s. Los dos casos nuevos verifican límite de consultas, tenants distintos, filtros/paginación, los cuatro tipos de ejecución, creación uncertain y equivalencia con detalle sin truncar eventos. El caso de conteo falló antes del cambio con 98 consultas para 12 filas y pasó después.

**No existe comparación HTTP desplegada después de optimizar en esta entrega.** Después del despliegue revisado, repetir la misma mezcla, población, política y límite de 5 RPS con calentamiento separado y una ventana sostenida compatible con el presupuesto autorizado. Si se exige una ventana de 15 minutos, resolver primero su presupuesto: no elevar cuotas para conseguirla. Registrar p50/p95/p99 por ruta y global, errores, RPS, duración, CPU/RAM, conexiones/locks y progreso de workers.
