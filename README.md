# BackupDock

**Automatic, consistent and incremental backups for Docker hosts.**

BackupDock is a small Linux CLI that discovers persistent Docker data automatically, groups containers by Docker Compose project, stops only the consistency group currently being backed up, and delegates deduplicated incremental storage to [Restic](https://restic.net/).

BackupDock is intentionally not a daemon, scheduler, web UI, or replacement for Restic.

> **Status:** early alpha. The backup/discovery core is usable for testing, but automated restore orchestration is not implemented yet. Test restores before relying on BackupDock for production data.

## Operating modes

BackupDock supports two operating modes. Both use the same Docker discovery, dependency ordering, safety checks, Stop/Restic/Restart transaction, manifests, exclusions, ignored-container handling, preseed logic, and dry-run code paths.

### 1. Local mode

BackupDock and Restic run directly on the Docker host. The permanent configuration is stored on that host.

```text
Docker host
├── /etc/backupdock/config.yaml
├── BackupDock
├── Docker
└── Restic ────────────────> repository
```

Run a local backup with:

```bash
backupdock backup
```

### 2. Remote/controller mode

BackupDock is installed on both the backup server and the Docker source host. The backup server owns the permanent configuration and initiates the backup over SSH. It creates a temporary reverse SSH tunnel so Restic on the Docker source can reach an append-only `rest-server` on the otherwise unreachable backup server.

```text
Backup server                         Docker source
-------------                         -------------
/etc/backupdock/config.yaml           no permanent config required
BackupDock controller ---- SSH -----> BackupDock
rest-server <------ reverse tunnel -- Restic
                                      Docker
```

The source receives only a temporary controller-generated configuration below `/run/backupdock/` for the current session. A permanent `/etc/backupdock/config.yaml` on a remote-only source is neither required nor used.

Run a remote backup with:

```bash
backupdock remote-backup docker-prod
```

The controller verifies that both BackupDock installations have exactly the same version and compatible remote protocol before opening the backup tunnel or sending backup credentials/configuration.

## Design goals

- No assumptions about host directory layouts.
- No mandatory Docker labels.
- Automatic bind-mount and Docker-volume discovery.
- Docker Compose projects are consistency groups.
- Standalone containers become one-container consistency groups.
- Only the group currently being backed up is stopped.
- Running services are stopped in reverse dependency order and restarted in dependency order.
- Containers that were already stopped stay stopped.
- Running state is restored even when Restic or a stop operation fails.
- Docker's own stop configuration is respected; BackupDock does not override the stop timeout.
- Running auto-remove (`--rm`) containers fail safe before they can be stopped accidentally.
- Explicitly ignored containers remain visible to safety checks so writable overlap cannot silently break consistency.
- Shared/overlapping persistent storage across different groups fails safe instead of silently producing an inconsistent backup.
- Restic remains the backup engine and repository format.
- Local backups remain the default; backup-server-initiated remote backups are optional.
- Optional preseed backups can reduce downtime for large first backups.
- Scheduling is left to systemd, cron, or another external scheduler.

## How grouping works

For containers created by Docker Compose, BackupDock uses Docker Compose metadata, primarily `com.docker.compose.project`, to form a consistency group.

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

Other Compose projects remain online while one project is being backed up. A container without Compose metadata becomes its own standalone backup group.

BackupDock reconstructs Compose service dependencies from `com.docker.compose.depends_on`. It also adds resolvable same-project runtime relationships from Docker inspect data for `links`, `volumes_from`, and container network sharing. Services without dependency relationships are ordered deterministically by name.

A dependency cycle aborts discovery instead of guessing an unsafe order. Dependency targets that have no container in the currently deployed project are ignored for ordering with a warning.

The dependency graph changes ordering only. A service that was stopped before the backup is never started merely because another service depends on it.

If writable persistent paths overlap across different groups, BackupDock aborts discovery. It never decides on its own to stop an unrelated Compose project. Shared read-only mounts are allowed.

## What is discovered

BackupDock reads the Docker daemon and discovers:

- bind mounts (`Type=bind`)
- Docker volumes (`Type=volume`)
- running/stopped container state
- Docker `AutoRemove` state
- Docker Compose project and service names
- Docker Compose service dependency metadata
- Compose config-file metadata exposed by Docker Compose
- the Compose working-directory `.env` file when present

`tmpfs` and non-file bind sources such as sockets or FIFOs are ignored because they are not persistent backup data.

There are **no built-in paths such as `/srv/files`, `/srv/docker-config`, `/opt/docker`, or `/var/lib/docker/volumes`**. Volume source paths come from Docker itself.

## Requirements

- Linux
- Python 3.11+
- Docker Engine on source hosts
- Restic
- permission to access the Docker daemon and host-side persistent data (normally root)
- OpenSSH client on a backup server when using `remote-backup`

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
- installs/upgrades BackupDock and Python dependencies only inside that virtual environment,
- creates `/usr/bin/backupdock` as a symlink to the venv command,
- installs/updates `/etc/backupdock/config.yaml.example`,
- never creates or overwrites `/etc/backupdock/config.yaml`,
- creates `/var/lib/backupdock` for BackupDock state.

To update an existing installation:

```bash
git pull
sudo ./install.sh
backupdock --version
```

A permanent `/etc/backupdock/config.yaml` is required for normal local backups and on a remote backup controller. It is deliberately **not required on a source host used only through `remote-backup`**.

### Uninstall

Preserve configuration and state:

```bash
sudo ./uninstall.sh
```

Remove BackupDock, configuration and state:

```bash
sudo ./uninstall.sh --purge
```

The uninstaller does not remove shared system packages such as Python or Restic.

### Development installation

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

The repository contains `config.yaml.example`. The installer copies the current example to:

```text
/etc/backupdock/config.yaml.example
```

Create the real configuration explicitly when needed:

```bash
sudo cp /etc/backupdock/config.yaml.example /etc/backupdock/config.yaml
sudo vim /etc/backupdock/config.yaml
```

`backup.state_dir`, `backup.include_compose_metadata`, and `restic.backup_args` are shared defaults for local and remote backup runs. Source-specific paths, exclusions, ignored containers, and project rules are scoped to the source host.

Local mode uses:

```text
backup.host_paths
backup.exclude_paths
backup.exclude_volumes
backup.ignore_containers
projects
```

Remote mode uses the corresponding fields below the selected remote:

```text
remotes.<name>.host_paths
remotes.<name>.exclude_paths
remotes.<name>.exclude_volumes
remotes.<name>.ignore_containers
remotes.<name>.projects
```

This keeps local mode compact while allowing one controller to manage multiple heterogeneous Docker hosts without mixing their rules.

### Minimal local example

```yaml
backup:
  state_dir: "/var/lib/backupdock"
  include_compose_metadata: true
  host_paths: []
  exclude_paths: []
  exclude_volumes: []
  ignore_containers: []

projects: {}
```

Provide Restic's standard environment variables:

```bash
export RESTIC_REPOSITORY='sftp:backup@example:/backups/docker-prod'
export RESTIC_PASSWORD_FILE='/root/.config/restic/password'
```

Alternatively configure them directly:

```yaml
restic:
  repository: "sftp:backup@example:/backups/docker-prod"
  password_file: "/root/.config/restic/password"
```

### Additional paths

Docker-managed persistent data normally requires no configuration.

Attach project-specific data that Docker cannot discover explicitly:

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

For a remote source, put the equivalent fields below that remote:

```yaml
remotes:
  docker-prod:
    # connection/repository fields omitted here
    host_paths:
      - "/etc/some-static-config"
    projects:
      paperless:
        extra_paths:
          - "/some/additional/path"
```

Do not use `host_paths` for live container data. Attach such data to the corresponding project instead.

### Excluding Docker volumes

Volumes can be excluded by their Docker volume name, avoiding dependence on Docker's host-side volume path.

Local example:

```yaml
projects:
  pve-backup-server-dockerfiles:
    exclude_volumes:
      - "pve-backup-server-dockerfiles_backups"
```

Remote example:

```yaml
remotes:
  docker-prod:
    # connection/repository fields omitted here
    projects:
      pve-backup-server-dockerfiles:
        exclude_volumes:
          - "pve-backup-server-dockerfiles_backups"
```

Other volumes of the project remain part of the backup. A source-wide exclusion is also available via `backup.exclude_volumes` locally or `remotes.<name>.exclude_volumes` remotely.

### Ignoring containers and `--rm` safety

A running container created with Docker `--rm` has `HostConfig.AutoRemove=true`. Stopping it removes the container, and Docker may also remove anonymous volumes. BackupDock therefore treats a running auto-remove container that would need to be stopped for a persistent-data backup as unsafe and aborts **before any container is stopped**.

`backupdock inventory` marks such containers as `auto-remove`.

If a one-shot or helper container is explicitly known to be safe to leave running, it can be ignored by its **exact Docker container name**.

Local example:

```yaml
backup:
  ignore_containers:
    - "temporary-helper"
```

Remote example:

```yaml
remotes:
  docker-prod:
    # connection/repository fields omitted here
    ignore_containers:
      - "temporary-helper"
```

An ignored container:

- is not stopped,
- is not restarted,
- does not contribute its own mounts as backup sources,
- remains visible in `inventory`,
- remains visible to BackupDock's safety checks.

If an ignored **running** container has a writable mount that overlaps data selected for backup, BackupDock still aborts. `ignore_containers` cannot be used to bypass the consistency guarantee.

If a configured ignored container name is not found, BackupDock prints a warning.

## Preseed mode for large backups

A normal backup keeps a consistency group stopped while Restic reads and uploads its data:

```text
STOP -> BACKUP -> START
```

For a very large first backup this can produce unnecessary downtime. `--preseed` adds an initial Restic pass while the containers are still running:

```text
Safety checks
    ↓
PRESEED while containers are running
    ↓
STOP
    ↓
FINAL consistent Restic backup
    ↓
START
```

Local mode:

```bash
backupdock backup --preseed
```

Remote mode:

```bash
backupdock remote-backup docker-prod --preseed
```

It can also be combined with `--project`:

```bash
backupdock remote-backup docker-prod --project nextcloud --preseed
```

The preseed pass is **not considered a consistent BackupDock backup**. It exists only to populate the Restic repository before the downtime window. Preseed snapshots receive the tag:

```text
backupdock-preseed
```

The final stopped-state snapshot receives the normal:

```text
backupdock
```

tag.

Restic can use the preseed snapshot as the parent for the final run when host/path grouping matches, reducing the amount of unchanged data that has to be processed again. Files changed during the preseed pass are handled again during the final stopped-state backup. Large changed files can still require significant local read/chunking time even when deduplication avoids retransmitting most blocks.

If the preseed pass fails, BackupDock aborts **before stopping any container**.

Preseed snapshots are deliberately excluded from `backupdock snapshots` and normal BackupDock retention because they are not consistency snapshots. They remain repository data until explicitly removed by repository maintenance.

For normal recurring incremental backups, `--preseed` is usually unnecessary because Restic already has previous snapshots to reuse.

## Remote backups through a reverse SSH tunnel

Local backups remain the default and continue to use `backupdock backup`.

For a backup server behind a firewall, BackupDock can run as the controller. The backup server initiates SSH to the Docker host and creates a loopback-only reverse forward on the source host. Restic runs on the source but reaches an authenticated `rest-server` through that tunnel.

Recommended layout:

```text
backup server                         Docker source
-------------                         -------------
/etc/backupdock/config.yaml           no permanent config required
rest-server --append-only
127.0.0.1:8000
+ HTTP authentication
       ^
       | reverse SSH tunnel
       +------------------------------ 127.0.0.1:18080
                                        |
                                        +-- Restic
                                        +-- BackupDock
                                        +-- Docker
```

The `rest-server` should be bound only to loopback, require HTTP authentication, and run with `--append-only`. Repository maintenance such as `forget`, `prune`, and removal of preseed snapshots belongs on the backup server with non-append-only repository access.

### Debian/Ubuntu controller setup

On Debian 13 and Ubuntu releases that provide `restic-rest-server`, the packaged service can be used directly. No custom service or separate systemd hardening is required for BackupDock.

Install the required packages:

```bash
apt update
apt install -y restic-rest-server apache2-utils
```

Create a repository root:

```bash
install -d \
  -o restic-rest-server \
  -g restic-rest-server \
  -m 0700 \
  /srv/backups/backupdock
```

Create controller-only secrets for the example remote `docker-prod`:

```bash
install -d -o root -g root -m 0700 /etc/backupdock/secrets

python3 - <<'PY' > /etc/backupdock/secrets/docker-prod.rest-server-password
import secrets
print(secrets.token_urlsafe(32))
PY

python3 - <<'PY' > /etc/backupdock/secrets/docker-prod.repository-password
import secrets
print(secrets.token_urlsafe(32))
PY

chmod 0600 \
  /etc/backupdock/secrets/docker-prod.rest-server-password \
  /etc/backupdock/secrets/docker-prod.repository-password
```

Add the REST-authentication user:

```bash
htpasswd -B -i \
  /etc/restic-rest-server/users.htpasswd \
  docker-prod \
  < /etc/backupdock/secrets/docker-prod.rest-server-password

chown root:restic-rest-server /etc/restic-rest-server/users.htpasswd
chmod 0640 /etc/restic-rest-server/users.htpasswd
```

Configure `/etc/default/restic-rest-server`:

```ini
LISTEN=127.0.0.1:8000
BACKUP_DIR=/srv/backups/backupdock
ARGS="\
  --htpasswd-file /etc/restic-rest-server/users.htpasswd \
  --append-only \
"
```

Do **not** use `--no-auth` for this setup. Loopback binding prevents access from other hosts but does not isolate the endpoint from local processes.

Start/restart the packaged service and verify the listener:

```bash
systemctl enable --now restic-rest-server
systemctl restart restic-rest-server
systemctl status restic-rest-server --no-pager
ss -ltnp | grep ':8000'
```

The listener should be bound to `127.0.0.1:8000`, not an externally reachable address.

### Controller configuration

Example `/etc/backupdock/config.yaml` on the backup server:

```yaml
restic:
  backup_args: []

backup:
  state_dir: "/var/lib/backupdock"
  include_compose_metadata: true

remotes:
  docker-prod:
    ssh_target: "root@docker-prod.example"
    password_file: "/etc/backupdock/secrets/docker-prod.repository-password"
    repository_path: "docker-prod"
    rest_server_username: "docker-prod"
    rest_server_password_file: "/etc/backupdock/secrets/docker-prod.rest-server-password"
    local_rest_server_host: "127.0.0.1"
    local_rest_server_port: 8000
    remote_tunnel_port: 18080

    host_paths: []
    exclude_paths: []
    exclude_volumes: []
    ignore_containers: []

    projects:
      pve-backup-server-dockerfiles:
        exclude_volumes:
          - "pve-backup-server-dockerfiles_backups"
```

Top-level `projects` and top-level `backup.host_paths/exclude_*/ignore_containers` belong only to local mode. They are never copied into a remote source session. Only the selected remote's source-specific settings are rendered into that source's temporary configuration.

The temporary source file is created below:

```text
/run/backupdock/session-<random>/config.yaml
```

Runtime/session directories use mode `0700`; the temporary configuration uses mode `0600`. The session directory is removed when the source-side command exits.

A permanent source-side `/etc/backupdock/config.yaml`, if present, is ignored for remote backups and causes a warning. It is not loaded or merged.

### Initialize a remote repository

After creating the controller configuration and password files:

```bash
backupdock init --remote docker-prod
```

The command validates the selected remote configuration and both password files before invoking `restic init` against the controller-local `rest-server`. SSH to the source host is not required for repository initialization.

### Strict version check

Before a remote backup, the controller runs the internal `backupdock source-info` command over SSH. Backup proceeds only when source and controller use exactly the same BackupDock version and compatible remote protocol.

The source validates the controller version/protocol again in the actual session payload.

### Remote session credentials

Repository and REST-authentication passwords are never placed in the SSH command line or repository URL. The controller sends them through SSH standard input. The source exposes them to Restic only through:

```text
RESTIC_PASSWORD
RESTIC_REST_USERNAME
RESTIC_REST_PASSWORD
```

Any source-side `RESTIC_PASSWORD_FILE`, `RESTIC_PASSWORD_COMMAND`, or previous values of those session variables are ignored for the remote session and restored afterward.

Automatic retention is disabled on the remote source because its repository endpoint is append-only.

If SSH is interrupted, BackupDock handles `SIGHUP` in addition to `SIGINT` and `SIGTERM`, allowing the guarded restart path to restore containers already stopped by BackupDock.

## CLI

Inspect discovered groups, containers, auto-remove/ignored state, sources, dependencies, and excluded/ignored mounts:

```bash
backupdock inventory
```

Preview a local backup:

```bash
backupdock backup --dry-run
```

Preview one project:

```bash
backupdock backup --project paperless --dry-run
```

Run local backups:

```bash
backupdock backup
backupdock backup --project paperless
backupdock backup --preseed
```

Run remote backups:

```bash
backupdock remote-backup docker-prod
backupdock remote-backup docker-prod --project nextcloud
backupdock remote-backup docker-prod --preseed
backupdock remote-backup docker-prod --dry-run
```

Initialize repositories:

```bash
backupdock init
backupdock init --remote docker-prod
```

Repository commands for the configured local repository:

```bash
backupdock snapshots
backupdock check
backupdock forget
backupdock forget --prune
```

## Dry-run

Dry-run uses the same discovery, source validation, dependency ordering, safety checks, group loop, stop/backup/restart transaction, host-path routine, preseed branch, and retention path as a real backup. Mutation boundaries print actions instead of executing them.

For example, Docker stops/restarts, Restic commands, and manifest writes appear as `DRY-RUN ...` output. The normal process lock is still acquired so a dry-run is not produced concurrently with another BackupDock operation.

## Safety behavior

Before any container is stopped, BackupDock:

1. discovers all containers and persistent mounts,
2. groups Compose/standalone consistency groups,
3. validates dependency ordering,
4. validates cross-group writable-storage overlap,
5. checks selected groups for running `AutoRemove` containers that would be unsafe to stop,
6. checks whether ignored running containers can write to selected backup data,
7. verifies that the Restic repository is reachable.

Only after those checks does BackupDock enter a group's stop/backup/restart transaction.

For each group it records which containers were originally running. Running containers are stopped in reverse dependency order. BackupDock calls Docker stop **without a timeout override**, so Docker applies the container's configured stop behavior. Containers are restarted in dependency order even after a Restic failure, interruption, or partial stop failure. Containers that were already stopped are never added to the restart set.

If preseed mode is enabled, its initial Restic pass occurs after the safety checks but before any stop. A failed preseed therefore cannot create downtime.

A manifest describing the group, containers, dependency metadata, mount destinations, volume names, auto-remove/ignored state, and host source paths is stored under BackupDock's state directory and included in the final Restic snapshot.

## Retention

Retention is optional:

```yaml
retention:
  after_backup: false
  prune: false
  keep_daily: 14
  keep_weekly: 8
  keep_monthly: 12
```

Keeping `after_backup: false` avoids repository maintenance on every normal backup run. `backupdock forget` can be scheduled independently for a directly configured local repository.

Normal BackupDock retention targets snapshots tagged `backupdock`; preseed snapshots use `backupdock-preseed` and must be handled separately by repository maintenance if they should be deleted.

For append-only remote backups, destructive repository maintenance must run on the backup server with appropriate non-append-only access rather than through `remote-backup`.

## Scheduling

BackupDock contains no scheduler. Example systemd unit and timer files are available in `examples/`.

For remote mode, schedule:

```bash
backupdock remote-backup docker-prod
```

on the backup server. The reverse SSH tunnel exists only for the duration of the backup.

## Restore status

Restic snapshots can already be restored manually with Restic. BackupDock writes a machine-readable manifest into each group backup, but automatic recreation/mapping of Docker volumes and project-aware restore is not implemented yet.

Automated restore remains a required milestone before BackupDock should be considered feature-complete.
