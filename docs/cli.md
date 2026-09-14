# BackupDock Command-Line Reference

[Documentation index](README.md) · [Main README](../README.md)


Inspect the discovered groups, sources, dependencies, AutoRemove state, ignored containers, and excluded Docker volumes:

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

## Preseed large first backups

For a large first backup, `--preseed` can reduce the period during which containers must remain stopped:

```bash
backupdock backup --preseed
```

or in controller mode:

```bash
backupdock remote-backup docker-prod --preseed
```

For each stateful group BackupDock first runs a Restic backup while the group's containers are still running. This snapshot is tagged `backupdock-preseed` and must **not** be treated as the consistency-guaranteed backup because files may change while it is being created. If the preseed fails, BackupDock aborts before stopping that group's containers.

After a successful preseed, BackupDock stops the group normally and creates the final snapshot tagged `backupdock`. Restic can then reuse already stored data through its normal incremental/deduplication mechanisms, often reducing the amount of new data that must be transferred during downtime. Changed large files may still need to be read again during the final pass, so preseed reduces expected downtime but cannot guarantee a specific maximum.

On append-only remote repositories the source cannot delete the preseed snapshot. It remains clearly separated by its tag and can be removed later during controller-side repository maintenance.

## Live backup progress

When BackupDock's output is attached to a TTY, real Restic backup operations automatically use Restic's JSON status stream and render a single updating progress line, for example:

```text
[compose:nextcloud] backup   34.8%  18.2 GiB / 52.3 GiB  12,481 / 31,205 files  ETA 04:17
```

The same applies to a preseed pass, where the phase is shown as `preseed`.

Remote/controller mode deliberately keeps SSH in non-PTY mode (`ssh -T`) because the controller payload and credentials are sent over standard input. If the controller's stdout is a TTY, it asks the source-side BackupDock process to enable the same JSON progress renderer. The resulting carriage-return status line is forwarded through the existing SSH connection without allocating a remote terminal.

When stdout is not a TTY, for example under systemd or when redirected to a file, BackupDock keeps Restic's normal non-interactive output path and does not emit a continuously rewritten progress line.

The byte counters in the live line represent source data processed by Restic, not bytes transferred over the network or final repository growth. Deduplication and compression can make the amount actually stored much smaller.

Initialize the configured local Restic repository:

```bash
backupdock init
```

Initialize the repository belonging to one configured remote on the controller:

```bash
backupdock init --remote docker-prod
```

List the latest normal BackupDock snapshot per host and group in a compact table, without source paths or raw tags:

```bash
backupdock snapshots
```

The table shows group, host, retained snapshot count (`SNAPS`), container count (`CONTAINERS`), snapshot time, source-data size (`SIZE`), stored group data (`GROUP STORED`), and snapshot ID. A summary counts how many known host/group pairs have their latest snapshot dated today. Times and the meaning of `today` use the local timezone of the machine running the command. Older groups remain visible in the default listing, so a missing recent snapshot is easier to spot. Groups that have never produced a normal snapshot cannot be inferred from repository history alone.

`SNAPS` counts all retained normal snapshots for that host/group. `GROUP STORED` uses `restic stats --mode raw-data` over their explicit snapshot IDs, counting stored blobs once across that group's history after deduplication and compression. Both columns cover the whole retained group history even with `--today` or `--all`; unrelated hosts, other groups, and preseeds are excluded. Shared blobs can be referenced by several groups, so group sizes cannot be added to obtain total repository usage and do not indicate how much space deleting a group would reclaim. Pack overhead, keys, indexes, and unreferenced repository data are not included.

`CONTAINERS` comes from the manifest archived in the displayed snapshot, including containers that were stopped or explicitly ignored. It describes the backed-up group, not current Docker state. In the default listing it is the latest snapshot's count; with `--all`, each historical row uses its own manifest. Host-path snapshots show zero. Missing, unreadable, or unrecognized manifests show `unknown`. The command locates the original manifest path from snapshot metadata; if only a containing directory was backed up, it uses `backup.state_dir/manifests` from the current configuration.

The compact view reads group statistics and archived manifests in addition to the snapshot list. Large histories may take longer to inspect; an immediate status message announces the additional reads. No source-host SSH or Docker access is used. `--details` and `--json` retain their original full-listing behavior and skip these additional reads.

```bash
backupdock snapshots --today         # Latest snapshots whose timestamps are dated today
backupdock snapshots --all           # Compact history of all normal snapshots
backupdock snapshots --all --today   # All normal snapshots dated today
backupdock snapshots --details       # Original detailed Restic listing, including paths
backupdock snapshots --json          # Raw Restic JSON for all normal snapshots
backupdock snapshots --remote docker-prod
```

`--details` and `--json` are alternative full-listing formats and cannot be combined with `--all` or `--today`. Preseed snapshots are excluded from the compact listing. Sizes describe processed source data, not repository space or network transfer; snapshots without size statistics show `unknown`.

The snapshot timestamp records the start of that backup. An existing normal snapshot does not prove that Restic read every file without errors, that container recovery succeeded, or that the complete scheduled backup and maintenance sequence finished. Use the run log to confirm those outcomes; listing snapshots does not perform `restic check`.

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

`backupdock forget` first applies the configured retention policy to normal `backupdock` snapshots. It then removes a `backupdock-preseed` snapshot only if a remaining normal snapshot has the same hostname, BackupDock group tag, and source paths, and an equal or later timestamp. Preseeds without such a replacement remain available, including after an unsuccessful first consistent backup. Unrecognized or incomplete snapshot metadata is never used to authorize preseed removal. Preseeds do not participate in the normal retention selection.

`backupdock forget --prune` runs pruning once, after both retention and preseed cleanup succeed. Without pruning, snapshot removal alone does not reclaim all unreferenced repository data.

For `snapshots`, `check`, and `forget`, an explicit top-level `restic.repository` (including `RESTIC_REPOSITORY`) retains the existing local access behavior. Otherwise, a controller with exactly one remote automatically selects it. BackupDock reads `BACKUP_DIR` from `/etc/default/restic-rest-server`, appends that remote's `repository_path`, and uses the remote's repository `password_file`. The storage root and password do not need to be duplicated in the BackupDock configuration. Reads happen only when invoking these commands, so a missing service configuration does not affect `remote-backup` or inventory.

With several remotes, select the repository explicitly:

```bash
backupdock snapshots --remote docker-prod
backupdock check --remote docker-prod
backupdock forget --remote docker-prod --prune
```

`--remote` explicitly selects the remote even if a top-level repository is configured. Selection applies to one repository per command; BackupDock does not silently clean every remote.

If the packaged service uses another configuration file, set `restic.rest_server_config_file` to that file's path. Supported `BACKUP_DIR` assignments are literal absolute paths, optionally quoted and with whitespace or inline comments. The file is never executed or sourced. Missing, duplicate, relative, or shell-expanded values, a `--path` override, path traversal, a symlink escaping the root, or a missing repository `config` file cause an error before invoking Restic. Automatic discovery requires a loopback rest-server and does not inspect custom systemd command overrides or Docker mount mappings; configure an explicit local repository for those setups.

For append-only remote backups, run retention locally on the backup server against the repository path rather than through `remote-backup`.
