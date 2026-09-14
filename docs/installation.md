# Installation and Updates

[Documentation index](README.md) · [Main README](../README.md)


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
