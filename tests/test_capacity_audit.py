"""La auditoría no convierte respuestas ambiguas en certificación RO."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
from iacp.services.capacity_readonly import CapacityError

spec = importlib.util.spec_from_file_location('capacity_audit',
    Path(__file__).resolve().parents[1]/'scripts/audit-admission-capacity.py')
audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)


def client(privileges=None, code=403):
    permissions = privileges if privileges is not None else {
        '/nodes/mng01': {'Sys.Audit': 1}, '/storage/DS_CYBERFUEL': {'Datastore.Audit': 1}}
    opener = Mock()
    opener.open.side_effect = HTTPError('https://mng01.invalid', code, 'fixture', None, None)
    return SimpleNamespace(endpoint='https://mng01.invalid', _token='fixture-synthetic-token',
        _request=Mock(side_effect=lambda method, path: permissions if path == '/access/permissions' else {}),
        _opener=opener)


def test_empty_write_probe_never_targets_vms_or_supplies_configuration():
    c = client(); result = audit.audit(c, verify_write_denied=True)
    assert len(result['lecturas']) == 3 and result['escritura']['http'] == 403
    r = c._opener.open.call_args.args[0]
    assert r.full_url == 'https://mng01.invalid/api2/json/nodes/mng01/config'
    assert r.method == 'PUT' and r.data == b''


@pytest.mark.parametrize('code', [401, 404, 500])
def test_only_real_403_counts_as_write_denial(code):
    with pytest.raises(CapacityError, match='WRITE_PROBE_UNCONFIRMED'):
        audit.audit(client(code=code), verify_write_denied=True)


@pytest.mark.parametrize('privileges', [
    {'/': {'Sys.Modify': 1}}, {'/vms': {'VM.PowerMgmt': 0}}, {'/': {'Sys.Audit': 'unknown'}}])
def test_write_privileges_or_unknown_flags_abort_before_probe(privileges):
    c = client(privileges)
    with pytest.raises(CapacityError, match='NON_READONLY_PRIVILEGES'):
        audit.audit(c, verify_write_denied=True)
    c._opener.open.assert_not_called()


def test_missing_credentials_is_blocked_not_pass(monkeypatch, capsys):
    monkeypatch.delenv('IACP_ADMISSION_CREDENTIALS_FILE', raising=False)
    monkeypatch.setattr('sys.argv', ['audit-admission-capacity.py'])
    assert audit.main() == 2
    text = capsys.readouterr().out
    assert 'BLOCKED' in text and 'CREDENTIALS_UNAVAILABLE' in text
