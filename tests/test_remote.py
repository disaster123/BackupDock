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
    controller_repository_url,
    repository_url,
)


class RemoteBackupTests(unittest.TestCase):
    def _remote(self, password_file: Path, rest_server_password_file: Path | None = None) -> RemoteConfig:
        return RemoteConfig(
            ssh_target="root@docker-host",
            password_file=password_file,
            repository_path="docker-host",
            rest_server_username="docker-host",
            rest_server_password_file=rest_server_password_file or password_file,
        )

    def _controller(
        self,
        password_file: Path,
        rest_server_password_file: Path | None = None,
    ) -> RemoteBackupController:
        return RemoteBackupController(
            AppConfig(),
            self._remote(password_file, rest_server_password_file),
        )

    def test_repository_url_uses_source_loopback_tunnel_port(self) -> None:
        config = RemoteConfig(
            ssh_target="root@docker-host",
            password_file=Path("/secret"),
            repository_path="repos/docker host",
            rest_server_username="docker-host",
            rest_server_password_file=Path("/rest-secret"),
            remote_tunnel_port=19090,
        )

        self.assertEqual(
            repository_url(config),
            "rest:http://127.0.0.1:19090/repos/docker%20host",
        )

    def test_controller_repository_url_uses_local_rest_server(self) -> None:
        config = RemoteConfig(
            ssh_target="root@docker-host",
            password_file=Path("/secret"),
            repository_path="repos/docker host",
            rest_server_username="docker-host",
            rest_server_password_file=Path("/rest-secret"),
            local_rest_server_host="127.0.0.1",
            local_rest_server_port=8100,
            remote_tunnel_port=19090,
        )

        self.assertEqual(
            controller_repository_url(config),
            "rest:http://127.0.0.1:8100/repos/docker%20host",
        )

    def test_command_contains_reverse_forward_and_source_backup(self) -> None:
        remote = RemoteConfig(
            ssh_target="backup-source",
            password_file=Path("/secret"),
            repository_path="docker-host",
            rest_server_username="docker-host",
            rest_server_password_file=Path("/rest-secret"),
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
        self.assertNotIn("docker-host", command[-1])
        self.assertEqual(command[-3:], ["--project", "nextcloud", "--dry-run"])

    def test_command_passes_preseed_to_source(self) -> None:
        remote = self._remote(Path("/secret"), Path("/rest-secret"))
        command = RemoteBackupController(AppConfig(), remote).command(
            ["nextcloud"],
            dry_run=False,
            preseed=True,
        )

        self.assertEqual(command[-3:], ["--project", "nextcloud", "--preseed"])

    def test_remote_init_uses_controller_rest_server_and_validates_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password_file = root / "repository-password"
            rest_password_file = root / "rest-password"
            password_file.write_text("repository-value\n", encoding="utf-8")
            rest_password_file.write_text("rest-value\n", encoding="utf-8")
            remote = self._remote(password_file, rest_password_file)
            controller = RemoteBackupController(AppConfig(), remote)

            with (
                patch("backupdock.remote.ResticRunner") as runner_class,
                patch("backupdock.remote.subprocess.run") as subprocess_run,
            ):
                controller.init_repository()

            subprocess_run.assert_not_called()
            runner_class.assert_called_once()
            restic_config = runner_class.call_args.args[0]
            self.assertEqual(
                restic_config.repository,
                "rest:http://127.0.0.1:8000/docker-host",
            )
            self.assertEqual(restic_config.password_file, password_file)
            self.assertEqual(
                runner_class.call_args.kwargs["env_overrides"],
                {
                    "RESTIC_REST_USERNAME": "docker-host",
                    "RESTIC_REST_PASSWORD": "rest-value",
                },
            )
            runner_class.return_value.init.assert_called_once_with()

    def test_remote_init_rejects_missing_secret_before_restic(self) -> None:
        controller = self._controller(
            Path("/definitely/not/present"),
            Path("/also/not/present"),
        )

        with patch("backupdock.remote.ResticRunner") as runner_class:
            with self.assertRaisesRegex(RemoteBackupError, "repository password file not found"):
                controller.init_repository()

        runner_class.assert_not_called()

    def test_version_is_checked_before_remote_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password_file = root / "password"
            rest_password_file = root / "rest-password"
            password_file.write_text("repository-value\n", encoding="utf-8")
            rest_password_file.write_text("rest-value\n", encoding="utf-8")
            controller = self._controller(password_file, rest_password_file)
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

    def test_version_mismatch_aborts_before_backup_and_secret_reads(self) -> None:
        controller = self._controller(
            Path("/definitely/not/present"),
            Path("/also/not/present"),
        )

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

    def test_payload_uses_only_selected_remote_source_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password_file = root / "password"
            rest_password_file = root / "rest-password"
            password_file.write_text("repository-value\n", encoding="utf-8")
            rest_password_file.write_text("rest-value\n", encoding="utf-8")
            remote = RemoteConfig(
                ssh_target="root@docker-host",
                password_file=password_file,
                repository_path="docker-host",
                rest_server_username="docker-host",
                rest_server_password_file=rest_password_file,
                host_paths=(Path("/remote/static"),),
                exclude_volumes=("remote-cache",),
                ignore_containers=("remote-temporary",),
                projects={
                    "pbs": ProjectConfig(exclude_volumes=("remote-pbs-backups",)),
                },
            )
            app_config = AppConfig(
                backup=BackupConfig(
                    host_paths=(Path("/local/static"),),
                    exclude_volumes=("local-cache",),
                    ignore_containers=("local-temporary",),
                ),
                projects={
                    "pbs": ProjectConfig(exclude_volumes=("local-pbs-backups",)),
                },
                remotes={"docker-host": remote},
            )
            controller = RemoteBackupController(app_config, remote)
            payload = json.loads(controller._payload(dry_run=False))
            source_yaml = payload["config_yaml"]

            self.assertEqual(payload["version"], __version__)
            self.assertEqual(payload["protocol"], REMOTE_PROTOCOL_VERSION)
            self.assertEqual(payload["repository_password"], "repository-value")
            self.assertEqual(payload["rest_server_username"], "docker-host")
            self.assertEqual(payload["rest_server_password"], "rest-value")
            self.assertIn("/remote/static", source_yaml)
            self.assertIn("remote-cache", source_yaml)
            self.assertIn("remote-temporary", source_yaml)
            self.assertIn("remote-pbs-backups", source_yaml)
            self.assertNotIn("/local/static", source_yaml)
            self.assertNotIn("local-cache", source_yaml)
            self.assertNotIn("local-temporary", source_yaml)
            self.assertNotIn("local-pbs-backups", source_yaml)
            self.assertNotIn("remotes:", source_yaml)
            self.assertNotIn(str(password_file), source_yaml)
            self.assertNotIn(str(rest_password_file), source_yaml)
            self.assertNotIn("rest-value", source_yaml)

    def test_secrets_are_sent_in_payload_and_not_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password_file = root / "password"
            rest_password_file = root / "rest-password"
            password_file.write_text("repository-value\n", encoding="utf-8")
            rest_password_file.write_text("rest-value\n", encoding="utf-8")
            controller = self._controller(password_file, rest_password_file)

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
            joined_command = " ".join(args)
            self.assertNotIn("repository-value", joined_command)
            self.assertNotIn("rest-value", joined_command)
            self.assertEqual(payload["repository_password"], "repository-value")
            self.assertEqual(payload["rest_server_password"], "rest-value")
            self.assertFalse(backup_call.kwargs["check"])

    def test_dry_run_does_not_read_or_send_real_secrets(self) -> None:
        controller = self._controller(
            Path("/definitely/not/present"),
            Path("/also/not/present"),
        )
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
            controller.run([], dry_run=True, preseed=True)

        backup_call = run.call_args_list[1]
        payload = json.loads(backup_call.kwargs["input"])
        self.assertEqual(payload["repository_password"], "dry-run")
        self.assertEqual(payload["rest_server_password"], "dry-run")
        self.assertEqual(payload["rest_server_username"], "docker-host")
        self.assertIn("--preseed", backup_call.args[0])
        self.assertIn("REMOTE DRY-RUN ssh command:", stdout.getvalue())

    def test_nonzero_backup_ssh_exit_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            password_file = root / "password"
            rest_password_file = root / "rest-password"
            password_file.write_text("repository-value\n", encoding="utf-8")
            rest_password_file.write_text("rest-value\n", encoding="utf-8")
            controller = self._controller(password_file, rest_password_file)

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
