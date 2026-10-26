# Identidades de integración LAB — inventario y procedimiento propuesto

Fecha: **25/10/2026, America/Costa_Rica**. Auditoría de solo lectura después de estabilizar el release `54d418119200710ee00eb7a9d406b9322fe8e40c` en el IACP Control Plane (VM 104). **No se crearon identidades, roles, tokens o tenants ni se modificó el registro.** Documento operativo para revisión; no incorporar estos procedimientos privilegiados a la guía web de Desarrollo.

## Conclusión

**Reutilizar `operator` con `administration=false`. No hace falta un rol nuevo ni modificar el núcleo RBAC.** Cubre catálogo, redes autorizadas, imágenes, creación, consultas, Lifecycle, eliminación comercial, jobs, observabilidad y reconciliación observacional, sujeto a tenant, propiedad, cuota y barreras existentes.

Las dos identidades propuestas son independientes por credencial y sujeto de auditoría:

| Estado | subject propuesto | role | administration | tenant_id persistente |
|---|---|---|---|---|
| NO CREADA | `lab-swagger-postman` | `operator` | `false` | `00000000-0000-0000-0000-000000000001` |
| NO CREADA | `lab-operation-center` | `operator` | `false` | `00000000-0000-0000-0000-000000000001` |

El tenant existente tiene slug `bootstrap`, nombre `Bootstrap` y está activo. Su nombre histórico **no convierte una nueva credencial en administrador**: ese atributo depende de la identidad/rol. La credencial especial `bootstrap` de plataforma sí es administrativa y **no se distribuirá** a Swagger, Postman ni Operation Center. No se propone renombrar ni crear otro tenant.

## Inventario real

Fuente: [identidades-metadata.json](identidades-metadata.json), sin valores ni hashes de tokens.

| Identidad actual | Rol exacto | administration registrada | Ámbito |
|---|---|---|---|
| `bootstrap`, mecanismo separado del registro | `operator` | `true` | Operador de plataforma, tenant LAB existente |
| `iacp-lab-approver` | `approver` | `false` | Tenant LAB; aprobaciones técnicas |
| `iacp-docs-operator` | `docs` | `false` | Acceso documental, tenant LAB |
| `self-service-isolation-reader` | `reader` | `false` | Tenant de prueba de aislamiento `0294373d-7075-4eb5-9dca-9ff256298828`; sin presupuesto comercial |

El registro contiene **tres entradas** y hashes únicos; no contiene un operador de integración no administrativo. Registro efectivo: `/etc/iacp/identity/tenant_credentials.json`, UID 0, GID 10001, modo 0440, montado como archivo en la API. No se asume vigente ninguna ruta histórica de operator.token.

**Advertencia de semántica real:** `approver` supera `authorize_administration` aunque su campo administration sea false. No usar esa identidad para el CRM. El [sondeo de funciones RBAC del runtime](matriz-funciones-rbac.json) confirma esta distinción; no autentica ni registra identidades nuevas.

## Matriz de permisos

La matriz considera administration=false. Toda lectura de recursos está limitada al tenant, y una lectura de catálogo puede informar disponibilidad=false.

| Capacidad / rutas existentes | operator | reader | approver | docs |
|---|---|---|---|---|
| GET /v1/plans, /v1/images, /v1/profiles | Sí | Sí | Sí | No API |
| GET /v1/instances y /v1/instances/{instance_id} | Sí | Sí | Sí | No API |
| GET /v1/operations, /v1/operations/{operation_id}, /v1/jobs/{job_id} | Sí | Sí | Sí | No API |
| GET /v1/events, /v1/observability | Sí | Sí | Sí | No API |
| GET /v1/lab/usage y políticas del tenant | Sí | Sí | Sí | No API |
| GET /v1/instances/{instance_id}/power | Sí | Sí | Sí | No API |
| POST /v1/instances comercial | Sí, condicionado | No | No | No API |
| POST .../network/discover, .../network/initialize, .../access/verify | Sí, condicionado | No | No | No API |
| POST .../power/start, shutdown, reboot | Sí, condicionado | No | No | No API |
| DELETE comercial .../instances/{instance_id} con {} | Sí, condicionado | No | No | No API |
| POST .../reconcile y recuperación LAB permitida | Sí, con fences/estado válidos | No | No | No API |
| POST /v1/jobs/smoke | Sí, trabajo de comprobación limitado | No | No | No API |
| Aprobar políticas/planes administrativos | No | No | Sí, con separación de funciones | No |
| Preparar planes administrativos que además exigen operator | No | No | No: no cumple authorize_write | No |
| HTTP Basic documental | Credencial documental separada cuando existe registro docs | Igual | Igual | Sí, sujeto/contraseña documental |

No existe una API para administrar secretos o emitir tokens, ni una ruta de Ceph. Las operaciones de configuración del host siguen requiriendo acceso operativo autorizado; el rol de API no otorga SSH, acceso al Docker socket ni credenciales Proxmox. `administration=true` no se asigna a ninguna identidad propuesta.

Fuentes: `security.py`, `api/commercial.py`, `api/catalog.py`, `api/instances.py`, `api/power.py`, `api/deletion.py`, `api/lab.py`, `api/jobs.py`, `api/operations.py`, `api/observability.py`, `api/terraform.py` y `api/applies.py` del código ejecutado. Las rutas administrativas tienen sus guardas adicionales; no confundir la eliminación comercial delegada con la aprobación manual de un plan histórico.

## Relación entre identidad, tenant y políticas

El consumidor envía Authorization Bearer. IACP compara el SHA256 del token opaco con el registro, obtiene subject, tenant_id, role y administration y exige que el tenant exista y esté activo. El registro se carga por solicitud; no hay JWT, sesión de renovación ni tenant aleatorio por petición.

La creación explícita de tenants existe mediante `python -m iacp.admin create-tenant --slug ... --name ...`, por el operador. Ese comando genera un UUID al dar de alta el tenant, **no durante cada creación KVM**. No se debe ejecutar para estas dos identidades: ya hay un tenant apropiado y persistente.

Un rol operator por sí solo no autoriza infraestructura. Se siguen comprobando:

- HTTPS para acceso root; identidad y permiso de escritura; tenant activo.
- Plan permitido, imagen habilitada/certificada y perfil autorizado al tenant.
- Presupuesto comercial, capacidad real, reservas y concurrencia.
- Red/destino/datastore definidos por el servidor; protección de recursos.
- Idempotencia, locks, propiedad, snapshot de autorización y barreras del worker.
- Bootstrap G6 cifrado/efímero, trazabilidad y reconciliación.

El perfil efectivo es lab-dhcp, Ubuntu 24.04, entorno lab, nodo actual mng01, almacenamiento DS_CYBERFUEL, vmbr0 sin tag. La configuración allowed_nodes enumera mng01/mng02; no Platinum/PRX. No se habilita WAN ni otra imagen por asignar un token.

Derecho comercial LAB actual: KVM 1/2; máximo 2 instancias, 4 vCPU, 6144 MiB, 120 GiB y concurrencia 1. Uso observado: 1 instancia, 2 vCPU, 2048 MiB y 20 GiB. Las dos identidades **comparten recursos, cuota y espacio de Idempotency-Key del tenant**. No tendrán dos cupos independientes ni aislamiento por subject. Usar prefijos de intención distintos, por ejemplo `swp:...` y `oc:...`, y nombres de VM únicos. La atribución de nuevas intenciones conserva el subject de cada cliente.

Hay seis políticas temporales históricas: dos aprobadas no vencidas (hasta el 30/10/2026), tres con estado registrado approved pero vencidas y una revoked. Sus fechas se detallan en el inventario en Costa Rica. No renovarlas ni seleccionar sus UUID para el nuevo contrato: la creación comercial usa el derecho/presupuesto vigente y delegación auditada, sin aprobación humana por cada VM.

## Procedimiento futuro de alta — NO EJECUTADO

No hay un subcomando genérico existente para instalar API tokens. `iacpctl docs-rotate` genera credenciales de rol docs y **no sirve** para integrar la API. `iacpctl docs-install` exige que las entradas API permanezcan idénticas; no utilizarlo ni modificar su validador para introducir estos operadores.

La configuración reconocida por el core ya es el registro JSON de credenciales. Se propone una actualización manual controlada de ese secreto, con el mismo esquema, preservando todas las entradas existentes y sin tocar RBAC. Requiere aprobación del responsable después de revisar esta propuesta.

1. Registrar el cambio autorizado para ambos sujetos y tenant, comprobar salud, trabajo pendiente y configuración efectiva. Capturar respaldo privado del registro y del entorno; conservar propietarios/modos actuales. No ampliar derechos del tenant.
2. En VM 104, preparar un directorio privado nuevo fuera de releases/Git. Generar dos valores opacos distintos con un CSPRNG, al menos 32 caracteres; por ejemplo secrets.token_urlsafe(48). Guardar el plaintext únicamente en archivos de entrega privados, nunca stdout/logs. Añadir a una copia candidata del registro estas entradas **computadas localmente**, sin escribir todavía el archivo activo:

```python
# Solo tras autorización; esquema existente, no tokens o hashes publicados.
entry = {
    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "subject": subject,  # lab-swagger-postman o lab-operation-center
    "role": "operator",
    "administration": False
}
```

3. Validar privadamente la copia con `iacp.security.Credential` del contenedor ejecutado: hashes únicos, dos sujetos nuevos distintos, mismo tenant activo, roles operator/administration=false, entradas previas intactas y ningún hash igual al bootstrap. No añadir expires_at/revoked: el modelo actual rechaza campos desconocidos y un registro inválido produce 503 fail-closed. No imprimir errores que incluyan el candidato o sus hashes. Revalidar permisos de perfil y presupuesto con limits_for/authorized_profile en lectura.
4. Excluir escritores y drenar admisión mediante el procedimiento aprobado; verificar colas/operaciones sin actividad. Bajo el bloqueo de promoción existente, instalar el candidato de forma atómica en la **misma ruta**, preservando UID 0, GID 10001 y modo 0440. Mantener la copia anterior privada para reversión.
5. El bind mount es de archivo: una sustitución atómica deja el inode anterior en el contenedor. Recrear **solo la API**, sin build ni pull, con el Compose/env canónicos y la imagen ya revisada. Es el mecanismo de remontaje utilizado por docs-install, pero la instalación de entradas API es manual y requiere su autorización propia:

```bash
# Referencias descubiertas y revisadas de la intervención futura; no ejecutar ahora.
sudo docker compose --env-file /etc/iacp/server.env \
  -p iacp-controlplane \
  -f /opt/iacp/releases/iacp-piloto-54d4181/deploy/mng/compose.yaml \
  up -d --no-deps --no-build --pull never --force-recreate api
```

6. Antes de reabrir, comprobar que la API ve el registro nuevo, los roles/sujetos esperados y el tenant correcto, sin mostrar hashes. Verificar 401 sin token, catálogos/consulta/observabilidad con cada nueva credencial y 403 en administración. Confirmar que la credencial revocada de prueba, si se realiza una rotación autorizada, ya no autentica. No ejecutar una creación válida para probar permisos durante esta intervención sin autorización separada de ensayo.
7. Restablecer la activación comercial mediante iacpctl/go_live.py y revisión vigente del tenant, con sus mismos límites. Ejecutar doctor y confirmar colas/locks. Si falla la autenticación, restaurar el registro previo y remontar la API; no deshabilitar autenticación ni rebajar permisos de archivos para conseguir acceso.
8. Entregar cada plaintext por el canal privado autorizado al custodio correspondiente. Swagger/Postman: variable local protegida y Authorize; no incluir en colecciones, sincronización compartida ni capturas. Operation Center: secreto del backend/IACP_API_TOKEN, nunca JavaScript del navegador. Comprobar recepción antes de retirar las copias temporales de entrega. No copiar hashes a Git, tickets o evidencia pública.

Este es un procedimiento administrativo propuesto, no un nuevo comando implementado ni una instalación certificada. La comprobación completa de escritura real se realizará mediante el piloto autorizado posterior; la matriz de permisos y pruebas aisladas ya cubre las guardas sin crear recursos.

## Rotación, revocación y alcance

- **Rotar:** preparar nuevo token/digest para el mismo subject, tenant y rol, validar copia e instalar con el procedimiento anterior. El core permite dos hashes distintos para un mismo subject de API durante una transición breve. Migrar el consumidor por el canal privado y retirar después el digest anterior. No reasignar tenant ni ampliar administration para rotar.
- **Revocar una identidad:** retirar sus entradas de hash, instalar y remontar la API; otras entradas permanecen intactas. No desactivar el tenant compartido: afectaría a ambas integraciones y a las identidades existentes asociadas a él.
- El core actual **no implementa vencimiento temporal de API tokens**. La rotación planificada se gestiona operativamente; no se instaló un scheduler. El TTL G6 de contraseñas bootstrap no es un TTL de estas credenciales.
- Revocar un token bloquea solicitudes posteriores; **no cancela automáticamente trabajos ya admitidos**, no borra auditoría y no elimina recursos. Evaluar esas intenciones y conservar su recuperación/reconciliación.
- La eliminación autorizada de la credencial de un cliente no exige crear otro tenant ni modificar políticas por VM. Ambas identidades podrán operar recursos del mismo tenant dentro de los controles; eso es una decisión explícita de este laboratorio.

**Pendiente de aprobación:** alta y entrega de las dos credenciales. No existe ningún nuevo token para publicar o recuperar de esta auditoría.
