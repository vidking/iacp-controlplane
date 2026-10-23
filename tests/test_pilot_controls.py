"""Promotion commands fail before mutation when real lab evidence is missing."""
import ast
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from tests.test_go_live import driver as driver_module


@pytest.fixture
def driver():
    return driver_module
from iacp.terraform.config import Allocation
from tests.test_terraform import allocation_data


@pytest.fixture
def promotion(driver,tmp_path):
    op=object.__new__(driver.Promotion)
    env=tmp_path/'server.env';env.write_text('IACP_TERRAFORM_APPLY_ENABLED=1\nIACP_ADMISSION_ENABLED=0\nIACP_KVM_LIFECYCLE_ENABLED=0\n')
    op.m={'environment_file':str(env),'release':'a'*40}
    op.evidence=Mock();op.approve=Mock(return_value={'reference':'lab-test'})
    op.preflight=Mock();op.verify=Mock();op.up=Mock();op.exec_python=Mock(return_value='PASS')
    allocations=tmp_path/'allocations.json';allocations.write_text('{}')
    op.compose=Mock(return_value=json.dumps({'services':{'terraform-worker':{'volumes':[{'source':str(allocations),'target':'/run/config/allocations.json'}]}}}))
    return op,env,allocations


def review(driver,promotion,key,**fields):
    op,env,allocation=promotion;p=env.with_name(key+'.json')
    data={'approved':True,'release':'a'*40,'change_reference':'lab-test',**fields}
    p.write_text(json.dumps(data));op.m[key]={'path':str(p),'sha256':driver.digest(p)}
    return data


@pytest.mark.parametrize('action',['admit','enable_delete'])
def test_new_actions_require_their_own_authorization(promotion,action):
    op,env,_=promotion;before=env.read_bytes();op.approve.side_effect=RuntimeError('not approved')
    with pytest.raises(RuntimeError):getattr(op,action)()
    assert env.read_bytes()==before;op.up.assert_not_called()


def test_admission_never_enables_lifecycle_and_requires_real_ipam(driver,promotion):
    op,env,allocation=promotion
    review(driver,promotion,'admission_review',allocation_file={'path':str(allocation),'sha256':driver.digest(allocation)},targets=[str(uuid4())+'/new-disposable'])
    op.admit();op.approve.assert_called_once_with('enable-admission')
    assert 'IACP_ADMISSION_ENABLED=1' in env.read_text() and 'IACP_KVM_LIFECYCLE_ENABLED=0' in env.read_text()
    assert 'IACP_TERRAFORM_APPLY_ENABLED=1' in env.read_text()
    ast.parse(op.exec_python.call_args.args[1]);op.verify.assert_called_with(admission=1,lifecycle=0)


def test_unsupported_ipam_never_opens_admission(driver,promotion):
    op,env,allocation=promotion;before=env.read_bytes()
    review(driver,promotion,'admission_review',allocation_file={'path':str(allocation),'sha256':driver.digest(allocation)},targets=[str(uuid4())+'/new-disposable'])
    op.exec_python.side_effect=RuntimeError('unsupported allocation')
    with pytest.raises(RuntimeError):op.admit()
    assert env.read_bytes()==before;op.up.assert_not_called()


def test_admission_closes_again_on_failed_verification(driver,promotion):
    op,env,allocation=promotion
    review(driver,promotion,'admission_review',allocation_file={'path':str(allocation),'sha256':driver.digest(allocation)},targets=[str(uuid4())+'/new-disposable'])
    op.verify.side_effect=[{},RuntimeError('not ready')]
    with pytest.raises(RuntimeError):op.admit()
    assert 'IACP_ADMISSION_ENABLED=0' in env.read_text() and 'IACP_TERRAFORM_APPLY_ENABLED=1' in env.read_text()


def test_delete_enable_has_explicit_identity_and_stays_drained(driver,promotion):
    op,env,_=promotion;identifier=str(uuid4())
    review(driver,promotion,'deletion_review',instance_ids=[identifier],identities={identifier:{'vmid':30000,'node':'mng01','name':'discardable'}})
    op.enable_delete();op.approve.assert_called_once_with('enable-delete')
    assert 'IACP_TERRAFORM_DELETE_ENABLED=1' in env.read_text() and 'IACP_ADMISSION_ENABLED=0' in env.read_text()
    assert 'IACP_DISPOSABLE_INSTANCE_IDS='+identifier in env.read_text()
    ast.parse(op.exec_python.call_args.args[1])


@pytest.mark.parametrize('fields',[{'instance_ids':[],'identities':{}},{'instance_ids':['bad-uuid'],'identities':{}}])
def test_delete_enable_invalid_allowlist_never_mutates(driver,promotion,fields):
    op,env,_=promotion;before=env.read_bytes();review(driver,promotion,'deletion_review',**fields)
    with pytest.raises((RuntimeError,ValueError)):op.enable_delete()
    assert env.read_bytes()==before;op.up.assert_not_called()


def test_current_network_profile_does_not_fabricate_dhcp_support():
    with pytest.raises(ValueError):Allocation.model_validate({**allocation_data(30000),'ipv4_cidr':'dhcp'})
    with pytest.raises(ValueError):Allocation.model_validate({**allocation_data(30000),'vlan_id':None})
