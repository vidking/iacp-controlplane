"""Anotaciones documentales; no cambia schemas, handlers ni requisitos HTTP."""
DESCRIPTION = (
    'API corporativa de IACP para integrar el ciclo de vida de instancias KVM con un CRM. '
    'Selecciona perfiles autorizados, conserva las intenciones idempotentes y sigue cada operación hasta su resultado.\n\n'
    '**Autenticación:** HTTP Basic permite consultar la documentación. Las llamadas a `/v1` requieren '
    'un token Bearer del tenant y el rol correspondiente. Introduce solo el token en **Authorize**; '
    'la contraseña de documentación no autoriza la API.\n\n'
    '**Flujo recomendado:** perfiles → creación → seguimiento por `operation_id` → acceso/Lifecycle → eliminación autorizada. '
    'Un 202 confirma admisión; no confirma disponibilidad ni eliminación. Conserva `Idempotency-Key`, UUID y cuerpo en reintentos. '
    'Ante `uncertain`, conserva la misma instancia y utiliza la reconciliación autorizada.\n\n'
    '**Alcance:** API privada por LAN/VPN, recursos propios del tenant, perfiles y cuotas vigentes. '
    'Los ejemplos son sintéticos y no constituyen autorizaciones. Las herramientas de diagnóstico y laboratorio '
    'se separan del recorrido CRM; las operaciones administrativas requieren su ámbito específico.'
    '\n\n**Precondiciones:** los perfiles DHCP y las cuotas proceden de la autorización vigente. '
    'Usa `power/shutdown` para apagar ordenadamente. DELETE requiere política o allowlist y autorización; '
    'VM 104, VM 106 y Golden Image 29000 permanecen protegidas. La admisión cerrada devuelve 503.'
)
TAGS = [
    {'name': 'instances', 'description': '01 · Perfiles, creación y consulta de instancias propias del tenant.'},
    {'name': 'operations', 'description': '02 · Seguimiento de intenciones asíncronas, estados terminales y eventos.'},
    {'name': 'kvm-lifecycle', 'description': '03 · Estado de energía, inicio, reinicio y apagado ordenado de KVM autorizadas.'},
    {'name': 'disposable-deletion', 'description': '04 · Eliminación autorizada de instancias descartables; recursos protegidos excluidos.'},
    {'name': 'observability', 'description': 'Diagnóstico · Eventos y estado del tenant; no sustituye la supervisión de infraestructura.'},
    {'name': 'health', 'description': 'Diagnóstico · Disponibilidad de la API y sus dependencias.'},
    {'name': 'jobs', 'description': 'Laboratorio · Comprobación técnica de cola y consulta de trabajos propios.'},
    {'name': 'lab-administration', 'description': 'Laboratorio · Consulta y administración autorizada de políticas y cuotas.'},
    {'name': 'provisioning-administration', 'description': 'Administración · Inspección y recuperación de aprovisionamiento.'},
    {'name': 'provisioning-approvals', 'description': 'Administración · Revisión independiente de autorizaciones de un solo uso.'},
]

OPERATIONS = {
    'listAuthorizedProfiles': ('Consultar el catálogo de perfiles', 'Devuelve imágenes, destinos y límites autorizados para el tenant. Selecciona un perfil y una política aprobada antes de solicitar recursos; el catálogo no amplía cuotas.'),
    'createInstance': ('Crear una instancia KVM', 'Admite una intención idempotente bajo un perfil y una política vigentes. Conserva los tres identificadores de respuesta. Un 202 no significa que la VM ni su acceso estén disponibles. Repite la misma clave y cuerpo si no recibes confirmación.'),
    'listInstances': ('Consultar las instancias del tenant', 'Lista paginada y estable del inventario propio. Utiliza limit, offset y los filtros disponibles. X-Total-Count indica el total del filtro; deleted conserva el historial de una instancia eliminada.'),
    'getInstance': ('Consultar una instancia', 'Obtiene el estado persistido del UUID propio, sus recursos y las observaciones de red/acceso. Una instancia ajena o no visible devuelve 404.'),
    'listOperations': ('Consultar el historial de operaciones', 'Lista paginada de intenciones del tenant. Filtra por instance_id para seguir creación, Lifecycle y eliminación. Consulta normalized_status y, para arranque automático, fulfillment.'),
    'getOperation': ('Seguir una operación por su UUID', 'Consulta la misma operación hasta completed, failed o uncertain. Revisa error_code y recovery_required antes de continuar. Un estado uncertain exige observación/reconciliación autorizada sobre la misma instancia.'),
    'getInstancePower': ('Consultar el estado de energía', 'Observa la KVM propia y devuelve las transiciones disponibles. Running solo describe energía; no demuestra SSH, Cloud-Init, Guest Agent ni disponibilidad de aplicaciones.'),
    'startInstancePower': ('Iniciar una instancia autorizada', 'Solicita start con una intención idempotente. Requiere identidad, propiedad, perfil/política y habilitación vigentes. Sigue operation_id hasta el resultado terminal; no garantiza disponibilidad del invitado.'),
    'rebootInstancePower': ('Reiniciar una instancia autorizada', 'Solicita un reinicio de una KVM running con autorización vigente. Sigue la operación: observar running no basta para demostrar que el reinicio ocurrió.'),
    'shutdownInstancePower': ('Apagar una instancia ordenadamente', 'Solicita shutdown mediante ACPI, sin apagado forzado de respaldo. Sigue operation_id hasta confirmar stopped antes de una eliminación autorizada.'),
    'deleteDisposableInstance': ('Eliminar una instancia descartable autorizada', 'Para una instancia nueva bajo una política vigente, envía {} y una nueva Idempotency-Key. Requiere VM apagada, autorización y ausencia de locks. Sigue la operación hasta confirmar eliminación; conserva UUID e historial. Los recursos protegidos permanecen excluidos. El contrato de aprobación independiente existente sigue disponible para operadores autorizados.'),
    'discoverInstanceNetwork': ('Observar la red del invitado', 'Solicita una observación asíncrona con identidad y MAC verificadas. Una IP pendiente no equivale a fallo de creación. Conserva operation_id y espera la observación; no cambia el direccionamiento autorizado.'),
    'initializeInstanceNetwork': ('Completar la inicialización autorizada', 'Inicialización fija y de un solo uso para una clon nueva bajo perfil/política compatibles. No admite comandos o archivos del cliente. Start la solicita automáticamente; esta ruta conserva la intención ante una entrega no confirmada.'),
    'verifyInstanceAccess': ('Verificar el acceso inicial', 'Solicita la verificación autorizada del acceso del invitado. Conserva la operación y consulta el resultado sin enviar de nuevo una creación. No devuelve contraseñas.'),
    'resumeLabProvisioning': ('Continuar la misma solicitud de aprovisionamiento', 'Conserva UUID, intención y autorización. Una mutación ya consumida solo se observa/reconcilia; no crea otra VM. No utiliza una clave nueva para sustituir una ejecución incierta.'),
    'retryLabProvisioning': ('Reintentar una planificación no consumida', 'Recupera únicamente una planificación fallida antes de consumir la autorización de ejecución. expected_attempt e Idempotency-Key protegen frente a reintentos concurrentes. No sustituye una reconciliación de infraestructura incierta.'),
    'reconcileInstance': ('Reconciliar el estado por observación', 'Observa la misma instancia para resolver un resultado incierto bajo los controles existentes. No autoriza una segunda creación ni una nueva mutación de infraestructura.'),
    'healthLive': ('Comprobar que la API responde', 'Comprobación de vida del proceso; no certifica dependencias ni crea infraestructura.'),
    'healthReady': ('Comprobar disponibilidad de dependencias', 'Verifica base de datos, esquema y cola. Un 503 indica que la plataforma no está preparada para atender operaciones.'),
    'createSmokeJob': ('Comprobar cola y persistencia del laboratorio', 'Herramienta técnica de laboratorio para operadores. No aprovisiona infraestructura ni forma parte del recorrido comercial.'),
    'getJob': ('Consultar un trabajo propio', 'Consulta técnica por job_id, restringida al tenant. El CRM sigue principalmente operation_id para conocer el resultado de la intención.'),
    'getLabUsage': ('Consultar el consumo autorizado del laboratorio', 'Muestra uso y reservas del tenant. Los recursos fallidos o inciertos conservan su reserva hasta que se verifica su eliminación.'),
    'listLabPolicies': ('Consultar las políticas visibles del tenant', 'Consulta de laboratorio. Comprueba vigencia, perfil, acciones y cuotas; consultar una política no la aprueba ni modifica.'),
    'getLabPolicy': ('Consultar una política de laboratorio', 'Devuelve una política propia y su revisión. El cliente debe usar referencias vigentes entregadas por el proceso autorizado.'),
    'listAuditEvents': ('Consultar los eventos auditables del tenant', 'Feed por cursor para observación. Deduplica event_id y solapa ventanas; no constituye una garantía de entrega transaccional para facturación.'),
    'getTenantDiagnostics': ('Consultar el diagnóstico del tenant', 'Estado persistido, dependencias y edad de operaciones visibles. No acredita liveness de workers ni publica colas globales de otros tenants.'),
}

IID = '11111111-1111-4111-8111-111111111111'
OID = '22222222-2222-4222-8222-222222222222'
JID = '33333333-3333-4333-8333-333333333333'
POLICY = '44444444-4444-4444-8444-444444444444'
CREATION = {'name': 'crm-ejemplo-001', 'image': 'ubuntu-2404', 'vcpus': 1,
    'memory_mb': 1536, 'disk_gb': 20, 'network_profile': 'lab-dhcp',
    'node_preference': 'mng01', 'self_service_policy_id': POLICY,
    'commercial_context': {'customer_id': 'cliente-ejemplo', 'order_id': 'pedido-ejemplo', 'product_id': 'kvm-lab'},
    'start_on_create': True}
INSTANCE = {'instance_id':IID,'operation_id':OID,'job_id':JID,
    'tenant_id':'55555555-5555-4555-8555-555555555555',**CREATION,
    'status':'ready','executor':'provisioning','provisioning_service':'provisioning',
    'infrastructure_kind':'kvm','vmid':30000,'node':'mng01','result':None,
    'network_mode':'dhcp','ip_addresses':['192.0.2.10'],'network_observation':None,'access':None,
    'created_at':'2026-10-27T09:00:00-06:00','updated_at':'2026-10-27T09:02:00-06:00'}
OPERATION = {'operation_id':OID,'instance_id':IID,'kind':'instance.create','status':'completed',
    'requested_by':'operador-ejemplo','error_code':None,'normalized_status':'completed',
    'created_at':'2026-10-27T09:00:00-06:00','updated_at':'2026-10-27T09:02:00-06:00','events':[]}
PROFILE = {'profile':'lab-dhcp','image':'ubuntu-2404','network_mode':'dhcp','bridge':'vmbr-lab',
    'vlan_tag':None,'nodes':['mng01'],'minimum_disk_gb':20,'max_vcpus_per_instance':2,
    'max_memory_mb_per_instance':2048,'max_disk_gb_per_instance':20,'self_service_enabled':True,
    'guest_initialization':None,'root_password_bootstrap':False,
    'quotas':{'instances':3,'vcpus':4,'memory_mb':6144,'disk_gb':60,'concurrent_operations':2}}


def present(schema):
    for methods in schema['paths'].values():
        for operation in methods.values():
            identifier = operation.get('operationId')
            if identifier in OPERATIONS:
                operation['summary'], operation['description'] = OPERATIONS[identifier]
            for parameter in operation.get('parameters', []):
                if parameter.get('name') == 'Idempotency-Key':
                    parameter['description'] = 'Clave de la intención dentro del tenant. Mismo cuerpo y clave conservan identificadores; otro cuerpo devuelve 409.'
            media = operation.get('requestBody', {}).get('content', {}).get('application/json')
            if media is not None and identifier in {'createInstance', 'startInstancePower', 'rebootInstancePower', 'shutdownInstancePower', 'deleteDisposableInstance'}:
                # Se añaden anotaciones al schema servido, nunca valores por defecto al handler.
                media.setdefault('examples', {})['crm_sintetico'] = {
                    'summary': 'Ejemplo sintético; sustituir referencias por recursos autorizados',
                    'value': CREATION if identifier == 'createInstance' else {}}
                if identifier=='createInstance' and media.get('schema',{}).get('$ref','').endswith('/CommercialInstanceCreate'):
                    from .commercial_contract import EXAMPLES
                    operation['summary']='Crear una KVM mediante plan_id'
                    operation['description']='Admisión automática autenticada: plan, imagen, red, cuota y capacidad. '
                    operation['description']+='Contraseña por HTTPS y Redis AES-256-GCM efímero; nunca plan/state. Un 202 exige seguimiento de operación e inicialización.'
                    media['examples']={f'kvm_{i}':{'summary':f'KVM {i}; reemplazar contraseña ilustrativa','value':e}
                        for i,e in enumerate(EXAMPLES,1)}
            for status, response in operation.get('responses', {}).items():
                labels = {'200':'Consulta completada', '201':'Autorización o propuesta registrada',
                    '202':'Intención admitida; seguir operation_id hasta resultado terminal',
                    '401':'Identidad ausente o inválida', '403':'Rol, tenant o permiso insuficiente',
                    '404':'Recurso inexistente o no visible para el tenant',
                    '409':'Conflicto de intención, estado, autorización o cuota',
                    '422':'Request incompatible con el schema; datos rechazados no se reflejan',
                    '503':'Admisión cerrada, persistencia o dependencia no disponible'}
                if status in labels:response['description'] = labels[status]
                response_media = response.get('content', {}).get('application/json')
                if response_media is None:continue
                value = None
                if status == '202' and identifier in {'createInstance','startInstancePower','shutdownInstancePower','rebootInstancePower','deleteDisposableInstance','discoverInstanceNetwork','initializeInstanceNetwork','verifyInstanceAccess','resumeLabProvisioning','reconcileInstance'}:
                    value = {'instance_id': IID, 'operation_id': OID, 'job_id': JID, 'status': 'queued'}
                    if identifier == 'createInstance':value.update(executor='provisioning', provisioning_service='provisioning')
                elif status == '200' and identifier == 'getOperation':
                    value = OPERATION
                elif status == '200' and identifier == 'listOperations':value = [OPERATION]
                elif status == '200' and identifier == 'getInstance':value = INSTANCE
                elif status == '200' and identifier == 'listInstances':value = [INSTANCE]
                elif status == '200' and identifier == 'listAuthorizedProfiles':value = [PROFILE]
                elif status == '200' and identifier == 'getInstancePower':
                    value = {'instance_id':IID,'power_state':'stopped','observed_at':'2026-10-27T09:02:00-06:00',
                        'os_boot':'unknown','guest_agent':'unknown','cloud_init':'unknown','accessible':'unknown',
                        'guest_ready':None,'mutations_available':True,'blocked_reason':None}
                elif status in {'401','403','404','409','503'}:
                    value = {'detail': {'401':'Unauthorized','403':'Write permission required','404':'Instance not found',
                        '409':'INSTANCE_BUSY','503':'Admission drained'}[status]}
                if value is not None:
                    response_media.setdefault('examples', {})['respuesta_sintetica'] = {
                        'summary':'Respuesta de ejemplo; identificadores sintéticos', 'value':value}
    return schema
