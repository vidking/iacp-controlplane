"""Entrega, expiración y reconciliación sin volver a clonar ni reenviar secretos."""
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.models import CommercialIntent,GuestInitialization,Instance,Job,Operation,TerraformApply,TerraformExecution,utcnow,BOOTSTRAP_TENANT_ID
from iacp.security import Principal
from iacp.services.commercial import create,advance
from iacp.services.commercial_bootstrap import request,execute
from iacp.terraform.lifecycle import execute_plan
from tests.test_commercial_admission import store,commercial_config,specification
from tests.test_commercial_catalog import catalog_profile
from tests.test_terraform import FakeExecutor
from uuid import uuid4


class Delivery:
    def __init__(self):self.calls=0;self.observations=0;self.lost=False
    def apply(self,*args):
        self.calls+=1
        if self.lost:raise RuntimeError(args[3])
        return {'status':'completed'}
    def observe(self,*args):self.observations+=1;return {'status':'completed'}


@pytest.fixture
def initialized(sessions,commercial_config,store):
    p=Principal(BOOTSTRAP_TENANT_ID,'crm','operator')
    published=[]
    with sessions() as s:accepted=create(s,p,specification(),'creation',published.append,store,capacity=lambda *_:None)
    executor=FakeExecutor();execute_plan(accepted.job_id,1,sessions,executor)
    with sessions() as s:
        instance=s.get(Instance,accepted.instance_id);approval=advance(s,instance,lambda *_:None)
        execution=s.get(TerraformExecution,instance.id)
        result={'simulated':False,'vmid':execution.vmid,'node':'mng01','name':instance.name,
            'observation':{'postconditions_verified':True,'state_present':True,'proxmox_present':True}}
        approval.status='completed';approval.result=result;approval.started_at=approval.finished_at=utcnow()
        s.get(Job,approval.job_id).status='completed'
        instance.status='ready';instance.vmid=execution.vmid;instance.node='mng01';instance.result=result
        instance.ip_addresses=['192.0.2.10']
        s.get(Operation,accepted.operation_id).status='completed'
        op=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,kind='instance.network.initialize',
            status='completed',idempotency_key='fixture-initialization',request_hash='a'*64,requested_by='crm')
        s.add(op);s.flush()
        s.add(GuestInitialization(operation_id=op.id,instance_id=instance.id,mode='ubuntu-2404-lab-identity-v1',
            mac_address='aa:bb:cc:dd:ee:ff',program_sha256='a'*64,provisioning_binding='b'*64,status='completed'))
        s.add(Job(id=str(uuid4()),operation=op.kind,tenant_id=instance.tenant_id,operation_id=op.id,status='completed'))
        s.commit()
        bootstrap=request(s,instance,p,'bootstrap-'+instance.id,lambda _:None)
    return accepted,bootstrap,executor,published


def test_success_deletes_ciphertext_and_duplicate_task_does_not_send_again(sessions,initialized,store,caplog):
    accepted,bootstrap,executor,published=initialized
    delivery=Delivery();execute(bootstrap.operation_id,sessions,store,delivery)
    execute(bootstrap.operation_id,sessions,store,delivery)
    assert delivery.calls==1 and len(executor.calls)==1 and len(published)==1
    assert store.redis.values=={}
    with sessions() as s:
        row=s.get(CommercialIntent,accepted.instance_id)
        assert row.bootstrap_status=='completed' and row.secret_reference is None
        for table in Instance.metadata.sorted_tables:
            data=json.dumps([dict(x) for x in s.execute(table.select()).mappings()],default=str)
            assert specification().access.root_password.get_secret_value() not in data+caplog.text


def test_expired_secret_is_recoverable_by_identical_https_intent_without_clone(sessions,initialized,store,monkeypatch):
    accepted,bootstrap,executor,published=initialized
    with sessions() as s:reference=s.get(CommercialIntent,accepted.instance_id).secret_reference
    store.delete(reference)
    execute(bootstrap.operation_id,sessions,store,Delivery())
    with sessions() as s:
        row=s.get(CommercialIntent,accepted.instance_id)
        assert row.error_code=='BOOTSTRAP_SECRET_EXPIRED_OR_MISSING' and row.bootstrap_started_at is None
    from iacp.api import dependencies
    bootstrap_messages=[]
    monkeypatch.setattr(dependencies,'get_commercial_bootstrap_publisher',lambda:bootstrap_messages.append)
    with sessions() as s:
        replay=create(s,Principal(BOOTSTRAP_TENANT_ID,'crm','operator'),specification(),'creation',published.append,store,
            capacity=lambda *_:pytest.fail('No volver a reservar capacidad'))
        assert replay.instance_id==accepted.instance_id
    delivery=Delivery();execute(bootstrap.operation_id,sessions,store,delivery)
    assert len(published)==len(executor.calls)==delivery.calls==1
    assert bootstrap_messages==[bootstrap.operation_id] and store.redis.values=={}


def test_lost_delivery_response_only_observes_even_after_secret_expiry(sessions,initialized,store,caplog):
    accepted,bootstrap,_,_=initialized
    delivery=Delivery();delivery.lost=True
    execute(bootstrap.operation_id,sessions,store,delivery)
    with sessions() as s:
        row=s.get(CommercialIntent,accepted.instance_id)
        assert row.bootstrap_status=='uncertain' and row.bootstrap_started_at
        store.delete(row.secret_reference)
        assert row.error_code=='BOOTSTRAP_DELIVERY_UNCONFIRMED'
    execute(bootstrap.operation_id,sessions,store,delivery)
    assert delivery.calls==1 and delivery.observations==1
    with sessions() as s:assert s.get(CommercialIntent,accepted.instance_id).bootstrap_status=='completed'
    assert specification().access.root_password.get_secret_value() not in caplog.text


def test_ssh_uses_pinned_key_and_stdin_not_password_in_command_or_connect(tmp_path,monkeypatch):
    import paramiko
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from iacp.services.secure_bootstrap import SecureSSHBootstrap,COMMAND
    from iacp.services.guest_identity import GuestClient
    from types import SimpleNamespace
    keyfile=tmp_path/'fixture_key';keyfile.write_text('fixture-key-never-used');keyfile.chmod(0o600)
    monkeypatch.setenv('IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE',str(keyfile))
    monkeypatch.setattr(paramiko.PKey,'from_path',lambda _:object())
    host=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    client=Mock()
    def guest_read(method,path):
        if 'ssh_host_ed25519' in path:return {'content':host}
        if 'result.json' in path:return {'content':json.dumps({'v1':{'errors':[]}})}
        return {'content':''}
    client._request.side_effect=guest_read
    monkeypatch.setattr(GuestClient,'configured',lambda:SimpleNamespace(client=client))
    ssh=Mock();ssh.__enter__=Mock(return_value=ssh);ssh.__exit__=Mock(return_value=False)
    stdin,stdout,stderr=Mock(),Mock(),Mock();stdout.read.return_value=b'COMPLETED\n';stdout.channel.recv_exit_status.return_value=0
    ssh.exec_command.return_value=(stdin,stdout,stderr)
    monkeypatch.setattr(paramiko,'SSHClient',lambda:ssh)
    secret=specification().access.root_password.get_secret_value()
    result=SecureSSHBootstrap().apply(SimpleNamespace(id='iid',ip_addresses=['192.0.2.10']),
        SimpleNamespace(path='/fixture'),'R'*43,secret,[],'ubuntu')
    assert result['status']=='completed'
    assert secret not in COMMAND and secret not in str(ssh.connect.call_args)
    assert 'password' not in ssh.connect.call_args.kwargs
    assert json.loads(stdin.write.call_args.args[0])['password']==secret
    assert isinstance(ssh.set_missing_host_key_policy.call_args.args[0],paramiko.RejectPolicy)


@pytest.mark.parametrize('vmid',[104,106,29000])
def test_commercial_binding_never_authorizes_protected_resources(sessions,initialized,vmid):
    from iacp.services.commercial import member,binding
    from iacp.services.instances import Conflict
    accepted,_,_,_=initialized
    with sessions() as s:
        instance=s.get(Instance,accepted.instance_id);execution=s.get(TerraformExecution,instance.id)
        execution.vmid=instance.vmid=vmid
        row=s.get(CommercialIntent,instance.id);row.execution_binding=binding(instance,execution,row)
        with pytest.raises(Conflict,match='OWNERSHIP_BINDING_CHANGED'):member(s,instance,'delete')
