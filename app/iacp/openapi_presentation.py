"""Presentación pública del contrato, incluida la idempotencia HTTP opcional."""
DESCRIPTION = (
    'API de aprovisionamiento y ciclo de vida de IACP para infraestructura KVM. '
    'Selecciona perfiles autorizados, conserva las intenciones idempotentes y sigue cada operación hasta su resultado.\n\n'
    '**Autenticación:** HTTP Basic permite consultar la documentación. Las llamadas a `/v1` requieren '
    'un token Bearer del tenant y el rol correspondiente. Introduce solo el token en **Authorize**; '
    'la contraseña de documentación no autoriza la API.\n\n'
    '**Flujo recomendado:** perfiles → creación → seguimiento por `operation_id` → acceso/Lifecycle → eliminación autorizada. '
    'Un 202 confirma admisión; no confirma disponibilidad ni eliminación. Conserva `Idempotency-Key`, UUID y cuerpo en reintentos. '
    'Ante `uncertain`, conserva la misma instancia y utiliza la reconciliación autorizada.\n\n'
    '**Alcance:** API privada por LAN/VPN, recursos propios del tenant, perfiles técnicos autorizados y capacidad física. '
    'IACP no determina pagos, pedidos, productos ni elegibilidad comercial. Los ejemplos son sintéticos y no constituyen autorizaciones. '
    'Las operaciones administrativas requieren su ámbito específico.'
    '\n\n**Precondiciones:** los perfiles DHCP y los destinos proceden de la autorización técnica vigente. '
    'Usa `power/shutdown` para apagar ordenadamente. DELETE requiere ownership técnico verificable y autorización; '
    'VM 104 y las Golden Images 29000, 29001, 29010 y 29011 permanecen protegidas. '
    'Las KVM administradas, incluida la histórica VM 106, dependen de ownership técnico verificable. '
    'La admisión cerrada devuelve 503.'
)
TAGS = [
    {'name': 'instances', 'description': '01 · Perfiles, creación y consulta de instancias propias del tenant.'},
    {'name': 'operations', 'description': '02 · Seguimiento de intenciones asíncronas, estados terminales y eventos.'},
    {'name': 'kvm-lifecycle', 'description': '03 · Estado de energía, inicio, reinicio y apagado ordenado de KVM autorizadas.'},
    {'name': 'disposable-deletion', 'description': '04 · Eliminación autorizada de instancias descartables; recursos protegidos excluidos.'},
    {'name': 'observability', 'description': 'Diagnóstico · Eventos y estado del tenant; no sustituye la supervisión de infraestructura.'},
    {'name': 'health', 'description': 'Diagnóstico · Disponibilidad de la API y sus dependencias.'},
    {'name': 'jobs', 'description': 'Laboratorio · Comprobación técnica de cola y consulta de trabajos propios.'},
    {'name': 'provisioning-administration', 'description': 'Administración · Inspección y recuperación de aprovisionamiento.'},
    {'name': 'provisioning-approvals', 'description': 'Administración · Revisión independiente de autorizaciones de un solo uso.'},
]

OPERATIONS = {
    'createInstance': ('Crear una instancia KVM', 'Admite una intención idempotente bajo el catálogo y el perfil técnico autorizado. `Idempotency-Key` es opcional: si se omite, IACP genera una clave de intención y la devuelve en la respuesta. Conserva los tres identificadores de respuesta. Un 202 no significa que la VM ni su acceso estén disponibles. Ante un timeout, reutiliza la misma clave y cuerpo; dos solicitudes sin clave son intenciones independientes.'),
    'listInstances': ('Consultar las instancias del tenant', 'Lista paginada y estable del inventario propio. Utiliza limit, offset y los filtros disponibles. X-Total-Count indica el total del filtro; deleted conserva el historial de una instancia eliminada.'),
    'getInstance': ('Consultar una instancia', 'Obtiene el estado persistido del UUID propio, sus recursos y las observaciones de red/acceso. Una instancia ajena o no visible devuelve 404.'),
    'listOperations': ('Consultar el historial de operaciones', 'Lista paginada de intenciones del tenant. Filtra por instance_id para seguir creación, Lifecycle y eliminación. Consulta normalized_status y, para arranque automático, fulfillment.'),
    'getOperation': ('Seguir una operación por su UUID', 'Consulta la misma operación hasta completed, failed o uncertain. Revisa error_code y recovery_required antes de continuar. Un estado uncertain exige observación/reconciliación autorizada sobre la misma instancia.'),
    'getInstancePower': ('Consultar el estado de energía', 'Observa la KVM propia y devuelve las transiciones disponibles. Running solo describe energía; no demuestra SSH, Cloud-Init, Guest Agent ni disponibilidad de aplicaciones.'),
    'startInstancePower': ('Iniciar una instancia autorizada', 'Solicita start con una intención idempotente. `Idempotency-Key` es opcional; si se omite, IACP genera y devuelve una clave en la respuesta. Requiere identidad técnica, ownership, habilitación y ausencia de fences activos. Sigue operation_id hasta el resultado terminal; no garantiza disponibilidad del invitado. Para el recorrido guiado usa **Crear y administrar máquinas virtuales**: selecciona la VM y pulsa Iniciar; la consola completa instance_id y la intención.'),
    'rebootInstancePower': ('Reiniciar una instancia autorizada', 'Solicita un reinicio de una KVM running con autorización vigente. `Idempotency-Key` es opcional y se devuelve generada cuando se omite. Sigue la operación: observar running no basta para demostrar que el reinicio ocurrió. Para el recorrido guiado usa **Crear y administrar máquinas virtuales**; espera mutations_available=true y no copies IDs ni claves.'),
    'shutdownInstancePower': ('Apagar una instancia ordenadamente', 'Solicita shutdown mediante ACPI, sin apagado forzado de respaldo. `Idempotency-Key` es opcional y se devuelve generada cuando se omite. Sigue operation_id hasta confirmar stopped antes de una eliminación autorizada. La consola operativa gestiona el identificador y la intención automáticamente, sin cuerpo vacío.'),
    'deleteDisposableInstance': ('Eliminar una instancia autorizada', 'La eliminación requiere ownership técnico verificable, VM apagada y ausencia de locks. Sigue la operación hasta confirmar eliminación; conserva UUID e historial. Los recursos protegidos permanecen excluidos. Para operar sin copiar IDs, claves ni cuerpos, vuelve a **Crear y administrar máquinas virtuales**.'),
    'discoverInstanceNetwork': ('Observar la red del invitado', 'Solicita una observación asíncrona con identidad y MAC verificadas. Una IP pendiente no equivale a fallo de creación. Conserva operation_id y espera la observación; no cambia el direccionamiento autorizado.'),
    'initializeInstanceNetwork': ('Completar la inicialización autorizada', 'Inicialización fija y de un solo uso bajo un perfil técnico compatible. No admite comandos o archivos del cliente. Start la solicita automáticamente; esta ruta conserva la intención ante una entrega no confirmada.'),
    'verifyInstanceAccess': ('Verificar el acceso inicial', 'Solicita la verificación autorizada del acceso del invitado. Conserva la operación y consulta el resultado sin enviar de nuevo una creación. No devuelve contraseñas.'),
    'healthLive': ('Comprobar que la API responde', 'Comprobación de vida del proceso; no certifica dependencias ni crea infraestructura.'),
    'healthReady': ('Comprobar disponibilidad de dependencias', 'Verifica base de datos, esquema y cola. Un 503 indica que la plataforma no está preparada para atender operaciones.'),
    'createSmokeJob': ('Comprobar cola y persistencia', 'Herramienta técnica para operadores. No aprovisiona infraestructura ni forma parte del recorrido de creación.'),
    'getJob': ('Consultar un trabajo propio', 'Consulta técnica por job_id, restringida al tenant. El CRM sigue principalmente operation_id para conocer el resultado de la intención.'),
    'listAuditEvents': ('Consultar los eventos auditables del tenant', 'Feed por cursor para observación. Deduplica event_id y solapa ventanas; no constituye una garantía de entrega transaccional para facturación.'),
    'getTenantDiagnostics': ('Consultar el diagnóstico del tenant', 'Estado persistido, dependencias y edad de operaciones visibles. No acredita liveness de workers ni publica colas globales de otros tenants.'),
}

IID = '11111111-1111-4111-8111-111111111111'
OID = '22222222-2222-4222-8222-222222222222'
JID = '33333333-3333-4333-8333-333333333333'
CREATION = {'name': 'crm-ejemplo-001', 'image': 'ubuntu-2404', 'vcpus': 1,
    'memory_mb': 1536, 'disk_gb': 20, 'network_profile': 'lab-dhcp',
    'node_preference': 'mng01',
    'commercial_context': {'customer_id': 'cliente-ejemplo', 'order_id': 'pedido-ejemplo', 'product_id': 'kvm-core'},
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
def present(schema):
    optional_key_operations = {
        'createInstance', 'startInstancePower', 'shutdownInstancePower',
        'rebootInstancePower', 'deleteDisposableInstance',
    }
    for methods in schema['paths'].values():
        for operation in methods.values():
            identifier = operation.get('operationId')
            if identifier in OPERATIONS:
                operation['summary'], operation['description'] = OPERATIONS[identifier]
            for parameter in operation.get('parameters', []):
                if parameter.get('name') == 'Idempotency-Key':
                    if identifier in optional_key_operations:
                        parameter['required'] = False
                        parameter['description'] = 'Opcional. Si se omite, IACP genera una clave única de intención y la devuelve en el header de respuesta. Una clave explícita conserva deduplicación y replay; otro cuerpo devuelve 409.'
                    else:
                        parameter['description'] = 'Clave de la intención dentro del tenant. Mismo cuerpo y clave conservan identificadores; otro cuerpo devuelve 409.'
            if identifier in optional_key_operations:
                response_202 = operation.get('responses', {}).get('202')
                if response_202 is not None:
                    response_202.setdefault('headers', {})['Idempotency-Key'] = {
                        'description': 'Clave de intención usada por IACP; reutilícela si debe repetir la misma solicitud.',
                        'schema': {'type': 'string'},
                    }
            media = operation.get('requestBody', {}).get('content', {}).get('application/json')
            if media is not None and identifier in {'createInstance', 'startInstancePower', 'rebootInstancePower', 'shutdownInstancePower', 'deleteDisposableInstance'}:
                # Se añaden anotaciones al schema servido, nunca valores por defecto al handler.
                media.setdefault('examples', {})['crm_sintetico'] = {
                    'summary': 'Ejemplo sintético; sustituir referencias por recursos autorizados',
                    'value': CREATION if identifier == 'createInstance' else {}}
                if identifier=='createInstance' and media.get('schema',{}).get('$ref','').endswith('/CommercialInstanceCreate'):
                    from .commercial_contract import EXAMPLES, MINIMAL_EXAMPLE
                    operation['summary']='Crear una KVM mediante plan_id'
                    operation['description']='Admisión automática autenticada: plan, imagen, red, perfil y capacidad física. '
                    operation['description']+='Se puede omitir network (lab-dhcp) y commercial_context; el CRM conserva sus referencias fuera de la autorización técnica. '
                    operation['description']+='Contraseña por HTTPS y Redis AES-256-GCM efímero; nunca plan/state. Un 202 exige seguimiento de operación e inicialización.'
                    media['examples']={'core_minimo':{'summary':'Solicitud técnica mínima; valores predeterminados autorizados','value':MINIMAL_EXAMPLE},
                        **{f'kvm_{i}':{'summary':f'KVM {i}; solicitud completa compatible','value':e}
                           for i,e in enumerate(EXAMPLES,1)}}
            for status, response in operation.get('responses', {}).items():
                labels = {'200':'Consulta completada', '201':'Autorización o propuesta registrada',
                    '202':'Intención admitida; seguir operation_id hasta resultado terminal',
                    '401':'Identidad ausente o inválida', '403':'Rol, tenant o permiso insuficiente',
                    '404':'Recurso inexistente o no visible para el tenant',
                    '409':'Conflicto de intención, estado, ownership o capacidad',
                    '422':'Request incompatible con el schema; datos rechazados no se reflejan',
                    '503':'Admisión cerrada, persistencia o dependencia no disponible'}
                if status in labels:response['description'] = labels[status]
                response_media = response.get('content', {}).get('application/json')
                if response_media is None:continue
                value = None
                if status == '202' and identifier in {'createInstance','startInstancePower','shutdownInstancePower','rebootInstancePower','deleteDisposableInstance','discoverInstanceNetwork','initializeInstanceNetwork','verifyInstanceAccess'}:
                    value = {'instance_id': IID, 'operation_id': OID, 'job_id': JID, 'status': 'queued'}
                    if identifier == 'createInstance':value.update(executor='provisioning', provisioning_service='provisioning')
                elif status == '200' and identifier == 'getOperation':
                    value = OPERATION
                elif status == '200' and identifier == 'listOperations':value = [OPERATION]
                elif status == '200' and identifier == 'getInstance':value = INSTANCE
                elif status == '200' and identifier == 'listInstances':value = [INSTANCE]
                elif status == '200' and identifier == 'listGoldenImages':
                    # FastAPI elimina None en ejemplos anidados al generar su
                    # modelo OpenAPI. Restaurar null explícitos ya validados por
                    # ImageResponse para expresar capacidades no certificadas.
                    from copy import deepcopy
                    from .api.catalog import IMAGE_RESPONSE_EXAMPLE
                    response_media['examples']={'inventario_pendiente':{
                        'summary':'Cuatro imágenes; capacidad de ejemplo no certificada',
                        'value':deepcopy(IMAGE_RESPONSE_EXAMPLE)}}
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
