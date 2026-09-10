from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backupdock.config import AppConfig, BackupConfig, ProjectConfig
from backupdock.discovery import DiscoveryError, discover_groups
from backupdock.models import ContainerInfo, MountInfo


def container(
    name: str,
    *,
    project: str | None,
    running: bool = True,
    mounts: tuple[MountInfo, ...] = (),
    config_files: tuple[str, ...] = (),
    working_dir: str | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        id=f"id-{name}",
        name=name,
        running=running,
        compose_project=project,
        compose_service=name if project else None,
        compose_working_dir=working_dir,
        compose_config_files=config_files,
        compose_environment_files=(),
        mounts=mounts,
    )


class DiscoveryTests(unittest.TestCase):
    def test_groups_compose_containers_and_keeps_standalone_separate(self) -> None:
        containers = [
            container("web", project="app"),
            container("db", project="app"),
            container("oneoff", project=None),
        ]
        groups = discover_groups(containers, AppConfig())
        self.assertEqual([group.key for group in groups], ["compose:app", "standalone:oneoff"])
        self.assertEqual([item.name for item in groups[0].containers], ["db", "web"])

    def test_discovers_bind_and_volume_and_ignores_tmpfs(self) -> None:
        mounts = (
            MountInfo("bind", "/data/app", "/app/data"),
            MountInfo("volume", "/var/lib/docker/volumes/db/_data", "/var/lib/db", "db_data"),
            MountInfo("tmpfs", "", "/run"),
        )
        groups = discover_groups([container("db", project="app", mounts=mounts)], AppConfig())
        kinds = {source.kind for source in groups[0].sources}
        self.assertEqual(kinds, {"bind", "volume"})

    def test_compose_metadata_is_discovered_without_fixed_host_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            compose_file = working_dir / "compose.yaml"
            env_file = working_dir / ".env"
            compose_file.write_text("services: {}\n", encoding="utf-8")
            env_file.write_text("FOO=bar\n", encoding="utf-8")

            groups = discover_groups(
                [
                    container(
                        "web",
                        project="app",
                        config_files=(str(compose_file),),
                        working_dir=str(working_dir),
                    )
                ],
                AppConfig(),
            )
            compose_paths = {source.path for source in groups[0].sources if source.kind == "compose"}
            self.assertEqual(compose_paths, {compose_file, env_file})

    def test_project_extra_paths_are_configurable(self) -> None:
        config = AppConfig(projects={"app": ProjectConfig(extra_paths=(Path("/custom/app-extra"),))})
        groups = discover_groups([container("web", project="app")], config)
        self.assertIn(Path("/custom/app-extra"), {source.path for source in groups[0].sources})

    def test_project_volume_exclusion_keeps_other_volumes(self) -> None:
        mounts = (
            MountInfo("volume", "/docker/volumes/backups/_data", "/backups", "app_backups"),
            MountInfo("volume", "/docker/volumes/database/_data", "/var/lib/db", "app_database"),
        )
        config = AppConfig(
            projects={"app": ProjectConfig(exclude_volumes=("app_backups",))},
        )
        groups = discover_groups([container("db", project="app", mounts=mounts)], config)
        volume_names = {source.volume_name for source in groups[0].sources if source.kind == "volume"}
        excluded_volume_names = {source.volume_name for source in groups[0].excluded_sources}
        self.assertEqual(volume_names, {"app_database"})
        self.assertEqual(excluded_volume_names, {"app_backups"})
        self.assertEqual(groups[0].excluded_sources[0].destination, "/backups")

    def test_global_volume_exclusion_applies_to_all_projects(self) -> None:
        mount = MountInfo("volume", "/docker/volumes/cache/_data", "/cache", "shared_cache")
        config = AppConfig(backup=BackupConfig(exclude_volumes=("shared_cache",)))
        groups = discover_groups(
            [
                container("alpha", project="alpha", mounts=(mount,)),
                container("beta", project="beta", mounts=(mount,)),
            ],
            config,
        )
        self.assertTrue(all(not group.sources for group in groups))
        self.assertTrue(all([source.volume_name for source in group.excluded_sources] == ["shared_cache"] for group in groups))

    def test_ignored_container_mounts_are_not_backup_sources(self) -> None:
        ignored_mount = MountInfo("bind", "/data/temporary", "/data")
        regular_mount = MountInfo("bind", "/data/regular", "/data")
        config = AppConfig(backup=BackupConfig(ignore_containers=("temporary",)))

        groups = discover_groups(
            [
                container("temporary", project="app", mounts=(ignored_mount,)),
                container("regular", project="app", mounts=(regular_mount,)),
            ],
            config,
        )
        group = groups[0]

        temporary = next(item for item in group.containers if item.name == "temporary")
        self.assertTrue(temporary.ignored)
        self.assertNotIn(Path("/data/temporary"), {source.path for source in group.sources})
        self.assertEqual([source.path for source in group.ignored_sources], [Path("/data/temporary")])
        self.assertEqual(group.ignored_sources[0].container, "temporary")

    def test_overlapping_storage_across_compose_projects_fails_safe(self) -> None:
        first = container(
            "first",
            project="alpha",
            mounts=(MountInfo("bind", "/data/shared", "/data"),),
        )
        second = container(
            "second",
            project="beta",
            mounts=(MountInfo("bind", "/data/shared/sub", "/data"),),
        )
        with self.assertRaises(DiscoveryError):
            discover_groups([first, second], AppConfig())

    def test_shared_read_only_bind_does_not_create_cross_project_conflict(self) -> None:
        first = container(
            "first",
            project="alpha",
            mounts=(MountInfo("bind", "/etc/localtime", "/etc/localtime", read_only=True),),
        )
        second = container(
            "second",
            project="beta",
            mounts=(MountInfo("bind", "/etc/localtime", "/etc/localtime", read_only=True),),
        )
        groups = discover_groups([first, second], AppConfig())
        self.assertEqual(len(groups), 2)

    def test_global_exclusion_is_not_hardcoded(self) -> None:
        config = AppConfig(backup=BackupConfig(exclude_paths=(Path("/ignore"),)))
        groups = discover_groups(
            [container("web", project="app", mounts=(MountInfo("bind", "/ignore/data", "/data"),))],
            config,
        )
        self.assertEqual(groups[0].sources, [])


if __name__ == "__main__":
    unittest.main()
