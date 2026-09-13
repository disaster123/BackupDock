from __future__ import annotations

import contextlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.backup import BackupOrchestrator
from backupdock import __version__
from backupdock.cli import BackupInterrupted, _signal_handler, main
from backupdock.config import AppConfig, BackupConfig, RemoteConfig
from backupdock.processes import terminate_process
from backupdock.restic import ResticRunner
from test_backup import FakeDocker, FakeRestic, make_container
from backupdock.models import BackupGroup, BackupSource
from backupdock.locking import ProcessLock
from backupdock.remote import REMOTE_PROTOCOL_VERSION


class InterruptionTests(unittest.TestCase):
    def test_remote_signals_are_installed_before_version_check_and_restored(self):
        remote = RemoteConfig(
            ssh_target="source", password_file=Path("/secret"),
            repository_path="source", rest_server_username="source",
            rest_server_password_file=Path("/rest-secret"),
        )
        config = AppConfig(remotes={"source": remote})
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=signum):
                previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}

                def interrupt(*args, **kwargs):
                    self.assertIs(signal.getsignal(signum), _signal_handler)
                    os.kill(os.getpid(), signum)

                stderr = io.StringIO()
                with (
                    patch("backupdock.cli.load_config", return_value=config),
                    patch("backupdock.remote.RemoteBackupController._check_remote_version", side_effect=interrupt),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(stderr),
                ):
                    result = main(["remote-backup", "source"])
                self.assertEqual(result, 128 + signum)
                self.assertIn(signal.Signals(signum).name, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertEqual({s: signal.getsignal(s) for s in previous}, previous)

    def test_all_signals_restart_containers_after_backup_or_partial_stop(self):
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            for phase in ("stop", "backup", "preseed"):
                with self.subTest(signal=signum, phase=phase), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    data = root / "data"
                    data.mkdir()
                    docker = FakeDocker()
                    restic = FakeRestic()
                    interrupted = BackupInterrupted(signum)
                    if phase == "stop":
                        original_stop = docker.stop

                        def stop(container_id):
                            original_stop(container_id)
                            raise interrupted

                        docker.stop = stop
                    else:
                        restic.backup = MagicMock(side_effect=interrupted)
                    group = BackupGroup(
                        key="compose:app", name="app", compose_project="app",
                        containers=[make_container("db", True), make_container("web", True, ("db",)), make_container("disabled", False)],
                        sources=[BackupSource(data, "bind")],
                    )
                    config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(BackupInterrupted) as caught:
                        BackupOrchestrator(docker, restic, config, preseed=phase == "preseed").backup_group(group)
                    self.assertIs(caught.exception, interrupted)
                    expected = [] if phase == "preseed" else ["id-web"] if phase == "stop" else ["id-db", "id-web"]
                    self.assertEqual(docker.started, expected)

    def test_restart_survives_disconnected_output_and_repeated_signals(self):
        class DisconnectedOutput(io.StringIO):
            def write(self, text):
                raise BrokenPipeError("SSH output closed")

        docker = FakeDocker()
        docker.stopped = ["id-web", "id-db"]
        original = docker.ensure_running

        def restart(container_id):
            os.kill(os.getpid(), signal.SIGHUP)
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
            original(container_id)

        docker.ensure_running = restart
        from backupdock.processes import protected_cleanup
        with contextlib.redirect_stdout(DisconnectedOutput()), protected_cleanup():
            errors = BackupOrchestrator(docker, FakeRestic(), AppConfig())._restart(
                "compose:app", [make_container("web", True), make_container("db", True)],
            )
        self.assertEqual(errors, [])
        self.assertEqual(docker.started, ["id-db", "id-web"])

    def test_child_is_killed_and_reaped_if_it_ignores_termination(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
        terminate_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_streaming_restic_reaps_child_and_preserves_original_interruption(self):
        process = MagicMock()
        interrupted = BackupInterrupted(signal.SIGHUP)
        process.stdout.__iter__.side_effect = interrupted
        process.poll.return_value = None
        with (
            patch("backupdock.restic.subprocess.Popen", return_value=process),
            self.assertRaises(BackupInterrupted) as caught,
        ):
            ResticRunner(AppConfig().restic, progress=True).backup([Path("/data")], "compose:app")
        self.assertIs(caught.exception, interrupted)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.stdout.close.assert_called_once()


class RealProcessTests(unittest.TestCase):
    def _cleanup_worker(self, process, child_pid=None):
        if process.poll() is None:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
        process.communicate(timeout=5)

    def _wait_for_output(self, process, marker):
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 10
        output = b""
        try:
            while time.monotonic() < deadline:
                if selector.select(timeout=0.1):
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output += chunk
                    if marker in output:
                        return output
        finally:
            selector.close()
        self.fail("Worker did not reach backup phase: " + output.decode())

    def test_controller_signals_close_the_ssh_child_without_tracebacks(self):
        worker = '''
import sys
from pathlib import Path
from unittest.mock import patch
from backupdock.cli import main
from backupdock.config import AppConfig, RemoteConfig
root = Path(sys.argv[1])
remote = RemoteConfig(ssh_binary=str(root / 'ssh'), ssh_target='source', password_file=root / 'password', repository_path='source', rest_server_username='source', rest_server_password_file=root / 'password')
with patch('backupdock.cli.load_config', return_value=AppConfig(remotes={'source': remote})):
    sys.exit(main(['remote-backup', 'source']))
'''
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "password").write_text("test-password\n")
                ssh = root / "ssh"
                ssh.write_text(
                    "#!" + sys.executable + "\n"
                    "import json,os,sys,time\n"
                    "from pathlib import Path\n"
                    "from backupdock import __version__\n"
                    "from backupdock.remote import REMOTE_PROTOCOL_VERSION\n"
                    "if 'source-info' in sys.argv:\n"
                    " print(json.dumps({'version': __version__, 'protocol': REMOTE_PROTOCOL_VERSION}))\n"
                    "else:\n"
                    " sys.stdin.read()\n"
                    " Path(" + repr(str(root / "ssh.pid")) + ").write_text(str(os.getpid()))\n"
                    " print('ssh ready', flush=True)\n"
                    " time.sleep(60)\n"
                )
                ssh.chmod(0o700)
                process = subprocess.Popen([sys.executable, "-c", worker, str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                child_pid = None
                try:
                    self._wait_for_output(process, b"ssh ready\n")
                    child_pid = int((root / "ssh.pid").read_text())
                    os.kill(process.pid, signum)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + signum, stderr)
                    self.assertIn(signal.Signals(signum).name, stderr)
                    self.assertNotIn("Traceback", stderr)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if child_pid is None and (root / "ssh.pid").exists():
                        child_pid = int((root / "ssh.pid").read_text())
                    self._cleanup_worker(process, child_pid)

    def test_source_signals_and_closed_transport_restore_state_and_remove_credentials(self):
        worker = Path(__file__).parent / "fixtures" / "interruption_source.py"
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, None):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                payload = json.dumps({
                    "version": __version__, "protocol": REMOTE_PROTOCOL_VERSION,
                    "repository_password": "test-password", "rest_server_username": "test-user",
                    "rest_server_password": "test-rest-password",
                    "config_yaml": f"restic:\n  binary: {root / 'restic'}\n  repository: test\nbackup:\n  state_dir: {root / 'state'}\n",
                })
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                for key in ("RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD"):
                    env.pop(key, None)
                process = subprocess.Popen([sys.executable, str(worker), str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
                child_pid = None
                try:
                    process.stdin.write(payload)
                    process.stdin.close()
                    process.stdin = None
                    self._wait_for_output(process, b"restic ready\n")
                    child_pid = int((root / "restic.pid").read_text())
                    if signum is None:
                        process.stdout.close()
                        process.stdout = None
                    else:
                        os.kill(process.pid, signum)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 1 if signum is None else 128 + signum, stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual((root / "events").read_text().splitlines(), ["stop web", "stop db", "start db", "start web", "docker closed", "secrets cleared True"])
                    self.assertEqual(list((root / "runtime").iterdir()), [])
                    with ProcessLock(root / "state" / "backupdock.lock"):
                        pass
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if child_pid is None and (root / "restic.pid").exists():
                        child_pid = int((root / "restic.pid").read_text())
                    self._cleanup_worker(process, child_pid)

    def test_noninteractive_child_is_terminated_on_every_signal(self):
        worker = '''
import os, signal, sys
from backupdock.cli import _install_signal_handlers, BackupInterrupted
from backupdock.processes import run_process
_install_signal_handlers()
try:
    run_process([sys.executable, '-c', 'import os,time; print(os.getpid(), flush=True); time.sleep(60)'], text=True)
except BackupInterrupted as exc:
    sys.exit(128 + exc.signum)
'''
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=signum):
                process = subprocess.Popen([sys.executable, "-c", worker], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                child_pid = None
                try:
                    output = self._wait_for_output(process, b"\n")
                    child_pid = int(output.strip())
                    os.kill(process.pid, signum)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 128 + signum, stderr)
                    self.assertNotIn("Traceback", stderr)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    self._cleanup_worker(process, child_pid)


if __name__ == "__main__":
    unittest.main()
