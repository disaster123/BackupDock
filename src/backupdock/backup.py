from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict
from pathlib import Path

from backupdock.config import AppConfig
from backupdock.models import BackupGroup, BackupSource


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


def _minimal_backup_paths(sources: list[BackupSource]) -> list[Path]:
    paths = sorted({source.path.resolve() for source in sources}, key=lambda path: (len(path.parts), str(path)))
    minimal: list[Path] = []
    for path in paths:
        if any(path == parent or parent in path.parents for parent in minimal):
            continue
        minimal.append(path)
    return minimal


class BackupOrchestrator:
    def __init__(self, docker_backend, restic_runner, config: AppConfig) -> None:
        self.docker = docker_backend
        self.restic = restic_runner
        self.config = config

    def _write_manifest(self, group: BackupGroup) -> Path:
        manifest_dir = self.config.backup.state_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
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
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, manifest_path)
        return manifest_path

    def _restart(self, restart_candidates: list[str]) -> list[str]:
        errors: list[str] = []
        for container_id in reversed(restart_candidates):
            try:
                self.docker.ensure_running(container_id)
            except Exception as exc:
                errors.append(f"{container_id[:12]}: {exc}")
        return errors

    def backup_group(self, group: BackupGroup) -> None:
        available_sources = [source for source in group.sources if _usable_source(source)]
        manifest_path = self._write_manifest(group)
        paths = _minimal_backup_paths(available_sources + [BackupSource(manifest_path, "manifest")])

        has_persistent_data = any(source.persistent for source in available_sources)
        running = group.running_containers if has_persistent_data else []
        restart_candidates: list[str] = []
        primary_error: BaseException | None = None

        try:
            for container in running:
                restart_candidates.append(container.id)
                self.docker.stop(container.id, self.config.backup.stop_timeout_seconds)
            self.restic.backup(paths, group.key)
        except BaseException as exc:
            primary_error = exc
        finally:
            restart_errors = self._restart(restart_candidates)

        if restart_errors:
            message = "Failed to restore the original running state: " + "; ".join(restart_errors)
            if primary_error is not None:
                message += f". Original backup error: {primary_error}"
            raise BackupError(message) from primary_error
        if primary_error is not None:
            raise primary_error

    def backup_host_paths(self, sources: list[BackupSource]) -> None:
        available = [source for source in sources if _usable_source(source)]
        if not available:
            return
        self.restic.backup(_minimal_backup_paths(available), "host")

    def run(self, groups: list[BackupGroup], host_sources: list[BackupSource]) -> None:
        self.restic.preflight()
        for group in groups:
            self.backup_group(group)
        self.backup_host_paths(host_sources)
        if self.config.retention.after_backup and self.config.retention.configured():
            self.restic.forget(self.config.retention)
