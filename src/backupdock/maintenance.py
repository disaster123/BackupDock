from __future__ import annotations

import re
import shlex
from dataclasses import replace
from pathlib import Path

from backupdock.config import AppConfig, ResticConfig


def _backup_directory(config_file: Path) -> Path:
    try:
        content = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read rest-server configuration {config_file}: {exc}") from exc
    values = []
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        # An ARGS override would invalidate the BACKUP_DIR-derived location.
        if re.search(r"--path(?:\s|=|[\"']|$)", line):
            raise ValueError("rest-server --path override cannot be auto-resolved; configure restic.repository explicitly")
        match = re.fullmatch(r"\s*(?:export\s+)?BACKUP_DIR\s*=\s*(.*?)\s*", line)
        if match:
            try:
                parts = shlex.split(match.group(1), comments=True)
            except ValueError as exc:
                raise ValueError(f"Invalid BACKUP_DIR in {config_file}: {exc}") from exc
            if len(parts) != 1 or "$" in parts[0] or "`" in parts[0]:
                raise ValueError(f"BACKUP_DIR in {config_file} must be one literal absolute path; shell expansion is not supported")
            values.append(parts[0])
    if len(values) != 1 or not Path(values[0]).is_absolute():
        raise ValueError(f"Expected exactly one absolute BACKUP_DIR in {config_file}")
    return Path(values[0])


def maintenance_config(config: AppConfig, remote_name: str | None = None) -> ResticConfig:
    """Resolve controller-local maintenance access without executing service config."""
    if remote_name is None:
        if config.restic.repository or not config.remotes:
            return config.restic
        if len(config.remotes) != 1:
            raise ValueError("Multiple remotes configured: select the maintenance repository with --remote NAME")
        remote_name = next(iter(config.remotes))
    remote = config.remotes.get(remote_name)
    if remote is None:
        raise ValueError(f"Unknown remote: {remote_name}")
    if remote.local_rest_server_host not in ("127.0.0.1", "::1", "[::1]", "localhost"):
        raise ValueError("Automatic local maintenance requires a loopback rest-server; configure restic.repository explicitly")
    relative = remote.repository_path.lstrip("/")
    if not relative or ".." in relative.split("/"):
        raise ValueError(f"Unsafe repository_path for remote {remote_name}")
    root = _backup_directory(config.restic.rest_server_config_file).resolve()
    repository = (root / relative).resolve()
    if repository == root or not repository.is_relative_to(root):
        raise ValueError(f"Repository for remote {remote_name} is outside BACKUP_DIR")
    if not (repository / "config").is_file():
        raise ValueError(f"Restic repository config not found: {repository / 'config'}; check BACKUP_DIR and repository_path")
    return replace(config.restic, repository=str(repository), password_file=remote.password_file)
