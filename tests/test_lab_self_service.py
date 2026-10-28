"""Pruebas retiradas junto con el motor operativo LabPolicy."""
import pytest

pytest.skip("El flujo LabPolicy fue retirado; usar las regresiones del contrato Core", allow_module_level=True)

from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_apply_publisher, get_deletion_publisher, get_power_client, get_power_publisher
from iacp.models import BOOTSTRAP_TENANT_ID, DeletionExecution, Instance, LabPolicy, Operation, TerraformApply, TerraformExecution, utcnow
from iacp.services.lab import after_plan, advance_deletion
from iacp.terraform.apply import execute_apply
from iacp.terraform.config import PlanError
from iacp.terraform.deletion import execute_deletion
from iacp.terraform.lifecycle import execute_plan
from iacp.lifecycle.execution import execute_power
from tests.test_deletion import FakeDeletion
from tests.test_dhcp_profiles import policy, profile_data
from tests.test_instances import SPEC, identities
from tests.test_terraform import FakeExecutor, terraform_mode
from tests.power_helpers import FakePower, managed_instance

POLICY_SPEC={"profiles":["lab-dhcp"], "actions":["create","start","shutdown","reboot","delete"],
    "max_instances":3,"max_vcpus":8,"max_memory_mb":8192,"max_disk_gb":80,
    "max_concurrent_operations":2,"valid_for_seconds":3600,"change_reference":"SYNTHETIC-LAB-REVIEW"}


@pytest.fixture
def lab(client, auth, application, policy, terraform_mode, monkeypatch, tmp_path):
    data=profile_data();p=data['profiles']['lab-dhcp']
    p.update(self_service_enabled=True,max_live_instances=3,max_concurrent_operations=2,
        max_total_vcpus=8,max_total_memory_mb=8192,max_total_disk_gb=80,target_nodes=['mng01','mng02'])
    p['settings']['network_bridge']='vmbr0';policy.write_text(json.dumps(data))
    for key in ['IACP_LAB_SELF_SERVICE_ENABLED','IACP_TERRAFORM_APPLY_ENABLED','IACP_TERRAFORM_DELETE_ENABLED','IACP_KVM_LIFECYCLE_ENABLED']:
        monkeypatch.setenv(key,'1')
    monkeypatch.setenv('IACP_LIFECYCLE_INSTANCE_IDS','');monkeypatch.setenv('IACP_DISPOSABLE_INSTANCE_IDS','')
    entries=[];headers={}
    for subject,role in [('independent-lab-reviewer','approver'),('bootstrap','approver'),('lab-reader','reader')]:
        token='synthetic-'+subject+'-x'*32
        entries.append({'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'tenant_id':BOOTSTRAP_TENANT_ID,'subject':subject,'role':role})
        headers[subject]={'Authorization':'Bearer '+token}
    path=tmp_path/'lab-credentials.json';path.write_text(json.dumps(entries));monkeypatch.setenv('IACP_TENANT_CREDENTIALS_FILE',str(path))
    published=Mock();deletions=Mock();powers=Mock();power=FakePower()
    application.dependency_overrides[get_apply_publisher]=lambda:published
    application.dependency_overrides[get_deletion_publisher]=lambda:deletions
    application.dependency_overrides[get_power_publisher]=lambda:powers
    application.dependency_overrides[get_power_client]=lambda:lambda:power
    r=client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'policy-one'},json=POLICY_SPEC)
    assert r.status_code==201,r.text
    proposal=r.json()
    r=client.post('/v1/lab/policies/'+proposal['policy_id']+'/approve',headers=headers['independent-lab-reviewer'],json={'policy_sha256':proposal['policy_sha256']})
    assert r.status_code==200,r.text
    return SimpleNamespace(id=proposal['policy_id'],proposal=proposal,path=policy,reviewer=headers['independent-lab-reviewer'],
        same_actor=headers['bootstrap'],reader=headers['lab-reader'],published=published,deletions=deletions,power=power)


def create(client, auth, lab, number=1, **changes):
    return client.post('/v1/instances',headers={**auth,'Idempotency-Key':'lab-create-'+str(number)},json={**SPEC,
        'name':'lab-'+str(number),'network_profile':'lab-dhcp','self_service_policy_id':lab.id,**changes})


class LabExecutor:
    """Simulated provider with a real persisted mutation fence."""
    def __init__(self,failure=None):self.calls=[];self.failure=failure
    def prepare_apply(self,*args):pass
    def prepare_reconcile(self,*args):return {'plan_sha256':args[2],'execution_enabled':False}
    def apply(self,identifier,attempt,sha,key,allocation,variables,cancelled,before_mutation):
        before_mutation();self.calls.append(identifier)
        if self.failure:raise PlanError(self.failure)
        return self.observe(identifier,attempt,allocation,variables,cancelled)
    def observe(self,identifier,attempt,allocation,variables,cancelled):
        cancelled();v=variables['instance']
        return {'simulated':False,'vmid':v['vm_id'],'node':v['node_name'],'name':v['vm_name'],
            'power_state':'stopped','observation':{'postconditions_verified':True,'state_present':True,'proxmox_present':True}}


def provision(accepted,sessions,lab,executor=None):
    execute_plan(accepted['job_id'],1,sessions,FakeExecutor())
    after_plan(accepted['job_id'],sessions,lab.published)
    with sessions() as s:approval=s.scalar(select(TerraformApply).where(TerraformApply.instance_id==accepted['instance_id']))
    assert approval is not None
    execute_apply(approval.id,sessions,executor or LabExecutor())
    return approval.id


def test_three_independent_instances_delegated_without_per_vm_human_review(client,auth,lab,sessions):
    ids=[];vmids=[];nodes=[]
    for n in range(1,4):
        r=create(client,auth,lab,n);assert r.status_code==202,r.text
        accepted=r.json();ids.append(accepted['instance_id'])
        assert create(client,auth,lab,n).json()==accepted
        provision(accepted,sessions,lab)
        with sessions() as s:
            e=s.get(TerraformExecution,accepted['instance_id']);vmids.append(e.vmid);nodes.append(e.allocation['node_name'])
            approval=s.scalar(select(TerraformApply).where(TerraformApply.instance_id==e.instance_id))
            assert approval.approved_by=='policy:'+lab.id and approval.status=='completed'
            assert s.get(Instance,e.instance_id).status=='ready'
    assert len(set(ids))==len(set(vmids))==3 and nodes==['mng01','mng02','mng01']
    assert create(client,auth,lab,4).json()['detail']=='LAB_QUOTA_EXCEEDED:instances'
    assert client.get('/v1/lab/usage',headers=auth).json()['usage']['instances']==3
    result=client.get('/v1/instances?limit=1&offset=1&status=ready&network_profile=lab-dhcp',headers=auth)
    assert len(result.json())==1 and result.headers['X-Total-Count']=='3'


@pytest.mark.parametrize('change,code',[({'vcpus':9},'LAB_QUOTA_EXCEEDED:vcpus'),({'node_preference':'foreign'},'PROFILE_NODE_NOT_AUTHORIZED'),
    ({'network_profile':'unknown'},'LAB_PROFILE_NOT_AUTHORIZED')])
def test_request_outside_profile_is_rejected(client,auth,lab,change,code):
    r=create(client,auth,lab,**change);assert r.status_code in [409,503] and r.json()['detail']==code


def test_concurrency_limit_retains_uncertain_reservations(client,auth,lab,sessions):
    a=create(client,auth,lab,1).json();b=create(client,auth,lab,2).json()
    assert create(client,auth,lab,3).json()['detail']=='LAB_QUOTA_EXCEEDED:concurrent_operations'
    provision(a,sessions,lab,LabExecutor('CONTROLLED_FAILURE'))
    assert create(client,auth,lab,3).json()['detail']=='LAB_QUOTA_EXCEEDED:concurrent_operations'
    assert client.post('/v1/instances/'+b['instance_id']+'/retry-provision',headers={**auth,'Idempotency-Key':'stale'},
        json={'expected_attempt':9}).status_code==409


def test_independent_approval_digest_and_role_required(client,auth,lab):
    path='/v1/lab/policies/'+lab.id+'/approve';body={'policy_sha256':lab.proposal['policy_sha256']}
    assert client.post(path,headers=auth,json=body).status_code==403
    assert client.post(path,headers=lab.same_actor,json=body).json()['detail']=='INDEPENDENT_APPROVAL_REQUIRED'
    assert client.post(path,headers=lab.reviewer,json={'policy_sha256':'0'*64}).status_code==409
    assert client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'policy-one'},json=POLICY_SPEC).json()['policy_id']==lab.id
    assert client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'policy-one'},json={**POLICY_SPEC,'max_instances':2}).status_code==409
    assert create(client,lab.reader,lab).status_code==403


@pytest.mark.parametrize('mutation',['expired','revoked','profile','allocation','resources','tenant','flag'])
def test_policy_or_ownership_change_fails_before_mutation(client,auth,lab,sessions,monkeypatch,mutation):
    a=create(client,auth,lab).json();execute_plan(a['job_id'],1,sessions,FakeExecutor());after_plan(a['job_id'],sessions,lab.published)
    with sessions() as s:
        approval=s.scalar(select(TerraformApply).where(TerraformApply.instance_id==a['instance_id']))
        row=s.get(LabPolicy,lab.id);i=s.get(Instance,a['instance_id']);e=s.get(TerraformExecution,i.id)
        if mutation=='expired':row.expires_at=utcnow()-timedelta(seconds=1)
        if mutation=='revoked':row.status='revoked'
        if mutation=='resources':i.vcpus=4
        if mutation=='allocation':e.allocation={**e.allocation,'template_vm_id':106}
        if mutation=='tenant':i.tenant_id='00000000-0000-0000-0000-000000000002'
        s.commit()
    if mutation=='profile':
        data=json.loads(lab.path.read_text());data['profiles']['lab-dhcp']['settings']['cloudinit_username']='changed';lab.path.write_text(json.dumps(data))
    if mutation=='flag':monkeypatch.setenv('IACP_LAB_SELF_SERVICE_ENABLED','0')
    fake=LabExecutor();execute_apply(approval.id,sessions,fake)
    assert not fake.calls
    with sessions() as s:assert s.get(TerraformApply,approval.id).started_at is None


def test_uncertain_reconcile_survives_revocation_and_closed_admission(client,auth,lab,sessions,monkeypatch):
    a=create(client,auth,lab).json();fake=LabExecutor('CONTROLLED_FAILURE');approval_id=provision(a,sessions,lab,fake)
    assert len(fake.calls)==1
    assert client.post('/v1/lab/policies/'+lab.id+'/revoke',headers=lab.reviewer,json={}).status_code==200
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0');monkeypatch.setenv('IACP_LAB_SELF_SERVICE_ENABLED','0')
    r=client.post('/v1/instances/'+a['instance_id']+'/reconcile',headers=auth,json={});assert r.status_code==202,r.text
    lab.published.assert_called_with(approval_id,'reconcile')
    class Observer(LabExecutor):
        def observe(self,*args):
            current=client.get('/v1/operations/'+a['operation_id'],headers=auth).json()
            assert current['status']=='failed' and current['normalized_status']=='running'
            assert current['provisioning']['phase']=='running' and 'Conciliación en curso' in current['error_message']
            return super().observe(*args)
    observer=Observer();execute_apply(approval_id,sessions,observer,reconcile=True)
    assert not observer.calls
    with sessions() as s:assert s.get(Instance,a['instance_id']).status=='ready'


def test_self_service_delete_uses_existing_fence_and_preserves_history(client,auth,lab,sessions):
    a=create(client,auth,lab).json();provision(a,sessions,lab);path='/v1/instances/'+a['instance_id']
    headers={**auth,'Idempotency-Key':'lab-delete'}
    r=client.request('DELETE',path,headers=headers,json={});assert r.status_code==202,r.text
    op=r.json()['operation_id'];fake=FakeDeletion()
    execute_deletion(op,'plan',sessions,fake)
    with sessions() as s:advance_deletion(s,s.get(Instance,a['instance_id']),op,lab.deletions)
    execute_deletion(op,'apply',sessions,fake);execute_deletion(op,'apply',sessions,fake)
    assert fake.calls==['plan','apply','observe']
    assert client.request('DELETE',path,headers=headers,json={}).json()['status']=='completed'
    with sessions() as s:
        assert s.get(Instance,a['instance_id']).status=='deleted'
        assert s.get(DeletionExecution,op).approved_by=='policy:'+lab.id
    assert client.get('/v1/lab/usage',headers=auth).json()['usage']['instances']==0
    assert len(client.get('/v1/operations?instance_id='+a['instance_id'],headers=auth).json())==2


def test_self_service_delete_without_key_generates_one(client,auth,lab,sessions):
    a=create(client,auth,lab,77).json();provision(a,sessions,lab)
    response=client.request('DELETE','/v1/instances/'+a['instance_id'],headers=auth)
    assert response.status_code==202,response.text
    generated=response.headers.get('Idempotency-Key')
    assert generated and generated.startswith('http-delete-')
    with sessions() as session:
        assert session.get(Operation,response.json()['operation_id']).idempotency_key==generated


def test_lifecycle_and_historical_scope_stay_separate(client,auth,lab,sessions):
    a=create(client,auth,lab).json();provision(a,sessions,lab);path='/v1/instances/'+a['instance_id']
    for n,action in enumerate(['start','reboot','shutdown']):
        r=client.post(path+'/power/'+action,headers={**auth,'Idempotency-Key':'power-'+str(n)},json={})
        assert r.status_code==202,r.text
        execute_power(r.json()['operation_id'],sessions,lab.power)
        assert client.get('/v1/operations/'+r.json()['operation_id'],headers=auth).json()['status']=='completed'
    historical=managed_instance(sessions,BOOTSTRAP_TENANT_ID,vmid=106,name='protected-historical')
    r=client.post('/v1/instances/'+historical+'/power/start',headers={**auth,'Idempotency-Key':'protected-start'},json={})
    assert r.status_code==409 and r.json()['detail']=='POWER_INSTANCE_NOT_AUTHORIZED'
    assert client.request('DELETE','/v1/instances/'+historical,headers={**auth,'Idempotency-Key':'protected-delete'},json={}).status_code==409


def test_policy_cannot_expand_server_ceiling_or_retroactively_adopt(client,auth,lab,sessions):
    r=client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'over-quota'},json={**POLICY_SPEC,'max_instances':4})
    assert r.status_code==409 and r.json()['detail']=='LAB_POLICY_EXCEEDS_SERVER_LIMIT'
    historical=managed_instance(sessions,BOOTSTRAP_TENANT_ID,vmid=30050,name='preexisting')
    assert client.post('/v1/instances/'+historical+'/provision',headers=auth,json={}).json()['detail']=='LAB_POLICY_REQUIRED'


def test_cross_tenant_inventory_policy_commands_and_catalog_are_isolated(client,auth,lab,identities):
    a=create(client,auth,lab).json();other=identities['tenant-b-operator']['headers']
    assert client.get('/v1/lab/policies/'+lab.id,headers=other).status_code==404
    assert client.get('/v1/lab/policies',headers=other).json()==[]
    assert client.get('/v1/profiles',headers=other).json()==[]
    assert client.get('/v1/instances/'+a['instance_id'],headers=other).status_code==404
    assert client.get('/v1/operations/'+a['operation_id'],headers=other).status_code==404
    assert client.request('DELETE','/v1/instances/'+a['instance_id'],headers={**other,'Idempotency-Key':'foreign'},json={}).status_code==404
    assert create(client,other,lab).json()['detail']=='LAB_POLICY_NOT_AUTHORIZED'


@pytest.mark.parametrize('field,maximum,code',[('max_vcpus',3,'vcpus'),('max_memory_mb',3072,'memory_mb'),('max_disk_gb',30,'disk_gb')])
def test_aggregate_quota_cannot_be_bypassed_by_another_profile(client,auth,lab,field,maximum,code):
    data=json.loads(lab.path.read_text());data['profiles']['second']=data['profiles']['lab-dhcp'];lab.path.write_text(json.dumps(data))
    body={**POLICY_SPEC,'profiles':['lab-dhcp','second'],field:maximum}
    r=client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'aggregate'},json=body);assert r.status_code==201,r.text
    row=r.json()
    assert client.post('/v1/lab/policies/'+row['policy_id']+'/approve',headers=lab.reviewer,json={'policy_sha256':row['policy_sha256']}).status_code==200
    assert create(client,auth,lab,1,self_service_policy_id=row['policy_id']).status_code==202
    r=create(client,auth,lab,2,network_profile='second',self_service_policy_id=row['policy_id'])
    assert r.status_code==409 and r.json()['detail']=='LAB_QUOTA_EXCEEDED:'+code


def test_explicit_plan_endpoint_never_auto_deletes(client,auth,lab,sessions):
    a=create(client,auth,lab).json();provision(a,sessions,lab)
    r=client.post('/v1/instances/'+a['instance_id']+'/provisioning/deletion-plan',headers={**auth,'Idempotency-Key':'only-plan'},json={})
    op=r.json()['operation_id'];fake=FakeDeletion();execute_deletion(op,'plan',sessions,fake)
    with sessions() as s:
        advance_deletion(s,s.get(Instance,a['instance_id']),op,lab.deletions)
        assert s.get(DeletionExecution,op).approved_by is None
    assert fake.calls==['plan']


def test_independent_renewal_keeps_membership_and_records_review(client,auth,lab,sessions):
    a=create(client,auth,lab).json()
    with sessions() as s:
        row=s.get(LabPolicy,lab.id);approved_at=row.approved_at
        row.expires_at=utcnow()-timedelta(seconds=1);s.commit()
    path='/v1/lab/policies/'+lab.id
    body={'policy_sha256':lab.proposal['policy_sha256']}
    assert client.post(path+'/renew',headers=lab.same_actor,json=body).status_code==409
    r=client.post(path+'/renew',headers=lab.reviewer,json=body)
    assert r.status_code==200 and [e['action'] for e in r.json()['review_history']]==['proposed','approved','renewed']
    with sessions() as s:assert s.get(LabPolicy,lab.id).approved_at==approved_at
    provision(a,sessions,lab)
    client.post(path+'/revoke',headers=lab.reviewer,json={})
    assert client.post(path+'/renew',headers=lab.reviewer,json=body).status_code==409


def test_policy_review_can_precede_activation_while_infrastructure_stays_drained(client,auth,lab,monkeypatch):
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0');monkeypatch.setenv('IACP_LAB_SELF_SERVICE_ENABLED','0')
    r=client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'pre-activation'},json=POLICY_SPEC)
    assert r.status_code==201,r.text
    proposal=r.json();path='/v1/lab/policies/'+proposal['policy_id']
    r=client.post(path+'/approve',headers=lab.reviewer,json={'policy_sha256':proposal['policy_sha256']})
    assert r.status_code==200,r.text
    assert create(client,auth,lab).status_code==503
    assert client.post(path+'/revoke',headers=lab.reviewer,json={}).status_code==200


def test_self_service_profile_cannot_bypass_tenant_policy_quotas_by_omitting_policy(client,auth,lab,sessions):
    r=create(client,auth,lab,self_service_policy_id=None)
    assert r.status_code==409 and r.json()['detail']=='LAB_POLICY_REQUIRED'
    with sessions() as s:
        assert not s.scalars(select(Instance)).all()
        assert not s.scalars(select(TerraformExecution)).all()
        assert not s.scalars(select(Operation)).all()
