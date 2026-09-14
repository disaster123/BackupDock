# Remote Docker Backups with BackupDock

[Documentation index](README.md) · [Main README](../README.md)


Remote/controller mode lets a backup server initiate consistent Docker Compose backups when the Docker source cannot directly reach the repository.

## How remote/controller mode works

BackupDock is installed on both the backup server and the Docker source host. The backup server owns the single permanent configuration and initiates the backup over SSH. It creates a temporary reverse SSH tunnel so Restic on the Docker host can reach an append-only rest-server on the otherwise unreachable backup server.

```text
Backup server                         Docker source
-------------                         -------------
/etc/backupdock/config.yaml           no permanent config required
BackupDock controller ---- SSH -----> BackupDock
rest-server <------ reverse tunnel -- Restic
                                      Docker
```

The source receives only a temporary controller-generated configuration below `/run/backupdock/` for the current session. A permanent `/etc/backupdock/config.yaml` on a remote-only source is neither required nor used.

Use this mode when the backup server should initiate backups or is not directly reachable from the Docker host:

```bash
backupdock remote-backup REMOTE_NAME
```

The controller verifies that both BackupDock installations have exactly the same version and compatible remote protocol before opening the backup tunnel or sending backup credentials/configuration.

## Reverse SSH tunnel architecture

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

On Debian 13 and Ubuntu releases that provide the `restic-rest-server` package, the packaged service can be used directly. It already provides the `restic-rest-server` system user, the `restic-rest-server.service` unit, `/etc/default/restic-rest-server`, and the default authentication file `/etc/restic-rest-server/users.htpasswd`; no separate service or custom systemd hardening is required for the BackupDock setup.

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

Create a controller-only directory for secrets and generate separate REST authentication and Restic repository passwords for the example source `docker-prod`:

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

Add the rest-server user to Debian/Ubuntu's packaged authentication file using the same REST authentication password. The clear-text password remains readable only by root on the controller; rest-server stores only its bcrypt hash:

```bash
htpasswd -B -i \
  /etc/restic-rest-server/users.htpasswd \
  docker-prod \
  < /etc/backupdock/secrets/docker-prod.rest-server-password
chown root:restic-rest-server /etc/restic-rest-server/users.htpasswd
chmod 0640 /etc/restic-rest-server/users.htpasswd
```

Configure the packaged service:

```bash
vim /etc/default/restic-rest-server
```

For BackupDock's reverse-tunnel mode, a minimal configuration is:

```ini
LISTEN = 127.0.0.1:8000
BACKUP_DIR = /srv/backups/backupdock
ARGS = "\
  --htpasswd-file /etc/restic-rest-server/users.htpasswd \
  --append-only \
"
```

Do **not** use `--no-auth` for this setup. The packaged service explicitly uses `/etc/restic-rest-server/users.htpasswd`, which remains readable by the dedicated service user through its `restic-rest-server` group membership.

The Debian/Ubuntu package runs rest-server as its dedicated `restic-rest-server` user. It does not need root privileges: rest-server stores Restic repository objects, while ownership and permission metadata for the original files is handled by Restic and restored by Restic on the source/restore host.

Start or restart the packaged service and verify the listener:

```bash
systemctl enable --now restic-rest-server
systemctl restart restic-rest-server
systemctl status restic-rest-server --no-pager
ss -ltnp | grep ':8000'
```

The listener should be bound to `127.0.0.1:8000`, not `0.0.0.0:8000` or another externally reachable address.

Store the authoritative settings on the controller as described in the [configuration reference](configuration.md#remote-controller-configuration).

## Strict version check

Before opening the reverse tunnel, reading the Restic repository password or REST authentication password, or sending the generated source configuration, the controller runs the internal source command `backupdock source-info` over SSH.

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

The controller does not put the Restic repository password or REST authentication password into the SSH command line or repository URL. It reads both configured password files on the backup server and sends the session payload through SSH standard input. The source process exposes them to Restic only through `RESTIC_PASSWORD`, `RESTIC_REST_USERNAME`, and `RESTIC_REST_PASSWORD`. Any source-side values for those variables, `RESTIC_PASSWORD_FILE`, or `RESTIC_PASSWORD_COMMAND` are ignored for this remote session and restored afterward.

The source-side command is `backupdock source-backup`; it is normally invoked only by `remote-backup`. It loads only the temporary controller-provided configuration and then enters the same `_run_backup` path as a normal local backup. Discovery, dependency ordering, source validation, locking, Stop/Restic/Restart handling, manifests, preseed, and dry-run therefore remain shared rather than duplicated.

Automatic retention is intentionally disabled for `source-backup`. Destructive retention operations must not be sent through the append-only endpoint.

The controller and source handle `SIGINT`, `SIGHUP`, and `SIGTERM` without a Python traceback, returning exit codes 130, 129, and 143 respectively. Signal handlers are active before the controller's version check and before source discovery, and are restored when the command finishes. SSH remains non-interactive (`-T`); credentials still travel only through standard input.

On interruption, BackupDock terminates and waits for its active SSH or Restic child, escalating to a kill after five seconds if needed. Restic is stopped before container recovery begins. During recovery, additional interruption signals are ignored so every restart candidate can be attempted. A broken output pipe cannot prevent container recovery; temporary source-session configuration and credential environment variables are cleaned up afterward.

An SSH disconnect does not guarantee that OpenSSH delivers a signal to a non-PTY source process. Source recovery begins when it receives a handled signal, encounters a broken output pipe, or Restic reports a transport failure; it is not an acknowledgment from the source when the controller exits. Verify the actual SSH disconnect behavior on the deployment hosts. Forced process termination (`SIGKILL`) or host failure cannot run this cleanup.
