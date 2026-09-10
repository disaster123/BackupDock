from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backupdock import __version__
from backupdock.cli import main
from backupdock.config import AppConfig, RemoteConfig, ResticConfig
from backupdock.remote import REMOTE_PROTOCOL_VERSION, RemoteBackupController
from backupdock.restic import ResticRunner


class FakeProcess:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None) -> int:
        del timeout
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class ProgressTests(unittest.TestCase):
    def test_interactive_backup_streams_json_progress_and_prints_summary(self) -> None:
        output = "\n".join(
            [
                '{"message_type":"status","seconds_elapsed":2,"seconds_remaining":8,"percent_done":0.5,"total_files":20,"files_done":10,"total_bytes":2048,"bytes_done":1024}',
                '{"message_type":"summary","files_new":2,"files_changed":0,"files_unmodified":18,"data_added":800,"data_added_packed":600,"total_files_processed":20,"total_bytes_processed":2048,"total_duration":10.2,"snapshot_id":"abcdef1234567890"}',
                "",
            ]
        )
        process = FakeProcess(output)
        stdout = io.StringIO()
        runner = ResticRunner(ResticConfig(binary="restic"), progress=True)

        with (
            patch("backupdock.restic.subprocess.Popen", return_value=process) as popen,
            patch("backupdock.restic.subprocess.run") as run,
            contextlib.redirect_stdout(stdout),
        ):
            runner.backup([Path("/data/example")], "compose:app")

        run.assert_not_called()
        command = popen.call_args.args[0]
        self.assertEqual(command[0:2], ["restic", "backup"])
        self.assertIn("--json", command)
        self.assertIn("backupdock-group=compose:app", command)
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)

        rendered = stdout.getvalue()
        self.assertIn("[compose:app] backup", rendered)
        self.assertIn("50.0%", rendered)
        self.assertIn("1.0 KiB / 2.0 KiB", rendered)
        self.assertIn("10 / 20 files", rendered)
        self.assertIn("ETA 00:08", rendered)
        self.assertIn("20 files, 2.0 KiB processed, 600 B stored", rendered)
        self.assertIn("snapshot abcdef12 saved", rendered)

    def test_noninteractive_backup_keeps_normal_restic_output_path(self) -> None:
        runner = ResticRunner(ResticConfig(binary="restic"), progress=False)
        completed = subprocess.CompletedProcess(["restic"], 0)

        with (
            patch("backupdock.restic.subprocess.run", return_value=completed) as run,
            patch("backupdock.restic.subprocess.Popen") as popen,
        ):
            runner.backup([Path("/data/example")], "compose:app")

        popen.assert_not_called()
        command = run.call_args.args[0]
        self.assertNotIn("--json", command)

    def test_remote_progress_keeps_ssh_without_pty(self) -> None:
        remote = RemoteConfig(
            ssh_target="root@docker-prod.example",
            password_file=Path("/repository-password"),
            repository_path="docker-prod",
            rest_server_username="docker-prod",
            rest_server_password_file=Path("/rest-server-password"),
        )

        command = RemoteBackupController(AppConfig(), remote).command(
            ["nextcloud"],
            dry_run=False,
            progress=True,
        )

        self.assertIn("-T", command)
        self.assertNotIn("-t", command)
        self.assertIn("--progress", command)
        self.assertEqual(command[-3:], ["--project", "nextcloud", "--progress"])

    def test_remote_run_enables_source_progress_when_controller_stdout_is_tty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository_password = root / "repository-password"
            rest_password = root / "rest-password"
            repository_password.write_text("repository-value\n", encoding="utf-8")
            rest_password.write_text("rest-value\n", encoding="utf-8")
            remote = RemoteConfig(
                ssh_target="root@docker-prod.example",
                password_file=repository_password,
                repository_path="docker-prod",
                rest_server_username="docker-prod",
                rest_server_password_file=rest_password,
            )
            controller = RemoteBackupController(AppConfig(), remote)
            tty_stdout = MagicMock()
            tty_stdout.isatty.return_value = True

            with patch("backupdock.remote.subprocess.run") as run, patch(
                "backupdock.remote.sys.stdout", tty_stdout
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
                controller.run([], dry_run=False)

            self.assertIn("--progress", run.call_args_list[1].args[0])

    def test_internal_source_progress_flag_is_forwarded(self) -> None:
        with patch("backupdock.cli._run_source_backup") as run_source:
            result = main(["source-backup", "--progress"])

        self.assertEqual(result, 0)
        run_source.assert_called_once_with(
            [],
            dry_run=False,
            preseed=False,
            progress=True,
        )


if __name__ == "__main__":
    unittest.main()
