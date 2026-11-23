# Continuidad de las matrices WAN

Estado: **EN CURSO**, actualizado 2026-11-18. Rama: `devops/features`. Producción sin cambios.

### Estado operativo actual — 2026-11-18

Se excluyó `190.0.231.2` de las matrices y NetBox la mantiene reservada con la
etiqueta `reservada`; la siguiente dirección disponible quedó en `.3`. La
ejecución de Nextcloud ciclo 1 más reciente (`/opt/iacp/evidence/wan-services-c1-202611180951/results.jsonl`)
se detuvo ante el primer lote fallido y no certifica el ciclo. El usuario
confirmó que los accesos web de prueba en `190.0.231.4` y `.5` muestran el login
de Nextcloud; esos dos accesos no sustituyen las comprobaciones SSH, ciclo de
vida, IPAM ni el PASS de cada caso.

La inspección de clones AlmaLinux observó que sus imágenes no contenían
`cloud-init` ni `iproute`. La fábrica ahora instala ambos paquetes, habilita
las etapas de cloud-init y exige comprobar `cloud-init --version` e `ip -V`
antes de sellar. También falla cerrada si el stack dev o productivo tiene
operaciones, cola, incertidumbre o workers ocupados/no sanos, ya que ambos
utilizan el clúster MNG. La corrección se está validando localmente; las
templates compartidas 29010/29011 todavía no se modifican.

El flujo de reparación prepara copias candidatas temporales 29016/29017 a partir
de los templates públicos actuales. Solo después de certificar los paquetes, el
helper, SELinux y la paridad de hardware contra 29000, crea copias de recuperación
29018/29019 y recrea 29010/29011 desde las candidatas. Las imágenes temporales se
retiran cuando las matrices estén completas. La promoción no procede con trabajo
activo en dev o en producción.

En la lectura de producción de las 2026-11-18 se observaron operaciones activas
y workers ocupados. Por tanto, la preparación física de esas templates no es
segura mientras esa condición persista. Dev tenía seis workers de infraestructura
y cuatro de servicios sanos e idle, sin operaciones ni cola. No se inició otra
matriz WAN hasta que las imágenes AlmaLinux cumplan los requisitos anteriores.

### Incidente del primer ciclo WAN de servicios — 2026-11-18

La revisión ejecutada `f76798a768768d2c173318612f7bb9d4be908c88` inició el
ciclo 1 de Nextcloud con `prod_static`, seis casos y nombres de prueba
simbólicos. La evidencia privada está en
`/opt/iacp/evidence/wan-services-cycle1-f76798a-20261118080200/`. El ciclo no
completó la matriz y no cuenta como certificación:

| VMID | IP | Imagen | Resultado observado |
| --- | --- | --- | --- |
| 40161 | 190.0.231.2 | Ubuntu 24.04 | `STATIC_GATEWAY_UNREACHABLE`; operación incierta. |
| 40162 | 190.0.231.3 | Ubuntu 24.04 | Infraestructura lista; instalación incierta por `ANSIBLE_FAILED_SSH_CONNECTIVITY`. |
| 40163 | 190.0.231.4 | Ubuntu 24.04 | El usuario confirmó que carga el login de Nextcloud. Esto comprueba la interfaz web; no cierra el ciclo de acceso ni de vida útil. |
| 40164 | 190.0.231.5 | Ubuntu 24.04 | Nextcloud llegó a `awaiting_customer` y se observó el login; no se declara PASS integral. |
| 40165 | 190.0.231.6 | AlmaLinux 9 | `SERVICE_BOOTSTRAP_GUEST_FAILED`; observación DNS incompleta. |
| 40166 | 190.0.231.7 | AlmaLinux 9 | `SERVICE_BOOTSTRAP_GUEST_FAILED`; observación DNS incompleta. |

La rama `devops/features` incorpora el arreglo de DNS en `8979ddb`. El worker
acepta `resolvectl` solo si devuelve las direcciones exactas y usa NetworkManager
como fallback. AlmaLinux lleva una copia de este programa dentro de su helper;
por eso se deben regenerar y preparar las imágenes a partir de esta revisión.
Desplegar solo el worker/API no actualizará las Golden Images existentes. El
procedimiento de copias rechaza VMID destino ya existentes: Operaciones debe
verificar respaldo/reversión antes de reemplazar 29010/29011, preservar las
fuentes 29016/29017 y volver a comparar hardware, hash del helper y SELinux con
la referencia 29000. Esta preparación física corresponde al equipo de
operaciones de MNG. La IP `.2` fue asignada en este lote; reconciliar su estado
actual con NetBox y la evidencia anterior antes de reservarla o asignarla de
nuevo. No reutilizarla a partir de supuestos.

La revisión candidata también clasifica como fallo reintentable un error del
primer pre-task `Esperar conectividad SSH`: Ansible todavía no ha comenzado
cambios en el guest. El runner permitirá un solo reintento, únicamente cuando
el worker anterior ya no posea el job. Esta regla no cambia los resultados
históricos inciertos ni permite reintentar fallos posteriores al inicio de la
instalación.

**Siguiente paso operativo:** recuperar por la API las intenciones de prueba
propias, conservar sus evidencias y comprobar la liberación NetBox. Actualizar
únicamente el stack dev del IACP Control Plane (VM 104) con `scripts/iacpctl`,
preparar las imágenes AlmaLinux con el helper de esta revisión y verificar el
runtime, seis workers de infraestructura sanos e idle, cuatro workers de
servicios idle y cero operaciones activas o inciertas. Luego repetir servicios
ciclo 1 con `prod_static`, un identificador de corrida y carpeta de evidencia
nuevos. Detenerse ante cualquier estado incierto. No usar DHCP ni modificar
producción.

Las 64 aprobaciones siguen pendientes: 32 casos KVM y 32 casos de servicios
(Nextcloud/n8n), dos ciclos por categoría. Los controles de acceso y casos
adicionales no sustituyen esos 64 PASS. Los agentes permanecen fuera de esta
fase.

### Actualización posterior — 2026-11-18

Las Golden Images AlmaLinux definitivas se recrearon en el nodo `mng01` del
clúster MNG: AlmaLinux 9 en 29010, desde 29016, y AlmaLinux 10 en 29011, desde
29017. Ambas son plantillas detenidas y la comparación de hardware contra la
Golden Image Ubuntu 24.04 protegida 29000 dio `PASS`: BIOS OVMF, máquina q35,
VirtIO SCSI single, NIC VirtIO sobre `vmbr0`, disco SCSI de 20 GB, Cloud-Init en
SCSI y EFI; sin dispositivos IDE ni VGA. Las fuentes 29016/29017 permanecen
intactas. Evidencia privada de preparación:
`/opt/iacp/evidence/alma-hardware-v14-20261118-9/` y
`/opt/iacp/evidence/alma-hardware-v17-20261118-10/`.

El catálogo de la rama `devops/features` apunta a 29010/29011. Este cambio de
código no equivale a un despliegue ni certifica aprovisionamientos físicos. El
equipo de operaciones confirmó que una KVM creada manualmente, conectada a
`vmbr3` sin tag VLAN, alcanzó Internet y resolvió el gateway por ARP. Por ello,
el fallo previo de `190.0.231.2/24` se trata como una discrepancia de la VM
automatizada o de su ejecución, no como prueba de que `vmbr3` esté caído.

El código de `prod_static` selecciona `vmbr3`, omite `vlan_id` y el preflight de
la matriz rechaza cualquier `tag` en `net0`; también valida el `ipconfig0`
estático con gateway `190.0.231.1`. Antes de reanudar las matrices, comparar la
revisión y manifiesto realmente ejecutados con la revisión candidata, y
contrastar en la VM automatizada y en la manual `net0`, `ipconfig0`, la MAC, la
dirección asignada por NetBox, la ruta por defecto y el vecino ARP. Mantener
`.2` reservada mientras no se explique ese resultado; usar únicamente la
siguiente dirección que NetBox confirme disponible. Reanudar con `prod_static`,
seis workers de infraestructura sanos e idle, cuatro workers de servicios idle
y cero operaciones activas o inciertas. No cambiar a DHCP.

### Reintento de despliegue y preflight de servicios — 2026-11-18

La revisión `8d2dcd919a7f1d126f6f3faef1f851e1cb083b09` se desplegó en el stack
dev del IACP Control Plane (VM 104). `iacpctl preflight`, `inventory`, `deploy`
y `verify` dieron PASS. La API publicó `prod_static` habilitado con `vmbr3`,
NetBox verificó `190.0.231.0/24` y `190.0.231.1`, y los seis workers de
infraestructura y cuatro de servicios estaban sanos e idle.

El ciclo WAN de servicios 1 se detuvo antes de crear VMs con
`GOLDEN_IMAGE_NOT_READY`. El catálogo publicado apuntaba AlmaLinux 9/10 a
29010/29011, que no existen en el inventario del clúster MNG. Las plantillas
selladas y verificadas están en 29016/29017 en `mng01`. NetBox reportó `.2`
como la siguiente dirección disponible. La evidencia del bloqueo está en
`/opt/iacp/evidence/wan-servicios-ciclo1-20261118051100/results.jsonl`.

El código de desarrollo debe apuntar el catálogo y el preflight a las plantillas
29016/29017 antes de repetir. No se creó ninguna VM en este intento.

### Actualización de topología y prioridad — 2026-11-17

El bloqueo anterior se observó usando `vmbr0` con tag 211 en la NIC de la VM.
El equipo de redes autorizó temporalmente que las VMs de prueba usen `vmbr3`
sin tag de Proxmox, porque una capa superior ya entrega la VLAN 211 etiquetada.
Esta configuración se limita al stack dev. `lab_dhcp` continúa en `vmbr0` sin
tag y producción no se modifica.

El candidato separa segmento externo y configuración de NIC mediante
`IACP_PROD_STATIC_BRIDGE=vmbr3` y `IACP_PROD_STATIC_PROXMOX_VLAN_TAG=`. Antes de
inyectar, `scripts/wan_matrix.py` compara el perfil publicado por la API y la
configuración real de cada VM. El orden vigente prioriza servicios: completar
Nextcloud y n8n en los dos ciclos WAN, con los cuatro workers de servicios y
seis workers de infraestructura; luego continuar la matriz KVM. Los agentes
siguen aplazados hasta cerrar los servicios.

Antes de cada ciclo confirmar seis workers de infraestructura sanos e idle,
cuatro workers de servicios idle y cero operaciones activas, en cola o inciertas.
Limpiar por la API únicamente los servicios de prueba propios de ciclos previos,
apagándolos antes de eliminarlos y confirmando liberación NetBox cuando aplique.

1. El primer lote en la topología actual confirmó `vmbr3` sin tag de Proxmox y
   VLAN 211 aguas arriba. Aun así, el guest de `190.0.231.2` no recibió ARP de
   `190.0.231.1`; otros guests de esa tanda sí resolvieron el gateway. No
   continuar la matriz WAN hasta resolver o aislar ese caso de conectividad.
2. Confirmar la imagen ejecutada y el manifiesto canónico
   `/etc/iacp/stacks/manifest-dev.json`. El archivo de entorno es
   `/etc/iacp/stacks/dev.env`; el Bearer dev se obtiene allí privadamente.
3. Comprobar seis workers de infraestructura sanos e idle, cuatro de servicios y
   ausencia de operaciones activas o inciertas. Conservar los datos históricos.
4. Habilitar Ansible para el stack dev mediante `IACP_SERVICE_EXECUTION_ENABLED=1`
   y `service_execution_enabled=true` en el manifiesto. Aplicar el cambio con
   `iacpctl`, nunca reiniciando otros stacks.
5. Ejecutar servicios WAN ciclos 1 y 2. Solo tras aprobarlos continuar con KVM;
   la matriz de agentes queda pendiente hasta cerrar los servicios. Ante fallo,
   detener nuevas inyecciones, recuperar y limpiar únicamente instancias propias
   antes de repetir el ciclo. Mantener los resultados fallidos.

Ejemplo para el ciclo de servicios, usando el Python del entorno operativo
privado y el release realmente desplegado:

```bash
sudo /opt/iacp/tools/services-pilot-venv/bin/python \
  /opt/iacp/releases/controlplane-REVISION/scripts/wan_matrix.py \
  --category services --cycle 1 --network-profile prod_static \
  --run MARCA_UTC_NUMERICA \
  --evidence /opt/iacp/evidence/wan-servicios-ciclo1-MARCA_UTC_NUMERICA
```

Después de cerrar servicios, cambiar `--category` a `kvm` o `agents` según la
etapa, y `--cycle` a 2 cuando corresponda.
Los directorios de evidencia deben ser nuevos. No pasar credenciales en argumentos.
El runner fija el dominio dev y comprueba el header de revisión contra el entorno.
Cada categoría contiene 16 casos y dos controles para mantener tandas de seis.

## Primer lote físico en `vmbr3` — 2026-11-17

Revisión ejecutada: `5892ffc8517d0d008684f6a587ea112d54f45c73`. El perfil API
publicó `vmbr3`, tag Proxmox nulo y VLAN externa 211; la configuración efectiva
de las seis NIC coincidió. El lote se detuvo con `BLOCKED` antes de inyectar la
siguiente tanda:

- `190.0.231.2` quedó con el vecino del gateway en `INCOMPLETE` y causó
  `STATIC_GATEWAY_UNREACHABLE`. Los guests siguientes que recibieron
  `190.0.231.3`–`.5` sí observaron la MAC del gateway. Esta evidencia no
  determina por sí sola si existe una política de red o una condición puntual
  de esa dirección; el equipo de redes debe revisar el caso antes de reintentar.
- AlmaLinux 9 v2 no tenía `iproute`, necesario para validar la interfaz y ruta;
  dos aprovisionamientos acabaron en `SERVICE_BOOTSTRAP_GUEST_FAILED`. Las
  nuevas imágenes v3 incluyen ese paquete y ya pasaron su preparación.
- Una instalación Nextcloud quedó incierta y dos terminaron; al detectar
  operaciones inciertas no se iniciaron más casos.

Las seis VMs se apagaron y eliminaron con la API. NetBox marcó liberadas las
seis direcciones `.2`–`.7`, las consultas posteriores no encontraron objetos
NetBox ni VMID en Proxmox, y las tres operaciones físicas inciertas se
reconciliaron con recurso ausente. Al cierre había cero operaciones activas o
inciertas, seis workers de infraestructura idle y cuatro workers de servicios
idle. Evidencia privada: `/opt/iacp/evidence/wan-servicios-ciclo1-20261117222500/`
y `recovery-summary.json` dentro de ese directorio. Este lote no cuenta como
ciclo aprobado.

La CA del piloto está en el directorio privado indicado por
`IACP_SERVICE_CONFIG_DIR`; los workers necesitan `IACP_SERVICE_IP_TLS_ENABLED=1`.
Su clave privada no debe salir del Control Plane. La modalidad web por IP usa esa
CA y exige validación TLS. Los agentes usan `agent_mode=local`: no se certifican
inferencia, Telegram ni proveedores externos.

### Actualización operativa — 2026-11-18

Esta actualización reemplaza el próximo paso LAN indicado en la sección
histórica. El equipo de redes confirmó `vmbr3` sin tag de Proxmox y VLAN 211 en
la capa superior. Proxmox muestra el puente activo sobre `vlan211`; el guest
usó `vmbr3` sin tag. La prueba NetBox de `190.0.231.2/24` no resolvió ARP para
`190.0.231.1` (`STATIC_GATEWAY_UNREACHABLE`). Tras apagar y eliminar la VMID
40140 por la API, la IP quedó liberada y sin objeto NetBox; la reconciliación de
la operación original terminó `failed` por recurso físico ausente. Esta
dirección no está certificada. No se seguirá usando `lab_dhcp` en matrices
físicas; las siguientes matrices deben usar `prod_static` y NetBox. Se reservó
temporalmente `.2/24` en NetBox con estado `reserved` y tag `reservada`; el
endpoint `available-ips` confirmó que la siguiente dirección es `.3/24`. La
reserva permanece dentro del prefijo autorizado para impedir que una solicitud
de cliente reciba la dirección que falló. Evidencia privada de la revisión
`59ca188`: `/opt/iacp/evidence/wan-gateway-probe-59ca188-202611172359/`.

El ciclo LAN 1 `20261118005202`, revisión `59ca188`, quedó `BLOCKED`: cinco
servicios Nextcloud instalaron; el caso Ubuntu 24.04/`kvm-4` quedó `uncertain`
con `ANSIBLE_FAILED_APPLICATION_START`. Antes de limpiar se comprobó que el job
no tuviera worker asignado, el daemon Docker estuviera activo, Compose fuera
válido y la imagen de Nextcloud no estuviera descargada. Se conservó el error y
se apagaron y eliminaron por la API las seis VMs propias. Los seis workers de
infraestructura y los cuatro de servicios quedaron idle; no hubo operaciones
activas o inciertas al cierre. Este intento no cuenta como aprobado. Evidencia
privada:
`/opt/iacp/evidence/lan-servicios-ciclo1-20261118005202/results.jsonl` y
`/opt/iacp/evidence/recuperacion-lan-servicios-20261118020900/recovery-summary.json`.

El candidato local incorpora `docker compose pull` con reintentos antes del
arranque. Un fallo de descarga se clasifica como `failed` reintentable porque
no inicia contenedores; errores de arranque conservan `uncertain` hasta que el
worker anterior deje el job. El runner prueba una recuperación única solo para
los códigos permitidos y únicamente después de comprobar la liberación del
worker. La imagen y el navegador pasan por pruebas locales antes de desplegar
este candidato exclusivamente a dev.

### Resultados recientes — 2026-11-17

El equipo de redes confirmó que el enlace WAN temporal debe conectarse en
`vmbr3` sin tag de Proxmox; VLAN 211 ya viene etiquetada desde la capa superior.
El perfil `prod_static` de dev coincide con esa configuración. La confirmación
no cambia el resultado del sondeo aislado de `190.0.231.2`: con la NIC en
`vmbr3` sin tag, la VM no resolvió por ARP `190.0.231.1`. El caso terminó
`STATIC_GATEWAY_UNREACHABLE`; la VM se eliminó por la API, la reserva se liberó y
Proxmox confirmó la ausencia del recurso. No se certifica WAN ni se avanza a
otra IP mientras el caso siga sin explicación de red. Revisión ejecutada:
`08b57946bb41a080cb9523b28d1f0caf2a30fd60`. Evidencia privada:
`/opt/iacp/evidence/wan-gateway-probe-08b57946bb41a080cb9523b28d1f0caf2a30fd60/`.

El ciclo LAN 1 `202611172314`, revisión `08b57946bb41a080cb9523b28d1f0caf2a30fd60`,
quedó `BLOCKED`, no aprobado. Doce casos completaron el ciclo de vida y
verificaciones de extremo a extremo. En el lote final se instalaron cuatro
combinaciones n8n/AlmaLinux 9 y el control de contraseña sin llave; no se
completaron sus verificaciones de extremo a extremo. El control Nextcloud sin
llave SSH quedó `uncertain` durante ejecución Ansible. El job de servicios se
canceló solo después de confirmar que el worker había terminado; se preservó el
fallo en la evidencia. Las seis VMs del lote se apagaron y eliminaron por la API;
se verificó que no quedaran VMID ni operación activa/incierta y los cuatro
workers de servicios y seis de infraestructura quedaron idle. DHCP no usa
reservas NetBox. Evidencia privada:
`/opt/iacp/evidence/lan-servicios-ciclo1-202611172314/`.

La revisión candidata actual incorpora un callback Ansible de salida segura. El
job conserva solo un código de una lista fija, por ejemplo
`ANSIBLE_FAILED_NEXTCLOUD_INSTALL` o `ANSIBLE_FAILED_DOCKER_DEPENDENCIES`; no
persiste la salida de Ansible ni valores de tareas. Tras validar y desplegar
esta revisión únicamente en dev, repetir primero el ciclo LAN 1 con un nuevo
directorio de evidencia para obtener el diagnóstico del caso fallido y cerrar
los cinco casos aún sin verificación. Después de un ciclo completo `PASS`,
continuar con LAN ciclo 2. WAN sigue bloqueado por la resolución ARP de `.2`;
solicitar al equipo de redes revisar la conectividad de gateway/vecino para ese
segmento antes de reintentar.

Solo preparar promoción a `main` al aprobar ambos ciclos de cada categoría,
recuperación y limpieza. Reutilizar la imagen inmutable certificada y ajustar
únicamente la configuración productiva, incluida WAN exclusiva. No fusionar ni
actualizar producción como parte de esta continuidad.

## Evidencia histórica: ensayo LAN autorizado — 2026-11-17

Esta sección conserva el alcance y las decisiones vigentes durante el ensayo
LAN. La autorización posterior del equipo de redes para usar `vmbr3` sin tag de
Proxmox, registrada arriba, reemplaza la topología LAN como siguiente paso de
pruebas. La evidencia LAN no se convierte en evidencia WAN.

### Alcance del ensayo LAN

Completar primero los servicios. Ejecutar ciclos LAN 1 y 2 de Nextcloud y n8n:
cuatro planes por Ubuntu 24.04 y AlmaLinux 9, con seis operaciones físicas
simultáneas y dos controles por ciclo. Confirmar instalación y readiness,
conectividad SSH por llave y contraseña, HTTPS con la CA del piloto, uso real de
la aplicación, acceso del usuario, persistencia tras reinicio, entrega y
eliminación física. `BLOCKED` o `FAIL` detiene nuevas inyecciones y requiere
recuperación controlada de las instancias propias.

No iniciar la matriz de agentes hasta que ambos ciclos de servicios terminen
`PASS`. La matriz KVM completa y los ciclos WAN se mantienen como etapas
posteriores a servicios. El ensayo LAN no demuestra conectividad WAN.

El ciclo LAN `202611171525` quedó `BLOCKED` por pérdida de la salida SSH del
runner después de registrar dos verificaciones. Sus seis VMs propias fueron
recuperadas y confirmadas ausentes mediante la API. La corrección del runner
preserva resultados frente a `BrokenPipeError`; aun así, se debe desplegar y
verificar la revisión candidata antes de repetir la matriz. El detalle y las
rutas privadas de evidencia están en `docs/auditorias/SERVICIOS_LAN_20261117.md`.

Durante esa fase se autorizó ejecutar las combinaciones con
`--network-profile lab_dhcp`, sin tag y sin reservas NetBox. Las aplicaciones
web usan `access_mode=ip` y la CA privada del piloto para la IP observada
mediante QEMU Guest Agent. Esas instrucciones describen el ensayo histórico y
no sustituyen la configuración WAN actual de `vmbr3` sin tag de Proxmox.

El runner conserva WAN como valor predeterminado. Para LAN se debe indicar el
perfil explícitamente. La identidad DHCP se configura por MAC en Netplan o
NetworkManager según la imagen, y el certificado se emite para la dirección
observada. La suite comprueba SSH por clave y contraseña, TLS validado, persistencia,
entrega y eliminación. Durante estos ensayos se habilita la ejecución de servicios
en el manifiesto dev, conservando producción sin cambios.

El formulario de autorización acepta el token como valor, entre comillas, con
prefijo Bearer o como una única asignación `IACP_API_TOKEN=...`. No registra ni
persiste el secreto en el navegador. El recurso JavaScript cambia su versión
para evitar que una copia anterior del formulario permanezca en caché.

### Recuperación del primer ensayo LAN

La primera tanda creó seis KVM y verificó los cuatro planes de Ubuntu 24.04.
El primer reboot de Ubuntu 26.04 quedó incierto al consultar Guest Agent durante
el arranque. Se conserva el resultado fallido; no cuenta como ciclo aprobado.
La corrección espera disponibilidad del canal con lecturas y registra la identidad
verificada en todas las imágenes. No repite un POST exec de resultado incierto.

La recuperación de un reboot puede demostrar su ejecución mediante una única tarea
Proxmox exitosa del mismo nodo, VMID, identidad de API y ventana temporal de la
operación. Se verifica su UPID y estado final; tareas ambiguas, ajenas o fuera de
la ventana no autorizan cerrar la intención. No se vuelve a enviar el reboot.

Las verificaciones y limpiezas de instancias independientes admiten hasta seis
hilos, manteniendo el mismo límite de infraestructura. El cliente limita su
frecuencia de consultas y reintenta HTTP 429 solo en lecturas o solicitudes con
clave de idempotencia. Ante un fallo activa contención para nuevas mutaciones.
La evidencia identifica también la huella SHA-256 del runner utilizado.

La prueba local de TLS debe incluir un handshake por IP detrás de Docker NAT,
no solo `caddy validate`. `scripts/test_ip_tls_docker.py` genera una CA temporal,
comprueba Nextcloud/n8n con la dirección loopback, valida el certificado sin SNI y
verifica que el proxy no agrega autenticación HTTP Basic; las credenciales y el
alta de propietario se gestionan en las pantallas nativas de cada aplicación. No
utiliza MNG. Las recetas IP fijan `default_sni` para seleccionar el certificado correcto.
Los sockets SSH de Ansible se mantienen en el directorio temporal privado del
trabajo, compatible con el filesystem de solo lectura de los workers.

En Ubuntu 26.04 se observó que la dirección inicial del ensayo cambió tras reboot.
El binding genérico ahora comprueba `CLIENTID=01+MAC` en la concesión de networkd
y espera que esa dirección aparezca en QEMU Guest Agent antes de aceptarla.
Al cambiar la configuración Netplan se reinicia networkd dentro de la VM aislada;
no se modifica la red del hipervisor. NetworkManager reactiva su conexión con
client-id MAC. No se certifica una dirección anterior mientras converge DHCP.

En la prueba local de ejecución real de n8n se detectó que `n8n execute` intenta
abrir el mismo puerto interno del broker que el proceso principal. El runner
asigna `N8N_RUNNERS_BROKER_PORT=5681` únicamente al proceso CLI de validación;
no publica ese puerto ni cambia el servicio. La importación y ejecución del
workflow de código se comprobaron en Docker con la imagen fijada por digest.

### Actualización IPAM 2026-11-18

El equipo de desarrollo confirmó que `190.0.231.2/24` está disponible. Se
eliminó únicamente su registro reservado en NetBox (ID 2026). La lectura
posterior confirmó que `.2` ya no aparece y que el registro del gateway
`190.0.231.1/24` (ID 2010) conserva estado `reserved`. El pool de pruebas usa
las direcciones `.2` a `.253` que NetBox reporte disponibles; la primera lectura
posterior mostró `.2` como siguiente dirección elegible.

La primera verificación posterior a la eliminación quedó `FAIL` porque utilizó
el cliente de la revisión anterior, que todavía comenzaba en `.3`. Se conserva
esa evidencia y se agregó una verificación independiente de solo lectura con
resultado `PASS` en `/opt/iacp/evidence/netbox-post-delete-verification-20261118T042026Z`.
El código corregido y sus pruebas unitarias ahora incluyen `.2` cuando está
disponible en NetBox.

### Estado vigente de preparación y matrices — 2026-11-18

Después de esa lectura, el equipo reservó nuevamente `190.0.231.2/24` en NetBox
por la discrepancia ARP; los ensayos deben usar la siguiente dirección que el
pool confirme disponible. La revisión local corrige el acceso de preparación
AlmaLinux: QEMU Guest Agent instala `cloud-init`, `iproute`, OpenSSH y utilidades
SELinux antes de requerir SSH, luego ejecuta y verifica un arranque real de
Cloud-Init. La candidata solo pasa a promoción si el arranque termina, el
helper está fijado, SELinux sigue Enforcing y el hardware coincide con Ubuntu
24.04/29000. El cambio todavía no se ha ejecutado en MNG ni desplegado en el
IACP Control Plane (VM 104).

Alcance vigente solicitado: cuatro ciclos WAN KVM con las 16 combinaciones de
imagen/plan por ciclo (64 PASS principales), y dos ciclos por cada servicio,
Nextcloud y n8n (32 PASS principales por servicio). Las KVM verifican SSH por
llave y contraseña, salida WAN, reinicio, eliminación y liberación de IP. Los
servicios además verifican CA/TLS, GUI y autenticación funcional, persistencia,
reinicio, entrega y liberación de IP. Cada lote conserva seis aprovisionamientos
simultáneos y los controles se reportan aparte. La certificación permanece
pendiente; no empezar matrices con operaciones activas o inciertas ni con
workers no sanos. Producción permanece excluida.

### Catálogo Dev y VMID AlmaLinux confirmado — 2026-11-18

La revisión desplegada inicialmente seleccionó 29016/29017 como candidatas, pero
el inventario del clúster MNG consultado desde el IACP Control Plane (VM 104)
confirmó que esos VMID no existen. Las imágenes AlmaLinux actuales aparecen
como templates detenidos en `mng01`: AlmaLinux 9 en 29010 y AlmaLinux 10 en
29011. El usuario confirmó que estos son los VMID nuevos que deben probarse.

El catálogo de desarrollo se corrigió para utilizar 29010/29011, igual que el
catálogo estable. La matriz crea clones de ensayo y no modifica los templates.
Antes de inyectar, el preflight confirma imagen, VMID, nodo y `template: 1`.

La dirección `190.0.231.2` se excluye expresamente de las reservas NetBox en
desarrollo y del preflight WAN, incluso si NetBox la reporta disponible; el
runner exige seis direcciones adicionales dentro del rango autorizado. La regla
no cambia las reservas del entorno productivo. No usar esa IP en la matriz hasta
que Operaciones de red cierre la discrepancia ARP.

La selección corregida del catálogo está pendiente de despliegue y verificación
en Dev. No se ha inyectado la matriz física en esta revisión. Las plantillas
29010/29011 permanecen intactas; no declarar PASS integral antes de certificar
los ciclos previstos.

### Rango NetBox y consulta Fedora/Debian — 2026-11-18

Por solicitud del usuario, las nuevas asignaciones estáticas se limitan a
`190.0.231.10`–`190.0.231.250`. La lectura GET desde el API del stack dev en el
IACP Control Plane (VM 104) confirmó el prefijo `/24`, gateway `190.0.231.1` y
241 direcciones disponibles en ese rango, con `.10` como primera libre y `.250`
como última. NetBox no se modificó. La validación de asignaciones históricas
conserva `.2`–`.253` para permitir recuperación y liberación seguras; ese rango
amplio no se usa para nuevas reservas.

La lectura autenticada de Proxmox desde el mismo stack no mostró VMID 29005 ni
29006 en el inventario visible. Las consultas GET de configuración a ambos
VMID en `mng01` respondieron HTTP 403. Esto no demuestra que las VMs no existan;
el token actual no permite confirmar template, almacenamiento, hardware ni
Cloud-Init. Estado **BLOCKED** para Fedora 45 y Debian 13: no se añadieron al
catálogo ni se desplegó una referencia que el runtime no puede validar.

La política `.10`–`.250` quedó implementada localmente. `tests/test_netbox.py` y
`tests/test_wan_matrix.py`: 23 PASS, 28 SKIP. El runtime dev continúa en
`79e9d51431f9f6233cff529de2c94c95fe204d60`; preflight, inventory y verify de la
revisión activa dieron PASS con evidencias privadas bajo
`/opt/iacp/evidence/`. La política local aún no está desplegada.

### Alcance autorizado para la API de producción — 2026-11-23

El usuario autorizó ejecutar en `iac.cyberfuel.com` las matrices WAN completas
después de verificar el candidato en Dev. Los recursos físicos de ensayo se
crean en el clúster MNG, nodo `mng01`, con perfil `prod_static` e IPAM NetBox;
Platinum/PRX no forma parte de estos ensayos. `scripts/wan_matrix.py` mantiene
KVM productivo bloqueado por defecto y requiere `--production-full-kvm-matrix`
para cada ciclo completo. Los ciclos 1 y 2 cubren las 48 combinaciones
solicitadas (seis imágenes, cuatro planes, dos ciclos). La matriz de servicios
usa `--production-full-matrix` para los 16 casos de Ubuntu 24.04 y dos controles
de autenticación.

Antes de cada inyección, los seis workers de infraestructura y los cuatro de
servicios deben estar sanos, idle y con heartbeat reciente; no puede haber
operaciones inciertas, trabajos activos ni locks. Los lotes KVM conservan seis
aprovisionamientos simultáneos. La autorización amplía el alcance de ensayo de
la API productiva y no autoriza cambios en Platinum/PRX. Los resultados se
registran con el digest del runtime y el hash del harness; no declarar PASS
antes de cerrar todos los casos y verificar limpieza/IPAM.
