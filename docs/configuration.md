# BackupDock Configuration Reference

[Documentation index](README.md) · [Main README](../README.md)


## Configuration

The default configuration path for local/controller operation is:

```text
/etc/backupdock/config.yaml
```

A different file can be selected with `--config`.

The repository contains [`config.yaml.example`](../config.yaml.example). The installer always copies the current example to:

```text
/etc/backupdock/config.yaml.example
```

The installer deliberately does **not** create the real configuration. Create it explicitly when needed:

```bash
sudo cp /etc/backupdock/config.yaml.example /etc/backupdock/config.yaml
sudo vim /etc/backupdock/config.yaml
```

If `/etc/backupdock/config.yaml` is missing and no alternative `--config` file is selected, normal local/controller commands write a warning to `stderr` and continue with built-in defaults where possible.

`backup.state_dir`, `backup.include_compose_metadata`, and `restic.backup_args` are shared defaults used by both local and remote backup runs. Source-specific paths, exclusions, ignored containers, and project rules are scoped to the source host:

- Local mode uses top-level `backup.host_paths`, `backup.exclude_paths`, `backup.exclude_volumes`, `backup.ignore_containers`, and `projects`.
- Remote mode uses `remotes.<name>.host_paths`, `remotes.<name>.exclude_paths`, `remotes.<name>.exclude_volumes`, `remotes.<name>.ignore_containers`, and `remotes.<name>.projects`.

This keeps the existing local-mode configuration compact while allowing one controller to manage multiple Docker hosts without mixing their source-specific rules.

Minimal local example using Restic's standard environment variables:

```yaml
backup:
  state_dir: "/var/lib/backupdock"
  include_compose_metadata: true
  host_paths: []
  exclude_paths: []
  exclude_volumes: []
  ignore_containers: []

projects: {}
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

For a local backup, project-specific data that Docker cannot discover can be attached explicitly:

```yaml
projects:
  paperless:
    extra_paths:
      - "/some/additional/path"
```

Static local-host paths can be backed up separately without stopping containers:

```yaml
backup:
  host_paths:
    - "/etc/some-static-config"
```

For a remote source, put the equivalent settings below that remote instead:

```yaml
remotes:
  docker-prod:
    # connection and repository settings omitted here
    host_paths:
      - "/etc/some-static-config"
    projects:
      paperless:
        extra_paths:
          - "/some/additional/path"
```

`host_paths` backs up the explicitly supplied directories in full as a separate `host` snapshot while containers remain running. Paths may overlap data included in project snapshots; BackupDock does not split the root or automatically exclude container mount paths. Explicitly configured exclusions and Restic backup arguments still apply. For application-consistent backups of live container data, attach that data to its project instead. A separate host snapshot may include live copies of those same files and does not carry the stopped-container consistency guarantee.

### Excluding Docker volumes

Docker volumes can be excluded by their Docker volume name. This avoids depending on Docker's host-side mount path.

For local mode, prefer a project-specific exclusion when the volume belongs to one Compose project:

```yaml
projects:
  backup-service:
    exclude_volumes:
      - "backup-service_backups"
```

For remote mode, keep the rule with the remote host that owns the project:

```yaml
remotes:
  docker-prod:
    # connection and repository settings omitted here
    projects:
      backup-service:
        exclude_volumes:
          - "backup-service_backups"
```

The other volumes of that project remain part of the backup. Volume names can be copied directly from `backupdock inventory` on the corresponding source host.

A source-wide exclusion is also available when needed. In local mode use `backup.exclude_volumes`; in remote mode use `remotes.<name>.exclude_volumes`.

### Ignoring exceptional containers

A running container created with Docker AutoRemove (`docker run --rm`, `HostConfig.AutoRemove=true`) cannot safely participate in the normal Stop/Backup/Restart transaction: Docker removes it when it exits, and anonymous volumes may also be removed. BackupDock therefore detects such containers before any selected container is stopped and aborts the backup.

For an explicitly accepted exception, an exact Docker container name can be ignored. In local mode:

```yaml
backup:
  ignore_containers:
    - "temporary-worker"
```

For a remote source, keep the exception with that source:

```yaml
remotes:
  docker-prod:
    # connection and repository settings omitted here
    ignore_containers:
      - "temporary-worker"
```

An ignored container is not stopped and its own mounts are not selected as backup sources. This is deliberately not a way to bypass consistency protection: if a running ignored container has writable storage that overlaps data selected for a project backup, BackupDock still aborts before any stop operation. `backupdock inventory` marks both `auto-remove` and `ignored` containers and shows mounts skipped because of an ignored container.

## Remote controller configuration

In remote mode, `/etc/backupdock/config.yaml` on the backup server is the single authoritative configuration. General BackupDock behavior remains under the top-level `backup` section, while source-specific paths, exclusions, ignored containers, and projects are stored below the corresponding `remotes.<name>` entry. This allows multiple heterogeneous Docker hosts to be managed from one controller configuration.

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
  include_compose_metadata: true

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

    host_paths: []
    exclude_paths: []
    exclude_volumes: []
    ignore_containers: []
    projects:
      backup-service:
        exclude_volumes:
          - "backup-service_backups"

    source_command:
      - "backupdock"
    ssh_options:
      - "-i"
      - "/root/.ssh/backupdock"
```

Top-level `projects` and the source-specific fields inside top-level `backup` are used only by local mode. They are never copied into a remote source session. Only the selected remote's `host_paths`, exclusions, ignored containers, and `projects` are rendered into that source's temporary configuration.

`local_rest_server_host` and `local_rest_server_port` are resolved on the backup server. `remote_tunnel_port` is opened by SSH on `127.0.0.1` of the Docker source for the lifetime of that SSH session only. `repository_path` becomes the path below the rest-server data root.

After creating the controller configuration and password files, initialize that remote's Restic repository directly through BackupDock:

```bash
backupdock init --remote docker-prod
```

The command validates the selected remote configuration and both password files before invoking `restic init` against the controller-local rest-server. It does not require SSH to the source host.
