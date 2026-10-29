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


OPERATION = "4652c4aa-58bb-4f92-94c9-c81790e74150"


def operation_report(*, status="uncertain", queues=None, unacked=0, extra=None):
    nonterminal = {
        "operations": [{"id": OPERATION, "status": status}],
        "jobs": [],
        "guest_initializations": [],
        "terraform_applies": [],
        "power_executions": [],
        "deletion_executions": [],
    }
    if extra:
        nonterminal.update(extra)
    return {"schema": [driver.HEAD], "nonterminal": nonterminal,
            "queues": queues or {}, "unacked": unacked}


def test_queued_operation_always_blocks_even_with_an_uncertain_fence():
    report = operation_report(status="queued")
    with pytest.raises(RuntimeError, match="Reviewed operation uncertainty fence changed"):
        driver.check_idle(report, allowed_operation_uncertain=(OPERATION,))


def test_uncertain_operation_without_reviewed_fence_blocks():
    with pytest.raises(RuntimeError, match="Nonterminal DB work"):
        driver.check_idle(operation_report())


def test_isolated_attested_operation_can_be_the_only_allowed_uncertainty():
    report = operation_report()
    driver.check_idle(report, allowed_operation_uncertain=(OPERATION,))
    assert report["nonterminal"]["operations"] == [{"id": OPERATION, "status": "uncertain"}]


def test_operation_fence_cannot_hide_broker_or_worker_activity():
    for report in (
        operation_report(queues={"terraform": 1}),
        operation_report(unacked=1),
        operation_report(extra={"jobs": [{"id": "other", "status": "running"}]}),
    ):
        with pytest.raises(RuntimeError):
            driver.check_idle(report, allowed_operation_uncertain=(OPERATION,))


def test_operation_fence_invalidates_when_identity_or_status_changes():
    for changed in (
        operation_report(status="running"),
        operation_report(extra={"operations": [{"id": "other", "status": "uncertain"}]}),
    ):
        with pytest.raises(RuntimeError, match="Reviewed operation uncertainty fence changed"):
            driver.check_idle(changed, allowed_operation_uncertain=(OPERATION,))


def test_reviewed_bootstrap_fence_requires_observe_only_redelivery():
    review = {
        "approved": True,
        "release": "a" * 40,
        "change_reference": "change-1",
        "mode": "readonly-operation-fence",
        "source_revision": driver.SOURCE,
        "operation_id": OPERATION,
        "instance_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "vmid": 30096,
        "node": "mng01",
        "name": "e2e-g-kvm1-20261028-202710",
        "operation_kind": "instance.access.bootstrap",
        "operation_status": "uncertain",
        "job_status": "failed",
        "redelivery_mode": "observe-only-fenced",
        "state_key_sha256": "s" * 64,
        "ownership_binding_sha256": "b" * 64,
        "state_resource_id": "mng01/30096",
        "state_snapshot": {"version_id": "v", "lineage": "l", "serial": 1,
                           "sha256": "s" * 64, "content_sha256": "c" * 64},
        "bootstrap_executor_sha256": "e" * 64,
    }
    p = object.__new__(driver.Promotion)
    p.m = {"recovery_deployment_review": {}}
    p.canonical = Mock(return_value=True)
    p.operator_review = Mock(return_value=review)
    p.compose = Mock(return_value=json.dumps({
        "services": {
            "api": {"environment": {
                "IACP_ADMISSION_ENABLED": "0",
                "IACP_KVM_LIFECYCLE_ENABLED": "0",
                "IACP_TERRAFORM_DELETE_ENABLED": "0",
            }},
            "terraform-worker": {"volumes": [{"target": "/iac-source", "source": "/immutable/source"}]},
        }
    }))
    p.container = Mock(return_value={"Mounts": [{"Destination": "/iac-source", "Source": "/immutable/source"}]})
    p._run_operation_fence_probe = Mock(return_value={
        "passed": True, "operation_id": OPERATION, "instance_id": review["instance_id"],
        "redelivery": "observe-only-fenced", "broker": "empty", "state_snapshot": review["state_snapshot"],
    })
    p.evidence = Mock()
    assert p.review_recovery_deployment({"reference": "change-1"}, {"schema": [driver.HEAD]}) == ()
    assert p.reviewed_operation_fences == (review,)

    review["redelivery_mode"] = "replay-allowed"
    with pytest.raises(RuntimeError, match="consumed read-only fence"):
        p.review_recovery_deployment({"reference": "change-1"}, {"schema": [driver.HEAD]})


def test_revalidation_failure_keeps_the_fence_closed():
    p = object.__new__(driver.Promotion)
    p.reviewed_operation_fences = ({"operation_id": OPERATION},)
    p._run_operation_fence_probe = Mock(side_effect=RuntimeError("FENCE_STATE_CHANGED"))
    p.evidence = Mock()
    with pytest.raises(RuntimeError, match="FENCE_STATE_CHANGED"):
        p.revalidate_operation_fences()
