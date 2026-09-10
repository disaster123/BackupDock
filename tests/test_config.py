from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.config import load_config


class ConfigTests(unittest.TestCase):
    def test_volume_exclusions_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
backup:
  exclude_volumes:
    - global_cache

projects:
  pbs:
    exclude_volumes:
      - pbs_backups
""".lstrip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.backup.exclude_volumes, ("global_cache",))
            self.assertEqual(config.projects["pbs"].exclude_volumes, ("pbs_backups",))

    def test_remote_configuration_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    password_file: /root/.config/restic/docker-prod.password
    repository_path: docker-prod
    source_command:
      - sudo
      - backupdock
    ssh_options:
      - -i
      - /root/.ssh/backupdock
    local_rest_server_host: 127.0.0.1
    local_rest_server_port: 8100
    remote_tunnel_port: 18100
""".lstrip(),
                encoding="utf-8",
            )

            config = load_config(config_path)
            remote = config.remotes["docker-prod"]

            self.assertEqual(remote.ssh_target, "root@docker-prod.example")
            self.assertEqual(remote.password_file, Path("/root/.config/restic/docker-prod.password"))
            self.assertEqual(remote.repository_path, "docker-prod")
            self.assertEqual(remote.source_command, ("sudo", "backupdock"))
            self.assertEqual(remote.ssh_options, ("-i", "/root/.ssh/backupdock"))
            self.assertEqual(remote.local_rest_server_port, 8100)
            self.assertEqual(remote.remote_tunnel_port, 18100)

    def test_remote_requires_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    repository_path: docker-prod
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "password_file"):
                load_config(config_path)

    def test_empty_yaml_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("", encoding="utf-8")

            config = load_config(config_path)

            self.assertEqual(config.backup.stop_timeout_seconds, 30)
            self.assertEqual(config.backup.exclude_volumes, ())
            self.assertEqual(config.remotes, {})

    def test_invalid_boolean_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "backup:\n  include_compose_metadata: yes-please\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(config_path)

    def test_missing_default_config_warns_to_stderr_and_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_config = Path(directory) / "config.yaml"
            stderr = io.StringIO()

            with (
                patch("backupdock.config.DEFAULT_CONFIG_PATH", missing_config),
                contextlib.redirect_stderr(stderr),
            ):
                config = load_config()

            self.assertEqual(config.backup.stop_timeout_seconds, 30)
            self.assertIn("warning", stderr.getvalue().lower())
            self.assertIn(str(missing_config), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
