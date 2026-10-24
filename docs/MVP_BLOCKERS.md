# Cierre de bloqueos de inicialización y energía

VM 30008 ya estaba eliminada cuando comenzó esta investigación. Se correlacionan
su operación, PID y SHA del inicializador con el historial Proxmox conservado en
[MVP_BLOCKERS_DIAGNOSIS.json](validation/MVP_BLOCKERS_DIAGNOSIS.json). Su stderr
no se conservó: no se puede atribuir retrospectivamente una causa exacta al
proceso fallido ni recuperar logs internos de un disco eliminado.

La variante root tenía dos supuestos incompatibles con SSH activado por socket:
`/run/sshd` podía no existir y `reload ssh` falla si el servicio aún no arrancó.
El correctivo crea ese directorio temporal, conserva `sshd -t` y la comprobación
de configuración efectiva y usa `reload-or-restart ssh`. Solo cambia la variante
root de laboratorio. El SHA nuevo requiere otra política aprobada; no se editan
snapshots históricos ni se vuelve a consumir la inicialización de VM 30008.
Pruebas: **770 aprobadas**, incluida ejecución del programa de política con SSH
activo/inactivo y rechazo de configuración inválida antes de recargar.

El log de Shutdown acredita `guest-ping` con timeout, seguido del timeout de
apagado. Se solicitó dos segundos después del arranque de QEMU tras reboot.
La ausencia de QGA listo en ese momento no demuestra ausencia del paquete en la
Golden Image. `power_state=running` mantiene su significado de hipervisor;
`guest_ready` no se redefine. Antes de la siguiente acción de energía, observar
QGA y DHCP mediante `/network/discover` hasta `network_observation.status=observed`
y direcciones presentes. No repetir el inicializador ni verificar de nuevo la
contraseña inicial después de una rotación del cliente.

29000 permanece apagada como plantilla y declara `agent=enabled=1`, DHCP, root,
OVMF/q35. La instalación y arranque del servicio y ACPI dentro del invitado deben
acreditarse en la única clon descartable, conservando versión y configuración de
origen. No se cambia la plantilla ni se conceden permisos Guest Agent al Lifecycle
Worker. El Stop administrativo histórico sigue separado de Shutdown; no se añade
ningún fallback forzado automático.

Locust permanece en su repositorio externo. La revisión del historial inicial
`6aeff5a` no detectó secretos entre sus 12 blobs ni evidencia confidencial
versionada. La carga original conserva 334 solicitudes sin errores y p95 de
590/430/440/630 ms para 10/25/50/100 usuarios: **dos etapas incumplieron 500 ms**.
El plan sostenido del repositorio externo separa HTTP de provisión con cuotas y
mide acciones hasta su finalización, además de la admisión HTTP.


## Resultado de la nueva validación

Candidata `dff8052`: VM30009/UUID `93899a50-f9e4-48c1-987b-5a05ae79a670`,
2 vCPU/2048 MiB/24 GiB, raíz por API HTTPS y arranque automático. Cloud-Init
sin errores, QGA activo (unidad static activada por dispositivo), DHCP
192.168.3.73 y SSH root autenticado. Rotación por `passwd` sobre SSH con host key
fijada desde QGA; nueva contraseña válida e inicial rechazada después de start y
reboot. Reconciliación readonly con la VM apagada: postcondiciones verificadas y
state sin cambios. La API de reconciliación respondió correctamente
`RECONCILIATION_NOT_REQUIRED` una vez recuperada la creación; no se fabricó una
incertidumbre para probarla.

Dos Shutdown por API finalizaron, seguidos de DELETE de la misma VM. La Golden
Image se verificó por configuración/procedencia de clon y paquetes heredados;
permaneció apagada. Se observaron tablas ACPI y logind; el apagado acreditado usa
QGA, no una prueba independiente de señal ACPI. No se recuperó stderr histórico
de VM30008 y no se atribuye una causa retrospectiva definitiva a su inicializador.
La carga sostenida permanece preparada, sin resultado de capacidad declarado.
