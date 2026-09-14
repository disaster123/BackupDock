# BackupDock Upgrade Notes

[Documentation index](README.md) · [Main README](../README.md)


Review these notes after updating an installation. Fixes cannot add missing data to snapshots that already exist.

## Upgrading existing service environment-file backups

Versions through 0.3.19 did not automatically include service-level `env_file` inputs unless they were already covered by another backup source. Updating does not add missing files to existing snapshots. Create a new backup of affected groups before testing restore:

```bash
backupdock remote-backup docker-prod --project app --preseed
backupdock restore --remote docker-prod --project app --as app-test --test --dry-run
```

BackupDock does not silently replace missing archived environment files with current production files or files from another snapshot.

## Upgrading existing bind-volume backups

Versions through 0.3.16 read the Docker mountpoint for local bind-backed volumes. After container stop, this could save only empty volume directories alongside Compose files and manifests. Small snapshots should therefore be checked for actual application and database files. Updating does not add missing data to existing snapshots.

After updating both controller and source, inspect a dry-run and force preseed for the first new backup of an affected group:

```bash
backupdock remote-backup docker-prod --project app --dry-run
backupdock remote-backup docker-prod --project app --preseed
```

Check the new snapshot's file listing and perform a restore test. Existing normal snapshots still satisfy the automatic preseed history check, so `--preseed` is explicitly recommended for this first corrected backup. Older preseed snapshots with different source paths are preserved by the normal safety rules for preseed cleanup.
