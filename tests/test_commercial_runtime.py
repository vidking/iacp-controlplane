"""Cableado G6 acotado; nunca autoriza deriva general del despliegue."""
from copy import deepcopy
import pytest

from tests.test_normalization import fixture_config
from operations import compare
from tests.test_go_live import driver


def configuration():
    old = fixture_config()
    old['secrets'] = {'kvm_credentials': {'file': '/private/kvm.json',
        'name': 'iacp-controlplane_kvm_credentials'}}
    for role in ('api', 'terraform-worker'):
        old['services'][role].update(environment={}, secrets=[])
    new = deepcopy(old)
    review = dict(contract='commercial-g', aes_key_path='/private/bootstrap.key',
        ssh_key_path='/private/ssh.key', ttl_seconds=900, admission_credentials_path='/private/admission.json')
    for name, path in [('bootstrap_aes_key', review['aes_key_path']),
                       ('guest_bootstrap_ssh_key', review['ssh_key_path']),
                       ('admission_credentials', review['admission_credentials_path'])]:
        new['secrets'][name] = {'file': path, 'name': 'iacp-controlplane_'+name}
    for role in ('api', 'terraform-worker'):
        s = new['services'][role]
        s['environment'].update(IACP_BOOTSTRAP_AES_KEY_FILE='/run/secrets/bootstrap_aes_key',
            IACP_BOOTSTRAP_SECRET_TTL_SECONDS='900')
        names = ['bootstrap_aes_key']
        if role == 'api':
            s['environment']['IACP_CREATION_CONTRACT'] = 'commercial-g'
            s['environment']['IACP_ADMISSION_CREDENTIALS_FILE'] = '/run/secrets/admission_credentials'
            names += ['admission_credentials']
        else:
            s['environment'].update(IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE='/run/secrets/guest_bootstrap_ssh_key')
            names += ['guest_bootstrap_ssh_key']
        s['secrets'] += [{'source': n, 'target': '/run/secrets/'+n} for n in names]
    return old, new, review


def test_review_is_required_and_does_not_authorize_other_changes():
    old, new, review = configuration()
    assert not compare(old, new)['passed']
    assert compare(old, new, commercial_bootstrap=review)['passed']
    for role, key, value in [('redis', 'command', ['redis-server', '--appendonly', 'no']),
                             ('terraform-worker', 'privileged', True),
                             ('api', 'ports', ['8000:8000'])]:
        changed = deepcopy(new); changed['services'][role][key] = value
        assert not compare(old, changed, commercial_bootstrap=review)['passed']
    changed = deepcopy(new); changed['secrets']['kvm_credentials']['file'] = '/other-identity'
    assert not compare(old, changed, commercial_bootstrap=review)['passed']


def test_wrong_secret_mount_or_ttl_is_not_covered_by_review():
    old, new, review = configuration()
    bad = deepcopy(new)
    bad['services']['terraform-worker']['secrets'][-1]['target'] = '/wrong'
    assert not compare(old, bad, commercial_bootstrap=review)['passed']
    bad = deepcopy(new); bad['services']['api']['environment']['IACP_BOOTSTRAP_SECRET_TTL_SECONDS'] = '3601'
    assert not compare(old, bad, commercial_bootstrap=review)['passed']
    for change in ({'ttl_seconds': 3601}, {'aes_key_path': '/dev/null'},
                   {'ssh_key_path': '../identity'}, {'unknown': 'not-approved'}):
        with pytest.raises(ValueError):
            compare(old, new, commercial_bootstrap={**review, **change})


def test_active_aes_key_cannot_be_rotated_as_part_of_promotion():
    old, new, review = configuration()
    old['secrets']['bootstrap_aes_key'] = {'file': '/previous-key', 'name': 'iacp-controlplane_bootstrap_aes_key'}
    with pytest.raises(ValueError, match='Rotación'):
        compare(old, new, commercial_bootstrap=review)


def test_missing_g6_material_blocks_before_any_runtime_command(tmp_path):
    p = object.__new__(driver.Promotion)
    p.m = {'commercial_bootstrap_configuration': dict(contract='commercial-g',
        aes_key_path=str(tmp_path/'missing-aes'), ssh_key_path=str(tmp_path/'missing-ssh'), ttl_seconds=900,
        admission_credentials_path=str(tmp_path/'missing-admission'))}
    commands = []; p.run = lambda *a, **k: commands.append(a)
    _, cfg, _ = configuration()
    with pytest.raises(RuntimeError, match='secret missing'):
        p.review_commercial_configuration(cfg)
    assert not commands
