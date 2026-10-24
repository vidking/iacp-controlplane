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
