from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile

from .config import Allocation, PlanError, SOURCE_REVISION, TERRAFORM_VERSION
from .runner import CommandRunner
from .observation import proxmox_resource

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
        env = self.environment(workspace)
        try:
            version = json.loads(self.runner.run(["version", "-json"], root, env, cancelled, phase="plan.preflight"))
            if version.get("terraform_version") != TERRAFORM_VERSION:
                raise PlanError("TERRAFORM_VERSION_MISMATCH")
            self.runner.run(["init", "-input=false", "-no-color", "-lockfile=readonly", "-backend-config=backend.tfbackend.json"], root, env, cancelled, phase="plan.init")
            self.runner.run(["validate", "-no-color"], root, env, cancelled, phase="plan.validate")
            self.runner.run(["plan", "-input=false", "-no-color", "-lock-timeout=30s", "-detailed-exitcode", "-out=saved.tfplan"],
                            root, env, cancelled, allowed=(0, 2), phase="plan.create")
            plan = json.loads(self.runner.run(["show", "-json", "saved.tfplan"], root, env, cancelled, phase="plan.inspect"))
            summary = verify_plan(plan, variables)
        except (ValueError, TypeError):
            raise PlanError("INVALID_PLAN_OUTPUT") from None
        saved = root / "saved.tfplan"
        saved.chmod(0o600)
        sha256 = digest_file(saved)
        salt = secrets.token_hex(32)
        write_private(workspace / "manifest.json", {"sha256": sha256, "source_revision": SOURCE_REVISION,
                      "state_key": state_key, "variables": variables, "backend": backend, "summary": summary,
                      "execution_context_salt": salt,
                      "execution_context_sha256": self.context_digest(env, salt)})
        return PlanResult(sha256, SOURCE_REVISION, summary)

    def environment(self, workspace: Path) -> dict:
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
        return env

    @staticmethod
    def context_digest(env: dict, salt: str) -> str:
        keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "PROXMOX_VE_API_TOKEN", "PROXMOX_VE_ENDPOINT", "SSL_CERT_FILE")
        payload = {key: env.get(key) for key in keys}
        if env.get("SSL_CERT_FILE"):
            payload["SSL_CERT_FILE_SHA256"] = digest_file(Path(env["SSL_CERT_FILE"]))
        return hashlib.sha256((salt + json.dumps(payload, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()

    def verify_artifacts(self, instance_id: str, attempt: int, sha256: str, state_key: str, allocation: dict, variables: dict) -> dict:
        """Verificar el vínculo persistido sin exigir credenciales de una mutación nueva."""
        workspace = self.workspace(instance_id, attempt)
        try:
            manifest = json.loads((workspace / "manifest.json").read_text())
            saved = workspace / "source" / "stacks" / "kvm-instance" / "saved.tfplan"
            if (manifest["sha256"] != sha256 or digest_file(saved) != sha256 or manifest["state_key"] != state_key
                    or manifest["source_revision"] != SOURCE_REVISION
                    or manifest["variables"] != variables
                    or manifest["backend"] != Allocation.model_validate(allocation).backend.arguments(state_key)
                    or json.loads((saved.parent / "backend.tfbackend.json").read_text()) != manifest["backend"]
                    or json.loads((saved.parent / "iacp.auto.tfvars.json").read_text()) != variables):
                raise ValueError("Plan changed")
            context_keys = {"execution_context_salt", "execution_context_sha256"}
            present = context_keys.intersection(manifest)
            if present and (present != context_keys or any(
                    not isinstance(manifest[key], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[key])
                    for key in context_keys)):
                raise ValueError("Invalid execution context")
        except (OSError, ValueError, KeyError, TypeError):
            raise PlanError("PLAN_INTEGRITY_FAILED") from None
        return manifest

    def prepare_reconcile(self, instance_id: str, attempt: int, sha256: str, state_key: str, allocation: dict, variables: dict) -> dict:
        manifest = self.verify_artifacts(instance_id, attempt, sha256, state_key, allocation, variables)
        return {"plan_sha256": sha256, "legacy_manifest": "execution_context_salt" not in manifest,
                "state_key": state_key, "workspace": "default", "backend_bucket": manifest["backend"]["bucket"],
                "source_revision": SOURCE_REVISION, "execution_enabled": False}

    def prepare_apply(self, instance_id: str, attempt: int, sha256: str, state_key: str, allocation: dict, variables: dict) -> dict:
        manifest = self.verify_artifacts(instance_id, attempt, sha256, state_key, allocation, variables)
        env = self.environment(self.workspace(instance_id, attempt))
        if ("execution_context_salt" not in manifest
                or self.context_digest(env, manifest["execution_context_salt"]) != manifest["execution_context_sha256"]):
            raise PlanError("PLAN_INTEGRITY_FAILED")
        return {"plan_sha256": sha256, "state_key": state_key, "source_revision": SOURCE_REVISION,
                "approval_required": True, "execution_enabled": False}

    def apply(self, instance_id=None, attempt=None, sha256=None, state_key=None,
              allocation=None, variables=None, cancelled=lambda: False, before_mutation=lambda: None):
        if os.environ.get("IACP_TERRAFORM_APPLY_ENABLED") != "1":
            raise PlanError("APPLY_DISABLED")
        if not all((instance_id, attempt, sha256, state_key, allocation, variables)):
            raise PlanError("APPROVED_PLAN_REQUIRED")
        self.prepare_apply(instance_id, attempt, sha256, state_key, allocation, variables)
        workspace = self.workspace(instance_id, attempt)
        root = workspace / "source" / "stacks" / "kvm-instance"
        env = self.environment(workspace)
        self.initialize(root, env, cancelled, "apply")
        plan = self.read_json(["show", "-json", "saved.tfplan"], root, env, cancelled, "apply.inspect")
        verify_plan(plan, variables)
        # Revalidar el binario inmediatamente antes de consumir la autorización.
        self.prepare_apply(instance_id, attempt, sha256, state_key, allocation, variables)
        if cancelled():
            raise PlanError("CANCELLED")
        before_mutation()
        self.runner.apply_saved(root, env, cancelled)
        return self.observe(instance_id, attempt, allocation, variables, cancelled)

    def read_json(self, args, root, env, cancelled, phase):
        try:
            value = json.loads(self.runner.run(args, root, env, cancelled, phase=phase))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except (ValueError, TypeError):
            raise PlanError("INVALID_TERRAFORM_OUTPUT", phase=phase, command=args[0]) from None

    def initialize(self, root, env, cancelled, phase):
        if self.read_json(["version", "-json"], root, env, cancelled, f"{phase}.preflight").get("terraform_version") != TERRAFORM_VERSION:
            raise PlanError("TERRAFORM_VERSION_MISMATCH", phase=f"{phase}.preflight", command="version")
        self.runner.run(["init", "-input=false", "-no-color", "-lockfile=readonly", "-reconfigure",
                         "-backend-config=backend.tfbackend.json"], root, env, cancelled, phase=f"{phase}.init")

    def observe(self, instance_id, attempt, allocation, variables, cancelled):
        workspace = self.workspace(instance_id, attempt)
        root = workspace / "source" / "stacks" / "kvm-instance"
        try:
            env = self.environment(workspace)
        except PlanError as failure:
            failure.phase = failure.phase or "observe.credentials"
            raise
        self.initialize(root, env, cancelled, "observe")
        state_present, proxmox_present = None, None
        try:
            state = self.read_json(["state", "pull"], root, env, cancelled, "observe.state")
            resource = state_resource(state, variables)
            state_present = resource is not None
            proxmox = proxmox_resource(env, variables["instance"], cancelled)
            proxmox_present = proxmox["present"]
            if state_present and proxmox_present:
                # El plan refresh-only NO contiene cambios de recursos. Sus valores
                # refrescados se serializan en prior_state, no en planned_values.
                self.runner.run(["plan", "-refresh-only", "-input=false", "-no-color", "-lock-timeout=30s",
                                 "-out=observation.tfplan"], root, env, cancelled, phase="observe.refresh")
                observed = self.read_json(["show", "-json", "observation.tfplan"], root, env, cancelled, "observe.inspect")
                if (not isinstance(observed, dict) or observed.get("errored") is not False
                        or observed.get("complete") is not True or observed.get("resource_changes")
                        or observed.get("deferred_changes") or observed.get("action_invocations")
                        or observed.get("deferred_action_invocations")):
                    raise PlanError("INCOMPLETE_OBSERVATION", phase="observe.inspect")
                try:
                    values = observed.get("prior_state", {}).get("values", {})
                    result = observed_resource(values, variables)
                except PlanError as failure:
                    if failure.code == "RESOURCE_ABSENT":
                        raise PlanError("PROVIDER_OBSERVATION_INCONSISTENT", phase="observe.inspect") from None
                    raise
                result["power_state"] = proxmox["power_state"]
            # Los pulls no adquieren lock. Detectar cambios del backend durante
            # la observación; refresh-only mantiene el lock nativo de Terraform.
            latest = self.read_json(["state", "pull"], root, env, cancelled, "observe.state_verify")
            if latest != state:
                raise PlanError("STATE_CHANGED_DURING_OBSERVATION", phase="observe.state_verify")
            if not state_present or not proxmox_present:
                code = ("UNMANAGED_RESOURCE" if proxmox_present else
                        "STATE_RESOURCE_MISSING" if state_present else "RESOURCE_ABSENT")
                raise PlanError(code, phase="observe.compare")
            return {**result, "observation": {"state_present": True, "proxmox_present": True,
                    "state_serial": state["serial"], "values_source": "prior_state.values"}}
        except PlanError as failure:
            failure.phase = failure.phase or "observe.inspect"
            failure.state_present, failure.proxmox_present = state_present, proxmox_present
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PlanError("INVALID_TERRAFORM_OUTPUT", phase="observe.inspect") from None


def state_resource(state, variables):
    """State pull usa recursos planos con module e instances[].attributes (v4)."""
    try:
        if (state["version"] != 4 or not isinstance(state["lineage"], str) or not state["lineage"]
                or type(state["serial"]) is not int or state["serial"] < 0
                or not isinstance(state["resources"], list)):
            raise ValueError("Invalid state")
        if any(row["mode"] not in {"managed", "data"} for row in state["resources"]):
            raise ValueError("Invalid resource mode")
        resources = [row for row in state["resources"] if row["mode"] == "managed"]
        if not resources:
            return None
        if len(resources) != 1:
            raise PlanError("UNOWNED_STATE", phase="observe.state")
        resource = resources[0]
        address = ".".join(filter(None, [resource.get("module"), resource["type"], resource["name"]]))
        if address != ADDRESS:
            raise PlanError("UNOWNED_STATE", phase="observe.state")
        instances = resource["instances"]
        if (len(instances) != 1 or instances[0].get("deposed") or instances[0].get("index_key") is not None
                or instances[0].get("status") == "tainted"):
            raise PlanError("INCOMPLETE_OBSERVATION", phase="observe.state")
        attributes = instances[0]["attributes"]
        verify_identity(attributes, variables["instance"])
        return attributes
    except PlanError as failure:
        failure.phase = failure.phase or "observe.state"
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise PlanError("INVALID_TERRAFORM_OUTPUT", phase="observe.state") from None


def verify_identity(actual, expected):
    # id es una cadena en el provider; vm_id es numérico. Exigir ambos evita
    # adoptar un objeto con identidad parcial o un ID de Terraform diferente.
    if (str(actual.get("id")) != str(expected["vm_id"])
            or str(actual.get("vm_id")) != str(expected["vm_id"])
            or expected["vm_id"] == 104 or actual.get("node_name") != expected["node_name"]
            or actual.get("name") != expected["vm_name"]):
        raise PlanError("OBSERVED_RESOURCE_MISMATCH")


def observed_resource(values: dict, variables: dict) -> dict:
    resources = []
    def collect(module):
        resources.extend(row for row in module.get("resources", []) if row.get("mode", "managed") == "managed")
        for child in module.get("child_modules", []):
            collect(child)
    collect(values.get("root_module", {}))
    if not resources:
        raise PlanError("RESOURCE_ABSENT")
    if len(resources) != 1 or resources[0].get("address") != ADDRESS:
        raise PlanError("UNOWNED_STATE")
    actual = resources[0].get("values", {})
    expected = variables["instance"]
    verify_identity(actual, expected)
    try:
        disk = next(row for row in actual["disk"] if row["interface"] == expected["boot_disk"]["interface"])
        if (actual["vm_id"] != expected["vm_id"] or actual["vm_id"] == 104
                or actual["node_name"] != expected["node_name"] or actual["name"] != expected["vm_name"]
                or actual["cpu"][0]["cores"] != expected["cpu_cores"]
                or actual["memory"][0]["dedicated"] != expected["memory_mb"]
                or disk["size"] != expected["boot_disk"]["size_gb"]
                or disk["datastore_id"] != expected["boot_disk"]["datastore_id"]
                or not isinstance(actual["started"], bool)):
            raise ValueError("Unexpected resource")
        return {"simulated": False, "infrastructure_kind": "kvm", "vmid": actual["vm_id"],
                "node": actual["node_name"], "name": actual["name"],
                "vcpus": actual["cpu"][0]["cores"], "memory_mb": actual["memory"][0]["dedicated"],
                "disk_gb": disk["size"], "datastore": disk["datastore_id"],
                "power_state": "running" if actual["started"] else "stopped"}
    except (KeyError, TypeError, IndexError, StopIteration, ValueError):
        raise PlanError("OBSERVED_RESOURCE_MISMATCH") from None
