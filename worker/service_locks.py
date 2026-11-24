"""Locks compartidos para ejecución y recuperación de trabajos de servicio."""

from contextlib import contextmanager
import contextvars
import fcntl
import hashlib
import os
from pathlib import Path


_CURRENT_LOCK_FD: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "iacp_service_job_lock_fd", default=None
)


def service_job_lock_path(job_id: str) -> Path:
    root = Path(os.getenv("IACP_WORKDATA_DIR", "/var/lib/iacp-controlplane"))
    digest = hashlib.sha256(job_id.encode()).hexdigest()
    directory = root / "service-job-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / f"{digest}.lock"


def try_acquire_service_job_lock(job_id: str) -> int | None:
    descriptor = os.open(
        service_job_lock_path(job_id),
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def release_service_job_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def service_job_execution_lock(job_id: str):
    descriptor = os.open(
        service_job_lock_path(job_id),
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    token = _CURRENT_LOCK_FD.set(descriptor)
    try:
        yield
    finally:
        _CURRENT_LOCK_FD.reset(token)
        release_service_job_lock(descriptor)


def current_service_job_lock_fd() -> int | None:
    return _CURRENT_LOCK_FD.get()
