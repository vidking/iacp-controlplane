import hmac
import os
import uuid
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from redis import Redis
from .db import Base, engine, SessionLocal, secret
from .models import Job
from .worker import smoke


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap de laboratorio. Reemplazar por migraciones Alembic antes de producción.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="IACP Control Plane", version="0.1.0-bootstrap", lifespan=lifespan)


def authorize(authorization: str | None = Header(default=None)) -> None:
    expected = secret("IACP_API_TOKEN_FILE")
    submitted = (authorization or "").removeprefix("Bearer ")
    if len(expected) < 32 or not hmac.compare_digest(submitted, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health/live")
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    try:
        with engine.connect() as con:
            con.execute(text("SELECT 1"))
        Redis.from_url(os.environ["CELERY_BROKER_URL"]).ping()
    except Exception:
        raise HTTPException(status_code=503, detail="Database or queue unavailable")
    return {"status": "ready"}


@app.post("/v1/jobs/smoke", status_code=202, dependencies=[Depends(authorize)])
def create_smoke_job():
    job_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(Job(id=job_id, operation="smoke", status="queued"))
        db.commit()
    try:
        smoke.delay(job_id)
    except Exception:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            job.status = "failed"
            job.message = "Error publicando trabajo en la cola"
            db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable")
    return {"job_id": job_id, "status": "queued"}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
def get_job(job_id: str):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"job_id": job.id, "operation": job.operation,
                "status": job.status, "message": job.message,
                "created_at": job.created_at, "updated_at": job.updated_at}


@app.post("/v1/instances", dependencies=[Depends(authorize)])
def create_instance():
    # No simular provisionamiento ni ejecutar Terraform sin idempotencia, RBAC y locks.
    return JSONResponse(status_code=503, content={
        "code": "IAC_EXECUTOR_NOT_CONFIGURED",
        "detail": "Aprovisionamiento deshabilitado hasta integrar y validar el IaC Worker"
    })
