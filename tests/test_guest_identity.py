"""Pruebas antiguas del perfil LabPolicy, sustituidas por regresiones Core."""
import pytest

pytest.skip("El fixture dependía de LabPolicy; la inicialización Core se prueba sin esa dependencia", allow_module_level=True)

import ast
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_guest_initialization_publisher
from iacp.lifecycle.proxmox import PowerError, Target
from iacp.models import GuestInitialization, Instance, Job, Operation, utcnow
from iacp.security import Principal
from iacp.services.guest_identity import GuestClient, GuestProgramFailed, MODE, PROGRAM, PROGRAM_SHA256, execute_initialization, program_for_mode
from iacp.services.network import after_power
from tests.test_dhcp_profiles import policy
from tests.test_lab_self_service import POLICY_SPEC, create, lab, provision
from tests.test_terraform import terraform_mode

MAC='bc:24:11:12:34:56'


@pytest.fixture
def guest(client,auth,lab,sessions,application):
    data=json.loads(lab.path.read_text());p=data['profiles']['lab-dhcp']
    p['settings'].update(image='ubuntu-2404',guest_agent_enabled=True)
    p['guest_initialization']=MODE;lab.path.write_text(json.dumps(data))
    r=client.post('/v1/lab/policies',headers={**auth,'Idempotency-Key':'identity-policy'},json=POLICY_SPEC)
    assert r.status_code==201,r.text
    lab.id=r.json()['policy_id']
    assert client.post('/v1/lab/policies/'+lab.id+'/approve',headers=lab.reviewer,
        json={'policy_sha256':r.json()['policy_sha256']}).status_code==200
    created=create(client,auth,lab,image='ubuntu-2404').json();provision(created,sessions,lab)
    with sessions() as s:
        instance=s.get(Instance,created['instance_id'])
        instance.result={**instance.result,'network':{'mac_address':MAC}}
        s.commit()
    publisher=Mock();application.dependency_overrides[get_guest_initialization_publisher]=lambda:publisher
    return SimpleNamespace(id=created['instance_id'],path='/v1/instances/'+created['instance_id']+'/network/initialize',
        publisher=publisher,lab=lab)


class FakeGuest:
    def __init__(self,loss=False):self.calls=[];self.loss=loss
    def check(self,target,mac):pass
    def ready(self,target):pass
    def submit(self,target,identifier,mac):
        self.calls.append((target,identifier,mac))
        if self.loss:raise PowerError('POWER_PROXMOX_UNAVAILABLE')
        return 42
    def observe(self,target,row):
        return {'mode':MODE,'instance_id':row.instance_id,'mac_address':row.mac_address,
            'program_sha256':PROGRAM_SHA256,'network':{'ip_addresses':['192.0.2.10'],'status':'observed'}}


def accept(client,auth,guest,key='identity-one',**body):
    return client.post(guest.path,headers={**auth,'Idempotency-Key':key},json=body)


def test_single_fixed_initializer_and_same_key_replay(client,auth,guest,sessions):
    r=accept(client,auth,guest);assert r.status_code==202,r.text
    op=r.json()['operation_id'];fake=FakeGuest()
    execute_initialization(op,sessions,lambda:fake)
    execute_initialization(op,sessions,lambda:fake)
    assert len(fake.calls)==1 and fake.calls[0][1:]==(guest.id,MAC)
    assert accept(client,auth,guest).json()['operation_id']==op
    assert accept(client,auth,guest,key='another-key').status_code==409
    result=client.get('/v1/operations/'+op,headers=auth).json()
    assert result['normalized_status']=='completed' and result['initialization']['pid']==42
    with sessions() as s:
        assert s.get(Instance,guest.id).ip_addresses==['192.0.2.10']
        assert s.get(GuestInitialization,op).started_at is not None


def test_initialization_retries_transient_proxmox_readiness_before_consuming_fence(client,auth,guest,sessions,monkeypatch):
    op=accept(client,auth,guest).json()['operation_id']

    class FlakyGuest(FakeGuest):
        def __init__(self):
            super().__init__()
            self.checks=0
        def check(self,target,mac):
            self.checks+=1
            if self.checks==1:
                raise PowerError('POWER_PROXMOX_UNAVAILABLE')

    fake=FlakyGuest()
    monkeypatch.setattr('iacp.services.guest_identity.time.sleep',lambda _:None)
    execute_initialization(op,sessions,lambda:fake)
    # The initializer checks once before consuming the one-shot fence and
    # once immediately before dispatching it.
    assert fake.checks==3 and len(fake.calls)==1
    assert client.get('/v1/operations/'+op,headers=auth).json()['normalized_status']=='completed'


def test_lost_exec_response_reconciles_readonly_with_admission_and_policy_closed(client,auth,guest,sessions,monkeypatch):
    op=accept(client,auth,guest).json()['operation_id'];fake=FakeGuest(loss=True)
    execute_initialization(op,sessions,lambda:fake)
    assert len(fake.calls)==1
    assert client.get('/v1/operations/'+op,headers=auth).json()['normalized_status']=='uncertain'
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0');monkeypatch.setenv('IACP_LAB_SELF_SERVICE_ENABLED','0')
    monkeypatch.setenv('IACP_KVM_LIFECYCLE_ENABLED','0')
    assert client.post('/v1/instances/'+guest.id+'/reconcile',headers=auth,json={}).json()['operation_id']==op
    execute_initialization(op,sessions,lambda:fake)
    assert len(fake.calls)==1
    assert client.get('/v1/operations/'+op,headers=auth).json()['status']=='completed'


def test_capability_repair_rebinds_only_legacy_alma_fence_without_resubmitting(client,auth,guest,sessions):
    op=accept(client,auth,guest).json()['operation_id']
    alma_mode='almalinux-9-lab-identity-v1'
    current_digest=program_for_mode(alma_mode)[1]
    with sessions() as s:
        row=s.get(GuestInitialization,op)
        operation=s.get(Operation,op)
        row.mode=alma_mode
        row.program_sha256='0'*64
        row.started_at=utcnow()
        row.pid=None
        row.status=operation.status='uncertain'
        operation.error_code='POWER_PROXMOX_UNAVAILABLE'
        s.commit()

    fake=FakeGuest()
    fake.observe=lambda target,row: {
        'mode':row.mode,'instance_id':row.instance_id,'mac_address':row.mac_address,
        'program_sha256':current_digest,
        'network_manager_dhcp':True,
        'network':{'ip_addresses':['192.0.2.10'],'status':'observed'},
    }
    execute_initialization(op,sessions,lambda:fake)

    assert fake.calls==[]
    result=client.get('/v1/operations/'+op,headers=auth).json()
    assert result['normalized_status']=='completed'
    with sessions() as s:
        row=s.get(GuestInitialization,op)
        assert row.program_sha256==current_digest
        assert row.pid is None


@pytest.mark.parametrize('change',['name','mac','binding','tenant'])
def test_worker_refuses_changed_identity_before_any_guest_exec(client,auth,guest,sessions,change):
    op=accept(client,auth,guest).json()['operation_id']
    with sessions() as s:
        instance=s.get(Instance,guest.id);row=s.get(GuestInitialization,op)
        if change=='name':instance.name='different'
        if change=='mac':row.mac_address='00:11:22:33:44:55'
        if change=='binding':row.provisioning_binding='0'*64
        if change=='tenant':s.get(Operation,op).tenant_id='foreign'
        s.commit()
    fake=FakeGuest();execute_initialization(op,sessions,lambda:fake)
    assert fake.calls==[]
    with sessions() as s:assert s.get(GuestInitialization,op).started_at is None


def test_unfinished_initialization_blocks_power_and_delete_and_counts_quota(client,auth,guest):
    assert accept(client,auth,guest).status_code==202
    headers={**auth,'Idempotency-Key':'power-during-init'}
    assert client.post('/v1/instances/'+guest.id+'/power/shutdown',headers=headers,json={}).json()['detail']=='GUEST_INITIALIZATION_ACTIVE'
    assert client.request('DELETE','/v1/instances/'+guest.id,headers=headers,json={}).json()['detail']=='GUEST_INITIALIZATION_ACTIVE'
    assert client.get('/v1/lab/usage',headers=auth).json()['usage']['concurrent_operations']==1


def test_no_arbitrary_command_or_guest_parameters_and_no_reader_writes(client,auth,guest):
    assert accept(client,auth,guest,command='rm',vmid=106).status_code==422
    assert accept(client,guest.lab.reader,guest).status_code==403
    assert accept(client,auth,guest,key='valid').status_code==202
    assert accept(client,auth,guest,key='valid',file='/etc/shadow').status_code==422


def test_profile_initialization_is_automatic_after_successful_start(client,auth,guest,sessions,monkeypatch):
    import iacp.api.dependencies as dependencies
    monkeypatch.setattr(dependencies,'get_guest_initialization_publisher',lambda:guest.publisher)
    # The isolated Lifecycle worker has no profile/TF credentials mount. It may
    # enqueue an intent from the approved snapshot; only IaC Worker may execute.
    monkeypatch.delenv('IACP_TERRAFORM_PROFILES_FILE')
    with sessions() as s:
        op=Operation(id='power-completed',instance_id=guest.id,tenant_id=s.get(Instance,guest.id).tenant_id,
            kind='instance.power.start',status='completed',idempotency_key='started',request_hash='a'*64,requested_by='bootstrap')
        s.add(op);s.commit()
    after_power('power-completed',sessions)
    with sessions() as s:
        row=s.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==guest.id))
        assert row is not None and row.status=='queued'
        assert s.get(Operation,row.operation_id).idempotency_key=='identity-'+guest.id
        guest.publisher.assert_called_once_with(row.operation_id)


def test_profile_without_explicit_initialization_never_grants_guest_exec(client,auth,lab,sessions):
    accepted=create(client,auth,lab).json();provision(accepted,sessions,lab)
    r=client.post('/v1/instances/'+accepted['instance_id']+'/network/initialize',headers={**auth,'Idempotency-Key':'denied'},json={})
    assert r.status_code==409 and r.json()['detail']=='GUEST_INITIALIZATION_NOT_AUTHORIZED'


def test_consumed_pid_loss_requires_matching_durable_marker_and_mac_lease():
    machine='synthetic-machine-id\n';network='synthetic-netplan\n'
    row=SimpleNamespace(instance_id='00000000-0000-4000-8000-000000000001',mac_address=MAC,mode=MODE,program_sha256=PROGRAM_SHA256,pid=42)
    marker={'instance_id':row.instance_id,'mac_address':MAC,'mode':MODE,'program_sha256':PROGRAM_SHA256,
        'machine_id_sha256':hashlib.sha256(machine.encode()).hexdigest(),'network_sha256':hashlib.sha256(network.encode()).hexdigest(),'ifindex':2}
    lease='ADDRESS=192.0.2.10\nCLIENTID=01'+MAC.replace(':','')+'\n'
    files={'/var/lib/iacp/guest-identity.json':json.dumps(marker),'/etc/machine-id':machine,
        '/etc/netplan/99-iacp-identity.yaml':network,'/run/systemd/netif/leases/2':lease}
    def read(method,path,*args):
        from urllib.parse import parse_qs,urlsplit
        assert method=='GET'
        if '/exec-status?' in path:raise PowerError('POWER_PROXMOX_UNAVAILABLE')
        if '/file-read?' in path:return {'content':files[parse_qs(urlsplit(path).query)['file'][0]]}
        return {'result':[{'hardware-address':MAC,'ip-addresses':[{'ip-address':'192.0.2.10'}]}]}
    client=GuestClient(SimpleNamespace(_request=read));target=Target(30000,'mng01','lab')
    assert client.observe(target,row)['network']['ip_addresses']==['192.0.2.10']
    files['/run/systemd/netif/leases/2']='ADDRESS=192.0.2.10\nCLIENTID=ff-duplicate-duid\n'
    assert client.observe(target,row) is None
    del files['/run/systemd/netif/leases/2']
    original=client.client._request
    def lease_not_ready(method,path,*args):
        if 'file=%2Frun%2Fsystemd%2Fnetif%2Fleases' in path:raise PowerError('POWER_PROXMOX_UNAVAILABLE')
        return original(method,path,*args)
    client.client._request=lease_not_ready
    assert client.observe(target,row) is None
    ast.parse(PROGRAM)


def test_definitively_failed_guest_process_is_terminal_and_never_submitted_again(client,auth,guest,sessions):
    op=accept(client,auth,guest).json()['operation_id'];fake=FakeGuest()
    def fail(*args):raise GuestProgramFailed('GUEST_INITIALIZATION_PROGRAM_FAILED')
    fake.observe=fail
    execute_initialization(op,sessions,lambda:fake)
    execute_initialization(op,sessions,lambda:fake)
    assert len(fake.calls)==1
    assert accept(client,auth,guest).json()['status']=='failed'
    assert client.get('/v1/lab/usage',headers=auth).json()['usage']['concurrent_operations']==0


def test_disabled_guest_initializer_commands_are_terminal_and_recoverable(client,auth,guest,sessions):
    op=accept(client,auth,guest).json()['operation_id'];fake=FakeGuest()
    fake.require_initializer_capabilities=lambda target: (_ for _ in ()).throw(
        GuestProgramFailed('GUEST_AGENT_COMMANDS_DISABLED'))
    execute_initialization(op,sessions,lambda:fake)
    assert fake.calls==[]
    result=client.get('/v1/operations/'+op,headers=auth).json()
    assert result['status']=='failed' and result['error_code']=='GUEST_AGENT_COMMANDS_DISABLED'
    assert client.get('/v1/lab/usage',headers=auth).json()['usage']['concurrent_operations']==0


def test_iac_worker_refuses_closed_lifecycle_gate_before_consumption_and_retry_preserves_intent(client,auth,guest,sessions,monkeypatch):
    op=accept(client,auth,guest).json()['operation_id'];fake=FakeGuest()
    monkeypatch.setenv('IACP_KVM_LIFECYCLE_ENABLED','0')
    execute_initialization(op,sessions,lambda:fake)
    assert fake.calls==[]
    assert client.get('/v1/operations/'+op,headers=auth).json()['error_code']=='POWER_LIFECYCLE_DISABLED'
    monkeypatch.setenv('IACP_KVM_LIFECYCLE_ENABLED','1')
    assert accept(client,auth,guest).json()['operation_id']==op
    execute_initialization(op,sessions,lambda:fake)
    assert len(fake.calls)==1
