import os
from pathlib import Path


def secret_root() -> Path:
    workdata = os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane")
    root = Path(workdata) / "operation-secrets"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def secret_path(operation_id: str) -> Path:
    return secret_root() / f"{operation_id}.root_password"


def write_secret(operation_id: str, value: str) -> Path:
    path = secret_path(operation_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def read_secret(path: str | None) -> str | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read().rstrip("\n")


def delete_secret(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
