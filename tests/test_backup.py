from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backupdock.backup import BackupError, BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


def make_container(
    name: str,
    running: bool,
    dependencies: tuple[str, ...] = (),
    *,
    auto_remove: bool = False,
    ignored: bool = False,
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
        auto_remove=auto_remove,
        ignored=ignored,
    )


class FakeDocker:
    def __init__(self, fail_stop: str | None = None, fail_start: str | None = None) -> None:
        self.fail_stop = fail_stop
        self.fail_start = fail_start
        self.stopped: list[str] = []
        self.started: list[str] = []

    def stop(self, container_id: str) -> None:
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
    def __init__(self, fail_backup: bool = False, fail_preseed: bool = False) -> None:
        self.fail_backup = fail_backup
        self.fail_preseed = fail_preseed
        self.backups: list[tuple[list[Path], str, bool]] = []
        self.preflight_count = 0

    def preflight(self) -> None:
        self.preflight_count += 1

    def backup(self, paths, group_key: str, *, preseed: bool = False) -> None:
        self.backups.append((list(paths), group_key, preseed))
        if preseed and self.fail_preseed:
            raise RuntimeError("preseed failed")
        if not preseed and self.fail_backup:
            raise RuntimeError("backup failed")

    def forget(self, retention) -> None:
        del retention


class BackupTests(unittest.TestCase):
    def _group(self, source: Path) -> BackupGroup:
        return BackupGroup(
            key="compose:app",
            name="app",
            compose_project="app",
            containers=[
                make_container("db", True),
                make_container("disabled", False),
                make_container("web", True, ("db",)),
            ],
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

            self.assertEqual(docker.stopped, ["id-web", "id-db"])
            self.assertEqual(docker.started, ["id-db", "id-web"])
            self.assertEqual(len(restic.backups), 1)
            self.assertFalse(restic.backups[0][2])

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

            self.assertEqual(docker.started, ["id-db", "id-web"])

    def test_partial_stop_failure_restarts_only_containers_already_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))
            docker = FakeDocker(fail_stop="id-db")
            restic = FakeRestic()

            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                BackupOrchestrator(docker, restic, config).backup_group(self._group(source))

            self.assertEqual(docker.stopped, ["id-web"])
            self.assertEqual(docker.started, ["id-web"])
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

    def test_auto_remove_container_aborts_before_preflight_or_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[make_container("worker", True, auto_remove=True)],
                sources=[BackupSource(source, "bind")],
            )
            docker = FakeDocker()
            restic = FakeRestic()

            with self.assertRaisesRegex(BackupError, "auto-remove"):
                BackupOrchestrator(docker, restic, AppConfig()).run([group], [])

            self.assertEqual(restic.preflight_count, 0)
            self.assertEqual(restic.backups, [])
            self.assertEqual(docker.stopped, [])

    def test_ignored_auto_remove_container_is_not_stopped_when_storage_is_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "regular"
            ignored_source = root / "temporary"
            source.mkdir()
            ignored_source.mkdir()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[
                    make_container("regular", True),
                    make_container("temporary", True, auto_remove=True, ignored=True),
                ],
                sources=[BackupSource(source, "bind", container="regular")],
                ignored_sources=[
                    BackupSource(
                        ignored_source,
                        "bind",
                        container="temporary",
                        required=False,
                    )
                ],
            )
            docker = FakeDocker()
            restic = FakeRestic()
            config = AppConfig(backup=BackupConfig(state_dir=root / "state"))

            BackupOrchestrator(docker, restic, config).run([group], [])

            self.assertEqual(docker.stopped, ["id-regular"])
            self.assertEqual(docker.started, ["id-regular"])

    def test_ignored_running_writer_overlapping_backup_data_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared"
            shared.mkdir()
            group = BackupGroup(
                key="compose:app",
                name="app",
                compose_project="app",
                containers=[
                    make_container("regular", True),
                    make_container("temporary", True, auto_remove=True, ignored=True),
                ],
                sources=[BackupSource(shared, "bind", container="regular")],
                ignored_sources=[
                    BackupSource(
                        shared,
                        "bind",
                        container="temporary",
                        required=False,
                    )
                ],
            )
            docker = FakeDocker()
            restic = FakeRestic()

            with self.assertRaisesRegex(BackupError, "overlapping selected backup data"):
                BackupOrchestrator(docker, restic, AppConfig()).run([group], [])

            self.assertEqual(restic.preflight_count, 0)
            self.assertEqual(docker.stopped, [])

    def test_preseed_runs_before_final_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            docker = FakeDocker()
            restic = FakeRestic()

            BackupOrchestrator(
                docker,
                restic,
                AppConfig(backup=BackupConfig(state_dir=root / "state")),
                preseed=True,
            ).run([self._group(source)], [])

            self.assertEqual([item[2] for item in restic.backups], [True, False])
            self.assertEqual(docker.stopped, ["id-web", "id-db"])
            self.assertEqual(docker.started, ["id-db", "id-web"])

    def test_preseed_failure_does_not_stop_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.mkdir()
            docker = FakeDocker()
            restic = FakeRestic(fail_preseed=True)

            with self.assertRaisesRegex(RuntimeError, "preseed failed"):
                BackupOrchestrator(
                    docker,
                    restic,
                    AppConfig(backup=BackupConfig(state_dir=root / "state")),
                    preseed=True,
                ).run([self._group(source)], [])

            self.assertEqual(docker.stopped, [])
            self.assertEqual(docker.started, [])
            self.assertEqual([item[2] for item in restic.backups], [True])


if __name__ == "__main__":
    unittest.main()
