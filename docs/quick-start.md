# BackupDock Quick Start

[Documentation index](README.md) · [Main README](../README.md)


This guide creates a first local Docker Compose backup and verifies that the snapshot exists. It uses a local filesystem repository for a quick functional test.

> A repository on the same disk is not disaster-resistant. After the first successful test, move the repository to separate storage or configure [remote/controller mode](remote-backup.md).

## 1. Check the requirements

- Linux
- Python 3.11 or newer
- Docker Engine
- root access

Restic is installed automatically on supported package managers when it is missing.

## 2. Install BackupDock

```bash
git clone https://github.com/disaster123/BackupDock.git
cd BackupDock
sudo ./install.sh
backupdock --version
```

## 3. Create a test repository and configuration

Create a repository directory and a protected password file:

```bash
sudo install -d -o root -g root -m 0700 /srv/backupdock-repository
sudo install -d -o root -g root -m 0700 /etc/backupdock
sudo install -o root -g root -m 0600 /dev/null /etc/backupdock/repository-password
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | sudo tee /etc/backupdock/repository-password >/dev/null
sudo cp /etc/backupdock/config.yaml.example /etc/backupdock/config.yaml
sudoedit /etc/backupdock/config.yaml
```

Uncomment and set these two values under `restic`:

```yaml
restic:
  repository: "/srv/backupdock-repository"
  password_file: "/etc/backupdock/repository-password"
```

The remaining defaults can stay unchanged for the first test.

## 4. Initialize the Restic repository

```bash
sudo backupdock init
```

A successful command confirms that the repository was created.

## 5. Preview Docker discovery

```bash
sudo backupdock inventory
sudo backupdock backup --dry-run
```

`inventory` should list the detected Compose projects, containers, bind mounts, and Docker volumes. The dry-run prints the planned stop, backup, and restart operations without changing Docker or writing a snapshot.

Review the listed paths. Persistent application and database data must appear before continuing.

## 6. Create the first backup

```bash
sudo backupdock backup
```

BackupDock processes one consistency group at a time. It stops only the containers that were running in that group, creates the Restic snapshot, and then restarts exactly those containers.

## 7. Verify the result

```bash
sudo backupdock snapshots
```

A successful first backup shows at least one row with a group name, host, snapshot time, size, and snapshot ID. Also review the backup command's final output: the existence of a snapshot alone does not prove that every container restarted successfully.

## Next steps

- [Configure a production repository and exclusions](configuration.md)
- [Set up backup-server-initiated remote backups](remote-backup.md)
- [Schedule backups, retention, and repository checks](scheduling.md)
- [Restore a Compose project as an isolated test](restore.md)
