from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock import __version__
from backupdock.cli import _run_source_backup, main
from backupdock.config import AppConfig, RemoteConfig
from backupdock.remote import REMOTE_PROTOCOL_VERSION


class SourceRemoteTests(unittest.TestCase):
    def test_remote_backup_does_not_construct_local_docker_backend(self) -> None:
        remote = RemoteConfig(
            ssh_target="root@source",
            password_file=Path("/tmp/test-password"),
            repository_path="source",
        )
        config = AppConfig(remotes={"source": remote})

        with (
            patch("backupdock.cli.load_config", return_value=config),
            patch("backupdock.cli.RemoteBackupController") as controller_class,
            patch("backupdock.cli.DockerBackend") as docker_class,
        ):
            result = main(["remote-backup", "source", "--project", "nextcloud", "--dry-run"])

        self.assertEqual(result, 0)
        controller_class.assert_called_once_with(config, remote)
        controller_class.return_value.run.assert_called_once_with(["nextcloud"], dry_run=True)
        docker_class.assert_not_called()

    def test_source_info_does_not_load_default_config(self) -> None:
        stdout = io.StringIO()

        with (
            patch("backupdock.cli.load_config") as load_config,
            contextlib.redirect_stdout(stdout),
        ):
            result = main(["source-info"])

        self.assertEqual(result, 0)
        load_config.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["protocol"], REMOTE_PROTOCOL_VERSION)

    def test_source_backup_uses_temporary_controller_config_and_warns_about_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_dir = root / "run"
            local_config = root / "config.yaml"
            local_config.write_text("backup:\n  stop_timeout_seconds: 999\n", encoding="utf-8")
            source_yaml = """
restic:
  repository: "rest:http://127.0.0.1:18080/source"
backup:
  state_dir: "/var/lib/backupdock"
  stop_timeout_seconds: 30
projects:
  pbs:
    exclude_volumes:
      - pbs-backups
retention:
  after_backup: false
  prune: false
""".lstrip()
            payload = json.dumps(
                {
                    "version": __version__,
                    "protocol": REMOTE_PROTOCOL_VERSION,
                    "password": "session-value",
                    "config_yaml": source_yaml,
                }
            )
            observed: dict[str, object] = {}
            stderr = io.StringIO()

            def capture(source_config, projects, *, dry_run):
                observed["config"] = source_config
                observed["projects"] = projects
                observed["dry_run"] = dry_run
                observed["password"] = os.environ.get("RESTIC_PASSWORD")
                observed["password_file"] = os.environ.get("RESTIC_PASSWORD_FILE")
                observed["password_command"] = os.environ.get("RESTIC_PASSWORD_COMMAND")

            previous = {
                key: os.environ.get(key)
                for key in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND")
            }
            os.environ["RESTIC_PASSWORD"] = "previous-value"
            os.environ["RESTIC_PASSWORD_FILE"] = "/tmp/previous-file"
            os.environ["RESTIC_PASSWORD_COMMAND"] = "previous-command"
            try:
                with (
                    patch("backupdock.cli.SOURCE_RUNTIME_DIR", runtime_dir),
                    patch("backupdock.cli.DEFAULT_CONFIG_PATH", local_config),
                    patch("backupdock.cli._run_backup", side_effect=capture),
                    patch("sys.stdin", io.StringIO(payload)),
                    contextlib.redirect_stderr(stderr),
                ):
                    _run_source_backup(["nextcloud"], dry_run=False)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            source_config = observed["config"]
            self.assertEqual(source_config.backup.stop_timeout_seconds, 30)
            self.assertEqual(source_config.restic.repository, "rest:http://127.0.0.1:18080/source")
            self.assertEqual(source_config.projects["pbs"].exclude_volumes, ("pbs-backups",))
            self.assertFalse(source_config.retention.after_backup)
            self.assertFalse(source_config.retention.prune)
            self.assertEqual(observed["password"], "session-value")
            self.assertIsNone(observed["password_file"])
            self.assertIsNone(observed["password_command"])
            self.assertEqual(observed["projects"], ["nextcloud"])
            self.assertFalse(observed["dry_run"])
            self.assertIn("ignored", stderr.getvalue())
            self.assertIn(str(local_config), stderr.getvalue())
            self.assertEqual(list(runtime_dir.glob("session-*")), [])

            for key, value in previous.items():
                self.assertEqual(os.environ.get(key), value)

    def test_source_backup_rejects_controller_version_mismatch(self) -> None:
        payload = json.dumps(
            {
                "version": "0.0.0",
                "protocol": REMOTE_PROTOCOL_VERSION,
                "password": "session-value",
                "config_yaml": "backup: {}\n",
            }
        )

        with (
            patch("sys.stdin", io.StringIO(payload)),
            patch("backupdock.cli._run_backup") as run_backup,
        ):
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                _run_source_backup([], dry_run=False)

        run_backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
