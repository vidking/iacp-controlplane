"""Run ONLY in ownership-certifier; inspect the historical milestone without writes."""
import json
import os
from pathlib import Path
import tempfile

from iacp.db import get_session_factory
from iacp.models import Instance, TerraformExecution, TerraformApply
from iacp.lifecycle.proxmox import ProxmoxPower, Target, granted
from iacp.terraform.config import Allocation
from iacp.terraform.executor import TerraformExecutor
from iacp.terraform.state import S3StateReader

with get_session_factory()() as session:
    instance = session.get(Instance, "a1b2d063-6240-4969-bc04-e7e0e6be9e73")
    execution = session.get(TerraformExecution, instance.id)
    approval = session.get(TerraformApply, "1750ee70-a6ca-4036-a764-c4b077a0b588")
    assert (instance.vmid, instance.node, instance.name, execution.attempt, execution.source_revision,
            instance.status, approval.status) == (106, "mng01", "iacp-kvm-lab-01", 4,
                "40dc5d10387cfb50384ecd3ddddfd75289fe7bc4", "ready", "completed")
    assert (approval.instance_id, approval.attempt, approval.plan_sha256) == (
        instance.id, execution.attempt, execution.plan_sha256)
    executor = TerraformExecutor(Path(os.environ["IACP_TERRAFORM_SOURCE_DIR"]), Path(os.environ["IACP_TERRAFORM_ARTIFACTS_DIR"]))
    with tempfile.TemporaryDirectory() as directory:
        reader = S3StateReader(Allocation.model_validate(execution.allocation).backend.arguments(execution.state_key), executor.environment(Path(directory)))
        try:
            first = reader.read(lambda: False)
            first.verify(reader.read(lambda: False))
            state = first.metadata()
        finally:
            reader.close()
    client = ProxmoxPower.configured()
    target = Target(instance.vmid, instance.node, instance.name)
    client.permissions(target, mutate=True)
    assert client.observe(target, preflight=True)["power_state"] == "stopped"
    denied = client._request("GET", "/access/permissions?path=/vms/104")
    assert not granted(denied.get("/vms/104", {}), "VM.PowerMgmt"), "VM 104 must be excluded"
    print(json.dumps(dict(instance_id=instance.id, tenant_id=instance.tenant_id, approval_id=approval.id,
        attempt=execution.attempt, vmid=instance.vmid, node=instance.node, name=instance.name,
        source_revision=execution.source_revision, plan_sha256=execution.plan_sha256, state_key=execution.state_key,
        state=state, pve_version=client._request("GET", "/version")["version"],
        actor=os.environ["CERT_ACTOR"], change_reference=os.environ["CERT_CHANGE"],
        release_reference=os.environ["CERT_RELEASE"]), sort_keys=True))
