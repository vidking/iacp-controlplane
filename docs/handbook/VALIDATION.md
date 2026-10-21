# Validación y reproducción

Evidencia preparada el 21 de octubre de 2026. Los resultados corresponden al
código revisado localmente, no a ejecución nueva en VM 104 ni a una KVM real.

| Entrega | Base/revisión | Resultado |
|---|---|---|
| Ejecutor autorizado, PR #7 | 2B `0c7219d`; código `2017d23`, aclaración operativa `25e571e` | 159 pruebas Docker aprobadas (33.00 s) y validación completa de recreación |
| Imagen terraform-worker | Mismo código de ejecución | UID/GID 10001, root:root, Alembic 0004, importaciones; Terraform 1.16.6 y Git 2.47.3 |
| TLS, PR #8 | main `4e764f4`; `8355f4b` | 108 pruebas aprobadas (14.01 s), Compose efectivo y Traefik real con certificado efímero |
| Módulo de disco iac-proxmox | Código fijado `40dc5d10387cfb50384ecd3ddddfd75289fe7bc4` | 13 pruebas Terraform nativas de plan documentadas en su PR #6 |
| Ejecutor + TLS combinados | `25e571e` + `8355f4b`, aplicación local del diff sin integrar main | 171 pruebas aprobadas (33.44 s) y prueba HTTPS repetida con éxito |
| Manuales | Rama de documentación sobre PR #7 | 10 manuales, ejemplos shell/Python y 9 operaciones autenticadas del contrato verificados sin red |

Resumen generado a partir de las salidas automatizadas: [validation-results.json](validation-results.json).

La validación completa del ejecutor incluyó API, PostgreSQL 17, Redis 7, Celery,
Traefik y recreación de todos los contenedores de desarrollo. Las comprobaciones
automatizadas confirmaron preservación del job bootstrap, inventario simulado,
marcador Redis, secretos y nombres/fechas de volúmenes. Se conservaron los
volúmenes de desarrollo. Los proyectos de prueba no usan recursos del servidor.

Los casos de Terraform cubren autorización independiente, tenant, idempotencia,
plan/configuración alterados, backend con locking, claim único, exclusión
PostgreSQL, expiración antes de mutar, publicación ambigua, timeout, salida
excesiva, cancelación, resultado incierto y recuperación que solo observa. El
adaptador de Terraform es simulado durante pruebas; el broker y DB son reales.

La prueba TLS utiliza Traefik v3.1, CA/certificado efímeros y una API fixture para
validar el transporte. Verifica HTTPS, confianza de CA, 401, Origin no autorizado,
filtro IP pese a XFF falsificado y listener diagnóstico. La suite API valida la
autenticación real por separado. No se publica ningún puerto de host en la prueba
TLS; los materiales privados se generan en un volumen aislado y no se versionan.

## Desde cualquier clon autorizado

Requisitos: Git, Docker local y Docker Compose. No instalar Terraform en el host
para ejecutar las pruebas. Los scripts no se deben combinar con Compose de
producción ni apuntar a un daemon remoto.

```sh
bash scripts/validate-docker.sh
IACP_IMAGE_TARGET=terraform-worker bash scripts/test-image-permissions.sh
# Después de integrar también el PR TLS:
bash scripts/test-tls.sh
python3 scripts/validate-handbook.py
```

La validación del manual también puede ejecutarse sin Python instalado en el host:

```sh
docker compose -p iacp-docs-check -f compose.test.yaml run --build --rm -T --no-deps tests python scripts/validate-handbook.py
docker compose -p iacp-docs-check -f compose.test.yaml down --remove-orphans
```

Para una iteración de suite sin recrear el runtime:

```sh
bash scripts/test-integration.sh
```

Los scripts imprimen salidas pytest y comprobaciones JSON `status=passed`.
Capturar evidencias en el almacenamiento autorizado de CI/validación; no guardar
logs con tokens, certificados, credenciales o state dentro de Git. Existe un aviso
de deprecación de AnyIO/Starlette en el cliente de pruebas; no produjo fallos.

## Pendiente antes de laboratorio

Obtener parámetros autorizados del [catálogo de laboratorio](LAB_DEMO.md),
validación de respaldos/restauración, DNS/CA/firewall y permisos mínimos. Revalidar
locking Wasabi y acceso Proxmox. Medir una clonación de la plantilla autorizada,
observar VMID/nodo/disco y comprobar recuperación bajo un protocolo aprobado.
La creación/eliminación real y la homologación CRM no están validadas por estas
pruebas. La API de eliminación permanece pendiente.
