from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import yaml

from backupdock.models import BackupSource, ContainerInfo
from backupdock.processes import run_process


class ComposeMetadataError(RuntimeError):
    pass


def service_environment_sources(containers: list[ContainerInfo]) -> list[BackupSource]:
    """Discover service env_file inputs in addition to Compose interpolation files."""
    contexts = {}
    for container in containers:
        if container.ignored or not container.compose_config_files:
            continue
        context = (container.compose_project, container.compose_working_dir,
                   container.compose_config_files, container.compose_environment_files)
        contexts.setdefault(context, set()).add(container.compose_service)
    paths: dict[Path, bool] = {}
    for (project, working_dir, config_files, interpolation_files), services in contexts.items():
        base = Path(working_dir) if working_dir else Path(config_files[0]).parent
        declarations = []
        for config_file in config_files:
            path = Path(config_file)
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                if "env_file" not in text:
                    continue
                document = yaml.safe_load(text)
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise ComposeMetadataError(f"Cannot read service environment declarations from {path}") from exc
            if not isinstance(document, dict) or not isinstance(document.get("services", {}), dict):
                raise ComposeMetadataError(f"Invalid Compose services in {path}")
            for service, options in document.get("services", {}).items():
                if service not in services or not isinstance(options, dict):
                    continue
                entries = options.get("env_file", [])
                if isinstance(entries, str):
                    entries = [entries]
                if not isinstance(entries, list):
                    raise ComposeMetadataError(f"Invalid env_file declaration for service {service} in {path}")
                for entry in entries:
                    item = {"path": entry} if isinstance(entry, str) else entry
                    if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                            or not item["path"] or not isinstance(item.get("required", True), bool)):
                        raise ComposeMetadataError(f"Invalid env_file entry for service {service} in {path}")
                    declarations.append((item["path"], item.get("required", True)))
        resolved = [value for value, _ in declarations]
        if any("$" in value for value in resolved):
            # Let Compose resolve defaults, escaped dollars and interpolation
            # files without opening service env files or creating Docker objects.
            with tempfile.TemporaryDirectory(prefix="backupdock-env-paths-") as directory:
                temporary = Path(directory)
                model = {"services": {"paths": {"image": "busybox", "environment": {
                    f"ENV_PATH_{index}": value for index, value in enumerate(resolved)
                }}}}
                config = temporary / "paths.json"
                config.write_text(json.dumps(model), encoding="utf-8")
                config.chmod(0o600)
                env_files = list(interpolation_files)
                if not env_files and (base / ".env").is_file():
                    env_files = [str(base / ".env")]
                if not env_files:
                    empty = temporary / "empty.env"
                    empty.touch(mode=0o600)
                    env_files = [str(empty)]
                command = ["docker", "compose", "--project-directory", str(base), "-p", project]
                for env_file in env_files:
                    command.extend(["--env-file", env_file])
                command.extend(["-f", str(config), "config", "--format", "json"])
                env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
                try:
                    result = run_process(command, text=True, capture_output=True, env=env, check=False)
                    if result.returncode:
                        raise ComposeMetadataError(f"Cannot resolve service env_file paths for project {project}; check Compose interpolation files and Docker Compose")
                    values = json.loads(result.stdout)["services"]["paths"]["environment"]
                    resolved = [values[f"ENV_PATH_{index}"].replace("$$", "$") for index in range(len(resolved))]
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise ComposeMetadataError(f"Cannot resolve service env_file paths for project {project}; Docker Compose V2 with JSON config output is required") from exc
        for value, (_, required) in zip(resolved, declarations):
            if not value or "\x00" in value:
                raise ComposeMetadataError(f"Empty or invalid service env_file path in project {project}")
            path = Path(value)
            if not path.is_absolute():
                path = base / path
            path = Path(os.path.abspath(path))
            paths[path] = paths.get(path, False) or required
    return [BackupSource(path, "compose", required=required) for path, required in sorted(paths.items())]
