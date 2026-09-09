from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("/etc/backupdock/config.yaml")
LEGACY_CONFIG_PATH = Path("/etc/backupdock/config.toml")


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


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _strings(values: Any, *, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(values)


def _paths(values: Any, *, field_name: str) -> tuple[Path, ...]:
    return tuple(Path(value).expanduser() for value in _strings(values, field_name=field_name))


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


def _boolean(value: Any, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be true or false")
    return value


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    explicit_path = path is not None

    if not config_path.exists():
        if explicit_path:
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        if LEGACY_CONFIG_PATH.exists():
            raise RuntimeError(
                f"Legacy configuration found at {LEGACY_CONFIG_PATH}. "
                f"BackupDock 0.2+ uses YAML; migrate it to {DEFAULT_CONFIG_PATH}."
            )
        return AppConfig()

    with config_path.open("r", encoding="utf-8") as handle:
        raw_data = yaml.safe_load(handle)

    data = _mapping(raw_data, field_name="Configuration root")
    restic_data = _mapping(data.get("restic"), field_name="restic")
    backup_data = _mapping(data.get("backup"), field_name="backup")
    retention_data = _mapping(data.get("retention"), field_name="retention")
    projects_data = _mapping(data.get("projects"), field_name="projects")

    repository = restic_data.get("repository") or os.environ.get("RESTIC_REPOSITORY")
    if repository is not None and not isinstance(repository, str):
        raise ValueError("restic.repository must be a string")

    password_file_raw = restic_data.get("password_file")
    if password_file_raw is None and os.environ.get("RESTIC_PASSWORD_FILE"):
        password_file_raw = os.environ["RESTIC_PASSWORD_FILE"]

    backup_args = _strings(restic_data.get("backup_args"), field_name="restic.backup_args")

    stop_timeout = backup_data.get("stop_timeout_seconds", 30)
    if not isinstance(stop_timeout, int) or isinstance(stop_timeout, bool) or stop_timeout < 1:
        raise ValueError("backup.stop_timeout_seconds must be a positive integer")

    project_configs: dict[str, ProjectConfig] = {}
    for project_name, project_raw in projects_data.items():
        project_data = _mapping(project_raw, field_name=f"projects.{project_name}")
        project_configs[project_name] = ProjectConfig(
            extra_paths=_paths(
                project_data.get("extra_paths"),
                field_name=f"projects.{project_name}.extra_paths",
            ),
            exclude_paths=_paths(
                project_data.get("exclude_paths"),
                field_name=f"projects.{project_name}.exclude_paths",
            ),
            exclude_volumes=_strings(
                project_data.get("exclude_volumes"),
                field_name=f"projects.{project_name}.exclude_volumes",
            ),
        )

    binary = restic_data.get("binary", "restic")
    if not isinstance(binary, str):
        raise ValueError("restic.binary must be a string")

    host = restic_data.get("host")
    if host is not None and not isinstance(host, str):
        raise ValueError("restic.host must be a string")

    state_dir_raw = backup_data.get("state_dir", "/var/lib/backupdock")
    if not isinstance(state_dir_raw, str):
        raise ValueError("backup.state_dir must be a string")

    return AppConfig(
        restic=ResticConfig(
            binary=binary,
            repository=repository,
            password_file=_optional_path(password_file_raw),
            backup_args=backup_args,
            host=host,
        ),
        backup=BackupConfig(
            state_dir=Path(state_dir_raw).expanduser(),
            stop_timeout_seconds=stop_timeout,
            include_compose_metadata=_boolean(
                backup_data.get("include_compose_metadata"),
                field_name="backup.include_compose_metadata",
                default=True,
            ),
            host_paths=_paths(backup_data.get("host_paths"), field_name="backup.host_paths"),
            exclude_paths=_paths(backup_data.get("exclude_paths"), field_name="backup.exclude_paths"),
            exclude_volumes=_strings(
                backup_data.get("exclude_volumes"),
                field_name="backup.exclude_volumes",
            ),
        ),
        retention=RetentionConfig(
            after_backup=_boolean(
                retention_data.get("after_backup"),
                field_name="retention.after_backup",
                default=False,
            ),
            prune=_boolean(
                retention_data.get("prune"),
                field_name="retention.prune",
                default=False,
            ),
            keep_last=_optional_int(retention_data.get("keep_last")),
            keep_daily=_optional_int(retention_data.get("keep_daily")),
            keep_weekly=_optional_int(retention_data.get("keep_weekly")),
            keep_monthly=_optional_int(retention_data.get("keep_monthly")),
            keep_yearly=_optional_int(retention_data.get("keep_yearly")),
        ),
        projects=project_configs,
    )
