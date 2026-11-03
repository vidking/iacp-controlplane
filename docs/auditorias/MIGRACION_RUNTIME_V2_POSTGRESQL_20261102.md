# Migración del runtime a controlplanev2 conservando PostgreSQL

## Alcance

- Base del cambio: `origin/main` de `iacp-controlplane`, commit
  `0c857c373f0a9f148ed3b40ff7f8dfc66359a61f`.
- Fuente funcional: `controlplanev2/develop`, commit
  `4903395face37aff78caa3ec4046dfaef7ed1bb0`.
- Contrato público: API `v1`, versión `1.0.0`, dominio
  `iac.cyberfuel.com`.
- Persistencia: PostgreSQL 17. Wasabi continúa deshabilitado para el entorno
  lógico `lab`.
- Destino habilitado: clúster MNG, VMID `30000–39999`; PRX sigue apagado.

## Cambios preparados

- Se trasladaron al repositorio `iacp-controlplane` el API, los modelos, los
  servicios, los workers, la integración de red y el módulo KVM de
  `controlplanev2`.
- Compose define `worker-1` a `worker-6`; API, manifiestos y workers comparten
  `IACP_MAX_CONCURRENT=6`.
- La capa PostgreSQL conserva el volumen y añade de forma aditiva
  las columnas de stack y contexto comercial, la tabla `ip_allocations` y la
  versión de esquema 2. No importa datos SQLite ni reinicializa el volumen.
- Las secciones que dependían de `BEGIN IMMEDIATE` usan un advisory lock
  transaccional PostgreSQL para serializar claims y reservas de VMID entre los
  seis procesos.
- La defensa de recursos conserva las VM protegidas configuradas (104, 106 y
  las Golden Images 29000, 29001, 29010 y 29011) y los VMID adicionales fijados
  por entorno. La API rechaza Lifecycle y reconciliación antes de encolar; los
  workers y el asignador vuelven a comprobarlo antes de actuar. Compose pasa
  la misma lista a API y workers, y `iacpctl preflight` verifica que coincida.
- La documentación visual conserva `/v1` y usa superficies blancas con
  acentos azules para producción.

## Verificación local

| Comprobación | Resultado | Evidencia |
| --- | --- | --- |
| Suite API, workers, NetBox y gates de promoción | PASS | `103 passed` contra PostgreSQL 17.6 temporal local. |
| Claims concurrentes y reservas VMID | PASS | Seis claims y seis VMID únicos; el asignador rechaza los VMID protegidos. |
| Mutaciones de VM protegida | PASS | API y workers rechazan power, reconcile, apply y destroy para VM 104, VM 106 y Golden Image 29000. |
| Compose del candidato | PASS | `docker compose config --quiet`; declara PostgreSQL, API y los seis workers. |
| Imagen Docker del candidato | PASS | `iacp-controlplane-candidate-local:20261102`; `terraform init` terminó para MNG y PRX. |
| Validación Terraform | PASS | `terraform validate` dentro de la imagen para MNG y PRX. |
| Gate `config-check` | PASS | Manifiesto temporal y valores ficticios; sin conexión a servicios externos. |
| Gate `preflight` | PASS | `/tmp/iacp-workers6-final-gate-20261102/evidence-preflight-final`; Wasabi apagado, sin llamadas a Wasabi o Proxmox. |
| Runtime en contenedores locales | PASS | PostgreSQL esquema 2, API `ready`, seis heartbeats saludables `idle`, `max_concurrent=6`, cola y operaciones activas vacías; OpenAPI `1.0.0`, sin `/v2`. |
| Baseline del IACP Control Plane (VM 104) | PASS | Antes del cambio, API healthy en imagen `050748b28601949c967c7ad69d6457aba4b79548`, dos workers idle, `IACP_MAX_CONCURRENT=1`, MNG habilitado, Wasabi apagado; PostgreSQL reportó 0 instancias, 0 operaciones, 0 leases held y 0 advisory locks. |
| Volúmenes de PostgreSQL y workdata | IDENTIFICADOS | `iacp_controlplane_prod_postgres` y `iacp_controlplane_prod_workdata`; conservar para mantener PostgreSQL y permitir rollback. |
| Inventario previo del clúster MNG | PASS | Consulta Proxmox de solo lectura: 7 recursos y cero VMID entre `30000–39999`; no se iniciaron ensayos físicos. |
| IACP Control Plane (VM 104) | SIN CAMBIOS | No se ha desplegado ni reiniciado el runtime candidato. |
| Aprovisionamientos físicos en el clúster MNG | PENDIENTE | Aún no se han iniciado; requieren release integrado y despliegue verificado. |

## Criterio de continuación

Esta evidencia cubre integración local, no certificación física. Antes de
probar, el release deberá estar integrado en `origin/main`; después se
verificarán imagen, migración PostgreSQL, ocho servicios del stack, seis
heartbeats y cola vacía en el IACP Control Plane (VM 104). Las cuatro pruebas
físicas se ejecutarán en dos rondas de hasta dos aprovisionamientos en el
clúster MNG.

## Adenda del 2026-11-02: seis workers y contrato de tags

Durante la revisión posterior se encontró que el Compose local del repositorio
todavía declaraba dos workers y concurrencia uno. Se corrigió para declarar
`worker-1` a `worker-6`, compartir el volumen de trabajo y usar el límite global
de seis; el Compose de Portainer ya tenía esa capacidad.

También se cerró la diferencia entre el contrato público y la guía: `tags` es
ahora un arreglo de etiquetas Proxmox adicionales en `POST /v1/instances`,
`commercial_context.customer_tag` se aplica automáticamente y el nombre no
genera etiquetas. Swagger documenta patrón y longitud, el request rechaza
campos extraños y las dos interfaces producen el mismo arreglo JSON. Las guías
ahora describen las rutas reales de catálogo; no anuncian `/v1/environments` ni
un selector `environment` en el cuerpo.

| Comprobación adicional | Resultado | Evidencia |
| --- | --- | --- |
| Suite API, workers y gates | PASS | `106 passed` contra PostgreSQL 17.6 temporal local; incluye una regresión del Compose local para seis workers y concurrencia seis. |
| Formulario `/docs` en Chromium | PASS | JSON con tags deduplicados y `customer_tag`; tag inválido bloqueado antes de enviar `POST /v1/instances`. |
| Compose local | PASS | API + seis workers; `IACP_MAX_CONCURRENT=6`; volumen de trabajo compartido. |
| Compose Portainer | PASS | API + seis workers; concurrencia seis; Wasabi y PRX apagados. |
| Imagen y Terraform | PASS | Imagen local `sha256:2a7bc77c96a204b965cb4a1d1e8596c1e5453095e7ee9585c872dcbf189ed0ab`; `terraform validate` pasó para MNG y PRX. |
| Runtime aislado en contenedores | PASS | PostgreSQL esquema 2, API `ready`, seis heartbeats saludables, `max_concurrent=6`, cero instancias/operaciones/locks; no se llamó Proxmox ni Wasabi. Se retiraron contenedores, red y volúmenes temporales tras verificar. |
| Sintaxis JavaScript/Python y `git diff --check` | PASS | Sin errores de sintaxis ni whitespace. |
| IACP Control Plane (VM 104) y clúster MNG | SIN CAMBIOS | Sin despliegue ni aprovisionamientos físicos; las cuatro pruebas siguen pendientes de release integrado. |
