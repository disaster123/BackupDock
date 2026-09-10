from __future__ import annotations

import contextlib
import io
import signal
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.cli import main
from backupdock.config import AppConfig, BackupConfig, RemoteConfig
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
        orchestrator_class.assert_called_once_with(
            docker,
            restic,
            config,
            dry_run=True,
            preseed=False,
        )
        orchestrator.run.assert_called_once_with([group], [], observed_groups=[group])
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
        orchestrator_class.assert_called_once_with(
            docker,
            restic,
            config,
            dry_run=False,
            preseed=False,
        )
        orchestrator.run.assert_called_once_with([group], [], observed_groups=[group])

    def test_preseed_flag_is_passed_to_orchestrator(self) -> None:
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
            patch("backupdock.cli.DockerBackend", return_value=docker),
            patch("backupdock.cli.ResticRunner", return_value=restic),
            patch("backupdock.cli._discover", return_value=([group], [])),
            patch("backupdock.cli.ProcessLock", return_value=process_lock),
            patch("backupdock.cli.BackupOrchestrator", return_value=orchestrator) as orchestrator_class,
            patch("backupdock.cli.signal.signal"),
        ):
            result = main(["backup", "--preseed"])

        self.assertEqual(result, 0)
        orchestrator_class.assert_called_once_with(
            docker,
            restic,
            config,
            dry_run=False,
            preseed=True,
        )

    def test_remote_backup_keyboard_interrupt_is_clean(self) -> None:
        remote = RemoteConfig(
            ssh_target="root@docker-prod.example",
            password_file=Path("/repository-password"),
            repository_path="docker-prod",
            rest_server_username="docker-prod",
            rest_server_password_file=Path("/rest-server-password"),
        )
        config = AppConfig(remotes={"docker-prod": remote})
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch("backupdock.cli.load_config", return_value=config),
            patch("backupdock.cli.RemoteBackupController") as controller_class,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            controller_class.return_value.run.side_effect = KeyboardInterrupt()
            result = main(["remote-backup", "docker-prod"])

        self.assertEqual(result, 128 + signal.SIGINT)
        self.assertIn("backupdock: interrupted by SIGINT", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_remote_init_uses_selected_remote_controller(self) -> None:
        remote = RemoteConfig(
            ssh_target="root@docker-prod.example",
            password_file=Path("/repository-password"),
            repository_path="docker-prod",
            rest_server_username="docker-prod",
            rest_server_password_file=Path("/rest-server-password"),
        )
        config = AppConfig(remotes={"docker-prod": remote})

        with (
            patch("backupdock.cli.load_config", return_value=config),
            patch("backupdock.cli.RemoteBackupController") as controller_class,
            patch("backupdock.cli.ResticRunner") as restic_class,
        ):
            result = main(["init", "--remote", "docker-prod"])

        self.assertEqual(result, 0)
        controller_class.assert_called_once_with(config, remote)
        controller_class.return_value.init_repository.assert_called_once_with()
        restic_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
