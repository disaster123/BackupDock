from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backupdock.config import load_config


class ConfigTests(unittest.TestCase):
    def test_volume_exclusions_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                """
[backup]
exclude_volumes = ["global_cache"]

[projects."pbs"]
exclude_volumes = ["pbs_backups"]
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.backup.exclude_volumes, ("global_cache",))
            self.assertEqual(config.projects["pbs"].exclude_volumes, ("pbs_backups",))


if __name__ == "__main__":
    unittest.main()
