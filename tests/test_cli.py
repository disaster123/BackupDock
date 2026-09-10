from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock.cli import _dry_run, main
from backupdock.config import AppConfig
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


def _group() -> BackupGroup:
    return BackupGroup(
        key="compose:app",
        name="app",
        compose_project="app",
        containers=[
            _container("db", running=True),
            _container("worker", running=False),
            _container("web", running=True),
        ],
        sources=[
            BackupSource(
                path=Path("/docker/volumes/app_db/_data"),
                kind="volume",
                destination="/var/lib/db",
                volume_name="app_db",
            ),
            BackupSource(path=Path("/opt/app/compose.yaml"), kind="compose", required=False),
        ],
        excluded_sources=[
            BackupSource(
                path=Path("/docker/volumes/app_backups/_data"),
                kind="volume",
                destination="/backups",
                volume_name="app_backups",
            )
        ],
    )


class CliDryRunTests(unittest.TestCase):
    def test_dry_run_shows_stop_backup_excluded_and_restart_plan(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            _dry_run([_group()], [])

        output = stdout.getvalue()
        self.assertIn("Backup plan (dry-run)", output)
        self.assertIn("[compose] app", output)
        self.assertIn("app_db", output)
        self.assertIn("app_backups", output)
        self.assertIn("excluded:", output)
        self.assertIn("Dry-run only: no containers were stopped and Restic was not executed.", output)

        stop_section = output.split("  stop:\n", 1)[1].split("  backup:\n", 1)[0]
        restart_section = output.split("  restart:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("db", stop_section)
        self.assertIn("web", stop_section)
        self.assertNotIn("worker", stop_section)
        self.assertIn("db", restart_section)
        self.assertIn("web", restart_section)
        self.assertNotIn("worker", restart_section)

    def test_main_dry_run_does_not_execute_restic_or_modify_containers(self) -> None:
        docker = MagicMock()
        restic = MagicMock()

        with (
            patch("backupdock.cli.load_config", return_value=AppConfig()),
            patch("backupdock.cli.DockerBackend", return_value=docker),
            patch("backupdock.cli.ResticRunner", return_value=restic),
            patch("backupdock.cli._discover", return_value=([_group()], [])),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = main(["backup", "--dry-run"])

        self.assertEqual(result, 0)
        restic.preflight.assert_not_called()
        restic.backup.assert_not_called()
        restic.forget.assert_not_called()
        docker.stop.assert_not_called()
        docker.ensure_running.assert_not_called()
        docker.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
