"""Operator workflows use synthetic identities and private temporary directories."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from iacp.models import BOOTSTRAP_TENANT_ID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from operations import compare, live_compatibility, EXEC_TMPFS


def basic(username, password):
    return {'Authorization': 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()}


def test_docs_only_rotation_preserves_api_and_never_prints_credentials(tmp_path, client, auth, monkeypatch):
    source = tmp_path / 'registry.json'
    api_token = 'a' * 64
    original = [{'token_sha256': hashlib.sha256(api_token.encode()).hexdigest(), 'tenant_id': BOOTSTRAP_TENANT_ID,
                 'subject': 'fixture-api', 'role': 'reader'}]
    source.write_text(json.dumps(original))
    handoff = tmp_path / 'operator'
    command = [sys.executable, str(ROOT / 'scripts/iacpctl'), 'docs-rotate', '--registry', str(source),
               '--tenant', BOOTSTRAP_TENANT_ID, '--username', 'fixture-docs', '--handoff-dir', str(handoff)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    packet = json.loads((handoff / 'operator-handoff.json').read_text())
    candidate = handoff / 'registry.next.json'
    assert packet['password'] not in result.stdout + result.stderr
    assert 'token_sha256' not in result.stdout + result.stderr
    assert json.loads(source.read_text()) == original
    assert json.loads(candidate.read_text())[0] == original[0]
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert (handoff / 'operator-handoff.json').stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('IACP_TENANT_CREDENTIALS_FILE', str(candidate))
    for path in ('/docs', '/openapi.json'):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=basic('fixture-docs', packet['password'])).status_code == 200
        assert client.get(path, headers=basic('fixture-docs', 'wrong')).status_code == 401
        assert client.get(path, headers=basic('iacp', auth['Authorization'].removeprefix('Bearer '))).status_code == 401
    assert client.get('/v1/instances', headers={'Authorization': 'Bearer ' + packet['password']}).status_code == 401
    assert client.get('/v1/instances', headers={'Authorization': 'Bearer ' + api_token}).status_code == 200
    assert client.get('/docs', headers=auth).status_code == 200  # Tenant Bearer automation unchanged.


def fixture_config():
    return {'services': {role: {} for role in ('api', 'worker', 'terraform-worker', 'lifecycle-worker', 'traefik', 'db', 'redis', 'migrate', 'ownership-certifier')}}


def test_comparison_allows_only_certifier_fix_and_declared_image_changes():
    old = fixture_config()
    old['services']['ownership-certifier'] = {'environment': {}, 'tmpfs': ['/tmp']}
    old['services']['api'] = {'image': 'fixture:before', 'build': {'context': '/fixture/source'}}
    new = deepcopy(old)
    cert = new['services']['ownership-certifier']
    cert['environment']['TMPDIR'] = '/run/iacp-certification'
    cert['tmpfs'].append(EXEC_TMPFS)
    new['services']['api'].pop('build')
    assert compare(old, new)['passed']
    new['services']['api']['image'] = 'fixture:after'
    assert not compare(old, new)['passed']
    assert compare(old, new, ['/services/api/image'])['passed']
    with pytest.raises(ValueError): compare(old, new, ['/services/db/volumes'])


@pytest.mark.parametrize('key', ['ports', 'volumes', 'networks', 'secrets', 'read_only', 'healthcheck', 'restart'])
def test_comparison_rejects_operational_drift(key):
    old = fixture_config(); new = deepcopy(old)
    new['services']['api'][key] = 'unexpected-sensitive-value'
    result = compare(old, new)
    assert not result['passed']
    assert 'unexpected-sensitive-value' not in json.dumps(result)


def test_canonical_preserves_roles_private_ports_and_certifier_isolation():
    c = yaml.safe_load((ROOT / 'deploy/mng/compose.yaml').read_text())
    assert len(c['services']) == 9
    assert not any(s.get('build') for s in c['services'].values())
    assert {p['target'] for p in c['services']['traefik']['ports']} == {80, 443}
    assert all(not s.get('ports') for role, s in c['services'].items() if role != 'traefik')
    cert = c['services']['ownership-certifier']
    assert cert['environment']['TMPDIR'] == '/run/iacp-certification'
    assert EXEC_TMPFS in cert['tmpfs'] and '/tmp' in cert['tmpfs']
    assert cert['read_only'] is True
    assert next(v for v in cert['volumes'] if v['target'] == '/var/lib/iacp/terraform')['read_only'] is True
    assert cert['environment']['IACP_KVM_LIFECYCLE_ENABLED'] == '0'
    life = c['services']['lifecycle-worker']
    assert not life.get('volumes')
    assert {s['source'] for s in life['secrets']} == {'db_password', 'kvm_credentials'}
    assert all(v['external'] is True for v in c['volumes'].values())


def test_iacpctl_runs_independent_of_cwd_and_does_not_mutate_on_help(tmp_path):
    r = subprocess.run([str(ROOT / 'scripts/iacpctl'), '--help'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert 'config-check' in r.stdout and 'doctor' in r.stdout
    source = (ROOT / 'scripts/iacpctl').read_text()
    assert '--remove-orphans' not in source
    assert source.count("'--force-recreate', 'api'") == 1  # Only explicit, approved credential remount.
    assert "'down'" not in source


def test_environment_cannot_reinterpret_live_flags_and_ports():
    cfg = {'services': {'api': {'environment': {'IACP_ADMISSION_ENABLED': '0'}, 'networks': {'backend': None}}},
           'networks': {'backend': {'name': 'fixture-backend'}}}
    live = [{'Config': {'Labels': {'com.docker.compose.service': 'api'}, 'Env': ['IACP_ADMISSION_ENABLED=0']},
             'HostConfig': {'PortBindings': {}}, 'NetworkSettings': {'Networks': {'fixture-backend': {}}}}]
    assert live_compatibility(cfg, live)['passed']
    cfg['services']['api']['environment']['IACP_ADMISSION_ENABLED'] = '1'
    assert not live_compatibility(cfg, live)['passed']
    live[0]['HostConfig']['PortBindings'] = {'8000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8000'}]}
    assert '/live/api/ports' in live_compatibility(cfg, live)['unexpected_paths']
