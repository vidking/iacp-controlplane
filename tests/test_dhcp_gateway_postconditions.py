"""Known DHCP gateway null -> empty representation of bpg/proxmox 0.116.1."""
from copy import deepcopy
import pytest
from iacp.terraform.config import PlanError
from iacp.terraform.executor import verify_postconditions


def fixture(address='dhcp',gateway=None,provider='0.116.1'):
    after={'initialization':[{'ip_config':[{'ipv4':[{'address':address,'gateway':gateway}]}]}]}
    approved={'resource_changes':[{'provider_name':'registry.terraform.io/bpg/proxmox','change':{'after':after,'after_unknown':{},'after_sensitive':{}}}],
              'configuration':{'provider_config':{'proxmox':{'full_name':'registry.terraform.io/bpg/proxmox','version_constraint':provider}}}}
    return approved,deepcopy(after)


def test_pinned_dhcp_missing_gateway_is_equivalent_to_empty_readback():
    approved,actual=fixture();actual['initialization'][0]['ip_config'][0]['ipv4'][0]['gateway']=''
    verify_postconditions(approved,actual)
    verify_postconditions(approved,actual,source='refreshed_state',persisted=actual)


@pytest.mark.parametrize('address,gateway,found,provider',[
    ('dhcp',None,'192.0.2.1','0.116.1'),('dhcp',None,0,'0.116.1'),
    ('dhcp',None,'','0.116.0'),('192.0.2.10/24',None,'','0.116.1'),
    ('192.0.2.10/24','192.0.2.1','','0.116.1')])
def test_gateway_semantics_cannot_waive_static_policy_or_wrong_provider(address,gateway,found,provider):
    approved,actual=fixture(address,gateway,provider);actual['initialization'][0]['ip_config'][0]['ipv4'][0]['gateway']=found
    with pytest.raises(PlanError,match='APPROVED_POSTCONDITION_MISMATCH'):verify_postconditions(approved,actual)
