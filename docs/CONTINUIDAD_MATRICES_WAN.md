# Continuidad de las matrices WAN

Estado: **EN CURSO**, 2026-11-17. Rama: `devops/features`. Producción sin cambios.

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
