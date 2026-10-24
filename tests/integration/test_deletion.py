"""Real PostgreSQL/Redis/Celery, synthetic Terraform/Proxmox. No lab mutation."""
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from iacp.api.dependencies import get_deletion_publisher
from iacp.models import BOOTSTRAP_TENANT_ID, DeletionExecution, Instance, Operation, PowerExecution
from iacp.terraform.deletion import execute_deletion
from iacp.terraform.locks import instance_lock
from tests.helpers import upgrade
from tests.test_deletion import FakeDeletion, plan, prepare, delete, seed

pytestmark=pytest.mark.integration


@pytest.fixture
def integrated(pg_engine,runtime,application,monkeypatch,tmp_path):
    from iacp.db import get_session
    from tests.test_deletion import disposable
    upgrade(pg_engine)
    factory=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    def sessions_override():
        with factory() as session:yield session
    application.dependency_overrides[get_session]=sessions_override
    # Reuse the exact API setup with synthetic principals, against PostgreSQL.
    fixture=disposable.__wrapped__(factory,application,monkeypatch,tmp_path)
    return factory,fixture


def test_real_postgres_delete_and_tombstone(client,auth,integrated):
    sessions,fixture=integrated;op,body=prepare(client,auth,fixture,sessions)
    assert delete(client,auth,fixture,body).status_code==202
    fake=FakeDeletion();execute_deletion(op['operation_id'],'apply',sessions,fake)
    with sessions() as s:assert s.get(Instance,fixture.id).status=='deleted'
    assert fake.calls.count('apply')==1


def test_real_advisory_lock_blocks_delete(client,auth,integrated):
    sessions,fixture=integrated
    with instance_lock(sessions.kw['bind'],fixture.id):assert plan(client,auth,fixture).status_code==409
    assert plan(client,auth,fixture).status_code==202


def test_real_worker_redelivery_only_observes_consumed_delete(client,auth,integrated):
    sessions,fixture=integrated;op,body=prepare(client,auth,fixture,sessions)
    delete(client,auth,fixture,body);fake=FakeDeletion();fake.interrupt=True
    with pytest.raises(KeyboardInterrupt):execute_deletion(op['operation_id'],'apply',sessions,fake)
    fake.interrupt=False;execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert fake.calls.count('apply')==1


def test_real_celery_queue_plan_then_apply(client,auth,integrated,application,tmp_path):
    sessions,fixture=integrated
    application.dependency_overrides.pop(get_deletion_publisher)
    script=Path(__file__).resolve().parents[1]/'deletion_worker.py'
    with (tmp_path/'worker.log').open('w') as output:
        process=subprocess.Popen([sys.executable,str(script)],stdout=output,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONPATH=str(Path.cwd()/'app')+':'+str(Path.cwd())))
        try:
            op=plan(client,auth,fixture).json();deadline=time.monotonic()+20
            while True:
                body=client.get('/v1/operations/'+op['operation_id'],headers=auth).json()
                if body['deletion']['status']=='planned':break
                assert time.monotonic()<deadline and process.poll() is None
                time.sleep(.1)
            payload={'operation_id':op['operation_id'],'plan_sha256':body['deletion']['plan_sha256']}
            assert client.post(fixture.path+'/terraform/deletion-approval',headers={**fixture.reviewer,'Idempotency-Key':'actual-queue-review'},json=payload).status_code==201
            assert delete(client,auth,fixture,payload).status_code==202
            while True:
                body=client.get('/v1/operations/'+op['operation_id'],headers=auth).json()
                if body['status']=='completed':break
                assert time.monotonic()<deadline and process.poll() is None
                time.sleep(.1)
            assert body['deletion']['phase']=='deleted'
        finally:
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)


def test_real_postgres_constraints_reject_protected_id_and_two_active_deletions(client,auth,integrated):
    from uuid import uuid4
    sessions,fixture=integrated;op=plan(client,auth,fixture).json()
    with pytest.raises(IntegrityError):
        with sessions() as s:
            s.get(DeletionExecution,op['operation_id']).vmid=106;s.commit()
    with pytest.raises(IntegrityError):
        with sessions() as s:
            original=s.get(DeletionExecution,op['operation_id']);identifier=str(uuid4())
            s.add(Operation(id=identifier,instance_id=fixture.id,tenant_id=BOOTSTRAP_TENANT_ID,kind='instance.delete',
                status='queued',idempotency_key='concurrent',request_hash='a'*64,requested_by='bootstrap'));s.flush()
            s.add(DeletionExecution(operation_id=identifier,instance_id=fixture.id,status='queued',phase='plan_queued',
                vmid=30000,node='mng01',name=original.name,provisioning_binding=original.provisioning_binding));s.commit()


def test_upgrade_from_0006_preserves_real_postgres_provisioning(pg_engine,runtime):
    from sqlalchemy import MetaData,select,text
    from tests.test_deletion import seed
    upgrade(pg_engine,'0006_power_ownership');factory=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    seed(factory)
    metadata=MetaData()
    with pg_engine.connect() as c:
        metadata.reflect(c)
        before={name:list(c.execute(select(table)).mappings()) for name,table in metadata.tables.items() if name!='alembic_version'}
    upgrade(pg_engine);upgrade(pg_engine)
    with pg_engine.connect() as c:
        for name,rows in before.items():assert list(c.execute(select(metadata.tables[name])).mappings())==rows
        assert c.scalar(text('SELECT count(*) FROM deletion_executions'))==0
        assert c.scalar(text('SELECT version_num FROM alembic_version'))=='0009_lab_self_service'
