"""Explicit commercial projections; persisted audit and worker data stay intact."""
import re


def public_executor(value):
    return "provisioning" if value == "terraform" else value


def public_code(value):
    if value is None:
        return None
    return re.sub(r"terraform|opentofu|(?<![a-z])tofu(?![a-z])", lambda m: (
        "PROVISIONING" if m.group().isupper() else "provisioning"), value, flags=re.I)


def public_result(value):
    if value is None:
        return None
    # Keep the consumer's resource snapshot, never execution manifests, addresses,
    # source revisions, backend locations, state versions or CLI output.
    keys = {"simulated", "infrastructure_kind", "vmid", "node", "name", "vcpus",
            "memory_mb", "disk_gb", "datastore", "power_state", "phase", "observed_at",
            "network", "approval_required", "apply_enabled", "reserved_vmid", "plan_sha256", "error_code"}
    result = {key: item for key, item in value.items() if key in keys}
    if isinstance(value.get("observation"), dict):
        result["observation"] = {key: item for key, item in value["observation"].items()
                                 if key in {"state_present", "proxmox_present", "postconditions_verified"}}
    if "error_code" in result:
        result["error_code"] = public_code(result["error_code"])
    return result


def public_message(value):
    # Job messages are internal free text, not a supported diagnostic channel.
    if re.search(r"terraform|opentofu|\btofu\b|provider|backend", value, re.I):
        return "Consulte la operación y sus eventos de provisioning."
    return value
