"""PostgreSQL transaction locks and real Celery delivery; infrastructure is simulated."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, select
from sqlalchemy.orm import sessionmaker

from iacp.api.dependencies import get_instance_publisher
from iacp.db import get_session_factory
from iacp.main import create_app
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, LabPolicy, Operation, TerraformExecution
from iacp.schemas import InstanceCreate
from iacp.security import Principal
from iacp.services.instances import create_instance, Conflict
from iacp.services.lab import LabPolicySpec, propose, approve_policy
from iacp.terraform import profiles
from tests.helpers import upgrade
from tests.test_dhcp_profiles import profile_data
from tests.test_lab_self_service import POLICY_SPEC

pytestmark=pytest.mark.integration


@pytest.fixture
def pg_lab(pg_engine,runtime,tmp_path,monkeypatch):
    upgrade(pg_engine)
    data=profile_data();p=data['profiles']['lab-dhcp']
    p.update(self_service_enabled=True,max_live_instances=3,max_concurrent_operations=2,
        max_total_vcpus=8,max_total_memory_mb=8192,max_total_disk_gb=80)
    p['settings']['network_bridge']='vmbr0'
    path=tmp_path/'profiles.json';path.write_text(json.dumps(data))
    monkeypatch.setenv('IACP_TERRAFORM_PROFILES_FILE',str(path))
    monkeypatch.setattr(profiles,'cluster_vmids',lambda:set())
    for k in ['IACP_LAB_SELF_SERVICE_ENABLED','IACP_TERRAFORM_APPLY_ENABLED','IACP_TERRAFORM_DELETE_ENABLED']:
        monkeypatch.setenv(k,'1')
    monkeypatch.setenv('IACP_INSTANCE_EXECUTOR','terraform')
    sessions=get_session_factory()
    operator=Principal(BOOTSTRAP_TENANT_ID,'bootstrap','operator')
    with sessions() as s:
        proposal=propose(s,operator,LabPolicySpec.model_validate(POLICY_SPEC),'pg-lab-policy')
        approved=approve_policy(s,proposal,Principal(BOOTSTRAP_TENANT_ID,'independent-reviewer','approver'),proposal.policy_sha256)
    return sessions,approved.id


def test_two_concurrent_requests_have_unique_durable_reservations(pg_lab):
    sessions,policy_id=pg_lab;barrier=Barrier(2)
    def create(n):
        barrier.wait(10)
        with sessions() as s:
            return create_instance(s,Principal(BOOTSTRAP_TENANT_ID,'bootstrap','operator'),
                InstanceCreate(name='pg-lab-'+str(n),image='test-image',network_profile='lab-dhcp',self_service_policy_id=policy_id),
                'parallel-'+str(n),lambda _:None)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(create,(1,2)))
    assert len({r.instance_id for r in results})==2
    with sessions() as s:
        assert set(s.scalars(select(TerraformExecution.vmid)))=={30000,30001}
        with pytest.raises(Conflict,match='concurrent_operations'):
            create_instance(s,Principal(BOOTSTRAP_TENANT_ID,'bootstrap','operator'),
                InstanceCreate(name='blocked',image='test-image',network_profile='lab-dhcp',self_service_policy_id=policy_id),
                'third',lambda _:None)


def test_concurrent_same_key_commits_one_intent(pg_lab):
    sessions,policy_id=pg_lab;barrier=Barrier(2)
    def create(_):
        barrier.wait(10)
        with sessions() as s:
            return create_instance(s,Principal(BOOTSTRAP_TENANT_ID,'bootstrap','operator'),
                InstanceCreate(name='same',image='test-image',network_profile='lab-dhcp',self_service_policy_id=policy_id),
                'same-key',lambda _:None)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(create,(1,2)))
    assert results[0]==results[1]
    with sessions() as s:
        assert len(s.scalars(select(Instance)).all())==len(s.scalars(select(Operation)).all())==1


def test_real_queue_autonomous_provision_and_delete(pg_lab,runtime,tmp_path):
    sessions,policy_id=pg_lab;app=create_app()
    # Admission still performs native inventory checks in this process via the explicit fixture.
    script=Path(__file__).resolve().parents[1]/'lab_worker.py'
    with (tmp_path/'worker.log').open('w') as log:
        worker=subprocess.Popen([sys.executable,str(script)],env={**os.environ,'PYTHONPATH':'/workspace/app:/workspace'},stdout=log,stderr=subprocess.STDOUT)
        try:
            with TestClient(app) as client:
                r=client.post('/v1/instances',headers={**runtime,'Idempotency-Key':'queued-lab'},json={
                    'name':'queued-lab','image':'test-image','network_profile':'lab-dhcp','self_service_policy_id':policy_id})
                assert r.status_code==202,r.text
                accepted=r.json();deadline=time.monotonic()+25
                def wait(operation_id):
                    while True:
                        op=client.get('/v1/operations/'+operation_id,headers=runtime).json()
                        if op['status'] in ['completed','failed','uncertain']:return op
                        assert worker.poll() is None and time.monotonic()<deadline,op
                        time.sleep(.1)
                op=wait(accepted['operation_id']);assert op['status']=='completed',op
                instance=client.get('/v1/instances/'+accepted['instance_id'],headers=runtime).json()
                assert instance['status']=='ready' and instance['vmid']==30000
                r=client.request('DELETE','/v1/instances/'+accepted['instance_id'],headers={**runtime,'Idempotency-Key':'queued-delete'},json={})
                assert r.status_code==202,r.text
                op=wait(r.json()['operation_id']);assert op['status']=='completed',op
                assert client.get('/v1/instances/'+accepted['instance_id'],headers=runtime).json()['status']=='deleted'
        finally:
            worker.terminate()
            try:worker.wait(10)
            except subprocess.TimeoutExpired:worker.kill();worker.wait(3)


def test_0009_preserves_0008_rows_and_never_adopts_existing_instances(pg_engine):
    from tests.power_helpers import managed_instance
    upgrade(pg_engine,'0008_dhcp_profiles')
    sessions=sessionmaker(bind=pg_engine,expire_on_commit=False,autoflush=False)
    identifier=managed_instance(sessions,BOOTSTRAP_TENANT_ID,vmid=30000)
    with pg_engine.connect() as c:
        metadata=MetaData();metadata.reflect(c)
        before={name:list(c.execute(select(table)).mappings()) for name,table in metadata.tables.items() if name!='alembic_version'}
    upgrade(pg_engine);upgrade(pg_engine)
    with pg_engine.connect() as c:
        for name,rows in before.items():assert list(c.execute(select(metadata.tables[name])).mappings())==rows
    with sessions() as s:
        instance=s.get(Instance,identifier)
        assert instance.self_service_policy_id is None and instance.policy_binding_sha256 is None
        assert not s.scalars(select(LabPolicy)).all()
