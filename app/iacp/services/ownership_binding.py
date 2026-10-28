"""Huella técnica estable de un recurso aprovisionado.

Este módulo no importa modelos ni políticas.  Se comparte con la migración de
datos para que la evidencia creada durante la adopción sea exactamente la que
se valida en las operaciones posteriores.
"""

import hashlib
import json


def _result_identity(result):
    result = result if isinstance(result, dict) else {}
    observation = result.get("observation")
    observation = observation if isinstance(observation, dict) else {}
    return {
        key: result.get(key)
        for key in ("simulated", "vmid", "node", "name")
    } | {
        "observation": {
            key: observation.get(key)
            for key in ("postconditions_verified", "state_present", "proxmox_present")
        }
    }


def binding_payload(instance, execution, apply):
    """Return only immutable, non-secret provisioning identity facts."""

    return {
        "instance": {
            key: getattr(instance, key, None)
            for key in (
                "id", "tenant_id", "name", "image", "vcpus", "memory_mb",
                "disk_gb", "network_profile", "node_preference", "start_on_create",
            )
        },
        "execution": {
            "state_key": execution.state_key,
            "vmid": execution.vmid,
            "allocation": execution.allocation or {},
            "source_revision": execution.source_revision,
        },
        "apply": {
            "id": apply.id,
            "attempt": apply.attempt,
            "plan_sha256": apply.plan_sha256,
            "result": _result_identity(apply.result),
        },
    }


def binding_digest(instance, execution, apply):
    value = json.dumps(binding_payload(instance, execution, apply), sort_keys=True,
                       separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_source_revision(value):
    return isinstance(value, str) and len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )

