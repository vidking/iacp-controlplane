"""Subprocesos acotados; nunca aceptar comandos o argumentos del cliente HTTP."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .config import Cancelled, PlanError


class CommandRunner:
    def __init__(self, binary=("terraform",), timeout=300):
        self.binary = tuple(binary)
        self.timeout = timeout

    def apply_saved(self, cwd: Path, env: dict, cancelled) -> bytes:
        # Ruta interna de argumentos fijos; run() sigue rechazando apply del llamador.
        if os.environ.get("IACP_TERRAFORM_APPLY_ENABLED") != "1":
            raise PlanError("APPLY_DISABLED")
        return self._execute(["apply", "-input=false", "-no-color", "-lock-timeout=30s", "saved.tfplan"],
                             cwd, env, cancelled, (0,), "apply")

    @staticmethod
    def deletion_enabled():
        if (os.environ.get("IACP_TERRAFORM_DELETE_ENABLED") != "1"
                or os.environ.get("IACP_TERRAFORM_APPLY_ENABLED") != "1"):
            raise PlanError("DELETE_DISABLED")

    def plan_destroy(self, cwd: Path, env: dict, cancelled, *, refresh=True) -> bytes:
        self.deletion_enabled()
        args = ["plan", "-destroy", "-input=false", "-no-color", "-lock-timeout=30s"]
        if not refresh:
            args.append("-refresh=false")
        args.extend(["-detailed-exitcode", "-out=deletion.tfplan"])
        return self._execute(args, cwd, env, cancelled, (0, 2), "delete.plan")

    def apply_destroy(self, cwd: Path, env: dict, cancelled) -> bytes:
        self.deletion_enabled()
        return self._execute(["apply", "-input=false", "-no-color", "-lock-timeout=30s", "deletion.tfplan"],
                             cwd, env, cancelled, (0,), "delete.apply")

    def destroy(self, cwd: Path, env: dict, cancelled, *, refresh=False) -> bytes:
        """Ejecuta el destroy interno solo para recuperación tainted.

        Terraform 1.16.6 puede invalidar un plan guardado al refrescar el
        state durante ``apply`` cuando el recurso fallido quedó tainted. La
        recuperación ya inspeccionó un plan exclusivo de ``delete`` y selló
        identidad, state y ausencia de escritores; en ese caso el comando
        fijo vuelve a planificar y destruye sin refresh para no adoptar ni
        reemplazar el recurso.
        """
        self.deletion_enabled()
        args = ["destroy", "-auto-approve", "-input=false", "-no-color", "-lock-timeout=30s"]
        if not refresh:
            args.append("-refresh=false")
        return self._execute(args, cwd, env, cancelled, (0,), "delete.destroy")

    def run(self, args, cwd: Path, env: dict, cancelled, allowed=(0,), phase=None) -> bytes:
        if args != ["state", "pull"] and (not args or args[0] not in {"version", "init", "validate", "plan", "show"}):
            raise PlanError("COMMAND_FORBIDDEN")
        if any(value.startswith(("-destroy", "-lock=false", "-target", "-replace", "-migrate-state", "-force")) for value in args):
            raise PlanError("COMMAND_FORBIDDEN")
        return self._execute(args, cwd, env, cancelled, allowed, phase)

    def _execute(self, args, cwd, env, cancelled, allowed, phase=None):
        if cancelled():
            raise Cancelled()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
            lease_read, lease_write = os.pipe()
            try:
                status_read, status_write = os.pipe()
            except BaseException:
                os.close(lease_read)
                os.close(lease_write)
                raise
            process = None
            try:
                try:
                    # The helper detects worker SIGKILL through EOF. A file path
                    # works with Terraform's intentionally minimal environment.
                    helper = str(Path(__file__).with_name('process_supervisor.py'))
                    process = subprocess.Popen([sys.executable, helper, str(lease_read), str(status_write), *self.binary, *args],
                                               cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=diagnostics, start_new_session=True,
                                               pass_fds=(lease_read, status_write))
                except OSError:
                    raise PlanError("TERRAFORM_UNAVAILABLE", phase=phase, command=args[0]) from None
                finally:
                    os.close(lease_read)
                    os.close(status_write)
                deadline = time.monotonic() + self.timeout
                while process.poll() is None:
                    if cancelled():
                        raise Cancelled()
                    if time.monotonic() >= deadline:
                        raise PlanError("TERRAFORM_TIMEOUT", phase=phase, command=args[0])
                    if os.fstat(output.fileno()).st_size + os.fstat(diagnostics.fileno()).st_size > 16 * 1024 * 1024:
                        raise PlanError("OUTPUT_LIMIT_EXCEEDED", phase=phase, command=args[0])
                    time.sleep(0.1)
                if cancelled():
                    raise Cancelled()
                status = os.read(status_read, 32)
                if status == b'unavailable':
                    raise PlanError("TERRAFORM_UNAVAILABLE", phase=phase, command=args[0])
                digits = status[1:] if status.startswith(b'-') else status
                if not digits.isdigit():
                    # The helper's exit code is not Terraform's status. In
                    # particular, helper exit=2 must not satisfy plan allowed=(0,2).
                    raise PlanError("TERRAFORM_COMMAND_FAILED", phase=phase,
                                    command=args[0], exit_code=process.returncode)
                returncode = int(status)
                if returncode not in allowed:
                    raise PlanError("TERRAFORM_COMMAND_FAILED", phase=phase,
                                    command=args[0], exit_code=returncode)
                if os.fstat(output.fileno()).st_size + os.fstat(diagnostics.fileno()).st_size > 16 * 1024 * 1024:
                    raise PlanError("OUTPUT_LIMIT_EXCEEDED", phase=phase, command=args[0])
                output.seek(0)
                data = output.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise PlanError("OUTPUT_LIMIT_EXCEEDED", phase=phase, command=args[0])
                return data
            finally:
                os.close(lease_write)
                os.close(status_read)
                # Also covers soft-timeout/BaseException during Popen startup,
                # including a helper created before Popen returned to this frame.
                if process is not None:
                    self._stop_helper(process)

    @staticmethod
    def _stop_helper(process):
        # The helper handles TERM and stops Terraform/provider descendants.
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
