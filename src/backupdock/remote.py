from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote

from backupdock import __version__
from backupdock.config import AppConfig, RemoteConfig, ResticConfig, render_source_config
from backupdock.restic import ResticRunner


REMOTE_PROTOCOL_VERSION = 2


class RemoteBackupError(RuntimeError):
    pass


def _encoded_repository_path(config: RemoteConfig) -> str:
    path = "/" + config.repository_path.lstrip("/")
    return quote(path, safe="/-._~")


def repository_url(config: RemoteConfig) -> str:
    return f"rest:http://127.0.0.1:{config.remote_tunnel_port}{_encoded_repository_path(config)}"


def controller_repository_url(config: RemoteConfig) -> str:
    host = config.local_rest_server_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"rest:http://{host}:{config.local_rest_server_port}{_encoded_repository_path(config)}"


def _read_secret(path: Path, *, description: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RemoteBackupError(f"{description} file not found: {path}") from exc
    except OSError as exc:
        raise RemoteBackupError(f"Cannot read {description} file {path}: {exc}") from exc

    secret = content.splitlines()[0] if content.splitlines() else ""
    if not secret:
        raise RemoteBackupError(f"{description} file is empty: {path}")
    return secret


class RemoteBackupController:
    def __init__(self, app_config: AppConfig, remote_config: RemoteConfig) -> None:
        self.app_config = app_config
        self.config = remote_config

    def _ssh_base(self) -> list[str]:
        return [
            self.config.ssh_binary,
            "-T",
            *self.config.ssh_options,
            self.config.ssh_target,
        ]

    def info_command(self) -> list[str]:
        return [*self._ssh_base(), *self.config.source_command, "source-info"]

    def command(self, projects: list[str], *, dry_run: bool, preseed: bool = False) -> list[str]:
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
        ]
        for project in projects:
            command.extend(["--project", project])
        if preseed:
            command.append("--preseed")
        if dry_run:
            command.append("--dry-run")
        return command

    def _check_remote_version(self) -> None:
        command = self.info_command()
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RemoteBackupError(f"SSH binary not found: {self.config.ssh_binary}") from exc
        except OSError as exc:
            raise RemoteBackupError(f"Cannot start SSH command: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise RemoteBackupError(
                f"Cannot query remote BackupDock version; SSH exit code {result.returncode}{suffix}"
            )

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RemoteBackupError("Remote source-info returned invalid JSON") from exc

        remote_version = info.get("version")
        remote_protocol = info.get("protocol")
        if remote_version != __version__:
            raise RemoteBackupError(
                "Remote BackupDock version mismatch: "
                f"controller={__version__}, source={remote_version or 'unknown'}. "
                "Update BackupDock on both hosts before running a remote backup."
            )
        if remote_protocol != REMOTE_PROTOCOL_VERSION:
            raise RemoteBackupError(
                "Remote BackupDock protocol mismatch: "
                f"controller={REMOTE_PROTOCOL_VERSION}, source={remote_protocol!r}"
            )

    def _payload(self, *, dry_run: bool) -> str:
        repository_password = (
            "dry-run"
            if dry_run
            else _read_secret(
                self.config.password_file,
                description="Restic repository password",
            )
        )
        rest_server_password = (
            "dry-run"
            if dry_run
            else _read_secret(
                self.config.rest_server_password_file,
                description="rest-server password",
            )
        )
        source_yaml = render_source_config(
            self.app_config,
            self.config,
            repository=repository_url(self.config),
        )
        return json.dumps(
            {
                "version": __version__,
                "protocol": REMOTE_PROTOCOL_VERSION,
                "repository_password": repository_password,
                "rest_server_username": self.config.rest_server_username,
                "rest_server_password": rest_server_password,
                "config_yaml": source_yaml,
            }
        ) + "\n"

    def init_repository(self) -> None:
        _read_secret(
            self.config.password_file,
            description="Restic repository password",
        )
        rest_server_password = _read_secret(
            self.config.rest_server_password_file,
            description="rest-server password",
        )
        restic_config = ResticConfig(
            binary=self.app_config.restic.binary,
            repository=controller_repository_url(self.config),
            password_file=self.config.password_file,
        )
        ResticRunner(
            restic_config,
            env_overrides={
                "RESTIC_REST_USERNAME": self.config.rest_server_username,
                "RESTIC_REST_PASSWORD": rest_server_password,
            },
        ).init()

    def run(
        self,
        projects: list[str],
        *,
        dry_run: bool = False,
        preseed: bool = False,
    ) -> None:
        self._check_remote_version()
        command = self.command(projects, dry_run=dry_run, preseed=preseed)
        payload = self._payload(dry_run=dry_run)

        if dry_run:
            print(f"REMOTE DRY-RUN ssh command: {shlex.join(command)}")

        try:
            result = subprocess.run(
                command,
                input=payload,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RemoteBackupError(f"SSH binary not found: {self.config.ssh_binary}") from exc
        except OSError as exc:
            raise RemoteBackupError(f"Cannot start SSH command: {exc}") from exc

        if result.returncode != 0:
            raise RemoteBackupError(f"Remote backup failed with SSH exit code {result.returncode}")
