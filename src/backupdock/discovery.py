from __future__ import annotations

import logging
import os
import stat
from dataclasses import replace
from pathlib import Path

from backupdock.config import AppConfig
from backupdock.compose_metadata import ComposeMetadataError, service_environment_sources
from backupdock.models import BackupGroup, BackupSource, ContainerInfo
from backupdock.ordering import DependencyOrderError, containers_in_start_order


logger = logging.getLogger(__name__)


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


def _special_bind_source(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return not (stat.S_ISDIR(mode) or stat.S_ISREG(mode))


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

    sources = [
        BackupSource(path=path, kind="compose", required=False)
        for path in sorted(paths, key=lambda item: str(item))
    ]
    try:
        sources.extend(service_environment_sources(containers))
    except ComposeMetadataError as exc:
        raise DiscoveryError(str(exc)) from exc
    # Preserve the original links and also archive the file contents they refer to.
    sources.extend(BackupSource(source.path.resolve(), "compose", required=source.required)
                   for source in list(sources) if source.path.is_symlink())
    return sources


def _order_group_containers(group: BackupGroup) -> None:
    known_services = {
        container.compose_service
        for container in group.containers
        if container.compose_service
    }
    for container in group.containers:
        for dependency in container.compose_dependencies:
            if dependency not in known_services:
                logger.warning(
                    "Compose dependency target has no container in project %s: service=%s dependency=%s; ignoring it for stop/start ordering",
                    group.name,
                    container.compose_service or container.name,
                    dependency,
                )

    try:
        group.containers = containers_in_start_order(group.containers)
    except DependencyOrderError as exc:
        raise DiscoveryError(f"Cannot determine safe container order for project {group.name}: {exc}") from exc


def _source_from_mount(container: ContainerInfo, mount, *, required: bool) -> BackupSource:
    return BackupSource(
        path=Path(mount.backup_source or mount.source),
        kind=mount.type,
        container=container.name,
        destination=mount.destination,
        volume_name=mount.volume_name,
        read_only=mount.read_only,
        required=required,
    )


def discover_groups(containers: list[ContainerInfo], config: AppConfig) -> list[BackupGroup]:
    grouped: dict[str, BackupGroup] = {}
    ignored_names = set(config.backup.ignore_containers)
    found_names = {container.name for container in containers}

    for ignored_name in sorted(ignored_names - found_names):
        logger.warning("Configured ignored container was not found: %s", ignored_name)

    for original in containers:
        container = replace(original, ignored=original.name in ignored_names)
        key, name, compose_project = _group_key(container)
        grouped.setdefault(
            key,
            BackupGroup(key=key, name=name, compose_project=compose_project),
        ).containers.append(container)

    global_exclusions = config.backup.exclude_paths
    global_volume_exclusions = set(config.backup.exclude_volumes)

    for group in grouped.values():
        project_config = config.projects.get(group.compose_project or group.name)
        project_exclusions = project_config.exclude_paths if project_config else ()
        project_volume_exclusions = set(project_config.exclude_volumes) if project_config else set()
        exclusions = global_exclusions + project_exclusions
        excluded_volumes = global_volume_exclusions | project_volume_exclusions
        sources: list[BackupSource] = []
        excluded_sources: list[BackupSource] = []
        ignored_sources: list[BackupSource] = []

        for container in group.containers:
            for mount in container.mounts:
                if mount.type == "tmpfs":
                    continue
                if mount.type not in {"bind", "volume"}:
                    continue
                if not mount.source:
                    if mount.type == "volume" and mount.volume_name not in excluded_volumes:
                        raise DiscoveryError(f"Cannot safely back up volume {mount.volume_name}: Docker reported no source path")
                    continue

                source_path = Path(mount.backup_source or mount.source)
                if mount.type == "bind" and _special_bind_source(source_path):
                    logger.warning("Ignoring non-file bind mount source: %s", source_path)
                    continue

                if container.ignored:
                    if mount.backup_error and container.running and not mount.read_only:
                        raise DiscoveryError(f"Cannot verify ignored writable volume {mount.volume_name}: {mount.backup_error}")
                    ignored_sources.append(_source_from_mount(container, mount, required=False))
                    continue

                if mount.type == "volume" and mount.volume_name in excluded_volumes:
                    excluded_sources.append(_source_from_mount(container, mount, required=False))
                    continue

                if _excluded(source_path, exclusions) or _excluded(Path(mount.source), exclusions):
                    continue

                if mount.backup_error:
                    raise DiscoveryError(f"Cannot safely back up volume {mount.volume_name} in {group.key}: {mount.backup_error}")

                sources.append(_source_from_mount(container, mount, required=True))

        if project_config:
            for extra_path in project_config.extra_paths:
                if not _excluded(extra_path, exclusions):
                    sources.append(BackupSource(path=extra_path, kind="extra", required=True))

        if config.backup.include_compose_metadata and group.compose_project:
            for source in _metadata_sources(group.containers):
                if _excluded(source.path, exclusions):
                    continue
                if source.required and not source.path.is_file():
                    raise DiscoveryError(f"Required Compose environment file is missing or not a regular file: {source.path}")
                sources.append(source)

        unique: dict[tuple[str, str], BackupSource] = {}
        for source in sources:
            identity = Path(os.path.abspath(source.path)) if source.kind == "compose" else normalized(source.path)
            key = (source.kind, str(identity))
            if key not in unique or (source.required and not unique[key].required):
                unique[key] = source
        group.sources = list(unique.values())

        unique_excluded: dict[tuple[str, str], BackupSource] = {}
        for source in excluded_sources:
            key = (source.kind, str(normalized(source.path)))
            unique_excluded.setdefault(key, source)
        group.excluded_sources = list(unique_excluded.values())

        unique_ignored: dict[tuple[str, str, str | None], BackupSource] = {}
        for source in ignored_sources:
            key = (source.kind, str(normalized(source.path)), source.container)
            unique_ignored.setdefault(key, source)
        group.ignored_sources = list(unique_ignored.values())

        _order_group_containers(group)
        group.sources.sort(key=lambda source: (str(normalized(source.path)), source.kind))
        group.excluded_sources.sort(key=lambda source: (str(normalized(source.path)), source.kind))
        group.ignored_sources.sort(
            key=lambda source: (str(normalized(source.path)), source.container or "", source.kind)
        )

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
                if first_source.read_only and second_source.read_only:
                    continue
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
    for path in config.backup.host_paths:
        if _excluded(path, config.backup.exclude_paths):
            continue
        sources.append(BackupSource(path=path, kind="host", required=True))

    return sources
