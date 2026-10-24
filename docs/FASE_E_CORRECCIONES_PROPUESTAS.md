# Correcciones pequeñas y comparativas de aceptación

Fecha: 24/10/2026, zona `America/Costa_Rica` (UTC−06:00). Aplicación evaluada:
IACP 0.10.1 / `7c0804d`, en el **IACP Control Plane (VM 104)**. No se modifica
la aplicación desplegada, Compose, credenciales, workers ni backend en esta fase.

Este documento distingue las comparativas medidas de las repruebas propuestas.
Los percentiles y el límite HTTP definitivo están en la matriz de capacidad de
Fase E. No se atribuye al hardware una limitación impuesta por admisión.

## 1. Admisión HTTPS por fuente

Se observó `request-rate.average=10`, `burst=20`, `period=1s` y
`inFlightReq.amount=40`. Es una política vigente. Aumentar usuarios no la
elimina. Su instancia en memoria es por router y fuente: la mezcla observada
cruza `iacp-crm` e `iacp-admin`. No es un techo global agregado de 10 RPS.
El benchmark conserva una sola fuente y no reparte identidades/IPs
para conseguir más admisión. Los RPS enviados y los exitosos deben mostrarse
por separado; un 429 rápido no acredita que la API sostenga ese throughput.

Corrección pequeña del consumidor: mantener presupuesto agregado por origen,
reservar margen para polling/UI y aplicar espera acotada con jitter ante 429.
No reintentar mutaciones con otra key ni reemplazar una VM por un rechazo.
Para la demo se recomienda el antecedente conservador de 5 RPS agregados.

Comparativa antes/después propuesta: reproducir la misma secuencia en un
fixture HTTP que imponga 10 RPS/ráfaga 20; comparar cliente sin presupuesto y
cliente con presupuesto agregado, verificando cero pérdidas de intención y
menor tasa 429. Reprueba de regresión: las dos intenciones simultáneas conservan
UUID/key, el reintento respeta deadline, y 401/403/422 continúan deteniendo el
flujo. **No se relajó el limitador del runtime para medir un “después”.**

## 2. Costo de historial y continuación de aprovisionamiento

El observador SQL dirigido midió nueve sentencias para páginas de 1, 10 y
50 operaciones, y veinte para una página de 100 sobre 238 operaciones. Las
medianas observadas de tiempo total fueron 10,560 / 12,718 / 21,928 / 53,287 ms,
respectivamente. La muestra incluye el contexto real de ese momento y no es
un contador exacto de SQL de todas las requests Locust.

`list_operations` ya precarga relaciones por página. Sin embargo,
`services/fulfillment.py::progress` consulta start, inicialización, política y,
cuando corresponde, acceso por cada creación con `start_on_create`. Esa ruta
explica por qué el lote de Fase D no acota todo el trabajo del contrato CRM.
Es un costo cuantificado; no se demostró saturación de PostgreSQL a las tasas
admitidas. Los waits, conexiones y CPU medidos delimitan esa conclusión.

Corrección pequeña propuesta: precargar únicamente las continuaciones de las
creaciones autorizadas de la página y permitir que `progress` reciba ese
contexto de lectura. Conservar exactamente la proyección del detalle, el filtro
de tenant, los estados `uncertain` y la ausencia de secretos. No añadir caché
global de operaciones ni cambiar políticas.

Comparativa antes/después propuesta: mismo snapshot de 100 operaciones,
calentamiento idéntico, diez repeticiones alternadas, payload canónico idéntico,
sentencias acotadas por página y tiempos mediana/p95. La medición “antes” queda
en `PERFIL_SQL_FISICO.json`; el “después” requiere implementar y revisar el
candidato, por lo que **permanece pendiente**.

Regresión necesaria: 1/10/50/100 creaciones con `start_on_create`, combinando
start/init/access ausentes, queued/completed/uncertain; dos tenants; comparación
de cada fila con el detalle; paginación/filtros; recuento de consultas constante.
La prueba actual de batching con creaciones genéricas sin autostart no basta.

## 3. Serialización real del IaC Worker

El comando desplegado conserva `--concurrency=1`. El E2E individual midió
25,502 s de aprovisionamiento y 0,028 s de cola; el doble midió 50,451/56,823 s
de aprovisionamiento y 0,026/8,261 s de cola. Hubo un solo Apply consumido por
VM. El doble demuestra dos flujos admitidos, no dos procesos Terraform
simultáneos. Los episodios `uncertain` y la reconciliación readonly impiden
atribuir toda la diferencia temporal exclusivamente a la cola o al worker.

Corrección pequeña propuesta para la demo/cliente: mostrar posición/espera y
seguir la misma operación con polling acotado, sin nuevas inyecciones mientras
hay dos flujos activos. Mantener concurrencia y cuotas; elevar workers o
aprovisionamientos requiere otra evaluación y queda fuera de esta fase.

Comparativa antes/después propuesta: misma secuencia individual/doble y mismas
cuotas, con tiempos de espera visibles y presupuesto de polling agregado;
contrastar 429, número de solicitudes y tiempo a terminal. Regresión: nunca
más de dos aprovisionamientos, un Apply por VM y limpieza PG/PVE/S3 completa.
No se presenta una mejora temporal no medida.

## Lecturas físicas de Proxmox

Tres observaciones dirigidas readonly de energía de la VM 106 histórica
protegida midieron 202,178 / 164,907 / 190,679 ms. Cada una hizo tres GET
(`access/permissions`, config y status); las mutaciones continuaron bloqueadas
con `POWER_INSTANCE_NOT_AUTHORIZED`. No se estresó esa ruta ni se mutó la VM.
La escalera excluye `/power`: extrapolar sus RPS a llamadas físicas sería
incorrecto. Una eventual reducción de polling requiere otra comparación
idéntica y no una caché que oculte cambios de energía.

## Límites de la comparación

No se fuerza una optimización de API para superar una política de admisión.
Las comparativas entre páginas, individual/doble y niveles de RPS son mediciones
distintas; no son un antes/después causal de un cambio de código. Las propuestas
anteriores requieren sus propios PR y regresiones antes de promoción. La
pérdida del diagnóstico inicial tras reconciliación continúa pendiente, al igual
que la certificación física de muerte del worker.

La carga sostenida también dejó dos ventanas cortas con p95 >500 ms y un
máximo individual de 1,867 s, aunque el agregado cumple. Su causa permanece
sin determinar. Antes de corregir API/DB/red, una reprueba debe capturar
`X-Request-ID`, duración del cliente y tiempos de servidor correlacionados,
manteniendo esta mezcla y el mismo instrumental. No se propone aumentar
workers o relajar el limitador para ocultar esos picos.
