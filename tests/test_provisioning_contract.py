"""HTTP contract, RBAC and presentation regressions without external infrastructure."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from iacp.api.dependencies import get_instance_publisher
from iacp.main import create_app
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Job, Operation, OperationEvent, Tenant
from iacp.services.provisioning import ProvisioningError
from tests.test_instances import SPEC, create
from tests.test_apply import prepared, approval, dispatch, ApplyingExecutor
from tests.test_terraform import terraform_mode
from iacp.terraform.apply import execute_apply

ROOT = Path(__file__).resolve().parents[1]
ENGINE = re.compile(r'terraform|opentofu|\btofu\b', re.I)
IID = '00000000-0000-4000-8000-000000000099'


def administrative_operations():
    public = create_app().openapi()['paths']
    admin = create_app().administrative_openapi()['paths']
    return [(method, path) for path, item in admin.items() for method in item
            if method not in public.get(path, {})]


ADMIN = administrative_operations()
LEGACY = [(method, path) for path, item in json.loads(
    (ROOT / 'docs/contracts/openapi-before-provisioning.json').read_text())['paths'].items()
    if '/terraform' in path for method in item]


@pytest.fixture(autouse=True)
def bootstrap_secret_configuration(tmp_path, monkeypatch):
    """El contrato de creación requiere una clave efímera aislada por test."""
    key = tmp_path / "bootstrap-aes.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("IACP_BOOTSTRAP_AES_KEY_FILE", str(key))


@pytest.fixture
def credentials(tmp_path, monkeypatch, sessions):
    records, headers = [], {}
    other = str(uuid4())
    with sessions() as session:
        session.add(Tenant(id=other, slug='contract-other', name='Contract other tenant'))
        session.commit()
    for name, role, admin, tenant in [
        ('crm-operator', 'operator', False, BOOTSTRAP_TENANT_ID),
        ('crm-reader', 'reader', False, BOOTSTRAP_TENANT_ID),
        ('admin-operator', 'operator', True, BOOTSTRAP_TENANT_ID),
        ('admin-reader', 'reader', True, BOOTSTRAP_TENANT_ID),
        ('reviewer', 'approver', False, BOOTSTRAP_TENANT_ID),
        ('docs', 'docs', False, BOOTSTRAP_TENANT_ID),
        ('other-admin', 'operator', True, other),
    ]:
        token = secrets.token_hex(32)
        records.append(dict(token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                            subject=name, role=role, tenant_id=tenant, administration=admin))
        headers[name] = {'Authorization': 'Bearer ' + token}
    path = tmp_path / 'contract-credentials.json'
    path.write_text(json.dumps(records))
    monkeypatch.setenv('IACP_TENANT_CREDENTIALS_FILE', str(path))
    return headers


def concrete(path):
    return re.sub(r'\{[^}]+\}', IID, path)


def test_public_and_administrative_openapi_are_neutral(client, auth):
    public = client.get('/openapi.json', headers=auth)
    admin = client.get('/admin/openapi.json', headers=auth)
    assert public.status_code == admin.status_code == 200
    assert not ENGINE.search(public.text)
    assert not ENGINE.search(admin.text)
    assert admin.json() == json.loads((ROOT / 'docs/openapi-admin.json').read_text())
    assert 'state_key' not in public.text and 'EmptyStateEvidence' not in public.text
    assert 'provider' not in public.text.lower()
    assert public.json()['info']['title'] == 'IACP Orchestrator'
    # La recuperación Core es una ruta administrativa única; no reintroduce
    # el motor LAB ni sus endpoints de políticas.
    assert len(ADMIN) == 14
    assert not any('/terraform' in route.path for route in client.app.routes)
    assert client.get('/redoc').status_code == 404
    for method, path in ADMIN:
        assert method not in public.json()['paths'].get(path, {})
        assert method in admin.json()['paths'][path]


@pytest.mark.parametrize('method,path', ADMIN)
@pytest.mark.parametrize('identity', ['crm-operator', 'crm-reader'])
def test_crm_cannot_call_any_administrative_operation(client, auth, credentials, method, path, identity):
    headers = {**credentials[identity], 'Idempotency-Key': 'attempt', 'X-IACP-Administration': 'true'}
    response = client.request(method, concrete(path), headers=headers, json={} if method == 'post' else None)
    assert response.status_code == 403, response.text
    assert not ENGINE.search(response.text)


@pytest.mark.parametrize('method,path', ADMIN)
def test_anonymous_cannot_call_administration(client, auth, method, path):
    assert client.request(method, concrete(path), json={} if method == 'post' else None).status_code == 401


@pytest.mark.parametrize('method,path', LEGACY)
@pytest.mark.parametrize('drained', [False, True])
def test_legacy_routes_are_absent_for_every_identity_even_during_drain(client, auth, credentials, monkeypatch, method, path, drained):
    monkeypatch.setenv('IACP_ADMISSION_ENABLED', '0' if drained else '1')
    for headers in ({}, auth, credentials['crm-operator'], credentials['reviewer']):
        response = client.request(method, concrete(path), headers=headers, json={} if method == 'post' else None)
        assert response.status_code == 404
        assert not ENGINE.search(response.text)


def test_documentation_requires_real_administrative_scope(client, auth, credentials):
    for path in ['/docs', '/openapi.json', '/admin/docs', '/admin/openapi.json']:
        assert client.get(path).status_code == 401
    for identity in ['crm-operator', 'crm-reader']:
        assert client.get('/docs', headers=credentials[identity]).status_code == 200
        for path in ['/admin/docs', '/admin/openapi.json']:
            assert client.get(path, headers=credentials[identity]).status_code == 403
    docs_token = credentials['docs']['Authorization'].removeprefix('Bearer ')
    assert client.get('/docs', auth=('docs', docs_token)).status_code == 200
    assert client.get('/admin/docs', auth=('docs', docs_token)).status_code == 401
    token = auth['Authorization'].removeprefix('Bearer ')
    assert client.get('/admin/docs', auth=('iacp-admin', token)).status_code == 200
    assert client.get('/admin/openapi.json', auth=('iacp-admin', token)).status_code == 200


def test_scoped_readers_and_operators_keep_existing_role_and_tenant_checks(client, auth, credentials, terraform_mode):
    accepted = create(client, auth).json()
    plan = '/v1/instances/' + accepted['instance_id'] + '/provisioning/plan'
    assert client.get(plan, headers=credentials['admin-reader']).status_code == 200
    assert client.post(plan + '/cancel', headers=credentials['admin-reader']).status_code == 403
    assert client.post(plan + '/cancel', headers=credentials['reviewer']).status_code == 403
    assert client.get(plan, headers=credentials['other-admin']).status_code == 404
    assert client.post(plan + '/cancel', headers=credentials['other-admin']).status_code == 404
    assert client.post(plan + '/cancel', headers=credentials['admin-operator']).status_code == 200
    # Unknown instance still has no administrative information to disclose.
    assert client.get('/v1/instances/' + str(uuid4()) + '/provisioning/plan',
                      headers=credentials['admin-reader']).status_code == 404


def test_crm_creation_replay_and_operation_contract_remain_available(client, auth, credentials, application, terraform_mode, sessions):
    calls = []
    application.dependency_overrides[get_instance_publisher] = lambda: calls.append
    headers = credentials['crm-operator']
    accepted = create(client, headers)
    assert accepted.status_code == 202
    body = accepted.json()
    assert body['executor'] == body['provisioning_service'] == 'provisioning'
    assert create(client, headers).json() == body
    assert create(client, headers, spec={**SPEC, 'vcpus': 4}).status_code == 409
    assert calls == [body['job_id']]
    for path in ['/v1/instances', '/v1/instances/' + body['instance_id'],
                 '/v1/operations', '/v1/operations/' + body['operation_id'], '/v1/jobs/' + body['job_id']]:
        response = client.get(path, headers=credentials['crm-reader'])
        assert response.status_code == 200 and not ENGINE.search(response.text)
    with sessions() as session:
        assert session.get(Instance, body['instance_id']).executor == 'terraform'


def test_commercial_projection_preserves_internal_records(client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED', '1')
    row = approval(client, prepared).json()
    dispatch(client, prepared, auth, row)
    execute_apply(row['approval_id'], sessions, ApplyingExecutor())
    identifier = prepared[0]['instance_id']
    with sessions() as session:
        instance = session.get(Instance, identifier)
        internal = copy.deepcopy(instance.result)
        internal['observation'] = {'state_present': True, 'proxmox_present': True,
            'postconditions_verified': True, 'backend': {'key': 'private/terraform.tfstate'},
            'resource_address': 'module.kvm.provider_resource.this', 'workspace': 'private'}
        instance.result = internal
        operation = session.get(Operation, prepared[0]['operation_id'])
        operation.error_code = 'TERRAFORM_EXECUTION_FAILED'
        job = session.scalar(select(Job).where(Job.operation_id == operation.id))
        job.message = 'Terraform provider failure with private backend information'
        session.commit()
    for path in ['/v1/instances/' + identifier, '/v1/instances', '/v1/operations',
                 '/v1/operations/' + operation.id, '/v1/jobs/' + job.id, '/v1/events']:
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        # El nombre de la instancia es dato controlado por el consumidor. La
        # prueba debe detectar filtraciones del backend, no reescribir nombres.
        sanitized = response.text.replace('test-kvm-terraform', 'test-kvm-provisioning')
        assert not ENGINE.search(sanitized)
        assert 'private/terraform.tfstate' not in response.text
        assert 'resource_address' not in response.text and 'backend' not in response.text
    with sessions() as session:
        assert session.get(Instance, identifier).result == internal
        assert session.get(Operation, operation.id).error_code == 'TERRAFORM_EXECUTION_FAILED'
        assert 'Terraform' in session.get(Job, job.id).message
        assert session.scalar(select(OperationEvent).where(OperationEvent.actor == 'worker:terraform'))


@pytest.mark.parametrize('failure,code', [
    (ProvisioningError('TERRAFORM_UNAVAILABLE'), 503),
    (HTTPException(409, 'PROFILE_REQUIRES_TERRAFORM'), 409),
    (RuntimeError('terraform provider private backend'), 500),
])
def test_exception_responses_do_not_reveal_engine(client, auth, application, failure, code):
    def fail():
        raise failure
    application.dependency_overrides[get_instance_publisher] = fail
    response = create(client, auth)
    assert response.status_code == code
    assert not ENGINE.search(response.text)
    assert 'private' not in response.text


def test_phase_g_core_changes_are_explicit_and_other_invariants_remain_identical():
    checksums = json.loads((ROOT / 'docs/contracts/provisioning-invariants.json').read_text())
    reviewed=json.loads((ROOT/'docs/contracts/phase-g-core-reviewed.json').read_text())
    assert set(reviewed)=={'app/iacp/models.py','app/iacp/terraform/apply.py','app/iacp/terraform/config.py',
        'app/iacp/terraform/deletion.py','app/iacp/terraform/lifecycle.py','app/iacp/terraform/worker.py',
        'deploy/mng/compose.yaml','app/iacp/terraform/profiles.py',
        'app/iacp/terraform/executor.py','app/iacp/terraform/execution_health.py'}
    # Se conserva el sello histórico y se añade una revisión fechada del hardware.
    golden=json.loads((ROOT/'docs/contracts/golden-hardware-core-reviewed.json').read_text())
    assert set(golden)=={'app/iacp/terraform/access_adapter.py','app/iacp/terraform/config.py',
        'app/iacp/terraform/deletion_executor.py','app/iacp/terraform/executor.py',
        'app/iacp/terraform/lifecycle.py','app/iacp/terraform/observation.py',
        'app/iacp/terraform/golden_adapter.py','app/iacp/terraform/golden_hardware.py',
        'app/iacp/services/secure_bootstrap.py'}
    core=json.loads((ROOT/'docs/contracts/provisioning-api-core-reviewed.json').read_text())
    assert set(core)=={'app/iacp/models.py','app/iacp/terraform/access_adapter.py','app/iacp/terraform/apply.py',
        'app/iacp/terraform/config.py','app/iacp/terraform/deletion.py','app/iacp/terraform/deletion_executor.py',
        'app/iacp/terraform/executor.py','app/iacp/terraform/lifecycle.py','app/iacp/terraform/observation.py',
        'app/iacp/terraform/profiles.py','app/iacp/terraform/worker.py','deploy/mng/compose.yaml'}
    for path, digest in (checksums | reviewed | golden | core).items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_business_http_and_models_preserve_the_baseline_except_agreed_service_names():
    from scripts.openapi_contract import transport_projection
    before = json.loads((ROOT / 'docs/contracts/openapi-before-provisioning.json').read_text())
    after = create_app().openapi()
    before_http, after_http = map(transport_projection, [before, after])
    additions = {'/v1/plans', '/v1/images'}
    assert set(after['paths']) - set(before['paths']) == additions
    for path, methods in after['paths'].items():
        if path in additions:
            assert set(methods) == {'get'}
            assert methods['get']['security'] == [{'HTTPBearer': []}]
            continue
        for method, operation in methods.items():
            original = before_http['paths'][path][method]
            operation = after_http['paths'][path][method]
            # La clave pasó a ser opcional en las cinco mutaciones públicas:
            # el servidor genera una intención durable cuando se omite. El
            # resto del contrato HTTP permanece idéntico al histórico.
            if path == '/v1/instances' and method == 'post':
                # El endpoint Core usa su esquema comercial-técnico explícito;
                # la forma pública sustituye la antigua InstanceCreate.
                original['requestBody'] = operation['requestBody']
            if (path == '/v1/instances' and method == 'post') or (
                    path.endswith('/power/start') or path.endswith('/power/shutdown') or
                    path.endswith('/power/reboot') or
                    (path.startswith('/v1/instances/') and method == 'delete')):
                for candidate in (original, operation):
                    for parameter in candidate.get('parameters', []):
                        if parameter.get('name') == 'Idempotency-Key':
                            parameter['required'] = False
                operation.get('responses', {}).get('202', {}).pop('headers', None)
            for field in ['operationId', 'parameters', 'requestBody', 'responses', 'security']:
                assert operation.get(field) == original.get(field), (method, path, field)
    old_models, new_models = (s['components']['schemas'] for s in [before, after])
    assert set(new_models) - set(old_models) == {
        'Availability', 'PlanResponse', 'ImageResponse', 'MinimumRequirements',
        'OrderContext', 'CommercialAccess', 'CommercialInstanceCreate',
        'DHCPNetwork', 'StaticNetwork'}
    for name, model in new_models.items():
        if name not in old_models:
            continue
        original, current = copy.deepcopy(old_models[name]), copy.deepcopy(model)
        if name in {'InstanceAccepted', 'InstanceResponse'}:
            for field in ['executor', 'provisioning_service']:
                original['properties'].pop(field)
                current['properties'].pop(field)
        if name=='InstanceResponse':
            # El vínculo a LabPolicy ya no es parte del modelo Core.
            original['properties'].pop('self_service_policy_id')
            variants=current['properties']['commercial_context']['anyOf']
            assert {'$ref':'#/components/schemas/OrderContext'} in variants
            variants.remove({'$ref':'#/components/schemas/OrderContext'})
            assert current['properties']['commercial_context'].pop('title')=='Commercial Context'
        if name == 'PowerRequest':
            # Documentation wording is neutral; the empty, strict body is exact.
            original['description'] = current['description']
        assert current == original, name
