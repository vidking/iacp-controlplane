# Recuperación y cierre MVP — 26/10/2026

## Dictamen

**Integración piloto: pendiente. Certificación física integral: NO-GO.** El cambio sigue local sobre `origin/main` `47f5fd22800900b8f374881657662df78680c07b`; aún no hay un release corregido en el IACP Control Plane (VM 104). El runtime observado continúa en la release LAB `47f5fd2`, esquema `0012_commercial_intents`, con API, workers, PostgreSQL, Redis y Traefik saludables. Doctor: PASS. Los servicios DB, Redis y Traefik conservan sus imágenes e IDs observados.

El trabajo interrumpido se recuperó en la rama `feat/configurador-aprovisionamiento-lab`: configurador en Swagger, adaptador IaC, hardware Golden Image, correcciones de lifecycle/observación y pruebas. `../iac-proxmox` no tiene cambios locales; su `origin/main` es `8ae5d768`. El PR #80 es el último PR publicado visible en Git; su merge `47f5fd2` ya es `origin/main`. La nueva corrección aún no tiene commit ni PR al momento de esta evidencia.

## Causa y corrección de CPU/NUMA

Las cuatro plantillas verificadas en `mng01` tienen CPU `x86-64-v4,flags=+aes` y `numa=1`. El provider fijado `bpg/proxmox 0.116.1` predetermina `cpu.type=qemu64`, `cpu.numa=false` y un socket. El módulo asignaba `type` y `numa` explícitamente al recurso Terraform, por lo que Terraform sustituyó la herencia del clon. Proxmox rechaza `hotplug=memory` con NUMA apagado. La VM 30029 mostró precisamente `qemu64`, mientras la plantilla VM 29001 conserva el CPU y NUMA correctos.

El adaptador nuevo observa por HTTPS la plantilla original real antes de planificar, guarda CPU, flags, NUMA, sockets y características de firmware, máquina, almacenamiento, hotplug y arranque en la intención vinculada. Un adaptador Terraform declarado y fijado pasa los valores de CPU al módulo y verifica plan, apply y configuración efectiva. No fija un modelo CPU global ni cambia las plantillas. Si la topología no es compatible o tiene NUMA personalizado que el adaptador no soporta, falla cerrado. `terraform validate` con Terraform 1.16.6 y provider 0.116.1: PASS.

## Catálogo y matriz

El catálogo HTTP observado publica cuatro imágenes autorizadas (`ubuntu-2404`→29000, `ubuntu-2604`→29001, `almalinux-9`→29010, `almalinux-10`→29011) y los cuatro planes KVM con sus recursos exactos. La comprobación automatizada cubre contrato/compatibilidad para las 16 combinaciones. Este estado no acredita clonación o acceso físico.

| Combinación | Configurada | Contrato y capacidad observada | E2E corregido certificado |
|---|---|---|---|
| KVM 1 × cuatro imágenes | Sí | Sí | No |
| KVM 2 × cuatro imágenes | Sí | Sí | No |
| KVM 3 × cuatro imágenes | Sí | Sí | No |
| KVM 4 × cuatro imágenes | Sí | Sí | No |

Antes de esta corrección, el clúster MNG ejecutó instancias desechables KVM 1 Ubuntu 24.04 y Ubuntu 26.04. VM 30027 completó guest-ready, SSH por llave, reboot, shutdown, DELETE, state ausente y lock ausente. Es evidencia anterior a esta candidata, no certifica la herencia corregida ni el acceso root por contraseña. La prueba de Ubuntu 26.04 que originó VM 30029 alcanzó `ready`, pero Lifecycle falló (`POWER_TASK_FAILED`) y dos operaciones de DELETE fallaron (`APPROVED_POSTCONDITION_MISMATCH`). No se declara aprobada.

## VM 30029 y state

El responsable informó que eliminó manualmente la VM 30029. La lectura independiente posterior confirma: Proxmox no la enumera; no quedan discos en `DS_CYBERFUEL`; IACP todavía la registra `ready`; Terraform conserva el recurso; no hay jobs, operaciones, colas o locks activos. La base de datos y Terraform no concuerdan, así que la reserva requiere conciliación. La corrección introduce un DELETE fail-closed que verifica ausencia nativa y aplica un plan refresh guardado para registrar el recurso ausente. No usa `state rm`; Terraform debe escribir una nueva versión vacía y preservar las versiones anteriores. Terraform Core 1.16.6 con provider descartable `local 2.5.3` confirmó que, cuando `plan -destroy` omite cambios y state anterior, apply refresh-only aumenta serial y escribe state vacío. Esto prueba Terraform Core, no la recuperación en Wasabi/MNG de 30029. El cierre de esta reserva está pendiente de promover el cambio y observar una operación DELETE completa en IACP.

## Swagger, secretos y regresiones

La página se llama **Crear máquina virtual**, obtiene el catálogo autenticado, ofrece cuatro planes e imágenes, genera JSON/cURL y POST con clave idempotente por intención, permite consultar operación/IP y presenta acciones Lifecycle/DELETE usando IDs de la instancia creada. El cURL ejecuta Bash sin red en el harness, los secretos no pasan a storage/URL y los reintentos conservan clave/cuerpo. Chromium escritorio, Chromium móvil y Firefox: PASS con transporte simulado; cada uno generó las 16 combinaciones. No es validación del dominio HTTPS desplegado.

El flujo de bootstrap previo permitía contraseña root de consola y bloqueaba autenticación root por SSH. La corrección configura SSH por contraseña en el piloto LAB y amplía la sonda E2E a autenticación SSH con contraseña y llave. Esto aún no se probó en una VM corregida.

Regresión completa: **1154 passed**, 32 avisos; las 61 pruebas de integración PostgreSQL 17/Redis 7 descartables pasan. Comprobación focal posterior de Terraform/hardware/deletion: 49 passed. Se revisaron los cambios y la evidencia para evitar incluir valores de credenciales o secretos.

## Próximos gates

1. Abrir un solo PR de esta corrección, revisar y fusionar sobre `main`.
2. Ejecutar preflight y promoción según `scripts/iacpctl`/`scripts/go_live.py`, actualizar solo API y workers del IACP Control Plane (VM 104), conservar DB/Redis/Traefik/volúmenes/secretos/state.
3. Confirmar Doctor, catálogo autenticado por HTTPS, OpenAPI y Playwright de lectura sobre el runtime corregido.
4. Conciliar y borrar VM 30029 mediante DELETE de IACP; verificar state/versiones, disco y lock.
5. Probar secuencialmente el E2E corregido por la identidad existente `lab-swagger-postman`, comenzando KVM 1 × Ubuntu 24.04 y continuar combinaciones mientras la capacidad física lo permita. Esperar Cloud-Init, DHCP, SSH root con contraseña y llave, Lifecycle, DELETE, discos, state y locks.
6. Registrar resultado individual de cada combinación; no declarar GO integral hasta completar los 16 escenarios críticos.
