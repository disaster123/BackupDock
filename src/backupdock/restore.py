from __future__ import annotations

import copy
import json
import os
import re
import shlex
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from backupdock.locking import ProcessLock
from backupdock.processes import protected_cleanup, run_process
from backupdock.snapshots import select_snapshots


class RestoreError(RuntimeError):
    pass


# Unknown options require an explicit implementation before test containers run.
SERVICE_OPTIONS = set("""
image build platform environment env_file command entrypoint depends_on volumes
ports expose healthcheck user working_dir init stop_grace_period stop_signal
read_only tmpfs shm_size ulimits cpus mem_limit mem_reservation memswap_limit
pids_limit group_add stdin_open tty restart container_name hostname domainname
labels label_file networks profiles attach pull_policy
""".split())


def validate_test_name(project: str, name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) or name == project:
        raise RestoreError("--as must be a different project name using lowercase letters, digits, '_' or '-'")


def archive_path(value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or ".." in value.split("/"):
        raise RestoreError(f"Invalid archived absolute path: {value!r}")
    path = Path(value)
    if path == Path("/"):
        raise RestoreError("Restoring the filesystem root as a test mount is not supported")
    return path


def restored_path(root: Path, original: str) -> Path:
    path = root / archive_path(original).relative_to("/")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RestoreError(f"Restored path escapes the test directory: {original}")
    return path


def choose_snapshot(restic, project: str, requested: str, host: str | None = None) -> dict:
    if requested != "latest" and not re.fullmatch(r"[0-9a-f]{8,64}", requested):
        raise RestoreError("--snapshot must be 'latest' or a hexadecimal snapshot ID of at least eight characters")
    result = restic._run(["snapshots", "--tag", "backupdock", "--json"], capture=True)
    values = json.loads(result.stdout)
    if not isinstance(values, list):
        raise RestoreError("Restic returned an invalid snapshot list")
    records, _, _ = select_snapshots(values, all_snapshots=True, today=False, now=datetime.now().astimezone())
    matches = [record for record in records if record[3] == f"compose:{project}" and (host is None or record[2] == host)]
    if requested != "latest":
        matches = [record for record in matches if record[0]["id"].startswith(requested)]
    if not matches:
        raise RestoreError(f"No normal snapshot found for Compose project {project}")
    if len({record[2] for record in matches}) > 1:
        raise RestoreError("This project has snapshots from multiple hosts; select one with --host")
    if requested != "latest" and len(matches) != 1:
        raise RestoreError("Ambiguous snapshot ID; specify more characters")
    selected = max(matches, key=lambda record: (record[1], record[0]["id"]))[0]
    if not re.fullmatch(r"[0-9a-f]{64}", selected["id"]):
        raise RestoreError("Restic returned an invalid full snapshot ID")
    return selected


def read_manifest(restic, snapshot: dict, project: str, manifest_dir: Path) -> dict:
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"compose:{project}")
    paths = [archive_path(path) for path in snapshot.get("paths", [])]
    candidates = {path for path in paths if path.parent.name == "manifests" and path.name == f"{slug}.json"}
    fallback = manifest_dir / f"{slug}.json"
    if not candidates and any(fallback == path or fallback.is_relative_to(path) for path in paths):
        candidates.add(fallback)
    if len(candidates) != 1:
        raise RestoreError("Cannot uniquely locate the archived project manifest")
    value = json.loads(restic._run(["dump", snapshot["id"], str(next(iter(candidates)))], capture=True).stdout)
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("group"), dict) or value["group"].get("key") != f"compose:{project}":
        raise RestoreError("Unsupported or mismatched project manifest")
    containers = value.get("containers")
    sources = value.get("sources")
    if not isinstance(containers, list) or not containers or not isinstance(sources, list):
        raise RestoreError("Incomplete project manifest")
    services = []
    for container in containers:
        if not isinstance(container, dict) or container.get("ignored") or container.get("compose_project") != project:
            raise RestoreError("Test restore requires a complete Compose group without ignored containers")
        service = container.get("compose_service")
        if not isinstance(service, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service):
            raise RestoreError("Manifest contains an invalid Compose service")
        services.append(service)
    if len(set(services)) != len(services):
        raise RestoreError("Scaled Compose services are not supported by test restore yet")
    for source in sources:
        if not isinstance(source, dict):
            raise RestoreError("Invalid manifest source")
        archive_path(source.get("path"))
    return value


def covered(manifest: dict, original: str) -> bool:
    path = archive_path(original)
    return any(path == parent or path.is_relative_to(parent) for parent in (archive_path(source["path"]) for source in manifest["sources"]))


def mount_sources(manifest: dict) -> dict[tuple[str, str], str]:
    sources = {}
    for container in manifest["containers"]:
        mounts = container.get("mounts")
        if not isinstance(mounts, list):
            raise RestoreError("Manifest is missing container mount metadata")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise RestoreError("Manifest contains invalid mount metadata")
            if mount.get("type") not in ("bind", "volume"):
                raise RestoreError(f"Unsupported mount type for service {container['compose_service']}")
            original = mount.get("backup_source") or mount.get("source")
            archive_path(original)
            if mount.get("type") == "volume" and not mount.get("backup_source"):
                raise RestoreError("Volume metadata predates reliable volume discovery; create a new backup before test restore")
            if mount.get("backup_error") or not covered(manifest, original):
                raise RestoreError(f"Excluded or unsupported mount cannot be restored: {original}")
            if original.endswith((".sock", "/docker.sock", "/podman.sock")):
                raise RestoreError(f"Host socket mounts are not supported: {original}")
            key = (container["compose_service"], mount.get("destination"))
            archive_path(key[1])
            if key in sources:
                raise RestoreError("Duplicate mount destination in manifest")
            sources[key] = original
    return sources


def isolate_compose(model: dict, manifest: dict, root: Path, name: str) -> dict:
    services = model.get("services")
    expected = {container["compose_service"] for container in manifest["containers"]}
    if not isinstance(services, dict) or set(services) != expected:
        raise RestoreError("Compose services do not exactly match the archived container group")
    mapping = mount_sources(manifest)
    output = {"name": name, "services": {}, "networks": {"default": {"internal": True}}}
    for service, original in services.items():
        if not isinstance(original, dict):
            raise RestoreError(f"Invalid Compose service {service}")
        unknown = set(original) - SERVICE_OPTIONS
        if unknown:
            raise RestoreError(f"Service {service} has unsupported test-restore options: {', '.join(sorted(unknown))}")
        if not isinstance(original.get("image"), str) or not original["image"]:
            raise RestoreError(f"Service {service} requires an available image; build-only services are not supported yet")
        item = copy.deepcopy(original)
        for field in ("build", "container_name", "hostname", "domainname", "labels", "label_file", "profiles", "networks", "pull_policy"):
            item.pop(field, None)
        item["restart"] = "no"
        aliases = {original[field] for field in ("container_name", "hostname") if original.get(field)}
        original_networks = original.get("networks", {})
        if isinstance(original_networks, dict):
            for settings in original_networks.values():
                if isinstance(settings, dict):
                    aliases.update(settings.get("aliases") or [])
        item["networks"] = {"default": {"aliases": sorted(aliases)}} if aliases else ["default"]
        if any(value is None for value in item.get("environment", {}).values()):
            raise RestoreError(f"Service {service} inherits environment variables that are not stored in Compose; explicit archived values are required")
        mounts = []
        seen = set()
        for mount in item.get("volumes", []):
            if not isinstance(mount, dict) or mount.get("type") not in ("bind", "volume", "tmpfs"):
                raise RestoreError(f"Service {service} has an unsupported Compose mount")
            destination = mount.get("target")
            archive_path(destination)
            if mount["type"] == "tmpfs":
                mounts.append(mount)
                continue
            key = (service, destination)
            if key not in mapping:
                raise RestoreError(f"Mount is missing from the archived manifest: {service}:{destination}")
            seen.add(key)
            mounts.append({"type": "bind", "source": escape_interpolation(str(restored_path(root, mapping[key]))), "target": destination,
                           "read_only": bool(mount.get("read_only")), "bind": {"create_host_path": False}})
        if seen != {key for key in mapping if key[0] == service}:
            raise RestoreError(f"Compose mounts do not match the archived container: {service}")
        item["volumes"] = mounts
        ports = []
        for port in item.get("ports", []):
            if not isinstance(port, dict) or port.get("protocol", "tcp") not in ("tcp", "udp"):
                raise RestoreError(f"Unsupported port mapping in service {service}")
            target = port.get("target")
            if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= 65535:
                raise RestoreError("Port ranges are not supported by test restore yet")
            ports.append({"target": target, "host_ip": "127.0.0.1", "protocol": port.get("protocol", "tcp")})
        item["ports"] = ports
        output["services"][service] = item
    return output


def escape_interpolation(value):
    """Preserve literal dollars when Compose reads the resolved generated file."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escape_interpolation(item) for item in value]
    if isinstance(value, dict):
        return {key: escape_interpolation(item) for key, item in value.items()}
    return value


def compose_command(args: list[str]) -> list[str]:
    endpoint = os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"
    if not endpoint.startswith("unix://") or os.environ.get("DOCKER_CONTEXT", "default") not in ("", "default"):
        raise RestoreError("Test restore requires a local Docker Unix socket without a custom DOCKER_CONTEXT")
    # CLI contexts must not send host bind paths to a different Docker daemon.
    return ["docker", "--host", endpoint, "compose", *args]


class TestRestore:
    def __init__(self, restic, state_dir: Path):
        if not state_dir.is_absolute():
            raise RestoreError("Test restore requires an absolute backup.state_dir")
        self.restic = restic
        self.state_dir = state_dir

    @staticmethod
    def _compose(args: list[str], *, capture: bool = False, env=None):
        result = run_process(compose_command(args), text=True, capture_output=capture, env=env, check=False)
        if result.returncode:
            detail = (result.stderr or "").strip() if capture else ""
            raise RestoreError(f"Docker Compose failed with exit code {result.returncode}" + (f": {detail}" if detail else ""))
        return result

    def _prepare(self, snapshot: dict, manifest: dict, root: Path, name: str) -> dict:
        # Compose may read env/include files during normalization. Only archived
        # files are supplied, and include/extends/label_file are never evaluated.
        with tempfile.TemporaryDirectory(prefix="backupdock-restore-") as directory:
            temporary = Path(directory)
            clean_env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
            files = []
            inherited_variables = set()
            config_lists = {tuple(c.get("compose_config_files", [])) for c in manifest["containers"]}
            if len(config_lists) != 1 or not next(iter(config_lists)):
                raise RestoreError("Manifest must identify one ordered Compose file set")
            working_dirs = {c.get("compose_working_dir") for c in manifest["containers"]}
            if len(working_dirs) != 1 or not next(iter(working_dirs)):
                raise RestoreError("Manifest does not identify one Compose working directory")
            working_dir = restored_path(temporary, next(iter(working_dirs)))
            working_dir.mkdir(parents=True, exist_ok=True)

            def fetch(original: str) -> Path:
                if not covered(manifest, original):
                    raise RestoreError(f"Required configuration file was not included in this snapshot: {original}. Create a new project backup with service env_file discovery enabled.")
                destination = restored_path(temporary, original)
                if not destination.exists():
                    aliases = manifest.get("compose_file_aliases", {})
                    if not isinstance(aliases, dict):
                        raise RestoreError("Invalid archived Compose file aliases")
                    source = aliases.get(original, original)
                    if not covered(manifest, source):
                        raise RestoreError(f"Compose symlink target was not included in this snapshot: {source}")
                    content = self.restic._run(["dump", snapshot["id"], source], capture=True).stdout
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
                    destination.chmod(0o600)
                return destination

            for original in next(iter(config_lists)):
                path = fetch(original)
                try:
                    document = yaml.safe_load(path.read_text(encoding="utf-8"))
                except yaml.YAMLError as exc:
                    raise RestoreError("Cannot parse archived Compose YAML") from exc
                if not isinstance(document, dict) or "include" in document:
                    raise RestoreError("Compose include is not supported by test restore yet")
                raw_services = document.get("services", {})
                if not isinstance(raw_services, dict):
                    raise RestoreError("Invalid archived Compose services")
                for service, options in raw_services.items():
                    if not isinstance(options, dict) or "extends" in options:
                        raise RestoreError(f"Compose extends is not supported: {service}")
                    environment = options.get("environment", {})
                    if isinstance(environment, dict):
                        inherited_variables.update(key for key, value in environment.items() if value is None)
                    elif isinstance(environment, list):
                        inherited_variables.update(value for value in environment if isinstance(value, str) and "=" not in value)
                    options.pop("label_file", None)
                    options.pop("labels", None)
                    options.pop("build", None)
                path.write_text(yaml.safe_dump(document), encoding="utf-8")
                files.extend(["-f", str(path)])
            environment_files = {tuple(c.get("compose_environment_files", [])) for c in manifest["containers"]}
            if len(environment_files) != 1:
                raise RestoreError("Manifest contains different Compose interpolation file sets")
            explicit_env = next(iter(environment_files))
            env_args = []
            if explicit_env:
                for original in explicit_env:
                    env_args.extend(["--env-file", str(fetch(original))])
            else:
                dotenv = str(archive_path(next(iter(working_dirs))) / ".env")
                if any(source["path"] == dotenv for source in manifest["sources"]):
                    env_args = ["--env-file", str(fetch(dotenv))]
                else:
                    empty_env = temporary / "empty.env"
                    empty_env.touch(mode=0o600)
                    env_args = ["--env-file", str(empty_env)]
            args = ["--project-directory", str(working_dir), "-p", name, *env_args, *files]
            # ToModel (--no-interpolate) does not resolve service env files.
            # --no-env-resolution alone has differed between Compose versions.
            raw = json.loads(self._compose([*args, "config", "--format", "json", "--no-interpolate", "--no-path-resolution"], capture=True, env=clean_env).stdout)
            if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
                raise RestoreError("Docker Compose returned an invalid configuration")
            declarations = []
            for service, options in raw["services"].items():
                unknown = set(options) - SERVICE_OPTIONS
                if unknown:
                    raise RestoreError(f"Service {service} has unsupported test-restore options: {', '.join(sorted(unknown))}")
                entries = options.get("env_file", [])
                if isinstance(entries, str):
                    entries = [entries]
                for entry in entries:
                    item = {"path": entry} if isinstance(entry, str) else dict(entry)
                    if not isinstance(item.get("path"), str):
                        raise RestoreError("Invalid archived service environment file")
                    declarations.append((options, item))
                options.pop("env_file", None)
            if declarations or inherited_variables:
                # Resolve only declared path strings with Compose's own variable
                # rules, before any service env_file is allowed to be opened.
                path_environment = {
                    f"ARCHIVE_PATH_{index}": item["path"] for index, (_, item) in enumerate(declarations)
                }
                for index, variable in enumerate(sorted(inherited_variables)):
                    if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                        raise RestoreError("Unsupported inherited environment variable name")
                    path_environment[f"ARCHIVE_VARIABLE_{index}"] = "${" + variable + "?Required archived variable " + variable + "}"
                path_model = {"services": {"paths": {"image": "busybox", "environment": path_environment}}}
                path_file = temporary / "env-paths.json"
                path_file.write_text(json.dumps(path_model), encoding="utf-8")
                path_args = ["--project-directory", str(working_dir), "-p", name, *env_args, "-f", str(path_file)]
                paths = json.loads(self._compose([*path_args, "config", "--format", "json"], capture=True, env=clean_env).stdout)["services"]["paths"]["environment"]
                for index, (options, item) in enumerate(declarations):
                    value = paths[f"ARCHIVE_PATH_{index}"]
                    if not isinstance(value, str) or not value:
                        raise RestoreError("Cannot resolve archived service environment file path")
                    value = value.replace("$$", "$")
                    path = Path(value)
                    if not path.is_absolute():
                        path = archive_path(next(iter(working_dirs))) / path
                    original = os.path.normpath(str(path))
                    item["path"] = str(fetch(original)).replace("$", "$$")
                    options.setdefault("env_file", []).append(item)
            generated = temporary / "archived.json"
            generated.write_text(json.dumps(raw), encoding="utf-8")
            normalize_args = ["--project-directory", str(working_dir), "-p", name, *env_args, "-f", str(generated)]
            resolved = json.loads(self._compose([*normalize_args, "config", "--format", "json"], capture=True, env=clean_env).stdout)
            if not isinstance(resolved, dict):
                raise RestoreError("Docker Compose returned an invalid resolved configuration")
            model = isolate_compose(resolved, manifest, root, name)
            for options in model["services"].values():
                options.pop("env_file", None)
            # Compose config already returns interpolation-safe strings.
            return model

    @staticmethod
    def _check_project_unused(name: str) -> None:
        compose_command([])
        try:
            import docker
        except ImportError as exc:
            raise RestoreError("Docker SDK for Python is not installed") from exc
        try:
            client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise RestoreError(f"Cannot connect to the target Docker daemon: {exc}") from exc
        try:
            filters = {"label": f"com.docker.compose.project={name}"}
            if client.containers.list(all=True, filters=filters) or client.networks.list(filters=filters) or client.volumes.list(filters=filters):
                raise RestoreError(f"Docker project already exists: {name}; choose another --as name")
        except docker.errors.DockerException as exc:
            raise RestoreError(f"Cannot check the target Docker project: {exc}") from exc
        finally:
            client.close()

    def run(self, snapshot: dict, project: str, name: str, *, dry_run: bool = False, no_start: bool = False) -> None:
        validate_test_name(project, name)
        root = self.state_dir / "restores" / name
        if root.exists() or root.is_symlink():
            raise RestoreError(f"Test restore directory already exists: {root}; choose another --as name")
        self._check_project_unused(name)
        print(f"[{name}] reading snapshot {snapshot['id'][:8]} and project manifest", flush=True)
        manifest = read_manifest(self.restic, snapshot, project, self.state_dir / "manifests")
        mount_sources(manifest)
        model = self._prepare(snapshot, manifest, root, name)
        print(f"[{name}] test data directory: {root}", flush=True)
        for service, options in model["services"].items():
            print(f"[{name}] service {service}: isolated network; restart disabled", flush=True)
            for mount in options.get("volumes", []):
                if mount["type"] == "bind":
                    print(f"[{name}] mount {mount['source']} -> {service}:{mount['target']}", flush=True)
            for port in options.get("ports", []):
                print(f"[{name}] port {service}:{port['target']}/{port.get('protocol', 'tcp')} -> 127.0.0.1:automatic", flush=True)
        generated = root / "compose.test.json"
        command = ["-p", name, "-f", str(generated)]
        if dry_run:
            print(f"DRY-RUN restic restore {snapshot['id']} --target {root}")
            if not no_start:
                print(f"DRY-RUN {shlex.join(compose_command([*command, 'up', '-d', '--no-build']))}")
            return
        with ProcessLock(self.state_dir / "restore.lock"):
            self._check_project_unused(name)
            root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.mkdir(mode=0o700)
            print(f"[{name}] restoring application and database files", flush=True)
            self.restic._run(["restore", snapshot["id"], "--target", str(root)])
            root.chmod(0o700)
            for original in mount_sources(manifest).values():
                path = restored_path(root, original)
                if not path.exists() or not (path.is_dir() or path.is_file()):
                    raise RestoreError(f"Restored mount is missing or unsupported: {original}")
            generated.write_text(json.dumps(model, indent=2), encoding="utf-8")
            generated.chmod(0o600)
            if no_start:
                print(f"[{name}] prepared; start with {shlex.join(compose_command([*command, 'up', '-d', '--no-build']))}", flush=True)
                return
            self._check_project_unused(name)
            try:
                print(f"[{name}] starting test containers", flush=True)
                self._compose([*command, "up", "-d", "--no-build"])
            except BaseException:
                with protected_cleanup():
                    try:
                        self._compose([*command, "down", "--remove-orphans"])
                    except Exception as exc:
                        try:
                            print(f"[{name}] test cleanup failed: {exc}; inspect Docker project {name}", flush=True)
                        except OSError:
                            # Preserve the original failure after SSH output loss.
                            pass
                raise
            print(f"[{name}] test project started; ports are local to this Docker host", flush=True)
            self._compose([*command, "ps"])
            print(f"[{name}] stop test: {shlex.join(compose_command([*command, 'down']))}", flush=True)
