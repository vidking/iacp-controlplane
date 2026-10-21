# IACP — Control Plane | Fase 1, incremento 1

Base de ingeniería sobre el bootstrap de **integración laboratorio**. La VM `iacp-controlplane-01` ya existe en
Proxmox (VMID 104, `mng01`, 192.168.1.50/20) y está **administrada manualmente**,
**fuera del Terraform State** de IACP. Este paquete **NO** contiene Terraform y
**NO** crea ni modifica VMs de clientes.

## Alcance

- PostgreSQL 17, Redis 7, FastAPI, Celery y Traefik HTTP **solo en loopback**
  `127.0.0.1:8080` de VM 104; no hay DNS público ni HTTPS configurado.
- DB, Redis, API y worker se comunican por redes privadas de Docker.
- Una prueba `POST /v1/jobs/smoke` guarda job en PostgreSQL, lo manda a Redis,
  el worker lo consume y lo marca `completed`.
- `POST /v1/instances` devuelve **503** explícitamente. Ningún código hace
  `terraform plan/apply/destroy` ni llama a Proxmox.
- `POST /v1/jobs/smoke` y `GET /v1/jobs/{id}` exigen un Bearer token de laboratorio.
- API modular con contratos Pydantic y [OpenAPI versionado](docs/openapi.json).
- La tabla `jobs` se administra con Alembic. El servicio `migrate` precede a API
  y worker y adopta el esquema bootstrap compatible sin modificar sus filas.
  El arranque de la API no ejecuta DDL. No es un stack productivo.

## Ingeniería y pruebas

La [auditoría completa y los incrementos](docs/AUDIT_PHASE1.md) documentan los
riesgos y el trabajo pendiente. La [guía de desarrollo](docs/DEVELOPMENT.md)
describe arquitectura, contratos y pruebas. Consultar el
[procedimiento de migración](docs/MIGRATIONS.md) antes de cualquier despliegue.

Pruebas rápidas con Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r app/requirements-dev.txt
.venv/bin/python -m pytest -m 'not integration'
```

Suite completa en un stack temporal separado, sin volúmenes persistentes:

```bash
bash scripts/test-integration.sh
```

Los modelos de instancias/tenants, RBAC, idempotencia, outbox, auditoría y
`IaCExecutor` se implementarán en los siguientes incrementos. El endpoint de
instancias sigue bloqueado; este incremento no habilita Terraform.

## Prerrequisitos ya verificados en VM 104

Ubuntu 24.04.5 LTS, Docker Engine 29.9.2, Compose 5.6.0, QEMU Guest Agent
activo, SSH con llave, `192.168.1.50/20`, ~142 GB libres.

**Verificaciones operativas pendientes:** formalizar la IP en IPAM y habilitar
**Backup** del disco `scsi0` (`backup=0` se observó antes). Verificar PBS y
restauración antes de considerar el plano de control protegido. Mantener acceso
Proxmox independiente.

## Despliegue histórico del bootstrap

Los siguientes pasos describen la instalación inicial. **No son una autorización
para desplegar.** Para actualizar al incremento 1, seguir
[MIGRATIONS.md](docs/MIGRATIONS.md), con backup verificado y aprobación explícita.

1. Transferir ZIP desde la laptop a `/tmp/IACP_ControlPlane_Runtime_VM104_v3.zip`.
2. `sudo unzip -q /tmp/IACP_ControlPlane_Runtime_VM104_v3.zip -d /opt/iacp/stacks/`
3. `cd /opt/iacp/stacks/controlplane`
4. `sudo bash scripts/preflight-runtime.sh` y **revisar subredes/rutas**.
5. `sudo bash scripts/create-secrets.sh` crea archivos locales root:10001, modo 0640.
6. `sudo cp .env.example .env`; no colocar credenciales en `.env`.
7. `sudo docker compose config --quiet` (no imprimir `docker compose config` sin necesidad).
8. `sudo docker compose up -d --build`.
9. `sudo docker compose ps` y `sudo docker compose logs --tail=50 --no-color`.
10. `curl -fsS http://127.0.0.1:8080/health/ready` -> `{"status":"ready"}`.
11. `bash scripts/smoke.sh` -> job `completed`; NO crea VM alguna.

Para usar API desde Omarchy sin exponerla en la LAN:

`ssh -i ~/.ssh/id_ed25519_iacp -L 8080:127.0.0.1:8080 ubuntu@192.168.1.50`

En la laptop: `http://127.0.0.1:8080/docs` (HTTP a través del túnel SSH).
Si el puerto local 8080 está ocupado, usar `-L 18080:127.0.0.1:8080`.

## Operación

- `sudo docker compose ps`
- `sudo docker compose logs --tail=100 api worker`
- `sudo docker compose restart worker`
- `sudo docker compose down` **sin** `-v`; `down -v` borra los volúmenes de datos.
- La persistencia vive en **volúmenes Docker `postgres_data` y `redis_data`**,
  no en `/srv/iacp/data`, aunque ese directorio exista.
- No compartir `secrets/api_token` ni `secrets/db_password`; excluirlos de Git
  y respaldarlos en gestor de secretos apropiado.

## Seguridad / siguiente iteración

- Hoy la API solo es accesible localmente por `127.0.0.1:8080`. **No abrir 8080,
  5432, 6379 ni 9001 en WAN**. Redis y PostgreSQL no publican puertos.
- El endpoint de autenticación usa un token estático de laboratorio, no reemplaza
  autorización por tenant/cliente. No conectar al CRM ni al portal aún.
- El worker no contiene Terraform. Antes del worker real: RBAC, idempotencia,
  outbox o mecanismo equivalente de publicación confiable, conciliación de jobs,
  locks por recurso/state y controles de plan/apply con auditoría.
- Portainer corporativo podrá leer el entorno mediante Agent, **después** de
  verificar su compatibilidad con Docker 29 y configurar firewall/RBAC. No
  instalar otro Portainer Server ni manejar el mismo stack simultáneamente
  desde Portainer y Compose CLI.
- Definir plan de backup de PostgreSQL con dumps consistentes y restauración
  probada, no confiar exclusivamente en snapshots/backup de VM.
- Para entrar en producción requerimos autenticación corporativa, TLS,
  políticas de red, alertas, versiones de imágenes validadas, migraciones y HA.

## Validación del incremento 1

La [evidencia de ejecución](docs/VALIDATION_INCREMENT1.md) registra 48 pruebas
aprobadas, incluidas seis de integración con servicios temporales reales.

La suite verifica rutas, autenticación, contratos, migración inicial, adopción
del esquema legado, rechazo de drift y flujo con PostgreSQL 17, Redis 7 y un
proceso Celery real. Se ejecuta en un entorno aislado; no requiere VM 104.
La validación de imágenes y del arranque Compose completo requiere Docker local.
