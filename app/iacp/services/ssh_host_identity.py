"""Lectura autenticada de la clave pública SSH del invitado.

AlmaLinux confina qemu-ga en ``virt_qemu_ga_t`` y no debe recibir acceso
general a ``/etc/ssh``. Las imágenes Alma preparadas para IACP publican la
clave pública Ed25519 en un archivo dedicado, de solo lectura para qemu-ga.
Ubuntu conserva por compatibilidad el canal histórico hasta que su plantilla
también adopte el canal dedicado.
"""
import base64
import binascii
from dataclasses import dataclass
from urllib.parse import urlencode


ALMALINUX_IMAGES = frozenset({"almalinux-9", "almalinux-10"})
DEDICATED_HOST_KEY_PATH = "/var/lib/iacp/host-identity/ssh_host_ed25519_key.pub"
LEGACY_HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key.pub"


class HostIdentityError(ValueError):
    """La identidad pública del host no puede verificarse de forma segura."""


@dataclass(frozen=True)
class HostKeyEvidence:
    path: str
    line: str
    key: object


def path_for_image(image_id: str | None) -> str:
    return DEDICATED_HOST_KEY_PATH if image_id in ALMALINUX_IMAGES else LEGACY_HOST_KEY_PATH


def _parse_public_key(content: str, path: str) -> HostKeyEvidence:
    import paramiko

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1:
        raise HostIdentityError("BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID")
    fields = lines[0].split()
    if len(fields) not in {2, 3} or fields[0] != "ssh-ed25519":
        raise HostIdentityError("BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID")
    try:
        raw = base64.b64decode(fields[1], validate=True)
        key = paramiko.Ed25519Key(data=raw)
    except (ValueError, TypeError, binascii.Error, paramiko.SSHException):
        raise HostIdentityError("BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID") from None
    return HostKeyEvidence(path=path, line=" ".join(fields), key=key)


def read(client, target, image_id: str | None) -> HostKeyEvidence:
    """Lee solo la ruta autorizada y valida que sea una clave Ed25519."""
    path = path_for_image(image_id)
    try:
        result = client._request("GET", target.path + "/agent/file-read?" + urlencode({
            "file": path,
            "count": 4096,
        }))
        if result.get("truncated") or not isinstance(result.get("content"), str):
            raise HostIdentityError("BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID")
        return _parse_public_key(result["content"], path)
    except HostIdentityError:
        raise
    except Exception:
        raise HostIdentityError("BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE") from None
