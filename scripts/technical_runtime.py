"""Comparación acotada de configuración técnica; no contiene reglas comerciales.

La revisión solo cubre rutas de secretos técnicos y el TTL efímero del
bootstrap. El contrato servido es siempre ``provisioning-api-core``.
"""

from pathlib import PurePosixPath


def normalize_technical_runtime(current, candidate, review):
    """Normaliza únicamente los campos técnicos explícitamente revisados."""
    if review is None:
        return []
    expected = {"aes_key_path", "ssh_key_path", "ttl_seconds", "admission_credentials_path"}
    if set(review) != expected:
        raise ValueError("Revisión técnica incompleta o con campos no autorizados")
    ttl = review["ttl_seconds"]
    if type(ttl) is not int or not 60 <= ttl <= 3600:
        raise ValueError("TTL técnico inválido")
    paths = {
        "bootstrap_aes_key": review["aes_key_path"],
        "guest_bootstrap_ssh_key": review["ssh_key_path"],
        "admission_credentials": review["admission_credentials_path"],
    }
    for path in paths.values():
        if (not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or path == "/dev/null"):
            raise ValueError("Referencia absoluta de secreto técnico requerida")
    changed = []

    def field(old, new, key, value, prefix):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        if new.get(key) != value:
            return
        if key in old:
            if old[key] != value:
                new[key] = value
                changed.append(prefix + "/" + key)
        else:
            new.pop(key)
            changed.append(prefix + "/" + key)

    for name, path in paths.items():
        old = current.get("secrets", {})
        new = candidate.get("secrets", {})
        if name in old and old[name].get("file") not in {path}:
            raise ValueError("Rotación de secreto técnico fuera de alcance")
        field(old, new, name, {"name": "iacp-controlplane_" + name, "file": path}, "/secrets")

    settings = {
        "api": {
            "IACP_ADMISSION_CREDENTIALS_FILE": "/run/secrets/admission_credentials",
            "IACP_BOOTSTRAP_AES_KEY_FILE": "/run/secrets/bootstrap_aes_key",
            "IACP_BOOTSTRAP_SECRET_TTL_SECONDS": str(ttl),
        },
        "terraform-worker": {
            "IACP_BOOTSTRAP_AES_KEY_FILE": "/run/secrets/bootstrap_aes_key",
            "IACP_BOOTSTRAP_SECRET_TTL_SECONDS": str(ttl),
            "IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE": "/run/secrets/guest_bootstrap_ssh_key",
        },
    }
    mounts = {
        "api": ["bootstrap_aes_key", "admission_credentials"],
        "terraform-worker": ["bootstrap_aes_key", "guest_bootstrap_ssh_key"],
    }
    for role, values in settings.items():
        old_service, new_service = current["services"][role], candidate["services"][role]
        for key, value in values.items():
            field(old_service.get("environment", {}), new_service.get("environment", {}),
                  key, value, "/services/" + role + "/environment")
        for name in mounts[role]:
            mount = {"source": name, "target": "/run/secrets/" + name}
            before, after = old_service.get("secrets", []), new_service.get("secrets", [])
            if (isinstance(before, list) and isinstance(after, list)
                    and all(isinstance(s, dict) for s in before + after)
                    and mount in after
                    and not any(s.get("source") == name for s in before)):
                after.remove(mount)
                changed.append("/services/" + role + "/secrets/" + name)
    return changed
