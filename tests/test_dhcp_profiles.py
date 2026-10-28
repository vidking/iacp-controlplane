"""DHCP policy, static compatibility and durable identity; no enterprise traffic."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from iacp.models import Instance,TerraformExecution,BOOTSTRAP_TENANT_ID
from iacp.terraform import profiles
from iacp.terraform.config import Allocation,PlanError
from iacp.terraform.lifecycle import execute_plan
from iacp.commercial_contract import MINIMAL_EXAMPLE
from tests.test_terraform import allocation_data,FakeExecutor,terraform_mode
from tests.test_instances import identities, SPEC


def profile_data(tenant=BOOTSTRAP_TENANT_ID):
    settings=allocation_data(30000)
    for k in ['vm_id','ipv4_cidr','ipv4_gateway']:settings.pop(k)
    settings.update(image='ubuntu-2404',template_vm_id=29000,template_node_name='mng01',
                     node_name='mng01',clone_datastore_id='DS_CYBERFUEL',
                     cloudinit_datastore_id='DS_CYBERFUEL',cloudinit_username='root',
                     guest_agent_enabled=True,network_mode='dhcp',network_bridge='vmbr0',
                     vlan_id=None,allowed_nodes=['mng01'])
    settings['boot_disk']['datastore_id']='DS_CYBERFUEL'
    return {'profiles':{'lab-dhcp':{'allowed_tenants':[tenant],'settings':settings,'vmid_min':30000,'vmid_max':30010,
                                  'max_live_instances':1,'max_vcpus':8,'max_memory_mb':16384,'max_disk_gb':400,
                                  'max_total_vcpus':16,'max_total_memory_mb':32768,'max_total_disk_gb':800,
                                  'max_concurrent_operations':2,'self_service_enabled':True,
                                  'guest_initialization':'ubuntu-2404-lab-identity-v1'}}}


@pytest.fixture
def policy(tmp_path,monkeypatch):
    p=tmp_path/'profiles.json';p.write_text(json.dumps(profile_data()))
    monkeypatch.setenv('IACP_TERRAFORM_PROFILES_FILE',str(p))
    monkeypatch.setattr(profiles,'cluster_vmids',lambda:set())
    return p


def create(client,auth,key='dhcp-one',**changes):
    body={**MINIMAL_EXAMPLE,'name':'dhcp-test','start_on_create':False,
          'access':{**MINIMAL_EXAMPLE['access'],'root_password':'SENUELO-DHCP-NO-PERSISTIR-2026'},**changes}
    return client.post('https://testserver/v1/instances',headers={**auth,'Idempotency-Key':key},json=body)


def test_dhcp_does_not_open_ipam_and_seals_identity(client,auth,terraform_mode,policy,sessions,monkeypatch):
    monkeypatch.setenv('IACP_TERRAFORM_ALLOCATIONS_FILE','/must-not-be-opened/ipam.json')
    response=create(client,auth);assert response.status_code==202,response.text
    data=response.json()
    with sessions() as s:
        row=s.get(Instance,data['instance_id']);e=s.get(TerraformExecution,row.id)
        assert row.network_mode=='dhcp' and e.vmid==30000
        assert e.allocation['ipv4_cidr'] is None and e.allocation['ipv4_gateway'] is None and e.allocation['vlan_id'] is None
    assert create(client,auth).json()==data
    fake=FakeExecutor()
    execute_plan(data['job_id'],1,sessions,fake,load_allocation=lambda i:pytest.fail('DHCP must skip IPAM'))
    assert fake.calls[0][4]['instance']['network_mode']=='dhcp'
    assert fake.calls[0][4]['instance']['ipv4_cidr'] is None
    assert client.get('/v1/instances/'+data['instance_id'],headers=auth).json()['ip_addresses']==[]


def test_dhcp_allocates_multiple_instances_without_stale_profile_quota(
        client,auth,terraform_mode,policy,sessions,monkeypatch):
    monkeypatch.setattr(profiles,'cluster_vmids',lambda:{30000,30001})
    r=create(client,auth).json()
    with sessions() as s:assert s.get(TerraformExecution,r['instance_id']).vmid==30002
    second=create(client,auth,key='second',name='another-guest')
    assert second.status_code==202,second.text
    with sessions() as s:
        assert s.get(TerraformExecution,second.json()['instance_id']).vmid==30003


def test_profile_resources_and_tenant_authorization(client,auth,terraform_mode,policy,identities):
    assert create(client,identities['tenant-b-operator']['headers']).status_code==409
    assert create(client,auth,vcpus=64).status_code==422


def test_static_variables_remain_historical():
    a=Allocation.model_validate(allocation_data())
    from types import SimpleNamespace
    v=a.variables(SimpleNamespace(**SPEC))
    assert 'network_mode' not in v['instance'] and v['instance']['vlan_id']==204
    assert v['instance']['ipv4_cidr']=='192.0.2.10/24'
    with pytest.raises(ValueError):Allocation.model_validate({**allocation_data(),'network_mode':'dhcp'})


def test_policy_has_no_cluster_name_condition():
    data=allocation_data();data.update(node_name='authorized-lab-node',allowed_nodes=['authorized-lab-node'])
    assert Allocation.model_validate(data).node_name=='authorized-lab-node'


@pytest.mark.parametrize('vlan',[0,-1,4095])
def test_never_substitute_zero_for_no_tag(vlan):
    data=allocation_data();data.update(network_mode='dhcp',vlan_id=vlan,ipv4_cidr=None,ipv4_gateway=None)
    with pytest.raises(ValueError):Allocation.model_validate(data)
