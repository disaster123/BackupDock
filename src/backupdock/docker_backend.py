from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import replace

from backupdock.models import ContainerInfo, MountInfo


COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_WORKING_DIR = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES = "com.docker.compose.project.config_files"
COMPOSE_ENVIRONMENT_FILE = "com.docker.compose.project.environment_file"
COMPOSE_DEPENDENCIES = "com.docker.compose.depends_on"


def _split_label_paths(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        row = next(csv.reader([value], skipinitialspace=True))
    except (csv.Error, StopIteration):
        row = [value]
    return tuple(item.strip() for item in row if item.strip())


def _parse_compose_dependencies(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    dependencies: set[str] = set()
    for item in value.split(","):
        service = item.split(":", 1)[0].strip()
        if service:
            dependencies.add(service)
    return tuple(sorted(dependencies))


def _runtime_dependency_refs(attrs: dict) -> tuple[str, ...]:
    host_config = attrs.get("HostConfig") or {}
    references: set[str] = set()

    for link in host_config.get("Links") or []:
        source = str(link).split(":", 1)[0].lstrip("/")
        if source:
            references.add(source)

    for volume_from in host_config.get("VolumesFrom") or []:
        source = str(volume_from).split(":", 1)[0].lstrip("/")
        if source:
            references.add(source)

    network_mode = str(host_config.get("NetworkMode") or "")
    if network_mode.startswith("container:"):
        source = network_mode.split(":", 1)[1].lstrip("/")
        if source:
            references.add(source)

    return tuple(sorted(references))


def _resolve_container_reference(
    reference: str,
    by_id: dict[str, ContainerInfo],
    by_name: dict[str, ContainerInfo],
) -> ContainerInfo | None:
    normalized = reference.lstrip("/")
    if normalized in by_name:
        return by_name[normalized]
    if normalized in by_id:
        return by_id[normalized]

    matches = [container for container_id, container in by_id.items() if container_id.startswith(normalized)]
    if len(matches) == 1:
        return matches[0]
    return None


def merge_runtime_dependencies(
    containers: list[ContainerInfo],
    attrs_list: list[dict],
) -> list[ContainerInfo]:
    by_id = {container.id: container for container in containers if container.id}
    by_name = {container.name: container for container in containers if container.name}
    enriched: list[ContainerInfo] = []

    for container, attrs in zip(containers, attrs_list, strict=True):
        dependencies = set(container.compose_dependencies)
        if container.compose_project and container.compose_service:
            for reference in _runtime_dependency_refs(attrs):
                dependency = _resolve_container_reference(reference, by_id, by_name)
                if dependency is None:
                    continue
                if dependency.compose_project != container.compose_project:
                    continue
                if not dependency.compose_service or dependency.compose_service == container.compose_service:
                    continue
                dependencies.add(dependency.compose_service)

        enriched.append(replace(container, compose_dependencies=tuple(sorted(dependencies))))

    return enriched


def parse_container_attrs(attrs: dict) -> ContainerInfo:
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    state = attrs.get("State") or {}
    mounts: list[MountInfo] = []

    for mount in attrs.get("Mounts") or []:
        mount_type = str(mount.get("Type") or "")
        source = str(mount.get("Source") or "")
        destination = str(mount.get("Destination") or "")
        if not mount_type or not destination:
            continue
        mounts.append(
            MountInfo(
                type=mount_type,
                source=source,
                destination=destination,
                volume_name=(str(mount.get("Name")) if mount.get("Name") else None),
                read_only=not bool(mount.get("RW", True)),
            )
        )

    name = str(attrs.get("Name") or "").lstrip("/")
    container_id = str(attrs.get("Id") or attrs.get("ID") or "")

    return ContainerInfo(
        id=container_id,
        name=name or container_id[:12],
        running=bool(state.get("Running", False)),
        compose_project=labels.get(COMPOSE_PROJECT),
        compose_service=labels.get(COMPOSE_SERVICE),
        compose_working_dir=labels.get(COMPOSE_WORKING_DIR),
        compose_config_files=_split_label_paths(labels.get(COMPOSE_CONFIG_FILES)),
        compose_environment_files=_split_label_paths(labels.get(COMPOSE_ENVIRONMENT_FILE)),
        mounts=tuple(mounts),
        compose_dependencies=_parse_compose_dependencies(labels.get(COMPOSE_DEPENDENCIES)),
    )


class DockerBackend:
    """Small adapter around Docker SDK so orchestration remains testable."""

    def __init__(self, *, dry_run: bool = False) -> None:
        try:
            import docker
        except ImportError as exc:
            raise RuntimeError("Docker SDK for Python is not installed") from exc

        self.dry_run = dry_run
        self._client = docker.from_env()
        self._client.ping()
        self._containers_by_id: dict[str, ContainerInfo] = {}

    def list_containers(self) -> list[ContainerInfo]:
        docker_containers = list(self._client.containers.list(all=True))
        attrs_list = [container.attrs for container in docker_containers]
        containers = [parse_container_attrs(attrs) for attrs in attrs_list]
        containers = merge_runtime_dependencies(containers, attrs_list)
        self._containers_by_id = {container.id: container for container in containers if container.id}
        return containers

    def _describe_container(self, container_id: str) -> str:
        container = getattr(self, "_containers_by_id", {}).get(container_id)
        if container is None:
            return f"container={container_id}"
        service = f" service={container.compose_service}" if container.compose_service else ""
        return f"container={container.name}{service} id={container_id[:12]}"

    def stop(self, container_id: str) -> None:
        if self.dry_run:
            print(f"DRY-RUN docker stop {self._describe_container(container_id)}")
            return
        self._client.containers.get(container_id).stop()

    def start(self, container_id: str) -> None:
        if self.dry_run:
            print(f"DRY-RUN docker start {self._describe_container(container_id)}")
            return
        self._client.containers.get(container_id).start()

    def ensure_running(self, container_id: str) -> None:
        if self.dry_run:
            print(f"DRY-RUN docker ensure-running {self._describe_container(container_id)}")
            return
        container = self._client.containers.get(container_id)
        container.reload()
        if not bool((container.attrs.get("State") or {}).get("Running", False)):
            container.start()

    def close(self) -> None:
        self._client.close()


class FakeDockerBackendProtocol:
    """Documentation-only protocol shape used by tests and alternative backends."""

    def list_containers(self) -> Iterable[ContainerInfo]:
        raise NotImplementedError

    def stop(self, container_id: str) -> None:
        raise NotImplementedError

    def start(self, container_id: str) -> None:
        raise NotImplementedError

    def ensure_running(self, container_id: str) -> None:
        raise NotImplementedError
