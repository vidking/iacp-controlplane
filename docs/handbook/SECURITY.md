# Permisos, secretos y aislamiento

La identidad es un token Bearer de servicio configurado fuera de Git. El registro
contiene digest SHA-256, tenant UUID, subject y role. La API valida el digest en
tiempo constante, rechaza registros inválidos o duplicados y exige tenant activo.
El token bootstrap queda ligado exclusivamente al tenant bootstrap y al rol
operator; no puede convertirse en approver mediante remapeo.

| Acción | reader | operator | approver | Infraestructura fuera de API |
|---|---|---|---|---|
| Consultar inventario, jobs y operaciones propios | Sí | Sí | Sí | Según identidad |
| Consultar planes y ejecuciones administrativos | Solo con administration | Solo con administration | Sí | Según identidad |
| Crear instancia / solicitar plan | No | Sí | No | No necesita acceso directo a DB |
| Cancelar/reintentar plan | No | Solo con administration | No | Inspección de incidentes |
| Aprobar hash e intento | No | No | Sí, sujeto independiente | Revisar binario y asignación |
| Solicitar ejecución autorizada | No | Con administration y distinto del aprobador | No | Habilitar entorno después de aprobación |
| Cancelar/recuperar ejecución administrativa | No | Solo con administration | No | Conciliar sin duplicar recursos |
| Crear tenant/emitir o rotar credencial | No | No | No | Administración explícita y registro externo |
| Seleccionar VMID, plantilla, redes y backend | No | No | No | Asignación autorizada externa |
| Eliminar KVM descartable | No | Sí, con plan aprobado | Solo aprobar plan, otro sujeto | Feature OFF, allowlist y propiedad; VM 104/106 protegidas |
| Modificar VM 104 | No | No | No | Fuera del alcance de este despliegue |

La aprobación debe ser de un sujeto distinto del creador; quien solicita apply
también debe ser distinto del aprobador. No basta con emitir dos tokens para el
mismo subject. El approver puede leer objetos de su tenant para revisarlos, pero
no puede crear instancias ni solicitar ejecución.

El registro de credenciales se relee por petición. Rotar tokens mediante entrega
segura y reemplazo del archivo externo; retirar el anterior después de migrar el
consumidor. Retirar un token no cancela automáticamente autorizaciones ya emitidas:
revocarlas explícitamente, deshabilitar apply y considerar su caducidad. Un tenant
inactivo detiene nuevas peticiones y la ejecución en curso al siguiente control.

Aislamiento multi-tenant:

- Los filtros de API utilizan el tenant del Principal autenticado; otro tenant
  recibe 404 en objetos, jobs y operaciones; un administrador autorizado recibe
  404 al consultar una ejecución ajena. CRM recibe 403 en controles administrativos.
- Restricciones únicas por tenant protegen nombres e idempotencia; reservas VMID
  y claves state evitan colisiones entre instancias.
- Un job histórico sin tenant solo es visible para bootstrap. Los jobs nuevos
  siempre llevan tenant; los jobs apply se vinculan al run y a sus eventos.
- Terraform no acepta parámetros arbitrarios del cliente; solo asignaciones lab
  validadas y congeladas. No puede apuntar al VMID o plantilla 104.

PostgreSQL comparte tablas entre tenants; no existe RLS ni una base por tenant.
Los administradores de PostgreSQL, Docker, Wasabi y Proxmox siguen siendo
privilegiados. La autenticación actual usa tokens estáticos; OIDC, credenciales
con expiración automática, rate limiting de API, cuotas y RBAC granular están
pendientes y deben evaluarse antes de ampliar el servicio.

Secretos: separar por entorno y función el token de servicio, digest registry,
contraseña DB, token Proxmox, credenciales Wasabi y claves TLS. No construir
imágenes con secretos ni pasar archivos de producción al entorno de pruebas.
El worker Terraform no recibe el token de la API; solo la API monta el registro
de tenants. Usar permisos de filesystem mínimos y respaldos cifrados autorizados.

La API y el código se ejecutan como UID 10001; las fuentes pertenecen a root y
son legibles, no escribibles. El worker Terraform utiliza filesystem readonly,
cap_drop ALL, no-new-privileges, tmpfs y volumen privado de planes. Los procesos
hijos tienen argumentos fijos, entorno filtrado y límites de tiempo/salida.
Un hard kill del worker sigue siendo una ejecución incierta hasta inspección.

TLS exige Host/SNI oficial y fuentes IP autorizadas; CORS solo controla Origin
cuando existe y no reemplaza autenticación ni firewall. Restringir también la
salida del worker a los endpoints autorizados, DNS y descarga de providers
necesarios. La red Docker egress por sí sola no implementa una allowlist externa.

La auditoría guarda eventos con actor, código, estado y fecha, junto con IDs de
instancia, operación, job y autorización. X-Request-ID identifica la petición HTTP.
No hay todavía un ledger inmutable externo ni exportación SIEM automática. El
provider puede producir diagnósticos sensibles: no devolver stdout/state completo
al CRM. Los errores persistidos son códigos controlados, no credenciales o trazas.

Desde IACP 0.10.1, operator/reader de consumidores no acceden a controles
administrativos. Los operadores y lectores administrativos necesitan el atributo
servidor administration=true; approver conserva revisión independiente. Bootstrap
es de plataforma y nunca se entrega como identidad CRM. Los detalles y rutas de
documentación separadas están en [el contrato administrativo](../PROVISIONING_CONTRACT.md).
