from __future__ import annotations

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

    def test_empty_yaml_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("", encoding="utf-8")

            config = load_config(config_path)

            self.assertEqual(config.backup.stop_timeout_seconds, 30)
            self.assertEqual(config.backup.exclude_volumes, ())

    def test_invalid_boolean_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "backup:\n  include_compose_metadata: yes-please\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_config(config_path)

    def test_legacy_toml_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yaml_path = root / "config.yaml"
            toml_path = root / "config.toml"
            toml_path.write_text("[backup]\n", encoding="utf-8")

            with (
                patch("backupdock.config.DEFAULT_CONFIG_PATH", yaml_path),
                patch("backupdock.config.LEGACY_CONFIG_PATH", toml_path),
            ):
                with self.assertRaisesRegex(RuntimeError, "Legacy configuration"):
                    load_config()


if __name__ == "__main__":
    unittest.main()
