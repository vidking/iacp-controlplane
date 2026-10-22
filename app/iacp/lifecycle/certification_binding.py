"""Stable binding to original provisioning facts; no historical row is rewritten."""
import hashlib
import json


def binding_digest(instance, execution, approval):
    payload = {
        "instance": {key: getattr(instance, key) for key in (
            "id", "tenant_id", "name", "image", "vcpus", "memory_mb", "disk_gb", "executor", "status", "vmid", "node", "result")},
        "execution": {key: getattr(execution, key) for key in (
            "instance_id", "operation_id", "job_id", "status", "attempt", "state_key", "vmid", "allocation", "source_revision", "plan_sha256")},
        "approval": {key: getattr(approval, key) for key in (
            "id", "instance_id", "attempt", "plan_sha256", "status", "result", "approved_by", "requested_by", "request_hash")},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
