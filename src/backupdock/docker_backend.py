from __future__ import annotations

import csv
from collections.abc import Iterable

from backupdock.models import ContainerInfo, MountInfo


COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_WORKING_DIR = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES = "com.docker.compose.project.config_files"
COMPOSE_ENVIRONMENT_FILE = "com.docker.compose.project.environment_file"


def _split_label_paths(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        row = next(csv.reader([value], skipinitialspace=True))
    except (csv.Error, StopIteration):
        row = [value]
    return tuple(item.strip() for item in row if item.strip())


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
    )


class DockerBackend:
    """Small adapter around Docker SDK so orchestration remains testable."""

    def __init__(self) -> None:
        try:
            import docker
        except ImportError as exc:
            raise RuntimeError("Docker SDK for Python is not installed") from exc

        self._client = docker.from_env()
        self._client.ping()

    def list_containers(self) -> list[ContainerInfo]:
        return [parse_container_attrs(container.attrs) for container in self._client.containers.list(all=True)]

    def stop(self, container_id: str, timeout: int) -> None:
        self._client.containers.get(container_id).stop(timeout=timeout)

    def start(self, container_id: str) -> None:
        self._client.containers.get(container_id).start()

    def ensure_running(self, container_id: str) -> None:
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

    def stop(self, container_id: str, timeout: int) -> None:
        raise NotImplementedError

    def start(self, container_id: str) -> None:
        raise NotImplementedError

    def ensure_running(self, container_id: str) -> None:
        raise NotImplementedError
