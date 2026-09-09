from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("/etc/backupdock/config.toml")


@dataclass(frozen=True, slots=True)
class ResticConfig:
    binary: str = "restic"
    repository: str | None = None
    password_file: Path | None = None
    backup_args: tuple[str, ...] = ()
    host: str | None = None


@dataclass(frozen=True, slots=True)
class BackupConfig:
    state_dir: Path = Path("/var/lib/backupdock")
    stop_timeout_seconds: int = 30
    include_compose_metadata: bool = True
    host_paths: tuple[Path, ...] = ()
    exclude_paths: tuple[Path, ...] = ()
    exclude_volumes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    extra_paths: tuple[Path, ...] = ()
    exclude_paths: tuple[Path, ...] = ()
    exclude_volumes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    after_backup: bool = False
    prune: bool = False
    keep_last: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None

    def configured(self) -> bool:
        return any(
            value is not None
            for value in (
                self.keep_last,
                self.keep_daily,
                self.keep_weekly,
                self.keep_monthly,
                self.keep_yearly,
            )
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    restic: ResticConfig = ResticConfig()
    backup: BackupConfig = BackupConfig()
    retention: RetentionConfig = RetentionConfig()
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def _strings(values: Any, *, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(values)


def _paths(values: Any) -> tuple[Path, ...]:
    return tuple(Path(value).expanduser() for value in _strings(values, field_name="Path lists"))


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Path values must be strings")
    return Path(value).expanduser()


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("Retention values must be positive integers")
    return value


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    explicit_path = path is not None

    if not config_path.exists():
        if explicit_path:
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        return AppConfig()

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    restic_data = data.get("restic", {})
    backup_data = data.get("backup", {})
    retention_data = data.get("retention", {})
    projects_data = data.get("projects", {})

    if not isinstance(projects_data, dict):
        raise ValueError("[projects] must be a TOML table")

    repository = restic_data.get("repository") or os.environ.get("RESTIC_REPOSITORY")
    password_file_raw = restic_data.get("password_file")
    if password_file_raw is None and os.environ.get("RESTIC_PASSWORD_FILE"):
        password_file_raw = os.environ["RESTIC_PASSWORD_FILE"]

    backup_args = restic_data.get("backup_args", [])
    if not isinstance(backup_args, list) or not all(isinstance(value, str) for value in backup_args):
        raise ValueError("restic.backup_args must be an array of strings")

    stop_timeout = backup_data.get("stop_timeout_seconds", 30)
    if not isinstance(stop_timeout, int) or isinstance(stop_timeout, bool) or stop_timeout < 1:
        raise ValueError("backup.stop_timeout_seconds must be a positive integer")

    project_configs: dict[str, ProjectConfig] = {}
    for project_name, project_data in projects_data.items():
        if not isinstance(project_data, dict):
            raise ValueError(f"projects.{project_name} must be a TOML table")
        project_configs[project_name] = ProjectConfig(
            extra_paths=_paths(project_data.get("extra_paths")),
            exclude_paths=_paths(project_data.get("exclude_paths")),
            exclude_volumes=_strings(
                project_data.get("exclude_volumes"),
                field_name=f"projects.{project_name}.exclude_volumes",
            ),
        )

    return AppConfig(
        restic=ResticConfig(
            binary=str(restic_data.get("binary", "restic")),
            repository=repository,
            password_file=_optional_path(password_file_raw),
            backup_args=tuple(backup_args),
            host=restic_data.get("host"),
        ),
        backup=BackupConfig(
            state_dir=Path(str(backup_data.get("state_dir", "/var/lib/backupdock"))).expanduser(),
            stop_timeout_seconds=stop_timeout,
            include_compose_metadata=bool(backup_data.get("include_compose_metadata", True)),
            host_paths=_paths(backup_data.get("host_paths")),
            exclude_paths=_paths(backup_data.get("exclude_paths")),
            exclude_volumes=_strings(
                backup_data.get("exclude_volumes"),
                field_name="backup.exclude_volumes",
            ),
        ),
        retention=RetentionConfig(
            after_backup=bool(retention_data.get("after_backup", False)),
            prune=bool(retention_data.get("prune", False)),
            keep_last=_optional_int(retention_data.get("keep_last")),
            keep_daily=_optional_int(retention_data.get("keep_daily")),
            keep_weekly=_optional_int(retention_data.get("keep_weekly")),
            keep_monthly=_optional_int(retention_data.get("keep_monthly")),
            keep_yearly=_optional_int(retention_data.get("keep_yearly")),
        ),
        projects=project_configs,
    )
