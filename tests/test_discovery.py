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

    def test_global_exclusion_is_not_hardcoded(self) -> None:
        config = AppConfig(backup=BackupConfig(exclude_paths=(Path("/ignore"),)))
        groups = discover_groups(
            [container("web", project="app", mounts=(MountInfo("bind", "/ignore/data", "/data"),))],
            config,
        )
        self.assertEqual(groups[0].sources, [])


if __name__ == "__main__":
    unittest.main()
