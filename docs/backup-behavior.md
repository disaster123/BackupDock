# Docker Compose Backup Behavior

[Documentation index](README.md) · [Main README](../README.md)


## Design goals

- No assumptions about host directory layouts.
- No mandatory Docker labels.
- Automatic bind-mount and Docker-volume discovery.
- Docker Compose projects are consistency groups.
- Only containers in the project currently being backed up are stopped.
- Within a Compose project, running services are stopped in reverse dependency order and restarted in dependency order.
- Containers that were already stopped stay stopped.
- Running state is restored even when Restic or a stop operation fails.
- Docker's own stop timeout and stop signal configuration are respected; BackupDock does not override the stop timeout.
- Running `--rm`/AutoRemove containers are detected before any stop operation and fail safely unless explicitly ignored.
- Explicitly ignored containers cannot silently defeat consistency checks when they have writable access to selected project backup data.
- Standalone containers are supported as one-container backup groups.
- Shared/overlapping persistent storage across different groups fails safely instead of silently producing an inconsistent backup.
- An optional preseed pass can warm a repository while containers are still running before the final consistent stopped-container snapshot.
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
- container AutoRemove (`--rm`) state
- Docker Compose project and service names
- Docker Compose service dependency metadata
- Compose config-file metadata exposed by Docker Compose
- the Compose working-directory `.env` file when present
- service-level `env_file` inputs declared in the Compose file set for known, non-ignored services

`tmpfs` and non-file bind sources such as sockets or FIFOs are ignored because they are not persistent backup data.

There are **no built-in paths such as `/srv/files`, `/srv/docker-config`, `/opt/docker`, or `/var/lib/docker/volumes`**. Volume source paths come from Docker itself.

BackupDock also inspects each named or anonymous volume's driver and options. Ordinary local volumes use their persistent host directory. Local bind-backed volumes (`o=bind` or `rbind`, `type` empty or `none`, and an absolute `device`) use the actual device directory, because Docker's volume mountpoint may be unmounted when the last container stops. The manifest preserves Docker's original mount source and records the resolved backup source. Mounted subdirectories are preserved. Exclusions by volume name remain unchanged; path exclusions recognize both the original and resolved path. Storage overlap and ignored-container writer checks use the resolved data path.

Included volumes with uninspectable metadata, plugin drivers, or unsupported local mount options such as NFS, CIFS, or block-device filesystems abort discovery before any container stop. Explicitly excluded volumes are skipped; an ignored running container with an unresolved writable volume also fails because its storage overlap cannot be verified. These volume types need a separate supported backup strategy rather than reading a potentially empty Docker mountpoint.

Docker images and the container writable filesystem layer are not included. Compose configuration and backed-up persistent data support recreating containers, provided their images can be pulled or rebuilt. Files stored only in the container layer need a separate backup strategy.

With `backup.include_compose_metadata: true`, service environment files are included alongside the Compose configuration. Relative paths are resolved from the Compose working directory. String/list declarations and long-form `path`/`required` entries are supported; a missing required file aborts discovery before containers are stopped, while missing optional files are skipped during backup. Explicit path exclusions still apply. Symlink metadata files retain their original link and also include the target file; the manifest records the mapping so test restore reads archived contents rather than following the link into a live host path.

Variable-based `env_file` paths require Docker Compose V2 with JSON config output. BackupDock resolves those paths using the project's `.env` or the interpolation files recorded by Docker, without inheriting unrelated shell variables. Docker Compose is not required for discovering plain literal environment-file paths. Compose `include`/`extends` workflows still require separate consideration and are unsupported by the current test restore.

## Safety behavior

Before any container is stopped, BackupDock:

1. discovers every container and persistent mount,
2. groups containers into Compose/standalone consistency groups,
3. validates Compose dependency ordering,
4. checks for unsafe writable storage overlap between groups,
5. rejects running AutoRemove containers that would have to be stopped,
6. rejects writable overlap from running ignored containers into selected backup data,
7. verifies the Restic repository is reachable.

These checks happen before BackupDock starts changing container state. A safety failure therefore does not leave earlier projects stopped while a later unsafe project is discovered.

For each group it records which containers are running. Running containers are stopped in reverse dependency order and backed up inside a guarded transaction. BackupDock does not override Docker's stop timing; the stop request is sent without a timeout argument so Docker applies the container's configured stop behavior. Restart is attempted in dependency order even after a backup failure, interruption, or partial stop failure. Containers that were already stopped are never added to the restart set.

Dry-run deliberately follows this same orchestration code path. `DockerBackend` and `ResticRunner` switch only their execution behavior: Docker mutations and Restic subprocess calls are rendered as `DRY-RUN ...` output. Manifest generation follows the same routine but prints the target manifest path instead of writing it. The normal process lock is still acquired so the plan is not produced concurrently with another BackupDock run.

A manifest describing the group, containers, dependency metadata, AutoRemove state, mount destinations, volume names, and host source paths is stored under BackupDock's state directory and included in the Restic snapshot during a real backup. Test restore uses this archived metadata to map each service's mounts to restored data.
