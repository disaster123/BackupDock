from __future__ import annotations

import os
from pathlib import Path

from backupdock.config import AppConfig
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


class DiscoveryError(RuntimeError):
    pass


def normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.realpath(os.fspath(path))))


def paths_overlap(first: Path, second: Path) -> bool:
    a = normalized(first)
    b = normalized(second)
    return a == b or a in b.parents or b in a.parents


def _excluded(path: Path, exclusions: tuple[Path, ...]) -> bool:
    candidate = normalized(path)
    for exclusion in exclusions:
        excluded_path = normalized(exclusion)
        if candidate == excluded_path or excluded_path in candidate.parents:
            return True
    return False


def _group_key(container: ContainerInfo) -> tuple[str, str, str | None]:
    if container.compose_project:
        return f"compose:{container.compose_project}", container.compose_project, container.compose_project
    return f"standalone:{container.name}", container.name, None


def _metadata_sources(containers: list[ContainerInfo]) -> list[BackupSource]:
    paths: set[Path] = set()
    working_dirs: set[Path] = set()

    for container in containers:
        paths.update(Path(value) for value in container.compose_config_files if value)
        paths.update(Path(value) for value in container.compose_environment_files if value)
        if container.compose_working_dir:
            working_dirs.add(Path(container.compose_working_dir))

    for working_dir in working_dirs:
        dotenv = working_dir / ".env"
        if dotenv.is_file():
            paths.add(dotenv)

    return [
        BackupSource(path=path, kind="compose", required=False)
        for path in sorted(paths, key=lambda item: str(item))
    ]


def discover_groups(containers: list[ContainerInfo], config: AppConfig) -> list[BackupGroup]:
    grouped: dict[str, BackupGroup] = {}

    for container in containers:
        key, name, compose_project = _group_key(container)
        grouped.setdefault(
            key,
            BackupGroup(key=key, name=name, compose_project=compose_project),
        ).containers.append(container)

    global_exclusions = config.backup.exclude_paths

    for group in grouped.values():
        project_config = config.projects.get(group.compose_project or group.name)
        project_exclusions = project_config.exclude_paths if project_config else ()
        exclusions = global_exclusions + project_exclusions
        sources: list[BackupSource] = []

        for container in group.containers:
            for mount in container.mounts:
                if mount.type == "tmpfs":
                    continue
                if mount.type not in {"bind", "volume"}:
                    continue
                if not mount.source:
                    continue

                source_path = Path(mount.source)
                if _excluded(source_path, exclusions):
                    continue

                sources.append(
                    BackupSource(
                        path=source_path,
                        kind=mount.type,
                        container=container.name,
                        destination=mount.destination,
                        volume_name=mount.volume_name,
                        required=True,
                    )
                )

        if project_config:
            for extra_path in project_config.extra_paths:
                if not _excluded(extra_path, exclusions):
                    sources.append(BackupSource(path=extra_path, kind="extra", required=True))

        if config.backup.include_compose_metadata and group.compose_project:
            sources.extend(_metadata_sources(group.containers))

        unique: dict[tuple[str, str], BackupSource] = {}
        for source in sources:
            key = (source.kind, str(normalized(source.path)))
            unique.setdefault(key, source)
        group.sources = list(unique.values())
        group.containers.sort(key=lambda container: container.name)
        group.sources.sort(key=lambda source: (str(normalized(source.path)), source.kind))

    groups = sorted(grouped.values(), key=lambda group: group.key)
    validate_no_cross_group_storage(groups)
    return groups


def validate_no_cross_group_storage(groups: list[BackupGroup]) -> None:
    persistent: list[tuple[BackupGroup, BackupSource]] = []
    for group in groups:
        for source in group.sources:
            if source.persistent:
                persistent.append((group, source))

    conflicts: set[tuple[str, str, str, str]] = set()
    for index, (first_group, first_source) in enumerate(persistent):
        for second_group, second_source in persistent[index + 1 :]:
            if first_group.key == second_group.key:
                continue
            if paths_overlap(first_source.path, second_source.path):
                conflicts.add(
                    (
                        first_group.name,
                        str(first_source.path),
                        second_group.name,
                        str(second_source.path),
                    )
                )

    if conflicts:
        lines = ["Persistent storage overlaps across backup groups:"]
        for first_name, first_path, second_name, second_path in sorted(conflicts):
            lines.append(f"  {first_name}: {first_path} <-> {second_name}: {second_path}")
        lines.append("BackupDock will not stop unrelated Compose projects implicitly.")
        raise DiscoveryError("\n".join(lines))


def host_sources(config: AppConfig, groups: list[BackupGroup]) -> list[BackupSource]:
    sources: list[BackupSource] = []
    persistent_group_paths = [
        source.path
        for group in groups
        for source in group.sources
        if source.persistent
    ]

    for path in config.backup.host_paths:
        if _excluded(path, config.backup.exclude_paths):
            continue
        if any(paths_overlap(path, existing) for existing in persistent_group_paths):
            raise DiscoveryError(
                f"Host path {path} overlaps Docker-managed backup data. "
                "Attach it to the corresponding project instead."
            )
        sources.append(BackupSource(path=path, kind="host", required=True))

    return sources
