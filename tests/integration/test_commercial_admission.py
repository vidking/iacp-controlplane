"""Reservas G concurrentes sobre PostgreSQL real y Redis solo local."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from iacp.models import BOOTSTRAP_TENANT_ID,CommercialIntent,Instance,TerraformExecution
from iacp.security import Principal
from iacp.services.commercial import create
from iacp.services.instances import Conflict
from tests.helpers import upgrade
from tests.test_commercial_admission import commercial_config,store,specification
from tests.test_commercial_catalog import catalog_profile

pytestmark=pytest.mark.integration


@pytest.mark.parametrize('minimal_body',[False,True])
def test_global_admission_serializes_two_core_requests_without_tenant_quota(
        pg_engine,commercial_config,store,minimal_body):
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    barrier=Barrier(2)
    def submit(n):
        spec=specification(name='concurrent-'+str(n))
        if minimal_body:
            from iacp.commercial_contract import CommercialInstanceCreate
            body=spec.model_dump(mode='json',exclude={'network','commercial_context','access'})
            body['access']={'root_password':spec.access.root_password.get_secret_value(),'ssh_public_keys':spec.access.ssh_public_keys}
            spec=CommercialInstanceCreate.model_validate(body)
        barrier.wait(timeout=10)
        with sessions() as s:
            try:return create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),
                spec,'concurrent-'+str(n),lambda _:None,store,
                capacity=lambda *_:None).instance_id
            except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,[1,2]))
    assert sum(x is not None for x in results)==2
    with sessions() as s:
        assert len(s.scalars(select(Instance)).all())==len(s.scalars(select(CommercialIntent)).all())==2
        vmids=s.scalars(select(TerraformExecution.vmid)).all()
        assert len(vmids)==len(set(vmids))==2
    assert len(store.redis.values)==2


def test_bootstrap_queue_transports_only_opaque_operation_reference(runtime):
    from kombu import Connection
    from iacp.worker import celery_app
    from iacp.api.dependencies import get_commercial_bootstrap_publisher
    celery_app.conf.broker_url=os.environ['CELERY_BROKER_URL']
    reference=str(uuid4())
    with Connection(os.environ['CELERY_BROKER_URL']) as connection:
        queue=connection.SimpleQueue('terraform');queue.clear()
        try:
            get_commercial_bootstrap_publisher()(reference)
            message=queue.get(timeout=10)
            assert message.headers['task']=='iacp.commercial.bootstrap'
            assert message.payload[0]==[reference]
            assert 'password' not in json.dumps(message.payload)
            message.ack()
        finally:queue.clear();queue.close()


@pytest.mark.parametrize('minimal_body',[False,True])
def test_rollback_to_historical_contract_keeps_schema_0012_and_commercial_rows_readable(pg_engine,runtime,commercial_config,store,monkeypatch,minimal_body):
    from fastapi.testclient import TestClient
    from iacp.main import create_app
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    spec=specification()
    if minimal_body:
        from iacp.commercial_contract import CommercialInstanceCreate
        body=spec.model_dump(mode='json',exclude={'network','commercial_context','access'})
        body['access']={'root_password':spec.access.root_password.get_secret_value(),'ssh_public_keys':spec.access.ssh_public_keys}
        spec=CommercialInstanceCreate.model_validate(body)
    with sessions() as s:
        accepted=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,
            'before-rollback',lambda _:None,store,capacity=lambda *_:None)
    monkeypatch.setenv('IACP_CREATION_CONTRACT','historical')
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0')
    with TestClient(create_app()) as client:
        assert client.get('/health/ready').status_code==200
        row=client.get('/v1/instances/'+accepted.instance_id,headers=runtime)
        assert row.status_code==200
        if minimal_body:assert row.json()['commercial_context'] is None
        else:assert row.json()['commercial_context']['order_id']==specification().commercial_context.order_id
        assert client.post('/v1/instances',headers=runtime,json={}).status_code==503
    with sessions() as s:assert s.get(CommercialIntent,accepted.instance_id).bootstrap_status=='pending'


def test_concurrent_minimal_retries_publish_only_one_job(pg_engine,commercial_config,store):
    from iacp.commercial_contract import CommercialInstanceCreate
    from iacp.models import Job,Operation
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    spec=specification();body=spec.model_dump(mode='json',exclude={'network','commercial_context','access'})
    body['access']={'root_password':spec.access.root_password.get_secret_value(),'ssh_public_keys':spec.access.ssh_public_keys}
    spec=CommercialInstanceCreate.model_validate(body)
    barrier=Barrier(2);published=[]
    def submit(_):
        barrier.wait(timeout=10)
        with sessions() as s:
            return create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,'same-minimal',published.append,
                store,capacity=lambda *_:None).model_dump()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,[1,2]))
    assert results[0]==results[1] and len(published)==1
    with sessions() as s:
        for model in (Instance,CommercialIntent,Job,Operation):assert len(s.scalars(select(model)).all())==1
    assert len(store.redis.values)==1
