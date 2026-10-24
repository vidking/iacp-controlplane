"""Synthetic resources only. These tests never delete a real VM or remote state."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_deletion_publisher, get_power_client
from iacp.models import (BOOTSTRAP_TENANT_ID, DeletionExecution, Instance, Job, Operation,
                         OperationEvent, TerraformExecution, utcnow)
from iacp.terraform.config import Allocation, PlanError
from iacp.terraform.deletion import execute_deletion
from iacp.terraform.deletion_executor import verify_delete_plan
from iacp.terraform.executor import ADDRESS
from iacp.terraform.locks import instance_lock
from iacp.terraform.runner import CommandRunner
from tests.power_helpers import FakePower, managed_instance
from tests.test_instances import identities
from tests.test_terraform import allocation_data


class FakeDeletion:
    def __init__(self, failure=None):
        self.calls, self.failure, self.interrupt = [], failure, False

    def plan_deletion(self, ctx, check):
        check();self.calls.append('plan')
        return {'plan_sha256': 'b'*64, 'state_before': {'serial': 1}}

    def apply_deletion(self, ctx, sha, check, before_mutation):
        check();before_mutation();self.calls.append('apply')
        if self.interrupt:
            raise KeyboardInterrupt()
        if self.failure:
            raise PlanError(self.failure)
        return self.reconcile_deletion(ctx, sha, check)

    def reconcile_deletion(self, ctx, sha, check):
        check();self.calls.append('observe')
        if self.failure:
            raise PlanError(self.failure)
        return {'deleted': True, 'state_present': False, 'proxmox_present': False}


def seed(sessions, tenant=BOOTSTRAP_TENANT_ID, vmid=30000, name='fixture-disposable'):
    identifier = managed_instance(sessions, tenant, vmid=vmid, name=name)
    with sessions() as session:
        execution = session.get(TerraformExecution, identifier)
        execution.allocation = allocation_data(vmid, image='synthetic-image')
        execution.state_key = f'iacp/lab/tenants/{tenant}/instances/{identifier}/terraform.tfstate'
        session.commit()
    return identifier


@pytest.fixture
def disposable(sessions, application, monkeypatch, tmp_path):
    identifier = seed(sessions)
    monkeypatch.setenv('IACP_TERRAFORM_DELETE_ENABLED','1');monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    monkeypatch.setenv('IACP_DISPOSABLE_INSTANCE_IDS',identifier)
    token = 'synthetic-reviewer-'+'r'*40
    p=tmp_path/'registry.json';p.write_text(json.dumps([{'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
        'tenant_id':BOOTSTRAP_TENANT_ID,'subject':'deletion-reviewer','role':'approver'}]))
    monkeypatch.setenv('IACP_TENANT_CREDENTIALS_FILE',str(p))
    publisher=Mock();application.dependency_overrides[get_deletion_publisher]=lambda:publisher
    application.dependency_overrides[get_power_client]=lambda:lambda:FakePower()
    return SimpleNamespace(id=identifier,path='/v1/instances/'+identifier,key='delete-pilot',publisher=publisher,
        reviewer={'Authorization':'Bearer '+token})


def plan(client, auth, fixture, key=None):
    return client.post(fixture.path+'/provisioning/deletion-plan',headers={**auth,'Idempotency-Key':key or fixture.key})


def prepare(client, auth, fixture, sessions):
    response=plan(client,auth,fixture);assert response.status_code==202,response.text
    operation=response.json();execute_deletion(operation['operation_id'],'plan',sessions,FakeDeletion())
    body={'operation_id':operation['operation_id'],'plan_sha256':'b'*64}
    r=client.post(fixture.path+'/provisioning/deletion-approval',headers={**fixture.reviewer,'Idempotency-Key':'review-delete'},json=body)
    assert r.status_code==201,r.text
    return operation,body


def delete(client, auth, fixture, body, key=None):
    return client.request('DELETE',fixture.path,headers={**auth,'Idempotency-Key':key or fixture.key},json=body)


def test_approved_delete_retains_tombstone_audit_and_provisioning(client,auth,sessions,disposable):
    with sessions() as s:before=s.get(Instance,disposable.id).result
    operation,body=prepare(client,auth,disposable,sessions)
    assert delete(client,auth,disposable,body).status_code==202
    fake=FakeDeletion();execute_deletion(operation['operation_id'],'apply',sessions,fake)
    execute_deletion(operation['operation_id'],'apply',sessions,fake)
    assert fake.calls.count('apply')==1
    assert delete(client,auth,disposable,body).json()['status']=='completed'
    observed=client.get('/v1/operations/'+operation['operation_id'],headers=auth).json()
    assert observed['deletion']['phase']=='deleted' and observed['status']=='completed'
    assert sum(e['code']=='delete_completed' for e in observed['events'])==1
    with sessions() as s:
        instance=s.get(Instance,disposable.id)
        assert instance.status=='deleted' and instance.result==before and instance.vmid==30000
        assert s.get(TerraformExecution,instance.id).status=='planned'


@pytest.mark.parametrize('which',['delete','apply','allowlist'])
def test_feature_gates_fail_closed(client,auth,disposable,monkeypatch,which):
    monkeypatch.delenv({'delete':'IACP_TERRAFORM_DELETE_ENABLED','apply':'IACP_TERRAFORM_APPLY_ENABLED','allowlist':'IACP_DISPOSABLE_INSTANCE_IDS'}[which])
    assert plan(client,auth,disposable).status_code==409
    disposable.publisher.assert_not_called()


@pytest.mark.parametrize('vmid',[104,106,29999])
def test_controlplane_historical_and_non_disposable_ids_protected(sessions,client,auth,disposable,monkeypatch,vmid):
    with sessions() as s:s.get(Instance,disposable.id).vmid=vmid;s.commit()
    assert plan(client,auth,disposable).status_code==409
    disposable.publisher.assert_not_called()


def test_plan_and_command_idempotency(client,auth,sessions,disposable):
    op=plan(client,auth,disposable).json()
    assert plan(client,auth,disposable).json()==op
    execute_deletion(op['operation_id'],'plan',sessions,FakeDeletion())
    progress=client.get('/v1/operations/'+op['operation_id'],headers=auth).json()['deletion']
    assert progress['status']=='planned' and progress['plan_sha256']=='b'*64
    assert plan(client,auth,disposable,'other-operation').status_code==409
    assert delete(client,auth,disposable,{'operation_id':op['operation_id'],'plan_sha256':'b'*64}).status_code==409


def test_approver_role_independence_and_exact_plan(client,auth,sessions,disposable):
    op=plan(client,auth,disposable).json();execute_deletion(op['operation_id'],'plan',sessions,FakeDeletion())
    body={'operation_id':op['operation_id'],'plan_sha256':'b'*64}
    assert client.post(disposable.path+'/provisioning/deletion-approval',headers={**auth,'Idempotency-Key':'bad'},json=body).status_code==403
    with sessions() as s:s.get(Operation,op['operation_id']).requested_by='deletion-reviewer';s.commit()
    r=client.post(disposable.path+'/provisioning/deletion-approval',headers={**disposable.reviewer,'Idempotency-Key':'review'},json=body)
    assert r.status_code==409 and r.json()['detail']=='INDEPENDENT_APPROVAL_REQUIRED'


def test_approval_expiry_and_changed_sha_never_publish(client,auth,sessions,disposable):
    op,body=prepare(client,auth,disposable,sessions);disposable.publisher.reset_mock()
    assert delete(client,auth,disposable,{**body,'plan_sha256':'c'*64}).status_code==409
    assert delete(client,auth,disposable,body,key='wrong-key').status_code==409
    with sessions() as s:s.get(DeletionExecution,op['operation_id']).expires_at=utcnow()-timedelta(seconds=1);s.commit()
    assert delete(client,auth,disposable,body).status_code==409
    disposable.publisher.assert_not_called()


@pytest.mark.parametrize('path_method',[('','DELETE'),('/provisioning/deletion-plan','POST'),('/provisioning/deletion-approval','POST')])
def test_auth_and_tenant_boundary(client,auth,sessions,disposable,identities,path_method):
    path,method=path_method
    body={'operation_id':str(uuid4()),'plan_sha256':'a'*64} if path!=' /provisioning/deletion-plan' and not path.endswith('deletion-plan') else {}
    url=disposable.path+path
    assert client.request(method,url,headers={'Idempotency-Key':'boundary'},json=body).status_code==401
    who=identities['tenant-b-operator']['headers'] if not path.endswith('approval') else disposable.reviewer
    if not path.endswith('approval'):
        assert client.request(method,url,headers={**who,'Idempotency-Key':'boundary'},json=body).status_code==(403 if path else 404)
    reader=identities['tenant-a-reader']['headers']
    assert client.request(method,url,headers={**reader,'Idempotency-Key':'boundary'},json=body).status_code==403


def test_power_and_same_instance_concurrency_blocked(client,auth,sessions,disposable,monkeypatch):
    with instance_lock(sessions.kw['bind'],disposable.id):assert plan(client,auth,disposable).status_code==409
    assert plan(client,auth,disposable).status_code==202
    monkeypatch.setenv('IACP_KVM_LIFECYCLE_ENABLED','1')
    assert client.post(disposable.path+'/power/start',headers={**auth,'Idempotency-Key':'power-concurrent'}).json()['detail']=='DELETE_OPERATION_ACTIVE'


def test_interrupted_apply_redelivery_only_observes(client,auth,sessions,disposable,monkeypatch):
    op,body=prepare(client,auth,disposable,sessions);delete(client,auth,disposable,body)
    fake=FakeDeletion();fake.interrupt=True
    with pytest.raises(KeyboardInterrupt):execute_deletion(op['operation_id'],'apply',sessions,fake)
    with sessions() as s:assert s.get(DeletionExecution,op['operation_id']).started_at
    fake.interrupt=False;monkeypatch.setenv('IACP_TERRAFORM_DELETE_ENABLED','0')
    execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert fake.calls.count('apply')==1 and fake.calls.count('observe')==1
    assert delete(client,auth,disposable,body).json()['status']=='completed'


def test_failed_proxmox_or_terraform_never_reapplies(client,auth,sessions,disposable):
    op,body=prepare(client,auth,disposable,sessions);delete(client,auth,disposable,body)
    fake=FakeDeletion('PROXMOX_OBSERVATION_FAILED');execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert delete(client,auth,disposable,body).status_code==202
    fake.failure=None;execute_deletion(op['operation_id'],'reconcile',sessions,fake)
    assert fake.calls.count('apply')==1
    with sessions() as s:assert s.get(Operation,op['operation_id']).status=='completed'


def test_binding_change_or_inactive_tenant_blocks_worker(client,auth,sessions,disposable):
    op,body=prepare(client,auth,disposable,sessions);delete(client,auth,disposable,body)
    with sessions() as s:s.get(Instance,disposable.id).name='different-owner';s.commit()
    fake=FakeDeletion();execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert not fake.calls
    with sessions() as s:assert s.get(Operation,op['operation_id']).error_code=='DELETE_BINDING_CHANGED'


def deletion_plan():
    values={'id':'30000','vm_id':30000,'node_name':'mng01','name':'disposable'}
    resource={'address':ADDRESS,'mode':'managed','values':values}
    return {'complete':True,'errored':False,'prior_state':{'values':{'root_module':{'resources':[resource]}}},
            'planned_values':{'root_module':{}},'resource_changes':[{'address':ADDRESS,'mode':'managed',
            'provider_name':'registry.terraform.io/bpg/proxmox','change':{'actions':['delete'],'before':values,'after':None}}]}


@pytest.mark.parametrize('mutation',['other-id','other-node','other-name','extra-resource','replace','create','incomplete','remaining-resource'])
def test_destroy_plan_rejects_wrong_identity_or_extra_changes(mutation):
    p=deletion_plan();change=p['resource_changes'][0]
    if mutation=='other-id':change['change']['before']['id']='106'
    elif mutation=='other-node':change['change']['before']['node_name']='other'
    elif mutation=='other-name':change['change']['before']['name']='customer'
    elif mutation=='extra-resource':p['resource_changes'].append(deepcopy(change))
    elif mutation in {'replace','create'}:change['change']['actions']=['delete','create'] if mutation=='replace' else ['create']
    elif mutation=='incomplete':p['complete']=False
    else:p['planned_values']=p['prior_state']['values']
    with pytest.raises(PlanError):verify_delete_plan(p,{'instance':{'vm_id':30000,'node_name':'mng01','vm_name':'disposable'}})


def test_destroy_plan_has_exactly_one_target():
    verify_delete_plan(deletion_plan(),{'instance':{'vm_id':30000,'node_name':'mng01','vm_name':'disposable'}})


def test_general_command_runner_still_rejects_destroy(tmp_path,monkeypatch):
    r=CommandRunner();r._execute=Mock()
    with pytest.raises(PlanError):r.run(['plan','-destroy'],tmp_path,{},lambda:False)
    with pytest.raises(PlanError):r.apply_destroy(tmp_path,{},lambda:False)
    monkeypatch.setenv('IACP_TERRAFORM_DELETE_ENABLED','1');monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    r.plan_destroy(tmp_path,{},lambda:False);r.apply_destroy(tmp_path,{},lambda:False)
    assert r._execute.call_args.args[0]==['apply','-input=false','-no-color','-lock-timeout=30s','deletion.tfplan']


def test_approver_cannot_cross_tenant(client,auth,sessions,disposable,identities,monkeypatch,tmp_path):
    import os
    tenant=identities['tenant-b-operator']['tenant_id'];token='other-tenant-approver-'+'x'*40
    p=Path(os.environ['IACP_TENANT_CREDENTIALS_FILE']);registry=json.loads(p.read_text())
    registry.append({'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'tenant_id':tenant,'subject':'other-reviewer','role':'approver'})
    p.write_text(json.dumps(registry))
    response=client.post(disposable.path+'/provisioning/deletion-approval',headers={'Authorization':'Bearer '+token,'Idempotency-Key':'other'},
                         json={'operation_id':str(uuid4()),'plan_sha256':'b'*64})
    assert response.status_code==404


def test_inactive_tenant_never_starts_delete(client,auth,sessions,disposable):
    from iacp.models import Tenant
    op,body=prepare(client,auth,disposable,sessions);delete(client,auth,disposable,body)
    with sessions() as s:s.get(Tenant,BOOTSTRAP_TENANT_ID).active=False;s.commit()
    fake=FakeDeletion();execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert not fake.calls
    with sessions() as s:assert s.get(Operation,op['operation_id']).error_code=='TENANT_INACTIVE'


def test_plan_redelivery_after_interruption_fails_closed(client,auth,sessions,disposable):
    op=plan(client,auth,disposable).json()
    with sessions() as s:
        row=s.get(DeletionExecution,op['operation_id']);row.status='running';row.phase='planning';s.commit()
    fake=FakeDeletion();execute_deletion(op['operation_id'],'plan',sessions,fake)
    assert not fake.calls
    with sessions() as s:assert s.get(Operation,op['operation_id']).error_code=='DELETE_PLAN_INTERRUPTED'


def test_broker_failure_keeps_one_durable_operation(client,auth,sessions,disposable):
    disposable.publisher.side_effect=ConnectionError('synthetic-private-detail')
    r=plan(client,auth,disposable);assert r.status_code==503 and 'synthetic-private-detail' not in r.text
    disposable.publisher.side_effect=None;r=plan(client,auth,disposable);assert r.status_code==202
    with sessions() as s:assert len(s.scalars(select(DeletionExecution)).all())==1


def test_swagger_documents_roles_preconditions_and_gated_delete(client,auth):
    schema=client.get('/openapi.json',headers=auth).json()
    description=schema['info']['description']
    assert all(word in description for word in ['Authorize','DHCP','503','power/shutdown','allowlist','104','106'])
    assert schema['components']['schemas']['InstanceCreate']['additionalProperties'] is False
    route=schema['paths']['/v1/instances/{instance_id}']['delete']
    assert route['security']==[{'HTTPBearer':[]}]
    assert route['responses']['202'] and any(p['name']=='Idempotency-Key' and p['required'] for p in route['parameters'])
    assert 'force' not in schema['components']['schemas']['DeleteRequest']['properties']
    html=client.get('/docs',headers=auth).text
    assert '"persistAuthorization": false' in html and '"displayRequestDuration": true' in html


def test_expired_unconsumed_plan_can_be_cancelled_while_drained(client,auth,sessions,disposable,monkeypatch):
    op,body=prepare(client,auth,disposable,sessions)
    with sessions() as s:s.get(DeletionExecution,op['operation_id']).expires_at=utcnow()-timedelta(seconds=1);s.commit()
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0')
    path=disposable.path+'/provisioning/deletions/'+op['operation_id']+'/cancel'
    assert client.post(path).status_code==401
    assert client.post(path,headers=auth).json()['status']=='failed'
    assert client.post(path,headers=auth).json()['status']=='failed'
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','1')
    assert plan(client,auth,disposable,'new-reviewed-intent').status_code==202
    fake=FakeDeletion();execute_deletion(op['operation_id'],'apply',sessions,fake)
    assert not fake.calls


def test_recovery_works_drained_but_cannot_start_or_repeat_deletion(client,auth,sessions,disposable,monkeypatch):
    op,body=prepare(client,auth,disposable,sessions)
    path=disposable.path+'/provisioning/deletions/'+op['operation_id']
    assert client.post(path+'/recover',headers=auth).status_code==409
    delete(client,auth,disposable,body);fake=FakeDeletion();fake.interrupt=True
    with pytest.raises(KeyboardInterrupt):execute_deletion(op['operation_id'],'apply',sessions,fake)
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0');monkeypatch.setenv('IACP_TERRAFORM_DELETE_ENABLED','0')
    assert client.post(path+'/recover').status_code==401
    assert client.post(path+'/cancel',headers=auth).status_code==409
    assert client.post(path+'/recover',headers=auth).status_code==202
    assert disposable.publisher.call_args.args==(op['operation_id'],'reconcile')
    fake.interrupt=False;execute_deletion(op['operation_id'],'reconcile',sessions,fake)
    assert fake.calls.count('apply')==1 and fake.calls.count('observe')==1
