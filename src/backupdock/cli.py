from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from backupdock import __version__
from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, load_config
from backupdock.discovery import DiscoveryError, discover_groups, host_sources
from backupdock.docker_backend import DockerBackend
from backupdock.locking import LockError, ProcessLock
from backupdock.models import BackupSource
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
    backup_parser.add_argument("--dry-run", action="store_true", help="Show the backup plan without stopping containers or running Restic")

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
            print(f"  container  {container.name} ({state}){service}")
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


def _print_section(name: str, values: list[str]) -> None:
    print(f"  {name}:")
    if values:
        for value in values:
            print(f"    {value}")
    else:
        print("    (none)")


def _dry_run(groups, host_path_sources) -> None:
    print("Backup plan (dry-run)")
    print()

    if not groups and not host_path_sources:
        print("Nothing to back up.")
        return

    for group in groups:
        group_type = "compose" if group.compose_project else "standalone"
        print(f"[{group_type}] {group.name}")

        has_persistent_data = any(source.persistent for source in group.sources)
        running = group.running_containers if has_persistent_data else []
        running_names = [container.name for container in running]

        _print_section("stop", running_names)
        _print_section(
            "backup",
            [f"{source.kind:<10} {source.path}{_source_detail(source)}" for source in group.sources],
        )
        if group.excluded_sources:
            _print_section(
                "excluded",
                [f"{source.kind:<10} {source.path}{_source_detail(source)}" for source in group.excluded_sources],
            )
        _print_section("restart", list(reversed(running_names)))
        print()

    if host_path_sources:
        print("[host]")
        _print_section("backup", [str(source.path) for source in host_path_sources])
        print()

    print("Dry-run only: no containers were stopped and Restic was not executed.")


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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        config = load_config(args.config)
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

            selected_groups = _select_groups(groups, args.project)
            selected_host_sources = host_path_sources if not args.project else []

            if args.dry_run:
                _dry_run(selected_groups, selected_host_sources)
                return 0

            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)

            lock_path = config.backup.state_dir / "backupdock.lock"
            with ProcessLock(lock_path):
                BackupOrchestrator(docker, restic, config).run(selected_groups, selected_host_sources)
            return 0
        finally:
            docker.close()

    except (BackupInterrupted, DiscoveryError, FileNotFoundError, LockError, ResticError, RuntimeError, ValueError) as exc:
        print(f"backupdock: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
