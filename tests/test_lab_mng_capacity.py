"""Excepción comercial LAB persistente, acotada y sin eliminar barreras técnicas."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from iacp.models import BOOTSTRAP_TENANT_ID
from iacp.security import Principal
from iacp.services import commercial
from iacp.services.instances import Conflict
from tests.test_commercial_admission import commercial_config, store, specification
from tests.test_commercial_catalog import catalog_profile


def enable():
    path = Path(os.environ['IACP_COMMERCIAL_LIMITS_FILE'])
    raw = json.loads(path.read_text())
    row = raw['tenants'][BOOTSTRAP_TENANT_ID]
    row.update(lab_mng_physical_capacity_only=True,
               allowed_plans=['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
    path.write_text(json.dumps(raw))
    return commercial.limits_for(BOOTSTRAP_TENANT_ID)


def test_exception_survives_reload_and_skips_commercial_budget_including_concurrency(commercial_config, monkeypatch):
    limits = enable()
    used = dict(instances=10000, vcpus=10000, memory_mb=1000000, disk_gb=1000000,
                concurrent_operations=0)
    monkeypatch.setattr(commercial, 'usage', lambda *_: used)
    commercial.quota(None, BOOTSTRAP_TENANT_ID, limits,
                     dict(vcpus=8, memory_mb=16384, disk_gb=400))
    assert commercial.limits_for(BOOTSTRAP_TENANT_ID) == limits
    used['concurrent_operations'] = 1
    commercial.quota(None, BOOTSTRAP_TENANT_ID, limits)


def test_existing_quotas_unchanged_without_explicit_exception(commercial_config, monkeypatch):
    limits = commercial.limits_for(BOOTSTRAP_TENANT_ID)
    monkeypatch.setattr(commercial, 'usage', lambda *_: dict(
        instances=2, vcpus=0, memory_mb=0, disk_gb=0, concurrent_operations=0))
    for tenant in [BOOTSTRAP_TENANT_ID, 'another-tenant']:
        with pytest.raises(Conflict, match='QUOTA_EXCEEDED:instances'):
            commercial.quota(None, tenant, limits, dict(vcpus=1, memory_mb=2048, disk_gb=50))


@pytest.mark.parametrize('change', ['tenant', 'node', 'allowed_nodes', 'storage', 'profile'])
def test_exception_rejects_scope_change(commercial_config, change):
    limits = enable()
    tenant = BOOTSTRAP_TENANT_ID
    if change == 'tenant': tenant = '0294373d-7075-4eb5-9dca-9ff256298828'
    else:
        raw = json.loads(commercial_config.read_text())
        settings = raw['profiles']['lab-dhcp']['settings']
        if change == 'node': settings['node_name'] = 'production'
        if change == 'allowed_nodes': settings['allowed_nodes'].append('production')
        if change == 'storage': settings['boot_disk']['datastore_id'] = 'production'
        if change == 'profile': raw['profiles'].pop('lab-dhcp')
        commercial_config.write_text(json.dumps(raw))
    with pytest.raises(Conflict, match='EXCEPTION_SCOPE_INVALID'):
        commercial.quota(None, tenant, limits)


@pytest.mark.parametrize('plan', ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
def test_minimal_plans_reach_real_capacity_barrier(sessions, commercial_config, store, plan):
    enable()
    body = specification(plan).model_dump()
    body.pop('network'); body.pop('commercial_context')
    from iacp.commercial_contract import CommercialInstanceCreate
    spec = CommercialInstanceCreate.model_validate(body)
    capacity = Mock(side_effect=Conflict('COMMERCIAL_PHYSICAL_CAPACITY_EXCEEDED'))
    publish = Mock()
    with sessions() as session:
        with pytest.raises(Conflict, match='PHYSICAL_CAPACITY_EXCEEDED'):
            commercial.create(session, Principal(BOOTSTRAP_TENANT_ID, 'lab-swagger-postman', 'operator'),
                              spec, 'capacity-'+plan, publish, store, capacity=capacity)
    assert capacity.call_count == 1
    publish.assert_not_called()
    assert not store.redis.values


def test_versioned_configuration_explicitly_scoped():
    raw = json.loads(Path('docs/deployment/commercial-limits.lab-mng.json').read_text())
    assert set(raw['tenants']) == {BOOTSTRAP_TENANT_ID}
    row = commercial.Limits.model_validate(raw['tenants'][BOOTSTRAP_TENANT_ID])
    assert row.lab_mng_physical_capacity_only and row.max_concurrent_operations == 1
    assert row.allowed_plans == ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4']


@pytest.mark.parametrize('plan', ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
def test_minimal_creation_keeps_idempotency_and_exact_plan(sessions, commercial_config, store, plan):
    enable()
    from iacp.catalog import resolve_plan
    from iacp.commercial_contract import CommercialInstanceCreate
    from iacp.models import Instance
    body = specification(plan).model_dump()
    body.pop('network'); body.pop('commercial_context')
    spec = CommercialInstanceCreate.model_validate(body)
    capacity, publish = Mock(), Mock()
    principal = Principal(BOOTSTRAP_TENANT_ID, 'lab-swagger-postman', 'operator')
    with sessions() as session:
        accepted = commercial.create(session, principal, spec, 'minimal-'+plan, publish, store, capacity=capacity)
        replay = commercial.create(session, principal, spec, 'minimal-'+plan, publish, store, capacity=capacity)
        assert accepted == replay
        instance = session.get(Instance, accepted.instance_id)
        expected = resolve_plan(plan)
        assert (instance.vcpus, instance.memory_mb, instance.disk_gb) == (
            expected.vcpus, expected.memory_mb, expected.disk_gb)
        assert instance.commercial_context is None and instance.network_profile == 'lab-dhcp'
    assert capacity.call_count == publish.call_count == 1


@pytest.mark.parametrize('image,vmid', [('ubuntu-2404',29000),('ubuntu-2604',29001),('almalinux-9',29010),('almalinux-10',29011)])
@pytest.mark.parametrize('plan', ['kvm-1','kvm-2','kvm-3','kvm-4'])
def test_four_images_resolve_internal_template_without_public_vmid(sessions, commercial_config, store, image, vmid, plan):
    enable()
    raw=json.loads(commercial_config.read_text())
    raw['lab_images']={key:{'enabled':True,'cloudinit_username':'root'}
        for key in ['ubuntu-2604','almalinux-9','almalinux-10']}
    commercial_config.write_text(json.dumps(raw))
    from iacp.commercial_contract import CommercialInstanceCreate
    from iacp.models import Instance, TerraformExecution
    # La parametrización se prueba con cuerpos mínimos y el mismo tenant LAB.
    body=specification(plan).model_dump();body.pop('network');body.pop('commercial_context')
    body['image_id']=image
    spec=CommercialInstanceCreate.model_validate(body)
    with sessions() as session:
        result=commercial.create(session,Principal(BOOTSTRAP_TENANT_ID,'lab-swagger-postman','operator'),
            spec,'images-'+image,Mock(),store,capacity=Mock())
        instance=session.get(Instance,result.instance_id)
        allocation=session.get(TerraformExecution,result.instance_id).allocation
        assert instance.network_profile=='lab-dhcp' and instance.image==image
        assert allocation['template_vm_id']==vmid
        assert allocation['clone_datastore_id']=='DS_CYBERFUEL'


def test_four_images_available_in_explicit_lab_configuration_only(sessions, commercial_config, monkeypatch):
    enable()
    raw=json.loads(commercial_config.read_text())
    raw['lab_images']={key:{'enabled':True,'cloudinit_username':'root'}
        for key in ['ubuntu-2604','almalinux-9','almalinux-10']}
    commercial_config.write_text(json.dumps(raw))
    from iacp.services import bootstrap_secrets
    from iacp.services.catalog import images_for
    monkeypatch.setenv('IACP_CREATION_CONTRACT','commercial-g')
    monkeypatch.setattr(bootstrap_secrets,'configured_key',lambda:b'x'*32)
    monkeypatch.setattr(commercial,'live_capacity',Mock())
    with sessions() as session:
        rows=images_for(BOOTSTRAP_TENANT_ID,session)
        assert len(rows)==4
        assert all(row['status']=='habilitada' and row['authorized'] and row['availability']['available'] for row in rows)
        assert all(row['compatible_plans']==['kvm-1','kvm-2','kvm-3','kvm-4'] for row in rows)
    raw['lab_images']['almalinux-9']['enabled']=False
    commercial_config.write_text(json.dumps(raw))
    with sessions() as session:
        row=next(r for r in images_for(BOOTSTRAP_TENANT_ID,session) if r['image_id']=='almalinux-9')
        assert row['status']=='habilitada' and not row['authorized'] and not row['availability']['available']


def test_programs_are_fixed_and_specific_to_image():
    from iacp.services.guest_identity import MODE,PROGRAM,PROGRAM_SHA256,program_for_mode
    from iacp.services.guest_image_programs import MODES
    import hashlib
    assert program_for_mode(MODE)==(PROGRAM,PROGRAM_SHA256)
    for mode in MODES:
        program,digest=program_for_mode(mode)
        compile(program,'guest-'+mode,'exec')
        assert hashlib.sha256(program.encode()).hexdigest()==digest
        assert mode in program and 'password' not in program.lower()
        if mode.startswith('almalinux'):
            assert "cloud-init','status','--format','json" in program
            assert "cloud-init','status','--wait" not in program
            assert "nmcli','connection','load" not in program
            assert "nmcli','connection','up" not in program
            assert 'nmcli' in program and 'dhcp-client-id=mac' in program
            assert 'machine_id.write_text(machine)' in program and 'path.parent.is_dir()' in program
        else:
            assert 'VERSION_ID="26.04"' in program and 'VERSION_ID="24.04"' not in program
    with pytest.raises(Conflict):program_for_mode('imagen-arbitraria')


@pytest.mark.parametrize('mode', ['ubuntu-2604-lab-identity-v1','almalinux-9-lab-identity-v1','almalinux-10-lab-identity-v1'])
def test_new_initializers_use_fixed_program_and_matching_evidence(mode):
    import hashlib
    from types import SimpleNamespace
    from urllib.parse import parse_qs,urlsplit
    from iacp.services.guest_identity import GuestClient,program_for_mode,MARKER,NETWORK_FILE
    from iacp.services.guest_image_programs import NETWORK_MANAGER_FILE
    identifier='00000000-0000-4000-8000-000000000077';mac='bc:24:11:12:34:56'
    program,digest=program_for_mode(mode)
    machine,network='maquina-unica\n','configuracion-DHCP-fija\n'
    marker=dict(instance_id=identifier,mac_address=mac,mode=mode,program_sha256=digest,ifindex=2,
        machine_id_sha256=hashlib.sha256(machine.encode()).hexdigest(),
        network_sha256=hashlib.sha256(network.encode()).hexdigest(),network_manager_dhcp=True)
    seen=[]
    def request(method,path,body=None):
        seen.append((method,path,body))
        if method=='POST':return {'pid':42}
        if 'network-get-interfaces' in path:
            return {'result':[{'hardware-address':mac,'ip-addresses':[{'ip-address-type':'ipv4','ip-address':'192.0.2.10','prefix':24}]}]}
        file=parse_qs(urlsplit(path).query)['file'][0]
        values={MARKER:json.dumps(marker),'/etc/machine-id':machine,
                NETWORK_MANAGER_FILE if mode.startswith('almalinux') else NETWORK_FILE:network,
                '/run/systemd/netif/leases/2':'CLIENTID=01bc2411123456\nADDRESS=192.0.2.10\n'}
        return {'content':values[file]}
    client=GuestClient(SimpleNamespace(_request=request))
    target=SimpleNamespace(path='/nodes/mng01/qemu/30001')
    assert client.submit_image(target,identifier,mac,mode)==42
    command=seen[0][2]
    assert command==[('command','/usr/bin/python3'),('command','-c'),('command',program),
                     ('command',identifier),('command',mac),('command',digest)]
    row=SimpleNamespace(pid=None,instance_id=identifier,mac_address=mac,mode=mode,program_sha256=digest)
    assert client.observe(target,row)['network']['ip_addresses']==['192.0.2.10']
    assert all(method=='GET' for method,_,_ in seen[1:])
    marker['network_sha256']='0'*64
    with pytest.raises(Conflict):client.observe(target,row)
