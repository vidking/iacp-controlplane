"""Admisión G y confidencialidad con señuelos; nunca conecta con MNG."""
from copy import deepcopy
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.catalog import catalog_digest
from iacp.commercial_contract import CommercialInstanceCreate,EXAMPLES
from iacp.models import BOOTSTRAP_TENANT_ID,CommercialIntent,Instance,Job,Operation,TerraformExecution
from iacp.security import Principal
from iacp.services.bootstrap_secrets import EphemeralSecrets,BootstrapSecretError,configured_key
from iacp.services.commercial import create,advance,binding
from iacp.services.instances import Conflict
from iacp.terraform import profiles
from tests.test_commercial_catalog import catalog_profile
from tests.test_instances import identities
from tests.test_terraform import FakeExecutor


class MemoryRedis:
    def __init__(self):self.values={};self.expirations={}
    def set(self,k,v,nx=False,ex=None):
        if nx and k in self.values:return False
        assert ex is not None
        self.values[k]=v;self.expirations[k]=ex;return True
    def get(self,k):return self.values.get(k)
    def delete(self,k):return self.values.pop(k,None) is not None


@pytest.fixture
def store():return EphemeralSecrets(MemoryRedis(),os.urandom(32),900)


@pytest.fixture
def commercial_config(catalog_profile,tmp_path,monkeypatch):
    raw=json.loads(catalog_profile.read_text())
    p=raw['profiles']['lab-dhcp']
    p.update(max_live_instances=1,max_vcpus=2,max_memory_mb=4096,max_disk_gb=100,
        max_total_vcpus=2,max_total_memory_mb=4096,max_total_disk_gb=100,
        max_concurrent_operations=1,self_service_enabled=True,guest_initialization='ubuntu-2404-lab-identity-v1')
    p['settings'].update(cloudinit_username='ubuntu',started=False)
    catalog_profile.write_text(json.dumps(raw))
    limits=tmp_path/'limits.json';limits.write_text(json.dumps({'tenants':{BOOTSTRAP_TENANT_ID:{
        'allowed_plans':['kvm-1','kvm-2'],'max_instances':1,'max_vcpus':2,'max_memory_mb':4096,
        'max_disk_gb':100,'max_concurrent_operations':1}}}))
    monkeypatch.setenv('IACP_COMMERCIAL_LIMITS_FILE',str(limits))
    monkeypatch.setenv('IACP_INSTANCE_EXECUTOR','terraform')
    monkeypatch.setenv('IACP_KVM_LIFECYCLE_ENABLED','1')
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    monkeypatch.setattr(profiles,'cluster_vmids',lambda:set())
    return catalog_profile


def specification(plan='kvm-1',**updates):
    body=deepcopy(EXAMPLES[0]);body.update(plan_id=plan,**updates)
    body['access']['root_password']='SENUELO-G6-NUNCA-PERSISTIR-4477'
    return CommercialInstanceCreate.model_validate(body)


@pytest.mark.parametrize('ttl',[0,59,3601,True])
def test_invalid_ttl_rejected(ttl):
    with pytest.raises(BootstrapSecretError):EphemeralSecrets(MemoryRedis(),os.urandom(32),ttl)


def test_ciphertext_aad_ttl_and_no_plaintext(store):
    secret='SENUELO-G6-NUNCA-PERSISTIR-4477'
    ref=store.put('tenant-a','instance-a',secret)
    assert len(ref)==43
    assert store.read(ref,'tenant-a','instance-a')==secret
    assert secret.encode() not in b''.join(store.redis.values.values())
    assert store._key not in b''.join(store.redis.values.values())
    assert set(store.redis.expirations.values())=={900}
    for tenant,instance in [('tenant-b','instance-a'),('tenant-a','instance-b')]:
        with pytest.raises(BootstrapSecretError,match='BINDING_INVALID'):store.read(ref,tenant,instance)
    k='iacp:bootstrap:v1:'+ref;raw=store.redis.values[k];store.redis.values[k]=raw[:-4]+b'AAAA'
    with pytest.raises(BootstrapSecretError):store.read(ref,'tenant-a','instance-a')
    store.delete(ref)
    with pytest.raises(BootstrapSecretError,match='EXPIRED_OR_MISSING'):store.read(ref,'tenant-a','instance-a')


def test_key_is_separate_raw_256_bits_private_file(tmp_path,monkeypatch):
    p=tmp_path/'aes';key=os.urandom(32);p.write_bytes(key);p.chmod(0o600)
    monkeypatch.setenv('IACP_BOOTSTRAP_AES_KEY_FILE',str(p));assert configured_key()==key
    p.chmod(0o644)
    with pytest.raises(BootstrapSecretError,match='KEY_UNAVAILABLE'):configured_key()
    assert str(BootstrapSecretError('SENUELO-G6-NUNCA-PERSISTIR'))=='BOOTSTRAP_DELIVERY_UNCONFIRMED'


@pytest.mark.parametrize('plan',['kvm-1','kvm-2'])
def test_plan_admitted_without_policy_and_secret_never_reaches_db_or_executor(sessions,commercial_config,store,plan,caplog):
    from iacp.terraform.lifecycle import execute_plan
    spec=specification(plan)
    published=[]
    with sessions() as s:
        accepted=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,'order-1',published.append,
            store,capacity=lambda *_:None)
        assert create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,'order-1',published.append,
            store,capacity=lambda *_:pytest.fail('Replay must not reserve')).model_dump()==accepted.model_dump()
        instance=s.get(Instance,accepted.instance_id);row=s.get(CommercialIntent,instance.id)
        assert instance.vcpus==(1 if plan=='kvm-1' else 2)
        assert instance.memory_mb==(2048 if plan=='kvm-1' else 4096)
        assert instance.disk_gb==(50 if plan=='kvm-1' else 100)
        assert instance.self_service_policy_id is None and instance.initial_access_ciphertext is None
        persisted=json.dumps([{c.name:getattr(o,c.name) for c in o.__table__.c}
            for o in [instance,row,s.get(Operation,accepted.operation_id),s.get(Job,accepted.job_id)]],default=str)
        assert spec.access.root_password.get_secret_value() not in persisted+caplog.text
        assert len(published)==1
    executor=FakeExecutor()
    execute_plan(accepted.job_id,1,sessions,executor)
    variables=executor.calls[0][4]
    assert 'iacp_root_password' not in variables
    assert spec.access.root_password.get_secret_value() not in json.dumps(variables)
    with sessions() as s:
        approval=advance(s,s.get(Instance,accepted.instance_id),lambda *_:None)
        assert approval.status=='queued' and approval.approved_by.startswith('commercial:')


def test_rejections_do_not_publish_or_persist_intents(sessions,commercial_config,store):
    published=[]
    with sessions() as s:
        for spec,capacity,code in [
            (specification('kvm-3'),lambda *_:None,'PLAN_NOT_AUTHORIZED'),
            (specification(),Mock(side_effect=Conflict('COMMERCIAL_PHYSICAL_CAPACITY_UNAVAILABLE')),'PHYSICAL_CAPACITY_UNAVAILABLE')]:
            with pytest.raises(Conflict,match=code):create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),spec,
                'rejected',published.append,store,capacity=capacity)
        assert not s.scalars(select(CommercialIntent)).all() and not s.scalars(select(Instance)).all()
    assert published==[] and store.redis.values=={}


def test_body_conflict_and_quota_keep_original_intent(sessions,commercial_config,store):
    principal=Principal(BOOTSTRAP_TENANT_ID,'crm','operator')
    with sessions() as s:
        result=create(s,principal,specification(),'same',lambda _:None,store,capacity=lambda *_:None)
        with pytest.raises(Conflict,match='another request'):
            create(s,principal,specification('kvm-2'),'same',lambda _:None,store,capacity=lambda *_:None)
        with pytest.raises(Conflict,match='QUOTA_EXCEEDED'):
            create(s,principal,specification(name='otra-vm'),'second',lambda _:None,store,capacity=lambda *_:None)
        assert s.get(Instance,result.instance_id).disk_gb==50


@pytest.mark.parametrize('plan_id', ['kvm-1','kvm-2','kvm-3','kvm-4'])
def test_commercial_catalog_ignores_legacy_size_caps_but_requires_explicit_budget(
        sessions, commercial_config, store, monkeypatch, plan_id):
    from iacp.catalog import resolve_plan
    raw=json.loads(commercial_config.read_text());profile=raw['profiles']['lab-dhcp']
    profile.update(max_vcpus=4,max_memory_mb=4096,max_disk_gb=40,
        max_total_vcpus=8,max_total_memory_mb=8192,max_total_disk_gb=80)
    commercial_config.write_text(json.dumps(raw))
    from pathlib import Path
    limits=Path(os.environ['IACP_COMMERCIAL_LIMITS_FILE'])
    limits.write_text(json.dumps({'tenants':{BOOTSTRAP_TENANT_ID:{
        'allowed_plans':['kvm-1','kvm-2','kvm-3','kvm-4'],'max_instances':1,
        'max_vcpus':8,'max_memory_mb':16384,'max_disk_gb':400,'max_concurrent_operations':1}}}))
    with sessions() as s:
        result=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),specification(plan_id),
            'catalog-test',lambda _:None,store,capacity=lambda *_:None)
        instance=s.get(Instance,result.instance_id);plan=resolve_plan(plan_id)
        assert (instance.vcpus,instance.memory_mb,instance.disk_gb)==(plan.vcpus,plan.memory_mb,plan.disk_gb)
        assert instance.self_service_policy_id is None


def test_commercial_reservation_rejects_resources_outside_catalog(sessions,commercial_config):
    from uuid import uuid4
    from iacp.terraform.config import PlanError
    with sessions() as s:
        instance=Instance(id=str(uuid4()),tenant_id=BOOTSTRAP_TENANT_ID,name='invalid-shape',
            image='ubuntu-2404',vcpus=3,memory_mb=4096,disk_gb=100,network_profile='lab-dhcp',
            commercial_revision='commercial-g',status='pending',executor='terraform')
        with pytest.raises(PlanError,match='RESOURCES_OUTSIDE_CATALOG'):
            profiles.reserve_profile(s,instance)


def test_commercial_availability_uses_tenant_budget_instead_of_legacy_caps(
        sessions,commercial_config,monkeypatch):
    from iacp.services.catalog import plans_for
    from iacp.services import commercial,bootstrap_secrets
    raw=json.loads(commercial_config.read_text());raw['profiles']['lab-dhcp'].update(
        max_vcpus=4,max_memory_mb=4096,max_disk_gb=40,max_total_disk_gb=80)
    commercial_config.write_text(json.dumps(raw))
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(bootstrap_secrets,'configured_key',lambda:b'x'*32)
    monkeypatch.setattr(commercial,'live_capacity',lambda *_:None)
    with sessions() as s:
        rows=plans_for(s,BOOTSTRAP_TENANT_ID)
    assert all(row['availability']['available'] for row in rows[:2])
    assert all(not row['availability']['available'] and
        'COMMERCIAL_PLAN_NOT_AUTHORIZED' in row['availability']['reasons'] for row in rows[2:])


def test_commercial_password_adapter_is_forbidden_even_if_legacy_data_corrupted():
    from iacp.terraform.config import Allocation,PlanError
    from tests.test_terraform import allocation_data
    allocation=Allocation.model_validate(allocation_data())
    data=SimpleNamespace(image='test-image',name='safe',vcpus=1,memory_mb=2048,disk_gb=50,
        commercial_revision='commercial-g',initial_access_ciphertext='ciphertext-historico')
    with pytest.raises(PlanError,match='COMMERCIAL_PASSWORD_IN_TERRAFORM_FORBIDDEN'):allocation.variables(data)


@pytest.mark.parametrize('kind',['disk','memory','cpu','ceph'])
def test_physical_budget_and_ceph_health_are_admission_requirements(sessions,commercial_config,monkeypatch,kind):
    from iacp.services.commercial import live_capacity
    from iacp.services.catalog import authorized_profile
    from iacp.catalog import resolve_plan
    from iacp.services.capacity_readonly import CapacityReadOnly
    def read(method,path):
        assert method=='GET'
        if '/storage/' in path:return {'active':1,'enabled':1,'type':'rbd','avail':(1 if kind=='disk' else 1024)*1024**3}
        if '/ceph/' in path:return {'health':{'status':'HEALTH_WARN' if kind=='ceph' else 'HEALTH_OK'}}
        return {'memory':{'free':(1 if kind=='memory' else 16)*1024**3},'cpuinfo':{'cpus':1 if kind=='cpu' else 16},'cpu':0.1}
    monkeypatch.setattr(CapacityReadOnly,'configured',lambda:SimpleNamespace(_request=read))
    with sessions() as s:
        with pytest.raises(Conflict):live_capacity(s,authorized_profile(BOOTSTRAP_TENANT_ID),resolve_plan('kvm-2'))
        assert not s.scalars(select(Instance)).all()


def test_commercial_openapi_and_runtime_keep_old_post_until_explicit_transition(monkeypatch):
    from iacp.main import create_app
    schema=create_app().openapi();assert schema['paths']['/v1/instances']['post']['requestBody']['content']['application/json']['schema']['$ref'].endswith('/InstanceCreate')
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    schema=create_app().openapi();media=schema['paths']['/v1/instances']['post']['requestBody']['content']['application/json']
    assert media['schema']['$ref'].endswith('/CommercialInstanceCreate')
    for sample in media['examples'].values():CommercialInstanceCreate.model_validate(sample['value'])


def test_available_catalog_requires_real_capacity_and_active_contract(sessions,commercial_config,tmp_path,monkeypatch):
    from iacp.services import commercial
    from iacp.services.catalog import plans_for,images_for
    p=tmp_path/'key';p.write_bytes(os.urandom(32));p.chmod(0o600)
    monkeypatch.setenv('IACP_BOOTSTRAP_AES_KEY_FILE',str(p))
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(commercial,'live_capacity',lambda *_:None)
    with sessions() as s:
        plans=plans_for(s,BOOTSTRAP_TENANT_ID)
        assert [p['plan_id'] for p in plans if p['availability']['available']]==['kvm-1','kvm-2']
        assert images_for(BOOTSTRAP_TENANT_ID,s)[0]['availability']['available']
        monkeypatch.setattr(commercial,'live_capacity',Mock(side_effect=Conflict('COMMERCIAL_PHYSICAL_CAPACITY_UNAVAILABLE')))
        assert not any(p['availability']['available'] for p in plans_for(s,BOOTSTRAP_TENANT_ID))


def test_https_admission_rejections_are_audited_without_secrets(sessions,commercial_config,store,auth,identities,monkeypatch,caplog):
    import logging
    from fastapi.testclient import TestClient
    from iacp.main import create_app
    from iacp.db import get_session
    from iacp.api.dependencies import get_instance_publisher
    from iacp.services import commercial
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(EphemeralSecrets,'configured',classmethod(lambda _:store))
    monkeypatch.setattr(commercial,'live_capacity',lambda *_:None)
    app=create_app()
    def session_override():
        with sessions() as s:yield s
    app.dependency_overrides[get_session]=session_override
    published=[]
    app.dependency_overrides[get_instance_publisher]=lambda:published.append
    caplog.set_level(logging.INFO,logger='uvicorn.error')
    body=deepcopy(EXAMPLES[0]);body['access']['root_password']='SENUELO-G6-NUNCA-PERSISTIR-4477'
    headers={**auth,'Idempotency-Key':'https-create'}
    with TestClient(app,base_url='https://fixture') as client:
        assert client.post('/v1/instances',json=body).status_code==401
        assert client.post('/v1/instances',json=body,headers={**identities['tenant-a-reader']['headers'],'Idempotency-Key':'reader'}).status_code==403
        assert client.post('/v1/instances',json={**body,'vcpus':1},headers=headers).status_code==422
        other=client.post('/v1/instances',json=body,headers={**identities['tenant-b-operator']['headers'],'Idempotency-Key':'other'})
        assert other.status_code==409
        result=client.post('/v1/instances',json=body,headers=headers)
        assert result.status_code==202,result.text
        assert len(published)==1
        assert client.post('/v1/instances',json=body,headers=headers).json()==result.json()
        assert len(published)==1
        assert body['access']['root_password'] not in result.text+caplog.text
        assert 'admission_rejected' in caplog.text
        instance=client.get('/v1/instances/'+result.json()['instance_id'],headers=auth)
        assert instance.status_code==200,instance.text
        assert instance.json()['commercial_context']['order_id']==body['commercial_context']['order_id']
        assert 'customer_id' not in instance.json()['commercial_context']


def test_dispatch_timeout_is_not_falsely_recorded_as_unexecuted_rejection(sessions,commercial_config,store,auth,monkeypatch,caplog):
    import logging
    from fastapi.testclient import TestClient
    from iacp.main import create_app
    from iacp.db import get_session
    from iacp.api.dependencies import get_instance_publisher
    from iacp.services import commercial
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(EphemeralSecrets,'configured',classmethod(lambda _:store))
    monkeypatch.setattr(commercial,'live_capacity',lambda *_:None)
    app=create_app()
    def session_override():
        with sessions() as s:yield s
    app.dependency_overrides[get_session]=session_override
    app.dependency_overrides[get_instance_publisher]=lambda:Mock(side_effect=TimeoutError('fixture'))
    caplog.set_level(logging.INFO,logger='uvicorn.error')
    with TestClient(app,base_url='https://fixture') as client:
        result=client.post('/v1/instances',json=EXAMPLES[0],headers={**auth,'Idempotency-Key':'lost-dispatch'})
        assert result.status_code==503
    records=[json.loads(r.message) for r in caplog.records if r.message.startswith('{')]
    audit=[r for r in records if r['event']=='admission_dispatch_unconfirmed']
    assert audit and audit[0]['admitted'] is True and audit[0]['executed'] is None
    assert not [r for r in records if r['event']=='admission_rejected']
