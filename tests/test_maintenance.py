from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from backupdock.cli import main
from backupdock.config import AppConfig, RemoteConfig, ResticConfig, RetentionConfig, load_config, render_source_config
from backupdock.maintenance import _backup_directory, maintenance_config


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backups = self.root / "Backups with spaces"
        self.repository = self.backups / "flexserver"
        self.repository.mkdir(parents=True)
        (self.repository / "config").write_text("{}")
        self.service_config = self.root / "rest-server-defaults"
        self.service_config.write_text(f'BACKUP_DIR = "{self.backups}"\nARGS = "--append-only"\n')
        self.remote = RemoteConfig(
            ssh_target="source", password_file=self.root / "repository-password",
            repository_path="flexserver", rest_server_username="source",
            rest_server_password_file=self.root / "rest-password",
        )
        self.config = AppConfig(
            restic=ResticConfig(binary="restic-custom", rest_server_config_file=self.service_config),
            remotes={"flexserver": self.remote}, retention=RetentionConfig(keep_daily=14, prune=True),
        )

    def test_single_remote_uses_service_root_and_remote_password(self):
        result = maintenance_config(self.config)
        self.assertEqual(result.repository, str(self.repository))
        self.assertEqual(result.password_file, self.remote.password_file)
        self.assertEqual(result.binary, "restic-custom")

    def test_changed_service_root_is_used_on_next_resolution(self):
        updated = self.root / "new-location"
        (updated / "flexserver").mkdir(parents=True)
        (updated / "flexserver" / "config").write_text("{}")
        self.service_config.write_text(f"BACKUP_DIR={updated}\n")
        self.assertEqual(maintenance_config(self.config).repository, str(updated / "flexserver"))

    def test_multiple_remotes_require_selection(self):
        config = replace(self.config, remotes={"flexserver": self.remote, "other": replace(self.remote, repository_path="other")})
        with self.assertRaisesRegex(ValueError, "--remote"):
            maintenance_config(config)
        self.assertEqual(maintenance_config(config, "flexserver").repository, str(self.repository))

    def test_explicit_local_repository_is_preserved_without_reading_service_config(self):
        restic = replace(self.config.restic, repository="sftp:backup:/repo", password_file=Path("/local-password"))
        config = replace(self.config, restic=restic)
        self.service_config.unlink()
        self.assertIs(maintenance_config(config), restic)

    def test_explicit_remote_overrides_top_level_repository_and_password(self):
        config = replace(self.config, restic=replace(self.config.restic, repository="/other", password_file=Path("/other-password")))
        result = maintenance_config(config, "flexserver")
        self.assertEqual(result.repository, str(self.repository))
        self.assertEqual(result.password_file, self.remote.password_file)

    def test_local_mode_without_remotes_does_not_read_service_config(self):
        self.service_config.unlink()
        config = replace(self.config, remotes={})
        self.assertIs(maintenance_config(config), config.restic)

    def test_unknown_remote_is_rejected_before_reading_service_config(self):
        self.service_config.unlink()
        with self.assertRaisesRegex(ValueError, "Unknown remote"):
            maintenance_config(self.config, "unknown")

    def test_common_assignment_syntax_and_inline_comments(self):
        for assignment in (f'BACKUP_DIR = "{self.backups}" # comment', f"export BACKUP_DIR='{self.backups}'", f'  BACKUP_DIR="{self.backups}"'):
            with self.subTest(assignment=assignment):
                self.service_config.write_text("# BACKUP_DIR=/unused\n" + assignment + "\n")
                self.assertEqual(_backup_directory(self.service_config), self.backups)

    def test_missing_invalid_or_ambiguous_service_settings_are_rejected(self):
        for text in ("", "BACKUP_DIR=relative", "BACKUP_DIR=", 'BACKUP_DIR="unterminated', "BACKUP_DIR=/one\nBACKUP_DIR=/two", "BACKUP_DIR=$HOME/backups", "BACKUP_DIR=$(touch /tmp/unused)", "BACKUP_DIR=/backups\nARGS=\"--path /other\""):
            with self.subTest(text=text):
                self.service_config.write_text(text)
                with self.assertRaises(ValueError):
                    maintenance_config(self.config)
        self.service_config.unlink()
        with self.assertRaisesRegex(ValueError, "Cannot read"):
            maintenance_config(self.config)

    def test_config_is_read_as_data_and_never_executed(self):
        marker = self.root / "must-not-exist"
        self.service_config.write_text(f'BACKUP_DIR="{self.backups}"\ntouch "{marker}"\n')
        maintenance_config(self.config)
        self.assertFalse(marker.exists())

    def test_traversal_missing_repository_and_symlink_escape_are_rejected(self):
        for path in ("../other", "/../other", "/", ".", "missing"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                maintenance_config(replace(self.config, remotes={"flexserver": replace(self.remote, repository_path=path)}))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "config").write_text("{}")
        (self.backups / "escaped").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            maintenance_config(replace(self.config, remotes={"flexserver": replace(self.remote, repository_path="escaped")}))

    def test_leading_slash_matches_existing_remote_url_behavior(self):
        config = replace(self.config, remotes={"flexserver": replace(self.remote, repository_path="/flexserver")})
        self.assertEqual(maintenance_config(config).repository, str(self.repository))

    def test_nonlocal_endpoint_cannot_use_local_discovery(self):
        config = replace(self.config, remotes={"flexserver": replace(self.remote, local_rest_server_host="elsewhere.example")})
        with self.assertRaisesRegex(ValueError, "loopback"):
            maintenance_config(config)

    def test_config_path_is_loaded_and_not_forwarded_to_source(self):
        config_path = self.root / "config.yaml"
        config_path.write_text(f"restic:\n  rest_server_config_file: {self.service_config}\n")
        result = load_config(config_path, use_environment=False)
        self.assertEqual(result.restic.rest_server_config_file, self.service_config)
        source_yaml = render_source_config(self.config, self.remote, repository="rest:http://127.0.0.1:18080/flexserver")
        self.assertNotIn("rest_server_config_file", source_yaml)
        self.assertNotIn(str(self.backups), source_yaml)

    def test_maintenance_cli_selects_remote_locally(self):
        for command in ("snapshots", "check", "forget"):
            with (
                self.subTest(command=command),
                patch("backupdock.cli.load_config", return_value=self.config),
                patch("backupdock.cli.ResticRunner") as runner,
                patch("backupdock.cli.DockerBackend") as docker,
                patch("backupdock.cli.RemoteBackupController") as ssh,
            ):
                self.assertEqual(main([command, "--remote", "flexserver"]), 0)
                selected = runner.call_args.args[0]
                self.assertEqual(selected.repository, str(self.repository))
                self.assertEqual(selected.password_file, self.remote.password_file)
                getattr(runner.return_value, command).assert_called_once()
                docker.assert_not_called()
                ssh.assert_not_called()

    def test_forget_stream_passes_discovered_path_and_password_to_restic(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs["env"]))
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        with (
            patch("backupdock.cli.load_config", return_value=self.config),
            patch("backupdock.restic.run_process", side_effect=run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["forget"]), 0)
        self.assertEqual([command[1] for command, env in calls], ["forget", "snapshots", "prune"])
        for command, env in calls:
            self.assertEqual(env["RESTIC_REPOSITORY"], str(self.repository))
            self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(self.remote.password_file))

    def test_discovery_failure_stops_before_restic(self):
        self.service_config.unlink()
        with (
            patch("backupdock.cli.load_config", return_value=self.config),
            patch("backupdock.cli.ResticRunner") as runner,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["forget"]), 1)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
