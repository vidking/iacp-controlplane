# Fase G: verificación del desbloqueo de admisión

Fecha de cierre de pruebas: 25/10/2026, 02:29, America/Costa_Rica.
Alcance: candidata local; **no desplegada** en el IACP Control Plane (VM 104).

## Regresión y corrección del manifiesto

La corrida completa con PostgreSQL 17 y Redis 7 descartables ejecutó 1038 casos:
**1037 PASS y un FAIL**, sin omitidos, en 272,39 s. El fallo fue la comprobación
del hash histórico de `executor.py`, modificado para incorporar la barrera.
Se conserva su [registro original](regresion-inicial.log).
Las copias versionadas solo normalizan espacios finales y líneas vacías al final;
conservan íntegros los resultados, el fallo y sus diagnósticos.

Se registraron explícitamente los hashes de `executor.py` y el archivo nuevo
`execution_health.py` en el manifiesto de cambios de la candidata. No se modificó
el manifiesto histórico. La prueba ahora verifica también archivos nuevos del
manifiesto, en lugar de limitarse a la lista histórica. Esto identifica el cambio
para revisión; no sustituye el merge ni una aprobación administrativa.

Tras ese ajuste exclusivo de manifiesto y prueba se repitieron las suites de
salud, clonación, admisión y contrato: **136 PASS**, cero omitidos, en 32,10 s.
Incluyen el caso que había fallado. [Registro de revalidación](revalidacion.log).
No se presenta la corrida inicial como verde ni la revalidación como una segunda
corrida completa. El código funcional no cambió entre ambas ejecuciones.

Las 17 pruebas nuevas de salud cubren HEALTH_OK/WARN/ERR, salud ausente o inválida,
permisos denegados, indisponibilidad y timeout, almacenes no disponibles,
cancelación, varios datastores, destinos no Ceph y degradación posterior al plan.
Comprueban que un fallo impide consumir autorización, crear el plan o llamar
a `apply`, según el punto de control. La admisión confirma que no consulta Ceph.

Los servicios de pruebas se retiraron con `docker compose down --remove-orphans`,
sin `-v`. No se usaron PostgreSQL ni Redis de VM 104 para pruebas de escritura.

## Contrato

Comparación completa de OpenAPI contra la candidata anterior `14966da`, en
procesos separados, para ambos valores de `IACP_CREATION_CONTRACT`:

| Contrato | OpenAPI | Resultado | SHA256 del JSON canónico |
|---|---|---|---|
| historical | 3.1.0 | Idéntico | `4354255fe42c22330ee33961807f87646d7066171ee7fd9ae19673b5056047d3` |
| commercial-g | 3.1.0 | Idéntico | `a1057933b7dcb02ba14871cd7dd944991eff6208a5ef37ab5c27d882202a09ca` |

La incompatibilidad comercial ya declarada respecto del contrato histórico no
desaparece: esta comparación verifica que el correctivo no añade otra revisión.
No se modificó Swagger ni su presentación. No se repitió Playwright porque este
correctivo no altera interfaz, recursos, rutas ni OpenAPI.

## Lecturas operativas y límites de la evidencia

[Evidencia sanitizada](operativo.json): capacity-ro devuelve HTTP 200 con TLS
en nodo y datastore. La barrera candidata, ejecutada en memoria contra HTTPS real
con la identidad Terraform, devuelve `EXECUTION_CEPH_NOT_HEALTHY`, sin apply.
Esto demuestra contención real, no despliegue ni aprovisionamiento certificado.

`iacp-lab` contiene las cuatro plantillas y VM 104/106; las cuatro tienen permiso
efectivo de clonación. La certificación de cada imagen se mantiene separada.
Persisten HEALTH_ERR y permisos excesivos de Terraform, incluidos sobre VM 106.
No se realizaron cambios administrativos en MNG.

G6: las pruebas de cifrado/TTL, filtración y bootstrap están incluidas en la
regresión; acceso root real en un nuevo guest sigue sin certificarse.
Promoción, E2E kvm-1 y E2E kvm-2: **BLOCKED, no ejecutados**.
Publicación: **BLOCKED**, Forgejo rechazó la conexión SSH al intentar fetch.
Dictamen: **NO-GO FASE G**; runtime estable conservado.
