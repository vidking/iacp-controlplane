"""Keep the Terraform process group bounded even if the Celery child is killed.

The caller holds the only write end of a private pipe. EOF means ownership was
lost, including SIGKILL/OOM: stop Terraform and its provider group. This helper
never reads credentials, interprets plans, retries, or removes backend locks.
"""
import os
import select
import signal
import subprocess
import sys


def supervise(lease_fd, command):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def owner_lost():
        ready, _, _ = select.select([lease_fd], [], [], .1)
        return bool(ready) and os.read(lease_fd, 1) == b''

    # Do not start infrastructure work if the owner died during startup.
    if owner_lost() or stopping:
        os.close(lease_fd)
        return 125
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        while process.poll() is None:
            if stopping or owner_lost():
                return 125
        return process.returncode
    finally:
        os.close(lease_fd)
        if process is not None:
            # A provider may outlive its Terraform leader. Always signal the group.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            process.wait(timeout=2)


if __name__ == '__main__':
    status_fd = int(sys.argv[2])
    try:
        try:
            result = str(supervise(int(sys.argv[1]), sys.argv[3:])).encode('ascii')
        except OSError:
            # Never serialize paths, argv, environment or exception text.
            result = b'unavailable'
        os.write(status_fd, result)
    finally:
        os.close(status_fd)
