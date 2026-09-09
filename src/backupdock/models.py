from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MountInfo:
    type: str
    source: str
    destination: str
    volume_name: str | None = None
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    name: str
    running: bool
    compose_project: str | None
    compose_service: str | None
    compose_working_dir: str | None
    compose_config_files: tuple[str, ...]
    compose_environment_files: tuple[str, ...]
    mounts: tuple[MountInfo, ...]


@dataclass(frozen=True, slots=True)
class BackupSource:
    path: Path
    kind: str
    container: str | None = None
    destination: str | None = None
    volume_name: str | None = None
    required: bool = True

    @property
    def persistent(self) -> bool:
        return self.kind in {"bind", "volume", "extra"}


@dataclass(slots=True)
class BackupGroup:
    key: str
    name: str
    compose_project: str | None
    containers: list[ContainerInfo] = field(default_factory=list)
    sources: list[BackupSource] = field(default_factory=list)

    @property
    def running_containers(self) -> list[ContainerInfo]:
        return [container for container in self.containers if container.running]
