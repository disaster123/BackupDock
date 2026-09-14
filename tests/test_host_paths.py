from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from backupdock.backup import BackupOrchestrator
from backupdock.config import AppConfig, BackupConfig
from backupdock.discovery import host_sources
from backupdock.models import BackupGroup, BackupSource, ContainerInfo


class HostPathTests(unittest.TestCase):
    def test_explicit_host_root_is_preserved_for_all_container_overlap_shapes(self):
        root = Path("/example/config")
        config = AppConfig(backup=BackupConfig(host_paths=(root,)))
        for data_path in [root, root / "app" / "settings.json", root.parent]:
            with self.subTest(data_path=data_path):
                group = BackupGroup("compose:app", "app", "app", [], [BackupSource(data_path, "bind")])
                sources = host_sources(config, [group])
                self.assertEqual(sources, [BackupSource(root, "host", required=True)])

    def test_full_host_root_reaches_backup_without_implicit_container_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "compose.yaml").write_text("services: {}")
            mounted = root / "app" / "settings.json"
            mounted.parent.mkdir()
            mounted.write_text("container configuration")
            config = AppConfig(backup=BackupConfig(host_paths=(root,)))
            group = BackupGroup("compose:app", "app", "app", [], [BackupSource(mounted, "bind")])
            captured = {}
            class Restic:
                def backup(self, paths, group_key):
                    captured["group"] = group_key
                    captured["paths"] = paths
                    captured["files"] = {str(file.relative_to(root)): file.read_text() for path in paths for file in path.rglob("*") if file.is_file()}
            docker = MagicMock()
            with contextlib.redirect_stdout(io.StringIO()):
                BackupOrchestrator(docker, Restic(), config).backup_host_paths(host_sources(config, [group]))
            self.assertEqual(captured["group"], "host")
            self.assertEqual(captured["paths"], [root])
            self.assertEqual(set(captured["files"]), {"compose.yaml", "app/settings.json"})
            docker.stop.assert_not_called()

    def test_explicit_configured_path_exclusion_is_still_respected(self):
        root = Path("/example/config")
        config = AppConfig(backup=BackupConfig(host_paths=(root,), exclude_paths=(root,)))
        self.assertEqual(host_sources(config, []), [])

    def test_ignored_writer_does_not_block_explicit_live_host_backup(self):
        root = Path("/example/config")
        writer = ContainerInfo("id-worker", "worker", True, "worker", "worker", None, (), (), (), ignored=True)
        group = BackupGroup("compose:worker", "worker", "worker", [writer], [], ignored_sources=[BackupSource(root / "settings.json", "bind", container="worker")])
        config = AppConfig(backup=BackupConfig(host_paths=(root,)))
        orchestrator = BackupOrchestrator(MagicMock(), MagicMock(), config)
        orchestrator._validate_safety([group], host_sources(config, [group]), [group])


if __name__ == "__main__":
    unittest.main()
