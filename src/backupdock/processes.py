from __future__ import annotations

import signal
import subprocess
from contextlib import contextmanager


INTERRUPTION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def protected_cleanup():
    """Let cleanup finish even if another interruption arrives."""
    previous = {signum: signal.getsignal(signum) for signum in INTERRUPTION_SIGNALS}
    try:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def terminate_process(process: subprocess.Popen) -> None:
    """Terminate and reap a child before releasing backup resources."""
    with protected_cleanup():
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run_process(command, *, input=None, capture_output=False, check=False, **kwargs):
    """Stream normal output and explicitly stop children on every exception."""
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    # Only BackupDock receives terminal signals; it controls child cleanup.
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(input)
    except BaseException:
        terminate_process(process)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
