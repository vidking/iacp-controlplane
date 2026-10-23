"""Network discovery is read-only, tenant scoped and bound to a guest MAC."""
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from iacp.models import BOOTSTRAP_TENANT_ID,Instance,TerraformExecution,Operation,NetworkDiscovery
from iacp.services.network import agent_addresses,execute_discovery
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

@pytest.mark.parametrize('observed',[False,True])
def test_agent_pending_never_fails_instance_and_replay_never_observes_twice(client,auth,sessions,network,observed):
    identifier,publish=network
    r=request(client,auth,identifier);assert r.status_code==202,r.text
    op=r.json()['operation_id'];publish.assert_called_once_with(op)
    result={'status':'observed' if observed else 'pending','ip_addresses':['192.0.2.50'] if observed else [],'source':'qemu-guest-agent'}
    observer=Mock(return_value=result)
    execute_discovery(op,sessions,observer);execute_discovery(op,sessions,observer)
    observer.assert_called_once()
    assert request(client,auth,identifier).json()['operation_id']==op
    data=client.get('/v1/instances/'+identifier,headers=auth).json()
    assert data['status']=='ready' and data['ip_addresses']==result['ip_addresses']
    assert data['result']['network']=={'mac_address':'aa:bb:cc:dd:ee:ff'}
    assert client.get('/v1/operations/'+op,headers=auth).json()['network']['status']==result['status']

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
