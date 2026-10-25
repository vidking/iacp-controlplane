# Cierre operativo de Fase G — MVP KVM

Fecha: **25/10/2026**, zona **America/Costa_Rica**. Cierre del runtime: **12:50:49 UTC−06:00**. Los timestamps técnicos originales se conservan; [fechas-costa-rica.json](fechas-costa-rica.json) presenta sus conversiones locales.

**GO de Fase G para el piloto supervisado de KVM 1 y KVM 2 sobre Ubuntu 24.04 en MNG, nodo mng01.** La promoción, bootstrap y los dos recorridos reales terminaron satisfactoriamente. **NO-GO para resiliencia integral:** no se ha certificado la muerte física aislada del worker ni alta disponibilidad. Este resultado no certifica físicamente KVM 3/4 ni las otras distribuciones registradas.

## Código integrado, desplegado y comprobado

| Elemento | Referencia verificada |
|---|---|
| Main y release operativo | `dedb344b49d401c4da00d0271882881a0ee34dae`, merge del PR #72 |
| Corrección del validador | `d2caee624beee403140d17b3f401ac2c9d0082f8` |
| Barrera integrada previamente | PR #71, merge `e855677ee58e516be3b06cdf9b3d372316115c91`; árbol equivalente a la candidata `e50c6f6` |
| Versión del producto | `0.10.1`; no se inventó un tag de release |
| Esquema efectivo | `0012_commercial_intents` |
| Módulos IaC | `2c0f319ba5d7a45987cd9d0395673828c28f678b`, archivo fuente SHA256 `05ed1d0d6f2b886df8072eb24cd764dc881f63e9f536baa9af3bafa0804d156b` |
| Motor y provider | Terraform `1.16.6`, bpg `0.116.1` |

La referencia histórica estable `8a58db244ec1e9cd1a0d627b21e086548489e75d` **no era el estado inmediatamente anterior a esta promoción**. Al reanudar se encontró una instalación parcial con imágenes `deploy-latest-dedb344b`, esquema 0012, contrato histórico y admisión/Lifecycle cerrados. Sus ImageID eran API `sha256:958d55c217378828b6d8049b0bb733fb01c50c824a7e51a1c598823a70d3a99a` y Terraform Worker `sha256:1dd3e8183b0806a0244a092dd40c31635e0e6b39e81d9dc32c469f5aef3a8b01`.

Imágenes locales efectivamente ejecutadas después de la promoción:

| Servicio | Referencia local | ImageID SHA256 |
|---|---|---|
| API y workers Python | `iacp/controlplane-api:fase-g-e855677` | `511c251830375a201cf6377f477774d07f39c992383cc589decc1fe9ec9ccfa6` |
| Terraform Worker | `iacp/terraform-worker:fase-g-e855677` | `c1157faf2bb71d6469d83c51a6c975b95be24dd8d05610d39c0bfe5c515da3c5` |

Las imágenes se construyeron con el árbol de aplicación de e855677, idéntico al de dedb344; el driver y checkout operativo son dedb344. [codigo-runtime.json](codigo-runtime.json) compara **99 archivos** reales de `/app` con main y registra igualdad. Estos identificadores son ImageID locales, **no digests de un registry publicado**. OCI permanece pendiente independiente. El commit documental de este informe no representa una nueva versión ejecutada.

## Corrección mínima y regresión

`go_live.py` comparte ahora la validación de montajes Lifecycle entre preflight y verify. Acepta el perfil público revisado únicamente como bind de solo lectura, en el destino autorizado, con origen y SHA256 del manifiesto; conserva rootfs de solo lectura y el conjunto limitado de secretos. Rechaza escritura, montajes adicionales, duplicados, enlaces simbólicos, perfil ausente o checksum distinto. La activación comercial verifica el tenant activo y los límites exactos revisados, sin exigir UUID históricos de políticas; la vía histórica conserva su validación independiente.

- Main previo a la corrección: **1.051 pruebas aprobadas**.
- Corrección final integrada, árbol idéntico a main: **1.062 aprobadas, cero omitidas**, 30 advertencias de deprecación, 144 s. [Registro completo](regresion-1062.log).
- Una ejecución previa tuvo un fixture histórico incompleto; se corrigió el fixture y se repitió la suite completa. No se presenta esa ejecución inicial como aprobada.
- Pruebas positivas y negativas: `tests/test_promotion_lifecycle_profile.py`, incluidas restricciones de montaje y límites reales del tenant.
- La revisión incompatible de creación comercial es intencional: `plan_id`, `image_id`, `network`, `access` y contexto comercial; no se afirma equivalencia con el cuerpo histórico. OpenAPI efectivo **3.1.0**, [snapshot congelado](evidencias/fase-g-e2e-kvm2-dedb344-geometria-final/openapi.json). Los cambios del validador no modificaron `/v1`.

## Promoción y salud

Se ejecutaron preflight, env-promote, promoción, activación revisada y doctor mediante el procedimiento existente. Se preservaron imágenes anteriores, volúmenes, datos y secretos. Evidencias originales:

- [Preflight](evidencias/fase-g-preflight-dedb344-02/).
- [Promoción de entorno](evidencias/fase-g-env-promote-dedb344/).
- [Promoción completada](evidencias/fase-g-promocion-dedb344-01/result.json).
- [Activación comercial](evidencias/fase-g-activar-comercial-dedb344/).
- [Doctor final sin deriva](evidencias/fase-g-doctor-final-dedb344/).
- [Cierre real](evidencias/fase-g-cierre-dedb344/cierre.json): API, Terraform Worker, Lifecycle Worker, worker, Traefik, PostgreSQL y Redis en ejecución; API, DB y Redis healthy. Los workers se verificaron además por sus comprobaciones operativas, no por un healthcheck Docker inexistente.

Cero operaciones no terminales, cero mensajes no confirmados y colas vacías al cierre. No se alteró Ceph, RBAC, Jira, producción ni las Golden Images. Terraform conserva Administrator en LAB por aceptación expresa; no equivale a mínimos privilegios certificados.

## Resultado E2E real y secuencial

| Comprobación | KVM 1 | KVM 2 |
|---|---|---|
| Instance ID | `b50a71f8-210c-44da-b5ca-f73b7ce00ee9` | `e1cc735c-e269-4423-a222-77f70f09c05a` |
| VMID desechable | 30023 | 30025 |
| Imagen / nodo | Ubuntu 24.04, clon de 29000 / mng01 | Ubuntu 24.04, clon de 29000 / mng01 |
| CPU / RAM / disco virtual | 1 / 2048 MiB / 50 GiB | 2 / 4096 MiB / 100 GiB |
| Admisión y replay idéntico | HTTP 202; mismos identificadores | HTTP 202; mismos identificadores |
| DHCP, Guest Agent, Cloud-Init | PASS | PASS |
| Root por contraseña PAM y SSH con llave | PASS / PASS | PASS / PASS |
| Root SSH por contraseña | Deshabilitado | Deshabilitado |
| Shutdown, start, reboot | PASS final; timeout ACPI inicial conservado | PASS |
| DELETE y conciliación | PASS; ausente en Proxmox, state sin recurso, lock ausente | PASS; ausente en Proxmox, state sin recurso, lock ausente |

KVM 1: [recursos y acceso](evidencias/fase-g-e2e-comercial-dedb344-final/kvm-1-proxmox-guest-state.json), [continuación y resultado](evidencias/fase-g-e2e-kvm1-continuacion-dedb344-02/resultado.json), [limpieza](evidencias/fase-g-e2e-kvm1-continuacion-dedb344-02/limpieza.json).

KVM 2: [recursos, filesystem y acceso](evidencias/fase-g-e2e-kvm2-dedb344-geometria-final/kvm-2-proxmox-guest-state.json), [resultado](evidencias/fase-g-e2e-kvm2-dedb344-geometria-final/resultado.json), [limpieza](evidencias/fase-g-e2e-kvm2-dedb344-geometria-final/kvm-2-limpieza.json).

La geometría KVM 2 confirma disco de **107.374.182.400 bytes**, partición raíz de **106.299.375.104 bytes** y filesystem bruto de **106.299.371.520 bytes**: diferencia de 3.584 bytes, menor que un bloque de 4.096 bytes. El espacio utilizable de datos es menor por particiones de arranque y metadatos; no debe compararse directamente con 100 GiB. KVM 1 reportó 50.884.108.288 bytes utilizables. No se redujeron discos ni se modificaron manualmente los filesystem para aprobar.

[limpieza-global.json](limpieza-global.json) verifica ausencia de las cuatro VMs de ensayo y sus volúmenes en DS_CYBERFUEL. Las plantillas 29000, 29001, 29010 y 29011 permanecen como template, sin locks. La VM 106 no se utilizó ni constituyó un bloqueo. Se mantuvo **una sola VM comercial de ensayo activa a la vez**.

### Incidencias conservadas y recuperación

1. El primer intento 30022 falló antes de clonar por `SOURCE_UNAVAILABLE`: el UID 10001 no podía leer `.git/config` del repositorio IaC fijado. Tras comprobar que no contenía credenciales ni comandos de transporte privilegiados, se ajustó exclusivamente su grupo a 10001 y modo a 0640; contenido y SHA del archivo fuente inalterados. El reintento utilizó la misma instancia. Una observación Proxmox transitoria requirió reconciliación por lectura, sin segundo apply. Esta VM también fue eliminada y conciliada.
2. El cliente inicial interpretaba el `status` histórico en lugar de `normalized_status`; se corrigió exclusivamente el cliente de certificación.
3. KVM 1 tuvo `POWER_ACPI_TIMEOUT` al reiniciar inmediatamente después de start. Se preservó la operación fallida, se observó red/Guest Agent/SSH y se emitió una nueva intención explícita de reboot sobre la misma VM. Finalizó correctamente; después se apagó y eliminó. No se ocultó el fallo ni se recreó la VM para eludirlo.
4. El primer ensayo KVM 2 (30024) falló en una comparación errónea del cliente entre espacio utilizable y disco nominal. La medición de partición y filesystem bruto confirmó expansión correcta. Se corrigió el criterio geométrico, se eliminó 30024 por API y el ensayo completo posterior 30025 pasó. [Limpieza 30024](evidencias/fase-g-limpieza-kvm2-30024/).
5. Cloud-Init retornó 2 con `errors=[]` y advertencias exclusivamente `DEPRECATED` sobre `user` string, anunciadas para eliminación en Cloud-Init 27.2. Se registró como advertencia técnica, sin modificar las imágenes.

## Bootstrap G6 y confidencialidad

La API cifró con AES-256-GCM; Redis almacenó ciphertext con TTL de 900 s y la cola referencias opacas. La clave permaneció separada, mediante secretos existentes. Terraform no recibió la contraseña. El worker utilizó su llave técnica y verificó la clave del host invitado obtenida por el canal Proxmox autorizado; la contraseña se entregó por stdin, no por argumentos. Se comprobó autenticación PAM de root sin habilitar root SSH por contraseña.

Las pruebas físicas buscaron la contraseña señuelo en PostgreSQL, Redis y artefactos/state y no la encontraron. KVM 2 completó también el barrido de logs del recorrido completo. **No se declara ese barrido físico final de logs para KVM 1**, cuyo cliente se interrumpió tras el primer reboot y no persistió la contraseña. La suite cubre además filtración, expiración y pérdida del secreto; estas últimas no se inyectaron físicamente en el runtime compartido.

Las referencias productivas quedaron eliminadas. El fixture cifrado temporal independiente del cliente KVM 2 también se eliminó y verificó; su referencia privada no se exporta a Git. Redis conserva AOF `everysec` y snapshots existentes: el TTL elimina la entrada lógica, no garantiza borrado físico inmediato del ciphertext en históricos. No se purgaron datos sensibles históricos ni se rotaron credenciales.

## Catálogo, autorización y consumidores

- Productos fijos: kvm-1 (1/2048/50), kvm-2 (2/4096/100), kvm-3 (4/8192/200), kvm-4 (8/16384/400). Sin overrides comerciales libres.
- Tenant de laboratorio con derecho explícito a kvm-1/2: máximo 2 instancias contando la histórica existente, 4 vCPU, 6144 MiB, 120 GiB acumulados y concurrencia 1. No habilita ejecución LAB de kvm-3/4.
- Ubuntu 24.04 habilitada; Ubuntu 26.04, AlmaLinux 9 y AlmaLinux 10 registradas pero pendientes y no seleccionables. Registro no equivale a certificación.
- `lab-dhcp` habilitado; WAN estática permanece bloqueada.
- Se preservaron propiedad, permisos por operación, idempotencia, locks y barrera fail-closed sobre dependencias reales. `capacity-ro` sigue separado de Terraform.
- Los ensayos usaron la identidad operativa existente y su tenant LAB con Bearer opaco. No prueban un CRM externo inexistente ni convierten cualquier Bearer en derecho ilimitado.
- Swagger/manual y el nuevo cliente secuencial usan el contrato comercial. Los consumidores históricos que aún envíen CPU/RAM/disco o política UUID deben migrar: no hay compatibilidad silenciosa y no se afirma que todo script histórico sea comercialmente compatible. Se preservan sus identificadores y trazabilidad.

## Swagger HTTPS y evidencias visuales

Playwright Chromium accedió realmente a `https://iac.cyberfuel.com/docs` en escritorio y móvil, abrió la guía local y comprobó contenido de planes, contraseña e idempotencia. Recursos JS/CSS locales HTTP 200, cero errores JavaScript, cero solicitudes externas y cero mutaciones de infraestructura en esta prueba. Se usó la autorización de automatización existente; no se presenta como prueba nueva de HTTP Basic interactivo ni como una nueva batería completa de historial/posicionamiento.

- [Resultado automatizado](playwright-https.json).
- [Captura escritorio](swagger-https-escritorio.png).
- [Captura móvil](swagger-https-movil.png).

La documentación conserva avisos conservadores de certificación pendiente del catálogo completo. Este informe certifica físicamente únicamente kvm-1/2 con Ubuntu 24.04. No se abrió un nuevo frente visual para cambiar esos avisos.

## Operación, repetición y reversión

Los siguientes comandos se ejecutan **en el IACP Control Plane (VM 104)** por personal autorizado. Los manifiestos y cambios aprobados contienen referencias privadas, no valores sensibles. No son instrucciones para ejecutar contra nodos MNG.

```bash
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/iacpctl \
  --env-file /etc/iacp/server.env \
  --compose-file /opt/iacp/releases/iacp-fase-g-dedb344/deploy/mng/compose.yaml \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/NUEVA-EVIDENCIA doctor
```

Secuencia utilizada para la promoción (registro, **no volver a ejecutar automáticamente**):

```bash
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/go_live.py preflight \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/fase-g-preflight-dedb344-02
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/iacpctl \
  --env-file /etc/iacp/server.env \
  --compose-file /opt/iacp/releases/iacp-fase-g-dedb344/deploy/mng/compose.yaml \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/fase-g-env-promote-dedb344 \
  env-promote --candidate /etc/iacp/changes/server.env.fase-g-dedb344.candidate
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/go_live.py promote \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/fase-g-promocion-dedb344-01
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/go_live.py activate-lab \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/fase-g-activar-comercial-dedb344
```

La promoción ya realizada utilizó `env-promote`, `go_live.py` y el manifiesto revisado. Una repetición requiere nueva evidencia, autorización vigente del cambio, operaciones/colas sin actividad, backup comprobado, fuente e imágenes fijadas y configuración de cifrado disponible. No repetir promoción ni migraciones únicamente para volver a generar un PASS.

Backup PostgreSQL 0012 previo a los E2E: `/opt/iacp/backups/iacp-fase-g-0012-20261025T113206CR.dump`, SHA256 `ea1c5b872ca7b1bd497e36029553633a64f0860d38954ca2a662aacddccb551e`. Se restauró en PostgreSQL 17 descartable aislado, sin red, y se comprobó esquema y tablas. [Evidencia de restauración](evidencias/fase-g-respaldo-0012-20261025T113206CR/restauracion.json). El dump no se publica. Backup de entorno: `/opt/iacp/backups/config/server.env.20261025T172315Z` (**25/10/2026 11:23:15**, Costa Rica).

Reversión funcional documentada, **no ejecutada**, para cerrar admisión/Lifecycle sin borrar datos ni state:

```bash
sudo python3 /opt/iacp/releases/iacp-fase-g-dedb344/scripts/iacpctl \
  --env-file /etc/iacp/server.env \
  --compose-file /opt/iacp/releases/iacp-fase-g-dedb344/deploy/mng/compose.yaml \
  --manifest /etc/iacp/changes/manifest-fase-g-dedb344.preparado.json \
  --evidence-dir /opt/iacp/evidence/NUEVA-REVERSION rollback
```

Conservar esquema 0012 y datos. No ejecutar downgrade Alembic ni volver ciegamente a 8a58db2 sobre este esquema. Una restauración de datos exige detener nuevas intenciones, conciliar trabajos y revisar efectos reales ya efectuados; rollback del Control Plane no deshace una VM ni su state remoto. Nunca `down -v`, borrar state o forzar unlock para aprobar.

El cliente [`scripts/e2e-commercial-sequential.py`](../../../../scripts/e2e-commercial-sequential.py) requiere `--execute`, comprueba el hostname y no permite comenzar con otra intención comercial viva. Ejecutarlo únicamente con autorización y presupuesto; `--plan kvm-1` o `--plan kvm-2` selecciona un recorrido. Sin `--execute` no crea recursos. Si falla, conservar UUID/evidencia y recuperar o eliminar mediante la API; no lanzar otro recorrido dejando la VM anterior huérfana.

## Matriz de cierre y límites

| Estado | Resultado |
|---|---|
| COMPLETADO | Correctivo integrado PR #72; 1.062 pruebas; promoción y activación; backup restaurado; evidencias y cliente reproducible |
| VERIFICADO EN RUNTIME | Código dedb344; siete servicios operativos; contrato comercial; E2E KVM 1/2 completos; acceso y expansión de disco; eliminación/state/locks; Swagger HTTPS |
| PENDIENTE | Certificación física de KVM 3/4 y Ubuntu 26.04/AlmaLinux 9/10; WAN; consumidor CRM externo; reducción futura de privilegios LAB |
| NO-GO independiente | Resiliencia integral y fallo físico aislado de worker |

Para la presentación del **27/10/2026**, usar Ubuntu 24.04, un plan LAB autorizado a la vez, consultar disponibilidad/cuota antes de crear y esperar red/Cloud-Init/SSH antes de reboot. Mantener demo de rendimiento separada a 5 RPS de lectura; la evidencia histórica de 10 RPS/30 minutos no se repitió ni se modifica en este cierre.

Fuera de alcance: Ceph, producción, Jira, OCI, normalización de Compose/directorios, centralización y rotación planificada de credenciales, GC Wasabi, HA, IaCoC, LXD y CRM Simulator. Ninguno se presenta como terminado.
