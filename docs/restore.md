# Restore and Test Docker Compose Backups

[Documentation index](README.md) · [Main README](../README.md)


Restore an entire Compose group, including its database, as a separate test project:

```bash
backupdock restore --remote docker-prod --project app --snapshot latest --as app-test --test --dry-run
backupdock restore --remote docker-prod --project app --snapshot latest --as app-test --test
```

The controller reads normal snapshot metadata through the same local repository access used by `snapshots`. `latest` selects the latest normal `backupdock` snapshot of this exact Compose group; preseeds never qualify. Use `--snapshot SNAPSHOT_ID` for a specific snapshot. If the group has snapshots from multiple hosts, select the original hostname with `--host HOSTNAME`.

With `--remote`, BackupDock restores on that remote's Docker host through the existing reverse SSH tunnel. Repository and REST credentials travel through standard input, and no permanent source configuration or intermediate data copy on the controller is needed. Update BackupDock on both hosts to the same version. To restore on another Docker host with the same BackupDock, Restic and Docker Compose installations, add `--ssh-target root@test-host`; controller SSH options and the source command remain those of the selected repository remote.

Without `--remote`, restore runs on the local Docker host using the configured repository. This also requires Docker on the machine executing the command; a controller-only server should use `--remote`. On the target, Docker must use a local Unix socket; TCP Docker endpoints and a custom `DOCKER_CONTEXT` are refused. Compose commands explicitly select that socket so CLI context settings cannot redirect restored host paths to another daemon.

The first implementation supports complete Compose groups with one container per service and an available `image` for every service. Docker Compose V2 must support `config --format json --no-interpolate --no-path-resolution`. The archived Compose file set, interpolation files and declared service environment files must be present in the selected snapshot. Variables inherited only from the original shell are not reconstructed. Missing data, excluded mounts, ignored containers, old volume metadata without a reliable backup source, build-only services, Compose `include`/`extends`, privileged/device/host-network settings and other unsupported service options fail before restoring persistent data or starting containers. File-backed Compose `secrets` and `configs` are not supported yet.

Data is restored below `backup.state_dir/restores/NEW_NAME`, preserving original absolute paths beneath that directory. All archived bind and Docker-volume mounts become bind mounts of these restored copies, including local bind-backed volumes. Docker's production volume paths, explicit volume names and volume drivers are not reused. Mount sources must exist after restore and may not resolve outside the test directory. Existing restore directories and Docker projects are refused; use a fresh `--as` name for another test.

The generated `compose.test.json` uses the new project name, removes explicit container names, build definitions and reverse-proxy labels, disables restart policies and joins services to one project-owned internal network. Existing service network aliases and container names remain DNS aliases only inside that test network, so common database references continue to work. Published ports receive automatic host ports bound to `127.0.0.1` on the target Docker host. Startup prints `docker compose ps` with the assigned ports. To access a web interface from another machine, forward the printed port through SSH, for example:

```bash
ssh -L 18080:127.0.0.1:ASSIGNED_PORT root@test-host
# Open http://127.0.0.1:18080 on the SSH client's machine.
```

The internal network prevents ordinary external network access, so integrations such as outgoing mail may not work during the test. Application files, environment values and embedded URLs are restored as archived; BackupDock does not rewrite arbitrary application configuration or guarantee that every application works without adjustments. For stronger separation from the production Docker host, use a separate test host. Images and container writable layers are not backed up: required images must still be available, and floating image tags may resolve to a different version. Pin image versions/digests when reproducible restores are needed.

`--dry-run` reads the snapshot, manifest and required configuration through Restic, uses private temporary files for Compose normalization, and checks project availability on the target Docker host. It prints mount/port mappings without creating the persistent restore directory or changing Docker resources. It therefore requires working repository authentication, SSH and Docker Compose access. Temporary files and session credentials are removed afterward.

`--no-start` restores and prepares the project but leaves its containers uncreated, printing the start command for inspection. A normal test restore starts all services with `docker compose up -d --no-build`; successful startup means Docker accepted the project, not that application data or health has been verified. Check login, database content and representative files yourself. If startup fails or is interrupted, BackupDock attempts to remove only the test project's Docker resources and retains the restored data. Cleanup errors do not hide the original failure; inspect the project if cleanup cannot finish. SSH loss can delay source cleanup until the source observes transport failure, as with remote backup.

## Remove a test restore

Run cleanup on the Docker host where the test project was created. `--volumes` also removes anonymous or project-owned volumes that an image may have created:

```bash
sudo docker compose \
  -p app-test \
  -f /var/lib/backupdock/restores/app-test/compose.test.json \
  down --volumes --remove-orphans
```

Verify that no project containers, volumes, or networks remain:

```bash
sudo docker ps -a --filter label=com.docker.compose.project=app-test
sudo docker volume ls --filter label=com.docker.compose.project=app-test
sudo docker network ls --filter label=com.docker.compose.project=app-test
```

Each command should show only its heading. Then remove the restored data copy:

```bash
sudo rm -rf -- /var/lib/backupdock/restores/app-test
```

This removes the test containers, test network, project or anonymous test volumes, generated Compose configuration, and restored data. It does not remove Restic snapshots or production data.

Images downloaded for the test may remain in Docker because other projects can share them. BackupDock's common `restore.lock` may also remain below the state directory and does not belong to a particular test restore.

Test containers are ordinary Compose containers and can be discovered by later backup runs. Remove the test project after validation if it should not become another backed-up group.

In-place restores, standalone-container recreation and automatic image archival are not implemented yet. Existing Restic snapshots can also be restored manually with Restic.
