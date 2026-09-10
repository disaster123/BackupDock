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

    def stop(self, container_id: str, timeout: int) -> None:
        self.actions.append(("stop", container_id, timeout))

    def ensure_running(self, container_id: str) -> None:
        self.actions.append(("ensure_running", container_id))


class RecordingRestic:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def preflight(self) -> None:
        self.actions.append(("preflight",))

    def backup(self, paths, group_key: str) -> None:
        self.actions.append(("backup", tuple(paths), group_key))

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
            self.assertEqual(docker.actions[0], ("stop", "id-web", 30))
            self.assertEqual(docker.actions[1], ("stop", "id-db", 30))
            self.assertEqual(restic.actions[1][0], "backup")
            self.assertEqual(restic.actions[1][2], "compose:app")
            self.assertEqual(docker.actions[2], ("ensure_running", "id-db"))
            self.assertEqual(docker.actions[3], ("ensure_running", "id-web"))
            self.assertFalse((state_dir / "manifests" / "compose_app.json").exists())

    def test_restic_dry_run_prints_command_without_subprocess(self) -> None:
        runner = ResticRunner(ResticConfig(binary="restic"), dry_run=True)
        stdout = io.StringIO()

        with patch("backupdock.restic.subprocess.run") as subprocess_run, contextlib.redirect_stdout(stdout):
            runner.preflight()
            runner.backup([Path("/data/example")], "compose:app")

        subprocess_run.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("DRY-RUN restic snapshots --json", output)
        self.assertIn("DRY-RUN restic backup --tag backupdock --tag backupdock-group=compose:app /data/example", output)

    def test_docker_dry_run_prints_mutations_without_sdk_calls(self) -> None:
        backend = object.__new__(DockerBackend)
        backend.dry_run = True
        backend._client = MagicMock()
        backend._containers_by_id = {}
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            backend.stop("container-123", 30)
            backend.ensure_running("container-123")

        backend._client.containers.get.assert_not_called()
        output = stdout.getvalue()
        self.assertIn("DRY-RUN docker stop container=container-123 timeout=30s", output)
        self.assertIn("DRY-RUN docker ensure-running container=container-123", output)


if __name__ == "__main__":
    unittest.main()
