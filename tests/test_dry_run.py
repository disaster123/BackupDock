from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig, ResticConfig
from backupdock.docker_backend import DockerBackend
from backupdock.models import BackupGroup, BackupSource, ContainerInfo
from backupdock.restic import ResticRunner


def _container(
    name: str,
    *,
    running: bool = True,
    dependencies: tuple[str, ...] = (),
) -> ContainerInfo:
    return ContainerInfo(
        id=f"id-{name}",
        name=name,
        running=running,
        compose_project="app",
        compose_service=name,
        compose_working_dir=None,
        compose_config_files=(),
        compose_environment_files=(),
        mounts=(),
        compose_dependencies=dependencies,
    )


class RecordingDocker:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def stop(self, container_id: str) -> None:
        self.actions.append(("stop", container_id))

    def ensure_running(self, container_id: str) -> None:
        self.actions.append(("ensure_running", container_id))


class RecordingRestic:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def preflight(self) -> None:
        self.actions.append(("preflight",))

    def backup(self, paths, group_key: str, *, preseed: bool = False) -> None:
        self.actions.append(("backup", tuple(paths), group_key, preseed))

    def forget(self, retention) -> None:
        self.actions.append(("forget", retention))


class DryRunTests(unittest.TestCase):
    def test_orchestrator_dry_run_uses_normal_preflight_stop_backup_restart_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            state_dir = root / "state"
            config = AppConfig(backup=BackupConfig(state_dir=state_dir))
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[
                    _container("db"),
                    _container("disabled", running=False),
                    _container("web", dependencies=("db",)),
                ],
                sources=[BackupSource(source, "bind")],
            )
            docker = RecordingDocker()
            restic = RecordingRestic()

            with contextlib.redirect_stdout(io.StringIO()):
                BackupOrchestrator(docker, restic, config, dry_run=True).run([group], [])

            self.assertEqual(restic.actions[0], ("preflight",))
            self.assertEqual(docker.actions[0], ("stop", "id-web"))
            self.assertEqual(docker.actions[1], ("stop", "id-db"))
            self.assertEqual(restic.actions[1][0], "backup")
            self.assertEqual(restic.actions[1][2], "compose:app")
            self.assertFalse(restic.actions[1][3])
            self.assertEqual(docker.actions[2], ("ensure_running", "id-db"))
            self.assertEqual(docker.actions[3], ("ensure_running", "id-web"))
            self.assertFalse((state_dir / "manifests" / "compose_app.json").exists())

    def test_restic_dry_run_prints_command_without_subprocess(self) -> None:
        runner = ResticRunner(ResticConfig(binary="restic"), dry_run=True)
        stdout = io.StringIO()

        with (
            patch("backupdock.restic.subprocess.run") as subprocess_run,
            patch("backupdock.restic.subprocess.Popen") as subprocess_popen,
            contextlib.redirect_stdout(stdout),
        ):
            runner.preflight()
            runner.backup([Path("/data/example")], "compose:app")
            runner.backup([Path("/data/example")], "compose:app", preseed=True)

        subprocess_run.assert_not_called()
        subprocess_popen.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("DRY-RUN restic snapshots --json", output)
        self.assertIn("DRY-RUN restic backup --tag backupdock --tag backupdock-group=compose:app /data/example", output)
        self.assertIn("DRY-RUN restic backup --tag backupdock-preseed --tag backupdock-group=compose:app /data/example", output)

    def test_preseed_dry_run_shows_preseed_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[_container("web")],
                sources=[BackupSource(source, "bind")],
            )
            stdout = io.StringIO()
            backend = object.__new__(DockerBackend)
            backend.dry_run = True
            backend._client = MagicMock()
            backend._containers_by_id = {"id-web": _container("web")}
            runner = ResticRunner(ResticConfig(binary="restic"), dry_run=True)

            with contextlib.redirect_stdout(stdout):
                BackupOrchestrator(
                    backend,
                    runner,
                    AppConfig(backup=BackupConfig(state_dir=root / "state")),
                    dry_run=True,
                    preseed=True,
                ).run([group], [])

            output = stdout.getvalue()
            preseed_position = output.index("--tag backupdock-preseed")
            stop_position = output.index("DRY-RUN docker stop")
            final_position = output.index("--tag backupdock --tag")
            self.assertLess(preseed_position, stop_position)
            self.assertLess(stop_position, final_position)

    def test_docker_dry_run_prints_mutations_without_sdk_calls(self) -> None:
        backend = object.__new__(DockerBackend)
        backend.dry_run = True
        backend._client = MagicMock()
        backend._containers_by_id = {}
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            backend.stop("container-123")
            backend.ensure_running("container-123")

        backend._client.containers.get.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("DRY-RUN docker stop container=container-123", output)
        self.assertNotIn("timeout=", output)
        self.assertIn("DRY-RUN docker ensure-running container=container-123", output)

    def test_real_docker_stop_passes_no_timeout_override(self) -> None:
        backend = object.__new__(DockerBackend)
        backend.dry_run = False
        backend._client = MagicMock()
        backend._containers_by_id = {}

        backend.stop("container-123")

        backend._client.containers.get.assert_called_once_with("container-123")
        backend._client.containers.get.return_value.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
