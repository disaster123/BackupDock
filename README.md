# BackupDock – Consistent Docker Compose Backups with Restic

[![Tests](https://github.com/disaster123/BackupDock/actions/workflows/tests.yml/badge.svg)](https://github.com/disaster123/BackupDock/actions/workflows/tests.yml)

BackupDock is a Linux command-line tool for consistent, incremental Docker Compose backups with Restic. It automatically discovers bind mounts, Docker volumes, Compose files, `.env` files, and service `env_file` inputs; stops only the affected containers; and restores their previous running state after the backup.

It supports local backups, backup-server-initiated remote backups through a reverse SSH tunnel, large-backup preseeding, retention, repository checks, and isolated Docker Compose restore tests. BackupDock is intentionally not a daemon, scheduler, web UI, or replacement for Restic.

> **Status:** early alpha. Test restores before relying on BackupDock for production data. In-place restore orchestration is not implemented yet.

## Contents

- [Quick Start](#quick-start-create-your-first-docker-backup)
- [Docker backup features](#docker-compose-backup-features)
- [What is backed up](#what-backupdock-backs-up)
- [Local or remote mode](#local-or-remote-docker-backups)
- [Restore testing](#test-a-docker-compose-restore)
- [Documentation](#documentation)

## Quick Start: Create Your First Docker Backup

Install BackupDock:

```bash
git clone https://github.com/disaster123/BackupDock.git
cd BackupDock
sudo ./install.sh
```

Create `/etc/backupdock/config.yaml` from the installed example and configure a Restic repository and password file:

```bash
sudo cp /etc/backupdock/config.yaml.example /etc/backupdock/config.yaml
sudoedit /etc/backupdock/config.yaml
```

Then initialize the repository, inspect the discovered Docker data, preview the workflow, create the backup, and verify the snapshot:

```bash
sudo backupdock init
sudo backupdock inventory
sudo backupdock backup --dry-run
sudo backupdock backup
sudo backupdock snapshots
```

The snapshot table is the first visible success check. Review the backup output as well to confirm that the backup completed and all previously running containers restarted.

For an exact first-run configuration using a local test repository, follow the [step-by-step BackupDock Quick Start](docs/quick-start.md). For production, use separate storage or [remote/controller mode](docs/remote-backup.md).

## Docker Compose Backup Features

- Automatic bind-mount and Docker-volume discovery
- Compose projects used as consistency groups
- Dependency-aware container stop and restart ordering
- Incremental, compressed, and deduplicated storage through Restic
- Local and backup-server-initiated remote operation
- Optional live preseed pass to reduce first-backup downtime
- Explicit host paths, project paths, exclusions, and ignored containers
- Compact snapshot overview, retention, pruning, and repository checks
- Isolated Compose restore tests using copied data and local-only ports
- Dry-run validation before Docker or backup data is changed

## What BackupDock Backs Up

| Included | Not included |
|---|---|
| Bind mounts and supported Docker volumes | Docker images |
| Compose files and the working-directory `.env` | Container writable filesystem layers |
| Service-level `env_file` inputs | Unsupported plugin, network, NFS, CIFS, or block-device volumes |
| Project-specific extra paths | Data stored outside discovered or configured persistent paths |
| Explicit static host paths | In-place production restore orchestration |
| Backup manifests for discovery and restore mapping | Automatic image archival |

Unsupported or uninspectable selected volumes fail before containers are stopped instead of producing an apparently successful empty backup.

Read [Docker Compose backup behavior](docs/backup-behavior.md) for discovery, grouping, supported volume types, metadata handling, and safety checks.

## Local or Remote Docker Backups

| | Local mode | Remote/controller mode |
|---|---|---|
| Backup starts on | Docker host | Backup server |
| Permanent configuration | Docker host | Backup server only |
| Repository access | Direct | Temporary reverse SSH tunnel |
| Backup command | `backupdock backup` | `backupdock remote-backup NAME` |
| Typical use | Source can reach repository | Controller initiates backups behind firewalls |

Both modes use the same discovery, validation, Stop/Restic/Restart transaction, manifests, exclusions, preseed support, and dry-run logic.

Use the [local Quick Start](docs/quick-start.md) for a first backup or the [remote backup guide](docs/remote-backup.md) for a central controller and append-only rest-server.

## How Consistent Docker Backups Work

For each selected Compose project, BackupDock:

1. discovers containers and persistent data;
2. validates dependencies, sources, exclusions, and storage overlap;
3. records which containers are currently running;
4. stops those containers in reverse dependency order;
5. creates a tagged Restic snapshot and manifest;
6. restarts exactly those containers in dependency order.

Other Compose projects remain online. Containers that were already stopped remain stopped. Recovery is attempted after backup errors and handled interruption signals.

## Everyday Commands

| Task | Command |
|---|---|
| Inspect discovered projects and data | `backupdock inventory` |
| Preview a local backup | `backupdock backup --dry-run` |
| Run a local backup | `backupdock backup` |
| Run one local project | `backupdock backup --project PROJECT` |
| Run a remote backup | `backupdock remote-backup NAME` |
| List normal snapshots | `backupdock snapshots` |
| Check repository integrity | `backupdock check` |
| Apply retention | `backupdock forget` |
| Apply retention and reclaim storage | `backupdock forget --prune` |

See the [command-line reference](docs/cli.md) for all documented workflows, snapshot columns, preseed behavior, and repository selection.

## Test a Docker Compose Restore

Preview an isolated restore of a complete Compose group:

```bash
backupdock restore --remote docker-prod --project app --snapshot latest --as app-test --test --dry-run
```

Run the same command without `--dry-run` to restore copied data and start a separate test project. BackupDock uses its own data directory, internal network, disabled restart policies, and automatically assigned ports bound to `127.0.0.1`.

Read [Restore and Test Docker Compose Backups](docs/restore.md) before running the real restore. The guide covers local and remote targets, generated paths, supported Compose settings, validation, access, and cleanup.

## Safety Guarantees

- Discovery and safety validation complete before the first container is stopped.
- Only the selected consistency group's previously running containers are stopped.
- Dependency order is honored for stopping and restarting Compose services.
- Restart is attempted after backup failure, interruption, or partial stop failure.
- Existing stopped containers are never started by the backup.
- Unsafe writable overlap across groups aborts instead of guessing.
- Running ignored containers cannot silently write into selected backup data.
- Dry-run uses the same orchestration path while suppressing mutations.

Forced process termination or host failure cannot run recovery logic. See the [security model](docs/security-model.md) and [detailed backup behavior](docs/backup-behavior.md).

## Current Limitations

- Linux and Python 3.11 or newer are required.
- Docker images and container writable layers are not backed up.
- In-place restores and standalone-container recreation are not implemented.
- Isolated restore currently supports complete Compose groups with one container per service and an available image.
- Special volume drivers and unsafe Compose options fail explicitly.
- Scheduling is delegated to systemd, cron, or another external scheduler.

## Documentation

- [Documentation index](docs/README.md)
- [Quick Start](docs/quick-start.md)
- [Installation and updates](docs/installation.md)
- [Configuration reference](docs/configuration.md)
- [Docker Compose backup behavior](docs/backup-behavior.md)
- [Remote Docker backups](docs/remote-backup.md)
- [Command-line reference](docs/cli.md)
- [Scheduling and maintenance](docs/scheduling.md)
- [Restore and recovery](docs/restore.md)
- [Upgrade notes](docs/upgrading.md)
- [Security model](docs/security-model.md)
- [Development and tests](docs/development.md)

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -v
```

See [Development and tests](docs/development.md) for all repository checks.

## License

Licensed under the [Apache License 2.0](LICENSE).
