from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.cli import main
from backupdock.config import AppConfig, BackupConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


def _container(name: str, *, running: bool) -> ContainerInfo:
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
    )


def _group(source: Path) -> BackupGroup:
    return BackupGroup(
        key="compose:app",
        name="app",
        compose_project="app",
        containers=[
            _container("db", running=True),
            _container("worker", running=False),
            _container("web", running=True),
        ],
        sources=[BackupSource(path=source, kind="bind")],
    )


class CliDryRunTests(unittest.TestCase):
    def test_main_dry_run_uses_normal_orchestrator_run(self) -> None:
        docker = MagicMock()
        restic = MagicMock()
        orchestrator = MagicMock()
        process_lock = MagicMock()
        process_lock.__enter__.return_value = process_lock
        process_lock.__exit__.return_value = None
        config = AppConfig(backup=BackupConfig(state_dir=Path("/tmp/backupdock-test-state")))
        group = _group(Path("/tmp/backupdock-test-source"))

        with (
            patch("backupdock.cli.load_config", return_value=config),
            patch("backupdock.cli.DockerBackend", return_value=docker) as docker_class,
            patch("backupdock.cli.ResticRunner", return_value=restic) as restic_class,
            patch("backupdock.cli._discover", return_value=([group], [])),
            patch("backupdock.cli.ProcessLock", return_value=process_lock),
            patch("backupdock.cli.BackupOrchestrator", return_value=orchestrator) as orchestrator_class,
            patch("backupdock.cli.signal.signal"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = main(["backup", "--dry-run"])

        self.assertEqual(result, 0)
        docker_class.assert_called_once_with(dry_run=True)
        restic_class.assert_called_once_with(config.restic, dry_run=True)
        orchestrator_class.assert_called_once_with(docker, restic, config, dry_run=True)
        orchestrator.run.assert_called_once_with([group], [])
        docker.close.assert_called_once()

    def test_real_backup_constructs_same_components_without_dry_run(self) -> None:
        docker = MagicMock()
        restic = MagicMock()
        orchestrator = MagicMock()
        process_lock = MagicMock()
        process_lock.__enter__.return_value = process_lock
        process_lock.__exit__.return_value = None
        config = AppConfig(backup=BackupConfig(state_dir=Path("/tmp/backupdock-test-state")))
        group = _group(Path("/tmp/backupdock-test-source"))

        with (
            patch("backupdock.cli.load_config", return_value=config),
            patch("backupdock.cli.DockerBackend", return_value=docker) as docker_class,
            patch("backupdock.cli.ResticRunner", return_value=restic) as restic_class,
            patch("backupdock.cli._discover", return_value=([group], [])),
            patch("backupdock.cli.ProcessLock", return_value=process_lock),
            patch("backupdock.cli.BackupOrchestrator", return_value=orchestrator) as orchestrator_class,
            patch("backupdock.cli.signal.signal"),
        ):
            result = main(["backup"])

        self.assertEqual(result, 0)
        docker_class.assert_called_once_with(dry_run=False)
        restic_class.assert_called_once_with(config.restic, dry_run=False)
        orchestrator_class.assert_called_once_with(docker, restic, config, dry_run=False)
        orchestrator.run.assert_called_once_with([group], [])


if __name__ == "__main__":
    unittest.main()
