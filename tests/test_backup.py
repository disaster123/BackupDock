from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


def make_container(name: str, running: bool) -> ContainerInfo:
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


class FakeDocker:
    def __init__(self, fail_stop: str | None = None, fail_start: str | None = None) -> None:
        self.fail_stop = fail_stop
        self.fail_start = fail_start
        self.stopped: list[str] = []
        self.started: list[str] = []

    def stop(self, container_id: str, timeout: int) -> None:
        del timeout
        if container_id == self.fail_stop:
            raise RuntimeError("stop failed")
        self.stopped.append(container_id)

    def start(self, container_id: str) -> None:
        if container_id == self.fail_start:
            raise RuntimeError("start failed")
        self.started.append(container_id)

    def ensure_running(self, container_id: str) -> None:
        if container_id == self.fail_start:
            raise RuntimeError("start failed")
        if container_id in self.stopped:
            self.started.append(container_id)


class FakeRestic:
    def __init__(self, fail_backup: bool = False) -> None:
        self.fail_backup = fail_backup
        self.backups: list[tuple[list[Path], str]] = []
        self.preflight_count = 0

    def preflight(self) -> None:
        self.preflight_count += 1

    def backup(self, paths, group_key: str) -> None:
        self.backups.append((list(paths), group_key))
        if self.fail_backup:
            raise RuntimeError("backup failed")

    def forget(self, retention) -> None:
        del retention


class BackupTests(unittest.TestCase):
    def _group(self, source: Path) -> BackupGroup:
        return BackupGroup(
            key="compose:app",
            name="app",
            compose_project="app",
            containers=[make_container("db", True), make_container("disabled", False), make_container("web", True)],
            sources=[BackupSource(source, "bind")],
        )

    def test_only_originally_running_containers_are_stopped_and_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            docker = FakeDocker()
            restic = FakeRestic()

            BackupOrchestrator(docker, restic, config).backup_group(self._group(source))

            self.assertEqual(docker.stopped, ["id-db", "id-web"])
            self.assertEqual(docker.started, ["id-web", "id-db"])
            self.assertEqual(len(restic.backups), 1)

    def test_containers_restart_when_restic_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            docker = FakeDocker()
            restic = FakeRestic(fail_backup=True)

            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                BackupOrchestrator(docker, restic, config).backup_group(self._group(source))

            self.assertEqual(docker.started, ["id-web", "id-db"])

    def test_partial_stop_failure_restarts_only_containers_already_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            docker = FakeDocker(fail_stop="id-web")
            restic = FakeRestic()

            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                BackupOrchestrator(docker, restic, config).backup_group(self._group(source))

            self.assertEqual(docker.stopped, ["id-db"])
            self.assertEqual(docker.started, ["id-db"])
            self.assertEqual(restic.backups, [])

    def test_manifest_is_part_of_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            restic = FakeRestic()

            BackupOrchestrator(FakeDocker(), restic, config).backup_group(self._group(source))
            paths = restic.backups[0][0]
            self.assertTrue(any("manifests" in str(path) for path in paths))

    def test_symlink_source_path_is_preserved_for_restic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("data", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            restic = FakeRestic()

            BackupOrchestrator(FakeDocker(), restic, config).backup_group(self._group(link))

            paths = restic.backups[0][0]
            self.assertIn(link, paths)
            self.assertNotIn(target, paths)

    def test_group_without_persistent_data_is_not_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "compose.yaml"
            metadata.write_text("services: {}\n", encoding="utf-8")
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            docker = FakeDocker()
            restic = FakeRestic()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[make_container("web", True)],
                sources=[BackupSource(metadata, "compose", required=False)],
            )

            BackupOrchestrator(docker, restic, config).backup_group(group)

            self.assertEqual(docker.stopped, [])
            self.assertEqual(docker.started, [])
            self.assertEqual(len(restic.backups), 1)


if __name__ == "__main__":
    unittest.main()
