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
