# Percentiles por endpoint de Fase E

Fecha local: 2026-10-24T16:05:07.699404-06:00. IACP Control Plane (VM 104), IACP 0.10.1 / `7c0804d`.

Percentiles calculados de solicitudes individuales; no promedio de percentiles. En 20 RPS se usa la observación completa de una etapa interrumpida, incluido calentamiento. Los códigos 429 permanecen en las estadísticas; no se interpreta su latencia como throughput exitoso.

| Nivel | Ventana | Endpoint | Solicitudes | Errores | p50 ms | p95 ms | p99 ms |
|---|---|---|---:|---:|---:|---:|---:|
| 5 | 180 s, sin calentamiento | `GET /docs` | 47 | 0 | 29.481 | 38.341 | 43.326 |
| 5 | 180 s, sin calentamiento | `GET /openapi.json` | 57 | 0 | 155.030 | 269.129 | 294.262 |
| 5 | 180 s, sin calentamiento | `GET /v1/instances` | 299 | 0 | 87.073 | 116.644 | 350.130 |
| 5 | 180 s, sin calentamiento | `GET /v1/instances/{id}` | 155 | 0 | 33.829 | 42.681 | 53.699 |
| 5 | 180 s, sin calentamiento | `GET /v1/operations` | 229 | 0 | 151.041 | 226.911 | 303.580 |
| 5 | 180 s, sin calentamiento | `GET /v1/operations/{id}` | 113 | 0 | 36.395 | 47.399 | 48.503 |
| 10 | 180 s, sin calentamiento | `GET /docs` | 105 | 0 | 26.568 | 46.550 | 155.907 |
| 10 | 180 s, sin calentamiento | `GET /openapi.json` | 115 | 0 | 146.450 | 256.486 | 276.849 |
| 10 | 180 s, sin calentamiento | `GET /v1/instances` | 542 | 0 | 80.945 | 116.451 | 186.563 |
| 10 | 180 s, sin calentamiento | `GET /v1/instances/{id}` | 338 | 0 | 30.722 | 43.990 | 134.195 |
| 10 | 180 s, sin calentamiento | `GET /v1/operations` | 451 | 0 | 128.157 | 225.573 | 315.686 |
| 10 | 180 s, sin calentamiento | `GET /v1/operations/{id}` | 249 | 0 | 34.409 | 49.724 | 199.791 |
| 20 | Observada, interrumpida | `GET /docs` | 64 | 0 | 30.960 | 137.198 | 335.887 |
| 20 | Observada, interrumpida | `GET /openapi.json` | 42 | 0 | 144.952 | 306.592 | 370.385 |
| 20 | Observada, interrumpida | `GET /v1/instances` | 260 | 24 | 82.746 | 158.350 | 434.294 |
| 20 | Observada, interrumpida | `GET /v1/instances/{id}` | 125 | 18 | 32.827 | 148.996 | 217.654 |
| 20 | Observada, interrumpida | `GET /v1/operations` | 238 | 0 | 124.685 | 254.626 | 420.646 |
| 20 | Observada, interrumpida | `GET /v1/operations/{id}` | 72 | 4 | 36.843 | 87.000 | 114.765 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /docs` | 1141 | 0 | 26.437 | 45.729 | 123.164 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /openapi.json` | 1147 | 0 | 161.950 | 278.989 | 387.363 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /v1/instances` | 5584 | 0 | 83.174 | 130.145 | 273.397 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /v1/instances/{id}` | 3369 | 0 | 30.253 | 46.646 | 111.321 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /v1/operations` | 4419 | 0 | 151.150 | 263.904 | 427.478 |
| 10 sostenidos | 1800 s, sin calentamiento | `GET /v1/operations/{id}` | 2340 | 0 | 33.130 | 50.411 | 134.842 |
