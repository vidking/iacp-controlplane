"""Reservas G concurrentes sobre PostgreSQL real y Redis solo local."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from iacp.models import BOOTSTRAP_TENANT_ID,CommercialIntent,Instance
from iacp.security import Principal
from iacp.services.commercial import create
from iacp.services.instances import Conflict
from tests.helpers import upgrade
from tests.test_commercial_admission import commercial_config,store,specification
from tests.test_commercial_catalog import catalog_profile

pytestmark=pytest.mark.integration


def test_global_quota_serializes_two_commercial_requests(pg_engine,commercial_config,store):
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    barrier=Barrier(2)
    def submit(n):
        barrier.wait(timeout=10)
        with sessions() as s:
            try:return create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),
                specification(name='concurrent-'+str(n)),'concurrent-'+str(n),lambda _:None,store,
                capacity=lambda *_:None).instance_id
            except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,[1,2]))
    assert sum(x is not None for x in results)==1
    with sessions() as s:
        assert len(s.scalars(select(Instance)).all())==len(s.scalars(select(CommercialIntent)).all())==1
    assert len(store.redis.values)==1


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


def test_rollback_to_historical_contract_keeps_schema_0012_and_commercial_rows_readable(pg_engine,runtime,commercial_config,store,monkeypatch):
    from fastapi.testclient import TestClient
    from iacp.main import create_app
    upgrade(pg_engine);sessions=sessionmaker(bind=pg_engine,autoflush=False,expire_on_commit=False)
    with sessions() as s:
        accepted=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),specification(),
            'before-rollback',lambda _:None,store,capacity=lambda *_:None)
    monkeypatch.setenv('IACP_CREATION_CONTRACT','historical')
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0')
    with TestClient(create_app()) as client:
        assert client.get('/health/ready').status_code==200
        row=client.get('/v1/instances/'+accepted.instance_id,headers=runtime)
        assert row.status_code==200
        assert row.json()['commercial_context']['order_id']==specification().commercial_context.order_id
        assert client.post('/v1/instances',headers=runtime,json={}).status_code==503
    with sessions() as s:assert s.get(CommercialIntent,accepted.instance_id).bootstrap_status=='pending'
