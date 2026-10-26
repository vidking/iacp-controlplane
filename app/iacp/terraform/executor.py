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
import zipfile

from .config import Allocation, PlanError, SOURCE_REVISION, TERRAFORM_VERSION
from .runner import CommandRunner
from .observation import proxmox_resource, require_clone_permissions
from .execution_health import require_execution_health
from .state import S3StateReader

ADDRESS = "module.kvm.proxmox_virtual_environment_vm.this"


def verify_network(actual, expected):
    if expected.get('network_mode', 'static') != 'dhcp':
        return
    try:
        nic=actual['network_device'];ipv4=actual['initialization'][0]['ip_config'][0]['ipv4'][0]
        if (len(nic)!=1 or nic[0]['bridge']!=expected['network_bridge'] or nic[0]['model']!='virtio'
                or nic[0].get('vlan_id') not in ({None,0} if expected['vlan_id'] is None else {expected['vlan_id']})
                or ipv4['address']!='dhcp' or ipv4.get('gateway') not in {None,''}):
            raise ValueError()
    except (KeyError,TypeError,IndexError,ValueError):
        raise PlanError('DHCP_NETWORK_CONTRACT_MISMATCH') from None


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_private(path: Path, value: dict) -> None:
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, sort_keys=True)
    path.chmod(0o600)


def approved_state_metadata(path):
    """Cabecera del snapshot sellado en el plan 1.16.6; nunca extraer/escribir state."""
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist().count("tfstate") != 1 or archive.getinfo("tfstate").file_size > 16 * 1024 * 1024:
                raise ValueError("Invalid embedded state")
            header = json.loads(archive.read("tfstate"))
        if (header["version"] != 4 or not isinstance(header["lineage"], str)
                or type(header["serial"]) is not int or header["serial"] < 0
                or (not header["lineage"] and header["serial"] != 0)):
            raise ValueError("Invalid embedded metadata")
        return {"lineage": header["lineage"], "serial": header["serial"]}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
        raise PlanError("PLAN_INTEGRITY_FAILED", phase="observe.approved_state") from None


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
                   and after["name"] == instance["vm_name"] and after["cpu"][0]["cores"] * after["cpu"][0].get("sockets",1) == instance["cpu_cores"]
                   and after["memory"][0]["dedicated"] == instance["memory_mb"]
                   and disk["size"] == instance["boot_disk"]["size_gb"]
                   and disk["datastore_id"] == instance["boot_disk"]["datastore_id"]
                   and after["clone"][0]["vm_id"] == instance["template_vm_id"])
    except (KeyError, TypeError, IndexError, StopIteration):
        correct = False
    if not correct:
        raise PlanError("UNSAFE_PLAN")
    verify_network(after,instance)
    from .golden_hardware import verify_base_plan, verify_cpu, verify_disk_plan
    verify_cpu(after,variables)
    verify_base_plan(after,variables)
    if after.get('disk'):
        disk = next((item for item in after['disk']
                     if item.get('interface') == instance['boot_disk']['interface']), None)
        if disk is None:
            raise PlanError('GOLDEN_DISK_PLAN_MISMATCH')
        verify_disk_plan(disk, variables)
    if 'iacp_root_password' in variables:
        import hmac
        try:
            account = after['initialization'][0]['user_account'][0]
            if (account['username'] != 'root' or after.get('started') is not False
                    or not hmac.compare_digest(account['password'], variables['iacp_root_password'])):
                raise ValueError()
        except (KeyError, TypeError, IndexError, ValueError):
            raise PlanError('ROOT_ACCESS_PLAN_MISMATCH') from None
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

    def resolve_golden_hardware(self, allocation, vcpus):
        import tempfile
        from .golden_hardware import read
        with tempfile.TemporaryDirectory() as scratch:
            env=self.environment(Path(scratch))
            return read(env,allocation,vcpus).model_dump()

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
        if allocation.network_mode == 'dhcp':
            # Never adopt an existing VM through a new DHCP profile intent.
            import tempfile
            with tempfile.TemporaryDirectory() as scratch:
                env=self.environment(Path(scratch))
                require_clone_permissions(env,variables['instance'])
                require_execution_health(env, variables['instance'], cancelled)
                if proxmox_resource(env,variables['instance'],cancelled)['present']:
                    raise PlanError('VMID_ALREADY_PRESENT')
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
        from .access_adapter import install as install_access_adapter, DIGEST as ACCESS_ADAPTER_SHA256
        if 'iacp_root_password' in variables:
            install_access_adapter(workspace / 'source')
        if 'iacp_golden_hardware' in variables:
            from .golden_adapter import install as install_golden_adapter
            install_golden_adapter(workspace / 'source')
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
        if 'iacp_root_password' in variables:
            manifest = json.loads((workspace / 'manifest.json').read_text())
            write_private(workspace / 'manifest.json', {**manifest, 'access_adapter_sha256': ACCESS_ADAPTER_SHA256})
        if 'iacp_golden_hardware' in variables:
            from .golden_adapter import DIGEST as GOLDEN_ADAPTER_SHA256
            manifest=json.loads((workspace / 'manifest.json').read_text())
            write_private(workspace / 'manifest.json',{**manifest,'golden_adapter_sha256':GOLDEN_ADAPTER_SHA256})
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
        return self._verify_artifacts_revision(instance_id, attempt, sha256, state_key, allocation, variables, SOURCE_REVISION)

    def _verify_artifacts_revision(self, instance_id, attempt, sha256, state_key, allocation, variables, revision):
        # Historical inspection is used only by the offline ownership certifier.
        # apply/reconcile retain the strict current SOURCE_REVISION check above.
        workspace = self.workspace(instance_id, attempt)
        try:
            manifest = json.loads((workspace / "manifest.json").read_text())
            from .access_adapter import verify as verify_access_adapter
            verify_access_adapter(workspace / 'source', manifest, variables)
            from .golden_adapter import verify as verify_golden_adapter
            verify_golden_adapter(workspace / 'source', manifest, variables)
            saved = workspace / "source" / "stacks" / "kvm-instance" / "saved.tfplan"
            if (manifest["sha256"] != sha256 or digest_file(saved) != sha256 or manifest["state_key"] != state_key
                    or manifest["source_revision"] != revision
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
        saved = self.workspace(instance_id, attempt) / "source/stacks/kvm-instance/saved.tfplan"
        return {"plan_sha256": sha256, "legacy_manifest": "execution_context_salt" not in manifest,
                "state_key": state_key, "workspace": "default", "backend_bucket": manifest["backend"]["bucket"],
                "source_revision": SOURCE_REVISION, "execution_enabled": False,
                "approved_state": approved_state_metadata(saved)}

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
        if variables['instance']['vm_id'] in {104, 29000, 29001, 29010, 29011}:
            # No afecta la observación/reconciliación histórica de estas VM.
            raise PlanError('PROTECTED_RESOURCE')
        self.prepare_apply(instance_id, attempt, sha256, state_key, allocation, variables)
        workspace = self.workspace(instance_id, attempt)
        root = workspace / "source" / "stacks" / "kvm-instance"
        env = self.environment(workspace)
        if variables["instance"].get("network_mode") == "dhcp":
            require_clone_permissions(env, variables["instance"])
        self.initialize(root, env, cancelled, "apply")
        plan = self.read_json(["show", "-json", "saved.tfplan"], root, env, cancelled, "apply.inspect")
        verify_plan(plan, variables)
        # Revalidar el binario inmediatamente antes de consumir la autorización.
        self.prepare_apply(instance_id, attempt, sha256, state_key, allocation, variables)
        if cancelled():
            raise PlanError("CANCELLED")
        if variables["instance"].get("network_mode") == "dhcp":
            # Relectura efectiva, sin reutilizar salud de admisión ni del plan.
            # Antes de consumir la autorización y de llamar a Terraform apply.
            require_execution_health(env, variables['instance'], cancelled)
        if 'iacp_golden_hardware' in variables:
            from .golden_hardware import read as read_golden_hardware
            current=read_golden_hardware(env,Allocation.model_validate(allocation),variables['instance']['cpu_cores']).model_dump()
            if current!=variables['iacp_golden_hardware']:
                raise PlanError('GOLDEN_HARDWARE_CHANGED',phase='preflight.golden_hardware')
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
        state_present, proxmox_present = None, None
        reader = None
        try:
            manifest = json.loads((workspace / "manifest.json").read_text())
            self.verify_artifacts(instance_id, attempt, manifest["sha256"], manifest["state_key"], allocation, variables)
            approved_state = approved_state_metadata(root / "saved.tfplan")
            reader = S3StateReader(manifest["backend"], env)
            snapshot = reader.read(cancelled)
            state = snapshot.state
            resource = state_resource(state, variables)
            state_present = resource is not None
            if (approved_state["lineage"] and state["lineage"] != approved_state["lineage"]
                    or state["serial"] < approved_state["serial"]):
                raise PlanError("APPROVED_STATE_MISMATCH", phase="observe.state")
            if 'iacp_golden_hardware' in variables:
                proxmox=proxmox_resource(env,variables['instance'],cancelled,golden_hardware=variables['iacp_golden_hardware'])
            else:
                proxmox = proxmox_resource(env, variables["instance"], cancelled)
            proxmox_present = proxmox["present"]
            if state_present and proxmox_present:
                # Solo inicializar Terraform después de constatar un objeto existente.
                # Ni la ausencia ni un error de lectura deben crear un state vacío.
                self.initialize(root, env, cancelled, "observe")
                approved = self.read_json(["show", "-json", "saved.tfplan"], root, env, cancelled, "observe.approved_plan")
                verify_plan(approved, variables)
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
                refreshed = managed_resources(values)[0]["values"]
                verify_postconditions(approved, resource, source="persisted_state")
                verify_postconditions(approved, refreshed, source="refreshed_state", persisted=resource)
                expected_power = "running" if variables["instance"]["started"] else "stopped"
                if result["power_state"] != expected_power:
                    postcondition_mismatch("refreshed_state", ("started",), "power_state_mismatch")
                if proxmox["power_state"] != expected_power:
                    postcondition_mismatch("proxmox", ("power_state",), "power_state_mismatch")
                verify_checks(observed)
            # Dos GET actuales, nunca una consulta fijada a una versión antigua.
            # La comparación identifica también rewrites idénticos y ABA versionado.
            latest = reader.read(cancelled, "observe.state_verify")
            consistency = snapshot.verify(latest)
            if not state_present or not proxmox_present:
                code = ("UNMANAGED_RESOURCE" if proxmox_present else
                        "STATE_RESOURCE_MISSING" if state_present else "RESOURCE_ABSENT")
                failure = PlanError(code, phase="observe.compare")
                failure.state_consistency = consistency
                raise failure
            return {**result, "observation": {"state_present": True, "proxmox_present": True,
                    "state_serial": state["serial"], "values_source": "prior_state.values",
                    "state_consistency": consistency, "postconditions_verified": True,
                    "backend": manifest["backend"], "workspace": "default",
                    "instance_id": instance_id, "attempt": attempt, "plan_sha256": manifest["sha256"],
                    "approved_state": approved_state,
                    "resource_address": ADDRESS, "resource_id": str(resource["id"])}}
        except PlanError as failure:
            failure.phase = failure.phase or "observe.inspect"
            failure.state_present, failure.proxmox_present = state_present, proxmox_present
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise PlanError("INVALID_TERRAFORM_OUTPUT", phase="observe.inspect") from None
        finally:
            if reader is not None:
                reader.close()


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


def managed_resources(values):
    resources = []
    def collect(module):
        resources.extend(row for row in module.get("resources", []) if row.get("mode", "managed") == "managed")
        for child in module.get("child_modules", []):
            collect(child)
    collect(values.get("root_module", {}))
    return resources


def postcondition_mismatch(source, path, category):
    # Solo nombres estáticos del contrato; una clave de mapa desconocida puede
    # contener información privada. Nunca incluir valores, máscaras ni checksums
    # de atributos (tampoco cuando after_sensitive falta o es incorrecto).
    attributes = {
        "cpu", "cores", "sockets", "type", "flags", "affinity", "architecture", "numa", "units", "limit",
        "memory", "dedicated", "floating", "shared", "hugepages", "keep_hugepages",
        "disk", "interface", "size", "datastore_id", "file_format", "file_id", "import_from", "serial",
        "aio", "cache", "discard", "backup", "replicate", "iothread", "ssd", "path_in_datastore",
        "initialization", "user_account", "username", "password", "keys", "ip_config", "ipv4", "ipv6",
        "address", "gateway", "network_device", "bridge", "vlan_id", "model", "disconnected", "trunks",
        "mac_address", "mtu", "queues", "firewall", "enabled", "agent", "pool_id", "started", "on_boot",
        "vm_id", "id", "node_name", "name", "clone", "description", "tags", "power_state",
        "checks", "instances", "status",
    }
    safe_path = ""
    for part in path:
        safe_path += (f"[{part}]" if type(part) is int else
                      ("." if safe_path else "") + (part if part in attributes else "<attribute>"))
    failure = PlanError("APPROVED_POSTCONDITION_MISMATCH", phase="observe.postconditions")
    failure.postcondition = {"source": source, "path": safe_path or "<resource>", "category": category}
    raise failure


def bpg_empty_default(path, found, desired=None):
    """Equivalencias direccionales null -> valor vacío de bpg 0.116.1.

    No se omite ningún atributo. Valores no vacíos y otras rutas siguen sujetos
    a igualdad estricta. Fundamento por atributo: docs/SEMANTIC_POSTCONDITIONS.md.
    """
    shape = tuple("*" if type(part) is int else part for part in path)
    match shape:
        case ("initialization", "*", "ip_config", "*", "ipv4", "*", "gateway"):
            try:
                ipv4 = desired["initialization"][path[1]]["ip_config"][path[3]]["ipv4"][path[5]]
            except (KeyError, IndexError, TypeError):
                return False
            # bpg 0.116.1 read flattens a missing GatewayIPv4 to an empty
            # string. Only DHCP with an explicitly omitted gateway qualifies.
            return type(found) is str and found == "" and ipv4.get("address") == "dhcp" and ipv4.get("gateway") is None
        case (("cpu", "*", "affinity" | "architecture")
              | ("disk", "*", "file_id" | "import_from" | "serial")
              | ("memory", "*", "hugepages") | ("network_device", "*", "trunks") | ("pool_id",)
              | ("initialization", "*", "user_account", "*", "password")):
            return type(found) is str and found == ""
        case ("network_device", "*", "disconnected"):
            return found is False
        case ("cpu", "*", "flags"):
            return isinstance(found, list) and not found
    return False


def verify_postconditions(approved, actual, *, source="persisted_state", persisted=None):
    """Verificar valores conocidos, incluidos sensibles, con semántica acotada.

    after_unknown solo exime su subárbol. after_sensitive nunca exime una
    comparación. Ausencia de claves y listas reordenadas no son equivalentes.
    """
    resource = approved["resource_changes"][0]
    change = resource["change"]
    provider = approved.get("configuration", {}).get("provider_config", {}).get("proxmox", {})
    bpg_contract = (resource.get("provider_name") == "registry.terraform.io/bpg/proxmox"
                    and provider.get("full_name") == "registry.terraform.io/bpg/proxmox"
                    and provider.get("version_constraint") in {"0.116.1", "= 0.116.1"})

    def mask_child(mask, key):
        if mask is True:
            return True
        if isinstance(mask, dict):
            return mask.get(key, False)
        if isinstance(mask, list) and type(key) is int and key < len(mask):
            return mask[key]
        return False

    def compare(expected, found, unknown=False, sensitive=False, path=()):
        if unknown is True:
            return
        def fail(category):
            postcondition_mismatch(source, path, "sensitive_value_mismatch" if sensitive is True else category)

        if (len(path) == 3 and path[0] == "initialization" and path[2] == "interface"
                and (expected is None or expected == "")):
            # Vacío significa selección automática, no ausencia de unidad.
            if not bpg_contract:
                fail("provider_contract_unverified")
            if not isinstance(found, str) or not re.fullmatch(r"ide[0-3]|sata[0-5]|scsi(?:30|[12][0-9]|[0-9])", found):
                fail("auto_interface_invalid")
            if persisted is not None:
                baseline = persisted
                for part in path:
                    baseline = baseline[part]
                if baseline != found:
                    fail("auto_interface_changed")
            return
        if expected is None and bpg_empty_default(path, found, change["after"]):
            if not bpg_contract:
                fail("provider_contract_unverified")
            return
        if isinstance(expected, dict):
            if not isinstance(found, dict):
                fail("type_mismatch")
            for key, value in expected.items():
                child_unknown = mask_child(unknown, key)
                if child_unknown is True:
                    continue
                if key not in found:
                    postcondition_mismatch(source, path + (key,), "missing_attribute")
                compare(value, found[key], child_unknown, mask_child(sensitive, key), path + (key,))
        elif isinstance(expected, list):
            if not isinstance(found, list):
                fail("type_mismatch")
            if len(expected) != len(found):
                fail("collection_length_mismatch")
            for index, value in enumerate(expected):
                compare(value, found[index], mask_child(unknown, index), mask_child(sensitive, index), path + (index,))
        elif expected != found or (type(expected) is bool) != (type(found) is bool):
            fail("value_mismatch")

    compare(change["after"], actual, change.get("after_unknown", False), change.get("after_sensitive", False))


def verify_checks(observed):
    # Un refresh completo puede contener checks fallidos sin cambios de recursos.
    for index, check in enumerate(observed.get("checks", [])):
        if check.get("status") != "pass":
            postcondition_mismatch("checks", ("checks", index, "status"), "check_not_passed")
        for item, row in enumerate(check.get("instances", [])):
            if row.get("status") != "pass":
                postcondition_mismatch("checks", ("checks", index, "instances", item, "status"), "check_not_passed")


def observed_resource(values: dict, variables: dict) -> dict:
    resources = managed_resources(values)
    if not resources:
        raise PlanError("RESOURCE_ABSENT")
    if len(resources) != 1 or resources[0].get("address") != ADDRESS:
        raise PlanError("UNOWNED_STATE")
    actual = resources[0].get("values", {})
    expected = variables["instance"]
    verify_identity(actual, expected)
    verify_network(actual,expected)
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
        network={}
        if expected.get('network_mode')=='dhcp':
            nic=actual['network_device'][0]
            network={'network':{'mode':'dhcp','bridge':nic['bridge'],'model':nic['model'],
                       'vlan_id':expected['vlan_id'],'mac_address':nic.get('mac_address'),'ip_addresses':[]}}
        return {**network,"simulated": False, "infrastructure_kind": "kvm", "vmid": actual["vm_id"],
                "node": actual["node_name"], "name": actual["name"],
                "vcpus": actual["cpu"][0]["cores"], "memory_mb": actual["memory"][0]["dedicated"],
                "disk_gb": disk["size"], "datastore": disk["datastore_id"],
                "power_state": "running" if actual["started"] else "stopped"}
    except (KeyError, TypeError, IndexError, StopIteration, ValueError):
        raise PlanError("OBSERVED_RESOURCE_MISMATCH") from None
