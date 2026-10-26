# Aprovisionamiento libre LAB/MNG y cuatro Golden Images

Inicio 25/10/2026; cierre de la candidata 26/10/2026, America/Costa_Rica. Rama `fix/lab-mng-capacidad-swagger`, basada en main `5b1c0a8c20f773d975a6ee2c8cb1946256848a30`. **Candidata no desplegada.** El IACP Control Plane (VM 104) conserva `54d418119200710ee00eb7a9d406b9322fe8e40c`.

## Resultado implementado

- Modo persistente `lab_mng_physical_capacity_only=true`: sin presupuesto comercial de cantidad de instancias, CPU, RAM o disco acumulados. Los valores anteriores permanecen en el esquema para compatibilidad y **no se aplican en este modo**. Concurrencia técnica 1 y capacidad física siguen vigentes.
- Alcance verificado al cargar límites y admitir: tenant LAB persistente, mng01/mng02, DS_CYBERFUEL, perfil lab-dhcp y autorización existente. Otros tenants conservan sus cuotas. No se modificaron ni eliminaron las dos instancias/reservas existentes.
- KVM 1/2/3/4 con recursos oficiales; JSON mínimo, Bearer e Idempotency-Key. Sin order_id ficticio, UUID de política ni aprobación humana por solicitud. Tenant, DHCP, nodo, datastore, IDs, jobs y executor se resuelven internamente.
- Catálogo KVMHOST-54.4 con las **cuatro imágenes habilitadas**, según confirmación del responsable de Infraestructura. La habilitación de plantillas se distingue del E2E de integración del orquestador.
- Swagger: **Crear máquina virtual**, selección de catálogo autenticado, JSON y cURL derivados de la selección, pasos Try it out/Execute, seguimiento, SSH, Lifecycle y DELETE. Recorrido plegable, copia y navegación existentes preservados.

Configuración persistente versionada: [modo LAB](../../deployment/commercial-limits.lab-mng.json) y [configuración de imágenes](../../deployment/lab-images.mng.json). No se incorpora ningún secreto, rol, tenant o token nuevo.

## Plantillas y adaptación real

[Lectura del inventario](plantillas-detalle.json): las cuatro son templates en mng01/DS_CYBERFUEL, disco base 20 GiB, ciuser root, Cloud-Init y QEMU Guest Agent configurados. Infraestructura confirmó su funcionamiento y autorizó su ejecución. No se arrancó ni modificó ninguna plantilla original.

| image_id | VMID interno | Estado del catálogo candidato LAB | Adaptación del clon | Flujo IACP |
|---|---:|---|---|---|
| ubuntu-2404 | 29000 | Habilitada | Perfil y programa históricos intactos; Netplan | KVM 1/2 histórico validado; nuevo modo por ejecutar |
| ubuntu-2604 | 29001 | Habilitada | Netplan con comprobación específica 26.04; bootstrap root | Por ejecutar |
| almalinux-9 | 29010 | Habilitada | NetworkManager, DHCP client-id MAC, SELinux y sshd | Por ejecutar |
| almalinux-10 | 29011 | Habilitada | NetworkManager, DHCP client-id MAC, SELinux y sshd | Por ejecutar |

El bloqueo anterior estaba en **IACP**: resolver, perfil, inicializador y bootstrap codificados para Ubuntu 24.04. No era falta de permiso demostrada para leer las plantillas. Ahora el servidor resuelve imagen, VMID, nodo y storage desde su registro y deriva el perfil por instancia. Los modos/digests del inicializador histórico se conservan. Los nuevos programas son fijos, con hash, MAC/instancia cercadas y observación de evidencia; no ejecutan comandos del consumidor.

El bootstrap conserva cifrado Redis/TTL, referencia opaca, llave técnica existente y host SSH fijado mediante QGA. Contraseña solo por stdin, fuera de PostgreSQL, colas, plan/state y logs. El servicio SSH de Ubuntu es ssh; el de AlmaLinux es sshd. La contraseña root es de consola/PAM; SSH root usa llave, sin habilitación indiscriminada de contraseña SSH.

[Capacidad real observada](preparacion-runtime.json): nodo y datastore activos/habilitados, 48 CPU, aproximadamente 170 GiB RAM libre y 10,5 TiB disponibles. Consultas de node/status y storage/status con la identidad de lectura existente. No se consultó ni modificó Ceph. Revalidar descontando reservas antes de cada ensayo; esta observación no reserva recursos.

## Pruebas

- Primer correctivo de modo libre: 1103 pruebas completas aprobadas; resultado histórico conservado en el commit anterior de esta misma rama.
- Integración de cuatro imágenes: **1127 pruebas completas aprobadas, cero omitidas**, y **155 focalizadas aprobadas**. Regresión completa con PostgreSQL/Redis descartables en [pruebas-regresion-imagenes.log](pruebas-regresion-imagenes.log); pruebas focales finales en [pruebas-integracion.log](pruebas-integracion.log). La ejecución previa señaló snapshots OpenAPI/sellos desactualizados; se regeneraron y documentaron, sin retirar validadores.
- 16 combinaciones de imagen/plan admitidas en pruebas aisladas, VMID interno correcto, recursos exactos, DHCP, G6, idempotencia y capacidad negativa. La prueba aislada no se presenta como clonación física.
- Programas de cada distribución: sintaxis, digest, despacho fijo, evidencia observada, rechazo de manipulación y transporte SSH confidencial. [Pruebas adicionales](pruebas-nuevas-imagenes.log).
- [Playwright](navegador/playwright.json): Chromium escritorio/móvil y Firefox, navegación, copia, selección de los cuatro planes y cuatro imágenes, historial, scroll y Try it out de lectura. Fixtures de catálogo identificados: no son disponibilidad del runtime. Cero errores JS y dependencias CDN.
- [Diff contractual](diff-contractual.json): mismos paths, campos, tipos, respuestas, operationId y esquemas de autenticación. Cambian descripciones, ejemplos y datos autorizados del catálogo. [Revisión acotada de core](revision-core-imagenes.json): resolver el perfil de la imagen de la instancia en profiles/lifecycle; restantes sellos de invariantes preservados.

## Promoción y ejecución autorizada tras el merge

1. Confirmar SHA exacto integrado. Preparar release e imágenes desde ese SHA; inventario/snapshot, servicios ociosos, colas/locks, respaldo PostgreSQL restaurado en aislamiento, registro de cambio y manifiesto revisados. Preservar identidades y secretos G6.
2. Preparar copia de la configuración activa `/etc/iacp/terraform/profiles-comercial-g-cierre-20261025.candidate.json`, actualmente montada readonly en `/run/config/profiles.json`. Incorporar solo el tenant del archivo de modo LAB en commercial_limits y el mapa lab_images del segundo archivo; conservar perfiles, otros tenants y snapshots. No renombrar directorios ni modificar Compose.
3. Pinear el hash nuevo y su reemplazo en el manifiesto/revisión vigente. La revisión de activación debe contener el modelo completo de límites, incluida la bandera true, y el hash completo del perfil. Instalar atómicamente bajo exclusión de admisión, conservando dueño/modo. Remontar los servicios afectados para que no retengan el inode anterior.
4. Seguir `go_live.py preflight`, `iacpctl env-promote`, `go_live.py promote`, `activate-lab`, verify y doctor con manifiesto y directorios nuevos. No sustituir PostgreSQL/Redis/Traefik ni volúmenes. Verificar SHA/archivos/configuración efectiva de cada servicio actualizado.
5. Autenticar por HTTPS con **lab-swagger-postman**, verificar los cuatro planes e imágenes disponibles según capacidad, contrato mínimo y documentación. Repetir Playwright contra el dominio real antes de certificar la presentación desplegada.
6. Ensayar secuencialmente, empezando por una KVM 1 Ubuntu 24.04 y limpiándola antes de la siguiente combinación. Runner existente ampliado con imagen, cuatro planes, cuerpo mínimo y token explícito:

```bash
# Rutas privadas del manifiesto y entrega vigentes; jamás pasar el valor del token.
python3 scripts/e2e-commercial-sequential.py --execute --minimal \
  --token-file "$TOKEN_LAB_FILE" --image ubuntu-2404 --plan kvm-1 \
  --evidence-dir "$EVIDENCIA_NUEVA"
```

Repetir con las imágenes y tamaños seleccionados físicamente viables, con directorios nuevos. Se revalida la disponibilidad antes de POST. El runner comprueba 202/replay, workers, recursos, OS, Cloud-Init, disco y filesystem ext4/XFS, DHCP, SSH root, contraseña PAM, Lifecycle, DELETE, ausencia física/state/lock, limpieza de referencia y confidencialidad de artefactos/logs. Conserva y compara la ocupación previa; no exige vaciar el tenant ni borra sus instancias. Detiene la secuencia ante fallo, guarda instance_id y requiere recuperación de la intención existente; no repite clonación ni fuerza state.

## Recuperación y estado real

No hay migración DB. Conservar imágenes, manifiesto, configuración y credenciales anteriores. Ante fallo de preflight no desplegar. Ante regresión cerrar admisión con el driver; resolver nuevas intenciones/clones con código capaz de interpretarlos antes de volver al binario anterior. Restaurar también su configuración: el binario anterior no conoce la bandera ni los nuevos perfiles derivados. No hacer downgrade, borrar state ni forzar locks. El rollback del Control Plane no deshace VMs.

**El modo libre y las cuatro imágenes están implementados y probados en la candidata; no se declaran activos en VM 104. El E2E físico del nuevo modo y su certificación HTTPS se ejecutan después de integración/promoción.** Se conserva el GO histórico KVM 1/2. No se declara GO ampliado ni resiliencia integral con esas pruebas por ejecutar. Sin cambios en Ceph, Jira, RBAC, producción, plantillas originales o instancias históricas.
