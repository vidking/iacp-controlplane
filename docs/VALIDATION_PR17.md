# Validación final del PR #17

## Resultado

La revisión de `c0e7bdcb0a250f9a9a3e98874557a51bed1ead4a` queda como candidata
a integración: **342 pruebas ejecutadas y aprobadas, 0 fallidas, 0 errores y
0 omitidas**. Incluye las 17 pruebas antes omitidas y tres pruebas adicionales
del contrato S3. No fue necesario modificar el código de producción ni la
arquitectura. Los cambios posteriores a `c0e7bdc` son pruebas y documentación.

Ejecución final: `2026-10-22T05:55:03Z`, Python 3.12.15, PostgreSQL 17.11,
Redis 7.4.11, Celery 5.5.2 y Terraform 1.16.6.

| Ejecución | Seleccionadas | Aprobadas | Fallidas/errores | Omitidas | Deseleccionadas |
| --- | ---: | ---: | ---: | ---: | ---: |
| Integración originalmente pendiente | 17 | 17 | 0 | 0 | 322 |
| Integración ampliada con S3 | 20 | 20 | 0 | 0 | 322 |
| Suite completa final | 342 | 342 | 0 | 0 | 0 |

La suite completa incluye 322 pruebas unitarias y 20 de integración. No se suman
los replays de integración a las 342 pruebas distintas. Duración final: 42,370 s.
Se observaron siete advertencias de obsolescencia: Starlette/AnyIO y el uso de
`datetime.utcnow()` por el botocore fijado. No son fallos ni pruebas omitidas.

Evidencia exportada de esa ejecución:
[resumen y snapshots](validation/PR17_FINAL_VALIDATION.json) y
[JUnit completo](validation/PR17_FINAL_VALIDATION.junit.xml). El resumen registra
hashes de los archivos de prueba, del JUnit y los digests de imágenes utilizados.

## Diagnóstico del acceso a Docker

El socket local era `root:docker`, modo `0660`, sin ACL adicional. La cuenta
`despinoza` ya figuraba como miembro de `docker` en la base de grupos, pero el
proceso existente solo tenía `despinoza` y `wheel` como grupos suplementarios.
Por eso la apertura del socket devolvía permission denied.

Se ejecutó cada comando Docker en un subproceso `newgrp docker -c ...` que adquirió
la pertenencia ya autorizada. El sistema dispone de `newgrp`; no dispone de `sg`.
Se comprobó acceso a Docker 29.9.1. No se cambió el socket, su ACL, el grupo
persistente ni la configuración del daemon; no se usó sudo ni `chmod 666`.
Para uso normal, una sesión nueva que cargue los grupos actualizados también
resuelve este caso. El acceso al daemon conserva el alcance que la cuenta ya
tenía concedido; no se presentó como un acceso rootless.

Los comandos apuntaron explícitamente a `unix:///var/run/docker.sock` y a
`compose.test.yaml`. Se crearon exclusivamente los proyectos
`iacp-pr17-integration` e `iacp-pr17-final`: red interna, sin puertos publicados,
PostgreSQL tmpfs, Redis sin persistencia, sin secretos ni volúmenes empresariales.
Los workers Celery fueron procesos hijos de los tests y se terminaron al cerrar
los casos. Ambos proyectos y sus redes se retiraron después de exportar el JUnit;
no se borraron volúmenes existentes.

Comando de la integración ampliada, desde la raíz del checkout:

```bash
newgrp docker -c 'docker --host unix:///var/run/docker.sock compose --ansi never --progress plain -p iacp-pr17-integration -f compose.test.yaml run --build --rm -T tests python -m pytest -p no:cacheprovider -m integration -ra'
```

La suite final utilizó otro proyecto y conservó temporalmente el contenedor para
exportar el JUnit antes de retirarlo:

```bash
newgrp docker -c 'docker --host unix:///var/run/docker.sock compose --ansi never --progress plain -p iacp-pr17-final -f compose.test.yaml run --build --name iacp-pr17-final-suite -T tests python -m pytest -p no:cacheprovider -o junit_family=xunit1 --junitxml=/tmp/pr17-final.xml -ra'
```

## Invariantes de lectura y locking comprobados

`tests/integration/test_readonly_state.py` añade pruebas sobre un servidor S3
versionado en loopback. Este servidor rechaza PUT/DELETE al objeto state y registra
todos los métodos. Sus valores son sintéticos; no es Wasabi productivo.

1. **Recovery completo, PostgreSQL real y lector S3 real:** la API publica
   reconcile, se verifica la aprobación y el ejecutor completa la observación.
   Terraform/provider y Proxmox están simulados en este caso; el SDK S3 usa HTTP
   real contra loopback. Solo se registran dos GET del objeto, con Apply habilitado
   en 1. Version ID, lineage, serial y ambos hashes permanecen iguales, y el run
   termina completed sin llamar a `apply_saved`.
2. **Escritura externa durante la observación:** un escritor independiente del
   cliente cambia el objeto almacenado, avanza su versión y serial entre los GET.
   El lector observa realmente esa respuesta nueva, no una metadata inventada en
   el resultado de la comparación. El run queda uncertain con
   `STATE_CHANGED_DURING_OBSERVATION`, `serial_advanced`, sin evento completed.
3. **CLI Terraform 1.16.6 real, backend S3 y locking nativo:** init, plan
   refresh-only y show se ejecutan sobre el servidor controlado con un recurso
   integrado `terraform_data` en módulo. No requiere provider externo ni Proxmox.
   El único PUT y DELETE corresponden a `.tflock`; el objeto state no recibe
   escrituras y su contenido queda idéntico. Los valores se leen de prior_state.

Ejemplo de la evidencia del CLI exportada al JUnit:

| Campo del objeto state | Antes | Después |
| --- | --- | --- |
| Version ID | `fixture-version-1` | `fixture-version-1` |
| Lineage | `d954af88-6ae6-2d04-9ead-55ed1f95d644` | igual |
| Serial | 3 | 3 |
| SHA-256 bytes | `c040077bc42c3f1c6f06e06e77587a10bf19bb9273126338bffeaa0f75c4810a` | igual |
| SHA-256 contenido | `90a34cb8c19009c376ceb235a45d5edc8e10833df4a5c8b652dc242c8aa6dcd2` | igual |

Las pruebas existentes de `test_readonly_reconciliation.py` también mantienen
bloqueos por cambios de lineage, contenido, versión, representación, regresión de
serial, pérdida de lock, cancelación y cambio de aprobación. La prueba con dos
workers en `tests/integration/test_apply.py` pasó con PostgreSQL real: el segundo
worker no alcanza el observador mientras el primero posee el advisory lock;
`/recover` concurrente recibe 409 y solo existe un evento terminal completed.
La suite conserva asimismo redelivery y reinicio con Celery/Redis reales.

Estos resultados verifican el contrato de lectura en un backend S3 controlado.
**No son una medición de Version ID ni de contenido en Wasabi productivo**.
Ese contraste sigue siendo parte de la futura recuperación autorizada, con
escritores externos coordinados; no se accedió a infraestructura productiva.

## Nivel de evidencia de la causa productiva

Se confirmó que **no se conservaron los dos snapshots originales del incidente**.
Los archivos locales disponibles no contienen esa pareja de lecturas. La
reproducción con Terraform 1.16.6 demuestra que el reordenamiento de checks puede
producir el error exacto con state intacto y que la comparación del release base
es defectuosa. No demuestra que ese haya sido el cambio observado en producción.

No es posible excluir retrospectivamente una escritura concurrente, otra
diferencia de contenido o una lectura obsoleta. Un lineage y serial históricos
aislados tampoco permiten hacerlo. El defecto de código está demostrado; la
causa específica del evento productivo permanece **no atribuida**. El PR no debe
anunciar el incidente como resuelto ni afirmar que se contrastaron snapshots que
ya no existen.

## Recuperación del intento 4 y rollback revisados

La [secuencia del intento 4](READ_ONLY_RECONCILIATION.md#recuperación-del-intento-4-existente)
se revisó contra las rutas y el worker. Su único POST operativo es:

```text
POST /v1/instances/a1b2d063-6240-4969-bc04-e7e0e6be9e73/terraform/applies/1750ee70-a6ca-4036-a764-c4b077a0b588/recover
```

Los otros accesos son GET de ejecución, applies e instancia. El endpoint llama a
`recover_apply`, que publica explícitamente `reconcile`; el worker lo traduce a
`execute_apply(..., reconcile=True)`. No pasa por la rama de provisioning.
Conservar aprobación, intento 4, SHA y VMID 106; no solicitar `/apply`, `/retry`,
import ni destroy, ni una aprobación nueva. Un 202 no es confirmación del recurso:
esperar completed/ready y revisar identidades, postcondiciones y snapshots.
El procedimiento no se ejecutó sobre el caso real durante esta validación.

El rollback a `74a1e92` conserva el mismo esquema `0004_authorized_apply`, modelos,
migraciones y Compose de producción: no hubo cambios en esos archivos respecto
de la base. Los nuevos metadatos se guardan en JSON ya existente. Se confirmó la
compatibilidad documental y de esquema, sin realizar un reemplazo operativo en
VM 104. Drenar workers sin interrumpir una mutación activa; conservar proyecto,
volúmenes, secretos, planes y datos; volver a las imágenes anteriores y suspender
recover del release anterior porque reintroduce el falso conflicto. No restaurar
state, VM o SQL para revertir código. Mantener Apply global en **1** durante todo
el procedimiento.

## Cierre para revisión

Se cumplen los gates de pruebas, lectura y concurrencia con la evidencia y límites
anteriores. No hay omisiones críticas pendientes. El PR #17 puede pasar de WIP a
candidato para revisión y posterior merge; no se autoriza merge, despliegue ni
recuperación productiva automática. No cambió `IACP_TERRAFORM_APPLY_ENABLED`, ni
Proxmox, Wasabi productivo, PostgreSQL productivo o VM 104.
