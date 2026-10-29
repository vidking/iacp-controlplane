# Preparación de Golden Images AlmaLinux para IACP

Las Golden Images originales `29010` (AlmaLinux 9) y `29011` (AlmaLinux 10)
permanecen protegidas e inmutables. Esta guía describe la revisión de una copia
de construcción en el clúster MNG; no se ejecuta sobre VM 104, VM 106 ni sobre
las plantillas originales.

## Capacidades requeridas

El servicio `qemu-guest-agent` debe mantener SELinux en `Enforcing` y un
allow-list que incluya solamente las RPC que IACP usa:

`guest-info`, `guest-exec`, `guest-exec-status`, `guest-file-open`,
`guest-file-read`, `guest-file-close` y `guest-network-get-interfaces` (además
de las capacidades de observación y energía ya aprobadas por el perfil).

La política `deploy/mng/almalinux/iacp_qga.te` concede al dominio confinado
`virt_qemu_ga_t` únicamente acceso a:

- lectura de Cloud-Init (`cloud_var_lib_t`);
- lectura de estado DHCP mediante NetworkManager;
- escritura del archivo de identidad del guest en el tipo dedicado `iacp_guest_data_t`;
- lectura, sin escritura, de la clave pública del host en el tipo dedicado
  `iacp_guest_host_public_t`;
- escritura controlada de la conexión de identidad (`NetworkManager_etc_rw_t`);
- actualización de `machine-id` (`machineid_t`).

No se habilita `virt_qemu_ga_run_unconfined`, no se pone SELinux en modo
permisivo y no se habilitan RPC adicionales.

## Inicialización

### Canal de clave pública del host

Las copias de construcción de AlmaLinux instalan el módulo SELinux y el
archivo de contextos de este directorio. El servicio
`iacp-publish-host-key.service` ejecuta
`iacp-publish-host-key` después de generar las claves de `sshd` y antes de
`cloud-init-final.service`. El publicador lee únicamente
`/etc/ssh/ssh_host_ed25519_key.pub`, valida que sea una clave Ed25519 pública,
y publica una copia desde el punto de vista de `qemu-ga` en:

`/var/lib/iacp/host-identity/ssh_host_ed25519_key.pub`

El directorio queda propiedad de `root:root` con modo `0555` y el archivo con
modo `0444`. El tipo `iacp_guest_host_public_t` solo permite a
`virt_qemu_ga_t` buscar, abrir y leer ese archivo; no permite crear, escribir,
renombrar ni eliminar. El proceso comprueba que una publicación existente
coincida con la clave efectiva y falla cerrado ante una discrepancia.

La preparación reproducible de una copia se ejecuta dentro de esa copia, como
`root`, antes de convertirla en plantilla:

```sh
make -f /usr/share/selinux/devel/Makefile iacp_qga.pp
semodule -i iacp_qga.pp
install -o root -g root -m 0755 iacp-publish-host-key /usr/local/sbin/iacp-publish-host-key
install -o root -g root -m 0644 iacp-publish-host-key.service /etc/systemd/system/iacp-publish-host-key.service
systemctl daemon-reload
systemctl enable iacp-publish-host-key.service
```

La preparación debe detenerse si faltan `checkmodule`, `semodule_package`,
`restorecon` o los paquetes de desarrollo SELinux. El servicio se ejecuta en
el primer arranque, cuando ya existe la clave host efectiva; no se ejecuta
contra una Golden Image original ni se copia una clave privada a la plantilla.

Antes de producir una nueva copia de plantilla, verificar con `restorecon`,
`matchpathcon` y una lectura QGA que el archivo dedicado es accesible, que la
lectura de la clave privada o de `authorized_keys` sigue denegada y que SELinux
continúa en `Enforcing`. Limpiar el archivo publicado, las claves host, el
`machine-id`, Cloud-Init y los artefactos de construcción antes de sellar la
Golden Image revisada. Las Golden Images originales `29010` y `29011` no se
modifican.

El inicializador AlmaLinux consulta `cloud-init status --format json` sin
bloquearse esperando una transición de systemd. Verifica `status=done`, el
marcador `boot-finished`, el resultado sin errores y el DHCP ya materializado
por Cloud-Init. No activa una segunda conexión mediante D-Bus desde QGA: esa
operación no es necesaria y queda fuera del dominio SELinux confinado.

Antes de convertir la copia en plantilla, crear y etiquetar `/var/lib/iacp`
con `iacp_guest_data_t`, limpiar el marcador, `machine-id`, claves host,
estado de Cloud-Init, credenciales temporales, módulos de construcción y
registros de auditoría. La nueva revisión debe conservar CPU, NUMA, firmware,
machine type, controladora, almacenamiento Ceph/RBD, `discard` e `iothread` de
la Golden Image de origen.
