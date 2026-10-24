from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import BOOTSTRAP_TENANT_ID, Job
from ..schemas import ErrorResponse, JobAccepted, JobResponse
from ..security import Principal, authorize, authorize_write
from ..services.jobs import Publisher, QueueUnavailable, create_smoke_job
from .dependencies import get_smoke_publisher

router = APIRouter(
    prefix="/v1/jobs", tags=["jobs"], dependencies=[Depends(authorize)],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)


@router.post(
    "/smoke", status_code=202, response_model=JobAccepted,
    operation_id="createSmokeJob",
    description="Valida la cola y persistencia del laboratorio. No ejecuta infraestructura.",
)
def create_smoke(
    session: Annotated[Session, Depends(get_session)],
    publish: Annotated[Publisher, Depends(get_smoke_publisher)],
    principal: Annotated[Principal, Depends(authorize_write)],
) -> JobAccepted:
    try:
        job = create_smoke_job(session, publish, principal)
    except QueueUnavailable:
        raise HTTPException(status_code=503, detail="Queue unavailable") from None
    return JobAccepted(job_id=job.id)


@router.get(
    "/{job_id}", response_model=JobResponse, operation_id="getJob",
    responses={404: {"model": ErrorResponse}},
)
def get_job(job_id: str, session: Annotated[Session, Depends(get_session)],
            principal: Annotated[Principal, Depends(authorize)]) -> JobResponse:
    job = session.get(Job, job_id)
    if job is None or not (job.tenant_id == principal.tenant_id or (
        job.tenant_id is None and principal.tenant_id == BOOTSTRAP_TENANT_ID
    )):
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.model_validate(job)
