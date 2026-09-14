# Scheduling and Repository Maintenance

[Documentation index](README.md) · [Main README](../README.md)


BackupDock contains no scheduler. Example systemd unit and timer files are available in [`examples/`](../examples/).

For remote mode, schedule `backupdock remote-backup REMOTE_NAME` on the backup server. The reverse SSH tunnel then exists only for the duration of each scheduled backup.

## Cron on the backup server

A complete remote-mode example is available in [`examples/backupdock.cron`](../examples/backupdock.cron). Replace `docker-prod` in the commands and lock-file names with your configured remote name, then install it as `/etc/cron.d/backupdock`:

```bash
sudo install -o root -g root -m 0644 examples/backupdock.cron /etc/cron.d/backupdock
```

The example contains:

```cron
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Daily backup; apply retention and prune only after a successful backup.
0 3 * * * root /usr/bin/flock -n /run/lock/backupdock-docker-prod.lock /bin/sh -c '/usr/bin/backupdock remote-backup docker-prod && /usr/bin/backupdock forget --remote docker-prod --prune' >> /var/log/backupdock.log 2>&1

# Weekly repository check.
0 12 * * 0 root /usr/bin/flock -n /run/lock/backupdock-docker-prod.lock /usr/bin/backupdock check --remote docker-prod >> /var/log/backupdock.log 2>&1
```

The daily job starts at 03:00 in the backup server's local timezone. After a successful backup, it applies the retention policy from `/etc/backupdock/config.yaml`, removes superseded preseeds, and prunes unreferenced data. Automatic preseed selection needs no extra flag. The weekly job checks the repository on Sundays at 12:00. SSH authentication must work unattended as `root`, without password or host-key confirmation prompts. Keep the final newline in the cron file; `/etc/cron.d/` entries require the `root` user field.

BackupDock already uses a process lock around local/source backup orchestration, and Restic uses repository locks. The external `flock` is optional but recommended here: it covers the entire backup-and-maintenance sequence and serializes it with the weekly check. With `-n`, a job is skipped without waiting when the same lock is held. This lock coordinates only commands using that same file on the backup server; use the same `flock` wrapper for manual jobs if they must also be serialized. It does not remove stale Restic locks or replace `restic unlock`.

Output and errors are appended to `/var/log/backupdock.log`:

```bash
sudo tail -f /var/log/backupdock.log
```

Configure log rotation for this file and separate failure monitoring as needed. Redirecting output to the log means this example does not send Cron output by email.
