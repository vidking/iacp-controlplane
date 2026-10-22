"""Operator-only historical certification. Default is validation without SQL writes.

Never executes a mutation, initializes a remote backend or changes old artifacts.
"""
import argparse
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select

from ..models import Instance, Operation, PowerExecution, PowerOwnershipCertificate, TerraformExecution
from ..services.instances import Conflict, event
from ..terraform.config import Allocation, PlanError, StrictModel, TERRAFORM_VERSION
from ..terraform.executor import (TerraformExecutor, approved_state_metadata, digest_file,
                                  observed_resource, state_resource, verify_checks, verify_plan,
                                  verify_postconditions, write_private)
from ..terraform.locks import instance_lock
from ..terraform.state import S3StateReader, unique_object
from .certification_binding import binding_digest
from .ownership import (KVM_POWER_SOURCE_ARCHIVE_SHA256, LEGACY_TERRAFORM_POWER_SOURCE,
                        ownership_revision)
from .proxmox import PowerError, ProxmoxPower
from .service import ACTIVE, managed_target


class ExpectedState(StrictModel):
    version_id: str = Field(min_length=1)
    lineage: str = Field(min_length=1)
    serial: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CertificationRequest(StrictModel):
    instance_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    tenant_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    approval_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    attempt: int = Field(ge=1)
    vmid: int = Field(ge=100)
    node: str = Field(min_length=1)
    name: str = Field(min_length=1)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_key: str = Field(min_length=1)
    state: ExpectedState
    pve_version: str = Field(min_length=1)
    actor: str = Field(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")
    change_reference: str = Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")
    release_reference: str = Field(min_length=1, max_length=256)


def require(condition, code):
    if not condition:
        raise PlanError(code, phase="ownership.certify")


def verify_noop(plan, variables, state_values, power_state):
    require(plan.get("errored") is False and plan.get("complete") is True
            and plan.get("applyable") is False, "POWER_CERTIFICATION_INCOMPLETE_PLAN")
    require(not any(plan.get(key) for key in ("resource_drift", "deferred_changes", "action_invocations",
                                               "deferred_action_invocations")), "POWER_CERTIFICATION_UNSAFE_PLAN")
    verify_plan(plan, variables)
    change = plan["resource_changes"][0]["change"]
    def unknown(value):
        if isinstance(value, dict):
            return any(unknown(item) for item in value.values())
        if isinstance(value, list):
            return any(unknown(item) for item in value)
        return value is True
    require(change["actions"] == ["no-op"] and change.get("before") == change.get("after")
            and not plan["resource_changes"][0].get("change", {}).get("replace_paths")
            and not unknown(change.get("after_unknown")), "POWER_CERTIFICATION_UNSAFE_PLAN")
    require(all(row.get("actions") == ["no-op"] for row in plan.get("output_changes", {}).values()),
            "POWER_CERTIFICATION_UNSAFE_PLAN")
    before = observed_resource(plan.get("prior_state", {}).get("values", {}), variables)
    after = observed_resource(plan.get("planned_values", {}), variables)
    require(before["power_state"] == after["power_state"] == power_state
            and change["before"].get("started") is state_values["started"], "POWER_CERTIFICATION_POWER_MISMATCH")
    verify_checks(plan)


def offline_plan(executor, snapshot, variables, state_values, power_state, heartbeat, historical_plan):
    """Exact reviewed archive; fresh local backend, offline provider and local copy."""
    revision = ownership_revision()
    require(revision is not None, "POWER_OWNERSHIP_UNAVAILABLE")
    try:
        archive = subprocess.run(["git", "-c", f"safe.directory={executor.source}", "-C", str(executor.source),
                                  "archive", revision], capture_output=True, check=True, timeout=30).stdout
        require(hashlib.sha256(archive).hexdigest() == KVM_POWER_SOURCE_ARCHIVE_SHA256,
                "POWER_CERTIFICATION_SOURCE_INTEGRITY_FAILED")
        with tempfile.TemporaryDirectory(prefix="iacp-ownership-") as directory:
            workspace = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source_tar:
                require(not any(item.issym() or item.islnk() for item in source_tar.getmembers()), "INVALID_SOURCE")
                source_tar.extractall(workspace / "source", filter="data")
            root = workspace / "source/stacks/kvm-instance"
            versions = root / "versions.tf"
            content = versions.read_text()
            require(content.count('  backend "s3" {}\n') == 1, "POWER_CERTIFICATION_SOURCE_INTEGRITY_FAILED")
            versions.write_text(content.replace('  backend "s3" {}\n', ""))
            # Never pass real credentials or endpoint to the offline provider.
            (root / "providers.tf").write_text('provider "proxmox" {\n'
                ' endpoint = "https://127.0.0.1:1/"\n'
                ' api_token = "fixture@pve!offline=synthetic"\n insecure = false\n}\n')
            write_private(root / "iacp.auto.tfvars.json", variables)
            write_private(root / "terraform.tfstate", snapshot.state)
            (workspace / "terraform.rc").write_text("disable_checkpoint = true\n")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory,
                   "TF_DATA_DIR": str(workspace / "data"), "TF_WORKSPACE": "default", "TF_INPUT": "0",
                   "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1",
                   "TF_CLI_CONFIG_FILE": str(workspace / "terraform.rc"), "AWS_EC2_METADATA_DISABLED": "true"}
            cancelled = lambda: False
            def run(args):
                heartbeat()
                return executor.read_json(args, root, env, cancelled, "ownership.plan")
            require(run(["version", "-json"])["terraform_version"] == TERRAFORM_VERSION,
                    "TERRAFORM_VERSION_MISMATCH")
            executor.runner.run(["init", "-backend=false", "-input=false", "-no-color", "-lockfile=readonly"],
                                root, env, cancelled, phase="ownership.init")
            require(run(["version", "-json"]).get("provider_selections") == {
                "registry.terraform.io/bpg/proxmox": "0.116.1"}, "POWER_CERTIFICATION_PROVIDER_MISMATCH")
            # Decode the old binary using the verified provider installed ONLY in
            # this local workspace. Never init the old S3 backend or write its
            # artifacts, and never give Terraform the real execution credentials.
            approved = run(["show", "-json", str(historical_plan.resolve())])
            verify_plan(approved, variables)
            verify_postconditions(approved, state_values, source="persisted_state")
            before = digest_file(root / "terraform.tfstate")
            executor.runner.run(["plan", "-refresh=false", "-input=false", "-no-color", "-lock-timeout=30s",
                                 "-out=certification.tfplan"], root, env, cancelled, phase="ownership.plan")
            verify_noop(run(["show", "-json", "certification.tfplan"]), variables, state_values, power_state)
            require(before == digest_file(root / "terraform.tfstate"), "POWER_CERTIFICATION_LOCAL_STATE_CHANGED")
            return {"source_archive_sha256": KVM_POWER_SOURCE_ARCHIVE_SHA256,
                    "plan_sha256": digest_file(root / "certification.tfplan"),
                    "terraform": TERRAFORM_VERSION, "provider": "0.116.1", "actions": ["no-op"],
                    "backend": "isolated-local", "refresh": False}
    except (OSError, subprocess.SubprocessError, tarfile.TarError):
        raise PlanError("POWER_CERTIFICATION_SOURCE_UNAVAILABLE", phase="ownership.certify") from None


def certify(request, sessions, executor, *, record=False, client=None):
    require(os.environ.get("IACP_KVM_LIFECYCLE_ENABLED", "0") == "0", "POWER_CERTIFICATION_REQUIRES_DISABLED_LIFECYCLE")
    require(os.environ.get("IACP_TERRAFORM_APPLY_ENABLED") == "1", "POWER_CERTIFICATION_APPLY_FLAG_CHANGED")
    revision = ownership_revision()
    require(revision is not None, "POWER_OWNERSHIP_UNAVAILABLE")
    with instance_lock(sessions.kw["bind"], request.instance_id) as heartbeat, sessions() as session:
        instance = session.get(Instance, request.instance_id, with_for_update=True)
        require(instance is not None and instance.tenant_id == request.tenant_id, "POWER_CERTIFICATION_IDENTITY_MISMATCH")
        target, approval = managed_target(session, instance)
        execution = session.get(TerraformExecution, instance.id, with_for_update=True)
        operation = session.get(Operation, execution.operation_id, with_for_update=True)
        require(operation.kind == "instance.create" and operation.status == "completed"
                and operation.instance_id == instance.id and operation.tenant_id == instance.tenant_id
                and approval.started_at is not None and approval.finished_at is not None
                and execution.status == "planned", "POWER_CERTIFICATION_PROVISIONING_NOT_TERMINAL")
        require((target.vmid, target.node, target.name, approval.id, execution.attempt,
                 execution.source_revision, execution.plan_sha256, execution.state_key) ==
                (request.vmid, request.node, request.name, request.approval_id, request.attempt,
                 request.source_revision, request.plan_sha256, request.state_key)
                and execution.source_revision == LEGACY_TERRAFORM_POWER_SOURCE,
                "POWER_CERTIFICATION_IDENTITY_MISMATCH")
        require(session.scalar(select(PowerExecution.operation_id).where(
            PowerExecution.instance_id == instance.id, PowerExecution.status.in_(ACTIVE))) is None,
            "POWER_OPERATION_ACTIVE")
        allocation = Allocation.model_validate(execution.allocation)
        variables = allocation.variables(instance)
        manifest = executor._verify_artifacts_revision(instance.id, execution.attempt, execution.plan_sha256,
            execution.state_key, execution.allocation, variables, LEGACY_TERRAFORM_POWER_SOURCE)
        original = executor.workspace(instance.id, execution.attempt) / "source/stacks/kvm-instance"
        approved_state = approved_state_metadata(original / "saved.tfplan")
        digest = binding_digest(instance, execution, approval)
        # Credentials are used only for two GET snapshots. Terraform receives none.
        with tempfile.TemporaryDirectory(prefix="iacp-ownership-read-") as directory:
            env = executor.environment(Path(directory))
            reader = S3StateReader(manifest["backend"], env)
            try:
                snapshot = reader.read(lambda: False)
                require(snapshot.metadata() == request.state.model_dump(), "POWER_CERTIFICATION_STATE_MISMATCH")
                require((not approved_state["lineage"] or approved_state["lineage"] == snapshot.state["lineage"])
                        and snapshot.state["serial"] >= approved_state["serial"], "APPROVED_STATE_MISMATCH")
                values = state_resource(snapshot.state, variables)
                require(values is not None, "STATE_RESOURCE_MISSING")
                client = client or ProxmoxPower.configured()
                client.permissions(target, mutate=True)
                version = client._request("GET", "/version")
                require(isinstance(version, dict) and version.get("version") == request.pve_version,
                        "POWER_CERTIFICATION_PVE_VERSION_MISMATCH")
                observed = client.observe(target, preflight=True)
                require(type(values.get("started")) is bool and observed["power_state"] == (
                    "running" if values["started"] else "stopped"), "POWER_CERTIFICATION_POWER_MISMATCH")
                evidence = offline_plan(executor, snapshot, variables, values, observed["power_state"], heartbeat,
                                        original / "saved.tfplan")
                require(client.observe(target, preflight=True)["power_state"] == observed["power_state"],
                        "POWER_CERTIFICATION_POWER_MISMATCH")
                evidence.update(state_consistency=snapshot.verify(reader.read(lambda: False)),
                                original_plan_sha256=execution.plan_sha256, pve_version=request.pve_version,
                                power_state=observed["power_state"], release_reference=request.release_reference)
            finally:
                reader.close()
        heartbeat()
        # Refuse any changed binding before recording; no updates to old entities.
        for row in (instance, execution, approval, operation):
            session.refresh(row)
        require(digest == binding_digest(instance, execution, approval) and operation.status == "completed",
                "POWER_CERTIFICATION_BINDING_CHANGED")
        certificate = session.scalar(select(PowerOwnershipCertificate).where(
            PowerOwnershipCertificate.approval_id == approval.id,
            PowerOwnershipCertificate.ownership_revision == revision))
        if record:
            if certificate:
                require(certificate.binding_sha256 == digest, "POWER_CERTIFICATION_BINDING_CHANGED")
            else:
                certificate = PowerOwnershipCertificate(id=str(uuid4()), instance_id=instance.id, approval_id=approval.id,
                    source_revision=execution.source_revision, ownership_revision=revision, binding_sha256=digest,
                    evidence=deepcopy(evidence), certified_by=request.actor, change_reference=request.change_reference)
                session.add(certificate)
                event(session, operation, "power_ownership_certified", request.actor)
            heartbeat()
            session.commit()
        return {"validated": True, "recorded": record, "certificate_id": certificate.id if record else None,
                "ownership_revision": revision, "source_revision": execution.source_revision, "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--record", action="store_true", help="Persist the certificate and its audit event after all checks")
    args = parser.parse_args()
    try:
        from ..db import get_session_factory
        request = CertificationRequest.model_validate(json.loads(args.expectations.read_text(), object_pairs_hook=unique_object))
        executor = TerraformExecutor(Path(os.environ["IACP_TERRAFORM_SOURCE_DIR"]), Path(os.environ["IACP_TERRAFORM_ARTIFACTS_DIR"]))
        print(json.dumps(certify(request, get_session_factory(), executor, record=args.record), sort_keys=True))
    except (PlanError, PowerError, Conflict) as failure:
        parser.exit(1, getattr(failure, "code", str(failure)) + "\n")
    except Exception:
        parser.exit(1, "POWER_CERTIFICATION_FAILED\n")


if __name__ == "__main__":
    main()
