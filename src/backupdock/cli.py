from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

from backupdock import __version__
from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, load_config
from backupdock.discovery import DiscoveryError, discover_groups, host_sources
from backupdock.docker_backend import DockerBackend
from backupdock.locking import LockError, ProcessLock
from backupdock.models import BackupSource
from backupdock.remote import RemoteBackupController, RemoteBackupError
from backupdock.restic import ResticError, ResticRunner


class BackupInterrupted(RuntimeError):
    pass


def _signal_handler(signum, frame) -> None:
    del frame
    raise BackupInterrupted(f"Interrupted by signal {signum}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backupdock",
        description="Docker-aware, consistent backups powered by Restic.",
    )
    parser.add_argument("--config", type=Path, help="Configuration file (default: /etc/backupdock/config.yaml)")
    parser.add_argument("--version", action="version", version=f"BackupDock {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory", help="Show discovered backup groups and sources")

    backup_parser = subparsers.add_parser("backup", help="Back up all groups sequentially")
    backup_parser.add_argument("--project", action="append", default=[], help="Back up only this Compose project or standalone group name")
    backup_parser.add_argument("--dry-run", action="store_true", help="Run the normal backup workflow but print mutating actions instead of executing them")

    remote_parser = subparsers.add_parser(
        "remote-backup",
        help="Start a source-host backup through a temporary reverse SSH tunnel",
    )
    remote_parser.add_argument("remote", help="Remote name from the remotes configuration")
    remote_parser.add_argument("--project", action="append", default=[], help="Back up only this Compose project or standalone group name")
    remote_parser.add_argument("--dry-run", action="store_true", help="Run the source backup in dry-run mode")

    source_parser = subparsers.add_parser(
        "source-backup",
        help="Source-side entry point used by remote-backup",
    )
    source_parser.add_argument("--repository", required=True, help=argparse.SUPPRESS)
    source_parser.add_argument("--project", action="append", default=[], help=argparse.SUPPRESS)
    source_parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser("init", help="Initialize the configured Restic repository")
    subparsers.add_parser("snapshots", help="List BackupDock Restic snapshots")
    subparsers.add_parser("check", help="Run restic check")

    forget_parser = subparsers.add_parser("forget", help="Apply the configured retention policy")
    forget_parser.add_argument("--prune", action="store_true", help="Prune unreferenced data after forgetting snapshots")
    return parser


def _source_detail(source: BackupSource) -> str:
    detail = ""
    if source.volume_name:
        detail = f" volume={source.volume_name}"
    if source.destination:
        detail += f" -> {source.destination}"
    return detail


def _inventory(groups, host_path_sources) -> None:
    if not groups:
        print("No Docker containers found.")
    for group in groups:
        group_type = "compose" if group.compose_project else "standalone"
        print(f"[{group_type}] {group.name}")
        for container in group.containers:
            state = "running" if container.running else "stopped"
            service = f" service={container.compose_service}" if container.compose_service else ""
            dependencies = (
                f" dependencies={','.join(container.compose_dependencies)}"
                if container.compose_dependencies
                else ""
            )
            print(f"  container  {container.name} ({state}){service}{dependencies}")
        if not group.sources and not group.excluded_sources:
            print("  source     (none)")
        for source in group.sources:
            print(f"  {source.kind:<10} {source.path}{_source_detail(source)}")
        for source in group.excluded_sources:
            print(f"  {source.kind:<10} {source.path}{_source_detail(source)} [excluded]")
        print()

    if host_path_sources:
        print("[host]")
        for source in host_path_sources:
            print(f"  host       {source.path}")


def _select_groups(groups, requested: list[str]):
    if not requested:
        return groups
    requested_set = set(requested)
    selected = [
        group
        for group in groups
        if group.name in requested_set or group.key in requested_set
    ]
    found = {group.name for group in selected} | {group.key for group in selected}
    missing = [value for value in requested if value not in found]
    if missing:
        raise ValueError("Unknown project/group: " + ", ".join(missing))
    return selected


def _discover(config: AppConfig, docker: DockerBackend):
    groups = discover_groups(docker.list_containers(), config)
    return groups, host_sources(config, groups)


def _install_signal_handlers() -> None:
    for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, _signal_handler)


def _run_backup(config: AppConfig, projects: list[str], *, dry_run: bool) -> None:
    restic = ResticRunner(config.restic, dry_run=dry_run)
    docker = DockerBackend(dry_run=dry_run)
    try:
        groups, host_path_sources = _discover(config, docker)
        selected_groups = _select_groups(groups, projects)
        selected_host_sources = host_path_sources if not projects else []
        _install_signal_handlers()

        lock_path = config.backup.state_dir / "backupdock.lock"
        with ProcessLock(lock_path):
            BackupOrchestrator(
                docker,
                restic,
                config,
                dry_run=dry_run,
            ).run(selected_groups, selected_host_sources)
    finally:
        docker.close()


def _read_source_password() -> str:
    if sys.stdin.isatty():
        raise ValueError("source-backup requires the Restic password on standard input")
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise ValueError("source-backup received an empty Restic password")
    return password


def _run_source_backup(config: AppConfig, repository: str, projects: list[str], *, dry_run: bool) -> None:
    password = _read_source_password()
    source_config = replace(
        config,
        restic=replace(config.restic, repository=repository, password_file=None),
        retention=replace(config.retention, after_backup=False, prune=False),
    )

    secret_keys = ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")
    previous = {key: os.environ.get(key) for key in secret_keys}
    try:
        os.environ["RESTIC_PASSWORD"] = password
        os.environ.pop("RESTIC_PASSWORD_FILE", None)
        os.environ.pop("RESTIC_PASSWORD_COMMAND", None)
        _run_backup(source_config, projects, dry_run=dry_run)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        config = load_config(args.config)

        if args.command == "remote-backup":
            remote_config = config.remotes.get(args.remote)
            if remote_config is None:
                raise ValueError(f"Unknown remote: {args.remote}")
            RemoteBackupController(remote_config).run(args.project, dry_run=bool(args.dry_run))
            return 0

        if args.command == "source-backup":
            _run_source_backup(
                config,
                args.repository,
                args.project,
                dry_run=bool(args.dry_run),
            )
            return 0

        if args.command == "backup":
            _run_backup(config, args.project, dry_run=bool(args.dry_run))
            return 0

        restic = ResticRunner(config.restic)
        if args.command == "init":
            restic.init()
            return 0
        if args.command == "snapshots":
            restic.snapshots()
            return 0
        if args.command == "check":
            restic.check()
            return 0
        if args.command == "forget":
            restic.forget(config.retention, prune=True if args.prune else None)
            return 0

        docker = DockerBackend()
        try:
            groups, host_path_sources = _discover(config, docker)
            if args.command == "inventory":
                _inventory(groups, host_path_sources)
                return 0
        finally:
            docker.close()

        raise ValueError(f"Unsupported command: {args.command}")

    except (
        BackupInterrupted,
        DiscoveryError,
        FileNotFoundError,
        LockError,
        RemoteBackupError,
        ResticError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"backupdock: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
