# Changelog

All notable user-facing changes to BackupDock releases are documented here.

## [0.3.20] - 2026-09-14

This is the first tagged public BackupDock release. BackupDock is still early alpha software; test restores before relying on it for production data.

### Highlights

- Added isolated Docker Compose test restores locally or on a configured remote host.
- Added automatic discovery of bind mounts, ordinary Docker volumes and local bind-backed volumes.
- Added consistent per-project backups with ordered container stop and restart handling.
- Added incremental Restic backups, preseed support, retention, maintenance and snapshot reporting.
- Added backup-server-initiated remote backups through a temporary reverse SSH tunnel.
- Added explicit full host-path backups alongside container projects.

### Important fixes

- Bind-backed Docker volumes now use their actual host data path. Versions through 0.3.16 could create apparently successful snapshots containing empty volume directories.
- Service-level Compose `env_file` inputs are now included. Versions through 0.3.19 could omit these files from project snapshots.
- Test restores use archived Compose configuration and environment files instead of current production files.

### Upgrade notes

- Install exactly the same BackupDock version on the controller and every remote source host.
- Existing snapshots are not repaired by upgrading. Create a new backup with `--preseed` for projects using bind-backed volumes or service `env_file` inputs, then perform a restore test.
- Docker images and container writable layers are not backed up.
- In-place production restore orchestration is not implemented yet.
