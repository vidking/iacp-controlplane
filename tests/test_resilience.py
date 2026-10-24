"""Phase C: bounded failures with dummy infrastructure and real OS processes."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from iacp.models import Instance, Job, TerraformApply, TerraformExecution
from iacp.terraform.apply import execute_apply
from iacp.terraform.actions import retry
from iacp.services.instances import create_instance
from iacp.models import BOOTSTRAP_TENANT_ID
from iacp.schemas import InstanceCreate
from iacp.security import Principal
from tests.test_apply import ApplyingExecutor, approval, dispatch, prepared
from tests.test_terraform import terraform_mode
from tests.test_terraform import FakeExecutor, run_plan


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return predicate()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux runtime process supervision')
def test_c02_killed_worker_cannot_leave_terraform_or_provider_running(tmp_path):
    marker = tmp_path / 'processes'
    # A fake Terraform starts a fake provider; both are harmless sleeping processes.
    fake = ("import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"open({str(marker)!r},'w').write(str(os.getpid())+' '+str(child.pid)); "
            "time.sleep(30)")
    parent_code = ("import os,sys; from pathlib import Path; "
                   "from iacp.terraform.runner import CommandRunner; "
                   f"CommandRunner((sys.executable,'-c',{fake!r})).run(['plan'],Path({str(tmp_path)!r}),"
                   "dict(os.environ),lambda:False)")
    parent = subprocess.Popen([sys.executable, '-c', parent_code],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        assert wait_for(marker.exists), 'Synthetic Terraform did not start'
        assert wait_for(lambda: len(marker.read_text().split()) == 2)
        pids = [int(value) for value in marker.read_text().split()]
        assert all(alive(pid) for pid in pids)
        parent.kill()
        parent.wait(timeout=5)
        assert wait_for(lambda: all(not alive(pid) for pid in pids)), \
            'Worker SIGKILL left an active Terraform/provider process'
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class WorkerKilled(BaseException):
    """Abrupt exit bypasses ordinary task exception handlers."""


def test_c01_commit_publish_gap_keeps_intent_and_manual_retry_fences_late_delivery(
        client, auth, sessions, terraform_mode):
    def killed_before_publish(job_id):
        raise WorkerKilled()

    with sessions() as s:
        with pytest.raises(WorkerKilled):
            create_instance(s, Principal(BOOTSTRAP_TENANT_ID, 'bootstrap', 'operator'),
                            InstanceCreate(name='phase-c-unpublished', image='test-image'),
                            'phase-c-commit-gap', killed_before_publish)
    from sqlalchemy import select
    with sessions() as s:
        execution = s.scalar(select(TerraformExecution))
        assert execution.status == 'queued'
        identifier, key = execution.instance_id, execution.state_key
        accepted = {'instance_id': identifier, 'job_id': execution.job_id}
        retried = retry(s, execution, 1, 'phase-c-controlled-retry', 'bootstrap', lambda *args: None)
        assert retried['instance_id'] == identifier and retried['attempt'] == 2
    late = run_plan(accepted, sessions, FakeExecutor(), attempt=1)
    assert not late.calls
    current = run_plan(accepted, sessions, FakeExecutor(), attempt=2)
    assert len(current.calls) == 1
    with sessions() as s:
        execution = s.get(TerraformExecution, identifier)
        assert execution.state_key == key and execution.status == 'planned'


def test_c02_supervisor_refuses_to_spawn_after_owner_already_died(tmp_path):
    marker = tmp_path / 'must-not-exist'
    lease_read, lease_write = os.pipe()
    status_read, status_write = os.pipe()
    os.close(lease_write)
    command = [sys.executable, '-c', f'open({str(marker)!r},"w").write("mutation")']
    # signal handlers are process-global; exercise the helper in a separate process.
    helper = str(Path(__file__).parents[1] / 'app/iacp/terraform/process_supervisor.py')
    try:
        result = subprocess.run([sys.executable, helper, str(lease_read), str(status_write), *command],
                                pass_fds=(lease_read, status_write), timeout=5, capture_output=True)
        os.close(status_write)
        assert os.read(status_read, 32) == b'125' and not marker.exists()
        assert result.returncode == 0
    finally:
        os.close(lease_read)
        os.close(status_read)


@pytest.mark.parametrize('kind', ['missing', 'not-executable'])
def test_supervised_launch_keeps_unavailable_error(tmp_path, kind):
    from iacp.terraform.runner import CommandRunner
    from iacp.terraform.config import PlanError
    executable = tmp_path / 'dummy'
    if kind == 'not-executable':
        executable.write_text('dummy')
        executable.chmod(0o600)
    with pytest.raises(PlanError, match='TERRAFORM_UNAVAILABLE') as failed:
        CommandRunner((str(executable),)).run(['show'], tmp_path, {}, lambda: False, phase='fixture')
    assert failed.value.phase == 'fixture' and failed.value.command == 'show'


def test_supervised_signal_exit_preserves_negative_diagnostic(tmp_path):
    from iacp.terraform.runner import CommandRunner
    from iacp.terraform.config import PlanError
    with pytest.raises(PlanError) as failed:
        CommandRunner((sys.executable, '-c', 'import os,signal; os.kill(os.getpid(),signal.SIGTERM)')).run(
            ['show'], tmp_path, {}, lambda: False)
    assert failed.value.exit_code == -signal.SIGTERM


def test_startup_exception_closes_lease_and_stops_spawned_child(tmp_path, monkeypatch):
    from iacp.terraform.runner import CommandRunner
    marker = tmp_path / 'started'
    real_popen = subprocess.Popen
    launched = []
    fd_count = len(os.listdir('/proc/self/fd'))

    def interrupted_start(*args, **kwargs):
        launched.append(real_popen(*args, **kwargs))
        assert wait_for(lambda: marker.exists() and marker.read_text().isdigit())
        raise WorkerKilled()

    monkeypatch.setattr(subprocess, 'Popen', interrupted_start)
    try:
        with pytest.raises(WorkerKilled):
            CommandRunner((sys.executable, '-c',
                f'import os,time; open({str(marker)!r},"w").write(str(os.getpid())); time.sleep(30)')).run(
                    ['plan'], tmp_path, dict(os.environ), lambda: False)
        pid = int(marker.read_text())
        assert wait_for(lambda: not alive(pid))
        assert len(os.listdir('/proc/self/fd')) <= fd_count
    finally:
        for child in launched:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def test_second_pipe_failure_closes_first_pipe(monkeypatch, tmp_path):
    import errno
    from iacp.terraform.runner import CommandRunner
    real_pipe, leases = os.pipe, []

    def pipe():
        if leases:
            raise OSError(errno.EMFILE, 'synthetic descriptor limit')
        fds = real_pipe()
        leases.extend(fds)
        return fds

    monkeypatch.setattr(os, 'pipe', pipe)
    with pytest.raises(OSError):
        CommandRunner().run(['show'], tmp_path, {}, lambda: False)
    for fd in leases:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


def test_missing_helper_status_never_satisfies_plan_exit_two(tmp_path, monkeypatch):
    import iacp.terraform.runner as runner
    from iacp.terraform.config import PlanError
    (tmp_path / 'process_supervisor.py').write_text('raise SystemExit(2)\n')
    monkeypatch.setattr(runner, '__file__', str(tmp_path / 'runner.py'))
    with pytest.raises(PlanError, match='TERRAFORM_COMMAND_FAILED'):
        runner.CommandRunner().run(['plan'], tmp_path, {}, lambda: False, allowed=(0, 2))


def test_c02_mutation_fence_survives_interruption_and_recovery_is_readonly(
        client, auth, prepared, sessions, monkeypatch):
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED', '1')
    row = approval(client, prepared).json()
    assert dispatch(client, prepared, auth, row).status_code == 202
    mutations = []

    def killed_after_clone():
        mutations.append(prepared[0]['instance_id'])
        raise WorkerKilled()

    executor = ApplyingExecutor(callback=killed_after_clone)
    with pytest.raises(WorkerKilled):
        execute_apply(row['approval_id'], sessions, executor)
    with sessions() as s:
        current = s.get(TerraformApply, row['approval_id'])
        assert current.status == 'running' and current.started_at is not None
        binding = s.get(TerraformExecution, current.instance_id)
        original = binding.vmid, binding.state_key, binding.attempt
        assert s.get(Job, current.job_id).status == 'running'
    execute_apply(row['approval_id'], sessions, executor)
    assert len(mutations) == 1
    observer = ApplyingExecutor()
    execute_apply(row['approval_id'], sessions, observer, reconcile=True)
    assert not observer.calls
    with sessions() as s:
        current = s.get(TerraformApply, row['approval_id'])
        binding = s.get(TerraformExecution, current.instance_id)
        assert (binding.vmid, binding.state_key, binding.attempt) == original
        assert current.status == 'completed'
        assert s.get(Instance, current.instance_id).status == 'ready'
    assert len(mutations) == 1
