from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock import __version__
from backupdock.cli import BackupInterrupted, main
from backupdock.config import AppConfig, BackupConfig, RemoteConfig
from backupdock.remote import RemoteBackupController, REMOTE_PROTOCOL_VERSION
from backupdock.restore import (RestoreError, TestRestore, choose_snapshot, compose_command, escape_interpolation,
                                isolate_compose, mount_sources, read_manifest, restored_path)


def snapshot(char="a", *, project="app", host="source.example", time="2026-01-20T03:00:00Z", preseed=False):
    return {"id": char * 64, "hostname": host, "time": time,
            "tags": ["backupdock-preseed" if preseed else "backupdock", f"backupdock-group=compose:{project}"],
            "paths": ["/srv/data", f"/var/lib/backupdock/manifests/compose_{project}.json"]}


def manifest():
    containers = []
    sources = [{"path": "/srv/config/app/compose.yaml"}, {"path": "/srv/config/app/.env"}]
    for service in ("web", "db"):
        original = f"/srv/data/{service}"
        containers.append({"id": service, "name": f"app-{service}-1", "compose_project": "app",
                           "compose_service": service, "compose_config_files": ["/srv/config/app/compose.yaml"],
                           "compose_working_dir": "/srv/config/app", "compose_environment_files": [],
                           "mounts": [{"type": "volume", "source": f"/var/lib/docker/volumes/app_{service}/_data",
                                       "backup_source": original, "destination": "/data"}]})
        sources.append({"path": original})
    return {"schema": 1, "group": {"key": "compose:app"}, "containers": containers, "sources": sources}


def compose_model():
    return {"name": "app", "volumes": {"data": {"external": True}},
            "networks": {"production": {"external": True}}, "services": {
                service: {"image": "example/app:1.0", "environment": {"VALUE": "literal$secret"},
                          "volumes": [{"type": "volume", "source": "data", "target": "/data"}],
                          "networks": {"production": {"aliases": [f"{service}-alias"]}},
                          "container_name": f"original-{service}", "labels": {"proxy.enable": "true"},
                          "restart": "always", "ports": [{"target": 8080, "published": "8080", "host_ip": "0.0.0.0"}]}
                for service in ("web", "db")}}


class RestoreSelectionTests(unittest.TestCase):
    def choose(self, values, requested="latest", host=None):
        runner = MagicMock()
        runner._run.return_value.stdout = json.dumps(values)
        return choose_snapshot(runner, "app", requested, host)

    def test_latest_ignores_preseed_and_other_groups(self):
        values = [snapshot(), snapshot("b", preseed=True, time="2026-01-21T03:00:00Z"),
                  snapshot("c", project="other", time="2026-01-22T03:00:00Z")]
        self.assertEqual(self.choose(values)["id"], "a" * 64)

    def test_latest_compares_timestamps_and_snapshot_prefix(self):
        values = [snapshot(), snapshot("b", time="2026-01-21T03:00:00Z")]
        self.assertEqual(self.choose(values)["id"], "b" * 64)
        self.assertEqual(self.choose(values, "a" * 8)["id"], "a" * 64)

    def test_multiple_hosts_require_explicit_selection(self):
        values = [snapshot(), snapshot("b", host="other.example")]
        with self.assertRaisesRegex(RestoreError, "multiple hosts"):
            self.choose(values)
        self.assertEqual(self.choose(values, host="other.example")["id"], "b" * 64)

    def test_no_snapshot_or_invalid_id_fails(self):
        for values, requested in [([], "latest"), ([snapshot()], "deadbeef"), ([snapshot()], "a; touch /tmp/x")]:
            with self.subTest(requested=requested), self.assertRaises(RestoreError):
                self.choose(values, requested)

    def test_manifest_group_schema_ignored_and_scaling_are_checked(self):
        runner = MagicMock()
        original = manifest()
        variants = [dict(original, schema=2), dict(original, group={"key": "compose:other"}),
                    dict(original, group=None), dict(original, containers=[])]
        ignored = copy.deepcopy(original)
        ignored["containers"][0]["ignored"] = True
        scaled = copy.deepcopy(original)
        scaled["containers"].append(scaled["containers"][0])
        for value in variants + [ignored, scaled]:
            runner._run.return_value.stdout = json.dumps(value)
            with self.subTest(value=value), self.assertRaises(RestoreError):
                read_manifest(runner, snapshot(), "app", Path("/var/lib/backupdock/manifests"))


class RestoreIsolationTests(unittest.TestCase):
    def test_production_names_mounts_ports_and_networks_are_replaced(self):
        original = compose_model()
        before = copy.deepcopy(original)
        isolated = isolate_compose(original, manifest(), Path("/restore/app-test"), "app-test")
        self.assertEqual(original, before)
        self.assertEqual(isolated["networks"], {"default": {"internal": True}})
        self.assertNotIn("volumes", isolated)
        for service, options in isolated["services"].items():
            self.assertNotIn("container_name", options)
            self.assertNotIn("labels", options)
            self.assertEqual(options["restart"], "no")
            self.assertEqual(options["volumes"][0]["source"], f"/restore/app-test/srv/data/{service}")
            self.assertEqual(options["volumes"][0]["bind"], {"create_host_path": False})
            self.assertEqual(options["ports"], [{"target": 8080, "host_ip": "127.0.0.1", "protocol": "tcp"}])
            self.assertIn(f"original-{service}", options["networks"]["default"]["aliases"])

    def test_privileges_external_references_and_hooks_fail_before_start(self):
        for key, value in [("privileged", True), ("network_mode", "host"), ("devices", ["/dev/sda"]),
                           ("pid", "host"), ("volumes_from", ["production"]), ("extra_hosts", ["db:1.2.3.4"]),
                           ("post_start", [{"command": "touch /tmp/x"}]), ("use_api_socket", True)]:
            model = compose_model()
            model["services"]["web"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(RestoreError, "unsupported"):
                isolate_compose(model, manifest(), Path("/restore"), "test")

    def test_missing_service_mount_or_inherited_environment_fails(self):
        variants = []
        for change in ("service", "mount", "environment", "image", "port"):
            model = compose_model()
            if change == "service":
                del model["services"]["db"]
            elif change == "mount":
                model["services"]["web"]["volumes"] = []
            elif change == "environment":
                model["services"]["web"]["environment"] = {"SECRET": None}
            elif change == "image":
                del model["services"]["web"]["image"]
            else:
                model["services"]["web"]["ports"][0]["target"] = "8000-8010"
            variants.append(model)
        for model in variants:
            with self.subTest(model=model), self.assertRaises(RestoreError):
                isolate_compose(model, manifest(), Path("/restore"), "test")

    def test_excluded_old_volume_socket_and_invalid_sources_fail(self):
        for change in ("excluded", "old", "socket", "traversal", "error"):
            value = manifest()
            mount = value["containers"][0]["mounts"][0]
            if change == "excluded":
                value["sources"] = value["sources"][:2]
            elif change == "old":
                del mount["backup_source"]
            elif change == "socket":
                mount["backup_source"] = "/srv/data/docker.sock"
            elif change == "traversal":
                mount["backup_source"] = "/srv/data/../outside"
            else:
                mount["backup_error"] = "unsupported driver"
            with self.subTest(change=change), self.assertRaises(RestoreError):
                mount_sources(value)

    def test_mount_symlink_cannot_escape_restore_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "test"
            root.mkdir()
            (root / "srv").symlink_to(Path(directory), target_is_directory=True)
            with self.assertRaisesRegex(RestoreError, "escapes"):
                restored_path(root, "/srv/data")

    def test_resolved_dollars_are_escaped_recursively(self):
        self.assertEqual(escape_interpolation({"environment": {"TOKEN": "a$b"}, "command": ["echo", "$HOME"]}),
                         {"environment": {"TOKEN": "a$$b"}, "command": ["echo", "$$HOME"]})

    def test_remote_docker_endpoints_and_custom_contexts_are_refused(self):
        for environment in ({"DOCKER_HOST": "tcp://production.example:2375"}, {"DOCKER_CONTEXT": "production"}):
            with patch.dict(os.environ, environment), self.assertRaisesRegex(RestoreError, "local Docker"):
                compose_command(["up", "-d"])


class RestoreExecutionTests(unittest.TestCase):
    def run_restore(self, root, *, dry_run=False, no_start=False, compose_side_effect=None, restore_side_effect=None):
        runner = MagicMock()
        def restore(args, **kwargs):
            if args[0] == "restore":
                if restore_side_effect:
                    raise restore_side_effect
                for service in ("web", "db"):
                    data = root / "restores" / "app-test" / "srv/data" / service
                    data.mkdir(parents=True)
                    (data / "payload").write_text(f"{service}-backup-data")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        runner._run.side_effect = restore
        orchestrator = TestRestore(runner, root)
        model = isolate_compose(compose_model(), manifest(), root / "restores/app-test", "app-test")
        with (patch.object(orchestrator, "_check_project_unused"),
              patch("backupdock.restore.read_manifest", return_value=manifest()),
              patch.object(orchestrator, "_prepare", return_value=model),
              patch.object(orchestrator, "_compose", side_effect=compose_side_effect) as compose,
              contextlib.redirect_stdout(io.StringIO())):
            orchestrator.run(snapshot(), "app", "app-test", dry_run=dry_run, no_start=no_start)
        return runner, compose

    def test_dry_run_has_no_persistent_restore_or_docker_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, compose = self.run_restore(root, dry_run=True)
            self.assertEqual(list(root.iterdir()), [])
            runner._run.assert_not_called()
            compose.assert_not_called()

    def test_complete_restore_generates_only_test_mounts_and_starts_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, compose = self.run_restore(root)
            target = root / "restores/app-test"
            model = json.loads((target / "compose.test.json").read_text())
            self.assertEqual(set(model["services"]), {"web", "db"})
            self.assertEqual((target / "srv/data/db/payload").read_text(), "db-backup-data")
            self.assertEqual((target / "compose.test.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(compose.call_args_list[0].args[0][-3:], ["up", "-d", "--no-build"])
            self.assertEqual(compose.call_args_list[1].args[0][-1], "ps")

    def test_prepare_only_does_not_start(self):
        with tempfile.TemporaryDirectory() as directory:
            _, compose = self.run_restore(Path(directory), no_start=True)
            compose.assert_not_called()

    def test_failed_restore_does_not_start_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RestoreError, "restore failed"):
                self.run_restore(Path(directory), restore_side_effect=RestoreError("restore failed"))
            self.assertFalse((Path(directory) / "restores/app-test/compose.test.json").exists())

    def test_interrupting_start_removes_only_test_resources_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = []
            def interrupt(args):
                commands.append(args)
                if args[-3:] == ["up", "-d", "--no-build"]:
                    raise BackupInterrupted(signal.SIGINT)
                self.assertEqual(args[-2:], ["down", "--remove-orphans"])
                self.assertIn("app-test", args)
                self.assertNotIn("--volumes", args)
                self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            with self.assertRaises(BackupInterrupted):
                self.run_restore(root, compose_side_effect=interrupt)
            self.assertEqual(len(commands), 2)
            self.assertTrue((root / "restores/app-test/srv/data/db/payload").is_file())

    def test_existing_directory_or_original_name_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "restores/app-test").mkdir(parents=True)
            with self.assertRaisesRegex(RestoreError, "already exists"):
                TestRestore(MagicMock(), root).run(snapshot(), "app", "app-test")
            with self.assertRaisesRegex(RestoreError, "different project"):
                TestRestore(MagicMock(), root).run(snapshot(), "app", "app")

    def test_archived_env_files_are_read_from_private_copy_without_host_environment(self):
        runner = MagicMock()
        documents = {"/srv/config/app/compose.yaml": "services:\n  web:\n    image: example/app:1.0\n  db:\n    image: example/db:1.0\n",
                     "/srv/config/app/.env": "TOKEN=archived-value\n", "/srv/config/app/web.env": "PASSWORD=archived-password\n"}
        runner._run.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=documents[args[2]])
        value = manifest()
        value["sources"].append({"path": "/srv/config/app/web.env"})
        observed = []
        def normalize(args, **kwargs):
            observed.append((args, kwargs))
            if "--no-interpolate" in args:
                model = compose_model()
                model["services"]["web"]["env_file"] = [{"path": "./web.env", "required": True}]
                self.assertEqual(Path(args[args.index("--env-file") + 1]).read_text(), "TOKEN=archived-value\n")
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(model))
            generated = Path(args[args.index("-f") + 1])
            model = json.loads(generated.read_text())
            if "paths" in model["services"]:
                model["services"]["paths"]["environment"]["ARCHIVE_PATH_0"] = "./web.env"
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(model))
            env_file = Path(model["services"]["web"]["env_file"][0]["path"])
            self.assertEqual(env_file.read_text(), "PASSWORD=archived-password\n")
            model["services"]["web"]["environment"]["PASSWORD"] = "archived-password"
            model["services"]["web"]["environment"]["VALUE"] = "literal$$secret"
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(model))
        with patch.dict(os.environ, {"TOKEN": "production-value", "COMPOSE_FILE": "/production.yaml"}), patch.object(TestRestore, "_compose", side_effect=normalize):
            model = TestRestore(runner, Path("/state"))._prepare(snapshot(), value, Path("/state/restores/test"), "test")
        self.assertEqual(model["services"]["web"]["environment"]["PASSWORD"], "archived-password")
        self.assertNotIn("env_file", model["services"]["web"])
        self.assertNotIn("TOKEN", observed[0][1]["env"])
        self.assertNotIn("COMPOSE_FILE", observed[0][1]["env"])
        self.assertEqual(model["services"]["web"]["environment"]["VALUE"], "literal$$secret")

    def test_compose_include_and_extends_never_reach_normalizer(self):
        for document in ["include: /production.yaml\nservices: {}\n", "services:\n  web:\n    extends:\n      file: /production.yaml\n"]:
            runner = MagicMock()
            runner._run.return_value.stdout = document
            with patch.object(TestRestore, "_compose") as compose, self.assertRaises(RestoreError):
                TestRestore(runner, Path("/state"))._prepare(snapshot(), manifest(), Path("/restore"), "test")
            compose.assert_not_called()

    def test_existing_docker_containers_networks_or_volumes_are_refused(self):
        for collection in ("containers", "networks", "volumes"):
            client = MagicMock()
            for key in ("containers", "networks", "volumes"):
                getattr(client, key).list.return_value = [object()] if key == collection else []
            fake_docker = SimpleNamespace(from_env=lambda: client, errors=SimpleNamespace(DockerException=OSError))
            with patch.dict("sys.modules", {"docker": fake_docker}), self.assertRaisesRegex(RestoreError, "already exists"):
                TestRestore._check_project_unused("app-test")
            client.close.assert_called_once()


@unittest.skipUnless(shutil.which("docker"), "Docker CLI/Compose is not installed in this environment")
class RealComposeRestoreTests(unittest.TestCase):
    def test_actual_compose_parser_resolves_archive_env_files_and_preserves_literals(self):
        documents = {
            "/srv/config/app/compose.yaml": """services:
  web:
    image: ${APP_IMAGE:?APP_IMAGE must be archived}
    container_name: production-web
    env_file: ./web.env
    environment:
      DATABASE_HOST: db
      INHERITED_TOKEN:
    depends_on: [db]
    ports: ['8080:8080']
    volumes: [web:/data]
  db:
    image: example/db:1.0
    volumes: [db:/data]
volumes:
  web:
    external: true
  db:
    driver_opts:
      type: none
      o: bind
      device: /srv/data/db
""",
            "/srv/config/app/.env": "APP_IMAGE=example/app:1.0\nINHERITED_TOKEN=archived-token\n",
            "/srv/config/app/web.env": "TOKEN='literal$secret'\n",
        }
        runner = MagicMock()
        runner._run.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=documents[args[2]])
        value = manifest()
        value["sources"].append({"path": "/srv/config/app/web.env"})
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"APP_IMAGE": "production-value", "COMPOSE_FILE": "/production.yaml"}):
            root = Path(directory)
            model = TestRestore(runner, root)._prepare(snapshot(), value, root / "restores/app-test", "app-test")
            generated = root / "compose.test.json"
            generated.write_text(json.dumps(model))
            resolved = json.loads(TestRestore._compose(["-p", "app-test", "-f", str(generated), "config", "--format", "json"], capture=True).stdout)
        self.assertEqual(resolved["services"]["web"]["image"], "example/app:1.0")
        self.assertEqual(model["services"]["web"]["environment"]["TOKEN"], "literal$$secret")
        self.assertEqual(resolved["services"]["web"]["environment"]["TOKEN"], model["services"]["web"]["environment"]["TOKEN"])
        self.assertEqual(resolved["services"]["web"]["environment"]["DATABASE_HOST"], "db")
        self.assertEqual(resolved["services"]["web"]["environment"]["INHERITED_TOKEN"], "archived-token")
        self.assertTrue(resolved["networks"]["default"]["internal"])
        self.assertFalse(resolved.get("volumes"))
        self.assertNotIn("container_name", resolved["services"]["web"])


class RestoreRemoteTests(unittest.TestCase):
    def test_remote_restore_reuses_reverse_tunnel_and_sends_secrets_only_on_stdin(self):
        remote = RemoteConfig(ssh_target="root@source.example", password_file=Path("/secret"), repository_path="source",
                              rest_server_username="source", rest_server_password_file=Path("/rest-secret"), source_command=("sudo", "backupdock"))
        controller = RemoteBackupController(AppConfig(), remote)
        with (patch.object(controller, "_check_remote_version"), patch.object(controller, "_payload", return_value="session-secret") as payload,
              patch("backupdock.remote.run_process", return_value=subprocess.CompletedProcess([], 0)) as process,
              contextlib.redirect_stdout(io.StringIO())):
            controller.restore(snapshot(), "app", "app-test", dry_run=True)
        command = process.call_args.args[0]
        self.assertIn("-T", command)
        self.assertIn("127.0.0.1:18080:127.0.0.1:8000", command)
        self.assertNotIn("session-secret", " ".join(command))
        self.assertEqual(process.call_args.kwargs["input"], "session-secret")
        self.assertIn("source-restore", shlex.split(command[-1]))
        self.assertIn("--dry-run", shlex.split(command[-1]))
        payload.assert_called_once_with(dry_run=False)

    def test_restore_cli_dispatches_without_local_docker(self):
        remote = RemoteConfig(ssh_target="source.example", password_file=Path("/secret"), repository_path="source",
                              rest_server_username="source", rest_server_password_file=Path("/rest-secret"))
        config = AppConfig(remotes={"source": remote})
        with (patch("backupdock.cli.load_config", return_value=config), patch("backupdock.cli.maintenance_config"),
              patch("backupdock.cli.choose_snapshot", return_value=snapshot()), patch("backupdock.cli.RemoteBackupController") as controller,
              patch("backupdock.cli.TestRestore") as restore):
            result = main(["restore", "--remote", "source", "--project", "app", "--as", "app-test", "--test", "--dry-run"])
        self.assertEqual(result, 0)
        restore.assert_not_called()
        controller.return_value.restore.assert_called_once_with(snapshot(), "app", "app-test", dry_run=True, no_start=False)

    def test_source_restore_cleans_session_credentials_even_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION, "repository_password": "repository-value",
                       "rest_server_username": "source", "rest_server_password": "rest-value",
                       "config_yaml": f"restic:\n  repository: rest:http://127.0.0.1:18080/source\nbackup:\n  state_dir: {root / 'state'}\n"}
            previous = dict(os.environ)
            def fail(*args, **kwargs):
                self.assertEqual(os.environ["RESTIC_PASSWORD"], "repository-value")
                self.assertEqual(os.environ["RESTIC_REST_PASSWORD"], "rest-value")
                raise RestoreError("failed test")
            with (patch("backupdock.cli.SOURCE_RUNTIME_DIR", root / "run"), patch("sys.stdin", io.StringIO(json.dumps(payload))),
                  patch("backupdock.cli.choose_snapshot", return_value=snapshot()), patch("backupdock.cli.TestRestore.run", side_effect=fail),
                  contextlib.redirect_stderr(io.StringIO())):
                result = main(["source-restore", "--project", "app", "--as", "app-test", "--test"])
            self.assertEqual(result, 1)
            self.assertEqual(dict(os.environ), previous)
            self.assertEqual(list((root / "run").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
