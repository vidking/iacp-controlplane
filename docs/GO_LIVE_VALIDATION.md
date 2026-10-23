# Evidencias y límites del go-live

**532 pruebas aprobadas**: 496 unitarias y 36 de integración, 87.51 s, sin fallos
ni skips. [JUnit](validation/GO_LIVE_FINAL.junit.xml) e
[informe JSON](validation/GO_LIVE_VALIDATION.json). PostgreSQL 17.11 y Redis 7.4.11
reales desechables; Python 3.12.15 y Terraform 1.16.6. Las advertencias son de
deprecación en dependencias. No se usaron secretos ni volúmenes empresariales.

Tras ajustar la inspección del perfil certification de Compose, aprobaron además
14 pruebas del driver/snapshot en 1.22 s
([JUnit de seguimiento](validation/GO_LIVE_DRIVER_FINAL.junit.xml)). La configuración
Compose consolidada aprobó con valores ficticios: nueve servicios, puertos 80/443
solo en Traefik y diagnóstico 127.0.0.1:8080. No se levantaron recursos de runtime
en esa comprobación.

| Control | Evidencia local | Validación empresarial |
|---|---|---|
| HTTP→HTTPS, cadena, hostname, CA | Traefik real en Docker, certificado efímero | No hay 443 activo; certificado corporativo pendiente |
| Dominio desde red externa | Probe entregado; rechazo de DNS privado cubierto por unidad | DNS consultado en 1.1.1.1 devuelve 192.168.1.50; no éxito externo |
| Swagger/OpenAPI autorizado | Bearer/Basic de docs y gateway HTTPS sintético + Bearer separado | Release anterior aún expone docs solo en loopback |
| Anónimos, roles y tenants | API unitarias e integración PostgreSQL | GET real /v1/instances anónimo 401, autenticado 200 |
| Idempotencia/concurrencia | Integración con locks/índices PostgreSQL y colas Redis | No se enviaron solicitudes reales de energía |
| Publicación/consumo y workers | Integración Celery/Redis; recuperación/fallo de publicación | Workers actuales responden, colas vacías; Lifecycle no desplegado |
| Readiness/esquema | Dependencias/migraciones en PostgreSQL real | API actual ready=200 sobre su esquema 0004; head nuevo pendiente |
| Preservación histórica 0004→0006 | Snapshot de filas/digests en PostgreSQL real, sin backfill | Snapshot real readonly adjunto; migración no ejecutada |
| Wasabi íntegro | Fixtures HTTPS/S3 y controles de versión/ABA | Dos GET reales de VM106 con Version ID, serial y hashes idénticos |
| Energía sin cambios de state | Aislamiento/mutaciones de fixture; comparación antes/después preparada | No se ejecutó power/start; invarianza empresarial pendiente |
| Rechazo sin certificado | Unitarias/integración de API, worker y certificador | Certificado histórico todavía no registrado |
| Interrupción/fallo PVE | Fixtures: UPID conocido, envío ambiguo, timeout, reserva uncertain, sin POST repetido | No se interrumpió ni provocó fallo en Proxmox real |
| Rollback funcional | Recuperación con flag 0, contratos/migraciones y guardas unitarias | No se ensayó un rollback del runtime empresarial |
| Permisos efectivos PVE | Regresión 0/1, ausencia y valores inválidos; fixtures corregidas | GET real concedido en 106 con flag 0; 104 sin PowerMgmt |

La prueba TLS usa una API/gateway de fixture: acredita los routers/middlewares,
no la identidad corporativa. La suite de integración usa PostgreSQL/Redis reales,
pero Proxmox y Wasabi simulados para los escenarios mutables y de fallo. No se
atribuye una mutación real a esas pruebas. El driver se revisó/compiló y sus
guardas se prueban por unidad; no se ejecutó una promoción empresarial completa.

Comandos de reproducción desde el checkout:

```sh
docker compose -p iacp-go-live-validation -f compose.test.yaml run --build --rm -T \
  tests python -m pytest -p no:cacheprovider -o junit_family=xunit1 --junitxml=/tmp/go-live.xml -ra
bash scripts/test-tls.sh
python3 scripts/validate-handbook.py
git diff --check
# Limpiar solo el proyecto local de pruebas, sin -v:
docker compose -p iacp-go-live-validation -f compose.test.yaml down --remove-orphans
```

El primer ensayo TLS detectó una espera de recarga insuficiente en el fixture;
se corrigió para esperar el router HTTP nuevo y el reensayo aprobó todos los
controles. Las ejecuciones locales de pytest impedidas por el sandbox se
detuvieron y se sustituyeron por la suite Docker completa; no cuentan como éxitos.

La [evidencia del entorno](validation/GO_LIVE_ENVIRONMENT.json) y el
[snapshot readonly](validation/GO_LIVE_READONLY_SNAPSHOT.json) contienen solo
metadatos/digests, sin contraseñas, tokens, planes o contenido de state. El
respaldo y restore aprobados previamente se preservaron sin repetir restauración.

Gates aún abiertos: ventana/cambio de despliegue, imágenes de este PR, permiso de
lectura del secreto Lifecycle, certificación offline/registro, DNS público/NAT/
firewall/443, PKI/renovación, gateway administrativo y versión Traefik aprobada,
IPAM de VM106, runner externo y autorización del primer encendido. El hito se
cierra únicamente con prueba externa autorizada y power/start auditado real.
