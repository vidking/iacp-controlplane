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
from iacp.models import (BOOTSTRAP_TENANT_ID, DeletionExecution, GuestInitialization, Instance, Job,
                         Operation, OperationEvent, TechnicalOwnership, TerraformExecution, utcnow)
from iacp.services.guest_identity import MODE, PROGRAM_SHA256, execute_initialization
from iacp.terraform.config import Allocation, PlanError
from iacp.terraform.deletion import execute_deletion
from iacp.terraform.deletion_executor import verify_delete_plan
from iacp.terraform.executor import ADDRESS
from iacp.terraform.locks import instance_lock
from iacp.terraform.runner import CommandRunner
from tests.power_helpers import FakePower, managed_instance, refresh_technical_ownership
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
        refresh_technical_ownership(session, identifier)
        session.commit()
    return identifier


def queue_guest_initialization(sessions, instance_id, *, with_job=True):
    with sessions() as session:
        instance = session.get(Instance, instance_id)
        ownership = session.get(TechnicalOwnership, instance_id)
        operation_id = str(uuid4())
        operation = Operation(id=operation_id, instance_id=instance_id, tenant_id=instance.tenant_id,
                              kind='instance.network.initialize', status='queued',
                              idempotency_key='queued-init-' + operation_id,
                              request_hash='c' * 64, requested_by='operator')
        session.add(operation)
        session.flush()
        session.add(GuestInitialization(operation_id=operation_id, instance_id=instance_id,
            provisioning_binding=ownership.binding_sha256, mode=MODE, program_sha256=PROGRAM_SHA256,
            mac_address='bc:24:11:12:34:56', status='queued'))
        if with_job:
            session.add(Job(id=str(uuid4()), tenant_id=instance.tenant_id,
                            operation_id=operation_id, operation=operation.kind, status='queued'))
        session.commit()
    return operation_id


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


def test_delete_supersedes_only_queued_unconsumed_initialization(
        client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    publisher = Mock()
    application.dependency_overrides[get_deletion_publisher] = lambda: publisher
    headers = {**auth, 'Idempotency-Key': 'delete-queued-initialization'}
    response = client.delete(disposable.path, headers=headers)
    assert response.status_code == 202, response.text
    delete_id = response.json()['operation_id']
    with sessions() as session:
        initialization = session.get(GuestInitialization, initialization_id)
        initialization_operation = session.get(Operation, initialization_id)
        initialization_job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        delete_operation = session.get(Operation, delete_id)
        codes = {row.code for row in session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id.in_([initialization_id, delete_id])))}
        assert initialization.status == initialization_operation.status == 'failed'
        assert initialization.finished_at is not None
        assert initialization_operation.error_code == 'GUEST_INITIALIZATION_SUPERSEDED_BY_DELETE'
        assert initialization_job.status == 'failed'
        assert delete_operation.status == 'queued'
        assert 'guest_initialization_superseded_by_delete' in codes
    # A delayed Celery delivery observes the terminal fence and does nothing.
    execute_initialization(initialization_id, sessions,
                           lambda: (_ for _ in ()).throw(AssertionError('guest-exec replayed')))
    repeated = client.delete(disposable.path, headers=headers)
    assert repeated.status_code == 202 and repeated.json()['operation_id'] == delete_id


def test_delete_supersedes_queued_initialization_without_job(client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id, with_job=False)
    application.dependency_overrides[get_deletion_publisher] = lambda: Mock()
    response = client.delete(disposable.path,
                             headers={**auth, 'Idempotency-Key': 'delete-without-init-job'})
    assert response.status_code == 202, response.text
    with sessions() as session:
        assert session.get(GuestInitialization, initialization_id).status == 'failed'
        assert session.scalar(select(Job).where(Job.operation_id == initialization_id)) is None


@pytest.mark.parametrize('event_code', [
    'guest_initialization_superseded_by_delete',
    'guest_initialization_cancelled_before_dispatch',
])
@pytest.mark.parametrize('with_job', [True, False])
def test_delete_accepts_failed_never_dispatched_initialization_without_rewriting_history(
        client, auth, sessions, disposable, application, event_code, with_job):
    initialization_id = queue_guest_initialization(sessions, disposable.id, with_job=with_job)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = 'failed'
        row.finished_at = utcnow()
        operation.error_code = ('GUEST_INITIALIZATION_SUPERSEDED_BY_DELETE'
                                if event_code == 'guest_initialization_superseded_by_delete'
                                else 'GUEST_INITIALIZATION_CANCELLED_BEFORE_DISPATCH')
        if job is not None:
            job.status = 'failed'
        session.add(OperationEvent(operation_id=initialization_id,
                                   status='failed', code=event_code, actor='operator:previous'))
        session.commit()
        old_finished, old_error = row.finished_at, operation.error_code
    publisher = Mock()
    application.dependency_overrides[get_deletion_publisher] = lambda: publisher
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-terminal-' + event_code + str(with_job)})
    assert response.status_code == 202, response.text
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        assert row.status == operation.status == 'failed'
        assert (row.finished_at.replace(tzinfo=None) == old_finished.replace(tzinfo=None)
                and operation.error_code == old_error)
        assert len(session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == initialization_id,
            OperationEvent.code == event_code)).all()) == 1
    deletion_id = response.json()['operation_id']
    execute_deletion(deletion_id, 'plan', sessions, FakeDeletion())
    observed = client.get('/v1/operations/' + deletion_id, headers=auth).json()
    assert observed['deletion']['phase'] == 'awaiting_approval'


def test_delete_accepts_verified_completed_guest_initialization(client, auth, sessions, disposable):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = job.status = 'completed'
        row.started_at = row.finished_at = utcnow()
        row.pid = 42
        row.result = {'verified': True}
        session.add(OperationEvent(operation_id=initialization_id, status='running',
            code='guest_initialization_dispatch_consumed', actor='worker:iac'))
        session.commit()
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-completed-initialization'})
    assert response.status_code == 202, response.text
    with sessions() as session:
        assert session.get(GuestInitialization, initialization_id).status == 'completed'


def test_delete_reconciles_exact_terminal_guest_program_failure_without_replay(
        client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = job.status = 'failed'
        row.started_at = utcnow()
        row.finished_at = utcnow()
        row.pid = 1048
        operation.error_code = 'GUEST_INITIALIZATION_PROGRAM_FAILED'
        session.add(OperationEvent(operation_id=initialization_id, status='running',
            code='guest_initialization_dispatch_consumed', actor='worker:iac'))
        session.commit()
    publisher = Mock()
    application.dependency_overrides[get_deletion_publisher] = lambda: publisher
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-terminal-guest-program-failure'})
    assert response.status_code == 202, response.text
    deletion_id = response.json()['operation_id']
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        assert row.status == operation.status == 'failed'
        assert operation.error_code == 'GUEST_INITIALIZATION_PROGRAM_FAILED'
        codes = {item.code for item in session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id.in_([initialization_id, deletion_id]))) }
        assert 'guest_initialization_failed_retained_for_delete' in codes
    execute_deletion(deletion_id, 'plan', sessions, FakeDeletion())
    observed = client.get('/v1/operations/' + deletion_id, headers=auth).json()
    assert observed['deletion']['phase'] == 'awaiting_approval'
    execute_initialization(initialization_id, sessions,
                           lambda: (_ for _ in ()).throw(AssertionError('guest-exec replayed')))


def test_delete_rejects_consumed_guest_program_failure_with_other_error_code(
        client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = job.status = 'failed'
        row.started_at = utcnow()
        row.finished_at = utcnow()
        row.pid = 1048
        operation.error_code = 'GUEST_INITIALIZATION_OBSERVATION_FAILED'
        session.add(OperationEvent(operation_id=initialization_id, status='running',
            code='guest_initialization_dispatch_consumed', actor='worker:iac'))
        session.commit()
    application.dependency_overrides[get_deletion_publisher] = lambda: Mock()
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-terminal-guest-other-failure'})
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == 'GUEST_INITIALIZATION_RECONCILIATION_REQUIRED'


def test_delete_accepts_terminal_access_reconciliation_failure(
        client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = job.status = 'failed'
        row.started_at = utcnow()
        row.finished_at = utcnow()
        row.pid = 1088
        row.result = {'reconciliation': {'status': 'failed',
                                         'error_code': 'ROOT_SSH_AUTHENTICATION_FAILED'}}
        operation.error_code = 'ROOT_SSH_AUTHENTICATION_FAILED'
        session.add(OperationEvent(operation_id=initialization_id, status='failed',
            code='guest_initialization_dispatch_consumed', actor='worker:iac'))
        session.add(OperationEvent(operation_id=initialization_id, status='failed',
            code='guest_initialization_reconciliation_failed', actor='operator:test'))
        session.commit()
    application.dependency_overrides[get_deletion_publisher] = lambda: Mock()
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-reconciled-access-failure'})
    assert response.status_code == 202, response.text


@pytest.mark.parametrize('corruption', [
    'started', 'pid', 'result', 'consumed_event', 'job_queued', 'operation_queued',
])
def test_delete_rejects_ambiguous_failed_guest_initialization(
        client, auth, sessions, disposable, application, corruption):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        operation = session.get(Operation, initialization_id)
        job = session.scalar(select(Job).where(Job.operation_id == initialization_id))
        row.status = operation.status = job.status = 'failed'
        row.finished_at = utcnow()
        if corruption == 'started':
            row.started_at = utcnow()
        elif corruption == 'pid':
            row.pid = 42
        elif corruption == 'result':
            row.result = {'unexpected': True}
        elif corruption == 'consumed_event':
            session.add(OperationEvent(operation_id=initialization_id, status='running',
                code='guest_initialization_dispatch_consumed', actor='worker:iac'))
        elif corruption == 'job_queued':
            job.status = 'queued'
        else:
            operation.status = 'queued'
        session.commit()
    response = client.delete(disposable.path, headers={**auth,
        'Idempotency-Key': 'delete-ambiguous-failed-' + corruption})
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == 'GUEST_INITIALIZATION_RECONCILIATION_REQUIRED'
    with sessions() as session:
        assert session.scalar(select(DeletionExecution).where(
            DeletionExecution.instance_id == disposable.id)) is None


def test_delete_with_consumed_initialization_requires_reconciliation(
        client, auth, sessions, disposable, application):
    initialization_id = queue_guest_initialization(sessions, disposable.id)
    with sessions() as session:
        row = session.get(GuestInitialization, initialization_id)
        row.status = 'running'
        row.started_at = utcnow()
        row.pid = 42
        session.get(Operation, initialization_id).status = 'running'
        session.scalar(select(Job).where(Job.operation_id == initialization_id)).status = 'running'
        session.commit()
    application.dependency_overrides[get_deletion_publisher] = lambda: Mock()
    response = client.delete(disposable.path,
                             headers={**auth, 'Idempotency-Key': 'delete-consumed-initialization'})
    assert response.status_code == 409
    assert response.json()['detail'] == 'GUEST_INITIALIZATION_RECONCILIATION_REQUIRED'


@pytest.mark.parametrize('which',['delete','apply'])
def test_feature_gates_fail_closed(client,auth,disposable,monkeypatch,which):
    monkeypatch.delenv({'delete':'IACP_TERRAFORM_DELETE_ENABLED','apply':'IACP_TERRAFORM_APPLY_ENABLED'}[which])
    assert plan(client,auth,disposable).status_code==409
    disposable.publisher.assert_not_called()


@pytest.mark.parametrize('vmid',[104,29000,29001,29010,29011,29999])
def test_controlplane_templates_and_non_disposable_ids_protected(sessions,client,auth,disposable,monkeypatch,vmid):
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
    assert all(word in description for word in ['Authorize','DHCP','503','power/shutdown','104','29000','29001','29010','29011'])
    assert schema['components']['schemas']['CommercialInstanceCreate']['additionalProperties'] is False
    route=schema['paths']['/v1/instances/{instance_id}']['delete']
    assert route['security']==[{'HTTPBearer':[]}]
    assert route['responses']['202'] and any(p['name']=='Idempotency-Key' and not p['required'] for p in route['parameters'])
    assert 'force' not in schema['components']['schemas']['DeleteRequest']['properties']
    html=client.get('/docs',headers=auth).text
    assert '/docs?asset=iacp-v1.js' in html
    script=client.get('/docs?asset=iacp-v1.js',headers=auth).text
    assert 'persistAuthorization: false' in script and 'displayRequestDuration: true' in script


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
