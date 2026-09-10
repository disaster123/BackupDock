from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote

from backupdock.config import RemoteConfig


class RemoteBackupError(RuntimeError):
    pass


def repository_url(config: RemoteConfig) -> str:
    path = "/" + config.repository_path.lstrip("/")
    encoded_path = quote(path, safe="/-._~")
    return f"rest:http://127.0.0.1:{config.remote_tunnel_port}{encoded_path}"


def _read_password(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RemoteBackupError(f"Restic password file not found: {path}") from exc
    except OSError as exc:
        raise RemoteBackupError(f"Cannot read Restic password file {path}: {exc}") from exc

    password = content.splitlines()[0] if content.splitlines() else ""
    if not password:
        raise RemoteBackupError(f"Restic password file is empty: {path}")
    return password


class RemoteBackupController:
    def __init__(self, config: RemoteConfig) -> None:
        self.config = config

    def command(self, projects: list[str], *, dry_run: bool) -> list[str]:
        reverse_forward = (
            f"127.0.0.1:{self.config.remote_tunnel_port}:"
            f"{self.config.local_rest_server_host}:{self.config.local_rest_server_port}"
        )
        command = [
            self.config.ssh_binary,
            "-T",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            reverse_forward,
            *self.config.ssh_options,
            self.config.ssh_target,
            *self.config.source_command,
            "source-backup",
            "--repository",
            repository_url(self.config),
        ]
        for project in projects:
            command.extend(["--project", project])
        if dry_run:
            command.append("--dry-run")
        return command

    def run(self, projects: list[str], *, dry_run: bool = False) -> None:
        command = self.command(projects, dry_run=dry_run)
        password = "dry-run" if dry_run else _read_password(self.config.password_file)

        if dry_run:
            print(f"REMOTE DRY-RUN ssh command: {shlex.join(command)}")

        try:
            result = subprocess.run(
                command,
                input=password + "\n",
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RemoteBackupError(f"SSH binary not found: {self.config.ssh_binary}") from exc
        except OSError as exc:
            raise RemoteBackupError(f"Cannot start SSH command: {exc}") from exc

        if result.returncode != 0:
            raise RemoteBackupError(f"Remote backup failed with SSH exit code {result.returncode}")
