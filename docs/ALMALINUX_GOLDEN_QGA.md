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
- escritura del archivo de identidad en el tipo dedicado `iacp_guest_data_t`;
- escritura controlada de la conexión de identidad (`NetworkManager_etc_rw_t`);
- actualización de `machine-id` (`machineid_t`).

No se habilita `virt_qemu_ga_run_unconfined`, no se pone SELinux en modo
permisivo y no se habilitan RPC adicionales.

## Inicialización

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
