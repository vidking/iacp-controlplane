"""El perfil revisado no amplía montajes ni credenciales de Lifecycle."""
from copy import deepcopy
import json
from unittest.mock import Mock
import pytest
from tests.test_go_live import driver
from tests.test_commercial_admission import commercial_config, catalog_profile


@pytest.fixture
def reviewed(tmp_path):
    path=tmp_path/'profile.json';path.write_text('{}')
    p=object.__new__(driver.Promotion)
    p.m={'profile_configuration':{'path':str(path),'sha256':driver.digest(path)}}
    service={'read_only':True,'environment':{'IACP_TERRAFORM_PROFILES_FILE':'/run/config/profiles.json'},
        'secrets':[{'source':'db_password'},{'source':'kvm_credentials'}],
        'volumes':[{'type':'bind','source':str(path),'target':'/run/config/profiles.json','read_only':True}]}
    return p,service


def test_reviewed_profile_is_allowed_and_legacy_without_profile_still_works(reviewed):
    p,s=reviewed
    assert p.lifecycle_mount_targets(s)=={'/run/config/profiles.json','/run/secrets/db_password','/run/secrets/kvm_credentials'}
    s['volumes']=[];s['environment']={}
    assert len(p.lifecycle_mount_targets(s))==2


@pytest.mark.parametrize('change',['write','extra','secret','source','digest','missing','rootfs','duplicate'])
def test_profile_exception_never_expands_isolation(reviewed,change):
    p,s=reviewed
    if change=='write':s['volumes'][0]['read_only']=False
    if change=='extra':s['volumes'].append({'type':'bind','source':'/etc','target':'/host','read_only':True})
    if change=='secret':s['secrets'].append({'source':'terraform_credentials'})
    if change=='source':s['volumes'][0]['source']='/unapproved'
    if change=='digest':p.m['profile_configuration']['sha256']='bad'
    if change=='missing':s['volumes']=[]
    if change=='rootfs':s['read_only']=False
    if change=='duplicate':s['volumes'].append(deepcopy(s['volumes'][0]))
    with pytest.raises(RuntimeError):p.lifecycle_mount_targets(s)
