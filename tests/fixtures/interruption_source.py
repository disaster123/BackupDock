"""Source CLI exercised in a real process with simulated Docker and Restic."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from backupdock import cli
from backupdock.models import BackupGroup, BackupSource, ContainerInfo
from backupdock.restic import ResticRunner


root = Path(sys.argv[1])
data = root / "data"
data.mkdir()


def event(value):
    with (root / "events").open("a") as stream:
        stream.write(value + "\n")


class Docker:
    def stop(self, container_id):
        event("stop " + container_id)

    def ensure_running(self, container_id):
        event("start " + container_id)

    def close(self):
        event("docker closed")


containers = [
    ContainerInfo(
        id=name, name=name, running=running, compose_project="app",
        compose_service=name, compose_dependencies=dependencies,
        compose_working_dir=None, compose_config_files=(),
        compose_environment_files=(), mounts=(),
    )
    for name, running, dependencies in (("db", True, ()), ("web", True, ("db",)), ("disabled", False, ()))
]
group = BackupGroup(key="compose:app", name="app", compose_project="app", containers=containers, sources=[BackupSource(data, "bind")])
binary = root / "restic"
binary.write_text(
    "#!" + sys.executable + "\n"
    "import json,os,time\n"
    "from pathlib import Path\n"
    "Path(" + repr(str(root / "restic.pid")) + ").write_text(str(os.getpid()))\n"
    "print('restic ready', flush=True)\n"
    "for _ in range(600):\n"
    " time.sleep(0.1)\n"
    " print(json.dumps({'message_type': 'status', 'bytes_done': 1}), flush=True)\n"
)
binary.chmod(0o700)
with (
    patch.object(cli, "SOURCE_RUNTIME_DIR", root / "runtime"),
    patch.object(cli, "DEFAULT_CONFIG_PATH", root / "unused-config"),
    patch.object(cli, "DockerBackend", return_value=Docker()),
    patch.object(cli, "_discover", return_value=([group], [])),
    patch.object(ResticRunner, "preflight", return_value={"compose:app"}),
):
    result = cli.main(["source-backup", "--progress"])
event("secrets cleared " + str(all(key not in os.environ for key in ("RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD"))))
sys.exit(result)
