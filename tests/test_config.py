from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backupdock.config import load_config


class ConfigTests(unittest.TestCase):
    def test_local_volume_exclusions_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
backup:
  exclude_volumes:
    - local_cache

projects:
  pbs:
    exclude_volumes:
      - pbs_backups
""".lstrip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.backup.exclude_volumes, ("local_cache",))
            self.assertEqual(config.projects["pbs"].exclude_volumes, ("pbs_backups",))

    def test_remote_configuration_and_source_settings_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    password_file: /root/.config/restic/docker-prod.password
    repository_path: docker-prod
    rest_server_username: docker-prod
    rest_server_password_file: /root/.config/restic/docker-prod.rest-server-password
    host_paths:
      - /etc/remote-static
    exclude_paths:
      - /srv/remote-cache
    exclude_volumes:
      - remote_global_cache
    projects:
      pbs:
        extra_paths:
          - /srv/pbs-extra
        exclude_volumes:
          - pbs_backups
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
            self.assertEqual(remote.rest_server_username, "docker-prod")
            self.assertEqual(
                remote.rest_server_password_file,
                Path("/root/.config/restic/docker-prod.rest-server-password"),
            )
            self.assertEqual(remote.host_paths, (Path("/etc/remote-static"),))
            self.assertEqual(remote.exclude_paths, (Path("/srv/remote-cache"),))
            self.assertEqual(remote.exclude_volumes, ("remote_global_cache",))
            self.assertEqual(remote.projects["pbs"].extra_paths, (Path("/srv/pbs-extra"),))
            self.assertEqual(remote.projects["pbs"].exclude_volumes, ("pbs_backups",))
            self.assertEqual(remote.source_command, ("sudo", "backupdock"))
            self.assertEqual(remote.ssh_options, ("-i", "/root/.ssh/backupdock"))
            self.assertEqual(remote.local_rest_server_port, 8100)
            self.assertEqual(remote.remote_tunnel_port, 18100)

    def test_local_and_remote_project_settings_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
backup:
  exclude_volumes:
    - local-volume
projects:
  app:
    exclude_volumes:
      - local-app-volume
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    password_file: /tmp/repository-password
    repository_path: docker-prod
    rest_server_username: docker-prod
    rest_server_password_file: /tmp/rest-server-password
    exclude_volumes:
      - remote-volume
    projects:
      app:
        exclude_volumes:
          - remote-app-volume
""".lstrip(),
                encoding="utf-8",
            )

            config = load_config(config_path)
            remote = config.remotes["docker-prod"]

            self.assertEqual(config.backup.exclude_volumes, ("local-volume",))
            self.assertEqual(config.projects["app"].exclude_volumes, ("local-app-volume",))
            self.assertEqual(remote.exclude_volumes, ("remote-volume",))
            self.assertEqual(remote.projects["app"].exclude_volumes, ("remote-app-volume",))

    def test_remote_requires_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    repository_path: docker-prod
    rest_server_username: docker-prod
    rest_server_password_file: /root/.config/restic/docker-prod.rest-server-password
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "password_file"):
                load_config(config_path)

    def test_remote_requires_rest_server_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                """
remotes:
  docker-prod:
    ssh_target: root@docker-prod.example
    password_file: /root/.config/restic/docker-prod.password
    repository_path: docker-prod
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "rest_server_password_file"):
                load_config(config_path)

    def test_empty_yaml_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("", encoding="utf-8")

            config = load_config(config_path)

            self.assertEqual(config.backup.state_dir, Path("/var/lib/backupdock"))
            self.assertTrue(config.backup.include_compose_metadata)
            self.assertEqual(config.backup.exclude_volumes, ())
            self.assertEqual(config.projects, {})
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

            self.assertEqual(config.backup.state_dir, Path("/var/lib/backupdock"))
            self.assertTrue(config.backup.include_compose_metadata)
            self.assertIn("warning", stderr.getvalue().lower())
            self.assertIn(str(missing_config), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
