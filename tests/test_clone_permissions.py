"""Read-only effective privilege checks fence DHCP before Terraform writes."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from iacp.terraform.config import Allocation,PlanError
from iacp.terraform.executor import TerraformExecutor
from iacp.terraform.observation import require_clone_permissions
from tests.test_terraform import allocation_data
from tests.test_instances import SPEC

@pytest.fixture
def target():
    data=allocation_data(30000);data.update(network_mode='dhcp',vlan_id=None,ipv4_cidr=None,ipv4_gateway=None)
    a=Allocation.model_validate(data);return a,a.variables(SimpleNamespace(**SPEC))['instance']

@pytest.fixture
def permission_api(monkeypatch):
    from iacp.lifecycle.proxmox import ProxmoxPower
    grants={'/vms/9000':{'VM.Clone':1},'/vms/30000':{'VM.Allocate':1},'/storage/fixture-storage':{'Datastore.AllocateSpace':1},
            '/sdn/zones/localnetwork/fixture-bridge':{'SDN.Use':1}}
    calls=[]
    def get(self,method,path,parameters=None):
        calls.append((method,path));resource=path.split('path=',1)[1];return {resource:grants.get(resource,{})}
    monkeypatch.setattr(ProxmoxPower,'_request',get)
    return grants,calls

def env():return {'PROXMOX_VE_ENDPOINT':'https://fixture.invalid','PROXMOX_VE_API_TOKEN':'synthetic'}

@pytest.mark.parametrize('path,key,code',[
    ('/vms/9000','VM.Clone','VM_CLONE_PERMISSION_REQUIRED'),('/vms/30000','VM.Allocate','VM_ALLOCATE_PERMISSION_REQUIRED'),
    ('/storage/fixture-storage','Datastore.AllocateSpace','DATASTORE_ALLOCATION_PERMISSION_REQUIRED'),
    ('/sdn/zones/localnetwork/fixture-bridge','SDN.Use','SDN_USE_PERMISSION_REQUIRED')])
def test_missing_permission_fails_closed_with_no_proxmox_post(target,permission_api,path,key,code):
    grants,calls=permission_api;grants[path].pop(key)
    with pytest.raises(PlanError,match=code):require_clone_permissions(env(),target[1])
    assert all(method=='GET' for method,_ in calls)

@pytest.mark.parametrize('propagation',[0,1])
def test_both_effective_permission_flags_grant_exact_resource(target,permission_api,propagation):
    grants,calls=permission_api
    for p in grants:
        for key in grants[p]:grants[p][key]=propagation
    require_clone_permissions(env(),target[1]);assert len(calls)==4

def test_revoked_permission_fails_before_initialization_or_approval_consumption(tmp_path,monkeypatch,target,permission_api):
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    permission_api[0]['/vms/9000'].pop('VM.Clone')
    executor=TerraformExecutor(tmp_path/'source',tmp_path/'artifacts',runner=Mock())
    monkeypatch.setattr(executor,'prepare_apply',Mock());monkeypatch.setattr(executor,'environment',lambda _:env())
    initialize=Mock();monkeypatch.setattr(executor,'initialize',initialize);before=Mock()
    with pytest.raises(PlanError,match='VM_CLONE_PERMISSION_REQUIRED'):
        executor.apply('00000000-0000-4000-8000-000000000001',1,'a'*64,'owned/key',target[0].model_dump(),{'instance':target[1]},before_mutation=before)
    initialize.assert_not_called();before.assert_not_called();executor.runner.apply_saved.assert_not_called()
