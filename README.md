# BackupDock

**Automatic, consistent and incremental backups for Docker hosts.**

BackupDock is a small Linux CLI that discovers persistent Docker data automatically, groups containers by Docker Compose project, stops only the project currently being backed up, and delegates deduplicated incremental storage to [Restic](https://restic.net/).

BackupDock is intentionally not a daemon, scheduler, web UI, or replacement for Restic.

> **Status:** early alpha. The backup/discovery core is usable for testing, but restore orchestration is not implemented yet. Test restores before relying on BackupDock for production data.

## Design goals

- No assumptions about host directory layouts.
- No mandatory Docker labels.
- Automatic bind-mount and Docker-volume discovery.
- Docker Compose projects are consistency groups.
- Only containers in the project currently being backed up are stopped.
- Within a Compose project, running services are stopped in reverse dependency order and restarted in dependency order.
- Containers that were already stopped stay stopped.
- Running state is restored even when Restic or a stop operation fails.
- Standalone containers are supported as one-container backup groups.
- Shared/overlapping persistent storage across different groups fails safely instead of silently producing an inconsistent backup.
- Restic remains the backup engine and repository format.
- Scheduling is left to systemd, cron, or another external scheduler.

## How grouping works

For containers created by Docker Compose, BackupDock uses Docker's Compose metadata, primarily `com.docker.compose.project`, to form a consistency group.

```text
Compose project A
  stop currently-running containers in reverse dependency order
  back up A's bind mounts, volumes and Compose metadata
  restart exactly the previously-running containers in dependency order

Compose project B
  stop currently-running containers in reverse dependency order
  back up B's data
  restart exactly the previously-running containers in dependency order
```

Other Compose projects remain online while a project is being backed up.

A container without Compose metadata becomes its own standalone backup group.

For Compose projects, BackupDock reconstructs service dependencies from the `com.docker.compose.depends_on` metadata written by Docker Compose. It also adds resolvable same-project runtime relationships from Docker inspect data for `links`, `volumes_from`, and container network sharing. Services without dependency relationships are ordered deterministically by name. A dependency cycle aborts discovery instead of guessing an unsafe order. Dependency targets that have no container in the currently deployed project are ignored for ordering with a warning.

The dependency graph only changes ordering. A service that was stopped before the backup is never started merely because another service depends on it.

If writable persistent paths overlap across two different groups, BackupDock aborts discovery. It will never decide on its own to stop an unrelated Compose project. Shared read-only bind mounts are allowed.

## What is discovered

BackupDock reads the Docker daemon and discovers:

- bind mounts (`Type=bind`)
- Docker volumes (`Type=volume`)
- running/stopped container state
- Docker Compose project and service names
- Docker Compose service dependency metadata
- Compose config-file metadata exposed by Docker Compose
- the Compose working-directory `.env` file when present

`tmpfs` and non-file bind sources such as sockets or FIFOs are ignored because they are not persistent backup data.

There are **no built-in paths such as `/srv/files`, `/srv/docker-config`, `/opt/docker`, or `/var/lib/docker/volumes`**. Volume source paths come from Docker itself.

## Requirements

- Linux
- Python 3.11+
- Docker Engine
- Restic
- permission to access the Docker daemon and the host-side persistent data (normally run as root)

Docker itself is never installed or modified by BackupDock.

## Installation

Clone the repository and run the installer as root:

```bash
git clone https://github.com/disaster123/BackupDock.git
cd BackupDock
sudo ./install.sh
```

The installer:

- installs missing Python/venv and Restic system dependencies on supported package managers,
- creates `/opt/backupdock/venv`,
- installs/upgrades BackupDock and all Python dependencies only inside that virtual environment,
- creates `/usr/bin/backupdock` as a symlink to the venv command,
- always installs or updates `/etc/backupdock/config.yaml.example` from the repository example,
- never creates or overwrites `/etc/backupdock/config.yaml`,
- creates `/var/lib/backupdock` for BackupDock state.

Running the installer again after updating the Git checkout upgrades the installed version and refreshes the example configuration:

```bash
git pull
sudo ./install.sh
```

Then BackupDock can be called directly:

```bash
sudo backupdock inventory
```

### Uninstall

Remove BackupDock itself while preserving configuration and state:

```bash
sudo ./uninstall.sh
```

Remove BackupDock, configuration and state:

```bash
sudo ./uninstall.sh --purge
```

The uninstaller deliberately does not remove shared system packages such as Python or Restic.

## Installation for development

For development, a local virtual environment can still be used:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Configuration

The default configuration path is:

```text
/etc/backupdock/config.yaml
```

A different file can be selected with `--config`.

The repository contains [`config.yaml.example`](config.yaml.example). The installer always copies the current example to:

```text
/etc/backupdock/config.yaml.example
```

The installer deliberately does **not** create the real configuration. Create it explicitly when needed:

```bash
sudo cp /etc/backupdock/config.yaml.example /etc/backupdock/config.yaml
sudo editor /etc/backupdock/config.yaml
```

If `/etc/backupdock/config.yaml` is missing and no alternative `--config` file is selected, BackupDock writes a warning to `stderr` and continues with built-in defaults.

Minimal example using Restic's standard environment variables:

```yaml
backup:
  state_dir: "/var/lib/backupdock"
  stop_timeout_seconds: 30
  include_compose_metadata: true
```

Then provide the normal Restic environment:

```bash
export RESTIC_REPOSITORY='sftp:backup@example:/backups/docker-host'
export RESTIC_PASSWORD_FILE='/root/.config/restic/password'
```

Alternatively the repository and password file can be configured directly:

```yaml
restic:
  repository: "sftp:backup@example:/backups/docker-host"
  password_file: "/root/.config/restic/password"
```

### Additional paths

Docker-managed persistent data should normally require no configuration.

Project-specific data that Docker cannot discover can be attached explicitly:

```yaml
projects:
  paperless:
    extra_paths:
      - "/some/additional/path"
```

Static host paths can be backed up separately without stopping containers:

```yaml
backup:
  host_paths:
    - "/etc/some-static-config"
```

Do not use `host_paths` for live container data. Attach such data to the relevant project instead.

### Excluding Docker volumes

Docker volumes can be excluded by their Docker volume name. This avoids depending on Docker's host-side mount path.

Prefer a project-specific exclusion when the volume belongs to one Compose project:

```yaml
projects:
  pve-backup-server-dockerfiles:
    exclude_volumes:
      - "pve-backup-server-dockerfiles_backups"
```

The other volumes of that project remain part of the backup. Volume names can be copied directly from `backupdock inventory`.

A global exclusion is also available when needed:

```yaml
backup:
  exclude_volumes:
    - "some_globally_ignored_volume"
```

## CLI

Inspect the discovered groups, sources, dependencies, and excluded Docker volumes:

```bash
backupdock inventory
```

Run the normal backup workflow in dry-run mode:

```bash
backupdock backup --dry-run
```

Dry-run uses the same discovery, source validation, dependency ordering, Restic preflight routine, group loop, stop/backup/restart transaction, host-path backup routine, and retention path as a real backup. The difference is at the execution boundary: mutating Docker actions, Restic commands, and manifest writes are printed instead of executed.

This makes dry-run useful for checking the actual command and action sequence without stopping containers or writing backup data.

Preview only one Compose project:

```bash
backupdock backup --project paperless --dry-run
```

Back up all groups sequentially:

```bash
backupdock backup
```

Back up only one Compose project:

```bash
backupdock backup --project paperless
```

Initialize the configured Restic repository:

```bash
backupdock init
```

List BackupDock snapshots:

```bash
backupdock snapshots
```

Check repository integrity:

```bash
backupdock check
```

Apply the configured retention policy:

```bash
backupdock forget
```

Or forget and prune:

```bash
backupdock forget --prune
```

## Safety behavior

Before any container is stopped, BackupDock:

1. discovers every container and persistent mount,
2. groups containers into Compose/standalone consistency groups,
3. validates Compose dependency ordering,
4. checks for unsafe writable storage overlap between groups,
5. verifies the Restic repository is reachable.

For each group it records which containers are running. Running containers are stopped in reverse dependency order and backed up inside a guarded transaction. Restart is attempted in dependency order even after a backup failure, interruption, or partial stop failure. Containers that were already stopped are never added to the restart set.

Dry-run deliberately follows this same orchestration code path. `DockerBackend` and `ResticRunner` switch only their execution behavior: Docker mutations and Restic subprocess calls are rendered as `DRY-RUN ...` output. Manifest generation follows the same routine but prints the target manifest path instead of writing it. The normal process lock is still acquired so the plan is not produced concurrently with another BackupDock run.

A manifest describing the group, containers, dependency metadata, mount destinations, volume names, and host source paths is stored under BackupDock's state directory and included in the Restic snapshot during a real backup. This metadata is intended to support automated restore workflows later.

## Retention

Retention is optional. Example:

```yaml
retention:
  after_backup: false
  prune: false
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 12
```

Keeping `after_backup: false` avoids making every normal backup run perform repository maintenance. `backupdock forget` can be scheduled independently.

## Scheduling

BackupDock contains no scheduler. Example systemd unit and timer files are available in [`examples/`](examples/).

## Restore status

Restic snapshots can already be restored manually with Restic. BackupDock writes a machine-readable manifest to each group backup, but automatic recreation/mapping of Docker volumes and project-aware restore is deliberately not implemented in this first alpha version.

Automated restore is a required milestone before BackupDock should be considered feature-complete.
