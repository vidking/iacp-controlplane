"""Catálogos y revisión candidata G, sin mutaciones ni credenciales reales."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator, RefResolver
from openapi_spec_validator import validate
from pydantic import ValidationError
import pytest

from iacp.catalog import CATALOG_VERSION, IMAGES, PLANS, catalog_digest, resolve_image, resolve_plan
from iacp.commercial_contract import CommercialInstanceCreate, EXAMPLES, require_safe_execution, resolved_resources
from iacp.models import BOOTSTRAP_TENANT_ID
from iacp.services.catalog import authorized_profile, plan_availability
from tests.test_dhcp_profiles import profile_data
from tests.test_instances import identities

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog_profile(tmp_path, monkeypatch):
    data = profile_data()
    settings = data['profiles']['lab-dhcp']['settings']
    settings.update(image='ubuntu-2404', template_vm_id=29000, template_node_name='mng01', node_name='mng01',
        clone_datastore_id='DS_CYBERFUEL', cloudinit_datastore_id='DS_CYBERFUEL',
        guest_agent_enabled=True, network_bridge='vmbr0')
    settings['boot_disk']['datastore_id'] = 'DS_CYBERFUEL'
    settings['allowed_nodes'] = ['mng01']
    data['profiles']['lab-dhcp']['max_disk_gb'] = 40
    p = tmp_path / 'profiles.json'
    p.write_text(json.dumps(data))
    monkeypatch.setenv('IACP_TERRAFORM_PROFILES_FILE', str(p))
    return p


def test_four_exact_plans_and_immutable_catalog():
    assert [(p.plan_id,p.vcpus,p.memory_mb,p.disk_gb) for p in PLANS] == [
        ('kvm-1',1,2048,50),('kvm-2',2,4096,100),('kvm-3',4,8192,200),('kvm-4',8,16384,400)]
    assert catalog_digest() == 'fa565a7f79f1ebd1c4bf2131c4cc0e0bd07e6598ea1e3c9f88f7082f919bd689'
    with pytest.raises(AttributeError):
        PLANS[0].disk_gb = 20
    with pytest.raises(ValueError, match='PLAN_NOT_FOUND'):
        resolve_plan('kvm-5')
    assert resolve_image('ubuntu-2404').template_vm_id == 29000
    with pytest.raises(ValueError, match='IMAGE_NOT_AVAILABLE'):
        resolve_image('debian-no-certificada')


@pytest.mark.parametrize('status',['pendiente','certificada','retirada'])
def test_only_enabled_images_resolve(monkeypatch,status):
    from dataclasses import replace
    from iacp import catalog
    monkeypatch.setattr(catalog,'IMAGES',(replace(IMAGES[0],status=status),))
    with pytest.raises(ValueError,match='IMAGE_NOT_AVAILABLE'):resolve_image('ubuntu-2404')


@pytest.mark.parametrize('path', ['/v1/plans', '/v1/images'])
def test_catalogs_require_bearer_and_never_expose_internal_targets(client, auth, catalog_profile, path):
    assert client.get(path).status_code == 401
    r = client.get(path, headers=auth)
    assert r.status_code == 200
    assert r.headers['X-IACP-Catalog-Version'] == CATALOG_VERSION
    assert r.headers['X-IACP-Catalog-SHA256'] == catalog_digest()
    assert all(not i['availability']['available'] for i in r.json())
    for forbidden in ['template_vm_id','source_node','source_storage','allowed_tenants','password','29000']:
        assert forbidden not in r.text


def test_authorization_and_existing_limits_are_not_expanded(client, auth, catalog_profile):
    rows = client.get('/v1/plans', headers=auth).json()
    assert len(rows) == 4
    assert all('ENVIRONMENT_PLAN_LIMIT_EXCEEDED' in r['availability']['reasons'] for r in rows)
    assert authorized_profile('11111111-1111-4111-8111-111111111111') is None
    before = catalog_profile.read_bytes()
    client.get('/v1/images', headers=auth)
    assert catalog_profile.read_bytes() == before


def test_reader_catalog_and_tenant_isolation(client,catalog_profile,identities):
    data=json.loads(catalog_profile.read_text())
    data['profiles']['lab-dhcp']['allowed_tenants']=[identities['tenant-a-reader']['tenant_id']]
    catalog_profile.write_text(json.dumps(data))
    own=client.get('/v1/images',headers=identities['tenant-a-reader']['headers'])
    other=client.get('/v1/images',headers=identities['tenant-b-reader']['headers'])
    assert own.status_code==other.status_code==200
    assert own.json()[0]['authorized'] is True
    assert other.json()[0]['authorized'] is False
    assert 'TENANT_PROFILE_NOT_AUTHORIZED' in other.json()[0]['availability']['reasons']


def test_quota_counts_whole_tenant_and_never_claims_physical_capacity(catalog_profile):
    profile = authorized_profile(BOOTSTRAP_TENANT_ID)
    assert profile is not None
    used = dict(instances=1,vcpus=8,memory_mb=8192,disk_gb=100,concurrent_operations=2)
    result = plan_availability(PLANS[0], profile, used)
    assert 'TENANT_TECHNICAL_QUOTA_EXCEEDED' in result['reasons']
    assert 'PHYSICAL_CAPACITY_NOT_VERIFIED' in result['reasons']
    assert not result['available'] and not result['capacity_verified']


@pytest.mark.parametrize('field,value', [('template_vm_id',106),('template_node_name','mng02'),
    ('clone_datastore_id','otro'),('cloudinit_datastore_id','otro'),('network_bridge','otro'),('vlan_id',204)])
def test_image_binding_rejects_configuration_drift(catalog_profile,field,value):
    data=json.loads(catalog_profile.read_text())
    data['profiles']['lab-dhcp']['settings'][field]=value
    catalog_profile.write_text(json.dumps(data))
    assert authorized_profile(BOOTSTRAP_TENANT_ID) is None


@pytest.mark.parametrize('example', EXAMPLES)
def test_canonical_examples_resolve_resources_without_password(example):
    spec=CommercialInstanceCreate.model_validate(example)
    resources=resolved_resources(spec)
    plan=resolve_plan(example['plan_id'])
    assert resources == dict(vcpus=plan.vcpus,memory_mb=plan.memory_mb,disk_gb=plan.disk_gb,image='ubuntu-2404')
    secret=spec.access.root_password.get_secret_value()
    assert secret not in repr(spec) and secret not in spec.model_dump_json()
    assert secret not in json.dumps(resources)
    with pytest.raises(ValueError,match='ROOT_BOOTSTRAP_OUTSIDE_STATE_UNAVAILABLE'):
        require_safe_execution(spec)


@pytest.mark.parametrize('field,value', [('vcpus',2),('memory_mb',4096),('disk_gb',100),
    ('self_service_policy_id','00000000-0000-4000-8000-000000000001'),('tenant_id',BOOTSTRAP_TENANT_ID),
    ('node_preference','mng01'),('image','ubuntu-2404'),('network_profile','lab-dhcp'),('profile_id','kvm-2')])
def test_obsolete_and_privileged_fields_explicitly_rejected(field,value):
    with pytest.raises(ValidationError) as e:
        CommercialInstanceCreate.model_validate({**EXAMPLES[0],field:value})
    assert any(x['type']=='extra_forbidden' and x['loc']==(field,) for x in e.value.errors())


@pytest.mark.parametrize('field', ['name','plan_id','image_id','network','access','commercial_context'])
def test_required_fields(field):
    data=deepcopy(EXAMPLES[0]);data.pop(field)
    with pytest.raises(ValidationError): CommercialInstanceCreate.model_validate(data)


def test_defaults_order_and_unsupported_image():
    data=deepcopy(EXAMPLES[0]);data.pop('start_on_create');data['access'].pop('ssh_public_keys')
    data['commercial_context'].pop('product_id')
    spec=CommercialInstanceCreate.model_validate(data)
    assert spec.start_on_create is True and spec.access.ssh_public_keys==[]
    data['commercial_context']={}
    with pytest.raises(ValidationError):CommercialInstanceCreate.model_validate(data)
    data=deepcopy(EXAMPLES[0]);data['image_id']='fedora-no-certificada'
    with pytest.raises(ValueError,match='IMAGE_NOT_AVAILABLE'):
        resolved_resources(CommercialInstanceCreate.model_validate(data))


@pytest.mark.parametrize('change',[dict(plan_id='kvm-5'),dict(start_on_create='true'),dict(plan_id=2),dict(name='Invalido')])
def test_invalid_product_types_and_names(change):
    with pytest.raises(ValidationError):CommercialInstanceCreate.model_validate({**EXAMPLES[0],**change})


@pytest.mark.parametrize('plan_id', ['kvm-1','kvm-2','kvm-3','kvm-4'])
def test_all_sizes_resolve_without_provisioning(plan_id):
    spec=CommercialInstanceCreate.model_validate({**EXAMPLES[0],'plan_id':plan_id})
    assert resolved_resources(spec)['disk_gb']==resolve_plan(plan_id).disk_gb


@pytest.mark.parametrize('plan_id', ['kvm-1','kvm-2','kvm-3','kvm-4'])
def test_resolved_disk_expands_existing_terraform_variables(plan_id):
    from types import SimpleNamespace
    from iacp.terraform.config import Allocation
    from tests.test_terraform import allocation_data
    spec=CommercialInstanceCreate.model_validate({**EXAMPLES[0],'plan_id':plan_id})
    resources=resolved_resources(spec)
    allocation=Allocation.model_validate(allocation_data(image='ubuntu-2404'))
    variables=allocation.variables(SimpleNamespace(name=spec.name, **resources))
    assert variables['instance']['boot_disk']['size_gb']==resolve_plan(plan_id).disk_gb
    assert variables['instance']['cpu_cores']==resolve_plan(plan_id).vcpus
    assert variables['instance']['memory_mb']==resolve_plan(plan_id).memory_mb
    assert 'iacp_root_password' not in variables
    assert spec.access.root_password.get_secret_value() not in json.dumps(variables)


def test_password_and_real_public_key_together():
    data=deepcopy(EXAMPLES[0])
    key=ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    data['access']['ssh_public_keys']=[key+' cliente@equipo']
    spec=CommercialInstanceCreate.model_validate(data)
    assert len(spec.access.ssh_public_keys)==1
    data['access']['ssh_public_keys'].append(key+' otro-comentario')
    with pytest.raises(ValidationError):CommercialInstanceCreate.model_validate(data)


@pytest.mark.parametrize('access', [dict(root_password='corta'),dict(root_password='x'*129),
    dict(root_password='x'*16+'\n'),dict(root_password='x'*16,ssh_public_keys=['ssh-ed25519 AAAA...']),
    dict(root_password='x'*16,ssh_public_keys=['-----BEGIN PRIVATE KEY-----'])])
def test_invalid_access_never_prints_password(access):
    with pytest.raises(ValidationError) as error:
        CommercialInstanceCreate.model_validate({**EXAMPLES[0],'access':access})
    assert access['root_password'] not in str(error.value)


def test_dhcp_rejects_static_overrides_and_wan_is_always_blocked():
    data=deepcopy(EXAMPLES[0]);data['network']['ipv4_address']='192.0.2.10'
    with pytest.raises(ValidationError):CommercialInstanceCreate.model_validate(data)
    data['network']=dict(profile_id='wan-static',ipv4_address='192.0.2.10',prefix_length=24,gateway='192.0.2.1')
    spec=CommercialInstanceCreate.model_validate(data)
    with pytest.raises(ValueError,match='WAN_STATIC_EXECUTION_NOT_CERTIFIED'):require_safe_execution(spec)
    for change in [dict(vlan_id=204),dict(ipv4_address='192.0.2.0'),dict(gateway='198.51.100.1')]:
        invalid={**data,'network':{**data['network'],**change}}
        with pytest.raises(ValidationError):CommercialInstanceCreate.model_validate(invalid)


def test_candidate_openapi_valid_examples_and_active_contract_is_historical():
    spec=importlib.util.spec_from_file_location('commercial_export',ROOT/'scripts/export-commercial-candidate.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    contract=module.candidate_contract();validate(contract)
    media=contract['paths']['/v1/instances']['post']['requestBody']['content']['application/json']
    resolver=RefResolver.from_schema(contract)
    for example in media['examples'].values():
        Draft202012Validator(media['schema'],resolver=resolver).validate(example['value'])
    from iacp.main import create_app
    current=create_app().openapi()
    assert current['paths']['/v1/instances']['post']['requestBody']['content']['application/json']['schema']['$ref'].endswith('/InstanceCreate')
    assert contract['x-iacp-deployment-status']=='BLOCKED_G6_Y_MIGRACION'


def test_local_manual_candidate_examples_match_real_schema(client,auth):
    from html import unescape
    import re
    page=client.get('/docs',headers=auth).text
    section=page.split('id="crm-catalogo-g"')[1].split('id="crm-introduccion"')[0]
    examples=re.findall(r'<pre><code>(.*?)</code></pre>',section,re.S)
    assert len(examples)==2
    for body in examples:
        spec=CommercialInstanceCreate.model_validate_json(unescape(body))
        assert resolved_resources(spec)['disk_gb'] in {50,100}
    assert 'creación todavía bloqueada' in section
