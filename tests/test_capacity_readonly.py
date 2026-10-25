"""La admisión no hereda identidad Terraform ni permite escribir a Proxmox."""
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
import yaml

from iacp.services import capacity_readonly as module
from iacp.services.capacity_readonly import CapacityError, CapacityReadOnly, IDENTITY


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path/'admission.json'
    value = {'endpoint': 'https://mng01.invalid:8006',
        'api_token': IDENTITY+'=identidad-sintetica-solo-pruebas'}
    path.write_text(json.dumps(value)); path.chmod(0o400)
    monkeypatch.setenv('IACP_ADMISSION_CREDENTIALS_FILE', str(path))
    return path, value


def test_exclusive_identity_and_no_fallback_to_privileged_worker(credentials, monkeypatch):
    path, value = credentials
    client = CapacityReadOnly.configured()
    assert client.endpoint == value['endpoint']
    assert value['api_token'] not in repr(client)
    monkeypatch.delenv('IACP_ADMISSION_CREDENTIALS_FILE')
    monkeypatch.setenv('IACP_KVM_CREDENTIALS_FILE', str(path))
    monkeypatch.setenv('IACP_TERRAFORM_CREDENTIALS_FILE', str(path))
    with pytest.raises(CapacityError, match='CREDENTIALS_UNAVAILABLE'):
        CapacityReadOnly.configured()


@pytest.mark.parametrize('change', [
    {'api_token': 'worker@pve!privilegiado=identidad-sintetica'},
    {'api_token': IDENTITY+'='}, {'api_token': IDENTITY+'=sintetico\n'},
    {'endpoint': 'http://mng01.invalid'}, {'endpoint': 'https://user:pass@mng01.invalid'},
    {'endpoint': 'https://mng01.invalid/otro'}, {'extra': True}])
def test_invalid_credentials_are_rejected_without_values(credentials, change):
    path, data = credentials
    path.chmod(0o600)
    path.write_text(json.dumps({**data, **change}))
    with pytest.raises(CapacityError) as error:
        CapacityReadOnly.configured()
    assert str(error.value) == 'ADMISSION_CAPACITY_CREDENTIALS_UNAVAILABLE'


def test_world_readable_secret_is_rejected(credentials):
    path, _ = credentials; path.chmod(0o644)
    with pytest.raises(CapacityError): CapacityReadOnly.configured()


@pytest.mark.parametrize('method,path,parameters', [
    ('POST', '/nodes/mng01/status', None), ('PUT', '/nodes/mng01/config', {}),
    ('DELETE', '/nodes/mng01/storage/DS_CYBERFUEL/status', None),
    ('GET', '/nodes/mng02/status', None), ('GET', '/cluster/resources', None),
    ('GET', '/nodes/mng01/storage/otro/status', None),
    ('GET', '/nodes/mng01/status', {'command': 'reboot'})])
def test_forbidden_writes_and_scope_never_reach_transport(method, path, parameters):
    opener = Mock(); client = CapacityReadOnly('https://mng01.invalid', 'sintetico', opener)
    with pytest.raises(CapacityError, match='REQUEST_FORBIDDEN'):
        client._request(method, path, parameters)
    opener.open.assert_not_called()


def test_reads_are_authenticated_and_transport_errors_are_sanitized(credentials, monkeypatch):
    response = Mock(); response.status = 200; response.read.return_value = b'{"data":{"active":1}}'
    opener = Mock(); opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, 'build_opener', lambda *args: opener)
    client = CapacityReadOnly.configured()
    assert client._request('GET', '/nodes/mng01/storage/DS_CYBERFUEL/status') == {'active': 1}
    request = opener.open.call_args.args[0]
    assert request.method == 'GET' and request.data is None
    assert request.get_header('Authorization') == 'PVEAPIToken='+credentials[1]['api_token']
    opener.open.side_effect = HTTPError('https://mng01.invalid', 403,
        credentials[1]['api_token'], None, None)
    with pytest.raises(CapacityError) as error:
        client._request('GET', '/nodes/mng01/status')
    assert str(error.value) == 'ADMISSION_CAPACITY_PERMISSION_DENIED'
    assert credentials[1]['api_token'] not in str(error.value)


def test_only_api_mounts_capacity_identity():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root/'deploy/mng/compose.yaml').read_text())
    mounted = [name for name, service in config['services'].items()
        if any(s['source'] == 'admission_credentials' for s in service.get('secrets', []))]
    assert mounted == ['api']
    assert config['services']['api']['environment']['IACP_ADMISSION_CREDENTIALS_FILE'] == '/run/secrets/admission_credentials'
    for name, service in config['services'].items():
        if name != 'api': assert 'IACP_ADMISSION_CREDENTIALS_FILE' not in service.get('environment', {})
