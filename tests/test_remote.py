from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.config import RemoteConfig
from backupdock.remote import RemoteBackupController, RemoteBackupError, repository_url


class RemoteBackupTests(unittest.TestCase):
    def _config(self, password_file: Path) -> RemoteConfig:
        return RemoteConfig(
            ssh_target="root@docker-host",
            password_file=password_file,
            repository_path="docker-host",
        )

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
        config = RemoteConfig(
            ssh_target="backup-source",
            password_file=Path("/secret"),
            repository_path="docker-host",
            source_command=("sudo", "backupdock"),
            ssh_options=("-i", "/root/.ssh/backupdock"),
            local_rest_server_host="127.0.0.1",
            local_rest_server_port=8100,
            remote_tunnel_port=18100,
        )

        command = RemoteBackupController(config).command(["nextcloud"], dry_run=True)

        self.assertIn("127.0.0.1:18100:127.0.0.1:8100", command)
        self.assertIn("backup-source", command)
        self.assertIn("source-backup", command)
        self.assertIn("rest:http://127.0.0.1:18100/docker-host", command)
        self.assertEqual(command[-3:], ["--project", "nextcloud", "--dry-run"])

    def test_password_is_sent_on_stdin_and_not_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("top-secret\n", encoding="utf-8")
            config = self._config(password_file)

            with patch("backupdock.remote.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0)
                RemoteBackupController(config).run([], dry_run=False)

            args = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertNotIn("top-secret", " ".join(args))
            self.assertEqual(kwargs["input"], "top-secret\n")
            self.assertFalse(kwargs["check"])

    def test_dry_run_does_not_send_real_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("top-secret\n", encoding="utf-8")
            config = self._config(password_file)
            stdout = io.StringIO()

            with (
                patch("backupdock.remote.subprocess.run") as run,
                contextlib.redirect_stdout(stdout),
            ):
                run.return_value = subprocess.CompletedProcess([], 0)
                RemoteBackupController(config).run([], dry_run=True)

            self.assertEqual(run.call_args.kwargs["input"], "dry-run\n")
            self.assertNotIn("top-secret", stdout.getvalue())
            self.assertIn("REMOTE DRY-RUN ssh command:", stdout.getvalue())

    def test_nonzero_ssh_exit_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("secret\n", encoding="utf-8")
            config = self._config(password_file)

            with patch("backupdock.remote.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 23)
                with self.assertRaisesRegex(RemoteBackupError, "exit code 23"):
                    RemoteBackupController(config).run([], dry_run=False)


if __name__ == "__main__":
    unittest.main()
