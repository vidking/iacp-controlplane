from fastapi import APIRouter, HTTPException
from redis import Redis
from sqlalchemy import text

from ..config import broker_url
from ..db import get_engine
from ..migrations import schema_is_current
from ..schemas import ErrorResponse, HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse, operation_id="healthLive")
def live() -> HealthResponse:
    return HealthResponse(status="live")


@router.get(
    "/ready", response_model=HealthResponse, operation_id="healthReady",
    responses={503: {"model": ErrorResponse}},
)
def ready() -> HealthResponse:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            if not schema_is_current(connection):
                raise RuntimeError("Schema migration required")
        with Redis.from_url(broker_url(), socket_connect_timeout=3, socket_timeout=3) as redis:
            redis.ping()
    except Exception:
        raise HTTPException(
            status_code=503, detail="Database, schema or queue unavailable"
        ) from None
    return HealthResponse(status="ready")
