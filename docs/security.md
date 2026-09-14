# BackupDock Security Model

[Documentation index](README.md) · [Main README](../README.md)


BackupDock requires privileged access to Docker and host-side persistent data. Treat its configuration, repository credentials, SSH keys, and restored data as sensitive.

## Local mode

- Run BackupDock with permission to access the Docker Unix socket and every selected source path.
- Protect the Restic password file with root ownership and mode `0600`.
- Keep the repository on independent storage for disaster recovery.
- Use `backupdock backup --dry-run` to inspect sources and planned Docker actions before the first real backup.

## Remote/controller mode

- Keep the permanent configuration and all repository credentials on the backup server.
- Bind rest-server to loopback only, require HTTP authentication, and enable `--append-only`.
- BackupDock transfers temporary session configuration and credentials through SSH standard input.
- The source-side session directory is mode `0700`; its temporary configuration is mode `0600` and is removed when the session ends.
- Repository maintenance such as `forget` and `prune` runs locally on the controller, not through the append-only source endpoint.
- Controller and source versions must match exactly before credentials or configuration are sent.

See [Remote Docker Backups](remote-backup.md) for the complete tunnel and rest-server setup.

## Container safety

Before stopping a container, BackupDock validates discovery, dependencies, selected paths, unsupported volumes, AutoRemove containers, ignored-container writers, and writable overlap between groups.

Restart is attempted even after backup failure or interruption. Containers that were stopped before the run remain stopped. `SIGKILL` and host failure cannot execute recovery logic.

See [Docker Compose Backup Behavior](backup-behavior.md#safety-behavior) for the full transaction model.

## Restore isolation

Test restores use copied data, a new Compose project, an internal network, disabled restart policies, and automatically assigned ports bound to `127.0.0.1`. For stronger isolation from production workloads, restore on a separate Docker host.

BackupDock does not archive Docker images or rewrite secrets embedded in application files. Restored content should receive the same access protection as production data.
