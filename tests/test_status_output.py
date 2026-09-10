from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


class FakeDocker:
    def stop(self, container_id: str) -> None:
        del container_id

    def ensure_running(self, container_id: str) -> None:
        del container_id


class FakeRestic:
    def preflight(self) -> None:
        pass

    def backup(self, paths, group_key: str, *, preseed: bool = False) -> None:
        del paths, group_key, preseed

    def forget(self, retention) -> None:
        del retention


def _container(name: str, dependencies: tuple[str, ...] = ()) -> ContainerInfo:
    return ContainerInfo(
        id=f"id-{name}",
        name=name,
        running=True,
        compose_project="app",
        compose_service=name,
        compose_working_dir=None,
        compose_config_files=(),
        compose_environment_files=(),
        mounts=(),
        compose_dependencies=dependencies,
    )


class StatusOutputTests(unittest.TestCase):
    def test_real_run_reports_backup_and_container_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[_container("db"), _container("web", ("db",))],
                sources=[BackupSource(source, "bind")],
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                BackupOrchestrator(
                    FakeDocker(),
                    FakeRestic(),
                    AppConfig(backup=BackupConfig(state_dir=root / "state")),
                ).run([group], [])

            text = output.getvalue()
            expected = [
                "BackupDock: validating backup safety",
                "BackupDock: checking Restic repository",
                "BackupDock: Restic repository ready",
                "[compose:app] preparing",
                "[compose:app] stopping container web (service=web)",
                "[compose:app] stopping container db (service=db)",
                "[compose:app] starting consistent backup",
                "[compose:app] ensuring container db (service=db) is running",
                "[compose:app] ensuring container web (service=web) is running",
                "[compose:app] complete",
                "BackupDock: backup run complete",
            ]
            positions = [text.index(line) for line in expected]
            self.assertEqual(positions, sorted(positions))

    def test_preseed_status_is_reported_before_stopping_containers(self) -> None:
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
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                BackupOrchestrator(
                    FakeDocker(),
                    FakeRestic(),
                    AppConfig(backup=BackupConfig(state_dir=root / "state")),
                    preseed=True,
                ).run([group], [])

            text = output.getvalue()
            self.assertLess(
                text.index("[compose:app] starting preseed; containers remain running"),
                text.index("[compose:app] stopping container web (service=web)"),
            )


if __name__ == "__main__":
    unittest.main()
