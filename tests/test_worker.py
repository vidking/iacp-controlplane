from uuid import uuid4

from iacp.models import Job
from iacp import worker


def test_smoke_worker_persists_result_and_ignores_completed_jobs(sessions, monkeypatch):
    monkeypatch.setattr(worker, "get_session_factory", lambda: sessions)
    job_id = str(uuid4())
    with sessions() as session:
        session.add(Job(id=job_id, operation="smoke", status="queued"))
        session.commit()
    worker.smoke.run(job_id)
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "completed"
        assert "Terraform NO fue ejecutado" in job.message
        updated = job.updated_at
    worker.smoke.run(job_id)
    worker.smoke.run(str(uuid4()))
    with sessions() as session:
        assert session.get(Job, job_id).updated_at == updated
