from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.cli import _run_source_backup, main
from backupdock.config import AppConfig, RemoteConfig, ResticConfig, RetentionConfig


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
        controller_class.assert_called_once_with(remote)
        controller_class.return_value.run.assert_called_once_with(["nextcloud"], dry_run=True)
        docker_class.assert_not_called()

    def test_source_backup_overrides_repository_and_disables_retention(self) -> None:
        config = AppConfig(
            restic=ResticConfig(
                repository="local:repo",
                password_file=Path("/tmp/local-password"),
            ),
            retention=RetentionConfig(after_backup=True, prune=True, keep_daily=7),
        )
        observed: dict[str, object] = {}

        def capture(source_config: AppConfig, projects: list[str], *, dry_run: bool) -> None:
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
                patch("backupdock.cli._run_backup", side_effect=capture),
                patch("sys.stdin", io.StringIO("session-value\n")),
            ):
                _run_source_backup(
                    config,
                    "rest:http://127.0.0.1:18080/source",
                    ["nextcloud"],
                    dry_run=False,
                )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        source_config = observed["config"]
        self.assertIsInstance(source_config, AppConfig)
        self.assertEqual(source_config.restic.repository, "rest:http://127.0.0.1:18080/source")
        self.assertIsNone(source_config.restic.password_file)
        self.assertFalse(source_config.retention.after_backup)
        self.assertFalse(source_config.retention.prune)
        self.assertEqual(source_config.retention.keep_daily, 7)
        self.assertEqual(observed["password"], "session-value")
        self.assertIsNone(observed["password_file"])
        self.assertIsNone(observed["password_command"])
        self.assertEqual(observed["projects"], ["nextcloud"])
        self.assertFalse(observed["dry_run"])

        for key, value in previous.items():
            self.assertEqual(os.environ.get(key), value)


if __name__ == "__main__":
    unittest.main()
