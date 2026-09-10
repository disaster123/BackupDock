from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig, ResticConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo
from backupdock.restic import ResticRunner


class FakeDocker:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.started: list[str] = []

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)

    def ensure_running(self, container_id: str) -> None:
        self.started.append(container_id)


class FakeRestic:
    def __init__(self, consistent_groups: set[str]) -> None:
        self.consistent_groups = consistent_groups
        self.backups: list[tuple[str, bool]] = []

    def preflight(self) -> set[str]:
        return set(self.consistent_groups)

    def backup(self, paths, group_key: str, *, preseed: bool = False) -> None:
        del paths
        self.backups.append((group_key, preseed))

    def forget(self, retention) -> None:
        del retention


def _group(source: Path) -> BackupGroup:
    return BackupGroup(
        key="compose:app",
        name="app",
        compose_project="app",
        containers=[
            ContainerInfo(
                id="id-app",
                name="app",
                running=True,
                compose_project="app",
                compose_service="app",
                compose_working_dir=None,
                compose_config_files=(),
                compose_environment_files=(),
                mounts=(),
            )
        ],
        sources=[BackupSource(source, "bind")],
    )


class AutoPreseedTests(unittest.TestCase):
    def test_first_consistent_backup_is_preseeded_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            docker = FakeDocker()
            restic = FakeRestic(set())

            BackupOrchestrator(
                docker,
                restic,
                AppConfig(backup=BackupConfig(state_dir=root / "state")),
            ).run([_group(source)], [])

            self.assertEqual(restic.backups, [("compose:app", True), ("compose:app", False)])
            self.assertEqual(docker.stopped, ["id-app"])
            self.assertEqual(docker.started, ["id-app"])

    def test_existing_consistent_snapshot_skips_automatic_preseed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            restic = FakeRestic({"compose:app"})

            BackupOrchestrator(
                FakeDocker(),
                restic,
                AppConfig(backup=BackupConfig(state_dir=root / "state")),
            ).run([_group(source)], [])

            self.assertEqual(restic.backups, [("compose:app", False)])

    def test_preseed_flag_forces_preseed_even_when_consistent_snapshot_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            restic = FakeRestic({"compose:app"})

            BackupOrchestrator(
                FakeDocker(),
                restic,
                AppConfig(backup=BackupConfig(state_dir=root / "state")),
                preseed=True,
            ).run([_group(source)], [])

            self.assertEqual(restic.backups, [("compose:app", True), ("compose:app", False)])

    def test_preflight_uses_only_normal_snapshots_for_current_host(self) -> None:
        snapshots = [
            {
                "hostname": "docker-prod",
                "tags": ["backupdock", "backupdock-group=compose:app"],
            },
            {
                "hostname": "docker-prod",
                "tags": ["backupdock-preseed", "backupdock-group=compose:first-only"],
            },
            {
                "hostname": "another-host",
                "tags": ["backupdock", "backupdock-group=compose:other"],
            },
        ]
        completed = subprocess.CompletedProcess(
            ["restic", "snapshots", "--json"],
            0,
            stdout=json.dumps(snapshots),
            stderr="",
        )
        runner = ResticRunner(ResticConfig(binary="restic", host="docker-prod"), progress=False)

        with patch("backupdock.restic.subprocess.run", return_value=completed):
            result = runner.preflight()

        self.assertEqual(result, {"compose:app"})

    def test_dry_run_cannot_claim_repository_snapshot_state(self) -> None:
        runner = ResticRunner(ResticConfig(binary="restic"), dry_run=True, progress=False)

        with patch("backupdock.restic.subprocess.run") as subprocess_run:
            result = runner.preflight()

        self.assertIsNone(result)
        subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
