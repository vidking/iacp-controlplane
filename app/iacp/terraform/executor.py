from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

from .config import Allocation, PlanError, SOURCE_REVISION, TERRAFORM_VERSION
from .runner import CommandRunner

ADDRESS = "module.kvm.proxmox_virtual_environment_vm.this"


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_private(path: Path, value: dict) -> None:
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, sort_keys=True)
    path.chmod(0o600)


def verify_plan(plan: dict, variables: dict) -> dict:
    """Aceptar solo creación/no-op de la KVM asignada; nunca adoptar recursos ajenos."""
    instance = variables["instance"]
    if instance["vm_id"] == 104 or instance["template_vm_id"] == 104:
        raise PlanError("CONTROLPLANE_PROTECTED")

    def check_prior(module):
        for resource in module.get("resources", []):
            if resource.get("address") != ADDRESS or resource.get("values", {}).get("vm_id") != instance["vm_id"]:
                raise PlanError("UNOWNED_STATE")
        for child in module.get("child_modules", []):
            check_prior(child)

    check_prior(plan.get("prior_state", {}).get("values", {}).get("root_module", {}))
    changes = plan.get("resource_changes", [])
    if len(changes) != 1:
        raise PlanError("UNSAFE_PLAN")
    change = changes[0]
    actions = change.get("change", {}).get("actions")
    after = change.get("change", {}).get("after") or {}
    if change.get("address") != ADDRESS or actions not in (["create"], ["no-op"]):
        raise PlanError("UNSAFE_PLAN")
    try:
        disk = next(item for item in after["disk"] if item["interface"] == instance["boot_disk"]["interface"])
        correct = (after["vm_id"] == instance["vm_id"] and after["node_name"] == instance["node_name"]
                   and after["name"] == instance["vm_name"] and after["cpu"][0]["cores"] == instance["cpu_cores"]
                   and after["memory"][0]["dedicated"] == instance["memory_mb"]
                   and disk["size"] == instance["boot_disk"]["size_gb"]
                   and disk["datastore_id"] == instance["boot_disk"]["datastore_id"]
                   and after["clone"][0]["vm_id"] == instance["template_vm_id"])
    except (KeyError, TypeError, IndexError, StopIteration):
        correct = False
    if not correct:
        raise PlanError("UNSAFE_PLAN")
    return {"address": ADDRESS, "actions": actions, "reserved_vmid": instance["vm_id"], "apply_enabled": False}


@dataclass(frozen=True)
class PlanResult:
    sha256: str
    source_revision: str
    summary: dict


class TerraformExecutor:
    """No importa FastAPI ni conoce sesiones SQL; solo prepara planes verificados."""
    def __init__(self, source: Path, artifacts: Path, runner=None):
        self.source = source
        self.artifacts = artifacts
        self.runner = runner or CommandRunner()

    def workspace(self, instance_id: str, attempt: int) -> Path:
        from uuid import UUID
        identifier = str(UUID(instance_id))
        if attempt < 1:
            raise PlanError("INVALID_ATTEMPT")
        return self.artifacts / identifier / f"attempt-{attempt}"

    def plan(self, instance_id: str, attempt: int, state_key: str, allocation: Allocation,
             variables: dict, cancelled) -> PlanResult:
        if os.environ.get("IACP_TERRAFORM_PLAN_ENABLED") != "1":
            raise PlanError("REAL_PLAN_DISABLED")
        workspace = self.workspace(instance_id, attempt)
        # Un intento tiene un directorio exclusivo; nunca sobrescribir un plan anterior.
        workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
        workspace.chmod(0o700)
        try:
            archive = subprocess.run(["git", "-c", f"safe.directory={self.source}", "-C", str(self.source), "archive", SOURCE_REVISION],
                                     check=True, capture_output=True, timeout=30).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as source_tar:
                if any(item.issym() or item.islnk() for item in source_tar.getmembers()):
                    raise PlanError("INVALID_SOURCE")
                source_tar.extractall(workspace / "source", filter="data")
        except (OSError, subprocess.SubprocessError, tarfile.TarError):
            raise PlanError("SOURCE_UNAVAILABLE") from None
        root = workspace / "source" / "stacks" / "kvm-instance"
        backend = allocation.backend.arguments(state_key)
        write_private(root / "iacp.auto.tfvars.json", variables)
        write_private(root / "backend.tfbackend.json", backend)
        try:
            credentials = json.loads(Path(os.environ["IACP_TERRAFORM_CREDENTIALS_FILE"]).read_text())
            required = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "PROXMOX_VE_API_TOKEN", "PROXMOX_VE_ENDPOINT"}
            if not required.issubset(credentials) or set(credentials) - (required | {"AWS_SESSION_TOKEN"}):
                raise ValueError("Invalid credentials")
            if not all(isinstance(value, str) and value for value in credentials.values()):
                raise ValueError("Invalid credentials")
            from urllib.parse import urlsplit
            endpoint = urlsplit(credentials["PROXMOX_VE_ENDPOINT"])
            if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
                raise ValueError("Invalid endpoint")
        except (OSError, KeyError, TypeError, ValueError):
            raise PlanError("CREDENTIALS_REQUIRED") from None
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workspace),
               "TF_DATA_DIR": str(workspace / "data"), "TF_WORKSPACE": "default", "TF_INPUT": "0",
               "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1", "AWS_EC2_METADATA_DISABLED": "true",
               **credentials}
        ca_file = os.environ.get("IACP_TERRAFORM_CA_FILE")
        if ca_file:
            if not Path(ca_file).is_file():
                raise PlanError("CA_CERTIFICATE_REQUIRED")
            env["SSL_CERT_FILE"] = ca_file
        try:
            version = json.loads(self.runner.run(["version", "-json"], root, env, cancelled))
            if version.get("terraform_version") != TERRAFORM_VERSION:
                raise PlanError("TERRAFORM_VERSION_MISMATCH")
            self.runner.run(["init", "-input=false", "-no-color", "-lockfile=readonly", "-backend-config=backend.tfbackend.json"], root, env, cancelled)
            self.runner.run(["validate", "-no-color"], root, env, cancelled)
            self.runner.run(["plan", "-input=false", "-no-color", "-lock-timeout=30s", "-detailed-exitcode", "-out=saved.tfplan"],
                            root, env, cancelled, allowed=(0, 2))
            plan = json.loads(self.runner.run(["show", "-json", "saved.tfplan"], root, env, cancelled))
            summary = verify_plan(plan, variables)
        except (ValueError, TypeError):
            raise PlanError("INVALID_PLAN_OUTPUT") from None
        saved = root / "saved.tfplan"
        saved.chmod(0o600)
        sha256 = digest_file(saved)
        write_private(workspace / "manifest.json", {"sha256": sha256, "source_revision": SOURCE_REVISION,
                      "state_key": state_key, "variables": variables, "backend": backend, "summary": summary})
        return PlanResult(sha256, SOURCE_REVISION, summary)

    def prepare_apply(self, instance_id: str, attempt: int, sha256: str, state_key: str, allocation: dict, variables: dict) -> dict:
        workspace = self.workspace(instance_id, attempt)
        try:
            manifest = json.loads((workspace / "manifest.json").read_text())
            saved = workspace / "source" / "stacks" / "kvm-instance" / "saved.tfplan"
            if (manifest["sha256"] != sha256 or digest_file(saved) != sha256 or manifest["state_key"] != state_key
                    or manifest["source_revision"] != SOURCE_REVISION
                    or manifest["variables"] != variables
                    or manifest["backend"] != Allocation.model_validate(allocation).backend.arguments(state_key)):
                raise ValueError("Plan changed")
        except (OSError, ValueError, KeyError):
            raise PlanError("PLAN_INTEGRITY_FAILED") from None
        return {"plan_sha256": sha256, "state_key": state_key, "source_revision": SOURCE_REVISION,
                "approval_required": True, "execution_enabled": False}

    def apply(self, *args, **kwargs):
        raise PlanError("APPLY_DISABLED")
