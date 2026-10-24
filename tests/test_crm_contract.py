import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from iacp.api.dependencies import get_access_verification_publisher
from iacp.models import Instance, Operation, TerraformApply, TerraformExecution
from iacp.services.access import decrypt_password, execute_verification, request_verification
from iacp.services.fulfillment import advance, after_apply
from iacp.security import Principal
from iacp.terraform.config import Allocation, PlanError
from iacp.terraform.access_adapter import install, verify, DIGEST, FILES, BASE_MAIN
from tests.test_instances import SPEC, create as plain_create, identities
from tests.test_dhcp_profiles import policy
from tests.test_lab_self_service import POLICY_SPEC, create, lab, provision
from tests.test_terraform import terraform_mode

CONTEXT = {'customer_id': 'customer-7', 'order_id': 'order-9', 'product_id': 'kvm-small'}
PASSWORD = 'synthetic-only-S3cure!'


def test_commercial_references_are_persisted_and_hashed(client, auth, sessions):
    r = plain_create(client, auth, spec={**SPEC, 'commercial_context': CONTEXT})
    assert r.status_code == 202
    assert plain_create(client, auth, spec={**SPEC, 'commercial_context': CONTEXT}).json() == r.json()
    assert plain_create(client, auth, spec={**SPEC, 'commercial_context': {**CONTEXT, 'order_id': 'other'}}).status_code == 409
    instance = client.get('/v1/instances/' + r.json()['instance_id'], headers=auth).json()
    assert instance['commercial_context'] == CONTEXT
    with sessions() as s:
        operation = s.get(Operation, r.json()['operation_id'])
        assert operation.correlation_id == r.headers['X-Request-ID']


def test_legacy_hash_unchanged_by_new_defaults(client, auth, sessions):
    r = plain_create(client, auth)
    assert plain_create(client, auth, spec={**SPEC, 'start_on_create': False, 'commercial_context': None}).json() == r.json()
    with sessions() as s:
        assert s.get(Operation, r.json()['operation_id']).request_hash == hashlib.sha256(
            json.dumps(SPEC, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_new_input_errors_never_echo_secrets(client, auth):
    for change in ({'password': PASSWORD}, {'access': {'username': 'ubuntu', 'password': PASSWORD}},
            {'access': {'username': 'root', 'password': 'short-secret'}}):
        r = plain_create(client, auth, spec={**SPEC, **change})
        assert r.status_code == 422 and PASSWORD not in r.text and 'short-secret' not in r.text
    assert plain_create(client, auth, spec={**SPEC, 'access': {'username': 'root', 'password': PASSWORD}}).json()['detail'] == 'ROOT_ACCESS_REQUIRES_HTTPS'


def test_neutral_tags_and_legacy_routes(client, auth):
    api = client.get('/openapi.json', headers=auth).json()
    assert api['info']['title'] == 'IACP Orchestrator'
    assert all('terraform' not in t['name'].lower() and 'opentofu' not in t['name'].lower() for t in api['tags'])
    assert '/v1/instances/{instance_id}/terraform/approvals' in api['paths']
    assert api['components']['schemas']['RootAccess']['properties']['password']['writeOnly'] is True
    assert PASSWORD not in json.dumps(api)


def test_autostart_is_separate_idempotent_lifecycle_intent(client, auth, lab, sessions):
    r = create(client, auth, lab, start_on_create=True)
    assert r.status_code == 202, r.text
    accepted = r.json(); approval = provision(accepted, sessions, lab)
    publish = Mock()
    for _ in range(2):
        advance(accepted['instance_id'], sessions, publish, lambda: lab.power)
    with sessions() as s:
        starts = s.scalars(select(Operation).where(Operation.instance_id == accepted['instance_id'], Operation.kind == 'instance.power.start')).all()
        assert len(starts) == 1 and starts[0].idempotency_key == 'autostart-' + accepted['instance_id']
    operation = client.get('/v1/operations/' + accepted['operation_id'], headers=auth).json()
    assert operation['normalized_status'] == 'completed'
    assert operation['fulfillment']['status'] == 'queued' and operation['fulfillment']['phase'] == 'start'
    assert publish.call_count == 2  # Safe publication replay; existing worker fence consumes once.


def test_autostart_drain_retains_intent_without_power_dispatch(client, auth, lab, sessions, monkeypatch):
    r = create(client, auth, lab, start_on_create=True).json(); provision(r, sessions, lab)
    monkeypatch.setenv('IACP_ADMISSION_ENABLED', '0')
    publish = Mock(); advance(r['instance_id'], sessions, publish, lambda: lab.power)
    publish.assert_not_called()
    assert client.get('/v1/operations/' + r['operation_id'], headers=auth).json()['events'][-1]['code'] == 'autostart_paused'


@pytest.fixture
def root_lab(client, auth, lab, monkeypatch, tmp_path):
    key = tmp_path / 'root-input.key'; key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
    monkeypatch.setenv('IACP_ROOT_INPUT_KEY_FILE', str(key))
    monkeypatch.setenv('IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED', '1')
    data = json.loads(lab.path.read_text()); p = data['profiles']['lab-dhcp']
    p.update(root_password_bootstrap=True, guest_initialization='ubuntu-2404-lab-identity-v1')
    p['settings'].update(template_vm_id=29000, image='ubuntu-2404', cloudinit_username='root', guest_agent_enabled=True)
    lab.path.write_text(json.dumps(data))
    r = client.post('/v1/lab/policies', headers={**auth, 'Idempotency-Key': 'root-policy'}, json=POLICY_SPEC)
    assert r.status_code == 201, r.text
    lab.id = r.json()['policy_id']
    assert client.post('/v1/lab/policies/' + lab.id + '/approve', headers=lab.reviewer,
        json={'policy_sha256': r.json()['policy_sha256']}).status_code == 200
    return lab


def root_create(client, auth, lab, number=1, password=PASSWORD):
    return client.post('https://testserver/v1/instances', headers={**auth, 'Idempotency-Key': 'root-' + str(number)},
        json={**SPEC, 'name': 'root-' + str(number), 'image': 'ubuntu-2404', 'network_profile': 'lab-dhcp',
            'self_service_policy_id': lab.id, 'start_on_create': True,
            'access': {'username': 'root', 'password': password}, 'commercial_context': CONTEXT})


def test_root_input_encrypted_tenant_bound_and_never_returned(client, auth, root_lab, sessions, caplog):
    r = root_create(client, auth, root_lab); assert r.status_code == 202, r.text
    assert root_create(client, auth, root_lab).json() == r.json()
    identifier = r.json()['instance_id']
    with sessions() as s:
        instance = s.get(Instance, identifier)
        assert PASSWORD not in instance.initial_access_ciphertext
        assert decrypt_password(instance) == PASSWORD
        allocation = Allocation.model_validate(s.get(TerraformExecution, identifier).allocation)
        assert allocation.variables(instance)['iacp_root_password'] == PASSWORD
        instance.tenant_id = 'changed'
        with pytest.raises(PlanError, match='ROOT_INPUT_BINDING_CHANGED'):
            decrypt_password(instance)
    for path in ('/v1/instances/' + identifier, '/v1/operations', '/v1/jobs/' + r.json()['job_id'], '/v1/events'):
        response = client.get(path, headers=auth)
        assert response.status_code == 200 and PASSWORD not in response.text
        assert 'initial_access_ciphertext' not in response.text and 'initial_access_fingerprint' not in response.text
    assert PASSWORD not in caplog.text
    assert root_create(client, auth, root_lab, 2).json()['detail'] == 'INITIAL_PASSWORD_ALREADY_USED'


def test_access_verification_async_and_provider_error_is_sanitized(client, auth, root_lab, sessions, application):
    r = root_create(client, auth, root_lab).json(); provision(r, sessions, root_lab)
    publisher = Mock(); application.dependency_overrides[get_access_verification_publisher] = lambda: publisher
    # The route obtains the publisher at execution, just like network discovery.
    from unittest.mock import patch
    with patch('iacp.api.dependencies.get_access_verification_publisher', return_value=publisher):
        accepted = client.post('/v1/instances/' + r['instance_id'] + '/access/verify', headers={**auth, 'Idempotency-Key': 'verify'}, json={})
    assert accepted.status_code == 202, accepted.text
    verifier = Mock(); verifier.verify.side_effect = RuntimeError(PASSWORD)
    execute_verification(accepted.json()['operation_id'], sessions, verifier)
    result = client.get('/v1/operations/' + accepted.json()['operation_id'], headers=auth)
    assert result.json()['status'] == 'uncertain' and PASSWORD not in result.text
    verifier.verify.side_effect = None
    verifier.verify.return_value = {'status': 'completed', 'cloud_init_complete': True, 'ssh_authenticated': True, 'observed_at': '2026-10-24T00:00:00Z'}
    with sessions() as s:
        instance = s.get(Instance, r['instance_id'])
        request_verification(s, instance, Principal(instance.tenant_id, 'bootstrap', 'operator'), 'verify', publisher)
    execute_verification(accepted.json()['operation_id'], sessions, verifier)
    assert client.get('/v1/instances/' + r['instance_id'], headers=auth).json()['access']['ssh_authenticated'] is True


def test_access_adapter_is_sealed_and_tampering_rejected(tmp_path):
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'stacks/kvm-instance/main.tf').write_text(BASE_MAIN)
    install(tmp_path)
    verify(tmp_path, {'access_adapter_sha256': DIGEST}, {'iacp_root_password': PASSWORD})
    with pytest.raises(PlanError):
        verify(tmp_path, {}, {'iacp_root_password': PASSWORD})
    (tmp_path / next(iter(FILES))).write_text('tampered')
    with pytest.raises(PlanError):
        verify(tmp_path, {'access_adapter_sha256': DIGEST}, {'iacp_root_password': PASSWORD})


def test_audit_event_feed_is_tenant_scoped_and_replayable(client, auth, identities):
    a = plain_create(client, auth).json()
    b = plain_create(client, identities['tenant-a-operator']['headers']).json()
    feed = client.get('/v1/events?limit=1', headers=auth).json()
    assert len(feed['events']) == 1 and feed['events'][0]['operation_id'] == a['operation_id']
    assert feed['events'][0]['schema_version'] == 'iacp.audit.v1'
    assert client.get('/v1/events?after=' + str(feed['next_after']), headers=auth).json()['events'] == []
    assert b['operation_id'] not in json.dumps(feed)


def test_https_termination_requires_the_exact_trusted_socket_peer(monkeypatch):
    from starlette.requests import Request
    from iacp.security import request_uses_https
    def request(peer, scheme='http'):
        return Request({'type':'http', 'method':'POST', 'path':'/', 'scheme':scheme,
            'server':('iac.cyberfuel.com',443), 'client':(peer,1234),
            'headers':[(b'x-forwarded-proto',b'https')]})
    assert not request_uses_https(request('192.0.2.4'))
    monkeypatch.setenv('IACP_TLS_PROXY_IPS','192.0.2.5')
    assert not request_uses_https(request('192.0.2.4'))
    assert request_uses_https(request('192.0.2.5'))
    assert request_uses_https(request('192.0.2.4', 'https'))


def test_autostart_worker_duplicate_delivery_submits_one_command(client, auth, lab, sessions):
    from iacp.lifecycle.execution import execute_power
    r = create(client,auth,lab,start_on_create=True).json(); provision(r,sessions,lab)
    advance(r['instance_id'],sessions,Mock(),lambda:lab.power)
    with sessions() as s:
        identifier = s.scalar(select(Operation.id).where(Operation.instance_id==r['instance_id'],Operation.kind=='instance.power.start'))
    execute_power(identifier,sessions,lab.power)
    execute_power(identifier,sessions,lab.power)
    assert len([c for c in lab.power.calls if c[0]=='submit'])==1


def test_world_readable_input_key_is_rejected(client,auth,root_lab,monkeypatch,tmp_path):
    path=tmp_path/'unsafe-key';path.write_bytes(Fernet.generate_key());path.chmod(0o644)
    monkeypatch.setenv('IACP_ROOT_INPUT_KEY_FILE',str(path))
    r=root_create(client,auth,root_lab)
    assert r.status_code==503 and r.json()['detail']=='ROOT_INPUT_KEY_UNAVAILABLE'
    assert PASSWORD not in r.text


def test_root_initializer_is_distinct_and_contains_no_customer_secret():
    import ast
    from iacp.services.guest_identity import PROGRAM,PROGRAM_SHA256,ROOT_PROGRAM,ROOT_PROGRAM_SHA256
    assert PROGRAM!=ROOT_PROGRAM and PROGRAM_SHA256!=ROOT_PROGRAM_SHA256
    ast.parse(ROOT_PROGRAM)
    assert 'PermitRootLogin yes' in ROOT_PROGRAM and 'PasswordAuthentication yes' in ROOT_PROGRAM
    assert PASSWORD not in ROOT_PROGRAM


def test_root_promotion_requires_exact_reviewed_key_and_proxy():
    from copy import deepcopy
    from tests.test_normalization import fixture_config
    from operations import compare
    old=fixture_config();new=deepcopy(old)
    old['secrets']={};new['secrets']={'root_input_key':{'name':'iacp-controlplane_root_input_key','file':'/private/root.key'}}
    for role in ('api','terraform-worker'):
        old['services'][role].update(environment={},secrets=[])
        new['services'][role].update(environment={'IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED':'1','IACP_ROOT_INPUT_KEY_FILE':'/run/secrets/root_input_key'},
            secrets=[{'source':'root_input_key','target':'/run/secrets/root_input_key'}])
    new['services']['api']['environment']['IACP_TLS_PROXY_IPS']='192.0.2.5'
    assert not compare(old,new)['passed']
    review={'key_path':'/private/root.key','enabled':True,'proxy_ips':'192.0.2.5'}
    assert compare(old,new,root_bootstrap=review)['passed']
    new['services']['api']['environment']['IACP_TLS_PROXY_IPS']='192.0.2.6'
    assert not compare(old,new,root_bootstrap=review)['passed']
