# IACP — Control Plane

Forgejo es la fuente oficial: [CyberFuel/iacp-controlplane](https://iac.cyberfuel.com/CyberFuel/iacp-controlplane).
El código y los contenedores son portables entre equipos autorizados. El destino
operativo es VM 104 `iacp-controlplane-01`; el equipo de desarrollo solo ejecuta
entornos locales independientes.

## Clonar y probar

Se requieren Git, Docker con contenedores Linux y Docker Compose 2.24 o posterior.
La autenticación de Git y el acceso al daemon Docker se configuran en cada equipo.
No se requieren Python, PostgreSQL, Redis, OpenSSL ni paquetes de la aplicación en
el host para ejecutar las pruebas Docker.

```bash
git clone https://iac.cyberfuel.com/CyberFuel/iacp-controlplane.git
cd iacp-controlplane
git switch feature/KVMHOST-91-controlplane-core
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

Los archivos de desarrollo y pruebas son independientes; no combinarlos con
`compose.yaml`. El archivo del servidor solo se combina con `compose.yaml` tras
inventariar el entorno. El cambio de directorio del código no debe cambiar el
proyecto Compose ni crear otra base de datos por accidente.

## Alcance del incremento 1

Se conserva API → Redis → Celery → PostgreSQL. La API está organizada en módulos,
usa contratos Pydantic y [OpenAPI versionado](docs/openapi.json), y el esquema se
administra con Alembic. `POST /v1/instances` devuelve 503: no hay Terraform ni
operaciones Proxmox. La autenticación sigue usando un token de laboratorio; el
sistema aún no está preparado para clientes ni producción.

La [auditoría y los siguientes incrementos](docs/AUDIT_PHASE1.md) cubren tenants,
RBAC, instancias, idempotencia, outbox, recuperación, auditoría e `IaCExecutor`.
Los [gates operativos](docs/NEXT_GATES.md) incluyen respaldo y restauración probada.

## Transición al servidor

El [plan de transición desde el bootstrap a Git](docs/BOOTSTRAP_TO_GIT.md) conserva
PostgreSQL, Redis, los secretos y sus volúmenes. El
[procedimiento Alembic](docs/MIGRATIONS.md) describe la adopción del esquema.

No se ha desplegado en VM 104. Fusionar el PR y ejecutar la transición requieren
aprobaciones explícitas. No usar `down -v`, regenerar secretos existentes ni
administrar el mismo stack con dos reconciliadores.
