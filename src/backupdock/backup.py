from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict
from pathlib import Path

from backupdock.config import AppConfig
from backupdock.discovery import paths_overlap
from backupdock.models import BackupGroup, BackupSource
from backupdock.ordering import DependencyOrderError, running_containers_in_stop_order


logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    pass


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_." else "_" for character in value)


def _usable_source(source: BackupSource) -> bool:
    try:
        mode = source.path.stat().st_mode
    except FileNotFoundError:
        if source.required:
            raise BackupError(f"Required backup source does not exist: {source.path}")
        return False
    except OSError as exc:
        if source.required:
            raise BackupError(f"Cannot stat backup source {source.path}: {exc}") from exc
        return False

    if stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        return True

    if source.kind == "bind":
        logger.warning("Skipping non-file bind mount source: %s", source.path)
        return False

    if source.required:
        raise BackupError(f"Unsupported backup source type: {source.path}")
    return False


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _minimal_backup_paths(sources: list[BackupSource]) -> list[Path]:
    paths = sorted({_absolute_path(source.path) for source in sources}, key=lambda path: (len(path.parts), str(path)))
    minimal: list[Path] = []
    for path in paths:
        if any(path == parent or parent in path.parents for parent in minimal):
            continue
        minimal.append(path)
    return minimal


class BackupOrchestrator:
    def __init__(
        self,
        docker_backend,
        restic_runner,
        config: AppConfig,
        *,
        dry_run: bool = False,
        preseed: bool = False,
    ) -> None:
        self.docker = docker_backend
        self.restic = restic_runner
        self.config = config
        self.dry_run = dry_run
        self.preseed = preseed

    def _status(self, message: str) -> None:
        if not self.dry_run:
            print(message, flush=True)

    @staticmethod
    def _container_label(container) -> str:
        service = f" (service={container.compose_service})" if container.compose_service else ""
        return f"{container.name}{service}"

    def _write_manifest(self, group: BackupGroup) -> Path:
        manifest_dir = self.config.backup.state_dir / "manifests"
        manifest_path = manifest_dir / f"{_slug(group.key)}.json"
        payload = {
            "schema": 1,
            "group": {
                "key": group.key,
                "name": group.name,
                "compose_project": group.compose_project,
            },
            "containers": [asdict(container) for container in group.containers],
            "sources": [
                {
                    **asdict(source),
                    "path": str(source.path),
                }
                for source in group.sources
            ],
        }

        if self.dry_run:
            print(f"DRY-RUN write manifest {manifest_path}")
            return manifest_path

        manifest_dir.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, manifest_path)
        return manifest_path

    def _restart(self, group_key: str, restart_candidates: list) -> list[str]:
        errors: list[str] = []
        for container in reversed(restart_candidates):
            self._status(
                f"[{group_key}] ensuring container {self._container_label(container)} is running"
            )
            try:
                self.docker.ensure_running(container.id)
            except Exception as exc:
                errors.append(f"{container.id[:12]}: {exc}")
        return errors

    def _validate_safety(
        self,
        selected_groups: list[BackupGroup],
        host_sources: list[BackupSource],
        observed_groups: list[BackupGroup],
    ) -> None:
        auto_remove: list[tuple[str, str]] = []
        for group in selected_groups:
            if not any(source.persistent for source in group.sources):
                continue
            for container in group.containers:
                if container.running and not container.ignored and container.auto_remove:
                    auto_remove.append((group.name, container.name))

        if auto_remove:
            lines = [
                "Cannot safely stop running auto-remove containers (--rm/AutoRemove=true):"
            ]
            for group_name, container_name in sorted(auto_remove):
                lines.append(f"  project={group_name} container={container_name}")
            lines.append(
                "Stopping these containers would remove them and may remove anonymous volumes. "
                "Configure the exact container name in backup.ignore_containers only when it is safe to leave it running."
            )
            raise BackupError("\n".join(lines))

        backed_sources = [
            (group.name, source)
            for group in selected_groups
            for source in group.sources
            if source.persistent
        ]
        backed_sources.extend(("host", source) for source in host_sources)

        conflicts: set[tuple[str, str, str, str]] = set()
        for group in observed_groups:
            running_ignored = {
                container.name
                for container in group.containers
                if container.running and container.ignored
            }
            for ignored_source in group.ignored_sources:
                if ignored_source.container not in running_ignored or ignored_source.read_only:
                    continue
                for backed_group, backed_source in backed_sources:
                    if paths_overlap(ignored_source.path, backed_source.path):
                        conflicts.add(
                            (
                                ignored_source.container or "unknown",
                                str(ignored_source.path),
                                backed_group,
                                str(backed_source.path),
                            )
                        )

        if conflicts:
            lines = [
                "Ignored running containers have writable storage overlapping selected backup data:"
            ]
            for container_name, ignored_path, group_name, backup_path in sorted(conflicts):
                lines.append(
                    f"  container={container_name} path={ignored_path} <-> "
                    f"backup={group_name} path={backup_path}"
                )
            lines.append(
                "BackupDock will not create a supposedly consistent snapshot while an ignored container can modify the data."
            )
            raise BackupError("\n".join(lines))

    def backup_group(self, group: BackupGroup, *, auto_preseed: bool | None = None) -> None:
        self._status(f"[{group.key}] preparing")
        available_sources = [source for source in group.sources if _usable_source(source)]
        manifest_path = self._write_manifest(group)
        paths = _minimal_backup_paths(available_sources + [BackupSource(manifest_path, "manifest")])

        if self.dry_run:
            print(f"DRY-RUN backup group {group.key}")
            for source in group.excluded_sources:
                detail = f" volume={source.volume_name}" if source.volume_name else ""
                destination = f" -> {source.destination}" if source.destination else ""
                print(f"DRY-RUN excluded {source.kind} {source.path}{detail}{destination}")
            for container in group.containers:
                if container.ignored:
                    print(f"DRY-RUN ignored container {container.name}")

        has_persistent_data = any(source.persistent for source in available_sources)
        try:
            running = running_containers_in_stop_order(group.containers) if has_persistent_data else []
        except DependencyOrderError as exc:
            raise BackupError(f"Cannot determine safe container order for project {group.name}: {exc}") from exc

        should_preseed = self.preseed or auto_preseed is True
        if should_preseed and running:
            if self.dry_run:
                reason = "forced" if self.preseed else "automatic-no-consistent-snapshot"
                print(
                    f"DRY-RUN preseed backup group {group.key} while containers remain running "
                    f"reason={reason}"
                )
            elif self.preseed:
                self._status(
                    f"[{group.key}] starting preseed; containers remain running (forced)"
                )
            else:
                self._status(
                    f"[{group.key}] no consistent snapshot found; starting automatic preseed; "
                    "containers remain running"
                )
            self.restic.backup(paths, group.key, preseed=True)
        elif running and auto_preseed is False:
            self._status(f"[{group.key}] consistent snapshot found; preseed not needed")

        restart_candidates: list = []
        primary_error: BaseException | None = None

        try:
            for container in running:
                restart_candidates.append(container)
                self._status(
                    f"[{group.key}] stopping container {self._container_label(container)}"
                )
                self.docker.stop(container.id)
            phase = "consistent backup" if running else "backup"
            self._status(f"[{group.key}] starting {phase}")
            self.restic.backup(paths, group.key)
        except BaseException as exc:
            primary_error = exc
        finally:
            restart_errors = self._restart(group.key, restart_candidates)

        if restart_errors:
            message = "Failed to restore the original running state: " + "; ".join(restart_errors)
            if primary_error is not None:
                message += f". Original backup error: {primary_error}"
            raise BackupError(message) from primary_error
        if primary_error is not None:
            raise primary_error
        self._status(f"[{group.key}] complete")

    def backup_host_paths(self, sources: list[BackupSource]) -> None:
        available = [source for source in sources if _usable_source(source)]
        if not available:
            return
        self._status("[host] starting backup")
        self.restic.backup(_minimal_backup_paths(available), "host")
        self._status("[host] complete")

    def run(
        self,
        groups: list[BackupGroup],
        host_sources: list[BackupSource],
        *,
        observed_groups: list[BackupGroup] | None = None,
    ) -> None:
        self._status("BackupDock: validating backup safety")
        self._validate_safety(groups, host_sources, observed_groups or groups)
        self._status("BackupDock: checking Restic repository")
        consistent_groups = self.restic.preflight()
        self._status("BackupDock: Restic repository ready")
        if self.dry_run and consistent_groups is None and not self.preseed:
            print(
                "DRY-RUN automatic preseed decision unavailable: repository state is not queried "
                "in dry-run; a real backup will automatically preseed groups without a "
                "consistent snapshot"
            )
        for group in groups:
            auto_preseed = None if consistent_groups is None else group.key not in consistent_groups
            self.backup_group(group, auto_preseed=auto_preseed)
        self.backup_host_paths(host_sources)
        if self.config.retention.after_backup and self.config.retention.configured():
            self._status("BackupDock: applying retention policy")
            self.restic.forget(self.config.retention)
        self._status("BackupDock: backup run complete")
