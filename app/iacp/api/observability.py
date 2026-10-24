"""Versioned tenant event feed. IaCoC consumes evidence, never edits stores."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import broker_url
from ..db import get_session
from ..models import Instance, Operation, OperationEvent, TerraformApply, TerraformExecution, utcnow
from ..schemas import ErrorResponse
from ..security import Principal, authorize

router = APIRouter(prefix='/v1', tags=['observability'], responses={
    401: {'model': ErrorResponse}, 403: {'model': ErrorResponse}, 503: {'model': ErrorResponse}})


class AuditEnvelope(BaseModel):
    schema_version: Literal['iacp.audit.v1'] = 'iacp.audit.v1'
    event_id: int
    correlation_id: str
    tenant_id: str
    operation_id: str
    instance_id: str
    occurred_at: datetime
    kind: str
    status: str
    code: str
    actor: str
    service: Literal['IaC Worker', 'Lifecycle Worker', 'IACP Orchestrator']


class EventPage(BaseModel):
    events: list[AuditEnvelope]
    next_after: int
    delivery: Literal['replayable-polling'] = 'replayable-polling'


@router.get('/events', response_model=EventPage, operation_id='listAuditEvents',
    description='Feed auditable por tenant y cursor ascendente. Repetir cursor puede repetir eventos: deduplicar event_id. '
    'Por commits concurrentes un ID menor puede aparecer tarde: solapar ventanas y deduplicar; no es un outbox transaccional para facturación.')
def events(principal: Annotated[Principal, Depends(authorize)], session: Annotated[Session, Depends(get_session)],
    after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    rows = session.execute(select(OperationEvent, Operation).join(Operation, Operation.id == OperationEvent.operation_id)
        .where(Operation.tenant_id == principal.tenant_id, OperationEvent.id > after)
        .order_by(OperationEvent.id).limit(limit)).all()
    result = [AuditEnvelope(event_id=e.id, correlation_id=o.correlation_id or o.id, tenant_id=o.tenant_id,
        operation_id=o.id, instance_id=o.instance_id, occurred_at=e.created_at, kind=o.kind,
        status=e.status, code=e.code, actor=e.actor, service=('Lifecycle Worker' if o.kind.startswith('instance.power.')
            else 'IaC Worker' if e.actor.startswith('worker:') else 'IACP Orchestrator')) for e, o in rows]
    return EventPage(events=result, next_after=result[-1].event_id if result else after)


class Diagnostics(BaseModel):
    schema_version: Literal['iacp.health.v1'] = 'iacp.health.v1'
    tenant_id: str
    observed_at: datetime
    dependencies: dict[str, str]
    active_operations: int
    oldest_active_age_seconds: float
    uncertain_operations: int
    worker_liveness: Literal['unknown'] = 'unknown'
    queue_depth: dict[str, int] | None = None
    queue_scope: Literal['not-disclosed'] = 'not-disclosed'


@router.get('/observability', response_model=Diagnostics, operation_id='getTenantDiagnostics',
    description='Estado persistido del tenant y comprobación Redis. No certifica procesos worker vivos ni publica colas globales de otros tenants. '
    'Health global DB/esquema/Redis: /health/ready. La edad mide desde admisión, incluidos reintentos.')
def diagnostics(principal: Annotated[Principal, Depends(authorize)], session: Annotated[Session, Depends(get_session)]):
    approvals = session.execute(select(TerraformExecution.operation_id, TerraformApply.status)
        .join(TerraformApply, (TerraformApply.instance_id == TerraformExecution.instance_id)
            & (TerraformApply.attempt == TerraformExecution.attempt))
        .join(Instance, Instance.id == TerraformExecution.instance_id)
        .where(Instance.tenant_id == principal.tenant_id, TerraformApply.status.in_(['queued', 'running', 'uncertain']))).all()
    current = dict(approvals)
    rows = session.scalars(select(Operation).where(Operation.tenant_id == principal.tenant_id,
        or_(Operation.status.in_(['queued', 'running', 'uncertain']), Operation.id.in_(current)))).all()
    now = utcnow()
    redis_status = 'unavailable'
    try:
        with Redis.from_url(broker_url(), socket_connect_timeout=1, socket_timeout=1) as client:
            if client.ping():
                redis_status = 'ready'
    except Exception:
        pass
    return Diagnostics(tenant_id=principal.tenant_id, observed_at=now,
        dependencies={'postgresql': 'ready', 'redis': redis_status}, active_operations=len(rows),
        oldest_active_age_seconds=max([(now-r.created_at.replace(tzinfo=timezone.utc)).total_seconds() for r in rows] or [0]),
        uncertain_operations=sum(current.get(r.id, r.status) == 'uncertain' for r in rows))
