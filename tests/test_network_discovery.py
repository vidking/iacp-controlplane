"""Network discovery is read-only, tenant scoped and bound to a guest MAC."""
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from iacp.models import (BOOTSTRAP_TENANT_ID,GuestInitialization,Instance,Job,
    TerraformExecution,Operation,NetworkDiscovery,utcnow)
from iacp.services.network import agent_addresses,after_power,execute_discovery
from tests.power_helpers import managed_instance
from tests.test_instances import identities

@pytest.fixture
def network(sessions,monkeypatch):
    identifier=managed_instance(sessions,BOOTSTRAP_TENANT_ID,vmid=30000)
    with sessions() as s:
        i=s.get(Instance,identifier);i.network_mode='dhcp';i.result={**i.result,'network':{'mac_address':'aa:bb:cc:dd:ee:ff'}};s.commit()
    publish=Mock()
    monkeypatch.setattr('iacp.api.dependencies.get_network_publisher',lambda:publish)
    return identifier,publish

def request(client,headers,identifier,key='discover-one'):
    return client.post(f'/v1/instances/{identifier}/network/discover',headers={**headers,'Idempotency-Key':key},json={})

def test_agent_observation_completes_once_and_replay_never_observes_twice(client,auth,sessions,network):
    identifier,publish=network
    r=request(client,auth,identifier);assert r.status_code==202,r.text
    op=r.json()['operation_id'];publish.assert_called_once_with(op)
    result={'status':'observed','ip_addresses':['192.0.2.50'],'source':'qemu-guest-agent'}
    observer=Mock(return_value=result)
    execute_discovery(op,sessions,observer);execute_discovery(op,sessions,observer)
    observer.assert_called_once()
    assert request(client,auth,identifier).json()['operation_id']==op
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['status']=='ready' and data['ip_addresses']==result['ip_addresses']
    assert data['result']['network']=={'mac_address':'aa:bb:cc:dd:ee:ff'}
    assert client.get('/v1/operations/'+op,headers=auth).json()['network']['status']==result['status']


def test_qga_pending_retries_until_dhcp_is_observed(client,auth,sessions,network):
    identifier,_=network
    op=request(client,auth,identifier,key='discover-retry').json()['operation_id']
    observer=Mock(side_effect=[
        {'status':'pending','ip_addresses':[],'source':'qemu-guest-agent','reason':'GUEST_AGENT_UNAVAILABLE'},
        {'status':'pending','ip_addresses':[],'source':'qemu-guest-agent','reason':'DHCP_LEASE_PENDING'},
        {'status':'observed','ip_addresses':['192.0.2.51'],'source':'qemu-guest-agent'},
    ])
    execute_discovery(op,sessions,observer,timeout=30,pause=lambda _:None)
    assert observer.call_count==3
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['ip_addresses']==['192.0.2.51']
    assert data['network_observation']['current_ip_verified'] is True
    assert data['network_observation']['attempt']==3
    assert client.get('/v1/operations/'+op,headers=auth).json()['status']=='completed'


@pytest.mark.parametrize('initialization_status',['queued','failed'])
def test_network_observation_is_independent_from_guest_initialization(client,auth,sessions,network,initialization_status):
    """QGA puede verificar DHCP mientras otro guest-exec sigue incompleto."""
    identifier,_=network
    with sessions() as s:
        instance=s.get(Instance,identifier)
        init_operation=Operation(id='guest-init-'+initialization_status,tenant_id=instance.tenant_id,
            instance_id=identifier,kind='instance.network.initialize',status=initialization_status,
            idempotency_key='guest-init-'+initialization_status,request_hash='b'*64,requested_by='bootstrap')
        s.add(init_operation);s.flush()
        s.add(Job(id='guest-job-'+initialization_status,tenant_id=instance.tenant_id,
            operation_id=init_operation.id,operation=init_operation.kind,status=initialization_status))
        s.add(GuestInitialization(operation_id=init_operation.id,instance_id=identifier,
            provisioning_binding='c'*64,mode='ubuntu-2604-lab-identity-v1',program_sha256='d'*64,
            mac_address='aa:bb:cc:dd:ee:ff',status=initialization_status))
        s.commit()
    op=request(client,auth,identifier,key='discover-with-init-'+initialization_status).json()['operation_id']
    execute_discovery(op,sessions,Mock(return_value={
        'status':'observed','ip_addresses':['192.0.2.60'],'source':'qemu-guest-agent'}))
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['ip_addresses']==['192.0.2.60']
    assert data['network_observation']['status']=='observed'
    assert data['network_observation']['current_ip_verified'] is True
    with sessions() as s:
        assert s.get(GuestInitialization,init_operation.id).status==initialization_status


def test_after_power_dispatches_network_while_guest_initialization_is_pending(sessions,network,monkeypatch):
    identifier,publish=network
    init_publish=Mock()
    with sessions() as s:
        instance=s.get(Instance,identifier)
        instance.commercial_revision='provisioning-api-core'
        instance.network_profile='lab-dhcp'
        power=Operation(id='power-with-pending-init',tenant_id=instance.tenant_id,instance_id=identifier,
            kind='instance.power.reboot',status='completed',idempotency_key='reboot-with-pending-init',
            request_hash='e'*64,requested_by='bootstrap')
        init_operation=Operation(id='init-with-pending-power',tenant_id=instance.tenant_id,instance_id=identifier,
            kind='instance.network.initialize',status='running',idempotency_key='init-with-pending-power',
            request_hash='f'*64,requested_by='bootstrap')
        s.add_all([power,init_operation]);s.flush()
        s.add(Job(id='init-job-with-pending-power',tenant_id=instance.tenant_id,
            operation_id=init_operation.id,operation=init_operation.kind,status='running'))
        s.add(GuestInitialization(operation_id=init_operation.id,instance_id=identifier,
            provisioning_binding='1'*64,mode='ubuntu-2604-lab-identity-v1',program_sha256='2'*64,
            mac_address='aa:bb:cc:dd:ee:ff',status='running'))
        s.commit()
    monkeypatch.setattr('iacp.services.commercial.delegation',lambda *args,**kwargs:
        SimpleNamespace(profile_snapshots={'lab-dhcp':{'guest_initialization':'ubuntu-2604-lab-identity-v1'}}))
    monkeypatch.setattr('iacp.api.dependencies.get_guest_initialization_publisher',lambda:init_publish)
    after_power('power-with-pending-init',sessions)
    init_publish.assert_called_once_with('init-with-pending-power')
    publish.assert_called_once()
    with sessions() as s:
        network_operations=s.scalars(select(Operation).where(
            Operation.instance_id==identifier,Operation.kind=='instance.network.discover')).all()
        assert len(network_operations)==1
        assert network_operations[0].idempotency_key.endswith('-power-with-pending-init')


def test_each_completed_power_transition_gets_a_new_network_observation(sessions,network):
    identifier,publish=network
    with sessions() as s:
        instance=s.get(Instance,identifier)
        for action in ('start','reboot'):
            power=Operation(id='power-refresh-'+action,tenant_id=instance.tenant_id,instance_id=identifier,
                kind='instance.power.'+action,status='completed',idempotency_key='power-refresh-'+action,
                request_hash='9'*64,requested_by='bootstrap')
            s.add(power)
        s.commit()
    after_power('power-refresh-start',sessions)
    after_power('power-refresh-reboot',sessions)
    assert publish.call_count==2
    with sessions() as s:
        network_operations=s.scalars(select(Operation).where(
            Operation.instance_id==identifier,Operation.kind=='instance.network.discover')).all()
        assert len(network_operations)==2
        assert len({row.idempotency_key for row in network_operations})==2


def test_qga_timeout_preserves_last_ip_as_unverified_history(client,auth,sessions,network):
    identifier,_=network
    with sessions() as s:
        instance=s.get(Instance,identifier)
        instance.ip_addresses=['192.0.2.50']
        instance.network_observation={'status':'observed','ip_addresses':['192.0.2.50'],
            'current_ip_verified':True,'observed_at':'2026-10-27T00:00:00+00:00'}
        s.commit()
    op=request(client,auth,identifier,key='discover-timeout').json()['operation_id']
    observer=Mock(return_value={'status':'pending','ip_addresses':[],'source':'qemu-guest-agent',
        'reason':'GUEST_AGENT_UNAVAILABLE'})
    execute_discovery(op,sessions,observer,timeout=0,pause=lambda _:None)
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['ip_addresses']==[]
    assert data['network_observation']['status']=='pending'
    assert data['network_observation']['current_ip_verified'] is False
    assert data['network_observation']['last_known_ip_addresses']==['192.0.2.50']
    operation=client.get('/v1/operations/'+op,headers=auth).json()
    assert operation['status']=='failed' and operation['error_code']=='NETWORK_DISCOVERY_TIMEOUT'


def test_reboot_may_replace_verified_dhcp_address(client,auth,sessions,network):
    identifier,_=network
    first=request(client,auth,identifier,key='discover-before-reboot').json()['operation_id']
    execute_discovery(first,sessions,Mock(return_value={
        'status':'observed','ip_addresses':['192.0.2.50'],'source':'qemu-guest-agent'}))
    second=request(client,auth,identifier,key='discover-after-reboot').json()['operation_id']
    execute_discovery(second,sessions,Mock(return_value={
        'status':'observed','ip_addresses':['192.0.2.51'],'source':'qemu-guest-agent'}))
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['ip_addresses']==['192.0.2.51']
    assert data['network_observation']['previous_ip_addresses']==['192.0.2.50']


def test_followup_dispatch_failure_keeps_last_ip_as_history(sessions,network,monkeypatch):
    identifier,_=network
    with sessions() as s:
        instance=s.get(Instance,identifier)
        instance.self_service_policy_id='fixture-policy'
        instance.ip_addresses=['192.0.2.50']
        instance.network_observation={'status':'observed','ip_addresses':['192.0.2.50'],
            'current_ip_verified':True,'observed_at':utcnow().isoformat()}
        power=Operation(id='power-followup',tenant_id=instance.tenant_id,instance_id=identifier,
            kind='instance.power.reboot',status='completed',idempotency_key='reboot-followup',
            request_hash='a'*64,requested_by='bootstrap')
        s.add(power);s.commit()
    monkeypatch.setattr('iacp.api.dependencies.get_network_publisher',lambda:Mock(side_effect=RuntimeError('queue')))
    after_power('power-followup',sessions)
    with sessions() as s:
        instance=s.get(Instance,identifier)
        assert instance.ip_addresses==[]
        assert instance.network_observation['status']=='pending'
        assert instance.network_observation['reason']=='NETWORK_FOLLOWUP_DISPATCH_UNCONFIRMED'
        assert instance.network_observation['last_known_ip_addresses']==['192.0.2.50']

def test_discovery_denies_anonymous_reader_other_tenant_and_overrides(client,auth,network,identities):
    identifier,publish=network
    assert request(client,{},identifier).status_code==401
    assert request(client,identities['tenant-a-reader']['headers'],identifier).status_code==403
    assert request(client,identities['tenant-b-operator']['headers'],identifier).status_code==404
    r=client.post(f'/v1/instances/{identifier}/network/discover',headers={**auth,'Idempotency-Key':'bad'},json={'vmid':104})
    assert r.status_code==422
    publish.assert_not_called()

def test_discovery_fails_closed_if_ownership_changes_after_queue(client,auth,sessions,network):
    identifier,publish=network
    op=request(client,auth,identifier).json()['operation_id']
    with sessions() as s:
        s.get(TerraformExecution,identifier).plan_sha256='f'*64;s.commit()
    observer=Mock();execute_discovery(op,sessions,observer);observer.assert_not_called()
    assert client.get('/v1/operations/'+op,headers=auth).json()['status']=='failed'
    assert client.get('/v1/instances/'+identifier,headers=auth).json()['status']=='ready'

def test_agent_filters_by_bound_mac_and_ipv4_address():
    rows={'result':[{'hardware-address':'wrong','ip-addresses':[{'ip-address':'192.0.2.99'}]},
        {'hardware-address':'AA:BB:CC:DD:EE:FF','ip-addresses':[{'ip-address':a} for a in ['127.0.0.1','169.254.1.2','::1','0.0.0.0','192.0.2.5','invalid']]}]}
    assert agent_addresses(rows,'aa:bb:cc:dd:ee:ff')['ip_addresses']==['192.0.2.5']

def test_observation_is_available_while_admission_is_drained(client,auth,network,monkeypatch):
    monkeypatch.setenv('IACP_ADMISSION_ENABLED','0')
    assert request(client,auth,network[0]).status_code==202

@pytest.mark.parametrize('mode',['valid','wrong-bridge','agent-offline'])
def test_real_observer_binds_native_dhcp_network_before_querying_agent(monkeypatch,mode):
    from types import SimpleNamespace
    from iacp.services.network import observe_guest
    from iacp.services.instances import Conflict
    from iacp.lifecycle.proxmox import PowerError,ProxmoxPower
    monkeypatch.setattr('iacp.terraform.lifecycle.build_executor',lambda **kw:SimpleNamespace(environment=lambda path:{
        'PROXMOX_VE_ENDPOINT':'https://synthetic.invalid','PROXMOX_VE_API_TOKEN':'synthetic'}))
    calls=[]
    def get(self,method,path,parameters=None):
        calls.append((method,path))
        if '/config?' in path:
            return {'name':'one','net0':'virtio=AA:BB:CC:DD:EE:FF,bridge='+('wrong' if mode=='wrong-bridge' else 'vmbr0'),'ipconfig0':'ip=dhcp'}
        if mode=='agent-offline':raise PowerError('POWER_PROXMOX_UNAVAILABLE')
        return {'result':[{'hardware-address':'AA:BB:CC:DD:EE:FF','ip-addresses':[{'ip-address':'192.0.2.3'}]}]}
    monkeypatch.setattr(ProxmoxPower,'_request',get)
    execution=SimpleNamespace(allocation={'network_bridge':'vmbr0','vlan_id':None})
    instance=SimpleNamespace(name='one',node='fixture',vmid=30000,result={'network':{'mac_address':'AA:BB:CC:DD:EE:FF'}})
    if mode=='wrong-bridge':
        with pytest.raises(Conflict,match='NETWORK_BINDING_CHANGED'):observe_guest(execution,instance)
        assert len(calls)==1
    else:
        result=observe_guest(execution,instance)
        assert result['status']==('observed' if mode=='valid' else 'pending')
    assert all(method=='GET' for method,_ in calls)
