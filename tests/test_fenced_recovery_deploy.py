"""Recovery rollout allows exactly reviewed uncertainty, never runnable jobs."""
import ast
from copy import deepcopy
import json
from unittest.mock import Mock
from uuid import uuid4
import pytest
from tests.test_go_live import driver

@pytest.fixture
def fence():
    identifier=str(uuid4())
    return identifier,{'schema':[driver.HEAD],'nonterminal':{'terraform_applies':[{'id':identifier,'status':'uncertain'}]},'queues':{},'unacked':0}

def test_default_drain_never_ignores_uncertainty(fence):
    identifier,report=fence
    with pytest.raises(RuntimeError):driver.check_idle(report)
    driver.check_idle(report,(identifier,))
    assert report['nonterminal']['terraform_applies']==[{'id':identifier,'status':'uncertain'}]

@pytest.mark.parametrize('bad',['running','different','other-job','queued','unacked','missing'])
def test_recovery_scope_cannot_hide_concurrent_work(fence,bad):
    identifier,report=fence;report=deepcopy(report)
    if bad=='running':report['nonterminal']['terraform_applies'][0]['status']='running'
    if bad=='different':report['nonterminal']['terraform_applies'][0]['id']=str(uuid4())
    if bad=='other-job':report['nonterminal']['jobs']=[{'id':str(uuid4()),'status':'queued'}]
    if bad=='queued':report['queues']={'terraform':1}
    if bad=='unacked':report['unacked']=1
    if bad=='missing':report['nonterminal']['terraform_applies']=[]
    with pytest.raises(RuntimeError):driver.check_idle(report,(identifier,))

def test_fenced_rollout_requires_drained_flags_and_native_empty_state_proof(fence):
    identifier,report=fence;p=object.__new__(driver.Promotion)
    p.m={'recovery_deployment_review':{}};p.canonical=Mock(return_value=True)
    p.operator_review=Mock(return_value={'mode':'readonly-apply-recovery','source_revision':driver.SOURCE,'approval_id':identifier})
    cfg={'services':{'api':{'environment':{'IACP_ADMISSION_ENABLED':'0','IACP_KVM_LIFECYCLE_ENABLED':'0','IACP_TERRAFORM_DELETE_ENABLED':'0'}},
                     'terraform-worker':{'volumes':[{'target':'/iac-source','source':'/immutable/source'}]}}}
    p.compose=Mock(return_value=json.dumps(cfg));p.container=Mock(return_value={'Mounts':[{'Destination':'/iac-source','Source':'/immutable/source'}]})
    p.exec_python=Mock(return_value=json.dumps({'approval_id':identifier}));p.evidence=Mock()
    assert p.review_recovery_deployment({},report)==(identifier,)
    code=p.exec_python.call_args.args[1];ast.parse(code)
    assert '.observe(' in code and '.apply(' not in code and '.plan(' not in code
    cfg['services']['api']['environment']['IACP_ADMISSION_ENABLED']='1';p.compose.return_value=json.dumps(cfg)
    with pytest.raises(RuntimeError,match='drained'):p.review_recovery_deployment({},report)
