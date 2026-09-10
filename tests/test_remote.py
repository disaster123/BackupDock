from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock import __version__
from backupdock.config import AppConfig, BackupConfig, ProjectConfig, RemoteConfig
from backupdock.remote import (
    REMOTE_PROTOCOL_VERSION,
    RemoteBackupController,
    RemoteBackupError,
    repository_url,
)


class RemoteBackupTests(unittest.TestCase):
    def _remote(self, password_file: Path) -> RemoteConfig:
        return RemoteConfig(
            ssh_target="root@docker-host",
            password_file=password_file,
            repository_path="docker-host",
        )

    def _controller(self, password_file: Path) -> RemoteBackupController:
        return RemoteBackupController(AppConfig(), self._remote(password_file))

    def test_repository_url_uses_source_loopback_tunnel_port(self) -> None:
        config = RemoteConfig(
            ssh_target="root@docker-host",
            password_file=Path("/secret"),
            repository_path="repos/docker host",
            remote_tunnel_port=19090,
        )

        self.assertEqual(
            repository_url(config),
            "rest:http://127.0.0.1:19090/repos/docker%20host",
        )

    def test_command_contains_reverse_forward_and_source_backup(self) -> None:
        remote = RemoteConfig(
            ssh_target="backup-source",
            password_file=Path("/secret"),
            repository_path="docker-host",
            source_command=("sudo", "backupdock"),
            ssh_options=("-i", "/root/.ssh/backupdock"),
            local_rest_server_host="127.0.0.1",
            local_rest_server_port=8100,
            remote_tunnel_port=18100,
        )

        command = RemoteBackupController(AppConfig(), remote).command(["nextcloud"], dry_run=True)

        self.assertIn("127.0.0.1:18100:127.0.0.1:8100", command)
        self.assertIn("backup-source", command)
        self.assertIn("source-backup", command)
        self.assertNotIn("--repository", command)
        self.assertEqual(command[-3:], ["--project", "nextcloud", "--dry-run"])

    def test_version_is_checked_before_remote_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("test-value\n", encoding="utf-8")
            controller = self._controller(password_file)
            calls: list[list[str]] = []

            def run(command, **kwargs):
                calls.append(command)
                if "source-info" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps(
                            {"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION}
                        ),
                        stderr="",
                    )
                return subprocess.CompletedProcess(command, 0)

            with patch("backupdock.remote.subprocess.run", side_effect=run):
                controller.run([], dry_run=False)

            self.assertEqual(len(calls), 2)
            self.assertIn("source-info", calls[0])
            self.assertIn("source-backup", calls[1])

    def test_version_mismatch_aborts_before_backup_and_password_read(self) -> None:
        missing_password = Path("/definitely/not/present")
        controller = self._controller(missing_password)

        with patch("backupdock.remote.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"version": "0.0.0", "protocol": REMOTE_PROTOCOL_VERSION}
                ),
                stderr="",
            )
            with self.assertRaisesRegex(RemoteBackupError, "version mismatch"):
                controller.run([], dry_run=False)

        run.assert_called_once()

    def test_payload_contains_generated_source_config_not_controller_remotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("test-value\n", encoding="utf-8")
            remote = self._remote(password_file)
            app_config = AppConfig(
                backup=BackupConfig(exclude_volumes=("global-cache",)),
                projects={
                    "pbs": ProjectConfig(exclude_volumes=("pbs-backups",)),
                },
                remotes={"docker-host": remote},
            )
            controller = RemoteBackupController(app_config, remote)
            payload = json.loads(controller._payload(dry_run=False))

            self.assertEqual(payload["version"], __version__)
            self.assertEqual(payload["protocol"], REMOTE_PROTOCOL_VERSION)
            self.assertEqual(payload["password"], "test-value")
            self.assertIn("pbs-backups", payload["config_yaml"])
            self.assertIn("global-cache", payload["config_yaml"])
            self.assertNotIn("remotes:", payload["config_yaml"])
            self.assertNotIn(str(password_file), payload["config_yaml"])

    def test_password_is_sent_in_payload_and_not_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("test-value\n", encoding="utf-8")
            controller = self._controller(password_file)

            with patch("backupdock.remote.subprocess.run") as run:
                run.side_effect = [
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION}
                        ),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 0),
                ]
                controller.run([], dry_run=False)

            backup_call = run.call_args_list[1]
            args = backup_call.args[0]
            payload = json.loads(backup_call.kwargs["input"])
            self.assertNotIn("test-value", " ".join(args))
            self.assertEqual(payload["password"], "test-value")
            self.assertFalse(backup_call.kwargs["check"])

    def test_dry_run_does_not_send_real_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("test-value\n", encoding="utf-8")
            controller = self._controller(password_file)
            stdout = io.StringIO()

            with (
                patch("backupdock.remote.subprocess.run") as run,
                contextlib.redirect_stdout(stdout),
            ):
                run.side_effect = [
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION}
                        ),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 0),
                ]
                controller.run([], dry_run=True)

            payload = json.loads(run.call_args_list[1].kwargs["input"])
            self.assertEqual(payload["password"], "dry-run")
            self.assertNotIn("test-value", stdout.getvalue())
            self.assertIn("REMOTE DRY-RUN ssh command:", stdout.getvalue())

    def test_nonzero_backup_ssh_exit_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("test-value\n", encoding="utf-8")
            controller = self._controller(password_file)

            with patch("backupdock.remote.subprocess.run") as run:
                run.side_effect = [
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {"version": __version__, "protocol": REMOTE_PROTOCOL_VERSION}
                        ),
                        stderr="",
                    ),
                    subprocess.CompletedProcess([], 23),
                ]
                with self.assertRaisesRegex(RemoteBackupError, "exit code 23"):
                    controller.run([], dry_run=False)


if __name__ == "__main__":
    unittest.main()
