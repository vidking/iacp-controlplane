# Bootstrap controlado para AlmaLinux 9 y 10

## Problema demostrado

Las imágenes actuales ejecutan Guest Agent como root bajo el dominio SELinux
`virt_qemu_ga_t`. En la VM 40023 del ensayo de MNG, leer la clave pública del host
SSH devolvió Permission denied; `getenforce` también quedó restringido. El agente
no puede efectuar la preparación administrativa que funciona en Ubuntu. No se
resuelve deshabilitando SELinux ni aceptando claves SSH sin verificar.

## Paquete revisable

Generar el paquete desde la revisión aprobada del repositorio:

```bash
.venv/bin/python scripts/prepare_almalinux_bootstrap.py \
  --output /ruta/privada/bootstrap-almalinux-REVISION
```

El generador no modifica el sistema ni llama a Proxmox. Produce un helper fijo,
`install.sh` y un manifiesto con su SHA-256. El helper acepta exclusivamente UUID
de instancia, MAC, modalidad DHCP/static y direccionamiento autorizado. No acepta
código, rutas ni comandos del consumidor. El intérprete usa modo aislado y PATH
fijo. Su salida contiene únicamente identidad y clave pública SSH.

## Preparación de una copia de imagen

Aplicar únicamente dentro de una copia versionada de la Golden Image de
laboratorio, después de revisar el manifiesto y disponer de acceso administrativo
confiable a esa copia. No modificar las imágenes originales ni la VM 29000.

1. Comprobar AlmaLinux 9/10 y SELinux Enforcing.
2. Instalar `policycoreutils-python-utils` si falta; comprobar que la política
   dispone del boolean `virt_qemu_ga_run_unconfined` y del tipo
   `virt_qemu_ga_unconfined_exec_t`.
3. Ejecutar `install.sh` como root en la copia. Instala solamente
   `/usr/local/libexec/iacp-guest-bootstrap`, etiqueta ese ejecutable y habilita
   el boolean documentado de Guest Agent. SELinux permanece Enforcing.
4. Validar desde Guest Agent la transición y el protocolo 1, lectura de la clave
   pública, DHCP por MAC y DNS, sin permitir ejecución de código arbitrario a
   través del helper.
5. Preparar la copia para clonado con el procedimiento habitual de la empresa,
   registrar versión y actualizar el catálogo únicamente tras certificarla.

La política estándar permite la transición del ejecutable etiquetado a
`virt_qemu_ga_unconfined_t`. Ese privilegio pertenece al helper fijo administrado
por root; no se concede al intérprete Python genérico ni se cambia SELinux a
permissive. Debe revisarse el alcance del boolean respecto de otros hooks ya
instalados en la imagen. Referencia primaria:
[notas oficiales de seguridad de Red Hat](https://docs.redhat.com/es/documentation/red_hat_enterprise_linux/8/html/8.9_release_notes/new-features).

## Comportamiento del worker

Para AlmaLinux 9/10, el worker exige ese helper y protocolo 1. Si falta, conserva
la intención y devuelve `IMAGE_BOOTSTRAP_REQUIRED`; no intenta eludir la política.
La clave SSH se obtiene por el canal Proxmox autenticado y se contrasta con MAC,
VMID, nombre, pool e IP observada. Las recetas Ansible posteriores mantienen
SELinux y emplean etiquetas de bind mounts compatibles.

## Preparación verificada en MNG — 2026-11-17

Con autorización explícita se prepararon copias completas en `mng01`:

| Imagen lógica | Original conservada | Copia preparada |
| --- | --- | --- |
| AlmaLinux 9 | 29010 | 29012 |
| AlmaLinux 10 | 29011 | 29013 |

Ambas copias aprobaron la ejecución del helper por Guest Agent, su protocolo 1,
identidad/MAC, clave pública SSH y DNS. Se verificó SELinux Enforcing antes y
después. Se retiraron las credenciales temporales, las claves de host, el estado
cloud-init y la identidad de instancia antes de convertirlas en templates. Las
configuraciones públicas observadas de las originales permanecieron iguales.
Los VMID nuevos y originales quedan protegidos por la API.

AlmaLinux 10 incorpora `resolvectl` aunque `systemd-resolved` esté inactivo. La
primera comprobación detectó ese caso y se detuvo; se corrigió la observación de
DNS para consultar NetworkManager cuando resolved no responde. Se recuperó
únicamente esa copia, después de comprobar que el helper anterior había acabado.

Evidencia privada en el IACP Control Plane (VM 104):
`/opt/iacp/evidence/alma9-golden-20261117/` y
`/opt/iacp/evidence/alma10-golden-20261117/`. No copiar claves ni contraseñas a Git.
Esto valida la preparación de las bases, no las matrices completas de aplicaciones.

El procedimiento reproducible está en `scripts/prepare_golden_copies.py`. Sin
`--execute` muestra exclusivamente el plan; la ejecución admite solo desarrollo
y el nodo MNG verificado. `--resume` requiere una copia con SSH previamente
verificado y la terminación observada del helper anterior. Un resultado incierto
no autoriza repetir una ejecución.

Las matrices completas con AlmaLinux permanecen pendientes de ejecución sobre
estas nuevas copias. No se ha actualizado producción.

## Revisión de arranque v2 — 2026-11-17

La tanda de clones descubrió que `cloud-init status --wait` escribe puntos de
progreso sin salto de línea en su formato tabular. La preparación inicial
posterior al login no reproducía esa carrera. El bootstrap v2 consume JSON y
comprueba estado `done`, ausencia de errores y únicamente deprecaciones conocidas,
incluso cuando cloud-init devuelve código cero. Las revisiones anteriores
29012/29013 se conservan como historial; el catálogo dev utiliza nuevas copias
29014 (AlmaLinux 9) y 29015 (AlmaLinux 10), ambas preparadas desde las originales
29010/29011 y verificadas con SELinux Enforcing. Evidencia privada en
`/opt/iacp/evidence/alma9-golden-v2-20261117/` y
`/opt/iacp/evidence/alma10-golden-v2-20261117/`. Se planeó repetir la matriz
con v2; la tanda WAN posterior detectó que faltaba `iproute`.

## Revisión de herramientas de red v3 — 2026-11-17

La primera tanda WAN encontró que las copias 29014/29015 no incluyen el
comando `ip`, requerido para comprobar la configuración estática, la ruta por
defecto y la vecindad del gateway dentro del guest. Se conservarán intactas esas
copias y se preparan nuevas imágenes desde los originales protegidos:

| Imagen lógica | Original conservada | Nueva copia v3 | Paquetes adicionales |
| --- | --- | --- | --- |
| AlmaLinux 9 | 29010 | 29016 | `iproute`, `policycoreutils-python-utils` |
| AlmaLinux 10 | 29011 | 29017 | `iproute`, `policycoreutils-python-utils` |

La fábrica verifica `ip -V` antes de instalar el helper y convertir la copia en
template. La preparación física v3 terminó `PASS`: los dos helpers aprobaron
protocolo e identidad con SELinux Enforcing, `ip -V` estuvo disponible y las
plantillas quedaron listas. Evidencia privada en
`/opt/iacp/evidence/alma9-golden-v3-20261117-r2/` y
`/opt/iacp/evidence/alma10-golden-v3-20261117/`. El cambio de catálogo aún debe
publicarse en dev; esta preparación no certifica las matrices de aplicaciones.
Las imágenes originales y las copias v2 no se modificaron.

## Consolidación de las plantillas AlmaLinux en MNG

**Estado: BLOCKED.** Las copias 29016 y 29017 aprobaron la preparación de la
imagen base. La [matriz física](CONTINUIDAD_MATRICES_WAN.md) mantiene casos
pendientes de verificación y registra un fallo de resolución ARP del gateway WAN.

El estado final solicitado para el nodo `mng01` del clúster MNG es `almalinux-9`
en la VMID **29010** y `almalinux-10` en la VMID **29011**, con los mismos orígenes
en los catálogos de producción y desarrollo. Las candidatas son **29016 → 29010**
y **29017 → 29011**. La 29015 corresponde a AlmaLinux 10 v2 y le falta `iproute`.
Las revisiones consultadas por HTTPS en el IACP Control Plane (VM 104) fueron
`85e54ac` en `iac.cyberfuel.com` y `61ea3fb` en `dev-iac.cyberfuel.com`. Sus
catálogos de repositorio apuntan a
29010/29011 y 29016/29017, respectivamente. Cambiar el contenido bajo 29010/29011
afecta los nuevos aprovisionamientos de producción aunque su imagen de API siga
siendo la misma.

1. **Verificar el estado real antes de la intervención.** El equipo de
   operaciones consulta `GET /v1/images` en ambos dominios, registra la revisión y
   los digests ejecutados y verifica el inventario y las dependencias de los
   ocho VMID AlmaLinux en MNG. El módulo Terraform crea clones completos. La
   inspección física también debe cubrir clones externos y cualquier referencia
   activa a las plantillas. Validar nodo, almacenamiento, etiquetas, Guest Agent,
   arranque y cloud-init.
2. **Certificar las candidatas en desarrollo.** Cerrar el bloqueo de WAN con el
   equipo de redes y completar los ciclos aprobados, con seis workers de
   infraestructura sanos e idle antes de cada tanda de seis. Cubrir los cuatro
   planes KVM en AlmaLinux 9 y 10 y las aplicaciones habilitadas para AlmaLinux 9.
   El alcance actual de aplicaciones incluye Ubuntu 24.04 y AlmaLinux 9. La
   cobertura de AlmaLinux 10 en este alcance corresponde a KVM. Registrar
   aprovisionamiento, identidad y SSH, red, reinicio, persistencia, eliminación y
   liberación IPAM cuando aplique. Arrancar los clones de prueba de ambas
   candidatas en MNG y comprobar `ip -V`, SELinux Enforcing y el protocolo 1 del
   helper a través de Guest Agent. Registrar el VMID de origen de cada clon y
   eliminar las instancias de ensayo al finalizar. Los ensayos se ejecutan en
   desarrollo con sus rangos, colas y credenciales aislados según la política
   vigente.
3. **Preparar una ventana operativa y reversión.** Antes de liberar 29010/29011,
   verificar la recuperación de ambas originales a partir de respaldos de
   configuración y discos. Conservar las candidatas 29016/29017 disponibles para
   desarrollo y reversión. Coordinar la suspensión de nuevos aprovisionamientos
   que consuman las plantillas durante el corte y confirmar cero operaciones
   afectadas activas, en cola o inciertas y ausencia de locks antes de proceder.
   La intervención física corresponde al equipo de operaciones de MNG.
4. **Establecer los VMID definitivos y verificar los dos catálogos.** Recrear las
   plantillas certificadas bajo 29010/29011, registrar su correspondencia con las
   candidatas y verificar la configuración en `mng01`. Publicar primero en
   desarrollo la revisión del catálogo con los VMID definitivos mediante
   `scripts/iacpctl` y el Compose canónico. Comprobar `GET /v1/images` y validar
   los aprovisionamientos sobre esos VMID. Cualquier promoción de software a
   producción usa la misma imagen inmutable validada en desarrollo y requiere
   autorización operativa. Verificar desde producción los aprovisionamientos
   admitidos para ambas Golden Images con sus propios recursos de ensayo.
5. **Retirar las copias al cierre.** Solo después del `PASS` en ambos dominios y de
   verificar ausencia de referencias y dependencias, el equipo de operaciones
   retira 29012–29017 del inventario de MNG. Conservar los respaldos y el
   historial de evidencias según la política operativa. Ante un fallo en el
   corte, suspender la limpieza, restaurar las originales en 29010/29011 y
   recuperar el catálogo anterior de desarrollo si se llegó a cambiar. Mantener
   las protecciones de la API, incluido el IACP Control Plane (VM 104), la VM 106
   y la VM 29000. El alcance excluye Platinum/PRX.

### Creación desde imágenes nuevas después del incidente

**Estado: BLOCKED, 2026-11-17.** La salida operativa de `mng01` confirmó la
ausencia de 29010 y 29011 tras el borrado. Los dos clones fueron rechazados
porque el parámetro `pool` estaba vacío. A continuación se ejecutó la limpieza
del rango 29012–29017. La creación nueva sustituye el procedimiento de corte
anterior para este incidente.

1. Obtener las imágenes Generic Cloud oficiales x86_64 de AlmaLinux 9 y 10 y
   verificar la firma de su manifiesto y el SHA-256 de cada imagen.
2. Confirmar el pool de IACP, el estado del almacenamiento `DS_CYBERFUEL` y
   los VMID libres en el clúster MNG antes de importar. Crear las VMs de
   preparación en 29016 y 29017 en `mng01`, que son los orígenes de la
   revisión de desarrollo `61ea3fb`.
   **Importación: PASS, 2026-11-17.** La salida de Proxmox confirma la creación
   de `scsi0` y del disco cloud-init de cada VM en `DS_CYBERFUEL`. Ambos
   SHA-256 coincidieron y las dos VMs quedaron detenidas.
3. Instalar `qemu-guest-agent`, `iproute` y `policycoreutils-python-utils`
   dentro de los guests. Aplicar el paquete generado por
   `scripts/prepare_almalinux_bootstrap.py`, verificar protocolo 1 y SELinux
   Enforcing, y retirar las credenciales temporales antes de convertir cada VM
   en plantilla. Completar los aprovisionamientos físicos de desarrollo.
4. Verificar que 29010 y 29011 estén libres y crear ambos destinos mediante
   clones completos de las candidatas certificadas. Aplicar el catálogo común
   en desarrollo y comprobar ambos dominios antes de retirar las candidatas.

### Resultado de preparación y preflight WAN

El 2026-11-17, hora de Costa Rica, se completó la preparación en el nodo
`mng01` del clúster MNG desde el IACP Control Plane (VM 104). Desarrollo
ejecutaba `61ea3fbe01157c89061186ae1473787e5d0e1fb3`.

| Comprobación | Resultado | Evidencia |
| --- | --- | --- |
| Bootstrap de 29016, AlmaLinux 9.9, y 29017, AlmaLinux 10.3 | PASS | Ambas imágenes ejecutaron el helper por QEMU Guest Agent con protocolo 1 y SELinux Enforcing. Coincidieron la MAC, el ID de prueba y la clave SSH observada por consola autenticada. Se confirmó `iproute` y el DNS esperado. |
| Sellado de 29016 y 29017 | PASS | Ambas VMs están detenidas y tienen `template: 1` y disco base en `DS_CYBERFUEL`. Se eliminaron las credenciales de preparación y se regeneró el disco cloud-init. Verificación en `/opt/iacp/evidence/almalinux-nuevas-preparacion-20261118T031925Z/confirmation.json`. |
| Matriz física KVM WAN, ciclo 1 | BLOCKED | `NETBOX_IPAM_NOT_FOUND` antes de aprovisionar. El NetBox configurado respondió 404 para el prefijo ID 52 y la búsqueda por `190.0.231.0/24` devolvió cero resultados con la credencial del stack. Evidencia en `/opt/iacp/evidence/almalinux-nuevas-wan-kvm-ciclo1-20261118032630/results.jsonl`. |

### Separación de catálogos — 2026-11-18

El catálogo estable `catalog.json` mantiene 29010/29011. El stack de desarrollo
selecciona `IACP_MNG_CATALOG=/app/terraform/environments/mng01/catalog-dev.json`,
que apunta a las candidatas 29016/29017. No se reemplazarán las plantillas
oficiales antes de terminar y revisar la certificación de desarrollo; la
promoción posterior requiere una ventana y reversión coordinadas porque MNG es
compartido.

La imagen oficial traía deshabilitados los RPC que invocan y consultan la
salida del helper. Las dos candidatas incorporan `guest-exec` y
`guest-exec-status` en la lista permitida existente de Guest Agent. Las
restricciones de SELinux permanecen activas. El preflight confirmó el puente
`vmbr3` sin tag de Proxmox, con VLAN 211 aguas arriba. La certificación física
requiere recuperar la visibilidad del prefijo autorizado en NetBox y completar
los ciclos documentados antes de crear los destinos 29010/29011.

### Diagnóstico del bootstrap de servicios en AlmaLinux — 2026-11-18

En el primer ciclo WAN de servicios, las VMs de prueba 40165 y 40166
(AlmaLinux 9) llegaron a `SERVICE_BOOTSTRAP_GUEST_FAILED`. La salida del
bootstrap indicaba que faltaban los DNS `1.1.1.1` y `8.8.8.8`, aunque la
configuración de Proxmox y cloud-init contenía ambos servidores. La causa está
en la observación del worker: `resolvectl dns eth0` puede devolver código 0 con
salida vacía o incompleta cuando `systemd-resolved` no administra el enlace de
NetworkManager.

La revisión de `worker/service_bootstrap.py` acepta la respuesta de `resolvectl`
solo si contiene las dos direcciones exactas; en otro caso consulta
`nmcli -g IP4.DNS device show`. La validación usa coincidencias de direcciones
completas para evitar aceptar valores parecidos. Las pruebas unitarias cubren
respuestas sin salida, parciales, código de error, direcciones parecidas y una
respuesta completa de `resolvectl`.

AlmaLinux ejecuta el helper que se generó e instaló dentro de la imagen; por
tanto, desplegar solo el worker/API no actualiza el helper de las imágenes que
ya existen. Para probar el arreglo, Operaciones debe generar y preparar nuevas
copias usando esta revisión, validar el hash del helper y volver a comparar todo
el hardware con la referencia 29000 antes de habilitarlas en el catálogo dev.
`scripts/prepare_golden_copies.py` rechaza un VMID destino existente. No
eliminar ni reemplazar 29010/29011 sin respaldo verificado y procedimiento de
reversión; conservar intactas las fuentes 29016/29017. La preparación física de
las imágenes corresponde al equipo de operaciones de MNG.

**Estado: corrección del código fuente verificada; actualización de las imágenes,
despliegue y repetición física pendientes.** Los resultados inciertos existentes
deben recuperarse mediante el flujo controlado de la API antes de cualquier
reintento. Esta corrección no certifica todavía las matrices KVM ni Nextcloud/n8n,
no libera sus IP de NetBox y no modifica las Golden Images ni la configuración
de producción.

### Preparación por QEMU Guest Agent — 2026-11-18

La fábrica ya no depende de acceso por consola o SSH antes de reparar paquetes
ausentes. Inicia la candidata, comprueba QEMU Guest Agent, instala
`cloud-init`, `iproute`, `openssh-server` y utilidades SELinux por ese canal,
entrega una llave temporal y verifica un arranque limpio donde Cloud-Init
termina y `ip -V` responde. El protocolo oficial de QEMU documenta `input-data`
codificado en Base64 para suministrar stdin a procesos del guest:
[QEMU Guest Agent reference](https://www.qemu.org/docs/master/interop/qemu-ga-ref).
La contraseña temporal no se guarda en argumentos de proceso ni en evidencia.
Antes de sellar se retiran la llave, el marcador temporal y el acceso de
preparación, y se comprueba que SELinux continúe Enforcing. La ejecución física
de este flujo todavía está pendiente.

### Requisito de cloud-init en clones AlmaLinux — 2026-11-18

La inspección de solo lectura de los clones WAN creados desde las imágenes AlmaLinux
29010/29011 confirmó que faltaban tanto `cloud-init` como `iproute`. El worker
depende de ambos contratos: Proxmox entrega la configuración NoCloud de red,
credenciales y clave SSH, y las comprobaciones WAN usan `iproute2`. La fábrica
ahora instala `cloud-init` además de `iproute`, habilita las cuatro etapas de
systemd de cloud-init y exige que `cloud-init --version` e `ip -V` respondan antes
de sellar una imagen. Una copia que no cumpla esas comprobaciones no se publica
como Golden Image.

La instalación mediante `dnf install cloud-init` y la habilitación del servicio
siguen el procedimiento documentado por [Red Hat para imágenes personalizadas
de RHEL 10](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/deploying_and_managing_rhel_on_amazon_web_services/deploying-a-rhel-instance-by-using-a-custom-base-image).
La compatibilidad de los paquetes debe confirmarse en los repositorios habilitados
de cada versión de AlmaLinux durante la preparación física.

La corrección de la fábrica y sus pruebas son locales. No certifica aún las
imágenes físicas 29010/29011 ni las matrices. La actualización de templates del
clúster MNG queda detenida mientras el stack productivo registra operaciones
activas que comparten el clúster; no se cambia el stack productivo ni se altera
ninguna VM de cliente para preparar estas pruebas.

La preparación parte de los templates actuales 29010/29011 y crea candidatas
temporales 29016/29017. Después verifica paquetes, helper, SELinux y paridad de
hardware. Este cambio no promueve ni elimina templates, y no crea copias de
respaldo 29018/29019. La sustitución futura de 29010/29011 requiere un
procedimiento operativo revisado, respaldo y ventana independiente, porque esos
VMID también sirven al stack productivo. Si cualquiera de los stacks tiene
actividad, se rechaza la preparación compartida.
