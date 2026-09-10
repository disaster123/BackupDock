from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from backupdock import __version__
from backupdock.backup import BackupOrchestrator
from backupdock.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from backupdock.discovery import DiscoveryError, discover_groups, host_sources
from backupdock.docker_backend import DockerBackend
from backupdock.locking import LockError, ProcessLock
from backupdock.models import BackupSource
from backupdock.remote import REMOTE_PROTOCOL_VERSION, RemoteBackupController, RemoteBackupError
from backupdock.restic import ResticError, ResticRunner


SOURCE_RUNTIME_DIR = Path("/run/backupdock")


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

    source_info_parser = subparsers.add_parser("source-info", help=argparse.SUPPRESS)
    source_info_parser.set_defaults(_internal_source_command=True)

    source_parser = subparsers.add_parser("source-backup", help=argparse.SUPPRESS)
    source_parser.set_defaults(_internal_source_command=True)
    source_parser.add_argument("--project", action="append", default=[], help=argparse.SUPPRESS)
    source_parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    init_parser = subparsers.add_parser("init", help="Initialize a local or remote Restic repository")
    init_parser.add_argument(
        "--remote",
        help="Initialize the repository configured for this remote on the controller",
    )
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


def _read_source_payload() -> tuple[str, str, str, str]:
    if sys.stdin.isatty():
        raise ValueError("source-backup requires a controller payload on standard input")
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        raise ValueError("source-backup received invalid controller payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("source-backup controller payload must be an object")

    if payload.get("version") != __version__:
        raise ValueError(
            "Controller BackupDock version mismatch: "
            f"controller={payload.get('version')!r}, source={__version__}"
        )
    if payload.get("protocol") != REMOTE_PROTOCOL_VERSION:
        raise ValueError(
            "Controller BackupDock protocol mismatch: "
            f"controller={payload.get('protocol')!r}, source={REMOTE_PROTOCOL_VERSION}"
        )

    repository_password = payload.get("repository_password")
    rest_server_username = payload.get("rest_server_username")
    rest_server_password = payload.get("rest_server_password")
    config_yaml = payload.get("config_yaml")
    if not isinstance(repository_password, str) or not repository_password:
        raise ValueError("source-backup received an empty Restic repository password")
    if not isinstance(rest_server_username, str) or not rest_server_username:
        raise ValueError("source-backup received an empty rest-server username")
    if not isinstance(rest_server_password, str) or not rest_server_password:
        raise ValueError("source-backup received an empty rest-server password")
    if not isinstance(config_yaml, str) or not config_yaml.strip():
        raise ValueError("source-backup received an empty source configuration")
    return repository_password, rest_server_username, rest_server_password, config_yaml


def _write_source_session_config(config_yaml: str) -> tuple[Path, Path]:
    SOURCE_RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SOURCE_RUNTIME_DIR, 0o700)
    session_dir = Path(tempfile.mkdtemp(prefix="session-", dir=SOURCE_RUNTIME_DIR))
    os.chmod(session_dir, 0o700)
    config_path = session_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    os.chmod(config_path, 0o600)
    return session_dir, config_path


def _run_source_backup(projects: list[str], *, dry_run: bool) -> None:
    repository_password, rest_server_username, rest_server_password, config_yaml = (
        _read_source_payload()
    )

    if DEFAULT_CONFIG_PATH.exists():
        print(
            f"backupdock: warning: {DEFAULT_CONFIG_PATH} exists on the source host but is ignored "
            "for this remote backup; using the temporary controller-provided configuration",
            file=sys.stderr,
        )

    session_dir, config_path = _write_source_session_config(config_yaml)
    secret_keys = (
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_PASSWORD_COMMAND",
        "RESTIC_REST_USERNAME",
        "RESTIC_REST_PASSWORD",
    )
    previous = {key: os.environ.get(key) for key in secret_keys}
    try:
        source_config = load_config(config_path, use_environment=False)
        os.environ["RESTIC_PASSWORD"] = repository_password
        os.environ.pop("RESTIC_PASSWORD_FILE", None)
        os.environ.pop("RESTIC_PASSWORD_COMMAND", None)
        os.environ["RESTIC_REST_USERNAME"] = rest_server_username
        os.environ["RESTIC_REST_PASSWORD"] = rest_server_password
        _run_backup(source_config, projects, dry_run=dry_run)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(session_dir, ignore_errors=True)


def _source_info() -> None:
    print(json.dumps({"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION}))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        if args.command == "source-info":
            _source_info()
            return 0

        if args.command == "source-backup":
            _run_source_backup(args.project, dry_run=bool(args.dry_run))
            return 0

        config = load_config(args.config)

        if args.command == "remote-backup":
            remote_config = config.remotes.get(args.remote)
            if remote_config is None:
                raise ValueError(f"Unknown remote: {args.remote}")
            RemoteBackupController(config, remote_config).run(
                args.project,
                dry_run=bool(args.dry_run),
            )
            return 0

        if args.command == "backup":
            _run_backup(config, args.project, dry_run=bool(args.dry_run))
            return 0

        if args.command == "init" and args.remote:
            remote_config = config.remotes.get(args.remote)
            if remote_config is None:
                raise ValueError(f"Unknown remote: {args.remote}")
            RemoteBackupController(config, remote_config).init_repository()
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
