# BackupDock

**Automatic, consistent and incremental backups for Docker hosts.**

BackupDock is a small Linux CLI that discovers persistent Docker data automatically, groups containers by Docker Compose project, stops only the project currently being backed up, and delegates deduplicated incremental storage to [Restic](https://restic.net/).

BackupDock is intentionally not a daemon, scheduler, web UI, or replacement for Restic.

> **Status:** early alpha. The backup/discovery core is usable for testing, but restore orchestration is not implemented yet. Test restores before relying on BackupDock for production data.

## Operating modes

BackupDock supports two operating modes. Both use the same Docker discovery, dependency ordering, Stop/Restic/Restart transaction, manifests, exclusions, and dry-run logic.

### 1. Local mode

BackupDock and Restic run directly on the Docker host. The permanent configuration is stored on that host.

```text
Docker host
├── /etc/backupdock/config.yaml
├── BackupDock
├── Docker
└── Restic ────────────────> repository
```

Use this mode when the Docker host can directly reach the Restic repository:

```bash
backupdock backup
```

### 2. Remote/controller mode

BackupDock is installed on both the backup server and the Docker source host. The backup server owns the single permanent configuration and initiates the backup over SSH. It creates a temporary reverse SSH tunnel so Restic on the Docker host can reach an authenticated, append-only rest-server on the otherwise unreachable backup server.

```text
Backup server                         Docker source
-------------                         -------------
/etc/backupdock/config.yaml           no permanent config required
BackupDock controller ---- SSH -----> BackupDock
rest-server <------ reverse tunnel -- Restic
+ authentication                      Docker
+ append-only
```

The source receives only a temporary controller-generated configuration below `/run/backupdock/` for the current session. A permanent `/etc/backupdock/config.yaml` on a remote-only source is neither required nor used.

Use this mode when the backup server should initiate backups or is not directly reachable from the Docker host:

```bash
backupdock remote-backup REMOTE_NAME
```

The controller verifies that both BackupDock installations have exactly the same version and compatible remote protocol before opening the backup tunnel or sending backup credentials/configuration.

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
- Local backups remain the default; backup-server-initiated remote backups are optional.
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
- Docker Engine on source hosts
- Restic
- permission to access the Docker daemon and the host-side persistent data (normally run as root)
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

A permanent `/etc/backupdock/config.yaml` is required for normal local backups and on a remote backup controller. It is deliberately **not required on a source host that is used only through `remote-backup`**; that host receives a temporary controller-generated source configuration for each session.

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

The default configuration path for local/controller operation is:

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
sudo vim /etc/backupdock/config.yaml
```

If `/etc/backupdock/config.yaml` is missing and no alternative `--config` file is selected, normal local/controller commands write a warning to `stderr` and continue with built-in defaults where possible.

Minimal local example using Restic's standard environment variables:

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

## Optional remote backups through a reverse SSH tunnel

Local backups remain the default and continue to use `backupdock backup` exactly as before.

For a backup server that is behind a firewall, BackupDock can instead run as the controller. The backup server initiates SSH to the Docker host and asks OpenSSH to create a loopback-only reverse forward on the source host. Restic still runs on the Docker host, but its REST repository URL points to that temporary loopback port and traffic is carried back through the SSH connection to an authenticated rest-server on the backup server.

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
                                        +-- restic
                                        +-- BackupDock
                                        +-- Docker
```

The rest-server should be bound only to loopback, require authentication, and run with `--append-only`. The source host can create new backup data during the tunnel session but cannot use the REST endpoint to delete or modify existing repository data. Repository maintenance such as `forget` and `prune` should be run locally on the backup server with normal repository access.

Loopback binding prevents network clients on other hosts from reaching the REST endpoint, but it does not isolate the endpoint from other local processes on the backup server. Authentication is therefore kept enabled even though the service listens only on `127.0.0.1`.

### Debian/Ubuntu controller setup

On Debian 13 and Ubuntu releases that provide the `restic-rest-server` package, the packaged service can be used directly. It already provides the `restic-rest-server` system user, the `restic-rest-server.service` unit, and `/etc/default/restic-rest-server`; no separate service or custom systemd hardening is required for the BackupDock setup.

Install rest-server and the `htpasswd` utility on the backup server:

```bash
apt update
apt install -y restic-rest-server apache2-utils
```

Create a repository root owned by the dedicated service user. The path below is only an example and can be changed:

```bash
install -d \
  -o restic-rest-server \
  -g restic-rest-server \
  -m 0700 \
  /srv/backups/backupdock
```

Create a controller-only directory for secrets and generate a REST authentication password for the example source `docker-prod`:

```bash
install -d -o root -g root -m 0700 /etc/backupdock/secrets
python3 - <<'PY' > /etc/backupdock/secrets/docker-prod.rest-server-password
import secrets
print(secrets.token_urlsafe(32))
PY
chmod 0600 /etc/backupdock/secrets/docker-prod.rest-server-password
```

Create the rest-server `.htpasswd` file using the same password. The clear-text password remains readable only by root on the controller; rest-server stores only its bcrypt hash:

```bash
htpasswd -B -c -i \
  /srv/backups/backupdock/.htpasswd \
  docker-prod \
  < /etc/backupdock/secrets/docker-prod.rest-server-password
chown restic-rest-server:restic-rest-server /srv/backups/backupdock/.htpasswd
chmod 0600 /srv/backups/backupdock/.htpasswd
```

Configure the packaged service:

```bash
vim /etc/default/restic-rest-server
```

For BackupDock's reverse-tunnel mode, a minimal configuration is:

```ini
LISTEN=127.0.0.1:8000
BACKUP_DIR=/srv/backups/backupdock
ARGS="--append-only"
```

Do **not** use `--no-auth` for this setup. With authentication enabled, rest-server uses `<BACKUP_DIR>/.htpasswd` by default and refuses to start if the password file cannot be opened.

The Debian/Ubuntu package runs rest-server as its dedicated `restic-rest-server` user. It does not need root privileges: rest-server stores Restic repository objects, while ownership and permission metadata for the original files is handled by Restic and restored by Restic on the source/restore host.

Start or restart the packaged service and verify the listener:

```bash
systemctl enable --now restic-rest-server
systemctl restart restic-rest-server
systemctl status restic-rest-server --no-pager
ss -ltnp | grep ':8000'
```

The listener should be bound to `127.0.0.1:8000`, not `0.0.0.0:8000` or another externally reachable address.

### Central configuration

In remote mode, `/etc/backupdock/config.yaml` on the backup server is the single authoritative configuration. Backup settings and project-specific exclusions are generated from that controller configuration and sent to the source only for the current backup session.

The source-side session file is created below:

```text
/run/backupdock/session-<random>/config.yaml
```

The runtime directory and session directory are mode `0700`; the temporary configuration is mode `0600`. It is removed when the source-side command exits, including error paths.

`restic.repository`, `restic.password_file`, `remotes`, controller-side retention access, and REST authentication credentials are not copied to the source configuration. The repository URL is generated from the temporary reverse tunnel. The Restic repository password and the separate REST authentication password are sent inside the SSH standard-input payload and are not written into the temporary YAML file.

If `/etc/backupdock/config.yaml` nevertheless exists on a remote source host, BackupDock prints a warning such as:

```text
backupdock: warning: /etc/backupdock/config.yaml exists on the source host but is ignored for this remote backup; using the temporary controller-provided configuration
```

The file is **not loaded or merged**. This avoids having two competing configurations for the same remote backup.

Example controller-side configuration on the backup server:

```yaml
backup:
  state_dir: "/var/lib/backupdock"
  stop_timeout_seconds: 30
  include_compose_metadata: true

projects:
  pve-backup-server-dockerfiles:
    exclude_volumes:
      - "pve-backup-server-dockerfiles_backups"

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
    source_command:
      - "backupdock"
    ssh_options:
      - "-i"
      - "/root/.ssh/backupdock"
```

`password_file` contains the Restic repository encryption password. `rest_server_username` and `rest_server_password_file` are separate credentials for access to the REST API. Restic receives the latter on the source only as `RESTIC_REST_USERNAME` and `RESTIC_REST_PASSWORD` for the lifetime of the remote backup process.

`local_rest_server_host` and `local_rest_server_port` are resolved on the backup server. `remote_tunnel_port` is opened by SSH on `127.0.0.1` of the Docker source for the lifetime of that SSH session only. `repository_path` becomes the path below the rest-server data root.

### Strict version check

Before opening the reverse tunnel, reading either password file, or sending the generated source configuration, the controller runs the internal source command `backupdock source-info` over SSH.

The source returns machine-readable version/protocol information. Remote backup proceeds only when the source BackupDock version is **exactly equal** to the controller version and the remote protocol version matches. Any mismatch aborts before containers, Restic, tunnel credentials, or source configuration are touched.

The source also validates the controller version/protocol embedded in the actual backup-session payload, protecting against a source update between the initial check and the backup command.

Start the remote backup from the backup server:

```bash
backupdock remote-backup docker-prod
```

Or only one Compose project:

```bash
backupdock remote-backup docker-prod --project nextcloud
```

Dry-run is also available:

```bash
backupdock remote-backup docker-prod --dry-run
```

Dry-run performs the normal remote version check but does not read the real repository or REST authentication password files. Placeholder values are used inside the non-mutating source session.

The controller does not put either password into the SSH command line or repository URL. Both are sent to the source-side BackupDock process through SSH standard input. The source process exposes the repository password to Restic as `RESTIC_PASSWORD` and REST authentication as `RESTIC_REST_USERNAME`/`RESTIC_REST_PASSWORD` only for the current process invocation. Source-side values for these variables, `RESTIC_PASSWORD_FILE`, or `RESTIC_PASSWORD_COMMAND` are ignored for the remote session and restored afterwards.

The source-side command is `backupdock source-backup`; it is normally invoked only by `remote-backup`. It loads only the temporary controller-provided configuration and then enters the same `_run_backup` path as a normal local backup. Discovery, dependency ordering, source validation, locking, Stop/Restic/Restart handling, manifests, and dry-run therefore remain shared rather than duplicated.

Automatic retention is intentionally disabled for `source-backup`. Destructive retention operations must not be sent through the append-only endpoint.

If the SSH session is interrupted, BackupDock handles `SIGHUP` in addition to `SIGINT` and `SIGTERM`, so an interrupted source-side run reaches the same guarded restart path for containers already stopped by BackupDock.

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

Start a configured remote backup from a backup server:

```bash
backupdock remote-backup docker-prod
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

For append-only remote backups, run retention locally on the backup server against the repository path rather than through `remote-backup`.

## Scheduling

BackupDock contains no scheduler. Example systemd unit and timer files are available in [`examples/`](examples/).

For remote mode, schedule `backupdock remote-backup REMOTE_NAME` on the backup server. The reverse SSH tunnel then exists only for the duration of each scheduled backup.

## Restore status

Restic snapshots can already be restored manually with Restic. BackupDock writes a machine-readable manifest to each group backup, but automatic recreation/mapping of Docker volumes and project-aware restore is deliberately not implemented in this first alpha version.

Automated restore is a required milestone before BackupDock should be considered feature-complete.
