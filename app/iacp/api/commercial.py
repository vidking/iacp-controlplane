"""POST técnico del Provisioning API Core, con compatibilidad histórica."""
from typing import Annotated
import json
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..commercial_contract import CommercialInstanceCreate, EXAMPLES
from ..db import get_session
from ..schemas import ErrorResponse, InstanceAccepted
from ..security import Principal,authorize_write,request_uses_https
from ..services.bootstrap_secrets import BootstrapSecretError
from ..services.commercial import create
from ..services.instances import Conflict
from ..services.jobs import QueueUnavailable
from .dependencies import get_instance_publisher
from .idempotency import KEY_PATTERN, resolve_key

router=APIRouter(tags=['instances'])


@router.post('/v1/instances',status_code=202,response_model=InstanceAccepted,operation_id='createInstance',
    summary='Crear una máquina virtual del catálogo técnico',
    description='Admisión automática autenticada por identidad técnica, catálogo, perfil autorizado y capacidad física. '
        'IACP no decide pagos, pedidos, productos ni cuotas comerciales. Contraseña solo por HTTPS y Redis cifrado efímero. '
        'Idempotency-Key conserva los mismos identificadores ante una repetición idéntica.',
    responses={code:{'model':ErrorResponse} for code in [401,403,409,503]})
def post(request:Request,spec:CommercialInstanceCreate,
    response: Response,
    principal:Annotated[Principal,Depends(authorize_write)],session:Annotated[Session,Depends(get_session)],
    publish=Depends(get_instance_publisher),
    key: str = Header(default=None, alias='Idempotency-Key', min_length=1, max_length=128, pattern=KEY_PATTERN)):
    key = resolve_key(key, 'create', response)
    if not request_uses_https(request):raise HTTPException(422,'ROOT_ACCESS_REQUIRES_HTTPS', headers={'Idempotency-Key': key})
    try:return create(session,principal,spec,key,publish)
    except Conflict as e:
        code=str(e)
        safe=code if code.startswith(('COMMERCIAL_','PROVISIONING_','WAN_STATIC_')) else 'PROVISIONING_REQUEST_REJECTED'
        logging.getLogger('uvicorn.error').info(json.dumps({'event':'admission_policy_rejected',
            'request_id':getattr(request.state,'request_id',None),'reason':safe,'executed':False}))
        raise HTTPException(409,code, headers={'Idempotency-Key': key}) from None
    except ValueError:raise HTTPException(409,'PROVISIONING_CATALOG_REQUEST_INVALID', headers={'Idempotency-Key': key}) from None
    except BootstrapSecretError as e:raise HTTPException(503,str(e), headers={'Idempotency-Key': key}) from None
    except QueueUnavailable:
        request.state.admission_committed=True
        raise HTTPException(503,'PROVISIONING_DISPATCH_UNCONFIRMED; repeat the same key', headers={'Idempotency-Key': key}) from None
