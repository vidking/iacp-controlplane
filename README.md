# IACP — Control Plane técnico

La ruta operativa de esta rama es el runtime técnico simplificado de KVM:
`Dockerfile.technical`, `compose.technical.yaml` y la migración
`migrations/versions/0001_technical_schema.py`. Se ejecuta directamente en el
**IACP Control Plane (VM 104)** y conserva PostgreSQL dedicado, State remoto en
Wasabi y seis workers con un máximo de dos aprovisionamientos físicos
simultáneos.

`iacp-controlplanev2` continúa como repositorio y orquestador independiente. No
se migran sus datos, volúmenes, operaciones ni estados `uncertain`. Portainer no
forma parte del runtime técnico de esta rama.

Para validar esta ruta sin infraestructura física:

```bash
bash scripts/test.sh
```

La entrada operativa técnica se invoca como `scripts/iacpctl --technical` y
exige un archivo privado `/etc/iacp/technical.env`:

```bash
scripts/iacpctl --technical config-check
scripts/iacpctl --technical deploy
scripts/iacpctl --technical status
```

El despliegue directo usa un archivo privado basado en
[`.env.technical.example`](.env.technical.example):

```bash
docker compose --env-file /etc/iacp/technical.env -f compose.technical.yaml up -d --build --wait
```

No ejecutar `docker compose down -v`; PostgreSQL, planes, secretos y State
remoto son persistentes. Los archivos `compose.yaml`, `app/` y la documentación
comercial que siguen en el repositorio son material histórico de la plataforma
anterior y no forman parte de este runtime técnico.

Forgejo es la fuente oficial: [CyberFuel/iacp-controlplane](https://scm.cyberfuel.com/CyberFuel/iacp-controlplane).
El código y los contenedores son portables entre equipos autorizados. El destino
operativo es VM 104 `iacp-controlplane-01`; el equipo de desarrollo solo ejecuta
entornos locales independientes.

La denominación operativa es **IACP Control Plane (VM 104)**. **MNG** designa
exclusivamente el clúster de laboratorio `mng01`/`mng02`; **Platinum/PRX** es el
clúster de producción excluido de estos ensayos. Consultar las [convenciones de idioma e infraestructura](docs/CONVENCIONES_INFRAESTRUCTURA.md).

Contrato KVM/CRM 0.10.1 (frontera neutral; flujo de negocio de 0.10.0): [runbook](docs/MVP_CRM_RUNBOOK.md),
[checklist de acreditación](docs/MVP_CHECKLIST.md), [contrato IaCoC](docs/IACOC_CONTRACT.md)
y [auditoría Wasabi](docs/STATE_RETENTION_AUDIT.md). Locust vive en un proyecto
externo de pruebas manuales y no forma parte del runtime IACP.

Autoservicio multiinstancia 0.9.1: [guía para Desarrollo y Swagger](docs/SELF_SERVICE.md),
[contrato futuro CRM](docs/CRM_INTEGRATION.md) y [evidencia de validación](docs/validation/SELF_SERVICE.json).
[Release desplegada, cuatro KVM reales y limpieza](docs/SELF_SERVICE_VALIDATION.md).

Entrada operativa canónica: [scripts/iacpctl](scripts/iacpctl) y
[docs/OPERATIONS.md](docs/OPERATIONS.md). `deploy/mng` es el primer entorno de
validación; la normalización no depende del clúster. Las credenciales docs-only
se rotan separadas de los permisos Bearer de la API.

Go-live MVP: [operación y rollback](docs/GO_LIVE_MVP.md),
[seguridad](docs/GO_LIVE_SECURITY.md), [consumo API](docs/GO_LIVE_API.md) y
[evidencias/bloqueos](docs/GO_LIVE_VALIDATION.md).

Fase C de resiliencia (24/10/2026): [dictamen NO-GO y prioridades](docs/MVP_GO_NO_GO.md),
[auditoría y alcance](docs/RESILIENCE_CERTIFICATION.md), [matriz C-01…C-12](docs/FAILURE_MATRIX.md),
[recuperación controlada](docs/RECOVERY_RUNBOOK.md) y [rendimiento medido](docs/PERFORMANCE_BASELINE.md).

Fase E (24/10/2026, Costa Rica): [capacidad, sostén, recuperación y regresión real](docs/FASE_E_CAPACIDAD.md),
[evaluación del simulador aislado](docs/FASE_E_SIMULADOR.md) y
[correcciones pequeñas propuestas](docs/FASE_E_CORRECCIONES_PROPUESTAS.md).
Los PASS locales y las correcciones integradas no certifican los ensayos físicos pendientes en MNG.

## Clonar y probar

Se requieren Git, Docker con contenedores Linux y Docker Compose 2.24 o posterior.
La autenticación de Git y el acceso al daemon Docker se configuran en cada equipo.
No se requieren Python, PostgreSQL, Redis, OpenSSL ni paquetes de la aplicación en
el host para ejecutar las pruebas Docker.

```bash
git clone https://scm.cyberfuel.com/CyberFuel/iacp-controlplane.git
cd iacp-controlplane
git switch main
docker compose -p iacp-tests -f compose.test.yaml run --build --rm -T tests
docker compose -p iacp-tests -f compose.test.yaml down --remove-orphans
```

En un shell Bash, la validación completa incluye suite, imagen de runtime,
Traefik y conservación de datos y secretos tras recrear contenedores:

```bash
bash scripts/validate-docker.sh
```

El script exige un endpoint Docker local y usa proyectos nuevos de prueba. No
elimina volúmenes. Consultar [desarrollo](docs/DEVELOPMENT.md) y la
[evidencia de validación](docs/VALIDATION_INCREMENT1.md).

## Desarrollo local

Copiar `.env.dev.example` a `.env.dev` y ajustar el nombre de proyecto y puerto si
es necesario. El archivo local no se versiona. No copiar secretos del servidor.

```bash
docker compose --env-file .env.dev -f compose.dev.yaml up -d --build --wait
docker compose --env-file .env.dev -f compose.dev.yaml run --rm -T --no-deps dev-check
```

La API estará en `http://127.0.0.1:18080/docs` con la configuración de ejemplo.
Swagger/OpenAPI requieren token del tenant: challenge de navegador con usuario
`iacp` y token como contraseña; Authorize HTTPBearer para llamadas /v1.
Los secretos se generan dentro de un volumen exclusivo de desarrollo y se
conservan al recrear los contenedores. La guía de desarrollo explica cómo usar
el token sin depender de rutas o identidades del host.

## Configuraciones separadas

| Archivo | Propósito | Datos y secretos |
| --- | --- | --- |
| `compose.test.yaml` | Pruebas automatizadas efímeras | PostgreSQL tmpfs, Redis sin persistencia; sin secretos del servidor |
| `compose.dev.yaml` | Runtime local completo | Volúmenes y secretos propios por proyecto de desarrollo |
| `compose.yaml` | Configuración compatible con el bootstrap | Volúmenes y archivos de secretos existentes |
| `compose.server.yaml` | Transición aprobada del bootstrap a Git | Exige nombres de volúmenes externos y rutas explícitas de secretos existentes |
| `compose.tenants.yaml` | Override optativo de identidades para la API | Registro de credenciales externo a Git |
| `compose.terraform.yaml` | Worker optativo de planificación Terraform | Clon readonly, credenciales externas y volumen privado de planes |

Los archivos de desarrollo y pruebas son independientes; no combinarlos con
`compose.yaml`. El archivo del servidor solo se combina con `compose.yaml` tras
inventariar el entorno. El cambio de directorio del código no debe cambiar el
proyecto Compose ni crear otra base de datos por accidente.

## Alcance del entregable 2A

Se conserva API → Redis → Celery → PostgreSQL. La API está organizada en módulos,
usa contratos Pydantic y [OpenAPI versionado](docs/openapi.json), y el esquema se
administra con Alembic. `POST /v1/instances` acepta peticiones idempotentes por
tenant y devuelve 202; Celery ejecuta simulación por defecto. El inventario y las
operaciones con eventos quedan persistidos. No se ejecuta Terraform ni Proxmox.
La [guía de 2A](docs/DELIVERY_2A.md) describe contratos, identidad por tenant,
roles mínimos, migración `0002`, fallos y límites de recuperación.

La [auditoría y los siguientes incrementos](docs/AUDIT_PHASE1.md) cubren tenants,
RBAC, instancias, idempotencia, outbox, recuperación, auditoría e `IaCExecutor`.
Los [gates operativos](docs/NEXT_GATES.md) incluyen respaldo y restauración probada.

## Preparación del incremento 2B

El [ejecutor Terraform](docs/TERRAFORM_EXECUTOR.md) usa el stack KVM original de
`CyberFuel/iac-proxmox` en un commit fijo, con disco configurable y state Wasabi
estable por instancia. Se integra con las entidades de 2A y una cola dedicada.
Incluye cancelación, historial de intentos, recuperación y preparación verificable
del plan. La simulación sigue siendo el comportamiento predeterminado; el plan
real requiere configuración autorizada y `apply` permanece bloqueado en código.
Los [resultados Docker](docs/VALIDATION_2B.md) no usan credenciales reales.

## Transición al servidor

El [plan de transición desde el bootstrap a Git](docs/BOOTSTRAP_TO_GIT.md) conserva
PostgreSQL, Redis, los secretos y sus volúmenes. El
[procedimiento Alembic](docs/MIGRATIONS.md) describe la adopción del esquema.

El [cierre técnico e inventario de VM 104](docs/RELEASE_READINESS.md) documenta
la inspección histórica de solo lectura y el bloqueo por ausencia de backup
constatado en esa revisión; no sustituye la verificación previa a cada cambio.

El incremento 2B no se ha desplegado en VM 104. Fusionar el PR y ejecutar la transición requieren
aprobaciones explícitas. No usar `down -v`, regenerar secretos existentes ni
administrar el mismo stack con dos reconciliadores.

Aplicación controlada de planes, permisos y recuperación: [guía del ejecutor autorizado](docs/AUTHORIZED_APPLY.md).

Consistencia del state, recuperación sin mutaciones y procedimiento del intento 4:
[reconciliación de solo lectura](docs/READ_ONLY_RECONCILIATION.md).
La [validación final del PR #17](docs/VALIDATION_PR17.md) registra la suite completa,
la evidencia S3 controlada y los límites de atribución del incidente productivo.

Manual para Desarrollo, Infraestructura y Seguridad: [arquitectura, integración, operación y demostración](docs/handbook/README.md).

Contrato de infraestructura IACP 0.10.1: [inventario, compatibilidad y validación
MNG](docs/PROVISIONING_CONTRACT.md). Consumidores: `/docs` y `/openapi.json`;
administración autorizada: `/admin/docs` y `/admin/openapi.json`.
