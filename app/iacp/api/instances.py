from fastapi import APIRouter, Depends

from ..schemas import ErrorResponse, ProvisioningUnavailable
from ..security import authorize

router = APIRouter(prefix="/v1/instances", tags=["instances"], dependencies=[Depends(authorize)])


@router.post(
    "", status_code=503, response_model=ProvisioningUnavailable | ErrorResponse,
    operation_id="createInstance",
    responses={401: {"model": ErrorResponse}},
    description="Reservado. Aprovisionamiento deshabilitado hasta validar autorización y ejecución.",
)
def create_instance() -> ProvisioningUnavailable:
    return ProvisioningUnavailable()
