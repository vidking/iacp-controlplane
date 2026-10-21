"""Subprocesos acotados; nunca aceptar comandos o argumentos del cliente HTTP."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from .config import Cancelled, PlanError


class CommandRunner:
    def __init__(self, binary=("terraform",), timeout=300):
        self.binary = tuple(binary)
        self.timeout = timeout

    def run(self, args, cwd: Path, env: dict, cancelled, allowed=(0,)) -> bytes:
        if not args or args[0] not in {"version", "init", "validate", "plan", "show"}:
            raise PlanError("COMMAND_FORBIDDEN")
        if any(value.startswith(("-destroy", "-lock=false", "-target", "-replace", "-migrate-state", "-force")) for value in args):
            raise PlanError("COMMAND_FORBIDDEN")
        if cancelled():
            raise Cancelled()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
            try:
                process = subprocess.Popen([*self.binary, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=diagnostics, start_new_session=True)
            except OSError:
                raise PlanError("TERRAFORM_UNAVAILABLE") from None
            try:
                deadline = time.monotonic() + self.timeout
                while process.poll() is None:
                    if cancelled():
                        raise Cancelled()
                    if time.monotonic() >= deadline:
                        raise PlanError("TERRAFORM_TIMEOUT")
                    if os.fstat(output.fileno()).st_size + os.fstat(diagnostics.fileno()).st_size > 16 * 1024 * 1024:
                        raise PlanError("OUTPUT_LIMIT_EXCEEDED")
                    time.sleep(0.1)
                if cancelled():
                    raise Cancelled()
                if process.returncode not in allowed:
                    raise PlanError("TERRAFORM_COMMAND_FAILED")
                if os.fstat(output.fileno()).st_size + os.fstat(diagnostics.fileno()).st_size > 16 * 1024 * 1024:
                    raise PlanError("OUTPUT_LIMIT_EXCEEDED")
                output.seek(0)
                data = output.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise PlanError("OUTPUT_LIMIT_EXCEEDED")
                return data
            finally:
                # Un worker interrumpido no debe dejar un proceso hijo planificando.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
